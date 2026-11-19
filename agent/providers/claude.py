"""Claude through the Anthropic Messages API (LLM_PROVIDER=anthropic).

Thinking stays on the model's default. The full response content goes back into the history
as it is, thinking blocks included, and agent.trim_history is the only place that cuts it.
Current Claude models reject a forced tool choice, so require_tool is not sent and agent.py
reminds the model in words instead.
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
        # server-side refusal fallback: with "default" Anthropic re-runs a declined request on
        # their fallback model within the same call. "off" disables it
        self.fallbacks = "" if fallbacks.lower() in ("", "off", "none", "false", "0") else fallbacks

    def create(self, system, messages, tools, require_tool=None):
        params = {"model": self.model, "max_tokens": self.limits.output_tokens, "system": system,
                  "tools": tools, "messages": messages}
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            return self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **params)
        return self._client.messages.create(**params)
