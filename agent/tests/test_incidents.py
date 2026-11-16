from types import SimpleNamespace as NS

from conftest import NOW, container, make_node, make_pod

import tools
from tools import find_incidents


def test_all_namespaces_mode_lists_every_namespace(monkeypatch):
    pods = [make_pod("a", ns="payments", containers=[container(waiting="CrashLoopBackOff")]),
            make_pod("b", ns="kube-system", containers=[container(waiting="ImagePullBackOff")])]
    fake_core = NS(list_pod_for_all_namespaces=lambda **kw: NS(items=pods),
                   list_namespaced_pod=lambda ns, **kw: (_ for _ in ()).throw(AssertionError("not per-namespace")))
    monkeypatch.setattr(tools, "core", fake_core)
    monkeypatch.setattr(tools, "ALLOWED", {"*"})
    assert set(tools.pods_by_namespace()) == {"payments", "kube-system"}
    assert {i["namespace"] for i in tools.scan_cluster_for_issues()} == {"payments", "kube-system"}
    assert tools.watched_namespaces_label() == "all namespaces"


def test_selected_namespaces_mode(monkeypatch):
    calls = []
    fake_core = NS(list_namespaced_pod=lambda ns, **kw: calls.append(ns) or NS(items=[]))
    monkeypatch.setattr(tools, "core", fake_core)
    monkeypatch.setattr(tools, "ALLOWED", {"orders", "payments"})
    assert tools.pods_by_namespace() == {"orders": [], "payments": []}
    assert calls == ["orders", "payments"]
    assert tools.watched_namespaces_label() == "orders, payments"


def test_crashloop_pods_of_one_deployment_are_one_incident():
    pods = [
        make_pod("web-5d8f7-a", containers=[container(restarts=4, waiting="CrashLoopBackOff",
                                                      last_reason="Error", last_exit=1, finished_ago=30)]),
        make_pod("web-5d8f7-b", containers=[container(restarts=3, waiting="CrashLoopBackOff",
                                                      last_reason="Error", last_exit=1, finished_ago=40)]),
    ]
    incidents, _ = find_incidents({"default": pods}, [], {}, NOW)
    assert list(incidents) == ["default/Deployment/web/CrashLoop"]
    assert incidents["default/Deployment/web/CrashLoop"]["pods"] == ["web-5d8f7-a", "web-5d8f7-b"]


def test_crashloop_after_oom_is_reported_as_oom():
    pod = make_pod("web-5d8f7-a", containers=[container(restarts=2, waiting="CrashLoopBackOff",
                                                        last_reason="OOMKilled", last_exit=137, finished_ago=20)])
    incidents, _ = find_incidents({"default": [pod]}, [], {}, NOW)
    assert list(incidents) == ["default/Deployment/web/OOMKilled"]


def test_restart_count_going_up_is_an_incident():
    pod = make_pod("api-77-x", owner=("ReplicaSet", "api-77"), pod_hash="77",
                   containers=[container(restarts=2, last_reason="OOMKilled", last_exit=137, finished_ago=5000)])
    incidents, restarts = find_incidents({"default": [pod]}, [], {"api-77-x/app": 1}, NOW)
    assert "default/Deployment/api/OOMKilled" in incidents
    assert restarts == {"api-77-x/app": 2}


def test_old_restart_is_not_an_incident():
    pod = make_pod("web-5d8f7-a", containers=[container(restarts=2, last_reason="Error", last_exit=1,
                                                        finished_ago=7200)])
    assert find_incidents({"default": [pod]}, [], {"web-5d8f7-a/app": 2}, NOW)[0] == {}  # count unchanged
    assert find_incidents({"default": [pod]}, [], {}, NOW)[0] == {}  # first sight, but long ago


def test_recent_crash_of_a_new_pod_is_an_incident():
    pod = make_pod("web-5d8f7-a", containers=[container(restarts=1, last_reason="Error", last_exit=1,
                                                        finished_ago=60)])
    incidents, _ = find_incidents({"default": [pod]}, [], {}, NOW)
    assert list(incidents) == ["default/Deployment/web/CrashLoop"]


def test_clean_exit_restart_is_ignored():
    pod = make_pod("web-5d8f7-a", containers=[container(restarts=3, last_reason="Completed", last_exit=0,
                                                        finished_ago=10)])
    assert find_incidents({"default": [pod]}, [], {"web-5d8f7-a/app": 1}, NOW)[0] == {}


def test_image_pull_on_pending_pod_is_image_pull_not_pending():
    pod = make_pod("web-5d8f7-a", phase="Pending", created_ago=3600,
                   containers=[container(waiting="ErrImagePull", waiting_msg="manifest unknown")])
    incidents, _ = find_incidents({"default": [pod]}, [], {}, NOW)
    assert list(incidents) == ["default/Deployment/web/ImagePull"]
    assert "manifest unknown" in incidents["default/Deployment/web/ImagePull"]["detail"]


def test_pending_respects_grace_period():
    unscheduled = [type("C", (), {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                  "message": "0/3 nodes are available: insufficient memory"})()]
    young = make_pod("job-x", phase="Pending", created_ago=120, conditions=unscheduled)
    old = make_pod("job-y", phase="Pending", created_ago=900, conditions=unscheduled)
    assert find_incidents({"default": [young]}, [], {}, NOW, pending_grace_minutes=5)[0] == {}
    incidents, _ = find_incidents({"default": [old]}, [], {}, NOW, pending_grace_minutes=5)
    (inc,) = incidents.values()
    assert inc["reason"] == "Pending" and "insufficient memory" in inc["detail"]


def test_workload_names_for_statefulset_and_bare_pod():
    sts = make_pod("db-0", owner=("StatefulSet", "db"), pod_hash=None,
                   containers=[container(waiting="CreateContainerConfigError", waiting_msg="secret not found")])
    bare = make_pod("debug", owner=None, pod_hash=None, containers=[container(waiting="ImagePullBackOff")])
    incidents, _ = find_incidents({"default": [sts, bare]}, [], {}, NOW)
    assert set(incidents) == {"default/StatefulSet/db/ContainerConfig", "default/Pod/debug/ImagePull"}


def test_finished_pods_are_ignored():
    pod = make_pod("job-1", phase="Failed", containers=[container(waiting="CrashLoopBackOff")])
    assert find_incidents({"default": [pod]}, [], {}, NOW)[0] == {}


def test_node_not_ready():
    nodes = [make_node("ip-10-0-1-1", ready="False", reason="KubeletNotReady", message="PLEG is not healthy"),
             make_node("ip-10-0-1-2")]
    incidents, _ = find_incidents({}, nodes, {}, NOW)
    assert list(incidents) == ["/Node/ip-10-0-1-1/NodeNotReady"]
    assert incidents["/Node/ip-10-0-1-1/NodeNotReady"]["pod"] is None
