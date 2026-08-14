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

from packages.core.services.model_provider_adapters import (
    KIMI_CHINA_BASE_URL,
    KIMI_GLOBAL_BASE_URL,
    KIMI_NATIVE_CHAT_ADAPTER,
    native_chat_adapter_for_base_url,
    native_chat_adapter_for_provider,
)


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
    base_url_aliases: tuple[str, ...] = ()

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
        roles=("primary", "worker", "image", "voice", "audio", "sfx", "stt", "embedding"),
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
        base_url=KIMI_GLOBAL_BASE_URL,
        env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        key_prefixes=("sk-",),
        api_shape=KIMI_NATIVE_CHAT_ADAPTER.api_shape,
        roles=("primary", "worker"),
        generic_sk=True,
        base_url_aliases=(KIMI_CHINA_BASE_URL,),
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
    "atlascloud": ModelProviderHandler(
        provider="atlascloud",
        display_name="Atlas Cloud",
        base_url="https://api.atlascloud.ai",
        # BYOK-only on purpose: no platform env var, so official-route
        # resolution can never pick up a Manor-side Atlas credential.
        env_vars=(),
        api_shape="atlascloud_video",
        roles=("video",),
    ),
    "zyphra": ModelProviderHandler(
        provider="zyphra",
        display_name="Zyphra",
        base_url="https://api.zyphracloud.com/api/v1",
        base_url_aliases=("https://api.zyphra.com/v1",),
        env_vars=("ZYPHRA_API_KEY",),
        api_shape="zyphra_tts",
        roles=("voice",),
    ),
    "sesame": ModelProviderHandler(
        provider="sesame",
        display_name="Sesame CSM via OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_vars=(
        ),
        key_prefixes=("sk-or-",),
        api_shape="openrouter_audio",
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
    "vercel": ModelProviderHandler(
        provider="vercel",
        display_name="Vercel AI Gateway",
        base_url="https://ai-gateway.vercel.sh/v1",
        env_vars=(
        ),
        api_shape="vercel_ai_gateway",
        roles=("primary", "worker", "image", "voice", "stt", "video", "embedding"),
    ),
}

GENERIC_SK_PROVIDERS = {
    provider for provider, handler in PROVIDER_HANDLERS.items() if handler.generic_sk
}


@dataclass(frozen=True)
class CatalogProviderCapability:
    """Executable BYOK contract for one catalog role/provider pair."""

    test_strategy: str
    runtime_strategy: str


# A catalog entry is not considered BYOK-capable merely because its provider
# has a base URL. It must have both a real live-test route and the same native
# provider runtime used after Save. Keeping this matrix explicit makes catalog
# additions fail closed in tests instead of silently falling through to a
# gateway or to a role-incompatible endpoint.
CATALOG_PROVIDER_CAPABILITIES: dict[
    tuple[str, str], CatalogProviderCapability
] = {
    **{
        (role, provider): CatalogProviderCapability(
            test_strategy="chat_tool_round_trip",
            runtime_strategy="native_chat",
        )
        for role in ("primary", "worker")
        for provider in (
            "openai",
            "anthropic",
            "google",
            "deepseek",
            "qwen",
            "moonshotai",
        )
    },
    ("image", "openai"): CatalogProviderCapability("model_lookup", "openai_images"),
    ("image", "google"): CatalogProviderCapability("model_lookup", "google_generate_content"),
    ("voice", "openai"): CatalogProviderCapability("model_lookup", "openai_speech"),
    ("voice", "google"): CatalogProviderCapability("model_lookup", "google_generate_content_audio"),
    ("voice", "zyphra"): CatalogProviderCapability("account_and_model_lookup", "zyphra_speech"),
    ("voice", "sesame"): CatalogProviderCapability("model_endpoint_lookup", "openrouter_audio"),
    ("audio", "google"): CatalogProviderCapability("model_lookup", "google_interactions_audio"),
    ("audio", "openai"): CatalogProviderCapability("model_lookup", "openai_chat_audio"),
    ("sfx", "openai"): CatalogProviderCapability("model_lookup", "openai_chat_audio"),
    ("stt", "openai"): CatalogProviderCapability("model_lookup", "openai_audio_transcriptions"),
    ("stt", "groq"): CatalogProviderCapability("model_lookup", "groq_audio_transcriptions"),
    ("embedding", "openai"): CatalogProviderCapability("model_lookup", "openai_embeddings"),
    ("video", "bytedance"): CatalogProviderCapability("task_list", "volcengine_video_tasks"),
    ("video", "kwaivgi"): CatalogProviderCapability("task_list", "kling_video_tasks"),
    ("video", "atlascloud"): CatalogProviderCapability("model_list", "atlascloud_video_tasks"),
}


CATALOG_NATIVE_MODEL_ALIASES: dict[str, str] = {
    # Backward compatibility for settings saved before Catalog IDs were
    # corrected to match the provider's real model IDs. These aliases are not
    # exposed by CATALOG; all current Catalog IDs strip directly to wire IDs.
    "openai/gpt-5-image-mini": "gpt-image-1-mini",
    "openai/gpt-5.4-image-2": "gpt-image-2",
}


# Vercel's public Model Catalog uses ``alibaba`` and ``klingai`` namespaces,
# while Manor's fixed product Catalog uses the native-provider namespaces.
# Translate only at the managed Gateway wire boundary.
VERCEL_MODEL_ALIASES: dict[str, str] = {
    # Stable picker IDs predate the corresponding Vercel catalog IDs. Keep
    # saved preferences stable and translate only at the Gateway boundary.
    "openai/gpt-4": "openai/gpt-4-turbo",
    "openai/gpt-5-image-mini": "openai/gpt-image-1-mini",
    "openai/gpt-5.4-image-2": "openai/gpt-image-2",
    "qwen/qwen3.8-max": "alibaba/qwen3.8-max",
    "qwen/qwen3.7-flash": "alibaba/qwen3.7-flash",
    "qwen/qwen3.6-plus": "alibaba/qwen3.6-plus",
    "kwaivgi/kling-v3.0-std": "klingai/kling-v3.0-t2v",
    "kwaivgi/kling-v3.0-pro": "klingai/kling-v3.0-t2v",
}


# The Catalog is a product contract, not a gateway catalog. These namespaces
# are translated only on the wire so saved selections and UI/API payloads stay
# stable when a gateway uses a different publisher namespace.
OPENROUTER_MODEL_ALIASES: dict[str, str] = {
    # Backward compatibility for settings saved while Manor temporarily used
    # Vercel's Alibaba namespace as the product Catalog ID.
    "alibaba/qwen3.8-max": "qwen/qwen3.8-max",
    "alibaba/qwen3.7-flash": "qwen/qwen3.7-flash",
    "alibaba/qwen3.6-plus": "qwen/qwen3.6-plus",
}


PROVIDER_NAMESPACE_ALIASES: dict[str, str] = {
    "alibaba": "qwen",
    "klingai": "kwaivgi",
}


VERCEL_ROLE_MODEL_TYPES: dict[str, str] = {
    "primary": "language",
    "worker": "language",
    "image": "image",
    "voice": "speech",
    "stt": "transcription",
    "video": "video",
    "embedding": "embedding",
}

# Only these media entries use Vercel's modality-specific v4 protocols.
# Other product-catalog entries retain their native or OpenRouter routes.
VERCEL_CATALOG_MEDIA_MODEL_TYPES: dict[tuple[str, str], str] = {
    ("image", "openai/gpt-image-2"): "image",
    ("image", "openai/gpt-5-image-mini"): "image",
    ("voice", "openai/tts-1-hd"): "speech",
    ("voice", "openai/tts-1"): "speech",
    ("stt", "openai/whisper-1"): "transcription",
    ("stt", "openai/gpt-4o-mini-transcribe"): "transcription",
    ("stt", "openai/gpt-4o-transcribe"): "transcription",
    ("video", "bytedance/seedance-2.0"): "video",
    ("video", "bytedance/seedance-2.0-fast"): "video",
    ("video", "kwaivgi/kling-v3.0-std"): "video",
    ("video", "kwaivgi/kling-v3.0-pro"): "video",
    ("embedding", "openai/text-embedding-3-small"): "embedding",
}


def vercel_catalog_model_type(role: str, model_id: str) -> str | None:
    """Return a Vercel v4 protocol type when this catalog entry supports one."""

    normalized_role = str(role or "").strip().lower()
    normalized_model = str(model_id or "").strip().lower()
    if normalized_role in {"primary", "worker"}:
        return "language"
    return VERCEL_CATALOG_MEDIA_MODEL_TYPES.get((normalized_role, normalized_model))


def vercel_model_id(model_id: str) -> str:
    """Return the exact Vercel AI Gateway Model Catalog ID."""

    canonical = str(model_id or "").strip()
    return VERCEL_MODEL_ALIASES.get(canonical, canonical)


def openrouter_model_id(model_id: str) -> str:
    """Return the exact OpenRouter model ID for a fixed Catalog ID."""

    canonical = str(model_id or "").strip()
    return OPENROUTER_MODEL_ALIASES.get(canonical, canonical)


def catalog_vercel_contract_issues(
    catalog: dict[str, list[dict[str, Any]]] | None = None,
    *,
    available_models: dict[str, str] | None = None,
) -> list[str]:
    """Validate catalog entries that use a Vercel v4 protocol.

    Native/BYOK and OpenRouter-backed entries are intentionally skipped.
    ``available_models`` is the live ``id -> type`` map returned by Vercel's
    public ``GET /v1/models`` endpoint. It is optional so ordinary unit tests
    remain hermetic; the release check supplies it for an authoritative audit.
    """

    if catalog is None:
        from packages.core.constants.models import CATALOG

        catalog = CATALOG

    issues: list[str] = []
    for role, entries in catalog.items():
        for item in entries:
            if item.get("deployment") == "local":
                continue
            model_id = str(item.get("id") or "").strip()
            label = f"{role}:{model_id or '<missing model id>'}"
            model_type = vercel_catalog_model_type(role, model_id)
            if not model_type:
                continue
            gateway_id = vercel_model_id(model_id)
            if available_models is not None:
                live_type = str(available_models.get(gateway_id) or "").strip()
                if not live_type:
                    issues.append(f"{label} is absent from Vercel's live Model Catalog")
                elif live_type != model_type:
                    issues.append(
                        f"{label} is Vercel type {live_type}, expected {model_type}"
                    )
    return issues


def catalog_openrouter_contract_issues(
    catalog: dict[str, list[dict[str, Any]]] | None = None,
    *,
    available_models: set[str] | None = None,
) -> list[str]:
    """Return chat Catalog entries unavailable to the OpenRouter fallback.

    Media roles use their native protocol adapters or Vercel's modality-specific
    v4 endpoints; OpenRouter is a managed fallback only for chat roles.
    """

    if catalog is None:
        from packages.core.constants.models import CATALOG

        catalog = CATALOG

    issues: list[str] = []
    for role in ("primary", "worker"):
        for item in catalog.get(role, []):
            if item.get("deployment") == "local":
                continue
            model_id = str(item.get("id") or "").strip()
            label = f"{role}:{model_id or '<missing model id>'}"
            gateway_id = openrouter_model_id(model_id)
            if not gateway_id:
                issues.append(f"{label} has no OpenRouter model mapping")
            elif available_models is not None and gateway_id not in available_models:
                issues.append(
                    f"{label} maps to {gateway_id}, which is absent from OpenRouter"
                )
    return issues


def native_catalog_model_id(model_id: str) -> str:
    """Return the provider wire ID, accepting deprecated saved aliases."""

    canonical = str(model_id or "").strip()
    return CATALOG_NATIVE_MODEL_ALIASES.get(
        canonical,
        canonical.split("/", 1)[1] if "/" in canonical else canonical,
    )


def catalog_provider_capability(
    role: str,
    model_id: str,
) -> CatalogProviderCapability | None:
    normalized_role = str(role or "").strip().lower()
    normalized_model = str(model_id or "").strip().lower()
    if normalized_role == "stt" and normalized_model in {
        "openai/gpt-4o-audio-preview",
        "openai/gpt-audio-mini",
        "openai/gpt-audio",
    }:
        return CatalogProviderCapability("model_lookup", "openai_chat_audio_transcription")
    provider = provider_for_model_id(model_id)
    return CATALOG_PROVIDER_CAPABILITIES.get((normalized_role, provider or ""))


def catalog_byok_contract_issues(
    catalog: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """Return every catalog entry that cannot Test, Save, and run natively."""

    if catalog is None:
        from packages.core.constants.models import CATALOG

        catalog = CATALOG

    issues: list[str] = []
    for role, entries in catalog.items():
        for item in entries:
            model_id = str(item.get("id") or "").strip()
            label = f"{role}:{model_id or '<missing model id>'}"
            if item.get("deployment") == "local":
                if item.get("byok") is not False:
                    issues.append(f"{label} must explicitly declare byok=False")
                continue
            provider = provider_for_model_id(model_id)
            if not provider:
                issues.append(f"{label} has no official provider handler")
                continue
            handler = handler_for_provider(provider)
            if handler is None or role not in handler.roles:
                issues.append(f"{label} is not supported for role {role} by {provider}")
                continue
            if catalog_provider_capability(role, model_id) is None:
                issues.append(f"{label} has no live-test/runtime capability contract")
    return issues


def provider_for_model_id(model_id: str | None) -> str | None:
    """Return the catalog provider implied by ``provider/model`` ids."""

    model = str(model_id or "").strip().lower()
    if not model or "/" not in model:
        return None
    provider = model.split("/", 1)[0].strip()
    provider = PROVIDER_NAMESPACE_ALIASES.get(provider, provider)
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
    host = urlsplit(lower).hostname or ""
    # Shared gateway hosts must win before model-specific handlers that use
    # the same transport (for example Sesame CSM via OpenRouter).
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("ai-gateway.vercel.sh"):
        return "vercel"
    if adapter := native_chat_adapter_for_base_url(lower):
        return adapter.provider
    for provider, handler in PROVIDER_HANDLERS.items():
        for candidate in (handler.base_url, *handler.base_url_aliases):
            provider_base = candidate.lower().rstrip("/")
            if provider_base and (lower == provider_base or lower.startswith(provider_base + "/")):
                return provider
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
    if host.endswith("maas.aliyuncs.com") or (
        host.startswith("dashscope-") and host.endswith(".aliyuncs.com")
    ):
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
    if host.endswith("zyphra.com") or host.endswith("zyphracloud.com"):
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

    provider = provider_from_base_url(base_url)
    if not model_id:
        return model_id
    if provider == "openrouter":
        return openrouter_model_id(model_id)
    if provider == "vercel":
        return vercel_model_id(model_id)
    if adapter := native_chat_adapter_for_provider(provider):
        return adapter.normalize_model(model_id)
    normalized = model_id.split("/", 1)[1] if "/" in model_id else model_id
    if provider == "anthropic":
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
            "base_url_aliases": list(handler.base_url_aliases),
        }
        for handler in PROVIDER_HANDLERS.values()
    ]
