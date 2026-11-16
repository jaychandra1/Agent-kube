"""Claude provider: the request it sends to the Anthropic API (no request leaves the machine)."""
from types import SimpleNamespace as NS

from providers.catalog import LARGE
from providers.claude import FALLBACK_BETA, ClaudeProvider


def recording_client():
    calls = []

    def create(**kw):
        calls.append(kw)
        return NS(stop_reason="end_turn", content=[])
    return NS(messages=NS(create=create), beta=NS(messages=NS(create=create))), calls


TOOLS = [{"name": "list_pods", "description": "List pods", "input_schema": {"type": "object"}, "strict": True}]


def test_request_with_effort_and_refusal_fallback():
    claude = ClaudeProvider("anthropic", "Anthropic Claude", "claude-opus-5-5", LARGE, api_key="sk-ant", effort="high")
    claude._client, calls = recording_client()
    claude.create("SYS", [{"role": "user", "content": "q"}], TOOLS, require_tool="submit_rca")
    (sent,) = calls
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == LARGE.output_tokens
    assert sent["system"] == "SYS" and sent["tools"] == TOOLS
    assert sent["output_config"] == {"effort": "high"}
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert "tool_choice" not in sent  # current Claude models reject a forced tool choice


def test_fallback_and_effort_can_be_switched_off():
    claude = ClaudeProvider("anthropic", "Anthropic Claude", "claude-opus-5-5", LARGE, api_key="sk-ant",
                            effort="", fallbacks="off")
    claude._client, calls = recording_client()
    claude.create("SYS", [{"role": "user", "content": "q"}], TOOLS)
    assert "betas" not in calls[0] and "output_config" not in calls[0]
