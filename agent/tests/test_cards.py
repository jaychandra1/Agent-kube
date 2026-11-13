import json

import cards
from conftest import SAMPLE_RCA

INC = {"key": "default/Deployment/web/CrashLoop", "namespace": "default", "workload": "Deployment/web",
       "reason": "CrashLoop", "pod": "web-1", "pods": ["web-1"], "detail": "exit 1"}


def test_message_envelope_for_workflows():
    msg = cards.message(cards.alert_card(INC, SAMPLE_RCA))
    assert msg["type"] == "message"
    att = msg["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard" and att["content"]["version"] == "1.2"
    assert "rca default/web-1" in json.dumps(msg)


def test_long_text_is_truncated():
    huge = dict(SAMPLE_RCA, root_cause="x" * 100_000, resolution=["y" * 10_000] * 50,
                evidence=[{"source": "logs", "excerpt": "z" * 50_000}] * 10)
    msg = cards.message(cards.rca_card(huge))
    assert len(json.dumps(msg)) < 28_000


def test_oversized_card_is_cut(monkeypatch):
    monkeypatch.setattr(cards, "MAX_CARD_BYTES", 1500)
    msg = cards.message(cards.answer_card("q", "Asha", "a" * 5000))
    assert "too long" in json.dumps(msg) and len(json.dumps(msg)) <= 1500


def test_node_incident_card_without_rca():
    node = {"key": "/Node/n1/NodeNotReady", "namespace": "", "workload": "Node/n1", "reason": "NodeNotReady",
            "pod": None, "pods": [], "detail": "Ready=False"}
    text = json.dumps(cards.alert_card(node, None))
    assert "(cluster)" in text and "@kubesupport rca" not in text
