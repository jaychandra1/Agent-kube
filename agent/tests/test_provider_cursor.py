"""Cursor provider, with a stand-in for the Cursor SDK: no Cursor process or API is used.

The fake agent runs a "script" for each message: it may call kubesupport's tools through
the custom tools it was given, exactly as Cursor's agent would, and returns a final text."""
import sys
import time
from types import SimpleNamespace as NS

import pytest

import agent
from conftest import SAMPLE_RCA
from providers import ProviderError
from providers.catalog import AGENT
from providers.cursor import WORKDIR, CursorProvider


def fake_sdk(script=None, status="finished", delay=0.0):
    """A module shaped like cursor_sdk. `script(agent, text)` plays the agent's part."""
    created = []

    class FakeRun:
        def __init__(self, owner, text):
            self.owner, self.text, self.cancelled = owner, text, False

        def wait(self):
            time.sleep(delay)
            result = script(self.owner, self.text) if script else "ok"
            return NS(status=status, result=result, error_message="boom" if status != "finished" else None)

        def cancel(self):
            self.cancelled = True

    class FakeAgent:
        def __init__(self, options):
            self.options, self.sent, self.closed, self.runs = options, [], False, []

        @classmethod
        def create(cls, options):
            created.append(cls(options))
            return created[-1]

        def send(self, text):
            self.sent.append(text)
            self.runs.append(FakeRun(self, text))
            return self.runs[-1]

        def call(self, tool, args):  # what Cursor's agent does when it uses a tool
            return self.options.local.custom_tools[tool].execute(args, NS(tool_call_id="t1"))

        def close(self):
            self.closed = True

    return NS(Agent=FakeAgent, AgentOptions=lambda **kw: NS(**kw), LocalAgentOptions=lambda **kw: NS(**kw),
              CustomTool=lambda **kw: NS(**kw), created=created)


TOOLS = [{"name": "list_pods", "description": "List pods", "input_schema": {"type": "object"}}]


def provider(monkeypatch, sdk, timeout=None):
    monkeypatch.setitem(sys.modules, "cursor_sdk", sdk)
    limits = AGENT.with_overrides(timeout=timeout)
    return CursorProvider("cursor", "Cursor", "composer-2.5", limits, api_key="cur-key")


def test_cursor_agent_gets_only_kubesupport_tools(monkeypatch):
    sdk = fake_sdk()
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda name, args: "x") as session:
        session.send("hello")
    (a,) = sdk.created
    o = a.options
    # Cursor offers custom tools through its "mcp" group: that group only, and never the shell.
    # (tools=[] would give the model no tools at all, so it could only answer in text.)
    assert o.tools == ["mcp"] and o.disallowed_tools == ["shell"]
    assert not hasattr(o, "mcp_servers")  # no other MCP server is configured
    assert o.model == "composer-2.5" and o.api_key == "cur-key"
    assert o.local.cwd == WORKDIR and o.local.setting_sources == []
    assert set(o.local.custom_tools) == {"list_pods"}
    assert o.local.custom_tools["list_pods"].input_schema == {"type": "object"}
    assert a.closed


def test_system_prompt_goes_with_the_first_message_only(monkeypatch):
    sdk = fake_sdk()
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda name, args: "x") as session:
        session.send("first")
        session.send("second")
    first, second = sdk.created[0].sent
    assert first.startswith("SYS") and first.endswith("first") and second == "second"
    assert "MCP tools" in first and "no shell and no kubectl" in first  # tells the agent where its tools are


def test_tool_calls_reach_kubesupport_tools(monkeypatch):
    calls = []
    sdk = fake_sdk(script=lambda a, text: a.call("list_pods", {"namespace": "apps"}))
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda n, args: calls.append((n, args)) or "pods") as s:
        assert s.send("which pods?") == "pods"
        assert s.tool_calls == 1
    assert calls == [("list_pods", {"namespace": "apps"})]


def test_failed_run_is_reported(monkeypatch):
    sdk = fake_sdk(status="error")
    with pytest.raises(ProviderError, match="status 'error' boom"):
        with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda n, a: "x") as session:
            session.send("q")


def test_slow_run_is_cancelled(monkeypatch):
    sdk = fake_sdk(delay=1.0)
    with pytest.raises(ProviderError, match="longer than"):
        with provider(monkeypatch, sdk, timeout=0.1).session("SYS", TOOLS, lambda n, a: "x") as session:
            session.send("q")
    assert sdk.created[0].runs[0].cancelled and sdk.created[0].closed


# ─── Through agent.py: RCA and chat with an agent provider ───────────────────

def rca_script(remind_first=False):
    """Investigate with list_pods, then submit the RCA (optionally only after a reminder)."""
    def script(a, text):
        a.call("list_pods", {"namespace": "default"})
        if remind_first and len(a.sent) == 1:
            return "I think it is the config."
        a.call("submit_rca", dict(SAMPLE_RCA))
        return "Submitted."
    return script


def use_cursor(monkeypatch, sdk):
    monkeypatch.setattr(agent, "llm", provider(monkeypatch, sdk))
    monkeypatch.setattr(agent, "call_tool", lambda name, args, *a: f"<untrusted_data>{name}")


def test_rca_through_cursor(monkeypatch):
    sdk = fake_sdk(script=rca_script())
    use_cursor(monkeypatch, sdk)
    rca = agent.investigate_and_rca("why is web down?")
    assert rca["generated_by"] == "llm:cursor:composer-2.5"
    assert {k: rca[k] for k in SAMPLE_RCA} == SAMPLE_RCA
    a = sdk.created[0]
    assert len(a.sent) == 1 and agent.RCA_INSTRUCTIONS in a.sent[0]
    assert set(a.options.local.custom_tools) >= {"list_pods", "submit_rca", "get_pod_logs"}


def test_cursor_is_reminded_to_submit(monkeypatch):
    sdk = fake_sdk(script=rca_script(remind_first=True))
    use_cursor(monkeypatch, sdk)
    assert agent._agent_rca("why?") == SAMPLE_RCA
    assert sdk.created[0].sent[1] == agent.RCA_NUDGE


def test_no_submission_falls_back_to_rules(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: "no idea")
    use_cursor(monkeypatch, sdk)
    monkeypatch.setattr(agent, "generate_heuristic_rca", lambda *a, **k: dict(SAMPLE_RCA))
    rca = agent.investigate_and_rca("why?")
    assert rca["generated_by"] == "heuristic" and "without calling submit_rca" in rca["llm_error"]
    assert len(sdk.created[0].sent) == 1 + agent.llm.limits.steps  # first message + reminders


def test_rca_without_reading_the_cluster_is_rejected(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: a.call("submit_rca", dict(SAMPLE_RCA)) and "Submitted.")
    use_cursor(monkeypatch, sdk)
    monkeypatch.setattr(agent, "generate_heuristic_rca", lambda **k: dict(SAMPLE_RCA))
    rca = agent.investigate_and_rca("why?")
    assert rca["generated_by"] == "heuristic" and "without reading the cluster" in rca["llm_error"]


def test_instructions_instead_of_data_get_one_push_to_use_the_tools(monkeypatch):
    def script(a, text):
        if len(a.sent) == 1:
            return "I could not run any cluster tools. Run: kubectl get ingress -A"
        return "Ingresses: " + a.call("list_resources", {"kind": "ingress"})
    sdk = fake_sdk(script=script)
    use_cursor(monkeypatch, sdk)
    answer = agent.chat([], "give me the ingress endpoints")
    assert answer == "Ingresses: <untrusted_data>list_resources"
    assert sdk.created[0].sent[1] == agent.TOOLS_NUDGE


def test_plain_answers_are_not_pushed(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: "Hello! Ask me about the cluster.")
    use_cursor(monkeypatch, sdk)
    assert agent.chat([], "hi") == "Hello! Ask me about the cluster."
    assert len(sdk.created[0].sent) == 1


def test_chat_through_cursor_keeps_the_conversation(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: f"answer {len(a.sent)}")
    use_cursor(monkeypatch, sdk)
    history = []
    assert agent.chat(history, "is web healthy?") == "answer 1"
    assert agent.chat(history, "and payments?") == "answer 1"  # each question is a new Cursor agent
    second_prompt = sdk.created[1].sent[0]
    assert "Conversation so far:" in second_prompt and "User: is web healthy?" in second_prompt
    assert "kubesupport: answer 1" in second_prompt and second_prompt.endswith("New question: and payments?")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert set(sdk.created[0].options.local.custom_tools) == {t["name"] for t in agent.TOOL_SCHEMAS}
