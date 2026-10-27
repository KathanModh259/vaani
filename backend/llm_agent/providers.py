"""Which model answers the phone, and whether its weights are open.

Every host below speaks the OpenAI-compatible chat API, so one client covers
all of them. The resolution order puts open weights first on purpose: a small
business should be able to self-host its receptionist, and the hackathon rule
is "open models", not "whatever API key happened to be in the environment".

Resolution order for ``LLM_PROVIDER=auto``:

    explicit LLM_BASE_URL -> OpenRouter -> Groq -> Together -> OpenAI

Ollama is never probed automatically - it only answers when selected with
``LLM_PROVIDER=ollama``, because a dead local port at import time makes cold
starts unpredictable.

``resolve_candidates()`` returns *every* configured provider in that order.
llm_provider uses the list as a failover chain: if OpenRouter rate-limits
mid-call, the next configured provider answers instead of the caller hearing
silence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from scripts.config import settings

logger = logging.getLogger(__name__)

# Families whose weights are published. Used for the honest "open weights"
# badge in /api/llm/status and the README - not for routing.
_OPEN_FAMILIES = re.compile(
    r"(qwen|llama|mistral|mixtral|deepseek|gemma|phi-|granite|olmo|glm|kimi|"
    r"gpt-oss|command-r|falcon|yi-|internlm|smollm|nemo)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    base_url: str
    default_model: str
    key_env: str
    open_weights: bool
    notes: str = ""


PRESETS: Dict[str, ProviderPreset] = {
    "openrouter": ProviderPreset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="qwen/qwen3-30b-a3b-instruct-2507",
        key_env="OPENROUTER_API_KEY",
        open_weights=True,
        notes="One key, most open-weight models. Append :free for the free tier.",
    ),
    "groq": ProviderPreset(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        key_env="GROQ_API_KEY",
        open_weights=True,
        notes="Highest token throughput; good for sub-second voice turns.",
    ),
    "together": ProviderPreset(
        name="together",
        base_url="https://api.together.xyz/v1",
        default_model="Qwen/Qwen2.5-72B-Instruct-Turbo",
        key_env="TOGETHER_API_KEY",
        open_weights=True,
    ),
    "ollama": ProviderPreset(
        name="ollama",
        base_url="http://127.0.0.1:11434/v1",
        default_model="qwen2.5:7b-instruct",
        key_env="OLLAMA_API_KEY",
        open_weights=True,
        notes="Fully local. No key, no data leaves the machine.",
    ),
    "openai": ProviderPreset(
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
        open_weights=False,
        notes="Closed weights. Supported, but not the default story.",
    ),
}


@dataclass
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    open_weights: bool

    @property
    def configured(self) -> bool:
        # Ollama and other local servers accept any key, including none.
        return bool(self.base_url and (self.api_key or self.provider in ("ollama", "custom")))


def _env(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def _model_override() -> str:
    return _env("LLM_MODEL")


def _custom(choice: str) -> Optional[LLMConfig]:
    """An explicit endpoint wins: vLLM, LM Studio, Azure, a proxy."""
    base_url = _env("LLM_BASE_URL")
    if not base_url:
        return None
    model = _model_override()
    return LLMConfig(
        provider=choice or "custom",
        base_url=base_url,
        api_key=_env("LLM_API_KEY") or "not-needed",
        model=model or "default",
        # Only claimed as open weights when the model name says so.
        open_weights=bool(_OPEN_FAMILIES.search(model or "")),
    )


def _preset_config(name: str) -> Optional[LLMConfig]:
    preset = PRESETS[name]
    key = _env(preset.key_env)
    if not key and name != "ollama":
        return None
    return LLMConfig(
        provider=preset.name,
        base_url=_env("LLM_BASE_URL") or preset.base_url,
        api_key=key or "ollama",
        model=_model_override() or preset.default_model,
        open_weights=preset.open_weights,
    )


def resolve_candidates() -> List[LLMConfig]:
    """Every configured provider, in the order Vaani should try them."""
    choice = _env("LLM_PROVIDER") or "auto"
    if choice == "none":
        return []

    if choice != "auto":
        if choice in PRESETS:
            config = _preset_config(choice)
            if config is not None:
                return [config]
        custom = _custom(choice)
        if custom is not None:
            return [custom]
        logger.warning("LLM_PROVIDER=%r is not configured; falling back to auto", choice)

    ordered: List[LLMConfig] = []
    custom = _custom("custom")
    if custom is not None:
        ordered.append(custom)
    for name in ("openrouter", "groq", "together", "openai"):
        config = _preset_config(name)
        if config is not None:
            ordered.append(config)
    return ordered


def resolve_config() -> Optional[LLMConfig]:
    """The provider that answers first, or None when nothing is configured."""
    candidates = resolve_candidates()
    return candidates[0] if candidates else None


def status() -> Dict[str, Any]:
    config = resolve_config()
    if config is None:
        return {
            "available": False,
            "provider": None,
            "model": None,
            "open_weights": None,
            "detail": "No LLM configured - Vaani is using its built-in rules engine",
        }
    return {
        "available": config.configured,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "open_weights": config.open_weights,
        "fallbacks": [c.provider for c in resolve_candidates()[1:]],
        "detail": None if config.configured else "Missing API key for this provider",
    }