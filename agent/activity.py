"""Cluster activity feed: who did what, batched into one Teams card per minute.

The API server calls POST /admission (HTTPS, 8443) for the requests matched in
deploy/activity-webhook.yaml. We always answer "allowed" and only keep a short event, so
nothing is ever blocked (failurePolicy is Ignore as well, in case we're down).

Reported: exec / attach / port-forward / debug containers, rollout restarts and every other
change to a workload, scaling, Helm releases, ConfigMap and Secret changes, pods deleted or
evicted by hand, node cordons, RBAC, and created / changed / deleted objects of every other
kind the webhook rules send us.

An event only says which object, which fields or keys changed, and who did it. Values are
never copied out of an object. For a Secret we don't even keep the key names.
"""
import logging
import os
import re
import threading
import time
from collections import Counter, deque
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
IGNORE_KINDS = {k.lower() for k in _list(os.getenv("ACTIVITY_IGNORE_KINDS", ""))}   # e.g. "Pod,Job"
TLS_DIR = os.getenv("WEBHOOK_TLS_DIR", "/etc/kubesupport/webhook-tls")
TARGETS = teams.parse_targets(os.getenv("TEAMS_ACTIVITY_WEBHOOK_URLS", ""), "activity") or teams.ALERT_TARGETS
MAX_CARD_LINES = 30
MAX_PENDING = 2000      # cap between two cards, anything above is counted and dropped
MAX_DETAIL = 400
SERVER_ERROR = None     # set by main.py if the 8443 server didn't come up

CONNECT_ACTIONS = {"exec": "exec", "attach": "attach", "portforward": "port-forward"}
RBAC = {"roles", "rolebindings", "clusterroles", "clusterrolebindings"}
SECURITY = RBAC | {"serviceaccounts", "networkpolicies", "customresourcedefinitions"}
CONFIG = {"configmaps", "secrets"}
WORKLOADS = {"deployments", "statefulsets", "daemonsets"}
ROLLOUT = WORKLOADS | {"jobs", "cronjobs"}
SCALE_KINDS = {"deployments": "Deployment", "statefulsets": "StatefulSet", "replicasets": "ReplicaSet"}
# order = order on the card
CATEGORY_TITLES = [("exec", "Exec / attach / port-forward into pods"),
                   ("security", "Security changes"),
                   ("config", "ConfigMaps and Secrets"),
                   ("rollout", "Deployments, restarts and scaling"),
                   ("pods", "Pods deleted, evicted or started by hand"),
                   ("node", "Nodes"),
                   ("change", "Created / deleted"),
                   ("update", "Other changes")]

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"     # what `kubectl rollout restart` sets on the pod template
# these change with every apply or on their own, they say nothing about what somebody did
NOISY_ANNOTATIONS = {"kubectl.kubernetes.io/last-applied-configuration", "deployment.kubernetes.io/revision",
                     "control-plane.alpha.kubernetes.io/leader", "endpoints.kubernetes.io/last-change-trigger-time",
                     "meta.helm.sh/release-name", "meta.helm.sh/release-namespace"}
DATA_SECTIONS = ("data", "binaryData", "stringData")
HELM_RELEASE = re.compile(r"^sh\.helm\.release\.v1\.(.+)\.v(\d+)$")   # helm keeps each revision in a Secret


def tls_files() -> tuple[str, str] | None:
    """Cert and key paths from the kubesupport-webhook-tls mount, None if they aren't there."""
    cert, key = os.path.join(TLS_DIR, "tls.crt"), os.path.join(TLS_DIR, "tls.key")
    return (cert, key) if os.path.isfile(cert) and os.path.isfile(key) else None


# --- comparing two versions of an object (names only, never values) ---

def _meta(obj, key: str) -> dict:
    return ((obj or {}).get("metadata") or {}).get(key) or {}


def _name_of(obj) -> str:
    meta = (obj or {}).get("metadata") or {}
    if meta.get("name"):
        return meta["name"]
    return f"{meta['generateName']}*" if meta.get("generateName") else ""   # name isn't generated yet at admission


def _changed_keys(old, new) -> list[str]:
    old, new = old or {}, new or {}
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _some(names: list, limit: int = 6) -> str:
    return ", ".join(names[:limit]) + (f" +{len(names) - limit} more" if len(names) > limit else "")


def _template(obj) -> dict:
    return ((obj or {}).get("spec") or {}).get("template") or {}


def _containers(obj) -> dict:
    spec = _template(obj).get("spec") or {}
    return {c.get("name"): c for c in (spec.get("initContainers") or []) + (spec.get("containers") or [])}


def _env(container: dict) -> dict:
    # only used to see which variables differ, the values never leave this function
    return {e.get("name"): (e.get("value"), e.get("valueFrom")) for e in container.get("env") or []}


def _resource_changes(old: dict, new: dict) -> str:
    parts = []
    for section in ("requests", "limits"):
        was, now = (old or {}).get(section) or {}, (new or {}).get(section) or {}
        parts += [f"{k} {section[:-1]} {was.get(k, '-')} → {now.get(k, '-')}" for k in _changed_keys(was, now)]
    return ", ".join(parts)


def _restarted(old, new) -> bool:
    stamp = _meta(_template(new), "annotations").get(RESTARTED_AT)
    return bool(stamp) and stamp != _meta(_template(old), "annotations").get(RESTARTED_AT)


def _workload_changes(old, new) -> list[str]:
    """What changed between two versions of a Deployment / StatefulSet / DaemonSet, apart
    from a rollout restart."""
    changes = []
    was, now = _containers(old), _containers(new)
    for name, c in now.items():
        if name not in was:
            changes.append(f"container {name} added ({c.get('image')})")
            continue
        before = was[name]
        if before.get("image") != c.get("image"):
            changes.append(f"image {name}: {before.get('image')} → {c.get('image')}")
        env = _changed_keys(_env(before), _env(c))
        if env or before.get("envFrom") != c.get("envFrom"):
            changes.append(f"env {name}: {_some(env) or 'envFrom'}")
        if before.get("resources") != c.get("resources"):
            changes.append(f"resources {name}: {_resource_changes(before.get('resources'), c.get('resources'))}")
        other = [k for k in _changed_keys(before, c) if k not in ("image", "env", "envFrom", "resources")]
        if other:
            changes.append(f"{name}: {_some(other)} changed")
    changes += [f"container {name} removed" for name in was if name not in now]

    old_spec, new_spec = (old or {}).get("spec") or {}, (new or {}).get("spec") or {}
    if new_spec.get("replicas") is not None and old_spec.get("replicas") != new_spec.get("replicas"):
        changes.append(f"replicas {old_spec.get('replicas')} → {new_spec.get('replicas')}")

    pod = [k for k in _changed_keys(_template(old).get("spec"), _template(new).get("spec"))
           if k not in ("containers", "initContainers")]
    if pod:
        changes.append(f"pod spec: {_some(pod)} changed")   # volumes, nodeSelector, tolerations, serviceAccountName, ...
    if _meta(_template(old), "labels") != _meta(_template(new), "labels"):
        changes.append("pod labels changed")
    notes = [k for k in _changed_keys(_meta(_template(old), "annotations"), _meta(_template(new), "annotations"))
             if k != RESTARTED_AT]
    if notes:
        changes.append(f"pod annotations: {_some(notes)}")

    for key in _changed_keys(old_spec, new_spec):
        if key == "paused":
            changes.append("rollout paused" if new_spec.get("paused") else "rollout resumed")
        elif key not in ("template", "replicas"):
            changes.append(f"{key} changed")   # strategy, selector, volumeClaimTemplates, ...
    return changes


def _object_changes(resource: str, old: dict, new: dict) -> list[str]:
    """Which parts of an object differ: field names, data keys, label and annotation keys.
    metadata (other than labels and annotations) and status are ignored."""
    changes, data_keys = [], set()
    for key in _changed_keys(old, new):
        if key in ("metadata", "status", "apiVersion", "kind"):
            continue
        was, now = old.get(key), new.get(key)
        if not isinstance(was, dict) and not isinstance(now, dict):
            changes.append(key)
            continue
        inner = _changed_keys(was if isinstance(was, dict) else {}, now if isinstance(now, dict) else {})
        if key in DATA_SECTIONS:
            data_keys.update(inner)
        else:
            changes += [f"{key}.{k}" for k in inner[:6]] + ([f"+{len(inner) - 6} more in {key}"] if len(inner) > 6 else [])
    if data_keys and resource == "secrets":
        changes.append(f"{len(data_keys)} key(s) changed")   # not even the key names for a Secret
    elif data_keys:
        changes.append(f"keys: {_some(sorted(data_keys), 8)}")
    labels = _changed_keys(_meta(old, "labels"), _meta(new, "labels"))
    if labels:
        changes.append(f"labels: {_some(labels)}")
    notes = [k for k in _changed_keys(_meta(old, "annotations"), _meta(new, "annotations")) if k not in NOISY_ANNOTATIONS]
    if notes:
        changes.append(f"annotations: {_some(notes)}")
    return changes


# --- AdmissionReview -> event ---

def _category(resource: str, default: str) -> str:
    if resource in SECURITY:
        return "security"
    if resource in CONFIG:
        return "config"
    if resource in ROLLOUT:
        return "rollout"
    return {"pods": "pods", "nodes": "node"}.get(resource, default)


def _pod_subresource(operation: str, sub: str, old, new) -> dict | None:
    if operation == "CONNECT" and sub in CONNECT_ACTIONS:
        options = new or {}
        return dict(category="exec", action=CONNECT_ACTIONS[sub], kind="Pod",
                    container=options.get("container") or "",
                    command=redact(" ".join(options.get("command") or []))[:200],
                    ports=",".join(str(p) for p in options.get("ports") or []))
    if sub == "eviction" and operation == "CREATE":     # kubectl drain
        return dict(category="pods", action="evicted", kind="Pod")
    if sub == "ephemeralcontainers" and operation == "UPDATE":     # kubectl debug
        known = {c.get("name") for c in ((old or {}).get("spec") or {}).get("ephemeralContainers") or []}
        added = [c for c in ((new or {}).get("spec") or {}).get("ephemeralContainers") or [] if c.get("name") not in known]
        c = added[-1] if added else {}
        return dict(category="exec", action="debug", kind="Pod", container=c.get("name") or "",
                    image=c.get("image") or "", command=redact(" ".join(c.get("command") or []))[:200])
    return None


def _updated(resource: str, kind: str, old, new, name: str) -> dict | None:
    if resource in WORKLOADS:
        changes = _workload_changes(old, new)
        if _restarted(old, new):
            if not changes:
                return dict(category="rollout", action="restarted", kind=kind, detail="rollout restart")
            changes.insert(0, "rollout restart")
        if not changes:
            return None  # only metadata or status moved
        return dict(category="rollout", action="updated", kind=kind, detail="; ".join(changes)[:MAX_DETAIL])
    if resource == "secrets" and HELM_RELEASE.match(name or ""):
        return None  # helm relabels its own history on every upgrade
    if old is None or new is None:
        changes = []    # nothing to compare with, report it without detail
    else:
        changes = _object_changes(resource, old, new)
        if not changes:
            return None  # no visible change (status, managedFields, noisy annotations)
    action = "changed"
    if resource == "nodes" and "spec.unschedulable" in changes:
        changes.remove("spec.unschedulable")
        action = "cordoned" if (new.get("spec") or {}).get("unschedulable") else "uncordoned"
    return dict(category=_category(resource, "update"), action=action, kind=kind,
                detail=", ".join(changes)[:MAX_DETAIL])


def _classify(operation: str, resource: str, sub: str, kind: str, old, new, name: str) -> dict | None:
    if resource == "pods" and sub:
        return _pod_subresource(operation, sub, old, new)
    if sub == "scale":
        was, now = ((old or {}).get("spec") or {}).get("replicas"), ((new or {}).get("spec") or {}).get("replicas")
        if operation != "UPDATE" or was == now:
            return None
        return dict(category="rollout", action="scaled", kind=SCALE_KINDS.get(resource, kind), detail=f"{was} → {now}")
    if sub:
        return None
    if operation == "UPDATE":
        return _updated(resource, kind, old, new, name)
    action = {"CREATE": "created", "DELETE": "deleted"}.get(operation)
    if not action:
        return None
    helm = HELM_RELEASE.match(name or "") if resource == "secrets" else None
    if helm:
        if operation != "CREATE":
            return None  # helm prunes old revisions by itself
        return dict(category="rollout", action="deployed", kind="Helm release", name=helm.group(1),
                    detail=f"revision {helm.group(2)}")
    found = dict(category=_category(resource, "change"), action=action, kind=kind)
    if resource == "pods" and operation == "CREATE":
        images = [c.get("image") for c in ((new or {}).get("spec") or {}).get("containers") or [] if c.get("image")]
        found["detail"] = _some(images, 3)
    return found


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
    old, new = req.get("oldObject"), req.get("object")
    name = req.get("name") or _name_of(new) or _name_of(old)
    found = _classify(operation, (req.get("resource") or {}).get("resource", ""), req.get("subResource") or "",
                      (req.get("kind") or {}).get("kind", ""), old, new, name)
    if not found or str(found.get("kind", "")).lower() in IGNORE_KINDS:
        return None
    return {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user,
            "namespace": namespace, "name": name, "operation": operation, **found}


def describe(event: dict) -> str:
    """One line for the card, like 'Restarted Deployment payments/api (rollout restart) by alice@corp'"""
    where = f"{event['namespace']}/{event['name']}" if event.get("namespace") else event.get("name", "")
    who = event.get("user", "unknown")
    action, kind, detail = event["action"], event.get("kind", ""), event.get("detail", "")
    if event["category"] == "exec":
        if action == "debug":
            line = f"{who} started a debug container in pod {where}"
            extra = ", ".join(x for x in (event.get("container"), event.get("image")) if x)
            line += f" ({extra})" if extra else ""
        else:
            line = f"{who} {action} into pod {where}"
            if event.get("container"):
                line += f" (container {event['container']})"
        if event.get("command"):
            line += f": `{event['command']}`"
        if event.get("ports"):
            line += f" ports {event['ports']}"
        return line
    if action == "scaled":
        return f"Scaled {kind} {where} {detail} by {who}"
    if action == "deployed":
        return f"Helm install/upgrade of {where} ({detail}) by {who}"
    if action == "evicted":   # we see the request before a PodDisruptionBudget can still refuse it
        return f"Eviction of {kind} {where} requested by {who}"
    return f"{action.capitalize()} {kind} {where}" + (f" ({detail})" if detail else "") + f" by {who}"


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
        groups = []
        for cat, title in CATEGORY_TITLES:
            # the same line several times (a script looping over something) becomes "... (x5)"
            lines = Counter(describe(e) for e in shown if e["category"] == cat)
            if lines:
                groups.append((title, [f"{line} (x{n})" if n > 1 else line for line, n in lines.items()]))
        card = cards.activity_card(groups, total=len(events), more=len(events) - len(shown),
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
