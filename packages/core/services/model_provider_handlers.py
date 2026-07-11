"""Official model provider registry and routing helpers.

The catalog stores Manor model ids in OpenRouter-style ``provider/model``
form. This module is the provider boundary: callers can resolve the catalog
provider, the official API base URL, key-prefix detection, and env fallbacks
without knowing individual vendor details.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelProviderHandler:
    provider: str
    display_name: str
    base_url: str
    env_vars: tuple[str, ...]
    key_prefixes: tuple[str, ...] = ()
    api_shape: str = "openai_compatible"
    roles: tuple[str, ...] = ("primary", "worker")
    generic_sk: bool = False

    @property
    def is_openrouter(self) -> bool:
        return self.provider == "openrouter"


PROVIDER_HANDLERS: dict[str, ModelProviderHandler] = {
    "openai": ModelProviderHandler(
        provider="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        env_vars=("OPENAI_API_KEY",),
        key_prefixes=("sk-",),
        roles=("primary", "worker", "image", "audio", "sfx", "stt", "embedding"),
    ),
    "anthropic": ModelProviderHandler(
        provider="anthropic",
        display_name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        env_vars=("ANTHROPIC_API_KEY",),
        key_prefixes=("sk-ant-",),
        api_shape="anthropic_messages",
        roles=("primary", "worker"),
    ),
    "google": ModelProviderHandler(
        provider="google",
        display_name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        key_prefixes=("AIza",),
        roles=("primary", "worker", "image", "voice", "audio", "embedding"),
    ),
    "deepseek": ModelProviderHandler(
        provider="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        env_vars=("DEEPSEEK_API_KEY",),
        key_prefixes=("sk-",),
        roles=("primary", "worker"),
        generic_sk=True,
    ),
    "qwen": ModelProviderHandler(
        provider="qwen",
        display_name="Qwen / DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_vars=("DASHSCOPE_API_KEY", "QWEN_API_KEY", "ALIBABA_API_KEY"),
        key_prefixes=("sk-",),
        roles=("primary", "worker"),
        generic_sk=True,
    ),
    "moonshotai": ModelProviderHandler(
        provider="moonshotai",
        display_name="Moonshot / Kimi",
        base_url="https://api.moonshot.ai/v1",
        env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        key_prefixes=("sk-",),
        roles=("primary", "worker"),
        generic_sk=True,
    ),
    "groq": ModelProviderHandler(
        provider="groq",
        display_name="Groq",
        base_url="https://api.groq.com/openai/v1",
        env_vars=("GROQ_API_KEY",),
        key_prefixes=("gsk_",),
        roles=("primary", "worker", "stt"),
    ),
    "mistral": ModelProviderHandler(
        provider="mistral",
        display_name="Mistral",
        base_url="https://api.mistral.ai/v1",
        env_vars=("MISTRAL_API_KEY",),
        roles=("primary", "worker"),
    ),
    "bytedance": ModelProviderHandler(
        provider="bytedance",
        display_name="Volcengine / Seedance",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        env_vars=(
            "VOLCENGINE_LAS_API_KEY",
            "VOLCENGINE_API_KEY",
            "SEEDANCE_API_KEY",
            "BYTEDANCE_API_KEY",
        ),
        api_shape="volcengine_video",
        roles=("video",),
    ),
    "kwaivgi": ModelProviderHandler(
        provider="kwaivgi",
        display_name="Kling AI",
        base_url="https://api-singapore.klingai.com",
        env_vars=("KLING_API_KEY", "KLINGAI_API_KEY"),
        api_shape="kling_video",
        roles=("video",),
    ),
    "zyphra": ModelProviderHandler(
        provider="zyphra",
        display_name="Zyphra",
        base_url="https://api.zyphra.com/v1",
        env_vars=("ZYPHRA_API_KEY",),
        api_shape="zyphra_tts",
        roles=("voice",),
    ),
    "openrouter": ModelProviderHandler(
        provider="openrouter",
        display_name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_vars=(
        ),
        key_prefixes=("sk-or-",),
        api_shape="openrouter",
        roles=("primary", "worker", "image", "voice", "audio", "sfx", "stt", "video"),
    ),
}

GENERIC_SK_PROVIDERS = {
    provider for provider, handler in PROVIDER_HANDLERS.items() if handler.generic_sk
}


def provider_for_model_id(model_id: str | None) -> str | None:
    """Return the catalog provider implied by ``provider/model`` ids."""

    model = str(model_id or "").strip().lower()
    if not model or "/" not in model:
        return None
    provider = model.split("/", 1)[0].strip()
    return provider if provider in PROVIDER_HANDLERS else None


def handler_for_provider(provider: str | None) -> ModelProviderHandler | None:
    return PROVIDER_HANDLERS.get(str(provider or "").strip().lower())


def handler_for_model(model_id: str | None) -> ModelProviderHandler | None:
    return handler_for_provider(provider_for_model_id(model_id))


def detect_provider_from_key(api_key: str) -> str | None:
    """Best-effort provider detection from API key prefix."""

    key = str(api_key or "").strip()
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("AIza"):
        return "google"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-"):
        return "openai"
    return None


def official_env_key(provider: str | None) -> tuple[str, str]:
    """Return ``(key, env_name)`` for the first configured provider env var."""

    handler = handler_for_provider(provider)
    if not handler:
        return "", ""
    for env_name in handler.env_vars:
        value = str(os.getenv(env_name) or "").strip()
        if value:
            return value, env_name
    return "", ""


def provider_from_base_url(base_url: str) -> str | None:
    """Return provider implied by a known official base URL."""

    lower = str(base_url or "").lower().rstrip("/")
    if not lower:
        return None
    for provider, handler in PROVIDER_HANDLERS.items():
        provider_base = handler.base_url.lower().rstrip("/")
        if provider_base and (lower == provider_base or lower.startswith(provider_base + "/")):
            return provider
    host = urlsplit(lower).hostname or ""
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("anthropic.com"):
        return "anthropic"
    if host.endswith("openai.com"):
        return "openai"
    if host.endswith("googleapis.com"):
        return "google"
    if host.endswith("deepseek.com"):
        return "deepseek"
    if host.endswith("dashscope.aliyuncs.com"):
        return "qwen"
    if host.endswith("moonshot.ai"):
        return "moonshotai"
    if host.endswith("groq.com"):
        return "groq"
    if host.endswith("mistral.ai"):
        return "mistral"
    if host.endswith("volces.com"):
        return "bytedance"
    if host.endswith("klingai.com") or host.endswith("klingapi.com"):
        return "kwaivgi"
    if host.endswith("zyphra.com"):
        return "zyphra"
    return None


def resolve_provider_base_url(model_id: str, api_key: str, user_base_url: str | None = None) -> str:
    """Resolve a credential's base URL for native BYOK or official routing."""

    if user_base_url and user_base_url.strip():
        return user_base_url.strip().rstrip("/")

    model_provider = provider_for_model_id(model_id)
    key_provider = detect_provider_from_key(api_key)

    if (
        key_provider == "openai"
        and model_provider in GENERIC_SK_PROVIDERS
        and (handler := handler_for_provider(model_provider))
    ):
        return handler.base_url

    if key_provider and (handler := handler_for_provider(key_provider)):
        return handler.base_url

    if model_provider and (handler := handler_for_provider(model_provider)):
        return handler.base_url

    return PROVIDER_HANDLERS["openrouter"].base_url


def normalize_model_for_provider(model_id: str, base_url: str) -> str:
    """Strip OpenRouter catalog provider prefixes for native provider APIs."""

    if not model_id or provider_from_base_url(base_url) == "openrouter":
        return model_id
    normalized = model_id.split("/", 1)[1] if "/" in model_id else model_id
    if provider_from_base_url(base_url) == "anthropic":
        normalized = normalized.replace(".", "-")
    return normalized


def catalog_model_provider(model_id: str | None) -> str | None:
    return provider_for_model_id(model_id)


def provider_catalog() -> list[dict[str, Any]]:
    """Non-secret registry summary for admin/API surfaces."""

    return [
        {
            "provider": handler.provider,
            "display_name": handler.display_name,
            "base_url": handler.base_url,
            "api_shape": handler.api_shape,
            "roles": list(handler.roles),
            "env_vars": list(handler.env_vars),
        }
        for handler in PROVIDER_HANDLERS.values()
    ]
