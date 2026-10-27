"""Provider resolution: which model answers, and is it honest about openness?

No network here - these tests only exercise the configuration decision, which
is the part that decides whether a deployment quietly falls back to a closed
model or to no model at all.
"""

import pytest

from llm_agent import llm_provider, providers
from scripts.config import settings


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    """Every test starts with no provider and no cached client."""
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                 "TOGETHER_API_KEY", "LLM_BASE_URL", "LLM_API_KEY",
                 "LLM_MODEL", "LLM_PROVIDER"):
        monkeypatch.setattr(settings, name, "" if name != "LLM_PROVIDER" else "auto", raising=False)
    monkeypatch.setattr(llm_provider, "_client", None)
    monkeypatch.setattr(llm_provider, "_fallback", None)
    monkeypatch.setattr(llm_provider, "_client_config", None)
    monkeypatch.setattr(llm_provider, "_fallback_config", None)
    monkeypatch.setattr(llm_provider, "_unavailable_reason", None)


def test_nothing_configured_still_reports_rules_engine():
    assert providers.resolve_config() is None
    assert llm_provider.is_configured() is False
    status = llm_provider.status()
    assert status["available"] is False and status["provider"] is None


def test_openrouter_is_chosen_and_flagged_open(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-test")
    config = providers.resolve_config()
    assert config.provider == "openrouter"
    assert config.open_weights is True
    # The provider's own default model, not a leftover OpenAI name.
    assert config.model.startswith("qwen/")


def test_open_weights_beat_openai_when_both_are_present(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-closed")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk-open")
    assert providers.resolve_config().provider == "groq"


def test_openai_is_supported_but_never_called_open(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-closed")
    config = providers.resolve_config()
    assert config.provider == "openai" and config.open_weights is False


def test_every_configured_provider_becomes_a_failover(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk")
    assert [c.provider for c in providers.resolve_candidates()] == ["openrouter", "groq"]

    monkeypatch.setattr(llm_provider, "_fallback", None)
    config = llm_provider._fallback_config
    assert config is None  # not built until needed


def test_custom_endpoint_wins_and_judges_openness_by_name(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(settings, "LLM_MODEL", "qwen2.5-14b-instruct")
    config = providers.resolve_config()
    assert config.provider == "custom" and config.open_weights is True

    monkeypatch.setattr(settings, "LLM_MODEL", "acme-2026")
    assert providers.resolve_config().open_weights is False


def test_explicit_provider_without_a_key_falls_back_to_auto(monkeypatch):
    """A typo'd or key-less provider selection must not take the phone line
    down while another provider is configured and working."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or")
    assert providers.resolve_config().provider == "openrouter"


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    config = providers.resolve_config()
    assert config.provider == "ollama" and config.configured is True


def test_status_exposes_fallbacks_and_openness(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-closed")
    status = llm_provider.status()
    assert status["provider"] == "openrouter" and status["open_weights"] is True
    assert status["fallbacks"] == ["openai"]
    assert status["available"] is True


def test_explicit_fallback_is_listed_in_status(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(settings, "LLM_MODEL", "qwen2.5-7b-instruct")
    monkeypatch.setattr(settings, "LLM_FALLBACK_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "llama-3.3-70b-versatile")
    status = llm_provider.status()
    assert status["fallbacks"] == ["fallback"]
    assert llm_provider._explicit_fallback().open_weights is True


async def test_model_failure_flips_status_to_unavailable(monkeypatch):
    """A rate-limited provider must not keep claiming it is serving calls."""
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(llm_provider, "_client", None)

    class Boom:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise TimeoutError("provider timed out")

    monkeypatch.setattr(llm_provider, "_get_client", lambda: Boom())
    assert await llm_provider._complete(messages=[{"role": "user", "content": "hi"}]) is None
    status = llm_provider.status()
    assert status["available"] is False and "TimeoutError" in status["detail"]