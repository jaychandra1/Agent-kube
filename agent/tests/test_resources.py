"""Generic read tools (list_resources / get_resource) with a fake dynamic client, plus `get` in Teams."""
import copy
import json
import uuid
from types import SimpleNamespace as NS

import pytest

import main
import activity
import teams
import tools


class FakeObj:
    def __init__(self, d):
        self.d = d

    def to_dict(self):
        return copy.deepcopy(self.d)


class FakeResource:
    def __init__(self, kind, name, namespaced=True, group="", short=(), singular="", items=(), preferred=True,
                 verbs=("get", "list", "watch")):
        self.kind, self.name, self.namespaced, self.group = kind, name, namespaced, group
        self.short_names, self.singular_name, self.preferred, self.verbs = list(short), singular, preferred, list(verbs)
        self.group_version = f"{group}/v1" if group else "v1"
        self.items = list(items)
        self.calls = []

    def get(self, name=None, namespace=None, limit=None, _request_timeout=None):
        assert _request_timeout, "every API call needs a timeout"
        self.calls.append((name, namespace))
        if name:
            return FakeObj(next(i for i in self.items if i["metadata"]["name"] == name
                                and (namespace is None or i["metadata"].get("namespace") == namespace)))
        return NS(items=[FakeObj(i) for i in self.items
                         if namespace is None or i["metadata"].get("namespace") == namespace])


def svc(name, ns):
    return {"metadata": {"name": name, "namespace": ns, "creationTimestamp": "2026-11-01T00:00:00Z"},
            "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.1", "ports": [{"port": 80, "protocol": "TCP"}]}}


@pytest.fixture
def cluster(monkeypatch):
    services = FakeResource("Service", "services", short=["svc"], singular="service",
                            items=[svc("api", "default"), svc("web", "apps"), svc("other", "kube-system")])
    nodes = FakeResource("Node", "nodes", namespaced=False, short=["no"], singular="node", items=[
        {"metadata": {"name": "ip-10-0-1-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                                         "nodeInfo": {"kubeletVersion": "v1.30.2"}}}])
    configmaps = FakeResource("ConfigMap", "configmaps", short=["cm"], singular="configmap", items=[
        {"metadata": {"name": "app", "namespace": "default", "managedFields": [{"x": 1}],
                      "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"}},
         "data": {"DB_HOST": "db", "DB_PASSWORD": "hunter2"}}])
    secrets = FakeResource("Secret", "secrets", short=[], singular="secret")
    pod_logs = FakeResource("Pod", "pods/log")  # a subresource: must never match
    old_svc = FakeResource("Service", "services", group="legacy.example.com", preferred=False)
    fake = NS(resources=NS(search=lambda **kw: [pod_logs, old_svc, services, nodes, configmaps, secrets]))
    monkeypatch.setattr(tools, "_dynamic", fake)
    monkeypatch.setattr(tools, "ALLOWED", {"*"})
    return NS(services=services, nodes=nodes, configmaps=configmaps)


@pytest.mark.parametrize("name", ["Service", "service", "services", "svc", "SVC"])
def test_kind_lookup_by_kind_plural_or_short_name(cluster, name):
    assert tools._resolve_kind(name) is cluster.services  # preferred core version wins over the legacy group


@pytest.mark.parametrize("name", ["secret", "Secrets"])
def test_secrets_are_refused(cluster, name):
    with pytest.raises(ValueError, match="never reads Secrets"):
        tools._resolve_kind(name)


def test_unknown_kind(cluster):
    with pytest.raises(ValueError, match="unknown kind"):
        tools.list_resources("widgets")


def test_list_services_everywhere_and_in_one_namespace(cluster):
    everywhere = tools.list_resources("svc")
    assert everywhere["count"] == 3
    one = tools.list_resources("services", "default")
    assert [r["name"] for r in one["items"]] == ["api"]
    assert one["items"][0]["status"] == "ClusterIP 10.0.0.1 ports 80/TCP"


def test_namespace_allowlist_is_respected(cluster, monkeypatch):
    monkeypatch.setattr(tools, "ALLOWED", {"default", "apps"})
    assert {r["namespace"] for r in tools.list_resources("svc")["items"]} == {"default", "apps"}
    with pytest.raises(ValueError, match="not allowed"):
        tools.list_resources("svc", "kube-system")


def test_cluster_scoped_kind(cluster):
    nodes = tools.list_resources("nodes")
    assert nodes["items"][0]["status"] == "Ready=True kubelet v1.30.2"


def test_get_resource_is_trimmed_and_redacted(cluster):
    text = tools.get_resource("cm", "app", "default")
    assert "managedFields" not in text and "last-applied-configuration" not in text
    assert "hunter2" not in text and "[REDACTED]" in text and '"DB_HOST": "db"' in text
    with pytest.raises(ValueError, match="namespaced"):
        tools.get_resource("cm", "app")


def teams_msg(text):
    return {"type": "message", "id": str(uuid.uuid4()), "text": f"<at>kubesupport</at> {text}",
            "from": {"name": "Asha"}, "conversation": {"id": "c1"}}


def test_teams_get_lists_and_shows_objects(cluster):
    listing = teams.handle_outgoing(teams_msg("get services default"))["text"]
    assert "1 Service" in listing and "api" in listing
    node = teams.handle_outgoing(teams_msg("get node ip-10-0-1-1"))["text"]
    assert "v1.30.2" in node and cluster.nodes.calls[-1] == ("ip-10-0-1-1", None)
    assert "never reads Secrets" in teams.handle_outgoing(teams_msg("get secrets default"))["text"]


def test_teams_activity_command(monkeypatch):
    feed = activity.ActivityFeed(notify=lambda card: None)
    monkeypatch.setattr(activity, "FEED", feed)
    assert "No cluster activity recorded yet" in teams.handle_outgoing(teams_msg("activity"))["text"]
    feed.record({"time": "2026-11-13T12:00:00+00:00", "user": "alice", "namespace": "payments", "name": "api-1",
                 "category": "exec", "action": "exec", "kind": "Pod", "command": "sh"})
    assert "alice exec into pod payments/api-1: `sh`" in teams.handle_outgoing(teams_msg("activity"))["text"]


def test_main_starts_webhook_only_with_a_certificate(monkeypatch, tmp_path):
    monkeypatch.setattr(activity, "TLS_DIR", str(tmp_path))
    api, public, webhook = main.build_configs()
    assert api.port == 8000 and public.port == 8080 and webhook is None
    (tmp_path / "tls.crt").write_text("cert")
    (tmp_path / "tls.key").write_text("key")
    api, public, webhook = main.build_configs()
    assert webhook.port == 8443 and webhook.ssl_certfile == str(tmp_path / "tls.crt")
    monkeypatch.setattr(activity, "ENABLED", False)
    assert main.build_configs()[2] is None


def ingress(name, ns, host, paths, tls=False, annotations=None, address="k8s-shop-1234.eu-west-1.elb.amazonaws.com"):
    return {"metadata": {"name": name, "namespace": ns, "creationTimestamp": "2026-11-01T00:00:00Z",
                         "annotations": annotations or {}},
            "spec": {"ingressClassName": "alb", **({"tls": [{"hosts": [host]}]} if tls else {}),
                     "rules": [{"host": host, "http": {"paths": [
                         {"path": p, "backend": {"service": {"name": s, "port": {"number": n}}}}
                         for p, s, n in paths]}}]},
            "status": {"loadBalancer": {"ingress": [{"hostname": address}] if address else []}}}


def test_ingress_listing_gives_urls_backends_and_address():
    text = tools._ingress_summary(ingress("shop", "apps", "shop.example.com",
                                          [("/", "web", 80), ("/api", "api", 8080)], tls=True))
    assert text == ("class alb; address k8s-shop-1234.eu-west-1.elb.amazonaws.com; "
                    "https://shop.example.com/ -> web:80; https://shop.example.com/api -> api:8080")


def test_ingress_scheme_and_missing_address():
    plain = tools._ingress_summary(ingress("a", "apps", "a.example.com", [("/", "web", 80)], address=None))
    assert "http://a.example.com/ -> web:80" in plain and "address not assigned yet" in plain
    acm = tools._ingress_summary(ingress("b", "apps", "b.example.com", [("/", "web", 80)], annotations={
        "alb.ingress.kubernetes.io/certificate-arn": "arn:aws:acm:eu-west-1:1:certificate/x"}))
    assert "https://b.example.com/ -> web:80" in acm  # ALB + ACM: HTTPS without spec.tls
    many = tools._ingress_summary(ingress("c", "apps", "c.example.com", [(f"/p{i}", "web", 80) for i in range(20)]))
    assert "+5 more routes" in many


def test_service_listing_shows_the_external_address():
    obj = svc("public", "apps")
    obj["spec"]["type"] = "LoadBalancer"
    obj["status"] = {"loadBalancer": {"ingress": [{"hostname": "a1b2.elb.amazonaws.com"}]}}
    assert tools._summary("Service", obj) == "LoadBalancer 10.0.0.1 ports 80/TCP external a1b2.elb.amazonaws.com"
    assert tools._summary("Service", svc("internal", "apps")) == "ClusterIP 10.0.0.1 ports 80/TCP"


def test_tools_offered_to_the_model_include_generic_reads():
    names = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert {"list_resources", "get_resource"} <= names
    assert json.dumps(tools.TOOL_SCHEMAS)  # serialisable for the API
