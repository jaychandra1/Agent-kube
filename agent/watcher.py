"""Background watcher: finds incidents in the cluster and sends Teams alerts.

Every WATCH_INTERVAL_SECONDS it calls tools.detect_incidents and compares the result
with the incidents it already knows about:

* new incident      -> RCA (AI model while under MAX_LLM_RCA_PER_HOUR, else rules) -> alert card
* still open        -> a short reminder every ALERT_COOLDOWN_MINUTES
* gone for RESOLVE_AFTER_MINUTES -> "Resolved" card (the delay stops a crash-looping
  pod, which looks healthy between restarts, from flapping between open and resolved)
* first scan after start -> one summary card instead of one alert per ongoing incident,
  so restarts and upgrades of the agent don't flood the chat.

The scan itself only reads the cluster and updates this bookkeeping. RCAs and Teams posts run
one at a time on a separate worker, so a slow AI model or a Teams outage never delays the
next scan. Cards no Teams target accepted are retried on later scans. When part of the cluster
couldn't be read, incidents there are kept open instead of being reported as resolved.

State is kept in memory only, so the agent never writes to the cluster.
"""
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import agent
import cards
import teams
import tools

logger = logging.getLogger("kubesupport.watcher")

MAX_AI_BACKLOG = 5      # new incidents waiting for an AI RCA beyond this get a rule-based RCA right away
OUTBOX_SIZE = 50        # cards kept for a retry while Teams is unreachable


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Watcher:
    def __init__(self, interval=60.0, cooldown_min=60.0, resolve_after_min=10.0, pending_grace_min=5.0,
                 max_llm_per_hour=10, detect=None, rca=None, notify=None, clock=time.time, background=False):
        self.interval = interval
        self.cooldown = cooldown_min * 60
        self.resolve_after = resolve_after_min * 60
        self.pending_grace_min = pending_grace_min
        self.max_llm_per_hour = int(max_llm_per_hour)
        self._detect = detect or (lambda prev: tools.detect_incidents(
            prev, recent_seconds=max(180.0, 3 * self.interval), pending_grace_minutes=self.pending_grace_min))
        self._rca = rca or self._default_rca
        self._notify = notify or teams.post_alert
        self._clock = clock
        # background=True (in the pod): RCAs and posts on one worker thread; tests run them inline
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alerts") if background else None
        self.open: dict[str, dict] = {}       # key -> {"incident", "first_seen", "last_seen", "last_alerted"}
        self.restarts: dict[str, int] = {}
        self.started = False
        self.running = False
        self.run_started = None
        self.last_tick = None
        self.last_error = None
        self.unchecked: set = set()
        self.outbox = deque(maxlen=OUTBOX_SIZE)
        self.ai_backlog = 0
        self._llm_calls = deque()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        return cls(
            interval=_env_float("WATCH_INTERVAL_SECONDS", 60),
            cooldown_min=_env_float("ALERT_COOLDOWN_MINUTES", 60),
            resolve_after_min=_env_float("RESOLVE_AFTER_MINUTES", 10),
            pending_grace_min=_env_float("PENDING_GRACE_MINUTES", 5),
            max_llm_per_hour=_env_float("MAX_LLM_RCA_PER_HOUR", 10),
            background=True,
        )

    # ─── RCA with an hourly AI-model budget ─────────────────────────────────
    def _llm_budget_left(self, now: float) -> bool:
        while self._llm_calls and now - self._llm_calls[0] > 3600:
            self._llm_calls.popleft()
        return len(self._llm_calls) < self.max_llm_per_hour

    def _default_rca(self, incident: dict, use_llm: bool) -> dict | None:
        where = f" in namespace {incident['namespace']}" if incident.get("namespace") else ""
        question = (f"The cluster watcher detected '{incident['reason']}' for {incident['workload']}{where}. "
                    "Investigate and produce an RCA.")
        return agent.investigate_and_rca(question, target=incident, use_llm=use_llm)

    def _rca_for(self, incident: dict, now: float, allow_llm: bool = True) -> dict | None:
        with self._lock:
            use_llm = allow_llm and agent.llm is not None and self._llm_budget_left(now)
            if use_llm:
                self._llm_calls.append(now)
        try:
            return self._rca(incident, use_llm)
        except Exception as e:
            logger.warning("RCA for %s failed: %s", incident["key"], e)
            return None

    # ─── Sending ────────────────────────────────────────────────────────────
    def _send(self, card: dict):
        """Post a card; keep it for a later retry when no Teams target accepted it."""
        try:
            results = self._notify(card)
        except Exception as e:
            logger.warning("Posting a card failed: %s", type(e).__name__)
            results = [{"ok": False}]
        if isinstance(results, list) and results and not any(r.get("ok") for r in results):
            self.outbox.append(card)

    def _retry_outbox(self):
        for _ in range(len(self.outbox)):
            self._send(self.outbox.popleft())

    def _alert_new(self, incident: dict, now: float, allow_llm: bool):
        try:
            self._send(cards.alert_card(incident, self._rca_for(incident, now, allow_llm)))
        finally:
            with self._lock:
                self.ai_backlog = max(0, self.ai_backlog - 1)

    def _submit(self, fn, *args):
        if self._worker is None:
            fn(*args)
            return

        def safe():
            try:
                fn(*args)
            except Exception:
                logger.exception("Sending an alert failed")
        self._worker.submit(safe)

    # ─── One scan ───────────────────────────────────────────────────────────
    @staticmethod
    def _unverified(incident: dict, unchecked: set) -> bool:
        """True when this scan couldn't look at the incident's part of the cluster."""
        reason = incident.get("reason")
        if reason == "NodeNotReady":
            return "nodes" in unchecked
        if reason == "CannotCreatePods":
            return "events" in unchecked
        if reason == "JobFailed":
            return "jobs" in unchecked
        return "pods:*" in unchecked or f"pods:{incident.get('namespace')}" in unchecked

    def tick(self):
        now = self._clock()
        result = self._detect(self.restarts)
        found, self.restarts = result[0], result[1]
        unchecked = set(result[2]) if len(result) > 2 else set()
        self._submit(self._retry_outbox)
        with self._lock:
            self.unchecked = unchecked
            if not self.started:
                for key, inc in found.items():
                    self.open[key] = {"incident": inc, "first_seen": now, "last_seen": now, "last_alerted": now}
                self.started = True
                self.last_tick = now
                logger.info("Watcher started with %d ongoing incident(s)", len(found))
                self._submit(self._send, cards.startup_card(list(found.values())))
                return

            new, reminders = [], []
            for key, inc in found.items():
                entry = self.open.get(key)
                if entry is None:
                    self.open[key] = {"incident": inc, "first_seen": now, "last_seen": now, "last_alerted": now}
                    new.append(inc)
                else:
                    entry["incident"], entry["last_seen"] = inc, now
                    if now - entry["last_alerted"] >= self.cooldown:
                        entry["last_alerted"] = now
                        reminders.append(inc)

            resolved = []
            for key, entry in list(self.open.items()):
                if key in found:
                    continue
                if self._unverified(entry["incident"], unchecked):
                    entry["last_seen"] = now  # couldn't look: keep it open rather than call it resolved
                    continue
                if now - entry["last_seen"] >= self.resolve_after:
                    resolved.append((entry["incident"], int((now - entry["first_seen"]) // 60)))
                    del self.open[key]
            self.last_tick = now

        for inc in new:
            logger.info("New incident %s", inc["key"])
            with self._lock:
                allow_llm = self.ai_backlog < MAX_AI_BACKLOG
                self.ai_backlog += 1
            self._submit(self._alert_new, inc, now, allow_llm)
        for inc in reminders:
            self._submit(self._send, cards.alert_card(inc, None, reminder=True))
        for inc, minutes in resolved:
            logger.info("Resolved incident %s", inc["key"])
            self._submit(self._send, cards.resolved_card(inc, minutes))

    # ─── Thread control ─────────────────────────────────────────────────────
    def run(self):
        self.running = True
        self.run_started = self._clock()
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as e:  # keep watching even if one scan fails
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("Watcher tick failed")
            self._stop.wait(self.interval)
        self.running = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def healthy(self) -> tuple[bool, str]:
        """Liveness: False when the scan loop has stopped making progress (e.g. a hung API call)."""
        if not self._thread:
            return True, "watcher not started"
        if not self._thread.is_alive() and not self._stop.is_set():
            return False, "watcher thread stopped"
        limit = max(5 * self.interval, 300.0)
        since = self.last_tick if self.last_tick is not None else self.run_started
        if since is not None and self._clock() - since > limit:
            return False, f"no completed scan for {int(self._clock() - since)} s"
        return True, "ok"

    def current_incidents(self) -> list | None:
        """Open incidents, or None when the watcher isn't running (callers then scan live)."""
        if not self.running or not self.started:
            return None
        with self._lock:
            return [e["incident"] for e in self.open.values()]

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "started": self.started,
                "interval_seconds": self.interval,
                "last_tick_epoch": self.last_tick,
                "last_error": self.last_error,
                "unchecked": sorted(self.unchecked),
                "open_incidents": [e["incident"] for e in self.open.values()],
                "llm_rcas_last_hour": len(self._llm_calls),
                "llm_rca_budget_per_hour": self.max_llm_per_hour,
                "ai_backlog": self.ai_backlog,
                "cards_waiting_for_retry": len(self.outbox),
                "alert_targets": [name for name, _ in teams.ALERT_TARGETS],
            }


WATCHER = Watcher.from_env()
