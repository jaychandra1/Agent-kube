"""Built-in diagnosis rules (rules.py) with fake Kubernetes reads: each rule's trigger, what it
says, how confident it is, and the severity it derives from the workload."""
import pytest

import agent
import rules


def container(name="app", ready=False, restarts=0, waiting=None, message=None, last=None, exit_code=None,
              finished="2026-11-16 10:00:00+00:00", limits=None):
    state = {"waiting": waiting, "message": message} if waiting else {"running_since": "2026-11-16 10:01:00+00:00"}
    return {"name": name, "image": "repo/app:1", "ready": ready, "restarts": restarts, "state": state,
            "last_terminated": {"reason": last, "exit_code": exit_code, "finished_at": finished}
            if (last or exit_code is not None) else None,
            "requests": {}, "limits": limits or {}}


def pod(*containers, phase="Running", conditions=(), reason=None, message=None, init=()):
    return {"phase": phase, "reason": reason, "message": message, "node": "ip-10-0-1-1",
            "init_containers": list(init), "containers": list(containers), "conditions": list(conditions)}


def event(reason, message, obj="Pod/web-1", time="2026-11-16 09:59:00+00:00", count=1, type_="Warning"):
    return {"time": time, "type": type_, "reason": reason, "count": count, "object": obj, "message": message}


@pytest.fixture
def cluster(monkeypatch):
    """Set what the fake cluster returns: cluster(pod=..., events=[...], logs=..., deployment=(desired, ready))."""
    state = {"pod": pod(container(ready=True)), "events": [], "logs": "", "deployment": None, "calls": []}

    def describe(ns, name):
        if isinstance(state["pod"], Exception):
            raise state["pod"]
        return state["pod"]

    def logs(ns, name, container=None, tail_lines=100, previous=False):
        state["calls"].append(("logs", container, previous))
        return state["logs"]

    def deployment(ns, name):
        if state["deployment"] is None:
            raise LookupError("not found")
        desired, ready = state["deployment"]
        return {"desired": desired, "ready": ready}

    monkeypatch.setattr(rules, "describe_pod", describe)
    monkeypatch.setattr(rules, "get_events", lambda ns, limit=30: state["events"])
    monkeypatch.setattr(rules, "get_pod_logs", logs)
    monkeypatch.setattr(rules, "get_deployment", deployment)
    monkeypatch.setattr(rules, "scan_cluster_for_issues", lambda: [])

    def set_(**kw):
        state.update(kw)
        return state
    return set_


def diagnose(workload="Deployment/web"):
    rca = rules.diagnose("apps", "web-1", {"workload": workload})
    assert agent.normalize_rca(dict(rca)) == rca, "every rule must return a complete RCA"
    return rca


# ─── Pod rules ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expect", [
    ('rpc error: manifest for repo/app:9 not found: manifest unknown', "doesn't exist"),
    ("pull access denied, repository does not exist or may require authorization: unauthorized", "isn't authorized"),
    ("unauthorized: authentication required", "authentication"),
    ("toomanyrequests: You have reached your pull rate limit", "rate-limiting"),
    ("dial tcp 1.2.3.4:443: i/o timeout", "can't reach the registry"),
])
def test_image_pull_sub_causes(cluster, message, expect):
    cluster(pod=pod(container(waiting="ImagePullBackOff", message=message), phase="Pending"), deployment=(2, 0))
    rca = diagnose()
    assert rca["title"] == "Image pull failure in Deployment/web" and expect in rca["root_cause"]
    assert rca["severity"] == "SEV1" and "0 of 2 replicas" in rca["impact"]  # nothing ready: down


def test_missing_configmap_is_named(cluster):
    cluster(pod=pod(container(waiting="CreateContainerConfigError", message='configmap "app-config" not found'),
                    phase="Pending"))
    rca = diagnose()
    assert rca["confidence"] == "high" and 'configmap "app-config"' in rca["root_cause"]
    assert "Create configmap app-config in namespace apps" in rca["resolution"][0]


def test_scheduling_reason_comes_from_the_scheduler(cluster):
    msg = "0/3 nodes are available: 3 Insufficient cpu."
    cluster(pod=pod(container(waiting="ContainerCreating"), phase="Pending",
                    conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": msg}]),
            events=[event("FailedScheduling", msg)])
    rca = diagnose()
    assert rca["title"].startswith("Pod cannot be scheduled") and "enough free CPU" in rca["root_cause"]
    assert msg in rca["root_cause"]


def test_oom_quotes_the_memory_limit(cluster):
    cluster(pod=pod(container(restarts=3, last="OOMKilled", exit_code=137, limits={"memory": "256Mi"},
                              waiting="CrashLoopBackOff")), deployment=(3, 2))
    rca = diagnose()
    assert rca["title"].startswith("Container out of memory") and "(256Mi)" in rca["root_cause"]
    assert rca["severity"] == "SEV2" and rca["confidence"] == "high"


def test_liveness_probe_restarts(cluster):
    cluster(pod=pod(container(restarts=4, last="Error", exit_code=137)),
            events=[event("Unhealthy", "Liveness probe failed: HTTP probe failed with statuscode: 500")])
    rca = diagnose()
    assert "liveness probe" in rca["title"] and "statuscode: 500" in rca["root_cause"]


def test_missing_entrypoint_beats_log_patterns(cluster):
    cluster(pod=pod(container(restarts=5, last="Error", exit_code=127, waiting="CrashLoopBackOff")),
            logs="exec /app/run.sh: no such file or directory")
    rca = diagnose()
    assert rca["title"].startswith("Container exits with code 127")
    assert "doesn't exist in the image" in rca["root_cause"]


def test_database_limit_from_logs(cluster):
    cluster(pod=pod(container(restarts=2, last="Error", exit_code=1, waiting="CrashLoopBackOff")),
            logs="2026-11-16T09:58:00Z FATAL: sorry, too many connections for role app")
    rca = diagnose()
    assert rca["title"].startswith("Database connection limit reached") and rca["confidence"] == "medium"
    assert "too many connections" in rca["evidence"][0]["excerpt"]
    assert ("logs", "app", True) in cluster()["calls"]  # read the crashed container's previous logs


def test_generic_crash_without_error_lines_is_low_confidence(cluster):
    cluster(pod=pod(container(restarts=2, last="Error", exit_code=3, waiting="CrashLoopBackOff")), logs="starting\nbye")
    rca = diagnose(workload=None)
    assert rca["title"] == "Application crash loop in Pod/web-1" and rca["confidence"] == "low"
    assert rca["severity"] == "SEV2"  # unknown workload, pod not ready


def test_recovered_pod_is_sev3(cluster):
    cluster(pod=pod(container(ready=True, restarts=1, last="Error", exit_code=2)),
            logs="ERROR could not open socket")
    assert diagnose(workload=None)["severity"] == "SEV3"


def test_evicted_pod(cluster):
    cluster(pod=pod(container(), phase="Failed", reason="Evicted",
                    message="The node was low on resource: memory."))
    rca = diagnose()
    assert rca["title"].startswith("Pod evicted") and "low on resource: memory" in rca["root_cause"]


def test_healthy_pod_is_sev4(cluster):
    cluster(pod=pod(container(ready=True)))
    rca = diagnose()
    assert rca["severity"] == "SEV4" and rca["title"].startswith("No current problem found")


def test_pod_that_disappeared(cluster):
    cluster(pod=LookupError("pods \"web-1\" not found"))
    rca = diagnose()
    assert rca["title"] == "Pod web-1 could not be read" and rca["confidence"] == "low"


def test_init_container_failures_are_explained(cluster):
    cluster(pod=pod(container(), phase="Pending",
                    init=[container(name="migrate", restarts=3, last="Error", exit_code=1, waiting="CrashLoopBackOff")]),
            logs="ERROR relation users does not exist")
    rca = diagnose()
    assert "migrate" in rca["root_cause"] and ("logs", "migrate", True) in cluster()["calls"]


def test_timeline_uses_real_times_in_order(cluster, monkeypatch):
    cluster(pod=pod(container(restarts=2, last="OOMKilled", exit_code=137, waiting="CrashLoopBackOff",
                              finished="2026-11-16 09:30:00+00:00")),
            events=[event("BackOff", "Back-off restarting failed container", time="2026-11-16 09:45:00+00:00", count=7)])
    timeline = diagnose()["timeline"]
    assert [t["event"].split(":")[0] for t in timeline[:2]] == ["Container app terminated", "BackOff x7"]
    assert timeline[0]["time"] == "2026-11-16 09:30 UTC"
    assert timeline[-1]["event"] == "kubesupport diagnosed the problem (built-in rules)"


# ─── Incidents without a pod ─────────────────────────────────────────────────

def incident(reason, detail, workload="Deployment/kubesupport", namespace="kubesupport", **extra):
    return {"reason": reason, "detail": detail, "workload": workload, "namespace": namespace, "pod": None, **extra}


def test_pod_security_rejection(cluster):
    cluster(deployment=(1, 0))
    rca = rules.diagnose("kubesupport", None, incident(
        "CannotCreatePods", 'ReplicaSet/kubesupport-769bb95cc4: pods "x" is forbidden: violates PodSecurity '
        '"restricted:latest": allowPrivilegeEscalation != false (container "opentelemetry-auto-instrumentation-java")'))
    assert rca["title"] == "Pods rejected by Pod Security admission: Deployment/kubesupport"
    assert rca["severity"] == "SEV1" and rca["confidence"] == "high"
    assert any("mutating webhook injected it" in step for step in rca["resolution"])


def test_quota_rejection():
    rca = rules.diagnose(incident=incident("CannotCreatePods", "ReplicaSet/api-5d8f7: exceeded quota: compute",
                                           workload="ReplicaSet/api-5d8f7", namespace="apps"))
    assert rca["title"].startswith("Pods blocked by a ResourceQuota") and rca["severity"] == "SEV2"


def test_failed_job():
    rca = rules.diagnose(incident=incident("JobFailed", "BackoffLimitExceeded: Job has reached the specified backoff limit",
                                           workload="CronJob/report", namespace="batch", job="report-29001"))
    assert rca["severity"] == "SEV3" and "backoffLimit" in rca["root_cause"]
    assert "job/report-29001" in rca["resolution"][0]


def test_node_not_ready():
    rca = rules.diagnose(incident=incident("NodeNotReady", "Ready=Unknown NodeStatusUnknown - Kubelet stopped posting "
                                           "node status.", workload="Node/ip-10-0-1-1", namespace=""))
    assert rca["title"] == "Node not ready: ip-10-0-1-1" and "kubelet stopped reporting" in rca["root_cause"]
    assert any("disruptive" in step for step in rca["resolution"])


def test_no_target_and_nothing_wrong(cluster):
    rca = rules.diagnose()
    assert rca["severity"] == "SEV4" and rca["title"] == "No current problem found"


@pytest.mark.parametrize("reason", ["CannotCreatePods", "JobFailed", "Evicted", "NodeNotReady", "Something"])
def test_incident_rcas_are_complete(cluster, reason):
    rca = rules.diagnose(incident=incident(reason, "detail text"))
    assert agent.normalize_rca(dict(rca)) == rca
