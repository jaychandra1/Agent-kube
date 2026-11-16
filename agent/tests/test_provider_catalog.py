"""Choosing a provider with LLM_PROVIDER: defaults, required settings, keys, overrides."""
import sys

import pytest

from providers import CATALOG, ChatProvider, AgentProvider, load_provider, resolve
from providers.catalog import AGENT, FREE_TIER, LARGE, STANDARD
from test_provider_cursor import fake_sdk


@pytest.fixture(autouse=True)
def cursor_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake_sdk())


def env(provider, **values):
    return {"LLM_PROVIDER": provider, **values}


# Every provider with the least it needs: what kubesupport then uses.
READY = {
    "groq": (env("groq", GROQ_API_KEY="gsk"), "openai/gpt-oss-120b", "https://api.groq.com/openai/v1", FREE_TIER),
    "anthropic": (env("anthropic", ANTHROPIC_API_KEY="sk-ant"), "claude-opus-5-5", None, LARGE),
    "cursor": (env("cursor", CURSOR_API_KEY="cur"), "composer-2.5", None, AGENT),
    "gemini": (env("gemini", GEMINI_API_KEY="g"), "gemini-flash-latest",
               "https://generativelanguage.googleapis.com/v1beta/openai", STANDARD),
    "openai": (env("openai", OPENAI_API_KEY="sk", LLM_MODEL="my-model"), "my-model", "https://api.openai.com/v1",
               STANDARD),
    "azure-openai": (env("azure-openai", AZURE_OPENAI_API_KEY="az", LLM_MODEL="my-deployment",
                         LLM_BASE_URL="https://res.openai.azure.com/openai/v1"), "my-deployment",
                     "https://res.openai.azure.com/openai/v1", STANDARD),
    "openrouter": (env("openrouter", OPENROUTER_API_KEY="or"), "openai/gpt-oss-120b", "https://openrouter.ai/api/v1",
                   STANDARD),
    "mistral": (env("mistral", MISTRAL_API_KEY="m"), "mistral-large-latest", "https://api.mistral.ai/v1", STANDARD),
    "ollama": (env("ollama", LLM_MODEL="llama3.3", LLM_BASE_URL="http://ollama:11434/v1"), "llama3.3",
               "http://ollama:11434/v1", STANDARD),
    "custom": (env("custom", LLM_MODEL="m", LLM_BASE_URL="https://llm.example/v1"), "m", "https://llm.example/v1",
               STANDARD),
}


def test_every_catalog_provider_is_covered():
    assert set(READY) == set(CATALOG)


@pytest.mark.parametrize("name", sorted(READY))
def test_each_provider_loads_with_its_defaults(name):
    settings, model, base_url, limits = READY[name]
    provider, status = load_provider(settings)
    assert provider is not None, status
    assert provider.name == name and provider.model == model and provider.limits == limits
    assert status == f"{provider.title}, model {model}"
    expected_shape = AgentProvider if name == "cursor" else ChatProvider
    assert isinstance(provider, expected_shape)
    if base_url:
        assert provider.base_url == base_url


@pytest.mark.parametrize("value", ["", "none", "off", "rules"])
def test_no_provider_means_rules_only(value):
    provider, status = load_provider({"LLM_PROVIDER": value, "GROQ_API_KEY": "gsk"})
    assert provider is None and "rule-based" in status


def test_unknown_provider_lists_the_choices():
    provider, status = load_provider(env("copilot"))
    assert provider is None and "unknown LLM_PROVIDER 'copilot'" in status and "groq" in status


@pytest.mark.parametrize("settings,missing", [
    (env("groq"), "LLM_API_KEY (or GROQ_API_KEY)"),
    (env("openai", OPENAI_API_KEY="sk"), "LLM_MODEL"),
    (env("azure-openai", AZURE_OPENAI_API_KEY="az"), "LLM_MODEL and LLM_BASE_URL"),
    (env("custom", LLM_MODEL="m"), "LLM_BASE_URL"),
    (env("cursor"), "LLM_API_KEY (or CURSOR_API_KEY)"),
])
def test_missing_settings_are_named(settings, missing):
    provider, status = load_provider(settings)
    assert provider is None and f"needs {missing}" in status


def test_generic_key_works_and_provider_key_wins():
    assert load_provider(env("groq", LLM_API_KEY="generic"))[0].api_key == "generic"
    assert load_provider(env("groq", LLM_API_KEY="generic", GROQ_API_KEY="own"))[0].api_key == "own"


def test_aliases():
    assert load_provider(env("claude", LLM_API_KEY="k"))[0].name == "anthropic"
    assert load_provider(env("azure", LLM_API_KEY="k", LLM_MODEL="d", LLM_BASE_URL="https://x/openai/v1"))[0].name \
        == "azure-openai"


def test_configmap_overrides():
    provider, _ = load_provider(env("groq", GROQ_API_KEY="gsk", LLM_MODEL="llama-3.3-70b-versatile",
                                    LLM_MAX_STEPS="5", LLM_TOOL_OUTPUT_CHARS="4000", LLM_TIMEOUT_SECONDS="30"))
    assert provider.model == "llama-3.3-70b-versatile"
    assert (provider.limits.steps, provider.limits.tool_chars, provider.limits.timeout) == (5, 4000, 30.0)
    assert provider.limits.request_chars == FREE_TIER.request_chars  # not overridden


def test_bad_number_is_reported():
    provider, status = load_provider(env("groq", GROQ_API_KEY="gsk", LLM_MAX_STEPS="many"))
    assert provider is None and "LLM_MAX_STEPS must be a number" in status


def test_reasoning_effort_rules():
    def effort(**values):
        return load_provider(env("groq", GROQ_API_KEY="gsk", **values))[0].reasoning_effort
    assert effort() == "medium"                                            # gpt-oss: Groq's default
    assert effort(LLM_MODEL="llama-3.3-70b-versatile") == ""              # not a reasoning model
    assert effort(LLM_MODEL="llama-3.3-70b-versatile", LLM_EFFORT="high") == "high"  # asked for explicitly
    assert effort(LLM_EFFORT="none") == ""
    assert resolve(env("anthropic", ANTHROPIC_API_KEY="k"))["effort"] == "high"
    assert resolve(env("anthropic", ANTHROPIC_API_KEY="k", LLM_EFFORT="max"))["effort"] == "max"


def test_cursor_without_its_sdk_explains_why(monkeypatch):
    monkeypatch.setitem(sys.modules, "cursor_sdk", None)  # import fails, as in an image built without it
    provider, status = load_provider(env("cursor", CURSOR_API_KEY="cur"))
    assert provider is None and "WITH_CURSOR=false" in status
