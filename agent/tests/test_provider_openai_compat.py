"""OpenAI-compatible providers (Groq, Gemini, OpenAI, Azure OpenAI, ...): history conversion,
request budget, HTTP client, and a full investigation through it.
No request leaves the machine: requests.post is replaced in every client test."""
import json
from types import SimpleNamespace as NS

import pytest

import agent
from conftest import SAMPLE_RCA
from providers import ProviderError
from providers import openai_compat as oc
from providers.catalog import FREE_TIER, STANDARD
from providers.openai_compat import OpenAICompatibleProvider, fit_budget, from_openai_response, to_openai_messages


# ─── History conversion ──────────────────────────────────────────────────────

def test_history_converts_to_chat_completions_messages():
    history = [
        {"role": "user", "content": "why is web down?"},
        # assistant content as agent.py stores it: the response blocks themselves
        {"role": "assistant", "content": [NS(type="text", text="Checking pods."),
                                          NS(type="tool_use", id="c1", name="list_pods", input={"namespace": "apps"})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "<untrusted_data>[]"},
                                     {"type": "text", "text": "Call submit_rca now."}]},
    ]
    out = to_openai_messages("SYS", history)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "why is web down?"}
    assert out[2]["role"] == "assistant" and out[2]["content"] == "Checking pods."
    call = out[2]["tool_calls"][0]
    assert call["id"] == "c1" and call["function"]["name"] == "list_pods"
    assert json.loads(call["function"]["arguments"]) == {"namespace": "apps"}
    # tool results directly after the assistant turn, the extra text after them
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "name": "list_pods", "content": "<untrusted_data>[]"}
    assert out[4] == {"role": "user", "content": "Call submit_rca now."}


def test_empty_assistant_turn_is_left_out():
    out = to_openai_messages("SYS", [{"role": "user", "content": "q"}, {"role": "assistant", "content": []},
                                     {"role": "user", "content": "nudge"}])
    assert [m["role"] for m in out] == ["system", "user", "user"]


def test_budget_trims_oldest_tool_results_first():
    msgs = [{"role": "system", "content": "S"}]
    for i in range(5):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": f"{i}" * 2000})
    out = fit_budget(msgs, max_chars=6000)
    tools = [m["content"] for m in out if m["role"] == "tool"]
    assert tools[0].endswith(oc.TRIMMED)
    assert tools[-1] == "4" * 2000 and tools[-2] == "3" * 2000  # the latest results stay complete
    assert len(json.dumps(out)) <= 6000


def test_latest_results_are_cut_only_when_still_too_big():
    msgs = [{"role": "system", "content": "S"},
            {"role": "tool", "tool_call_id": "a", "name": "t", "content": "a" * 9000},
            {"role": "tool", "tool_call_id": "b", "name": "t", "content": "b" * 9000}]
    out = fit_budget(msgs, max_chars=5000)
    assert all(m["content"].startswith(c * 1500) and m["content"].endswith(oc.TRIMMED)
               for m, c in zip(out[1:], "ab"))


def test_small_request_is_not_changed():
    msgs = [{"role": "system", "content": "S"}, {"role": "tool", "tool_call_id": "c", "name": "t", "content": "x"}]
    assert fit_budget(msgs, max_chars=10000) == msgs


# ─── Response parsing ────────────────────────────────────────────────────────

def choice(message, finish="stop"):
    return {"choices": [{"message": message, "finish_reason": finish}]}


def test_tool_call_response_becomes_tool_use_blocks():
    resp = from_openai_response(choice({"content": None, "tool_calls": [
        {"id": "c9", "type": "function", "function": {"name": "submit_rca", "arguments": json.dumps(SAMPLE_RCA)}},
    ]}, finish="tool_calls"))
    assert resp.stop_reason == "tool_use"
    assert resp.content[0].type == "tool_use" and resp.content[0].input == SAMPLE_RCA


def test_malformed_arguments_do_not_crash():
    call = {"id": "c", "function": {"name": "list_pods", "arguments": "{oops"}}
    resp = from_openai_response(choice({"tool_calls": [call]}))
    assert resp.content[0].input == {"_unparsed_arguments": "{oops"}


@pytest.mark.parametrize("finish,stop", [("stop", "end_turn"), ("length", "max_tokens"),
                                         ("content_filter", "refusal")])
def test_finish_reasons_map_to_stop_reasons(finish, stop):
    resp = from_openai_response(choice({"content": "hello"}, finish=finish))
    assert resp.stop_reason == stop and resp.content[0].text == "hello"


# ─── HTTP client ─────────────────────────────────────────────────────────────

class FakeHTTP:
    """Stands in for requests.post: returns the scripted replies and records each call."""

    def __init__(self, monkeypatch, replies):
        self.replies, self.calls = list(replies), []
        monkeypatch.setattr(oc.requests, "post", self.post)
        self.sleeps = []
        monkeypatch.setattr(oc.time, "sleep", self.sleeps.append)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        status, body, hdrs = self.replies.pop(0)
        return NS(status_code=status, json=lambda: body, text=str(body), headers=hdrs)


OK = (200, choice({"content": "All pods are healthy."}), {})
TOOLS = [{"name": "list_pods", "description": "List pods", "input_schema": {"type": "object", "properties": {}}}]


def groq_client():
    return OpenAICompatibleProvider("groq", "Groq", "openai/gpt-oss-120b", FREE_TIER,
                                    base_url="https://api.groq.com/openai/v1", api_key="gsk_test_key",
                                    reasoning_effort="medium")


def test_request_shape(monkeypatch):
    http = FakeHTTP(monkeypatch, [OK])
    resp = groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert resp.stop_reason == "end_turn" and resp.content[0].text == "All pods are healthy."
    call = http.calls[0]
    assert call["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer gsk_test_key"
    body = call["json"]
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "medium"
    assert body["tool_choice"] == "auto"
    assert body["tools"][0] == {"type": "function", "function": {
        "name": "list_pods", "description": "List pods", "parameters": {"type": "object", "properties": {}}}}


def test_required_tool_is_forced(monkeypatch):
    http = FakeHTTP(monkeypatch, [OK])
    groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS, require_tool="submit_rca")
    assert http.calls[0]["json"]["tool_choice"] == {"type": "function", "function": {"name": "submit_rca"}}


def test_rate_limit_waits_retry_after_then_succeeds(monkeypatch):
    http = FakeHTTP(monkeypatch, [(429, {"error": "rate limit"}, {"retry-after": "7"}), OK])
    groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert len(http.calls) == 2 and http.sleeps == [7.0]


def test_daily_limit_fails_fast_instead_of_waiting(monkeypatch):
    http = FakeHTTP(monkeypatch, [(429, {"error": "tokens per day"}, {"retry-after": "3600"}), OK])
    with pytest.raises(ProviderError, match="retry after 3600s"):
        groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert len(http.calls) == 1 and http.sleeps == []


def test_request_too_large_is_resent_smaller(monkeypatch):
    history = [{"role": "user", "content": "q"}]
    for i in range(4):
        history.append({"role": "assistant", "content": [NS(type="tool_use", id=f"c{i}", name="list_pods", input={})]})
        history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{i}",
                                                      "content": "x" * 2400}]})
    too_large = {"error": {"message": "Request too large for model on tokens per minute (TPM)"}}
    http = FakeHTTP(monkeypatch, [(413, too_large, {}), OK])
    groq_client().create("SYS", history, TOOLS)
    first, second = (len(json.dumps(c["json"])) for c in http.calls)
    assert second < first and http.sleeps == []


def test_bad_request_is_retried_once_without_optional_fields(monkeypatch):
    http = FakeHTTP(monkeypatch, [(400, {"error": "tool_choice not supported"}, {}), OK])
    groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS, require_tool="submit_rca")
    retry = http.calls[1]["json"]
    assert retry["tool_choice"] == "auto" and "reasoning_effort" not in retry
    assert retry["max_tokens"] == 2500 and "max_completion_tokens" not in retry


def test_errors_never_include_the_api_key(monkeypatch):
    FakeHTTP(monkeypatch, [(401, {"error": "invalid api key"}, {})])
    with pytest.raises(ProviderError) as e:
        groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert "401" in str(e.value) and "gsk_test_key" not in str(e.value)


def test_gives_up_after_the_last_attempt(monkeypatch):
    http = FakeHTTP(monkeypatch, [(503, {}, {})] * 5)
    with pytest.raises(ProviderError, match="503"):
        groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert len(http.calls) == 5


def test_azure_openai_sends_the_key_as_api_key_header(monkeypatch):
    http = FakeHTTP(monkeypatch, [OK])
    azure = OpenAICompatibleProvider("azure-openai", "Azure OpenAI", "my-deployment", STANDARD,
                                     base_url="https://res.openai.azure.com/openai/v1/", api_key="az-key", auth="api-key")
    azure.create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    call = http.calls[0]
    assert call["url"] == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert call["headers"]["api-key"] == "az-key" and "Authorization" not in call["headers"]
    assert call["json"]["model"] == "my-deployment" and "reasoning_effort" not in call["json"]


def test_no_key_sends_no_auth_header(monkeypatch):
    http = FakeHTTP(monkeypatch, [OK])
    OpenAICompatibleProvider("ollama", "Ollama", "llama3.3", STANDARD, base_url="http://ollama:11434/v1").create(
        "SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert "Authorization" not in http.calls[0]["headers"] and "api-key" not in http.calls[0]["headers"]


def test_endpoint_is_required():
    with pytest.raises(ProviderError, match="LLM_BASE_URL"):
        OpenAICompatibleProvider("custom", "Custom", "m", STANDARD, base_url="")


# ─── A whole investigation through an OpenAI-compatible provider ─────────────

def test_full_groq_investigation(monkeypatch):
    """Model asks for a tool, gets the result, then submits the RCA: two HTTP calls."""
    http = FakeHTTP(monkeypatch, [
        (200, choice({"tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "list_pods", "arguments": '{"namespace": "default"}'}}]}, finish="tool_calls"), {}),
        (200, choice({"tool_calls": [{"id": "c2", "type": "function", "function": {
            "name": "submit_rca", "arguments": json.dumps(SAMPLE_RCA)}}]}, finish="tool_calls"), {}),
    ])
    monkeypatch.setattr(agent, "llm", groq_client())
    monkeypatch.setattr(agent, "call_tool", lambda name, args, *a: f"<untrusted_data>pods of {args['namespace']}")
    assert agent._llm_rca("why is web down?") == SAMPLE_RCA
    second = http.calls[1]["json"]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "list_pods"
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "name": "list_pods",
                          "content": "<untrusted_data>pods of default"}
    assert "submit_rca" in [t["function"]["name"] for t in http.calls[0]["json"]["tools"]]
