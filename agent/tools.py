"""Read-only Kubernetes tools the agent can call. Every tool is namespace-allowlisted,
logs are redacted, and all output is wrapped as untrusted data before the LLM sees it."""
import os, re, glob, json, logging
from datetime import datetime, timezone
from kubernetes import client, config

logger = logging.getLogger("kubesupport.tools")

def init_k8s_client():
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster kubernetes configuration")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded local kubeconfig")
        except Exception as e:
            logger.warning("Could not load in-cluster or local kubeconfig: %s. Kubernetes calls will fail if not configured.", e)

init_k8s_client()

core = client.CoreV1Api()
apps = client.AppsV1Api()
custom = client.CustomObjectsApi()

ALLOWED_RAW = os.getenv("ALLOWED_NAMESPACES", "default")
ALLOWED = {n.strip() for n in ALLOWED_RAW.split(",") if n.strip()}
RUNBOOK_DIR = os.getenv("RUNBOOK_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "runbooks"))

_KV = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\"?\s*[:=]\s*\"?)([^\s\",;]+)")
_OTHER = [re.compile(r"AKIA[0-9A-Z]{16}"),
          re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")]

def redact(text: str) -> str:
    if not text:
        return ""
    text = _KV.sub(r"\1\2[REDACTED]", text)
    for p in _OTHER:
        text = p.sub("[REDACTED]", text)
    return text

def _ns(ns: str):
    if "*" not in ALLOWED and ns not in ALLOWED:
        raise ValueError(f"namespace '{ns}' not allowed; allowed: {sorted(ALLOWED)}")

def _state(s):
    if s is None: return None
    if s.waiting: return {"waiting": s.waiting.reason, "message": s.waiting.message}
    if s.terminated: return {"terminated": s.terminated.reason, "exit_code": s.terminated.exit_code}
    if s.running: return {"running_since": str(s.running.started_at)}

def list_pods(namespace):
    _ns(namespace)
    now = datetime.now(timezone.utc)
    out = []
    pods = core.list_namespaced_pod(namespace).items
    for p in pods:
        cs = p.status.container_statuses or []
        out.append({
            "name": p.metadata.name,
            "phase": p.status.phase,
            "ready": f"{sum(1 for c in cs if c.ready)}/{len(cs)}",
            "restarts": sum(c.restart_count for c in cs),
            "waiting": [c.state.waiting.reason for c in cs if c.state and c.state.waiting],
            "age_min": int((now - p.metadata.creation_timestamp).total_seconds() / 60) if p.metadata.creation_timestamp else 0
        })
    return out

def describe_pod(namespace, pod):
    _ns(namespace)
    p = core.read_namespaced_pod(pod, namespace)
    spec = {c.name: c for c in p.spec.containers}
    cons = []
    for c in p.status.container_statuses or []:
        last = c.last_state.terminated if c.last_state else None
        res = spec[c.name].resources if c.name in spec else None
        cons.append({
            "name": c.name,
            "image": c.image,
            "ready": c.ready,
            "restarts": c.restart_count,
            "state": _state(c.state),
            "last_terminated": {
                "reason": last.reason,
                "exit_code": last.exit_code,
                "finished_at": str(last.finished_at)
            } if last else None,
            "requests": res.requests if res else {},
            "limits": res.limits if res else {}
        })
    return {
        "phase": p.status.phase,
        "node": p.spec.node_name,
        "containers": cons,
        "conditions": [{"type": c.type, "status": c.status, "reason": c.reason}
                       for c in (p.status.conditions or [])]
    }

def get_events(namespace, limit=30):
    _ns(namespace)
    evs = core.list_namespaced_event(namespace).items
    key = lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp or datetime.now(timezone.utc)
    evs.sort(key=key, reverse=True)
    return [{
        "time": str(key(e)),
        "type": e.type,
        "reason": e.reason,
        "count": e.count,
        "object": f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else "",
        "message": e.message
    } for e in evs[:min(int(limit), 60)]]

def get_pod_logs(namespace, pod, container=None, tail_lines=100, previous=False):
    _ns(namespace)
    try:
        logs = core.read_namespaced_pod_log(
            pod, namespace, container=container, previous=bool(previous),
            tail_lines=min(int(tail_lines), 300), timestamps=True
        )
    except client.ApiException as e:
        return f"log fetch failed: {e.status} {e.reason}"
    return redact(logs)[-12000:]

def get_deployment(namespace, name):
    _ns(namespace)
    d = apps.read_namespaced_deployment(name, namespace)
    sel = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
    rs = apps.list_namespaced_replica_set(namespace, label_selector=sel).items
    hist = sorted(({
        "revision": r.metadata.annotations.get("deployment.kubernetes.io/revision") if r.metadata.annotations else "unknown",
        "images": [c.image for c in r.spec.template.spec.containers],
        "created": str(r.metadata.creation_timestamp),
        "replicas": r.status.replicas
    } for r in rs), key=lambda x: x["created"], reverse=True)[:5]
    return {
        "desired": d.spec.replicas,
        "ready": d.status.ready_replicas,
        "available": d.status.available_replicas,
        "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                       for c in (d.status.conditions or [])],
        "rollout_history": hist
    }

def top_pods(namespace):
    _ns(namespace)
    try:
        m = custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")
    except Exception as e:
        return f"metrics unavailable: {e}"
    return [{
        "pod": i["metadata"]["name"],
        "containers": [{"name": c["name"], "cpu": c["usage"]["cpu"], "memory": c["usage"]["memory"]}
                       for c in i["containers"]]
    } for i in m.get("items", [])]

def search_runbooks(query):
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    scored = []
    for path in glob.glob(f"{RUNBOOK_DIR}/*.md"):
        try:
            text = open(path, encoding="utf-8").read()
            score = sum(text.lower().count(t) for t in terms)
            if score:
                scored.append((score, path, text[:3000]))
        except Exception:
            pass
    scored.sort(reverse=True)
    return [{"file": p, "content": t} for _, p, t in scored[:2]] or "no matching runbook"

def watched_namespaces_label() -> str:
    return "all namespaces" if "*" in ALLOWED else ", ".join(sorted(ALLOWED))


def pods_by_namespace() -> dict:
    """Pods in the watched namespaces: every namespace when ALLOWED_NAMESPACES is "*"."""
    if "*" in ALLOWED:
        out = {}
        for p in core.list_pod_for_all_namespaces().items:
            out.setdefault(p.metadata.namespace, []).append(p)
        return out
    out = {}
    for ns in sorted(ALLOWED):
        try:
            out[ns] = core.list_namespaced_pod(ns).items
        except Exception as e:
            logger.warning("Could not list pods in %s: %s", ns, e)
    return out


def scan_cluster_for_issues():
    """Scans all allowed namespaces and identifies any pods in CrashLoopBackOff, Error, OOMKilled, or high restart count."""
    issues = []
    for ns, pods in pods_by_namespace().items():
        try:
            for p in pods:
                cs = p.status.container_statuses or []
                total_restarts = sum(c.restart_count for c in cs)
                reasons = []
                last_reason = None
                last_exit = None

                for c in cs:
                    if c.state and c.state.waiting:
                        reasons.append(c.state.waiting.reason)
                    if c.last_state and c.last_state.terminated:
                        last_reason = c.last_state.terminated.reason
                        last_exit = c.last_state.terminated.exit_code

                is_problematic = (
                    p.status.phase in ["Failed", "Pending"] or
                    total_restarts > 0 or
                    any(r in ["CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff", "CreateContainerConfigError"] for r in reasons) or
                    last_reason in ["OOMKilled", "Error"]
                )

                if is_problematic:
                    issues.append({
                        "namespace": ns,
                        "pod": p.metadata.name,
                        "phase": p.status.phase,
                        "restarts": total_restarts,
                        "waiting_reasons": reasons,
                        "last_termination_reason": last_reason,
                        "last_exit_code": last_exit
                    })
        except Exception as e:
            logger.warning("Could not check pods in %s: %s", ns, e)
    return issues

# ─── Any object in the cluster (read-only, never Secrets) ───────────────────
# Uses the Kubernetes dynamic client, so every kind works: core objects, apps,
# networking, storage, RBAC, and custom resources (CRDs).

_dynamic = None
REFUSED_KINDS = {"secret", "secrets"}
MAX_LIST_ITEMS = 100


def _dyn():
    global _dynamic
    if _dynamic is None:
        from kubernetes import dynamic
        _dynamic = dynamic.DynamicClient(client.ApiClient())
    return _dynamic


def _resolve_kind(kind: str):
    """API resource for a kind, plural or short name: 'Service', 'services' or 'svc'."""
    k = (kind or "").strip().lower()
    if k in REFUSED_KINDS:
        raise ValueError("kubesupport never reads Secrets.")
    matches = []
    for r in _dyn().resources.search():
        name = getattr(r, "name", "") or ""
        if "/" in name or "list" not in (getattr(r, "verbs", None) or []):
            continue  # subresources and objects that can't be listed
        names = {r.kind.lower(), name.lower(), (getattr(r, "singular_name", "") or "").lower(),
                 *[s.lower() for s in (getattr(r, "short_names", None) or [])]}
        if k in names:
            matches.append(r)
    if not matches:
        raise ValueError(f"unknown kind '{kind}' (try e.g. services, ingresses, nodes, configmaps, jobs)")
    # Prefer the preferred API version, then the core group.
    matches.sort(key=lambda r: (not getattr(r, "preferred", False), bool(r.group), r.group or ""))
    if matches[0].kind.lower() == "secret":
        raise ValueError("kubesupport never reads Secrets.")
    return matches[0]


def _age_minutes(created) -> int | None:
    try:
        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
    except Exception:
        return None


def _summary(kind: str, obj: dict) -> str:
    """One line of status for common kinds; status.phase or ready conditions otherwise."""
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    if kind == "Service":
        ports = ",".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports") or [])
        return f"{spec.get('type', 'ClusterIP')} {spec.get('clusterIP', '')} ports {ports}".strip()
    if kind == "Ingress":
        hosts = [r.get("host", "*") for r in spec.get("rules") or []]
        return f"hosts {', '.join(hosts) or '*'}"
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        return f"ready {status.get('readyReplicas', 0)}/{spec.get('replicas', 0)}"
    if kind == "DaemonSet":
        return f"ready {status.get('numberReady', 0)}/{status.get('desiredNumberScheduled', 0)}"
    if kind == "Job":
        return f"succeeded {status.get('succeeded', 0)}, failed {status.get('failed', 0)}"
    if kind == "Node":
        ready = next((c.get("status") for c in status.get("conditions") or [] if c.get("type") == "Ready"), "?")
        return f"Ready={ready} kubelet {((status.get('nodeInfo') or {}).get('kubeletVersion', ''))}"
    if kind == "PersistentVolumeClaim":
        return f"{status.get('phase', '')} {((status.get('capacity') or {}).get('storage', ''))}".strip()
    if status.get("phase"):
        return str(status["phase"])
    true_conditions = [c.get("type") for c in status.get("conditions") or [] if c.get("status") == "True"]
    return ", ".join(true_conditions)


def list_resources(kind, namespace=None):
    """Objects of any kind (not Secrets), with name, namespace, age and a short status."""
    r = _resolve_kind(kind)
    if r.namespaced and namespace:
        _ns(namespace)
        results = [r.get(namespace=namespace)]
    elif r.namespaced and "*" not in ALLOWED:
        results = [r.get(namespace=ns) for ns in sorted(ALLOWED)]
    else:
        results = [r.get()]
    items = [item.to_dict() for res in results for item in (res.items or [])]
    rows = []
    for obj in items[:MAX_LIST_ITEMS]:
        meta = obj.get("metadata") or {}
        rows.append({"name": meta.get("name"), "namespace": meta.get("namespace"),
                     "age_min": _age_minutes(meta.get("creationTimestamp")),
                     "status": redact(_summary(r.kind, obj))})
    out = {"kind": r.kind, "api_version": getattr(r, "group_version", ""), "count": len(items), "items": rows}
    if len(items) > MAX_LIST_ITEMS:
        out["note"] = f"showing {MAX_LIST_ITEMS} of {len(items)}; narrow it with a namespace"
    return out


def get_resource(kind, name, namespace=None):
    """The full definition and status of one object of any kind (not Secrets), redacted."""
    r = _resolve_kind(kind)
    if r.namespaced:
        if not namespace:
            raise ValueError(f"{r.kind} is namespaced: give a namespace")
        _ns(namespace)
        obj = r.get(name=name, namespace=namespace).to_dict()
    else:
        obj = r.get(name=name).to_dict()
    meta = obj.get("metadata") or {}
    meta.pop("managedFields", None)
    (meta.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return redact(json.dumps(obj, default=str, indent=1))[:12000]


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}

NS = {"namespace": {"type": "string"}}
POD = {**NS, "pod": {"type": "string"}}

TOOLS = {
    "list_pods": (list_pods, "List pods with phase, readiness, restarts, waiting reasons.", _schema(NS, ["namespace"])),
    "describe_pod": (describe_pod, "Container states, last termination reason/exit code (e.g. OOMKilled), resources.", _schema(POD, ["namespace", "pod"])),
    "get_events": (get_events, "Recent Kubernetes events in a namespace (newest first).", _schema({**NS, "limit": {"type": "integer"}}, ["namespace"])),
    "get_pod_logs": (get_pod_logs, "Tail logs of a pod. Set previous=true for the last crashed container.",
                     _schema({**POD, "container": {"type": "string"}, "tail_lines": {"type": "integer"}, "previous": {"type": "boolean"}}, ["namespace", "pod"])),
    "get_deployment": (get_deployment, "Deployment status, conditions and rollout history (images per revision).",
                       _schema({**NS, "name": {"type": "string"}}, ["namespace", "name"])),
    "top_pods": (top_pods, "Current CPU/memory usage per pod.", _schema(NS, ["namespace"])),
    "search_runbooks": (search_runbooks, "Keyword search of internal runbooks for known fixes.", _schema({"query": {"type": "string"}}, ["query"])),
    "scan_cluster_for_issues": (scan_cluster_for_issues, "Scan all allowlisted namespaces for failed/restarting pods.", _schema({}, [])),
    "list_resources": (list_resources,
                       "List objects of ANY kind in the cluster (services, ingresses, nodes, configmaps, jobs, cronjobs, "
                       "statefulsets, PVCs, HPAs, namespaces, RBAC, CRDs, ...) with a short status. Omit namespace for "
                       "all namespaces. Secrets are never available.",
                       _schema({"kind": {"type": "string"}, "namespace": {"type": "string"}}, ["kind"])),
    "get_resource": (get_resource,
                     "Full definition and status of one object of any kind (not Secrets). Namespace is required for "
                     "namespaced kinds.",
                     _schema({"kind": {"type": "string"}, "name": {"type": "string"},
                              "namespace": {"type": "string"}}, ["kind", "name"])),
}

TOOL_SCHEMAS = [{"name": n, "description": d, "input_schema": s} for n, (_, d, s) in TOOLS.items()]

def call_tool(name: str, args: dict) -> str:
    try:
        result = TOOLS[name][0](**args)
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return f"<untrusted_data>\n{text[:15000]}\n</untrusted_data>"


# ─── Incident detection for the watcher ──────────────────────────────────────
# Unlike scan_cluster_for_issues (any restart, ever), this reports what is wrong
# *now*, with stable keys so the watcher can tell new incidents from old ones.

# Waiting reasons, grouped so that states a failing container alternates
# between (e.g. ErrImagePull <-> ImagePullBackOff) map to one incident.
WAITING_CATEGORY = {
    "CrashLoopBackOff": "CrashLoop",
    "ImagePullBackOff": "ImagePull",
    "ErrImagePull": "ImagePull",
    "InvalidImageName": "ImagePull",
    "CreateContainerConfigError": "ContainerConfig",
    "CreateContainerError": "ContainerConfig",
    "RunContainerError": "ContainerStart",
}


def _workload_of(pod) -> str:
    """Best-effort owning workload, e.g. "Deployment/web" (a ReplicaSet is
    named <deployment>-<pod-template-hash>), "StatefulSet/db" or "Pod/x"."""
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            pod_hash = (pod.metadata.labels or {}).get("pod-template-hash")
            if pod_hash and ref.name.endswith("-" + pod_hash):
                return f"Deployment/{ref.name[: -len(pod_hash) - 1]}"
            return f"ReplicaSet/{ref.name}"
        return f"{ref.kind}/{ref.name}"
    return f"Pod/{pod.metadata.name}"


def _age_seconds(ts, now) -> float:
    return (now - ts).total_seconds() if ts else 0.0


def find_incidents(pods_by_ns: dict, nodes: list, prev_restarts: dict, now: datetime,
                   recent_seconds: float = 180, pending_grace_minutes: float = 5):
    """Pure function behind detect_incidents (easy to unit-test with fake objects).

    pods_by_ns: {namespace: [V1Pod]}; nodes: [V1Node]; prev_restarts: {"<pod uid>/<container>": count}
    from the previous scan. A crash counts when the restart count went up since then, or,
    for a container not seen before, when it last terminated within `recent_seconds`.
    Returns ({incident key: incident}, new restart counts)."""
    incidents = {}
    restarts = {}

    def add(namespace, workload, category, pod_name, detail):
        key = f"{namespace}/{workload}/{category}"
        inc = incidents.setdefault(key, {
            "key": key, "namespace": namespace, "workload": workload, "reason": category,
            "pod": pod_name, "pods": [], "detail": detail,
        })
        if pod_name and pod_name not in inc["pods"]:
            inc["pods"].append(pod_name)

    for ns, pods in pods_by_ns.items():
        for p in pods:
            if p.status.phase not in ("Pending", "Running"):
                continue  # Succeeded/Failed pods are finished; not an ongoing incident
            name = p.metadata.name
            workload = _workload_of(p)
            found = False
            statuses = list(p.status.init_container_statuses or []) + list(p.status.container_statuses or [])
            for c in statuses:
                ckey = f"{p.metadata.uid}/{c.name}"
                restarts[ckey] = c.restart_count
                waiting = c.state.waiting if c.state else None
                last = c.last_state.terminated if c.last_state else None
                if waiting and waiting.reason in WAITING_CATEGORY:
                    category = WAITING_CATEGORY[waiting.reason]
                    if category == "CrashLoop" and last and last.reason == "OOMKilled":
                        category = "OOMKilled"
                    detail = f"container {c.name}: {waiting.reason}"
                    if last:
                        detail += f" (last exit {last.exit_code} {last.reason or ''}".rstrip() + ")"
                    if waiting.message:
                        detail += f" - {waiting.message[:300]}"
                    add(ns, workload, category, name, f"{detail}; restarts: {c.restart_count}")
                    found = True
                    continue
                if last and (last.exit_code or last.reason == "OOMKilled"):
                    prev = prev_restarts.get(ckey)
                    increased = prev is not None and c.restart_count > prev
                    recent = prev is None and _age_seconds(last.finished_at, now) <= recent_seconds
                    if increased or recent:
                        category = "OOMKilled" if last.reason == "OOMKilled" else "CrashLoop"
                        add(ns, workload, category, name,
                            f"container {c.name} restarted (exit {last.exit_code} {last.reason or ''}".rstrip()
                            + f"); restarts: {c.restart_count}")
                        found = True
            if not found and p.status.phase == "Pending" \
                    and _age_seconds(p.metadata.creation_timestamp, now) > pending_grace_minutes * 60:
                why = next((f"{cond.reason}: {cond.message}" for cond in (p.status.conditions or [])
                            if cond.type == "PodScheduled" and cond.status != "True"), "containers not started")
                add(ns, workload, "Pending", name, f"pending for "
                    f"{int(_age_seconds(p.metadata.creation_timestamp, now) // 60)} min - {why[:300]}")

    for n in nodes:
        ready = next((cond for cond in (n.status.conditions or []) if cond.type == "Ready"), None)
        if ready is not None and ready.status != "True":
            add("", f"Node/{n.metadata.name}", "NodeNotReady", None,
                f"Ready={ready.status} {ready.reason or ''} - {(ready.message or '')[:300]}".strip())

    return incidents, restarts


def detect_incidents(prev_restarts: dict, recent_seconds: float = 180, pending_grace_minutes: float = 5):
    """Read pods in the allowlisted namespaces (all namespaces with "*") and nodes,
    and return (incidents, restart counts) via find_incidents."""
    pods_by_ns = pods_by_namespace()
    try:
        nodes = core.list_node().items
    except Exception as e:
        logger.warning("Could not list nodes: %s", e)
        nodes = []
    return find_incidents(pods_by_ns, nodes, prev_restarts, datetime.now(timezone.utc),
                          recent_seconds, pending_grace_minutes)
