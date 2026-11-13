from types import SimpleNamespace as NS

import pytest

import agent
from conftest import SAMPLE_RCA


def resp(stop, blocks):
    return NS(stop_reason=stop, content=blocks)


def tool_use(name, inp, id="t1"):
    return NS(type="tool_use", name=name, input=inp, id=id)


def text(t):
    return NS(type="text", text=t)


def scripted(monkeypatch, responses):
    """Replace the API call with scripted responses; return the list of message histories sent."""
    sent = []

    def fake_create(messages, tools):
        sent.append([dict(m) for m in messages])
        return responses.pop(0)
    monkeypatch.setattr(agent, "_create", fake_create)
    return sent


def test_llm_rca_returns_submitted_rca(monkeypatch):
    scripted(monkeypatch, [resp("tool_use", [tool_use("submit_rca", SAMPLE_RCA)])])
    assert agent._llm_rca("why?") == SAMPLE_RCA


def test_llm_rca_runs_tools_then_submits(monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda name, args: f"<untrusted_data>{name}</untrusted_data>")
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
    monkeypatch.setattr(agent, "client", object())
    scripted(monkeypatch, [resp("refusal", [])])
    monkeypatch.setattr(agent, "generate_heuristic_rca", lambda *a, **k: dict(SAMPLE_RCA))
    rca = agent.investigate_and_rca("why?")
    assert rca["generated_by"] == "heuristic" and "refusal" in rca["llm_error"]


def test_use_llm_false_skips_claude(monkeypatch):
    monkeypatch.setattr(agent, "client", object())
    monkeypatch.setattr(agent, "_create", lambda *a: pytest.fail("Claude must not be called"))
    seen = {}

    def heuristic(**kw):
        seen.update(kw)
        return dict(SAMPLE_RCA)
    monkeypatch.setattr(agent, "generate_heuristic_rca", heuristic)
    target = {"namespace": "default", "pod": "web-1", "workload": "Deployment/web", "reason": "CrashLoop"}
    rca = agent.investigate_and_rca("q", target=target, use_llm=False)
    assert rca["generated_by"] == "heuristic" and "llm_error" not in rca
    assert seen == {"namespace": "default", "pod": "web-1", "service": "web"}


def test_heuristic_diagnoses_image_pull_for_the_target_pod(monkeypatch):
    monkeypatch.setattr(agent, "scan_cluster_for_issues", lambda: pytest.fail("must not scan with a target"))
    monkeypatch.setattr(agent, "describe_pod", lambda ns, pod: {
        "phase": "Pending", "node": None, "conditions": [],
        "containers": [{"name": "app", "state": {"waiting": "ImagePullBackOff", "message": "not found"},
                        "last_terminated": None}]})
    monkeypatch.setattr(agent, "get_events", lambda ns, limit=20: [])
    monkeypatch.setattr(agent, "get_pod_logs", lambda *a, **k: "")
    rca = agent.generate_heuristic_rca(namespace="default", pod="web-5d8f7-a", service="web")
    assert rca["title"] == "Image Pull Failure in web" and rca["severity"] == "SEV2"


def test_heuristic_reports_healthy_when_nothing_is_wrong(monkeypatch):
    monkeypatch.setattr(agent, "scan_cluster_for_issues", lambda: [])
    assert "Healthy" in agent.generate_heuristic_rca()["title"]


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
