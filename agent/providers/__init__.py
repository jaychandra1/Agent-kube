"""AI providers. LLM_PROVIDER picks one, the rest is never loaded.

  catalog.py        all supported providers: endpoint, key variable, default model, limits
  base.py           ChatProvider / AgentProvider and Limits
  claude.py         Anthropic Claude (Messages API)
  openai_compat.py  anything with an OpenAI-compatible Chat Completions API: Groq, Gemini,
                    OpenAI, Azure OpenAI, OpenRouter, Mistral, Ollama, custom endpoints
  cursor.py         Cursor, through the Cursor SDK (it runs the tool loop itself)
"""
from .base import AgentProvider, ChatProvider, Limits, Provider, ProviderError, response
from .catalog import ALIASES, CATALOG, load_provider, resolve

__all__ = ["AgentProvider", "ChatProvider", "Limits", "Provider", "ProviderError", "response",
           "ALIASES", "CATALOG", "load_provider", "resolve"]
