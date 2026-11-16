"""The two shapes of AI provider kubesupport understands.

ChatProvider   kubesupport runs the investigation loop and asks the provider for one model
               reply at a time (Claude, and every OpenAI-compatible API: Groq, Gemini, ...).
AgentProvider  the provider runs its own agent loop and calls kubesupport's read-only tools
               back (Cursor).

Both receive the same tool definitions and the same system prompt, and both end an
investigation with the same `submit_rca` tool, so agent.py treats them alike.
"""
from dataclasses import dataclass, replace
from types import SimpleNamespace


class ProviderError(RuntimeError):
    """A provider is misconfigured or its API answered with an error."""


@dataclass(frozen=True)
class Limits:
    steps: int           # model calls per answer or RCA (agent providers: reminders to submit the RCA)
    tool_chars: int      # characters of one tool result given to the model
    request_chars: int   # size of one request: messages + tool definitions (OpenAI-compatible only)
    output_tokens: int   # longest reply the model may write
    timeout: float       # seconds for one model call (agent providers: one whole run)

    def with_overrides(self, **values) -> "Limits":
        return replace(self, **{k: v for k, v in values.items() if v})


def response(stop_reason: str, content: list) -> SimpleNamespace:
    """One model reply in kubesupport's shape: .stop_reason ("tool_use", "end_turn",
    "max_tokens", "refusal") and .content (blocks with .type "text" or "tool_use")."""
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class Provider:
    kind = ""

    def __init__(self, name: str, title: str, model: str, limits: Limits):
        self.name, self.title, self.model, self.limits = name, title, model, limits

    def describe(self) -> dict:
        return {"provider": self.name, "title": self.title, "model": self.model, "kind": self.kind}


class ChatProvider(Provider):
    kind = "chat"

    def create(self, system: str, messages: list, tools: list, require_tool: str | None = None):
        """One model call. `messages` uses the Anthropic Messages shape (kubesupport's
        canonical history); `require_tool` asks the model to call that tool now, where the
        API allows it. Returns response(...)."""
        raise NotImplementedError


class AgentProvider(Provider):
    kind = "agent"

    def session(self, system: str, tools: list, execute):
        """Context manager for one conversation with the provider's agent. It has
        .send(text) -> final answer text. The agent calls `execute(tool_name, args) -> str`
        for every tool it uses; it has no other tools."""
        raise NotImplementedError
