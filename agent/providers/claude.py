"""Claude, through the Anthropic Messages API (LLM_PROVIDER=anthropic).

Thinking is left at the model's default (current models think adaptively). Callers send back
the full response content, thinking blocks included, and only append to the history;
agent.trim_history is the one place that cuts it. Current Claude models reject a forced
tool choice, so `require_tool` is not sent; agent.py reminds the model in words instead.
"""
from .base import ChatProvider, Limits, ProviderError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider(ChatProvider):
    def __init__(self, name: str, title: str, model: str, limits: Limits, api_key: str,
                 effort: str = "", fallbacks: str = "default"):
        super().__init__(name, title, model, limits)
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - the package is in requirements.txt
            raise ProviderError("the anthropic package is not installed") from e
        self._client = anthropic.Anthropic(api_key=api_key, timeout=limits.timeout)
        self.effort = effort
        # Server-side refusal fallback: "default" re-runs a declined request on Anthropic's
        # recommended fallback model inside the same API call; "off" disables it.
        self.fallbacks = "" if fallbacks.lower() in ("", "off", "none", "false", "0") else fallbacks

    def create(self, system, messages, tools, require_tool=None):
        params = {"model": self.model, "max_tokens": self.limits.output_tokens, "system": system,
                  "tools": tools, "messages": messages}
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            return self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **params)
        return self._client.messages.create(**params)
