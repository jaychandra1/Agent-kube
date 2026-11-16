"""AI model providers: one folder, one switch.

LLM_PROVIDER in the ConfigMap picks the provider; only that one is used.

  catalog.py        every supported provider: endpoint, key variable, default model, limits
  base.py           the two provider shapes (ChatProvider, AgentProvider) and Limits
  claude.py         Anthropic Claude (Messages API)
  openai_compat.py  any OpenAI-compatible Chat Completions API: Groq, Gemini, OpenAI,
                    Azure OpenAI, OpenRouter, Mistral, Ollama, custom endpoints
  cursor.py         Cursor, through the Cursor SDK (Cursor runs the tool loop itself)
"""
from .base import AgentProvider, ChatProvider, Limits, Provider, ProviderError, response
from .catalog import ALIASES, CATALOG, load_provider, resolve

__all__ = ["AgentProvider", "ChatProvider", "Limits", "Provider", "ProviderError", "response",
           "ALIASES", "CATALOG", "load_provider", "resolve"]
