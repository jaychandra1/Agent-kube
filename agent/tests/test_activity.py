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


def deploy(images, replicas=2):
    return {"metadata": {"name": "api"}, "spec": {"replicas": replicas, "template": {"spec": {
        "containers": [{"name": n, "image": i} for n, i in images.items()]}}}}


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
    assert e["category"] == "security" and e["name"] == "db-pass"
    assert "c2VjcmV0" not in json.dumps(e) and "data" not in e


def test_image_change_is_reported_but_noop_update_is_not():
    changed = activity.parse_admission(review("UPDATE", "deployments", kind="Deployment", group="apps",
                                              old=deploy({"app": "api:1.2"}), obj=deploy({"app": "api:1.3"})))
    assert changed["category"] == "update" and "image app: api:1.2 → api:1.3" in changed["detail"]
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


def test_status_without_certificate(feed, monkeypatch, tmp_path):
    f, _ = feed
    monkeypatch.setattr(activity, "TLS_DIR", str(tmp_path))
    status = f.status()
    assert status["webhook_certificate"] is False and status["targets"] == ["alert-1"]
