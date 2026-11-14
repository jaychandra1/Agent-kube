"""Groq / OpenAI-compatible provider: history conversion, request budget, HTTP client.
No request leaves the machine: requests.post is replaced in every client test."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import agent
import llm_openai
from conftest import SAMPLE_RCA
from llm_openai import OpenAICompatibleClient, fit_budget, from_openai_response, to_openai_messages

AGENT_DIR = Path(__file__).resolve().parents[1]


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
    assert tools[0].endswith(llm_openai.TRIMMED)
    assert tools[-1] == "4" * 2000 and tools[-2] == "3" * 2000  # the latest results stay complete
    assert len(json.dumps(out)) <= 6000


def test_latest_results_are_cut_only_when_still_too_big():
    msgs = [{"role": "system", "content": "S"},
            {"role": "tool", "tool_call_id": "a", "name": "t", "content": "a" * 9000},
            {"role": "tool", "tool_call_id": "b", "name": "t", "content": "b" * 9000}]
    out = fit_budget(msgs, max_chars=5000)
    assert all(m["content"].startswith(c * 1500) and m["content"].endswith(llm_openai.TRIMMED)
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
    resp = from_openai_response(choice({"tool_calls": [{"id": "c", "function": {"name": "list_pods",
                                                                               "arguments": "{oops"}}]}))
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
        monkeypatch.setattr(llm_openai.requests, "post", self.post)
        self.sleeps = []
        monkeypatch.setattr(llm_openai.time, "sleep", self.sleeps.append)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        status, body, hdrs = self.replies.pop(0)
        return NS(status_code=status, json=lambda: body, text=str(body), headers=hdrs)


OK = (200, choice({"content": "All pods are healthy."}), {})
TOOLS = [{"name": "list_pods", "description": "List pods", "input_schema": {"type": "object", "properties": {}}}]


def groq_client(**kw):
    return OpenAICompatibleClient(llm_openai.GROQ_BASE_URL, "gsk_test_key", "openai/gpt-oss-120b",
                                  reasoning_effort="medium", **kw)


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
    with pytest.raises(RuntimeError, match="retry after 3600s"):
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
    with pytest.raises(RuntimeError) as e:
        groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert "401" in str(e.value) and "gsk_test_key" not in str(e.value)


def test_gives_up_after_the_last_attempt(monkeypatch):
    http = FakeHTTP(monkeypatch, [(503, {}, {})] * 5)
    with pytest.raises(RuntimeError, match="503"):
        groq_client().create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert len(http.calls) == 5


# ─── Provider selection and agent wiring ─────────────────────────────────────

@pytest.mark.parametrize("setting,keys,expected", [
    ("auto", ("a", "g", "", ""), "anthropic"),
    ("auto", ("", "g", "", ""), "groq"),
    ("auto", ("", "", "k", "http://vllm:8000/v1"), "openai"),
    ("auto", ("", "", "", ""), ""),
    ("groq", ("a", "g", "", ""), "groq"),
    ("none", ("a", "g", "", ""), ""),
])
def test_provider_selection(setting, keys, expected):
    assert agent.select_provider(setting, *keys) == expected


def import_agent_with(env: dict) -> dict:
    """Import agent.py in a fresh interpreter with `env` and report its model settings."""
    clean = {k: v for k, v in os.environ.items()
             if k not in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER",
                          "AGENT_MODEL", "AGENT_EFFORT", "LLM_MAX_STEPS", "LLM_TOOL_OUTPUT_CHARS",
                          "LLM_MAX_REQUEST_CHARS", "LLM_MAX_OUTPUT_TOKENS")}
    code = ("import json, agent; c = agent.client; print(json.dumps({'provider': agent.PROVIDER, "
            "'model': agent.MODEL, 'steps': agent.RCA_STEPS, 'tool_chars': agent.TOOL_OUTPUT_CHARS, "
            "'client': type(c).__name__, 'base': getattr(c, 'base_url', None), "
            "'reasoning': getattr(c, 'reasoning_effort', None)}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=AGENT_DIR, env={**clean, **env},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_groq_key_alone_selects_groq_with_its_defaults():
    got = import_agent_with({"GROQ_API_KEY": "gsk_test"})
    assert got == {"provider": "groq", "model": "openai/gpt-oss-120b", "steps": 8, "tool_chars": 2500,
                   "client": "OpenAICompatibleClient", "base": "https://api.groq.com/openai/v1",
                   "reasoning": "medium"}


def test_configmap_values_override_groq_defaults():
    got = import_agent_with({"GROQ_API_KEY": "gsk_test", "AGENT_MODEL": "llama-3.3-70b-versatile",
                             "LLM_MAX_STEPS": "5", "AGENT_EFFORT": "high"})
    assert got["model"] == "llama-3.3-70b-versatile" and got["steps"] == 5
    assert got["reasoning"] is None  # reasoning_effort is only sent to gpt-oss models


def test_no_key_means_rules_only():
    got = import_agent_with({})
    assert got["provider"] == "" and got["client"] == "NoneType"


def test_non_anthropic_call_goes_through_the_compatible_client(monkeypatch):
    seen = {}

    class Fake:
        def create(self, system, messages, tools, require_tool=None):
            seen.update(system=system, require_tool=require_tool)
            return NS(stop_reason="end_turn", content=[])
    monkeypatch.setattr(agent, "PROVIDER", "groq")
    monkeypatch.setattr(agent, "client", Fake())
    agent._create([{"role": "user", "content": "q"}], TOOLS, require_tool="submit_rca")
    assert seen == {"system": agent.SYSTEM, "require_tool": "submit_rca"}


# ─── RCA from a model without a strict schema ────────────────────────────────

def test_normalize_fills_missing_and_fixes_values():
    rca = agent.normalize_rca({"title": "Web down", "root_cause": "bad config", "severity": "sev 2",
                               "confidence": "Very High", "resolution": "Roll back", "summary": "web is down"})
    assert rca["severity"] == "SEV2" and rca["confidence"] == "low"
    assert rca["resolution"] == ["Roll back"] and rca["prevention"] == [] and rca["timeline"] == []
    assert rca["stakeholder_summary"] == "web is down"


def test_normalize_keeps_a_complete_rca():
    assert agent.normalize_rca(dict(SAMPLE_RCA)) == SAMPLE_RCA


def test_normalize_rejects_an_rca_without_root_cause():
    with pytest.raises(ValueError, match="root_cause"):
        agent.normalize_rca({"title": "x"})


def test_full_groq_investigation(monkeypatch):
    """Model asks for a tool, gets the result, then submits the RCA: two HTTP calls."""
    http = FakeHTTP(monkeypatch, [
        (200, choice({"tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "list_pods", "arguments": '{"namespace": "default"}'}}]}, finish="tool_calls"), {}),
        (200, choice({"tool_calls": [{"id": "c2", "type": "function", "function": {
            "name": "submit_rca", "arguments": json.dumps(SAMPLE_RCA)}}]}, finish="tool_calls"), {}),
    ])
    monkeypatch.setattr(agent, "PROVIDER", "groq")
    monkeypatch.setattr(agent, "client", groq_client())
    monkeypatch.setattr(agent, "call_tool", lambda name, args, *a: f"<untrusted_data>pods of {args['namespace']}")
    assert agent._llm_rca("why is web down?") == SAMPLE_RCA
    second = http.calls[1]["json"]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "list_pods"
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "name": "list_pods",
                          "content": "<untrusted_data>pods of default"}
    assert "submit_rca" in [t["function"]["name"] for t in http.calls[0]["json"]["tools"]]


def test_submit_rca_is_required_after_a_nudge(monkeypatch):
    calls = []
    replies = [NS(stop_reason="end_turn", content=[NS(type="text", text="It's the config.")]),
               NS(stop_reason="tool_use", content=[NS(type="tool_use", id="r", name="submit_rca",
                                                      input=dict(SAMPLE_RCA))])]

    def fake_create(messages, tools, require_tool=None):
        calls.append(require_tool)
        return replies.pop(0)
    monkeypatch.setattr(agent, "_create", fake_create)
    assert agent._llm_rca("why?") == SAMPLE_RCA
    assert calls == [None, "submit_rca"]
