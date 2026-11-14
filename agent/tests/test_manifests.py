"""Checks of deploy/*.yaml (and the rename), so a broken manifest fails CI."""
import re
from pathlib import Path

import yaml

import activity

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
SA = [{"kind": "ServiceAccount", "name": "kubesupport", "namespace": "kubesupport"}]


def load(name):
    return [d for d in yaml.safe_load_all((ROOT / "deploy" / name).read_text(encoding="utf-8")) if d]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def settings_read_by_code() -> set:
    names = set()
    for f in AGENT_DIR.glob("*.py"):
        names |= set(re.findall(r'(?:os\.getenv|_env_float)\("([A-Z_]+)"', f.read_text(encoding="utf-8")))
    return names


DOCS = load("deployment.yaml")
WEBHOOK_DOCS = load("activity-webhook.yaml")
SECRET_KEYS = {"GROQ_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY", "AGENT_API_KEY", "TEAMS_ALERT_WEBHOOK_URLS",
               "TEAMS_REPLY_WEBHOOK_URL", "TEAMS_OUTGOING_WEBHOOK_SECRET"}


def test_all_objects_present_and_named_kubesupport():
    kinds = sorted(d["kind"] for d in DOCS)
    assert kinds == sorted(["Namespace", "ServiceAccount", "ClusterRoleBinding", "ClusterRole",
                            "ClusterRoleBinding", "ConfigMap", "Deployment", "Service"])
    for d in DOCS + WEBHOOK_DOCS:
        assert d["metadata"]["name"].startswith("kubesupport"), d["metadata"]["name"]
        if d["kind"] not in ("Namespace", "ClusterRole", "ClusterRoleBinding", "ValidatingWebhookConfiguration"):
            assert d["metadata"]["namespace"] == "kubesupport", d["kind"]


def test_permissions_cluster_wide_read_only_never_secrets():
    bindings = {b["metadata"]["name"]: b for b in by_kind(DOCS, "ClusterRoleBinding")}
    assert bindings["kubesupport-view"]["roleRef"]["name"] == "view"  # built-in: everything except Secrets
    assert bindings["kubesupport-cluster-read"]["roleRef"]["name"] == "kubesupport-cluster-read"
    assert all(b["subjects"] == SA for b in bindings.values())
    (role,) = by_kind(DOCS, "ClusterRole")
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
        assert "secrets" not in rule["resources"] and "*" not in rule["resources"] and "*" not in rule["apiGroups"]
    readable = {r for rule in role["rules"] for r in rule["resources"]}
    assert {"pods", "pods/log", "events", "nodes", "namespaces", "deployments", "replicasets",
            "clusterroles", "clusterrolebindings", "customresourcedefinitions"} <= readable


def test_configmap_keys_are_real_settings():
    (cm,) = by_kind(DOCS, "ConfigMap")
    unknown = set(cm["data"]) - settings_read_by_code()
    assert not unknown, f"ConfigMap keys the code never reads: {unknown}"
    assert cm["data"]["ALLOWED_NAMESPACES"] == "*"
    assert cm["data"]["ACTIVITY_IGNORE_USERS"] == activity.DEFAULT_IGNORE_USERS


def test_secret_keys_are_real_settings():
    assert SECRET_KEYS <= settings_read_by_code()


def test_deployment():
    (dep,) = by_kind(DOCS, "Deployment")
    spec = dep["spec"]
    assert spec["replicas"] == 1 and spec["strategy"]["type"] == "Recreate"
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "kubesupport"
    assert spec["selector"]["matchLabels"].items() <= spec["template"]["metadata"]["labels"].items()
    (c,) = pod["containers"]
    assert c["image"].startswith("docker.io/") and c["image"].endswith("/kubesupport:latest")
    assert {p["name"]: p["containerPort"] for p in c["ports"]} == {"http": 8000, "webhook": 8443}
    env_from = {next(iter(k for k in e if k.endswith("Ref"))): e for e in c["envFrom"]}
    assert env_from["configMapRef"]["configMapRef"]["name"] == "kubesupport-config"
    assert env_from["secretRef"]["secretRef"] == {"name": "kubesupport-secrets", "optional": True}
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["readinessProbe"]["httpGet"]["path"] == "/healthz"
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert mounts["tmp"]["mountPath"] == "/tmp"
    assert mounts["webhook-tls"]["mountPath"] == activity.TLS_DIR and mounts["webhook-tls"]["readOnly"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["webhook-tls"]["secret"] == {"secretName": "kubesupport-webhook-tls", "optional": True}


def test_service_targets_the_pod():
    (svc,) = by_kind(DOCS, "Service")
    (dep,) = by_kind(DOCS, "Deployment")
    assert svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"]
    assert svc["spec"]["type"] == "ClusterIP"


def test_ingress_exposes_only_the_teams_webhook():
    (ing,) = load("ingress.yaml")
    assert ing["metadata"]["namespace"] == "kubesupport"
    (rule,) = ing["spec"]["rules"]
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/teams/webhook" and path["pathType"] == "Exact"
    assert path["backend"]["service"] == {"name": "kubesupport", "port": {"name": "http"}}


def test_activity_webhook_never_blocks_and_points_at_kubesupport():
    (svc,) = by_kind(WEBHOOK_DOCS, "Service")
    (vwc,) = by_kind(WEBHOOK_DOCS, "ValidatingWebhookConfiguration")
    assert svc["spec"]["ports"] == [{"name": "webhook", "port": 443, "targetPort": "webhook"}]
    (hook,) = vwc["webhooks"]
    assert hook["failurePolicy"] == "Ignore" and hook["sideEffects"] == "NoneOnDryRun"
    assert hook["timeoutSeconds"] <= 5 and hook["admissionReviewVersions"] == ["v1"]
    assert hook["clientConfig"]["service"] == {"name": "kubesupport-webhook", "namespace": "kubesupport",
                                               "path": "/admission", "port": 443}
    assert hook["clientConfig"]["caBundle"] == "<CA_BUNDLE>"  # filled in by deploy/webhook-cert.sh
    connect = [r for r in hook["rules"] if r["operations"] == ["CONNECT"]]
    assert connect and {"pods/exec", "pods/attach", "pods/portforward"} <= set(connect[0]["resources"])


def test_every_resource_the_webhook_sends_is_understood():
    """For each rule in activity-webhook.yaml, a typical request must produce an event."""
    (vwc,) = by_kind(WEBHOOK_DOCS, "ValidatingWebhookConfiguration")
    for rule in vwc["webhooks"][0]["rules"]:
        for res in rule["resources"]:
            base, _, sub = res.partition("/")
            op = rule["operations"][0]
            req = {"uid": "u", "operation": op, "resource": {"resource": base}, "subResource": sub,
                   "kind": {"kind": "X"}, "namespace": "default", "name": "n", "userInfo": {"username": "alice"},
                   "object": {"spec": {"replicas": 3}}, "oldObject": {"spec": {"replicas": 1}}}
            if op == "UPDATE" and not sub and base not in activity.RBAC:
                req["object"] = {"spec": {"template": {"spec": {"containers": [{"name": "c", "image": "b"}]}}}}
                req["oldObject"] = {"spec": {"template": {"spec": {"containers": [{"name": "c", "image": "a"}]}}}}
            assert activity.parse_admission({"request": req}), f"{op} {res} produced no event"


def test_old_name_is_gone_everywhere():
    pattern = re.compile(r"sre-agent|SRE [Aa]gent")
    files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
             and p.suffix in {".py", ".md", ".yaml", ".yml", ".j2", ".sh", ".txt", ""}]
    leftovers = [str(p.relative_to(ROOT)) for p in files if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
                 and p.name != "test_manifests.py"]
    assert not leftovers, leftovers
