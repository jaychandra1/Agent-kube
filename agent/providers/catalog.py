"""The provider catalog, and the code that picks one.

LLM_PROVIDER (ConfigMap) names the provider and only that one is used. The key goes into
the Secret as LLM_API_KEY or under the provider's own name (GROQ_API_KEY etc).
All defaults below can be overridden in the ConfigMap:

  LLM_MODEL                model name (Azure OpenAI: the deployment name)
  LLM_BASE_URL             API endpoint (required for azure-openai, ollama and custom)
  LLM_EFFORT               reasoning effort: low | medium | high (Claude also xhigh | max); none = off
  LLM_MAX_STEPS            model calls per answer or RCA
  LLM_TOOL_OUTPUT_CHARS    characters of one tool result given to the model
  LLM_MAX_REQUEST_CHARS    size of one request (OpenAI-compatible providers)
  LLM_MAX_OUTPUT_TOKENS    longest reply
  LLM_TIMEOUT_SECONDS      one model call (Cursor: one whole run)
  LLM_FALLBACKS            Claude only: server-side refusal fallback ("default" or "off")

A new provider with an OpenAI-compatible API is one more Spec in CATALOG. Anything else
needs a class in this folder (see base.py) and a branch in _build().
"""
import os
from dataclasses import dataclass, field

from .base import Limits, Provider, ProviderError

# free plans only allow a few thousand tokens per minute, so small requests and short tool output
FREE_TIER = Limits(steps=8, tool_chars=2500, request_chars=14000, output_tokens=2500, timeout=120)
STANDARD = Limits(steps=12, tool_chars=8000, request_chars=60000, output_tokens=4000, timeout=120)
LARGE = Limits(steps=14, tool_chars=15000, request_chars=200000, output_tokens=16000, timeout=300)
# Cursor runs its own loop: steps = submit_rca reminders, timeout = one whole run
AGENT = Limits(steps=2, tool_chars=15000, request_chars=0, output_tokens=0, timeout=600)


@dataclass(frozen=True)
class Spec:
    name: str                # value of LLM_PROVIDER
    kind: str                # "claude" | "openai" (OpenAI-compatible) | "cursor"
    title: str
    key_env: str             # the provider's own key variable (LLM_API_KEY always works too)
    base_url: str = ""       # "" = set LLM_BASE_URL
    model: str = ""          # "" = set LLM_MODEL
    effort: str = ""         # default LLM_EFFORT
    key_required: bool = True
    auth: str = "bearer"     # how the key is sent: "bearer" or "api-key" (Azure)
    reasoning_models: tuple = ()  # model-name prefixes that take reasoning_effort by default
    limits: Limits = field(default=STANDARD)


CATALOG = {s.name: s for s in [
    Spec("groq", "openai", "Groq", "GROQ_API_KEY", base_url="https://api.groq.com/openai/v1",
         model="openai/gpt-oss-120b", effort="medium", reasoning_models=("openai/gpt-oss",), limits=FREE_TIER),
    Spec("anthropic", "claude", "Anthropic Claude", "ANTHROPIC_API_KEY", model="claude-opus-5-5", effort="high",
         limits=LARGE),
    Spec("cursor", "cursor", "Cursor", "CURSOR_API_KEY", model="composer-2.5", limits=AGENT),
    Spec("gemini", "openai", "Google Gemini", "GEMINI_API_KEY",
         base_url="https://generativelanguage.googleapis.com/v1beta/openai", model="gemini-flash-latest"),
    Spec("openai", "openai", "OpenAI", "OPENAI_API_KEY", base_url="https://api.openai.com/v1"),
    Spec("azure-openai", "openai", "Azure OpenAI (Microsoft Foundry)", "AZURE_OPENAI_API_KEY", auth="api-key"),
    Spec("openrouter", "openai", "OpenRouter", "OPENROUTER_API_KEY", base_url="https://openrouter.ai/api/v1",
         model="openai/gpt-oss-120b"),
    Spec("mistral", "openai", "Mistral AI", "MISTRAL_API_KEY", base_url="https://api.mistral.ai/v1",
         model="mistral-large-latest"),
    Spec("ollama", "openai", "Ollama (self-hosted)", "OLLAMA_API_KEY", key_required=False),
    Spec("custom", "openai", "OpenAI-compatible API", "LLM_API_KEY", key_required=False),
]}
ALIASES = {"claude": "anthropic", "azure": "azure-openai", "foundry": "azure-openai", "google": "gemini"}
OFF = ("", "none", "off", "rules")


def _setting(env, name: str) -> str:
    return (env.get(name) or "").strip()


def _number(env, name: str, cast):
    value = _setting(env, name)
    try:
        return cast(value) if value else None
    except ValueError:
        raise ProviderError(f"{name} must be a number, not '{value}'")


def resolve(env=None) -> dict:
    """Read the LLM_* settings into {"spec", "key", "model", "base_url", "effort", "limits",
    "fallbacks"}. Returns {"off": reason} or {"error": reason} if there's nothing to load."""
    env = os.environ if env is None else env
    raw = _setting(env, "LLM_PROVIDER").lower()
    name = ALIASES.get(raw, raw)
    if name in OFF:
        return {"off": "LLM_PROVIDER is not set: rule-based diagnosis only"}
    spec = CATALOG.get(name)
    if spec is None:
        return {"error": f"unknown LLM_PROVIDER '{raw}'; use one of: {', '.join(CATALOG)}, none"}
    key = _setting(env, spec.key_env) or _setting(env, "LLM_API_KEY")
    model = _setting(env, "LLM_MODEL") or spec.model
    base_url = _setting(env, "LLM_BASE_URL") or spec.base_url
    missing = [what for what, needed in (
        (f"LLM_API_KEY (or {spec.key_env}) in the Secret", spec.key_required and not key),
        ("LLM_MODEL", not model),
        ("LLM_BASE_URL", spec.kind == "openai" and not base_url),
    ) if needed]
    if missing:
        return {"error": f"{spec.title} needs {' and '.join(missing)}"}
    effort = _setting(env, "LLM_EFFORT").lower()
    if not effort and (spec.kind != "openai" or not spec.reasoning_models
                       or model.startswith(spec.reasoning_models)):
        effort = spec.effort
    limits = spec.limits.with_overrides(
        steps=_number(env, "LLM_MAX_STEPS", int), tool_chars=_number(env, "LLM_TOOL_OUTPUT_CHARS", int),
        request_chars=_number(env, "LLM_MAX_REQUEST_CHARS", int),
        output_tokens=_number(env, "LLM_MAX_OUTPUT_TOKENS", int), timeout=_number(env, "LLM_TIMEOUT_SECONDS", float))
    return {"spec": spec, "key": key, "model": model, "base_url": base_url,
            "effort": "" if effort in OFF else effort, "limits": limits,
            "fallbacks": _setting(env, "LLM_FALLBACKS") or "default"}


def _build(cfg: dict) -> Provider:
    spec = cfg["spec"]
    common = dict(name=spec.name, title=spec.title, model=cfg["model"], limits=cfg["limits"])
    if spec.kind == "claude":
        from .claude import ClaudeProvider
        return ClaudeProvider(**common, api_key=cfg["key"], effort=cfg["effort"], fallbacks=cfg["fallbacks"])
    if spec.kind == "cursor":
        from .cursor import CursorProvider
        return CursorProvider(**common, api_key=cfg["key"])
    from .openai_compat import OpenAICompatibleProvider
    effort = cfg["effort"] if cfg["effort"] in ("low", "medium", "high") else ""
    return OpenAICompatibleProvider(**common, base_url=cfg["base_url"], api_key=cfg["key"], auth=spec.auth,
                                    reasoning_effort=effort)


def load_provider(env=None) -> tuple[Provider | None, str]:
    """Returns (provider or None, a status line for the log and /alerts/status)."""
    try:
        cfg = resolve(env)
        if "spec" not in cfg:
            return None, cfg.get("off") or cfg["error"]
        provider = _build(cfg)
    except ProviderError as e:
        return None, str(e)
    except Exception as e:  # SDK constructors can throw, e.g. on a malformed key
        return None, f"{type(e).__name__}: {e}"
    return provider, f"{provider.title}, model {provider.model}"
