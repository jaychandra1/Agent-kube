"""Built-in diagnosis rules: a root cause analysis (RCA) from Kubernetes evidence alone.

Used when no AI provider is configured, when the hourly AI budget is used up, or when the
model fails. The rules only state what the evidence shows: they quote the waiting or
termination reason, the exit code, event messages and log lines they relied on.

Confidence says how direct that evidence is:
  high    a Kubernetes reason or event message names the cause
  medium  the cause is inferred from logs or an exit code
  low     no specific signal was found; a generic diagnosis
Severity comes from the workload's state when it can be read:
  SEV1  no replica of the workload is ready (or no pod can be created)
  SEV2  some replicas are not ready, or a node is down
  SEV3  the pod recovered or the problem is contained (failed job, evicted pod replaced)
  SEV4  nothing is wrong
"""
import logging
import re
from dataclasses import dataclass, field

import timefmt
from tools import describe_pod, get_deployment, get_events, get_pod_logs, scan_cluster_for_issues

logger = logging.getLogger("kubesupport.rules")

ERROR_LINE = re.compile(r"(?i)\b(error|exception|fatal|panic|traceback|refused|denied|failed|cannot|unable)\b")

# What a container's exit code usually means (128 + n = killed by signal n)
EXIT_CODES = {
    126: "the command can't be executed (permission or file format)",
    127: "the command or entrypoint doesn't exist in the image",
    137: "the process was killed with SIGKILL",
    139: "the process crashed with a segmentation fault (SIGSEGV)",
    143: "the process was stopped with SIGTERM",
}


@dataclass
class Finding:
    title: str
    root_cause: str
    confidence: str
    resolution: list
    prevention: list = field(default_factory=list)
    evidence: list = field(default_factory=list)


@dataclass
class PodFacts:
    """Everything the pod rules look at, read once."""
    namespace: str
    pod: str
    phase: str = ""
    status_reason: str = ""
    status_message: str = ""
    node: str = ""
    container: str = ""
    waiting: str = ""
    waiting_message: str = ""
    term_reason: str = ""
    exit_code: int | None = None
    finished_at: str = ""
    restarts: int = 0
    ready: bool = False
    limits: dict = field(default_factory=dict)
    conditions: list = field(default_factory=list)
    events: list = field(default_factory=list)     # warning events about this pod, newest first
    logs: str = ""                                 # previous container's logs after a restart, else current

    def event_messages(self, *reasons) -> list[str]:
        return [e.get("message") or "" for e in self.events if e.get("reason") in reasons]

    def logs_match(self, pattern: str) -> list[str]:
        rx = re.compile(pattern, re.I)
        return [line for line in self.logs.splitlines() if rx.search(line)][-3:]


# ─── Entry point ─────────────────────────────────────────────────────────────

def diagnose(namespace: str | None = None, pod: str | None = None, incident: dict | None = None) -> dict:
    """RCA for one pod, for an incident without a pod (node, job, pods that can't be
    created), or, with no target, for the first unhealthy pod the scan finds."""
    incident = incident or {}
    if namespace and pod:
        return _pod_rca(namespace, pod, incident.get("workload"))
    if incident.get("reason"):
        return _incident_rca(incident)
    issues = scan_cluster_for_issues()
    if issues:
        return _pod_rca(issues[0]["namespace"], issues[0]["pod"], None)
    return _rca(title="No current problem found", severity="SEV4", confidence="high",
                summary="The scan of the watched namespaces found no failing or restarting pods.",
                impact="No impact found.", affected=["none"], root_cause="Nothing is failing right now.",
                evidence=[{"source": "Kubernetes API", "excerpt": "No pods in CrashLoopBackOff, Error, "
                           "OOMKilled, image-pull or config-error states."}],
                resolution=["No action needed."], prevention=[], timeline=[], factors=[],
                stakeholder="All checked workloads are running normally.")


# ─── Pods ────────────────────────────────────────────────────────────────────

def _facts(namespace: str, pod: str) -> PodFacts:
    desc = describe_pod(namespace, pod)
    f = PodFacts(namespace=namespace, pod=pod, phase=desc.get("phase") or "",
                 status_reason=desc.get("reason") or "", status_message=desc.get("message") or "",
                 node=desc.get("node") or "", conditions=desc.get("conditions") or [])
    containers = (desc.get("init_containers") or []) + (desc.get("containers") or [])
    # The container to explain: one that is waiting, else one that terminated, else the first
    chosen = (next((c for c in containers if (c.get("state") or {}).get("waiting")), None)
              or next((c for c in containers if c.get("last_terminated")), None)
              or (containers[0] if containers else None))
    if chosen:
        state = chosen.get("state") or {}
        last = chosen.get("last_terminated") or {}
        f.container = chosen["name"]
        f.waiting = state.get("waiting") or ""
        f.waiting_message = state.get("message") or ""
        f.term_reason = last.get("reason") or state.get("terminated") or ""
        f.exit_code = last.get("exit_code", state.get("exit_code"))
        f.finished_at = last.get("finished_at") or ""
        f.restarts = chosen.get("restarts") or 0
        f.limits = chosen.get("limits") or {}
    f.ready = bool(containers) and all(c.get("ready") for c in containers)
    try:
        f.events = [e for e in get_events(namespace, limit=60)
                    if e.get("object") == f"Pod/{pod}" and e.get("type") == "Warning"]
    except Exception as e:
        logger.info("Could not read events for %s/%s: %s", namespace, pod, e)
    if f.container:
        crashed = bool(f.restarts or f.term_reason)
        logs = get_pod_logs(namespace, pod, container=f.container, tail_lines=80, previous=crashed)
        if crashed and logs.startswith("log fetch failed"):
            logs = get_pod_logs(namespace, pod, container=f.container, tail_lines=80)
        f.logs = "" if logs.startswith("log fetch failed") else logs
    return f


def _image_pull(f: PodFacts) -> Finding | None:
    if f.waiting not in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        return None
    text = " ".join([f.waiting_message, *f.event_messages("Failed")])
    low = text.lower()
    describe = f"kubectl -n {f.namespace} describe pod {f.pod}"
    if f.waiting == "InvalidImageName":
        cause, steps, conf = "The image reference is not a valid image name.", [
            "Fix the image field in the workload (registry/repository:tag, no spaces or uppercase in the repository)."], "high"
    elif (any(s in low for s in ("not found", "manifest unknown", "does not exist"))
          and any(s in low for s in ("unauthorized", "denied", "authorization"))):
        # Docker's "pull access denied, repository does not exist or may require authorization"
        cause, steps, conf = "The repository doesn't exist, or the node isn't authorized to pull it.", [
            "Check the image name and tag against the registry.",
            "For a private repository, check the pod's imagePullSecrets (or, for ECR, the node role)."], "high"
    elif any(s in low for s in ("not found", "manifest unknown", "does not exist")):
        cause, steps, conf = "The image or tag doesn't exist in the registry.", [
            "Check the exact image and tag in the workload against the registry.",
            "Deploy only tags that CI pushed successfully; roll back to the previous revision if this came from a release: "
            f"kubectl -n {f.namespace} rollout undo deployment/<name>"], "high"
    elif any(s in low for s in ("unauthorized", "denied", "forbidden", "no basic auth", "authentication required")):
        cause, steps, conf = "The node isn't allowed to pull the image (registry authentication).", [
            "For ECR: check the node role has ecr:GetAuthorizationToken / BatchGetImage on the repository.",
            "For a private registry: check the pod's imagePullSecrets and that the secret is in the same namespace."], "high"
    elif "toomanyrequests" in low or "rate limit" in low:
        cause, steps, conf = "The registry is rate-limiting pulls (Docker Hub's anonymous pull limit).", [
            "Pull through a mirror (e.g. ECR pull-through cache) or authenticate pulls with imagePullSecrets.",
            "Pin tags and use imagePullPolicy: IfNotPresent to avoid needless pulls."], "high"
    elif any(s in low for s in ("i/o timeout", "no such host", "dial tcp", "connection refused", "tls")):
        cause, steps, conf = "The node can't reach the registry (network, DNS or TLS).", [
            "Check the node's outbound route to the registry (NAT gateway, VPC endpoints for ECR, proxy, DNS)."], "medium"
    else:
        cause, steps, conf = "Kubernetes can't pull the container image.", [
            f"Read the exact pull error in the Events of: {describe}"], "medium"
    return Finding(title="Image pull failure", confidence=conf,
                   root_cause=f"{cause} Container {f.container} is waiting in {f.waiting}.",
                   resolution=[f"See the exact error: {describe}", *steps],
                   prevention=["Use immutable image tags (or digests) and turn on ECR tag immutability."],
                   evidence=[_ev(f"container {f.container} state", f"{f.waiting}: {text.strip()}")])


def _config_error(f: PodFacts) -> Finding | None:
    if f.waiting not in ("CreateContainerConfigError", "CreateContainerError", "RunContainerError"):
        return None
    msg = " ".join([f.waiting_message, *f.event_messages("Failed")]).strip()
    missing = re.search(r'(configmap|secret)s? "([^"]+)" not found', msg, re.I)
    key = re.search(r"couldn't find key (\S+) in (ConfigMap|Secret) (\S+)", msg, re.I)
    if missing:
        cause = f"The {missing.group(1)} \"{missing.group(2)}\" that the container references doesn't exist."
        steps = [f"Create {missing.group(1)} {missing.group(2)} in namespace {f.namespace}, or fix the name in the workload."]
        conf = "high"
    elif key:
        cause = f"Key {key.group(1)} is missing from {key.group(2)} {key.group(3)}."
        steps = [f"Add the key to {key.group(2)} {key.group(3)}, or fix the key name the workload references."]
        conf = "high"
    else:
        cause = f"The container can't be created or started ({f.waiting})."
        steps = [f"Read the exact message: kubectl -n {f.namespace} describe pod {f.pod}"]
        conf = "medium" if msg else "low"
    return Finding(title="Container configuration error", confidence=conf, root_cause=cause,
                   resolution=steps + ["The pod starts by itself once the reference is fixed."],
                   prevention=["Create ConfigMaps and Secrets before the workloads that use them, and validate "
                               "manifests in CI."],
                   evidence=[_ev(f"container {f.container} state", f"{f.waiting}: {msg}")])


def _scheduling(f: PodFacts) -> Finding | None:
    unscheduled = next((c for c in f.conditions if c.get("type") == "PodScheduled" and c.get("status") != "True"), None)
    msgs = f.event_messages("FailedScheduling")
    if f.phase != "Pending" or not (unscheduled or msgs):
        return None
    msg = (msgs[0] if msgs else (unscheduled or {}).get("message")) or ""
    low = msg.lower()
    rules = [
        (("insufficient cpu", "insufficient memory", "insufficient ephemeral"),
         "No node has enough free CPU/memory for the pod's requests.",
         ["Lower the pod's resource requests if they are higher than real usage.",
          "Add capacity: scale the node group, or check Cluster Autoscaler / Karpenter logs for why it didn't."]),
        (("didn't match pod's node affinity", "node selector", "didn't match node selector"),
         "No node matches the pod's nodeSelector or node affinity.",
         ["Fix the nodeSelector/affinity, or label nodes to match."]),
        (("untolerated taint", "had taint", "had untolerated"),
         "The available nodes have taints the pod doesn't tolerate.",
         ["Add the matching toleration, or schedule onto nodes without the taint."]),
        (("unbound immediate persistentvolumeclaims", "persistentvolumeclaim", "volume node affinity conflict"),
         "A volume can't be bound or attached in a zone where the pod can run.",
         [f"Check the PVC: kubectl -n {f.namespace} get pvc; and the StorageClass's volumeBindingMode and zones."]),
        (("too many pods",), "The nodes have reached their pod limit (max pods / IP addresses).",
         ["Add nodes, or raise max pods (VPC CNI prefix delegation)."]),
    ]
    cause, steps = "The scheduler can't place the pod on any node.", []
    for needles, why, how in rules:
        if any(n in low for n in needles):
            cause, steps = why, how
            break
    return Finding(title="Pod cannot be scheduled", confidence="high" if msg else "medium",
                   root_cause=f"{cause} Scheduler: {msg[:400]}" if msg else cause,
                   resolution=[f"See the scheduler's reason: kubectl -n {f.namespace} describe pod {f.pod}", *steps],
                   prevention=["Keep requests close to real usage, and run Cluster Autoscaler or Karpenter."],
                   evidence=[_ev("FailedScheduling event", msg)] if msg else [])


def _volume(f: PodFacts) -> Finding | None:
    msgs = f.event_messages("FailedMount", "FailedAttachVolume")
    if not msgs or f.ready:
        return None
    return Finding(title="Volume can't be mounted", confidence="high",
                   root_cause=f"A volume of pod {f.pod} can't be attached or mounted: {msgs[0][:400]}",
                   resolution=[f"Check the volumes in: kubectl -n {f.namespace} describe pod {f.pod}",
                               "For a missing ConfigMap/Secret volume, create it; for EBS, check the volume's zone "
                               "and that it isn't still attached to another node."],
                   prevention=["Use volumeBindingMode: WaitForFirstConsumer for zonal storage."],
                   evidence=[_ev("FailedMount event", msgs[0])])


def _oom(f: PodFacts) -> Finding | None:
    if f.term_reason != "OOMKilled":
        return None
    limit = f.limits.get("memory")
    return Finding(title="Container out of memory (OOMKilled)", confidence="high",
                   root_cause=(f"Container {f.container} used more memory than its limit"
                               f"{f' ({limit})' if limit else ' (no limit set: the node ran out of memory)'} "
                               f"and was killed by the kernel (exit {f.exit_code})."),
                   resolution=[f"Raise the memory limit of container {f.container}{f' above {limit}' if limit else ''} "
                               "if the usage is expected, or find what grows (heap size, caches, leaks).",
                               f"See current usage: kubectl -n {f.namespace} top pod {f.pod}"],
                   prevention=["Alert at 85% of the memory limit; set language-runtime heap limits (e.g. JVM "
                               "-XX:MaxRAMPercentage) below the container limit."],
                   evidence=[_ev(f"container {f.container} last termination",
                                 f"OOMKilled, exit {f.exit_code}, at {timefmt.fmt(f.finished_at)}")])


def _liveness(f: PodFacts) -> Finding | None:
    msgs = [m for m in f.event_messages("Unhealthy") if "liveness" in m.lower()]
    if not msgs or not f.restarts:
        return None
    return Finding(title="Container restarted by a failing liveness probe", confidence="high",
                   root_cause=(f"Kubernetes restarted container {f.container} because its liveness probe failed: "
                               f"{msgs[0][:300]}"),
                   resolution=["Check the probe's path/port answers while the app is healthy, and that the app "
                               "starts within initialDelaySeconds.",
                               "If startup is slow, add a startupProbe or raise failureThreshold instead of "
                               "removing the probe."],
                   prevention=["Make liveness checks cheap and independent of downstream services."],
                   evidence=[_ev("Unhealthy event", msgs[0])])


def _logs_pattern(f: PodFacts) -> Finding | None:
    checks = [
        (r"too many connections|remaining connection slots|pool.*(exhausted|limit)|QueuePool limit",
         "Database connection limit reached",
         "The application can't get a database connection: the pool or the database's connection limit is used up.",
         ["Raise the pool size or the database's max connections, or add a pooler (RDS Proxy, PgBouncer).",
          "Check for connections that are never released."]),
        (r"could not connect to server|connection refused|ECONNREFUSED|no route to host|timed? ?out.*connect",
         "Dependency unreachable",
         "The application can't reach a service it depends on (connection refused or timed out).",
         ["Check the dependency is up and its Service has endpoints: kubectl -n <ns> get endpoints <service>.",
          "Check NetworkPolicies, security groups and DNS between the two."]),
        (r"read-only file system|permission denied|EACCES",
         "File-system permission error",
         "The application tries to write where it may not (read-only root filesystem or file permissions).",
         ["Mount an emptyDir for the paths the app writes to, or fix the file ownership in the image."]),
        (r"address already in use|EADDRINUSE",
         "Port already in use",
         "The application can't bind its port because something else in the pod already uses it.",
         ["Check the container's command and ports; two processes or sidecars may use the same port."]),
        (r"KeyError|environment variable|is not set|missing required|no such file or directory|config.*not found",
         "Application configuration error",
         "The application stops at startup because a setting or file it needs is missing.",
         ["Compare the workload's env, ConfigMaps and mounted files with what the application expects."]),
    ]
    for pattern, title, cause, steps in checks:
        lines = f.logs_match(pattern)
        if lines:
            return Finding(title=title, confidence="medium", root_cause=cause, resolution=steps,
                           prevention=["Add a readiness probe that checks the dependency, and alert on it."],
                           evidence=[_ev(f"logs of {f.pod} ({f.container})", "\n".join(lines))])
    return None


def _exit_code(f: PodFacts, codes=(126, 127, 139, 137, 143)) -> Finding | None:
    meaning = EXIT_CODES.get(f.exit_code or 0)
    if not meaning or f.exit_code not in codes or not f.restarts:
        return None
    hints = {
        127: ["Check the image's ENTRYPOINT/CMD and the workload's command/args for typos or a missing binary."],
        126: ["Check the entrypoint file is executable and built for the node's CPU architecture (amd64/arm64)."],
        137: ["Look for node memory pressure or an eviction: kubectl describe node " + (f.node or "<node>"),
              "A liveness probe failure also ends in exit 137; check the pod's events."],
        139: ["A native library or the runtime crashed; check the image for an architecture mismatch or a bad build."],
        143: ["Often normal during a rollout or scale-down; if it repeats, check what stops the container "
              "(preStop hooks, probes)."],
    }[f.exit_code]
    return Finding(title=f"Container exits with code {f.exit_code}", confidence="medium",
                   root_cause=f"Container {f.container} keeps exiting with code {f.exit_code}: {meaning}.",
                   resolution=[f"Read the last output: kubectl -n {f.namespace} logs {f.pod} -c {f.container} --previous",
                               *hints],
                   evidence=[_ev(f"container {f.container} last termination",
                                 f"{f.term_reason or 'terminated'}, exit {f.exit_code}, at {timefmt.fmt(f.finished_at)}")])


def _readiness(f: PodFacts) -> Finding | None:
    msgs = [m for m in f.event_messages("Unhealthy") if "readiness" in m.lower()]
    if f.ready or not msgs or f.phase != "Running":
        return None
    return Finding(title="Pod not ready: readiness probe failing", confidence="high",
                   root_cause=f"Pod {f.pod} runs but receives no traffic because its readiness probe fails: {msgs[0][:300]}",
                   resolution=["Check the probe's path/port and the dependencies the readiness check calls.",
                               f"See: kubectl -n {f.namespace} describe pod {f.pod}"],
                   evidence=[_ev("Unhealthy event", msgs[0])])


def _crash(f: PodFacts) -> Finding | None:
    if not (f.restarts or f.waiting == "CrashLoopBackOff" or (f.exit_code not in (None, 0))):
        return None
    lines = [line for line in f.logs.splitlines() if ERROR_LINE.search(line)][-5:]
    cause = (f"Container {f.container} keeps exiting (exit code {f.exit_code}, {f.restarts} restarts)."
             + (" The last error lines are in the evidence." if lines else " The logs show no error line."))
    evidence = [_ev(f"container {f.container} last termination",
                    f"{f.term_reason or 'terminated'}, exit {f.exit_code}, at {timefmt.fmt(f.finished_at)}")]
    if lines:
        evidence.append(_ev(f"logs of {f.pod} ({f.container}, previous run)", "\n".join(lines)))
    return Finding(title="Application crash loop", confidence="medium" if lines else "low", root_cause=cause,
                   resolution=[f"Read the full output: kubectl -n {f.namespace} logs {f.pod} -c {f.container} --previous",
                               f"If a recent release caused it, roll back: kubectl -n {f.namespace} rollout undo "
                               "deployment/<name>"],
                   prevention=["Run the image's smoke tests in CI before deploying."], evidence=evidence)


def _evicted(f: PodFacts) -> Finding | None:
    if f.status_reason != "Evicted":
        return None
    return Finding(title="Pod evicted", confidence="high",
                   root_cause=f"The kubelet evicted pod {f.pod} from node {f.node or '?'}: {f.status_message}",
                   resolution=["Check the node's pressure conditions: kubectl describe node " + (f.node or "<node>"),
                               "Set memory and ephemeral-storage requests close to real usage so the scheduler "
                               "accounts for them."],
                   evidence=[_ev("pod status", f"Evicted: {f.status_message}")])


def _exit_code_program(f: PodFacts) -> Finding | None:
    """126/127/139 say what is wrong with the program itself: checked before the log patterns."""
    return _exit_code(f, codes=(126, 127, 139))


def _exit_code_signal(f: PodFacts) -> Finding | None:
    """137/143 only say the process was killed or stopped: checked after the log patterns."""
    return _exit_code(f, codes=(137, 143))


# First match wins: the most specific evidence first
POD_RULES = [_image_pull, _config_error, _scheduling, _volume, _evicted, _oom, _liveness, _exit_code_program,
             _logs_pattern, _exit_code_signal, _readiness, _crash]


def _pod_rca(namespace: str, pod: str, workload: str | None) -> dict:
    try:
        f = _facts(namespace, pod)
    except Exception as e:
        return _rca(title=f"Pod {pod} could not be read", severity="SEV3", confidence="low",
                    summary=f"kubesupport could not read pod {namespace}/{pod}: {type(e).__name__}.",
                    impact="Unknown.", affected=[workload or pod], root_cause="The pod may have been deleted or "
                    "replaced since the problem was detected.",
                    evidence=[_ev("Kubernetes API", f"{type(e).__name__}: {str(e)[:300]}")],
                    resolution=[f"Check the workload's current pods: kubectl -n {namespace} get pods"],
                    prevention=[], timeline=_timeline([], []), factors=[], stakeholder="")
    finding = next((r for rule in POD_RULES if (r := rule(f))), None)
    label = workload or f"Pod/{pod}"
    if finding is None:
        return _rca(title=f"No current problem found for {pod}", severity="SEV4", confidence="high",
                    summary=f"Pod {namespace}/{pod} is {f.phase} and ready, with no warning events.",
                    impact="No impact found.", affected=[label],
                    root_cause="Nothing is failing in this pod right now.",
                    evidence=[_ev("pod status", f"phase {f.phase}, ready {f.ready}, restarts {f.restarts}")],
                    resolution=["No action needed."], prevention=[], timeline=_timeline(f.events, []), factors=[],
                    stakeholder=f"{label} in {namespace} is running normally.")
    severity, impact = _impact(namespace, workload, f.ready)
    term = ([(f.finished_at, f"Container {f.container} terminated: {f.term_reason or 'exit'} {f.exit_code}")]
            if f.finished_at and f.finished_at != "None" else [])
    return _rca(title=f"{finding.title} in {label}", severity=severity, confidence=finding.confidence,
                summary=f"{label} in namespace {namespace}: {finding.title.lower()}. {impact}",
                impact=impact, affected=[label], root_cause=finding.root_cause, evidence=finding.evidence
                + [_ev(f"warning event: {e.get('reason')}", f"{e.get('message')}") for e in f.events[:3]
                   if e.get("message") and not any(e.get("message") in x["excerpt"] for x in finding.evidence)],
                resolution=finding.resolution, prevention=finding.prevention, timeline=_timeline(f.events, term),
                factors=[x for x in (f"Pod {pod} on node {f.node}" if f.node else "",
                                     f"Restarts: {f.restarts}" if f.restarts else "",
                                     f"Exit code {f.exit_code}: {EXIT_CODES[f.exit_code]}"
                                     if f.exit_code in EXIT_CODES else "") if x],
                stakeholder=(f"{label} in {namespace} has a problem: {finding.title.lower()}. {impact} "
                             f"Suggested first step: {finding.resolution[0]}"))


def _impact(namespace: str, workload: str | None, pod_ready: bool) -> tuple[str, str]:
    """(severity, impact sentence), from the Deployment's ready replicas when available."""
    if workload and workload.startswith("Deployment/"):
        try:
            d = get_deployment(namespace, workload.split("/", 1)[1])
            desired, ready = d.get("desired") or 0, d.get("ready") or 0
            text = f"{ready} of {desired} replicas of {workload} are ready."
            if desired and ready == 0:
                return "SEV1", text + " The workload is down."
            if ready < desired:
                return "SEV2", text + " The workload is degraded."
            return "SEV3", text + " The workload is still serving."
        except Exception as e:
            logger.info("Could not read %s/%s: %s", namespace, workload, e)
    if pod_ready:
        return "SEV3", "The pod is running and ready again; check how often it restarts."
    return "SEV2", "The pod is not ready; check whether other replicas are serving."


# ─── Incidents without a pod ─────────────────────────────────────────────────

def _incident_rca(incident: dict) -> dict:
    reason, detail = incident.get("reason", ""), incident.get("detail") or ""
    namespace, workload = incident.get("namespace") or "", incident.get("workload") or ""
    low = detail.lower()
    severity = "SEV2"
    if reason == "CannotCreatePods":
        severity, impact = _impact(namespace, workload, pod_ready=False) if workload.startswith("Deployment/") \
            else ("SEV2", f"{workload} can't create new pods.")
        if "violates podsecurity" in low:
            title, conf, cause, steps = "Pods rejected by Pod Security admission", "high", (
                "The namespace's Pod Security level rejects the pods the controller tries to create."), [
                "Fix the securityContext settings the message names (allowPrivilegeEscalation: false, "
                "capabilities.drop: [ALL], runAsNonRoot: true, seccompProfile: RuntimeDefault).",
                "If the container named isn't in your manifest, a mutating webhook injected it (for example "
                "OpenTelemetry auto-instrumentation from the CloudWatch add-on): opt the workload out or exclude "
                "the namespace in that add-on.",
                "Only as a last resort, lower the namespace's pod-security.kubernetes.io/enforce label."]
        elif "exceeded quota" in low:
            title, conf, cause, steps = "Pods blocked by a ResourceQuota", "high", (
                "Creating the pods would exceed the namespace's ResourceQuota."), [
                f"See the quota: kubectl -n {namespace} describe resourcequota",
                "Raise the quota, or lower the pods' requests/limits."]
        elif "admission webhook" in low and "denied" in low:
            title, conf, cause, steps = "Pods denied by an admission webhook", "high", (
                "An admission webhook (a policy engine such as Kyverno or Gatekeeper) denies the pods."), [
                "Read the policy named in the message and change the workload to comply, or ask the policy owner "
                "for an exception."]
        elif "serviceaccount" in low and "not found" in low:
            title, conf, cause, steps = "Pods reference a missing ServiceAccount", "high", (
                "The ServiceAccount the pods use doesn't exist."), [
                f"Create the ServiceAccount in {namespace}, or fix serviceAccountName in the workload."]
        else:
            title, conf, cause, steps = "Pods can't be created", "medium", (
                "The controller can't create the workload's pods."), []
        return _rca(title=f"{title}: {workload}", severity=severity, confidence=conf,
                    summary=f"{workload} in namespace {namespace} can't create pods. {impact}", impact=impact,
                    affected=[workload], root_cause=f"{cause} Controller event: {detail[:500]}",
                    evidence=[_ev("FailedCreate event", detail)],
                    resolution=[f"Read the full message: kubectl -n {namespace} get events "
                                "--field-selector reason=FailedCreate", *steps],
                    prevention=["Test new workloads in a namespace with the same Pod Security level and policies."],
                    timeline=_timeline([], []), factors=[],
                    stakeholder=f"{workload} in {namespace} can't start new pods. {impact}")
    if reason == "JobFailed":
        deadline = "deadline" in low
        cause = ("The Job ran longer than its activeDeadlineSeconds." if deadline
                 else "The Job's pods kept failing until it reached its backoffLimit.")
        steps = (["Check what takes long in its logs; raise activeDeadlineSeconds only if the duration is expected."]
                 if deadline else [f"Read why its pods failed: kubectl -n {namespace} logs "
                                   f"job/{incident.get('job') or workload.split('/', 1)[-1]} --tail=100"])
        return _rca(title=f"Job failed: {workload}", severity="SEV3", confidence="high",
                    summary=f"{workload} in namespace {namespace} failed. {detail}", impact=(
                        "The job's work was not done; for a CronJob, the next scheduled run will try again."),
                    affected=[workload], root_cause=f"{cause} Job condition: {detail[:400]}",
                    evidence=[_ev("Job status", detail)], resolution=steps + [
                        f"See its pods: kubectl -n {namespace} get pods -l job-name={incident.get('job') or ''}"],
                    prevention=["Alert on failed Jobs, and make job steps safe to retry."],
                    timeline=_timeline([], []), factors=[], stakeholder=f"The scheduled task {workload} failed.")
    if reason == "Evicted":
        return _rca(title=f"Pods evicted: {workload}", severity="SEV3", confidence="high",
                    summary=f"Pods of {workload} in {namespace} were evicted by the kubelet. {detail}",
                    impact="Evicted pods are replaced by the controller; requests may have failed meanwhile.",
                    affected=[workload], root_cause=f"The node ran low on a resource and evicted pods: {detail[:400]}",
                    evidence=[_ev("pod status", detail)],
                    resolution=["Check the node's MemoryPressure/DiskPressure: kubectl describe node <node>",
                                "Set memory and ephemeral-storage requests close to real usage."],
                    prevention=["Alert on node pressure; keep image and log disk usage in check."],
                    timeline=_timeline([], []), factors=[],
                    stakeholder=f"Some pods of {workload} were restarted elsewhere because a node ran low on resources.")
    if reason == "NodeNotReady":
        node = workload.split("/", 1)[-1]
        if "stopped posting" in low or "unknown" in low:
            cause = "The node's kubelet stopped reporting: the instance is down, overloaded, or lost its network."
        elif any(s in low for s in ("container runtime", "pleg", "network plugin", "cni")):
            cause = "The node's container runtime or network plugin isn't working."
        else:
            cause = "The node reports it isn't ready."
        return _rca(title=f"Node not ready: {node}", severity="SEV2", confidence="high" if detail else "medium",
                    summary=f"Node {node} is not ready. Pods on it can't run until it recovers or is replaced.",
                    impact=f"Pods on {node} are unavailable; Kubernetes reschedules them after the eviction timeout.",
                    affected=[workload], root_cause=f"{cause} Node condition: {detail[:400]}",
                    evidence=[_ev("node Ready condition", detail)],
                    resolution=[f"See the node's conditions and events: kubectl describe node {node}",
                                "Check the EC2 instance status checks and the node group's health in the AWS console.",
                                f"If it doesn't recover, replace it: kubectl drain {node} --ignore-daemonsets "
                                "--delete-emptydir-data (disruptive: moves its pods), then terminate the instance."],
                    prevention=["Use managed node groups with auto-repair; spread replicas across nodes and zones."],
                    timeline=_timeline([], []), factors=[], stakeholder=f"A cluster node ({node}) is down.")
    return _rca(title=f"{reason} in {workload}", severity=severity, confidence="low",
                summary=f"{reason} detected for {workload}. {detail}", impact="Unknown.", affected=[workload],
                root_cause=detail or "No details available.", evidence=[_ev("detector", detail)] if detail else [],
                resolution=[f"Inspect it: kubectl -n {namespace} describe {workload.lower()}"], prevention=[],
                timeline=_timeline([], []), factors=[], stakeholder="")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ev(source: str, excerpt: str) -> dict:
    return {"source": source, "excerpt": (excerpt or "").strip()[:800]}


def _timeline(events: list, extra: list) -> list:
    """Real times from the evidence (oldest first), then the time of this diagnosis."""
    items = [(e.get("time"), f"{e.get('reason')}{' x' + str(e['count']) if (e.get('count') or 1) > 1 else ''}: "
              f"{(e.get('message') or '')[:160]}") for e in events[:4]] + list(extra)
    dated = sorted(((timefmt.parse(t), text) for t, text in items if timefmt.parse(t)), key=lambda x: x[0])
    return [{"time": timefmt.fmt(t), "event": text} for t, text in dated] + [
        {"time": timefmt.fmt(timefmt.now()), "event": "kubesupport diagnosed the problem (built-in rules)"}]


def _rca(*, title, severity, confidence, summary, impact, affected, root_cause, evidence, resolution,
         prevention, timeline, factors, stakeholder) -> dict:
    return {"title": title, "severity": severity, "summary": summary, "impact": impact,
            "affected_services": affected, "timeline": timeline, "root_cause": root_cause,
            "evidence": evidence, "contributing_factors": factors, "resolution": resolution,
            "prevention": prevention, "confidence": confidence,
            "stakeholder_summary": stakeholder or summary}
