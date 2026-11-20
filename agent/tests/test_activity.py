import json

import pytest
from fastapi.testclient import TestClient

import activity


def review(op, resource, sub="", kind="", user="alice@corp", ns="payments", name="api", obj=None, old=None,
           dry=False, group="", uid="u-1"):
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "request": {
        "uid": uid, "operation": op, "resource": {"group": group, "version": "v1", "resource": resource},
        "subResource": sub, "kind": {"kind": kind}, "namespace": ns, "name": name,
        "userInfo": {"username": user, "groups": ["system:authenticated"]},
        "object": obj, "oldObject": old, "dryRun": dry}}


def deploy(images, replicas=2, env=None, resources=None, restarted_at=None, pod=None, **spec):
    containers = [{"name": n, "image": i} for n, i in images.items()]
    if env is not None:
        containers[0]["env"] = [{"name": k, "value": v} for k, v in env.items()]
    if resources is not None:
        containers[0]["resources"] = resources
    template = {"spec": {"containers": containers, **(pod or {})}}
    if restarted_at:
        template["metadata"] = {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}
    return {"metadata": {"name": "api"}, "spec": {"replicas": replicas, "template": template, **spec}}


def update(resource, kind, old, new, **kw):
    return activity.parse_admission(review("UPDATE", resource, kind=kind, old=old, obj=new, **kw))


def test_exec_is_reported_with_user_container_and_command():
    e = activity.parse_admission(review("CONNECT", "pods", "exec", "PodExecOptions",
                                        obj={"kind": "PodExecOptions", "container": "app", "command": ["sh", "-c", "id"]}))
    assert e["category"] == "exec" and e["action"] == "exec"
    assert activity.describe(e) == "alice@corp exec into pod payments/api (container app): `sh -c id`"


def test_port_forward_and_attach():
    pf = activity.parse_admission(review("CONNECT", "pods", "portforward", obj={"ports": [8080, 9090]}))
    assert pf["action"] == "port-forward" and "ports 8080,9090" in activity.describe(pf)
    assert activity.parse_admission(review("CONNECT", "pods", "attach", obj={"container": "app"}))["action"] == "attach"


def test_created_service_takes_name_from_object():
    e = activity.parse_admission(review("CREATE", "services", kind="Service", name="",
                                        obj={"metadata": {"name": "api-public"}}))
    assert e["category"] == "change"
    assert activity.describe(e) == "Created Service payments/api-public by alice@corp"


def test_deleted_secret_keeps_only_the_name():
    e = activity.parse_admission(review("DELETE", "secrets", kind="Secret", name="db-pass",
                                        old={"metadata": {"name": "db-pass"}, "data": {"password": "c2VjcmV0"}}))
    assert e["category"] == "config" and e["name"] == "db-pass"
    assert "c2VjcmV0" not in json.dumps(e) and "data" not in e


def test_image_change_is_reported_but_noop_update_is_not():
    changed = activity.parse_admission(review("UPDATE", "deployments", kind="Deployment", group="apps",
                                              old=deploy({"app": "api:1.2"}), obj=deploy({"app": "api:1.3"})))
    assert changed["category"] == "rollout" and changed["detail"] == "image app: api:1.2 → api:1.3"
    assert activity.describe(changed) == "Updated Deployment payments/api (image app: api:1.2 → api:1.3) by alice@corp"
    assert activity.parse_admission(review("UPDATE", "deployments", kind="Deployment", group="apps",
                                           old=deploy({"app": "api:1.2"}), obj=deploy({"app": "api:1.2"}))) is None


def test_scaling_through_the_scale_subresource():
    e = activity.parse_admission(review("UPDATE", "deployments", "scale", "Scale", group="apps",
                                        old={"spec": {"replicas": 2}}, obj={"spec": {"replicas": 5}}))
    assert e["action"] == "scaled" and e["kind"] == "Deployment"
    assert activity.describe(e) == "Scaled Deployment payments/api 2 → 5 by alice@corp"


def test_rbac_change_is_a_security_event():
    e = activity.parse_admission(review("UPDATE", "clusterrolebindings", kind="ClusterRoleBinding", ns="",
                                        name="temp-admin", group="rbac.authorization.k8s.io"))
    assert e["category"] == "security"
    assert activity.describe(e) == "Changed ClusterRoleBinding temp-admin by alice@corp"


@pytest.mark.parametrize("user", ["system:serviceaccount:kube-system:deployment-controller", "system:node:ip-10-0-1-1",
                                  "system:kube-scheduler", "eks:addon-manager", "system:serviceaccount:kubesupport:x"])
def test_kubernetes_own_components_are_ignored(user):
    assert activity.parse_admission(review("CREATE", "services", kind="Service", user=user)) is None


def test_ci_service_accounts_are_reported():
    e = activity.parse_admission(review("CREATE", "deployments", kind="Deployment",
                                        user="system:serviceaccount:ci:deployer", group="apps"))
    assert e and e["user"] == "system:serviceaccount:ci:deployer"


def test_dry_run_and_ignored_namespace():
    assert activity.parse_admission(review("CREATE", "services", kind="Service", dry=True)) is None
    assert activity.parse_admission(review("CREATE", "services", kind="Service", ns="kube-node-lease")) is None


# --- restarts and workload changes ---

def test_rollout_restart():
    e = update("deployments", "Deployment", deploy({"app": "api:1.2"}),
               deploy({"app": "api:1.2"}, restarted_at="2026-11-20T10:00:00Z"), group="apps")
    assert e["category"] == "rollout" and e["action"] == "restarted"
    assert activity.describe(e) == "Restarted Deployment payments/api (rollout restart) by alice@corp"
    # a later update that still carries the same stamp is not another restart
    same = deploy({"app": "api:1.2"}, restarted_at="2026-11-20T10:00:00Z")
    assert update("deployments", "Deployment", same, same, group="apps") is None
    again = update("statefulsets", "StatefulSet", same, deploy({"app": "api:1.2"}, restarted_at="2026-11-20T11:00:00Z"),
                   group="apps")
    assert again["action"] == "restarted" and again["kind"] == "StatefulSet"


def test_restart_together_with_an_image_change():
    e = update("deployments", "Deployment", deploy({"app": "api:1.2"}),
               deploy({"app": "api:1.3"}, restarted_at="2026-11-20T10:00:00Z"), group="apps")
    assert e["action"] == "updated" and e["detail"] == "rollout restart; image app: api:1.2 → api:1.3"


def test_env_change_names_variables_not_values():
    old = deploy({"app": "api:1"}, env={"DB_PASSWORD": "old-secret", "LOG_LEVEL": "info"})
    new = deploy({"app": "api:1"}, env={"DB_PASSWORD": "new-secret", "LOG_LEVEL": "info", "FEATURE_X": "on"})
    e = update("deployments", "Deployment", old, new, group="apps")
    assert e["detail"] == "env app: DB_PASSWORD, FEATURE_X"
    assert "secret" not in json.dumps(e)


def test_resources_pause_and_strategy():
    old = deploy({"app": "api:1"}, resources={"limits": {"memory": "256Mi"}})
    new = deploy({"app": "api:1"}, resources={"limits": {"memory": "512Mi"}, "requests": {"cpu": "100m"}},
                 paused=True, strategy={"type": "Recreate"})
    detail = update("deployments", "Deployment", old, new, group="apps")["detail"]
    assert "resources app: cpu request - → 100m, memory limit 256Mi → 512Mi" in detail
    assert "rollout paused" in detail and "strategy changed" in detail
    resumed = update("deployments", "Deployment", new, deploy({"app": "api:1"}, resources=new["spec"]["template"]["spec"]
                     ["containers"][0]["resources"], strategy={"type": "Recreate"}), group="apps")
    assert resumed["detail"] == "rollout resumed"


def test_pod_template_and_container_changes():
    old = deploy({"app": "api:1", "sidecar": "envoy:1"}, pod={"serviceAccountName": "default"})
    new = deploy({"app": "api:1"}, pod={"serviceAccountName": "api", "volumes": [{"name": "tmp"}]})
    new["spec"]["template"]["spec"]["containers"][0]["args"] = ["--verbose"]
    detail = update("daemonsets", "DaemonSet", old, new, group="apps")["detail"]
    assert "app: args changed" in detail and "container sidecar removed" in detail
    assert "pod spec: serviceAccountName, volumes changed" in detail


def test_workload_update_that_changes_nothing_real_is_dropped():
    old = deploy({"app": "api:1"})
    new = deploy({"app": "api:1"})
    new["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "7"}
    new["status"] = {"readyReplicas": 2}
    assert update("deployments", "Deployment", old, new, group="apps") is None


# --- ConfigMaps and Secrets ---

def test_configmap_change_lists_keys_not_values():
    old = {"metadata": {"name": "api-config"}, "data": {"LOG_LEVEL": "info", "DB_URL": "postgres://u:hunter2@db"}}
    new = {"metadata": {"name": "api-config"}, "data": {"LOG_LEVEL": "debug", "DB_URL": "postgres://u:hunter3@db", "NEW": "1"}}
    e = update("configmaps", "ConfigMap", old, new, name="api-config")
    assert e["category"] == "config"
    assert activity.describe(e) == "Changed ConfigMap payments/api-config (keys: DB_URL, LOG_LEVEL, NEW) by alice@corp"
    assert "hunter" not in json.dumps(e)


def test_secret_change_keeps_neither_key_names_nor_values():
    old = {"metadata": {"name": "db"}, "type": "Opaque", "data": {"password": "b2xk", "user": "YQ=="}}
    new = {"metadata": {"name": "db"}, "type": "Opaque", "data": {"password": "bmV3", "user": "YQ==", "host": "ZGI="}}
    e = update("secrets", "Secret", old, new, name="db")
    assert e["category"] == "config"
    assert activity.describe(e) == "Changed Secret payments/db (2 key(s) changed) by alice@corp"
    text = json.dumps(e)
    assert "password" not in text and "host" not in text and "bmV3" not in text


def test_update_without_a_visible_change_is_dropped():
    noise = "kubectl.kubernetes.io/last-applied-configuration"
    old = {"metadata": {"name": "c", "resourceVersion": "1", "managedFields": [1], "annotations": {noise: "{a}"}},
           "data": {"A": "1"}}
    new = {"metadata": {"name": "c", "resourceVersion": "2", "managedFields": [2], "annotations": {noise: "{b}"}},
           "data": {"A": "1"}, "status": {"x": 1}}
    assert update("configmaps", "ConfigMap", old, new) is None


def test_helm_release():
    release = {"metadata": {"name": "sh.helm.release.v1.api.v12", "labels": {"owner": "helm", "status": "deployed"}}}
    e = activity.parse_admission(review("CREATE", "secrets", kind="Secret", name="sh.helm.release.v1.api.v12", obj=release))
    assert e["category"] == "rollout"
    assert activity.describe(e) == "Helm install/upgrade of payments/api (revision 12) by alice@corp"
    # helm relabels and prunes its own history, that is not worth a line
    superseded = {"metadata": {"name": "sh.helm.release.v1.api.v11", "labels": {"owner": "helm", "status": "superseded"}}}
    assert update("secrets", "Secret", release, superseded, name="sh.helm.release.v1.api.v11") is None
    assert activity.parse_admission(review("DELETE", "secrets", kind="Secret", name="sh.helm.release.v1.api.v2")) is None


# --- pods and nodes ---

def test_deleted_pod_and_eviction():
    e = activity.parse_admission(review("DELETE", "pods", kind="Pod", name="api-7d9f8b-x"))
    assert e["category"] == "pods" and activity.describe(e) == "Deleted Pod payments/api-7d9f8b-x by alice@corp"
    drained = activity.parse_admission(review("CREATE", "pods", "eviction", "Eviction", name="api-7d9f8b-x",
                                              obj={"kind": "Eviction"}))
    assert drained["category"] == "pods" and drained["kind"] == "Pod"
    assert activity.describe(drained) == "Eviction of Pod payments/api-7d9f8b-x requested by alice@corp"


def test_pod_started_by_hand():
    pod = {"metadata": {"generateName": "debug-"}, "spec": {"containers": [{"name": "c", "image": "busybox:1.36"}]}}
    e = activity.parse_admission(review("CREATE", "pods", kind="Pod", name="", obj=pod))
    assert activity.describe(e) == "Created Pod payments/debug-* (busybox:1.36) by alice@corp"


def test_debug_container():
    old = {"spec": {"ephemeralContainers": [{"name": "debugger-a", "image": "busybox"}]}}
    new = {"spec": {"ephemeralContainers": [{"name": "debugger-a", "image": "busybox"},
                                            {"name": "debugger-b", "image": "nicolaka/netshoot", "command": ["sh"]}]}}
    e = activity.parse_admission(review("UPDATE", "pods", "ephemeralcontainers", "Pod", old=old, obj=new))
    assert e["category"] == "exec"
    assert activity.describe(e) == \
        "alice@corp started a debug container in pod payments/api (debugger-b, nicolaka/netshoot): `sh`"


def test_node_cordon_uncordon_and_taints():
    ready, cordoned = {"spec": {}}, {"spec": {"unschedulable": True}}
    e = update("nodes", "Node", ready, cordoned, ns="", name="ip-10-0-1-1")
    assert e["category"] == "node" and activity.describe(e) == "Cordoned Node ip-10-0-1-1 by alice@corp"
    assert activity.describe(update("nodes", "Node", cordoned, ready, ns="", name="ip-10-0-1-1")) == \
        "Uncordoned Node ip-10-0-1-1 by alice@corp"
    tainted = {"spec": {"taints": [{"key": "dedicated", "effect": "NoSchedule"}]}}
    assert activity.describe(update("nodes", "Node", ready, tainted, ns="", name="ip-10-0-1-1")) == \
        "Changed Node ip-10-0-1-1 (spec.taints) by alice@corp"


# --- everything else ---

def test_service_and_ingress_changes_name_the_fields():
    old = {"metadata": {"name": "api"}, "spec": {"type": "ClusterIP", "ports": [{"port": 80}]}}
    new = {"metadata": {"name": "api"}, "spec": {"type": "LoadBalancer", "ports": [{"port": 80}]}}
    e = update("services", "Service", old, new)
    assert e["category"] == "update" and activity.describe(e) == "Changed Service payments/api (spec.type) by alice@corp"
    scheme = "alb.ingress.kubernetes.io/scheme"
    ing = update("ingresses", "Ingress", {"metadata": {"annotations": {scheme: "internal"}}, "spec": {}},
                 {"metadata": {"annotations": {scheme: "internet-facing"}}, "spec": {}}, group="networking.k8s.io")
    assert activity.describe(ing) == f"Changed Ingress payments/api (annotations: {scheme}) by alice@corp"


def test_namespace_label_change():
    label = "pod-security.kubernetes.io/enforce"
    e = update("namespaces", "Namespace", {"metadata": {"labels": {label: "restricted"}}},
               {"metadata": {"labels": {label: "privileged"}}}, ns="", name="uat")
    assert activity.describe(e) == f"Changed Namespace uat (labels: {label}) by alice@corp"


def test_cronjob_change_and_manual_job():
    e = update("cronjobs", "CronJob", {"spec": {"schedule": "0 2 * * *"}}, {"spec": {"schedule": "0 3 * * *"}},
               group="batch", name="nightly")
    assert e["category"] == "rollout"
    assert activity.describe(e) == "Changed CronJob payments/nightly (spec.schedule) by alice@corp"
    job = activity.parse_admission(review("CREATE", "jobs", kind="Job", group="batch", name="nightly-manual"))
    assert job["category"] == "rollout" and job["action"] == "created"


@pytest.mark.parametrize("resource,kind,category", [
    ("networkpolicies", "NetworkPolicy", "security"), ("customresourcedefinitions", "CustomResourceDefinition", "security"),
    ("serviceaccounts", "ServiceAccount", "security"), ("horizontalpodautoscalers", "HorizontalPodAutoscaler", "change"),
    ("persistentvolumeclaims", "PersistentVolumeClaim", "change"), ("applications", "Application", "change"),
])
def test_any_other_kind_is_reported(resource, kind, category):
    created = activity.parse_admission(review("CREATE", resource, kind=kind))
    assert created["category"] == category and activity.describe(created) == f"Created {kind} payments/api by alice@corp"
    changed = update(resource, kind, {"spec": {"a": 1}}, {"spec": {"a": 2}})
    assert changed["category"] == ("update" if category == "change" else category)
    assert activity.describe(changed) == f"Changed {kind} payments/api (spec.a) by alice@corp"


def test_ignored_kinds(monkeypatch):
    monkeypatch.setattr(activity, "IGNORE_KINDS", {"pod", "job"})
    assert activity.parse_admission(review("DELETE", "pods", kind="Pod")) is None
    assert activity.parse_admission(review("CREATE", "jobs", kind="Job", group="batch")) is None
    assert activity.parse_admission(review("CONNECT", "pods", "exec", obj={"command": ["sh"]})) is None
    assert activity.parse_admission(review("CREATE", "services", kind="Service"))


@pytest.fixture
def feed(monkeypatch):
    sent = []
    f = activity.ActivityFeed(batch_seconds=60, notify=sent.append)
    monkeypatch.setattr(activity, "FEED", f)
    return f, sent


def test_webhook_always_allows_and_records(feed):
    f, _ = feed
    client = TestClient(activity.webhook_app)
    r = client.post("/admission", json=review("CONNECT", "pods", "exec", obj={"command": ["bash"]}, uid="abc"))
    assert r.json() == {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                        "response": {"uid": "abc", "allowed": True}}
    assert f.calls_total == 1 and f.latest()[0]["command"] == "bash"
    bad = client.post("/admission", content=b"not json", headers={"content-type": "application/json"})
    assert bad.status_code == 200 and bad.json()["response"]["allowed"] is True


def test_flush_sends_one_grouped_card(feed):
    f, sent = feed
    for i in range(33):
        f.record(activity.parse_admission(review("CREATE", "services", kind="Service", name=f"svc-{i}")))
    f.record(activity.parse_admission(review("CONNECT", "pods", "exec", obj={"command": ["sh"]})))
    card = f.flush()
    assert len(sent) == 1 and sent[0] is card
    text = json.dumps(card)
    assert "Cluster activity: 34 event(s)" in text and "and 4 more" in text
    assert text.index("Exec / attach") < text.index("Created / deleted")
    assert card["body"][0]["style"] == "warning"  # exec present: highlighted
    assert f.flush() is None  # nothing new


def test_card_sections_and_repeated_lines(feed):
    f, sent = feed
    for _ in range(3):  # a drain retries the same eviction while a PodDisruptionBudget blocks it
        f.record(activity.parse_admission(review("CREATE", "pods", "eviction", "Eviction", name="api-1")))
    f.record(update("configmaps", "ConfigMap", {"data": {"A": "1"}}, {"data": {"A": "2"}}, name="api-config"))
    f.record(update("deployments", "Deployment", deploy({"app": "api:1"}),
                    deploy({"app": "api:1"}, restarted_at="2026-11-20T10:00:00Z"), group="apps"))
    f.record(update("nodes", "Node", {"spec": {}}, {"spec": {"unschedulable": True}}, ns="", name="ip-10-0-1-1"))
    text = json.dumps(f.flush())
    assert "Cluster activity: 6 event(s)" in text
    assert "Eviction of Pod payments/api-1 requested by alice@corp (x3)" in text
    titles = ["ConfigMaps and Secrets", "Deployments, restarts and scaling", "Pods deleted, evicted", "Nodes"]
    positions = [text.index(t) for t in titles]
    assert positions == sorted(positions)
    assert sent[0]["body"][0]["style"] == "accent"  # nothing here is exec or a security change


def test_status_without_certificate(feed, monkeypatch, tmp_path):
    f, _ = feed
    monkeypatch.setattr(activity, "TLS_DIR", str(tmp_path))
    status = f.status()
    assert status["webhook_certificate"] is False and status["targets"] == ["alert-1"]
