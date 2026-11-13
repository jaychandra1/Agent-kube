"""Background watcher: finds incidents in the cluster and sends Teams alerts.

Every WATCH_INTERVAL_SECONDS it calls tools.detect_incidents and compares the result
with the incidents it already knows about:

* new incident      -> RCA (Claude while under MAX_LLM_RCA_PER_HOUR, else rules) -> alert card
* still open        -> a short reminder every ALERT_COOLDOWN_MINUTES
* gone for RESOLVE_AFTER_MINUTES -> "Resolved" card (the delay stops a crash-looping
  pod, which looks healthy between restarts, from flapping between open and resolved)
* first scan after start -> one summary card instead of one alert per ongoing incident,
  so restarts and upgrades of the agent don't flood the chat.

State is kept in memory only, so the agent never writes to the cluster.
"""
import logging
import os
import threading
import time
from collections import deque

import agent
import cards
import teams
import tools

logger = logging.getLogger("kubesupport.watcher")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


class Watcher:
    def __init__(self, interval=60.0, cooldown_min=60.0, resolve_after_min=10.0, pending_grace_min=5.0,
                 max_llm_per_hour=10, detect=None, rca=None, notify=None, clock=time.time):
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
        self.open: dict[str, dict] = {}       # key -> {"incident", "first_seen", "last_seen", "last_alerted"}
        self.restarts: dict[str, int] = {}
        self.started = False
        self.running = False
        self.last_tick = None
        self.last_error = None
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
        )

    # ─── RCA with an hourly Claude budget ───────────────────────────────────
    def _llm_budget_left(self, now: float) -> bool:
        while self._llm_calls and now - self._llm_calls[0] > 3600:
            self._llm_calls.popleft()
        return len(self._llm_calls) < self.max_llm_per_hour

    def _default_rca(self, incident: dict, use_llm: bool) -> dict | None:
        if not incident.get("pod"):
            return None  # node-level incidents: the card shows the node condition
        question = (f"The cluster watcher detected '{incident['reason']}' for {incident['workload']} "
                    f"in namespace {incident['namespace']}. Investigate and produce an RCA.")
        return agent.investigate_and_rca(question, target=incident, use_llm=use_llm)

    def _rca_for(self, incident: dict, now: float) -> dict | None:
        use_llm = bool(agent.client) and self._llm_budget_left(now)
        if use_llm:
            self._llm_calls.append(now)
        try:
            return self._rca(incident, use_llm)
        except Exception as e:
            logger.warning("RCA for %s failed: %s", incident["key"], e)
            return None

    # ─── One scan ───────────────────────────────────────────────────────────
    def tick(self):
        now = self._clock()
        found, self.restarts = self._detect(self.restarts)
        with self._lock:
            if not self.started:
                for key, inc in found.items():
                    self.open[key] = {"incident": inc, "first_seen": now, "last_seen": now, "last_alerted": now}
                self.started = True
                self.last_tick = now
                logger.info("Watcher started with %d ongoing incident(s)", len(found))
                self._notify(cards.startup_card(list(found.values())))
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
                if key not in found and now - entry["last_seen"] >= self.resolve_after:
                    resolved.append((entry["incident"], int((now - entry["first_seen"]) // 60)))
                    del self.open[key]
            self.last_tick = now

        # Slow work (RCA, HTTP) happens outside the lock
        for inc in new:
            logger.info("New incident %s", inc["key"])
            self._notify(cards.alert_card(inc, self._rca_for(inc, now)))
        for inc in reminders:
            self._notify(cards.alert_card(inc, None, reminder=True))
        for inc, minutes in resolved:
            logger.info("Resolved incident %s", inc["key"])
            self._notify(cards.resolved_card(inc, minutes))

    # ─── Thread control ─────────────────────────────────────────────────────
    def run(self):
        self.running = True
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
                "open_incidents": [e["incident"] for e in self.open.values()],
                "llm_rcas_last_hour": len(self._llm_calls),
                "llm_rca_budget_per_hour": self.max_llm_per_hour,
                "alert_targets": [name for name, _ in teams.ALERT_TARGETS],
            }


WATCHER = Watcher.from_env()
