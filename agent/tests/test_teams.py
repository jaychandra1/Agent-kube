import base64
import hashlib
import hmac
import time
import uuid

import pytest

import agent
import teams
from conftest import SAMPLE_RCA, TEST_SECRET, fake_llm


def sign(body: bytes) -> str:
    digest = hmac.new(base64.b64decode(TEST_SECRET), body, hashlib.sha256).digest()
    return "HMAC " + base64.b64encode(digest).decode()


@pytest.fixture
def posted(monkeypatch):
    calls = []
    monkeypatch.setattr(teams, "post_card", lambda url, card, thread=None: calls.append((url, card, thread)) or [])
    return calls


def activity(text, msg_id=None, tenant="t1", aad="u1"):
    return {"type": "message", "id": msg_id or str(uuid.uuid4()), "text": text,
            "from": {"name": "Asha", "aadObjectId": aad},
            "conversation": {"id": "19:abc@thread.tacv2;messageid=1731916515731"},
            "channelData": {"tenant": {"id": tenant}, "channel": {"id": "19:abc@thread.tacv2"}}}


THREAD = {"threadMessageId": "1731916515731", "channelId": "19:abc@thread.tacv2"}


def wait_for(predicate, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_hmac_valid_tampered_and_missing():
    body = b'{"text": "hi"}'
    assert teams.verify_hmac(body, sign(body))
    assert not teams.verify_hmac(body + b" ", sign(body))
    assert not teams.verify_hmac(body, None)
    assert not teams.verify_hmac(body, "Bearer abc")


def test_strip_mentions_and_parse():
    assert teams.strip_mentions("<at>kubesupport</at>&nbsp;pods&nbsp; default<br>") == "pods default"
    assert teams.parse_command("pods default") == ("pods", ["default"])
    assert teams.parse_command("why is web down?")[0] == "ask"
    assert teams.parse_command("") == ("help", [])


def test_help():
    assert "status" in teams.handle_outgoing(activity("<at>kubesupport</at> help"))["text"]


def test_pods_answered_immediately(monkeypatch):
    monkeypatch.setattr(teams.tools, "list_pods", lambda ns: [
        {"name": "web-1", "ready": "0/1", "phase": "Running", "restarts": 5, "waiting": ["CrashLoopBackOff"]}])
    reply = teams.handle_outgoing(activity("<at>kubesupport</at> pods default"))["text"]
    assert "web-1" in reply and "CrashLoopBackOff" in reply and reply.startswith("```")


def test_namespace_outside_allowlist_is_refused():
    reply = teams.handle_outgoing(activity("<at>kubesupport</at> pods kube-system"))["text"]
    assert "not allowed" in reply


def test_bad_logs_arguments():
    assert "<namespace>/<pod>" in teams.handle_outgoing(activity("<at>kubesupport</at> logs web-1"))["text"]


def test_other_tenant_is_rejected(monkeypatch):
    monkeypatch.setattr(teams, "TENANT_ID", "t1")
    assert "own organization" in teams.handle_outgoing(activity("help", tenant="evil"))["text"]


def test_allowed_users(monkeypatch):
    monkeypatch.setattr(teams, "ALLOWED_USER_IDS", {"u1"})
    assert "status" in teams.handle_outgoing(activity("help", aad="u1"))["text"]
    assert "allowed-users" in teams.handle_outgoing(activity("help", aad="u2"))["text"]


def test_retried_message_is_answered_once():
    first = teams.handle_outgoing(activity("help", msg_id="dup-1"))
    second = teams.handle_outgoing(activity("help", msg_id="dup-1"))
    assert "status" in first["text"] and second["text"] == "(already answered)"


def test_question_without_an_ai_model():
    assert "No AI model is configured" in teams.handle_outgoing(activity("<at>kubesupport</at> why is web down?"))["text"]


def test_question_is_answered_later_through_reply_webhook(monkeypatch, posted):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "chat", lambda msgs, q: f"answer to: {q}")
    reply = teams.handle_outgoing(activity("<at>kubesupport</at> why is web down?"))
    assert reply["text"].startswith("Looking into it")
    assert wait_for(lambda: posted)
    target, card, thread = posted[0]
    assert target == ("reply-1", "https://example.invalid/reply")
    assert "answer to: why is web down?" in str(card)
    assert thread == THREAD  # the answer says which thread it belongs to


def test_rca_answer_carries_the_thread_too(monkeypatch, posted):
    monkeypatch.setattr(agent, "investigate_and_rca", lambda *a, **k: dict(SAMPLE_RCA, generated_by="test"))
    teams.handle_outgoing(activity("<at>kubesupport</at> rca checkout returns 500"))
    assert wait_for(lambda: posted) and posted[0][2] == THREAD


def test_thread_is_found_for_replies_and_odd_payloads():
    reply_in_thread = {"id": "1731916599999", "replyToId": "1731916515731",
                       "conversation": {"id": "19:abc@thread.tacv2;messageid=1731916515731"}}
    assert teams.thread_of(reply_in_thread) == THREAD  # the thread's first message, not the reply
    assert teams.thread_of({"id": "42", "conversation": {"id": "19:abc@thread.tacv2"}}) == {
        "threadMessageId": "42", "channelId": "19:abc@thread.tacv2"}
    assert teams.thread_of({}) == {}


def test_thread_is_sent_to_the_workflow(monkeypatch):
    sent = []
    monkeypatch.setattr(teams.requests, "post",
                        lambda url, json=None, timeout=None: sent.append(json) or FakeResponse(202))
    teams.post_card(("reply-1", "https://a"), dict(CARD), THREAD)
    teams.post_card(("chat", "https://a"), dict(CARD))
    assert sent[0]["threadMessageId"] == "1731916515731" and sent[0]["channelId"] == "19:abc@thread.tacv2"
    assert sent[0]["type"] == "message" and sent[0]["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert "threadMessageId" not in sent[1]  # alerts aren't answers: no thread


def test_mention_in_the_middle_leaves_clean_text():
    assert teams.strip_mentions("Hi <at>KubeSupport</at>, can you list the ingresses?") == \
        "Hi, can you list the ingresses?"
    assert teams.strip_mentions("<at>kubesupport</at> logs apps/web-1 --previous") == "logs apps/web-1 --previous"


def test_slow_quick_command_falls_back_to_reply_webhook(monkeypatch, posted):
    monkeypatch.setattr(teams, "QUICK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(teams, "_status_text", lambda: time.sleep(0.3) or "all good")
    reply = teams.handle_outgoing(activity("<at>kubesupport</at> status"))
    assert reply["text"].startswith("Still working")
    assert wait_for(lambda: posted) and "all good" in str(posted[0][1]) and posted[0][2] == THREAD


SECRET_URL = "https://example.invalid/workflows/abc/invoke?sig=TOP-SECRET-SIGNATURE"
CARD = {"type": "AdaptiveCard", "body": []}


class FakeResponse:
    def __init__(self, status, headers=None, text=""):
        self.status_code, self.headers, self.text = status, headers or {}, text


def scripted_post(monkeypatch, outcomes):
    """Make requests.post return/raise the given outcomes in order; record calls; no real sleeping."""
    calls = []

    def fake_post(url, json, timeout):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(teams.requests, "post", fake_post)
    monkeypatch.setattr(teams.time, "sleep", lambda s: None)
    return calls


def test_parse_targets_with_and_without_names():
    assert teams.parse_targets("chat=https://a/x?sig=1, https://b/y?sig=2\noncall-private=https://c/z", "alert") == [
        ("chat", "https://a/x?sig=1"), ("alert-2", "https://b/y?sig=2"), ("oncall-private", "https://c/z")]
    assert teams.parse_targets("", "alert") == []


def test_posts_to_every_target_including_private_channels(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(202), FakeResponse(202)])
    results = teams.post_card([("chat", "https://a"), ("oncall-private", "https://b")], CARD)
    assert calls == ["https://a", "https://b"]
    assert [r["target"] for r in results] == ["chat", "oncall-private"] and all(r["ok"] for r in results)


def test_throttled_post_is_retried(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(429, {"Retry-After": "1"}), FakeResponse(202)])
    assert teams.post_card(("chat", "https://a"), CARD) == [{"target": "chat", "status": 202, "ok": True}]
    assert len(calls) == 2


def test_bad_request_is_not_retried(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(400, text="bad card")])
    assert teams.post_card(("chat", "https://a"), CARD) == [{"target": "chat", "status": 400, "ok": False}]
    assert len(calls) == 1


def test_post_card_never_raises_and_never_logs_the_url(monkeypatch, caplog):
    scripted_post(monkeypatch, [ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")] * 3)
    results = teams.post_card([("oncall-private", SECRET_URL)], CARD)
    assert results == [{"target": "oncall-private", "status": None, "ok": False, "error": "ConnectionError"}]
    assert "TOP-SECRET-SIGNATURE" not in caplog.text and "oncall-private" in caplog.text
