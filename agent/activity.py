"""Cluster activity feed: who did what, batched into one Teams card per minute.

The API server calls POST /admission (HTTPS, 8443) for the requests matched in
deploy/activity-webhook.yaml. We always answer "allowed" and only keep a short event, so
nothing is ever blocked (failurePolicy is Ignore as well, in case we're down).

For Secrets and ConfigMaps only the name is kept, never the object body.
"""
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import cards
import teams
from tools import redact

logger = logging.getLogger("kubesupport.activity")


def _list(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


DEFAULT_IGNORE_USERS = ("system:node:,system:kube-,system:apiserver,system:serviceaccount:kube-system:,"
                        "eks:,system:serviceaccount:kubesupport:")
ENABLED = os.getenv("ACTIVITY_ENABLED", "true").strip().lower() in ("1", "true", "yes")
BATCH_SECONDS = _float(os.getenv("ACTIVITY_BATCH_SECONDS", "60"), 60.0)
IGNORE_USERS = _list(os.getenv("ACTIVITY_IGNORE_USERS", DEFAULT_IGNORE_USERS))
IGNORE_NAMESPACES = set(_list(os.getenv("ACTIVITY_IGNORE_NAMESPACES", "kube-node-lease")))
TLS_DIR = os.getenv("WEBHOOK_TLS_DIR", "/etc/kubesupport/webhook-tls")
TARGETS = teams.parse_targets(os.getenv("TEAMS_ACTIVITY_WEBHOOK_URLS", ""), "activity") or teams.ALERT_TARGETS
MAX_CARD_LINES = 30
MAX_PENDING = 2000      # cap between two cards, anything above is counted and dropped
SERVER_ERROR = None     # set by main.py if the 8443 server didn't come up

CONNECT_ACTIONS = {"exec": "exec", "attach": "attach", "portforward": "port-forward"}
RBAC = {"roles", "rolebindings", "clusterroles", "clusterrolebindings"}
SECURITY = RBAC | {"serviceaccounts", "secrets", "configmaps"}
WORKLOADS = {"deployments", "statefulsets", "daemonsets"}
SCALE_KINDS = {"deployments": "Deployment", "statefulsets": "StatefulSet", "replicasets": "ReplicaSet"}
CATEGORY_TITLES = [("exec", "Exec / attach / port-forward into pods"), ("security", "Security changes"),
                   ("change", "Created / deleted"), ("update", "Workload updates")]


def tls_files() -> tuple[str, str] | None:
    """Cert and key paths from the kubesupport-webhook-tls mount, None if they aren't there."""
    cert, key = os.path.join(TLS_DIR, "tls.crt"), os.path.join(TLS_DIR, "tls.key")
    return (cert, key) if os.path.isfile(cert) and os.path.isfile(key) else None


# --- AdmissionReview -> event ---

def _name_of(obj) -> str:
    return ((obj or {}).get("metadata") or {}).get("name") or ""


def _images(obj) -> dict:
    pod_spec = ((((obj or {}).get("spec") or {}).get("template") or {}).get("spec")) or {}
    containers = (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or [])
    return {c.get("name"): c.get("image") for c in containers}


def _workload_changes(old, new) -> list[str]:
    """Image / replica diff between the old and new workload."""
    changes = []
    old_images, new_images = _images(old), _images(new)
    for container, image in new_images.items():
        if container not in old_images:
            changes.append(f"container {container} added ({image})")
        elif old_images[container] != image:
            changes.append(f"image {container}: {old_images[container]} → {image}")
    old_r, new_r = ((old or {}).get("spec") or {}).get("replicas"), ((new or {}).get("spec") or {}).get("replicas")
    if new_r is not None and old_r != new_r:
        changes.append(f"replicas {old_r} → {new_r}")
    return changes


def parse_admission(review: dict) -> dict | None:
    """Event for one AdmissionReview, or None if it's filtered or not worth reporting.
    Only names and the user are copied out, never the object body."""
    req = review.get("request") or {}
    if req.get("dryRun"):
        return None
    user = (req.get("userInfo") or {}).get("username", "") or "unknown"
    if any(user.startswith(prefix) for prefix in IGNORE_USERS):
        return None
    namespace = req.get("namespace") or ""
    if namespace in IGNORE_NAMESPACES:
        return None
    operation = req.get("operation", "")
    resource = (req.get("resource") or {}).get("resource", "")
    sub = req.get("subResource") or ""
    kind = (req.get("kind") or {}).get("kind", "")
    name = req.get("name") or _name_of(req.get("object")) or _name_of(req.get("oldObject"))
    event = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user,
             "namespace": namespace, "name": name, "operation": operation}

    if operation == "CONNECT" and resource == "pods" and sub in CONNECT_ACTIONS:
        options = req.get("object") or {}
        event.update(category="exec", action=CONNECT_ACTIONS[sub], kind="Pod",
                     container=options.get("container") or "",
                     command=redact(" ".join(options.get("command") or []))[:200],
                     ports=",".join(str(p) for p in options.get("ports") or []))
        return event

    if sub == "scale" and operation == "UPDATE":
        old_r = ((req.get("oldObject") or {}).get("spec") or {}).get("replicas")
        new_r = ((req.get("object") or {}).get("spec") or {}).get("replicas")
        if old_r == new_r:
            return None
        event.update(category="update", action="scaled", kind=SCALE_KINDS.get(resource, kind),
                     detail=f"{old_r} → {new_r}")
        return event
    if sub:
        return None

    if operation == "UPDATE":
        if resource in WORKLOADS:
            changes = _workload_changes(req.get("oldObject"), req.get("object"))
            if not changes:
                return None  # restart annotation, no-op apply, ...
            event.update(category="update", action="updated", kind=kind, detail="; ".join(changes)[:400])
            return event
        if resource in RBAC:
            event.update(category="security", action="changed", kind=kind)
            return event
        return None

    action = {"CREATE": "created", "DELETE": "deleted"}.get(operation)
    if not action:
        return None
    event.update(category="security" if resource in SECURITY else "change", action=action, kind=kind)
    return event


def describe(event: dict) -> str:
    """One line for the card, like 'alice@corp exec into pod payments/api-1 (container app): sh'"""
    where = f"{event['namespace']}/{event['name']}" if event.get("namespace") else event.get("name", "")
    who = event.get("user", "unknown")
    if event["category"] == "exec":
        line = f"{who} {event['action']} into pod {where}"
        if event.get("container"):
            line += f" (container {event['container']})"
        if event.get("command"):
            line += f": `{event['command']}`"
        if event.get("ports"):
            line += f" ports {event['ports']}"
        return line
    if event["action"] in ("updated", "scaled"):
        return f"{event['action'].capitalize()} {event['kind']} {where} {event.get('detail', '')} by {who}"
    return f"{event['action'].capitalize()} {event['kind']} {where} by {who}"


# --- batching ---

class ActivityFeed:
    def __init__(self, batch_seconds=BATCH_SECONDS, notify=None, clock=time.time):
        self.batch_seconds = batch_seconds
        self._notify = notify or (lambda card: teams.post_card(TARGETS, card) if TARGETS else [])
        self._clock = clock
        self._lock = threading.Lock()
        self.pending: list[dict] = []
        self.recent = deque(maxlen=500)
        self.calls_total = 0
        self.events_total = 0
        self.dropped_total = 0
        self.last_call = None
        self.last_sent = None
        self._stop = threading.Event()
        self._thread = None

    def seen_call(self):
        with self._lock:
            self.calls_total += 1
            self.last_call = self._clock()

    def record(self, event: dict):
        with self._lock:
            self.events_total += 1
            self.recent.append(event)
            if len(self.pending) >= MAX_PENDING:
                self.dropped_total += 1
                return
            self.pending.append(event)

    def flush(self) -> dict | None:
        """Send what was collected since the last flush as one card."""
        with self._lock:
            events, self.pending = self.pending, []
        if not events:
            return None
        order = {cat: i for i, (cat, _) in enumerate(CATEGORY_TITLES)}
        events.sort(key=lambda e: order.get(e["category"], 99))
        shown = events[:MAX_CARD_LINES]
        groups = [(title, [describe(e) for e in shown if e["category"] == cat]) for cat, title in CATEGORY_TITLES]
        card = cards.activity_card([g for g in groups if g[1]], total=len(events),
                                   more=len(events) - len(shown),
                                   highlight=any(e["category"] in ("exec", "security") for e in events),
                                   window_seconds=int(self.batch_seconds))
        self._notify(card)
        self.last_sent = self._clock()
        return card

    def run(self):
        while not self._stop.wait(self.batch_seconds):
            try:
                self.flush()
            except Exception:
                logger.exception("Sending the activity card failed")
        self.flush()  # last batch before shutdown

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="activity", daemon=True)
        self._thread.start()

    def stop(self, wait_seconds: float = 10.0):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=wait_seconds)

    def latest(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self.recent)[-n:]

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": ENABLED,
                "webhook_certificate": tls_files() is not None,
                "webhook_server_error": SERVER_ERROR,
                "events_dropped_total": self.dropped_total,
                "webhook_calls_total": self.calls_total,
                "last_webhook_call_epoch": self.last_call,
                "events_total": self.events_total,
                "pending": len(self.pending),
                "last_card_sent_epoch": self.last_sent,
                "batch_seconds": self.batch_seconds,
                "targets": [label for label, _ in TARGETS],
            }


FEED = ActivityFeed()


# --- webhook app (8443) ---

webhook_app = FastAPI(title="kubesupport activity webhook", docs_url=None, redoc_url=None, openapi_url=None)


@webhook_app.post("/admission")
async def admission(request: Request):
    """Always allows. Records the request if it's one we report."""
    uid = ""
    try:
        review = await request.json()
        uid = (review.get("request") or {}).get("uid", "")
        FEED.seen_call()
        event = parse_admission(review)
        if event:
            FEED.record(event)
    except Exception as e:  # we must never be the reason an API request fails
        logger.warning("Could not read an admission request: %s", type(e).__name__)
    return JSONResponse({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                         "response": {"uid": uid, "allowed": True}})
