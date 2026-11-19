"""Any OpenAI-compatible Chat Completions API: Groq, Google Gemini, OpenAI, Azure OpenAI
(Microsoft Foundry), OpenRouter, Mistral, Ollama or a custom endpoint (see catalog.py).

agent.py keeps its history in the Anthropic Messages shape (assistant turns with "text" and
"tool_use" blocks, tool results as "tool_result" blocks in a user turn). We convert that to
Chat Completions messages on every call and convert the reply back, so the investigation
loop doesn't care which provider it is talking to.

Free plans have small per-minute token limits. Groq's free plan is around 8K tokens per
minute and a single bigger request is simply refused. So:
  - each request is kept under a character budget, oldest tool results are shortened first
  - "too many requests" is retried after the retry-after delay
  - a request the server calls too large is resent with less history
"""
import json
import logging
import time
from types import SimpleNamespace as NS

import requests

from .base import ChatProvider, Limits, ProviderError, response

logger = logging.getLogger("kubesupport.llm")

TRIMMED = "... [older tool output trimmed to fit the model's request size limit]"


def _get(block, key, default=None):
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic style tool schemas -> Chat Completions function tools."""
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Our history -> Chat Completions messages (system, user, assistant + tool_calls, tool)."""
    out = [{"role": "system", "content": system}]
    tool_names = {}
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            text = "".join(_get(b, "text") or "" for b in blocks if _get(b, "type") == "text")
            calls = []
            for b in blocks:
                if _get(b, "type") == "tool_use":
                    tool_names[_get(b, "id")] = _get(b, "name")
                    calls.append({"id": _get(b, "id"), "type": "function",
                                  "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}})
            if not text and not calls:
                continue  # empty turn, servers reject an assistant message without content
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            texts = []
            for b in content:
                if _get(b, "type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                                "name": tool_names.get(_get(b, "tool_use_id"), ""), "content": str(_get(b, "content"))})
                elif _get(b, "type") == "text":
                    texts.append(_get(b, "text"))
            if texts:  # has to come after the tool results, those must directly follow the tool_calls
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def fit_budget(messages: list[dict], max_chars: int, keep_last: int = 2) -> list[dict]:
    """Shorten tool results until the messages fit max_chars. Older ones go first (down to a
    stub), the latest keep_last only if that wasn't enough (down to 1500 chars)."""
    size = len(json.dumps(messages))
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    older, latest = (tool_idx[:-keep_last], tool_idx[-keep_last:]) if keep_last else (tool_idx, [])
    for keep, indexes in ((200, older), (1500, latest)):
        for i in indexes:
            if size <= max_chars:
                return messages
            old = messages[i]["content"]
            if len(old) > keep + 100:
                messages[i] = {**messages[i], "content": old[:keep] + TRIMMED}
                size = len(json.dumps(messages))
    return messages


def from_openai_response(data: dict) -> NS:
    """Chat Completions reply -> response(...) with text / tool_use blocks."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    if msg.get("content"):
        blocks.append(NS(type="text", text=msg["content"]))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_unparsed_arguments": (fn.get("arguments") or "")[:500]}
        blocks.append(NS(type="tool_use", id=call.get("id"), name=fn.get("name"), input=args))
    if any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    else:
        stop = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(
            choice.get("finish_reason"), "end_turn")
    return response(stop, blocks)


class OpenAICompatibleProvider(ChatProvider):
    """Chat Completions with tools, retries and a request size budget.

    limits.request_chars covers the messages plus the tool definitions. Together with
    limits.output_tokens that keeps one request under a per-minute token limit."""

    def __init__(self, name: str, title: str, model: str, limits: Limits, base_url: str, api_key: str = "",
                 auth: str = "bearer", reasoning_effort: str = "", attempts: int = 5, max_wait: float = 60):
        super().__init__(name, title, model, limits)
        if not base_url:
            raise ProviderError(f"{title} needs LLM_BASE_URL")
        self.base_url = base_url.rstrip("/")
        self.api_key, self.auth = api_key, auth
        self.reasoning_effort = reasoning_effort
        self.attempts, self.max_wait = attempts, max_wait

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.auth == "api-key":  # Azure OpenAI
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, system, messages, tools, require_tool, max_chars):
        payload = {"model": self.model, "max_completion_tokens": self.limits.output_tokens}
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = ({"type": "function", "function": {"name": require_tool}}
                                      if require_tool else "auto")
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        room = max(max_chars - len(json.dumps(payload.get("tools", []))), 2000)
        payload["messages"] = fit_budget(to_openai_messages(system, messages), room)
        return payload

    def _compatible(self, payload: dict) -> dict:
        """The same request without the optional fields some servers reject."""
        p = dict(payload)
        if "tools" in p:
            p["tool_choice"] = "auto"
        p.pop("reasoning_effort", None)
        p["max_tokens"] = p.pop("max_completion_tokens", self.limits.output_tokens)
        return p

    def create(self, system, messages, tools, require_tool=None):
        budget = self.limits.request_chars
        payload = self._payload(system, messages, tools, require_tool, budget)
        tried_compatible = False
        last_error = "no attempt made"
        for attempt in range(1, self.attempts + 1):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(),
                                  timeout=self.limits.timeout)
            except requests.RequestException as e:
                last_error = type(e).__name__
                time.sleep(min(2 ** attempt, self.max_wait))
                continue
            if r.status_code == 200:
                return from_openai_response(r.json())
            last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code == 413 or "request too large" in r.text.lower():
                # over the per-minute token limit (Groq answers 413): retry with less history
                budget //= 2
                room = max(budget - len(json.dumps(payload.get("tools", []))), 2000)
                payload = {**payload, "messages": fit_budget(to_openai_messages(system, messages), room)}
                logger.info("Model API said the request is too large; retrying with less history")
                continue
            if r.status_code == 400 and not tried_compatible:
                tried_compatible = True  # forced tool_choice or reasoning_effort not supported, probably
                payload = self._compatible(payload)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float((r.headers or {}).get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                if wait > self.max_wait:  # daily token limit or similar, no point holding the caller for hours
                    last_error = f"HTTP {r.status_code}: rate limit, retry after {wait:.0f}s. {r.text[:200]}"
                    break
                logger.info("Model API returned %s; retrying in %.0fs (attempt %d/%d)",
                            r.status_code, wait, attempt, self.attempts)
                time.sleep(wait)
                continue
            break
        raise ProviderError(f"{self.title} API error ({self.base_url}): {last_error}")
