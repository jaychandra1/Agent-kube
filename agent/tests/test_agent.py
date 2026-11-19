from types import SimpleNamespace as NS

import pytest

import agent
from conftest import SAMPLE_RCA, fake_llm


def resp(stop, blocks):
    return NS(stop_reason=stop, content=blocks)


def tool_use(name, inp, id="t1"):
    return NS(type="tool_use", name=name, input=inp, id=id)


def text(t):
    return NS(type="text", text=t)


def scripted(monkeypatch, responses):
    """Replace the API call with scripted responses; return the list of message histories sent."""
    sent = []
    monkeypatch.setattr(agent, "llm", fake_llm())

    def fake_create(messages, tools, **kw):
        sent.append([dict(m) for m in messages])
        return responses.pop(0)
    monkeypatch.setattr(agent, "_create", fake_create)
    return sent


def test_llm_rca_returns_submitted_rca(monkeypatch):
    scripted(monkeypatch, [resp("tool_use", [tool_use("submit_rca", SAMPLE_RCA)])])
    assert agent._llm_rca("why?") == SAMPLE_RCA


def test_llm_rca_runs_tools_then_submits(monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda name, args, *a: f"<untrusted_data>{name}</untrusted_data>")
    sent = scripted(monkeypatch, [
        resp("tool_use", [tool_use("list_pods", {"namespace": "default"}, id="a")]),
        resp("tool_use", [tool_use("submit_rca", SAMPLE_RCA, id="b")]),
    ])
    assert agent._llm_rca("why?") == SAMPLE_RCA
    tool_result = sent[1][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "a"


def test_llm_rca_nudges_when_model_stops_without_submitting(monkeypatch):
    sent = scripted(monkeypatch, [
        resp("end_turn", [text("I think it's the config.")]),
        resp("tool_use", [tool_use("submit_rca", SAMPLE_RCA)]),
    ])
    assert agent._llm_rca("why?") == SAMPLE_RCA
    assert sent[1][-1] == {"role": "user", "content": agent.RCA_NUDGE}


def test_refusal_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    scripted(monkeypatch, [resp("refusal", [])])
    monkeypatch.setattr(agent, "generate_heuristic_rca", lambda *a, **k: dict(SAMPLE_RCA))
    rca = agent.investigate_and_rca("why?")
    assert rca["generated_by"] == "heuristic" and "refusal" in rca["llm_error"]


def test_use_llm_false_skips_the_model(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "_create", lambda *a, **k: pytest.fail("the model must not be called"))
    seen = {}

    def heuristic(**kw):
        seen.update(kw)
        return dict(SAMPLE_RCA)
    monkeypatch.setattr(agent, "generate_heuristic_rca", heuristic)
    target = {"namespace": "default", "pod": "web-1", "workload": "Deployment/web", "reason": "CrashLoop"}
    rca = agent.investigate_and_rca("q", target=target, use_llm=False)
    assert rca["generated_by"] == "heuristic" and "llm_error" not in rca
    assert seen == {"namespace": "default", "pod": "web-1", "incident": target}


def test_incident_without_pod_points_the_model_at_the_workload(monkeypatch):
    prompts = []
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "_llm_rca", lambda prompt: prompts.append(prompt) or dict(SAMPLE_RCA))
    target = {"namespace": "", "pod": None, "workload": "Node/ip-10-0-1-1", "reason": "NodeNotReady",
              "detail": "Ready=Unknown NodeStatusUnknown - Kubelet stopped posting node status."}
    agent.investigate_and_rca("q", target=target)
    assert "Focus on Node/ip-10-0-1-1; detected problem: NodeNotReady" in prompts[0]
    assert "<untrusted_data>\nReady=Unknown" in prompts[0]


def test_rules_only_chat_lists_failing_pods(monkeypatch):
    monkeypatch.setattr(agent, "llm", None)
    monkeypatch.setattr(agent, "scan_cluster_for_issues", lambda: [
        {"namespace": "apps", "pod": "api-1", "phase": "Running", "restarts": 4, "waiting_reasons": ["CrashLoopBackOff"]}])
    answer = agent.chat([], "what is broken?")
    assert "No AI model is configured" in answer and "- apps/api-1: Running, 4 restarts, CrashLoopBackOff" in answer
    monkeypatch.setattr(agent, "scan_cluster_for_issues", lambda: [])
    assert "no failing pods" in agent.chat([], "anything?")


def test_system_rules_are_production_ready():
    rules = agent.SYSTEM
    for must in ("read-only", "<untrusted_data>", "never say you did", "[REDACTED]", "SEV1", "Confidence: high",
                 "roll back", agent.timefmt.TIMEZONE, "only answer questions about the cluster",
                 "the only cluster you can see", "never ask which cluster", "Answer with real data",
                 "Never answer with kubectl commands"):
        assert must in rules, must
    assert "Bitbucket" not in rules  # no instructions for tools kubesupport doesn't have
    tools = {t["name"] for t in agent.TOOL_SCHEMAS}
    for name in ("describe_pod", "get_events", "get_pod_logs", "get_deployment", "get_resource", "list_resources",
                 "search_runbooks"):
        assert name in rules and name in tools, name


def test_trim_history_keeps_valid_start_and_drops_thinking():
    msgs = [{"role": "user", "content": "q0"}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                                      {"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
        msgs.append({"role": "user", "content": f"q{i + 1}"})
    trimmed = agent.trim_history(msgs, keep=20)
    assert trimmed[0]["role"] == "user" and isinstance(trimmed[0]["content"], str)
    assert len(trimmed) <= 20
    assert not any(isinstance(m["content"], list) and any(b.get("type") == "thinking" for b in m["content"])
                   for m in trimmed)


# --- RCA checks for models without a strict schema ---

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


def test_submit_rca_is_required_after_a_nudge(monkeypatch):
    calls = []
    replies = [NS(stop_reason="end_turn", content=[NS(type="text", text="It's the config.")]),
               NS(stop_reason="tool_use", content=[NS(type="tool_use", id="r", name="submit_rca",
                                                      input=dict(SAMPLE_RCA))])]

    def fake_create(messages, tools, require_tool=None):
        calls.append(require_tool)
        return replies.pop(0)
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "_create", fake_create)
    assert agent._llm_rca("why?") == SAMPLE_RCA
    assert calls == [None, "submit_rca"]
