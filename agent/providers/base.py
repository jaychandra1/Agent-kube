"""Provider base classes. There are two shapes:

ChatProvider   we run the investigation loop and ask the provider for one reply at a time
               (Claude and all the OpenAI-compatible APIs)
AgentProvider  the provider runs its own agent loop and calls our read-only tools back
               (Cursor)

Both get the same tool definitions and system prompt and both finish an investigation
through submit_rca, so agent.py can treat them more or less the same.
"""
from dataclasses import dataclass, replace
from types import SimpleNamespace


class ProviderError(RuntimeError):
    """Bad provider config, or the provider's API returned an error."""


@dataclass(frozen=True)
class Limits:
    steps: int           # model calls per answer or RCA (agent providers: submit_rca reminders)
    tool_chars: int      # max chars of one tool result
    request_chars: int   # max request size, messages + tool definitions (OpenAI-compatible only)
    output_tokens: int   # max reply length
    timeout: float       # seconds per model call (agent providers: per run)

    def with_overrides(self, **values) -> "Limits":
        return replace(self, **{k: v for k, v in values.items() if v})


def response(stop_reason: str, content: list) -> SimpleNamespace:
    """A model reply in our own shape: .stop_reason is "tool_use", "end_turn", "max_tokens"
    or "refusal", .content is a list of blocks with .type "text" or "tool_use"."""
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
        """One model call. messages are in the Anthropic Messages shape (that's how agent.py
        keeps the history). require_tool makes the model call that tool now, where the API
        supports it. Returns response(...)."""
        raise NotImplementedError


class AgentProvider(Provider):
    kind = "agent"

    def session(self, system: str, tools: list, execute):
        """Context manager for one conversation with the provider's agent, with
        .send(text) -> answer text. The agent calls execute(tool_name, args) -> str for every
        tool it uses and has no other tools."""
        raise NotImplementedError
