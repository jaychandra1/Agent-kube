import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import teams
from test_teams import sign


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_health(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_teams_webhook_requires_valid_signature(client):
    body = json.dumps({"type": "message", "id": "app-1", "text": "<at>kubesupport</at> help"}).encode()
    assert client.post("/teams/webhook", content=body).status_code == 401
    assert client.post("/teams/webhook", content=body, headers={"Authorization": "HMAC bad"}).status_code == 401
    r = client.post("/teams/webhook", content=body, headers={"Authorization": sign(body)})
    assert r.status_code == 200 and r.json()["type"] == "message" and "status" in r.json()["text"]


def test_teams_webhook_disabled_without_secret(client, monkeypatch):
    monkeypatch.setattr(teams, "OUTGOING_SECRET", "")
    assert client.post("/teams/webhook", content=b"{}").status_code == 404


def test_alerts_test_posts_sample_card(client, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "API_KEY", "k")
    monkeypatch.setattr(teams, "post_card", lambda urls, card: sent.append((urls, card)) or [{"ok": True}])
    r = client.post("/alerts/test", headers={"x-api-key": "k"})
    assert r.status_code == 200 and r.json()["sent_to"] == 1
    assert sent[0][0] == [("alert-1", "https://example.invalid/alert")]


def test_alerts_status_and_api_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "k")
    assert client.get("/alerts/status").status_code == 401
    status = client.get("/alerts/status", headers={"x-api-key": "k"}).json()
    assert status["teams"] == {"alert_targets": ["alert-1"], "reply_webhook": "reply-1", "outgoing_webhook": True}
    assert status["watcher"]["running"] is False  # WATCH_ENABLED=false in tests


def test_public_port_serves_only_the_teams_webhook():
    """Port 8080 is what the Cloudflare Tunnel reaches: nothing but POST /teams/webhook."""
    with TestClient(app_module.teams_app) as public:
        body = json.dumps({"type": "message", "id": "pub-1", "text": "<at>kubesupport</at> help"}).encode()
        assert public.post("/teams/webhook", content=body).status_code == 401
        assert public.post("/teams/webhook", content=body, headers={"Authorization": sign(body)}).status_code == 200
        for path in ("/healthz", "/alerts/status", "/activity", "/rca/latest", "/docs", "/openapi.json"):
            assert public.get(path).status_code == 404, path
        assert public.post("/rca", json={}).status_code == 404
        assert public.post("/chat", json={"message": "hi"}).status_code == 404
        assert public.get("/teams/webhook").status_code == 405
