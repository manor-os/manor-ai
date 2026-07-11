"""
Shared HTTP client and chat-completion helper for LLM API calls.

- get_llm_client(): reuse a single AsyncClient; closed on app shutdown.
- chat_completion(): single function for chat completions (used by orchestrator).
- Retries on 429 with exponential back-off (honours Retry-After header).

Ported from manor-multi-agent/apps/llm_client.py with config inlined (no
external config module dependency).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass
import inspect
import json
import logging
import os
import re
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from packages.core.services.model_gateway import (
    GENERIC_SK_PROVIDERS,
    ModelGatewayRoute,
    PROVIDER_HANDLERS,
    detect_provider_from_key as _detect_provider_from_key,
    normalize_model_for_provider as _normalize_model_for_provider,
    provider_for_model_id,
    provider_from_base_url as _provider_from_base_url_registry,
    resolve_official_model_route,
    resolve_provider_base_url as _resolve_provider_base_url,
    route_pricing_source,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers (inlined from apps/config.py — no external dependency)
# ---------------------------------------------------------------------------

DEFAULT_LLM_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_TIMEOUT = 300.0  # user-facing chat should fail loudly instead of hanging for 30 minutes
DEFAULT_LLM_STREAM_IDLE_TIMEOUT = 45.0

LLM_MODEL_ALIASES: Dict[str, str] = {
    # Aliases only point at first-party-served models (Anthropic /
    # OpenAI / Google). Third-party-hosted SKUs (DeepSeek, Moonshot,
    # etc.) are intentionally excluded — see CATALOG inclusion rule
    # in packages/core/constants/models.py for the reasoning.
    "gpt5": "openai/gpt-5",
    "gpt55": "openai/gpt-5.5",
    "gpt5.5": "openai/gpt-5.5",
    "gpt-5.5": "openai/gpt-5.5",
    "chatgpt55": "openai/gpt-5.5",
    "chatgpt5.5": "openai/gpt-5.5",
    "chatgpt-5.5": "openai/gpt-5.5",
    "gpt55-pro": "openai/gpt-5.5-pro",
    "gpt5.5-pro": "openai/gpt-5.5-pro",
    "gpt-5.5-pro": "openai/gpt-5.5-pro",
    "gpt4o": "openai/gpt-4o",
    "gpt4o-mini": "openai/gpt-4o-mini",
    "gpt4": "openai/gpt-4-turbo",
    "openai-40": "openai/gpt-4o",
    "claude": "anthropic/claude-sonnet-4",
    "claude-sonnet": "anthropic/claude-sonnet-4",
    "claude-opus": "anthropic/claude-opus-4.7",
    "opus": "anthropic/claude-opus-4.7",
    "opus-4.7": "anthropic/claude-opus-4.7",
    "claude-opus-4.7": "anthropic/claude-opus-4.7",
    "fable": "anthropic/claude-fable-5",
    "fable-5": "anthropic/claude-fable-5",
    "claude-fable": "anthropic/claude-fable-5",
    "claude-fable-5": "anthropic/claude-fable-5",
    "gemini": "google/gemini-2.5-pro-preview-06-05",
    "gemini-flash": "google/gemini-2.0-flash-001",
}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _resolve_llm_model(raw: str) -> str:
    """Resolve model alias to full OpenRouter model ID."""
    key = (raw or "").strip().lower()
    if key in LLM_MODEL_ALIASES:
        return LLM_MODEL_ALIASES[key]
    return raw.strip() if raw else DEFAULT_LLM_MODEL


_is_byok_call: contextvars.ContextVar[bool] = contextvars.ContextVar("_is_byok_call", default=False)
_llm_route_provider: contextvars.ContextVar[str] = contextvars.ContextVar("_llm_route_provider", default="")
_llm_pricing_source: contextvars.ContextVar[str] = contextvars.ContextVar("_llm_pricing_source", default="")


ResolvedLLMRouting = ModelGatewayRoute


class LLMAuthConfigurationError(Exception):
    """Raised when the configured LLM key cannot authenticate the target API."""


def _platform_model_routing_enabled() -> bool:
    """Cloud-only Manor-managed model routing.

    OSS must use tenant BYOK metadata (or explicitly local providers handled
    elsewhere); it should never silently fall back to Manor/OpenRouter platform
    credentials.
    """

    return os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"


def _missing_llm_key_detail() -> str:
    if not _platform_model_routing_enabled():
        return (
            "No usable LLM API key found. Self-hosted mode requires your own "
            "native provider API key for the selected model; Manor official "
            "routing is Cloud-only."
        )
    return (
        "No usable LLM API key found. A user-provided native provider key was "
        "not configured for this request, and no platform official provider "
        "token or OpenRouter fallback is configured in admin or the API process "
        "environment."
    )


# ---------------------------------------------------------------------------
# Provider registry — maps model prefixes to official API endpoints.
# When a BYOK user provides a direct provider key (not OpenRouter), we
# auto-detect the provider from the model ID and route accordingly.
# ---------------------------------------------------------------------------

# Providers with custom APIs (ByteDance Volcano Engine video tasks, etc.) are
# NOT included here — those must go through a role-specific adapter or
# OpenRouter which proxies them.
_PROVIDER_ENDPOINTS: dict[str, dict] = {
    provider: {
        "base_url": handler.base_url,
        "key_prefix": handler.key_prefixes[0] if handler.key_prefixes else "",
        "api_shape": handler.api_shape,
    }
    for provider, handler in PROVIDER_HANDLERS.items()
}

_GENERIC_SK_PROVIDER_PREFIXES = set(GENERIC_SK_PROVIDERS)


def detect_provider_from_key(api_key: str) -> str | None:
    """Detect which provider a BYOK key belongs to by its prefix.

    Some providers share the generic ``sk-`` prefix. For those providers,
    ``resolve_provider_base_url`` combines this signal with the selected
    model's catalog prefix.
    """
    return _detect_provider_from_key(api_key)


def detect_provider_from_model(model_id: str) -> str | None:
    """Detect provider from an OpenRouter-style model ID prefix."""
    return provider_for_model_id(model_id)


def resolve_provider_base_url(model_id: str, api_key: str, user_base_url: str | None = None) -> str:
    """Resolve the correct base URL for a BYOK call.

    Priority:
    1. User-specified base_url (explicit override)
    2. Auto-detect from API key prefix (most reliable)
    3. Auto-detect from model ID prefix
    4. Default: OpenRouter
    """
    return _resolve_provider_base_url(model_id, api_key, user_base_url)


def _provider_from_base_url(base_url: str) -> str | None:
    return _provider_from_base_url_registry(base_url)


def _is_anthropic_messages_api(base_url: str) -> bool:
    return _provider_from_base_url(base_url) == "anthropic"


def normalize_model_for_provider(model_id: str, base_url: str) -> str:
    """Strip the OpenRouter provider prefix when calling a provider directly.

    OpenRouter model IDs look like "anthropic/claude-sonnet-4.6" — the
    prefix routes within OpenRouter. Direct provider APIs don't
    understand the prefix; they need just "claude-sonnet-4.6".

    Skip stripping when the target IS OpenRouter.
    """
    return _normalize_model_for_provider(model_id, base_url)


def _native_byok_key_from_metadata(metadata: Optional[Dict[str, Any]]) -> str:
    if not metadata:
        return ""
    for field, label in (
        ("llm_api_key", "metadata.llm_api_key"),
        ("api_key", "metadata.api_key"),
        ("_resolved_api_key", "metadata._resolved_api_key"),
    ):
        raw = metadata.get(field)
        if raw and str(raw).strip():
            key = _sanitize_api_key(str(raw), label)
            if key and not key.startswith("sk-or-") and _metadata_allows_native_byok(metadata, key):
                return key
    return ""


def mark_byok_from_metadata(metadata: Optional[Dict[str, Any]]) -> bool:
    is_byok = bool(_native_byok_key_from_metadata(metadata))
    _is_byok_call.set(is_byok)
    return is_byok


def metadata_has_native_byok(metadata: Optional[Dict[str, Any]]) -> bool:
    """Pure BYOK check for entrypoint gates that must not mutate call context."""

    return bool(_native_byok_key_from_metadata(metadata))


def get_api_key(metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return LLM API key.

    Priority:
    1. metadata.llm_api_key (per-request override)
    2. metadata._resolved_api_key (entity-level key, resolved by chat service)
    3. Cloud-only platform environment fallback

    Sets _is_byok_call contextvar when user's own key is used (for billing).
    """
    if metadata:
        user_key = metadata.get("llm_api_key") or metadata.get("api_key")
        if user_key and str(user_key).strip():
            key = _sanitize_api_key(str(user_key), "metadata.llm_api_key")
            if key:
                if key.startswith("sk-or-"):
                    logger.warning(
                        "Ignoring user-supplied OpenRouter key in BYOK metadata; "
                        "BYOK is native-provider-only for this route."
                    )
                elif not _metadata_allows_native_byok(metadata, key):
                    logger.warning(
                        "Ignoring user-supplied native BYOK key because it does not match the selected model routing."
                    )
                else:
                    _is_byok_call.set(True)
                    return key
        # Entity-level key resolved async before LLM call
        resolved = metadata.get("_resolved_api_key")
        if resolved and str(resolved).strip():
            key = _sanitize_api_key(str(resolved), "metadata._resolved_api_key")
            if key:
                if key.startswith("sk-or-"):
                    logger.warning(
                        "Ignoring resolved OpenRouter key in BYOK metadata; "
                        "BYOK is native-provider-only for this route."
                    )
                elif not _metadata_allows_native_byok(metadata, key):
                    logger.warning(
                        "Ignoring resolved native BYOK key because it does not match the selected model routing."
                    )
                else:
                    _is_byok_call.set(True)
                    return key
    _is_byok_call.set(False)
    if not _platform_model_routing_enabled():
        return ""
    return ""


def _strip_api_key_wrappers(raw: str) -> str:
    """Accept common pasted key formats while returning the bare token."""
    key = (raw or "").strip()
    for _ in range(4):
        before = key
        if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
            key = key[1:-1].strip()
        lowered = key.lower()
        if lowered.startswith("authorization:"):
            key = key.split(":", 1)[1].strip()
            lowered = key.lower()
        if lowered.startswith("bearer "):
            key = key[7:].strip()
        if key == before:
            break
    return key


def _sanitize_api_key(key: str, source: str = "API key") -> str:
    """Validate API key is usable as an HTTP header value.

    Returns empty string if the key is invalid. The normalizer tolerates
    operator-friendly pasted forms like ``Bearer sk-or-...`` and quoted env
    values, but the final token must be a single ASCII header value.
    """
    key = _strip_api_key_wrappers(key)
    if not key:
        return ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in key) or len(key) < 20:
        logger.warning(
            "Invalid LLM API key in %s (whitespace/control chars or too short); ignoring. Preview: %r",
            source,
            key[:6],
        )
        return ""
    try:
        key.encode("ascii")
        return key
    except UnicodeEncodeError:
        logger.warning(
            "LLM API key in %s contains non-ASCII characters; ignoring. First 4 chars: %r",
            source,
            key[:4],
        )
        return ""


def _llm_request_headers(api_key: str, base_url: str) -> Dict[str, str]:
    key = _sanitize_api_key(api_key, "request")
    if not key:
        raise LLMAuthConfigurationError(
            "LLM API key is empty or malformed after normalization. "
            "Configure a valid API key for the selected provider."
        )

    if "openrouter.ai" in (base_url or "").lower() and not key.startswith("sk-or-"):
        if key.startswith("sk-ant-"):
            hint = "an Anthropic sk-ant key"
        elif key.startswith("sk-"):
            hint = "an OpenAI sk key"
        else:
            hint = "a non-OpenRouter key"
        raise LLMAuthConfigurationError(
            "OpenRouter requires an OpenRouter API key that starts with sk-or-. "
            f"The configured key looks like {hint}. "
            "Choose a direct provider/base URL that matches the configured key."
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in (base_url or "").lower():
        headers.update({"HTTP-Referer": "https://manor.ai", "X-Title": "Manor AI"})
    return headers


def _anthropic_request_headers(api_key: str) -> Dict[str, str]:
    key = _sanitize_api_key(api_key, "request")
    if not key:
        raise LLMAuthConfigurationError(
            "Anthropic API key is empty or malformed after normalization."
        )
    if not key.startswith("sk-ant-"):
        raise LLMAuthConfigurationError(
            "Anthropic native calls require an Anthropic API key that starts with sk-ant-. "
            "Use an OpenRouter sk-or-... key for cross-provider routing."
        )
    return {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _model_provider_prefix(model_id: str) -> str | None:
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0].strip().lower() or None


def _metadata_model_id(metadata: Optional[Dict[str, Any]]) -> str:
    if not metadata:
        return ""
    return str(metadata.get("_resolved_model") or metadata.get("model") or "").strip()


def _provider_matches_model(provider: str | None, model_provider: str | None) -> bool:
    if not provider or not model_provider:
        return True
    if provider == "openrouter":
        return True
    return provider == model_provider


def _base_url_matches_model(base_url: str, model_id: str) -> bool:
    model_provider = _model_provider_prefix(model_id)
    base_provider = _provider_from_base_url(base_url)
    return _provider_matches_model(base_provider, model_provider)


def _metadata_allows_native_byok(metadata: Optional[Dict[str, Any]], key: str) -> bool:
    """Return whether a saved native BYOK credential can serve this request.

    Users can switch the selected catalog model back to Manor official routing
    while a stale role-level BYOK key/base URL is still saved. In that case we
    ignore the mismatched native credential instead of routing, for example, an
    Anthropic model to a DeepSeek endpoint.
    """
    if not metadata:
        return True
    model_id = _metadata_model_id(metadata)
    model_provider = _model_provider_prefix(model_id)
    if not model_provider:
        # Bare custom model IDs depend on the user's custom endpoint.
        return True

    base_url = str(metadata.get("llm_base_url") or metadata.get("base_url") or "").strip()
    if base_url and not _base_url_matches_model(base_url, model_id):
        base_provider = _provider_from_base_url(base_url)
        logger.warning(
            "Ignoring native BYOK key because selected model %s is provider %s but base_url routes to %s.",
            model_id,
            model_provider,
            base_provider or "unknown",
        )
        return False

    key_provider = detect_provider_from_key(key)
    if not key_provider or key_provider == "openrouter":
        return key_provider != "openrouter"
    if key_provider == "openai" and model_provider in _GENERIC_SK_PROVIDER_PREFIXES:
        return True
    if key_provider != model_provider:
        logger.warning(
            "Ignoring native BYOK key from provider %s because selected model %s is provider %s.",
            key_provider,
            model_id,
            model_provider,
        )
        return False
    return True


def _validate_llm_key_model_compatibility(api_key: str, base_url: str, model_id: str) -> None:
    """Fail early when a BYOK key cannot call the selected chat model."""
    key = _sanitize_api_key(api_key, "request")
    if not key:
        return

    key_provider = detect_provider_from_key(key)
    if not key_provider or key_provider == "openrouter":
        return

    model_provider = _model_provider_prefix(model_id)
    base_provider = _provider_from_base_url(base_url)

    if base_provider and model_provider and base_provider != "openrouter" and model_provider != base_provider:
        raise LLMAuthConfigurationError(
            f"The selected model {model_id} is a {model_provider} model, but the request is routed to {base_provider}. "
            "Choose a model/base URL from the same provider."
        )

    if (
        key_provider == "openai"
        and model_provider in _GENERIC_SK_PROVIDER_PREFIXES
        and base_provider == model_provider
    ):
        return

    if model_provider and model_provider != key_provider:
        raise LLMAuthConfigurationError(
            f"A {key_provider} API key cannot call the selected {model_provider} model ({model_id}). "
            "Choose a model from the same provider."
        )


def get_llm_model() -> str:
    """Return model ID from env var. Supports aliases."""
    raw = (os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL).strip()
    return _resolve_llm_model(raw)


def resolve_model(
    user_model: str | None = None,
    entity_model: str | None = None,
    role: str = "primary",
    user_prefs: dict | None = None,
    entity_settings: dict | None = None,
) -> str:
    """Resolve model with priority: user > entity > env > default.

    Supports both legacy (single model string) and new (per-role from prefs/settings).

    Args:
        user_model: legacy User.llm_model string
        entity_model: legacy Entity.llm_model string
        role: model role — "primary", "worker", "image", "voice", "video", "embedding"
        user_prefs: User.preferences JSONB dict (has .models.{role})
        entity_settings: Entity.settings JSONB dict (has .models.{role})
    """
    # New per-role resolution from JSONB prefs/settings
    if user_prefs or entity_settings:
        from packages.core.constants.models import resolve_model_for_role
        resolved = resolve_model_for_role(role, user_prefs, entity_settings)
        if resolved:
            return _resolve_llm_model(resolved)

    # Legacy single-model resolution
    raw = (
        (user_model or "").strip()
        or (entity_model or "").strip()
        or (os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL).strip()
    )
    return _resolve_llm_model(raw)


def get_llm_base_url() -> str:
    """Return LLM base URL.  Override with LLM_BASE_URL."""
    return (
        os.getenv("OPENROUTER_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or DEFAULT_LLM_BASE_URL
    ).rstrip("/")


def get_llm_base_url_for_metadata(metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return LLM base URL.  Priority: user_url > auto-detect from BYOK key > env default."""
    if metadata:
        model = _metadata_model_id(metadata)
        # Explicit base_url override always wins
        user_url = metadata.get("llm_base_url") or metadata.get("base_url")
        if user_url and str(user_url).strip():
            url = str(user_url).strip().rstrip("/")
            try:
                url.encode("ascii")
                if _base_url_matches_model(url, model):
                    return url
                logger.warning(
                    "Ignoring LLM base_url %s because it does not match selected model %s.",
                    url,
                    model or "(unknown)",
                )
            except UnicodeEncodeError:
                logger.warning("User LLM base_url contains non-ASCII; ignoring: %r", url[:30])

        # Auto-detect from BYOK key + model when no explicit base_url
        user_key = (
            metadata.get("llm_api_key")
            or metadata.get("api_key")
            or metadata.get("_resolved_api_key")
            or ""
        )
        if user_key and str(user_key).strip():
            key = _sanitize_api_key(str(user_key), "metadata.llm_api_key")
            if key and key.startswith("sk-or-"):
                return get_llm_base_url()
            if key and _metadata_allows_native_byok(metadata, key):
                return resolve_provider_base_url(str(model), key)

    return get_llm_base_url()


async def resolve_llm_routing_for_model(
    model: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> ResolvedLLMRouting:
    """Resolve API key + base URL for a concrete catalog model.

    Order:
      1. Native user BYOK from metadata
      2. Platform official token for the model provider, configured in admin
      3. Platform OpenRouter fallback
      4. Legacy env fallback for bare/custom model ids
    """

    model_id = str(model or "").strip()
    if metadata:
        user_key = metadata.get("llm_api_key") or metadata.get("api_key")
        if user_key and str(user_key).strip():
            key = _sanitize_api_key(str(user_key), "metadata.llm_api_key")
            if key:
                if key.startswith("sk-or-"):
                    logger.warning(
                        "Ignoring user-supplied OpenRouter key in BYOK metadata; "
                        "BYOK is native-provider-only for this route."
                    )
                elif _metadata_allows_native_byok(metadata, key):
                    base_url = get_llm_base_url_for_metadata(metadata)
                    _is_byok_call.set(True)
                    return ResolvedLLMRouting(
                        api_key=key,
                        base_url=base_url,
                        provider=_provider_from_base_url(base_url) or detect_provider_from_key(key),
                        source="byok",
                    )
                else:
                    logger.warning(
                        "Ignoring user-supplied native BYOK key because it does not match the selected model routing."
                    )
        resolved = metadata.get("_resolved_api_key")
        if resolved and str(resolved).strip():
            key = _sanitize_api_key(str(resolved), "metadata._resolved_api_key")
            if key:
                if key.startswith("sk-or-"):
                    logger.warning(
                        "Ignoring resolved OpenRouter key in BYOK metadata; "
                        "BYOK is native-provider-only for this route."
                    )
                elif _metadata_allows_native_byok(metadata, key):
                    base_url = get_llm_base_url_for_metadata(metadata)
                    _is_byok_call.set(True)
                    return ResolvedLLMRouting(
                        api_key=key,
                        base_url=base_url,
                        provider=_provider_from_base_url(base_url) or detect_provider_from_key(key),
                        source="byok",
                    )

    model_provider = detect_provider_from_model(model_id)
    if not _platform_model_routing_enabled():
        _is_byok_call.set(False)
        return ResolvedLLMRouting(
            api_key="",
            base_url=get_llm_base_url(),
            provider=model_provider,
            source="missing",
        )

    if model_provider:
        try:
            route = await resolve_official_model_route(
                model_id,
                reason="llm.chat.official_provider_key",
                openrouter_reason="llm.chat.openrouter_fallback_key",
            )
            if route and route.api_key:
                _is_byok_call.set(False)
                return route
        except Exception:
            logger.debug("Official LLM provider routing lookup failed", exc_info=True)

    # Legacy fallback for bare custom model ids and test/dev setups. Keep the
    # old behavior here, but chat catalog models should normally resolve above.
    key = get_api_key(metadata)
    base_url = get_llm_base_url_for_metadata(metadata)
    if key:
        return ResolvedLLMRouting(
            api_key=key,
            base_url=base_url,
            provider=_provider_from_base_url(base_url) or detect_provider_from_key(key),
            source="byok" if _is_byok_call.get(False) else "legacy",
        )
    _is_byok_call.set(False)
    return ResolvedLLMRouting(
        api_key="",
        base_url=get_llm_base_url(),
        provider=model_provider,
        source="missing",
    )


def get_llm_timeout() -> float:
    """Return request timeout in seconds.  Override with LLM_TIMEOUT."""
    try:
        return float(
            os.getenv("OPENROUTER_TIMEOUT_SECONDS")
            or os.getenv("LLM_TIMEOUT")
            or DEFAULT_LLM_TIMEOUT
        )
    except Exception:
        return float(DEFAULT_LLM_TIMEOUT)


def get_llm_stream_idle_timeout() -> float:
    """Return max seconds to wait for the next streaming chunk before fallback."""
    try:
        return float(
            os.getenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS")
            or os.getenv("LLM_STREAM_IDLE_TIMEOUT")
            or DEFAULT_LLM_STREAM_IDLE_TIMEOUT
        )
    except Exception:
        return float(DEFAULT_LLM_STREAM_IDLE_TIMEOUT)


# ---------------------------------------------------------------------------
# LLMRateLimited exception
# ---------------------------------------------------------------------------

class LLMRateLimited(BaseException):
    """Cooperative-backoff signal raised on a long-duration 429.

    Inherits from :class:`BaseException` (not :class:`Exception`) for the same
    reason :class:`asyncio.CancelledError` does: this is a control-flow signal
    that should bypass the dozens of routine ``except Exception`` blocks
    between :func:`_post_with_retry` and the goal runner. Otherwise any
    upstream agent or tool layer that catches and converts ``Exception`` into
    a string error response would silently turn cooperative backoff into a
    completed-with-error step -- defeating the whole point of releasing the
    lease and rescheduling.

    The goal runner catches it in ``_execute_step_with_timeout`` and converts
    it to ``outcome=retry`` with ``retry_after_seconds`` set to
    :attr:`retry_after`, releasing the lease so the claim_tick sweeper can
    re-dispatch after the provider's cooldown. Short 429s (under
    :data:`_COOPERATIVE_BACKOFF_SECS`) still sleep in-process because the
    warm context is cheaper than a full DB round-trip.

    Note: ``asyncio.gather(return_exceptions=True)`` still aggregates
    BaseException subclasses into its results list, so any caller using that
    pattern needs an explicit isinstance check to re-raise.
    """

    def __init__(self, retry_after: float, message: str = "") -> None:
        self.retry_after = max(float(retry_after), 0.0)
        super().__init__(message or f"LLM rate limited; retry after {self.retry_after:.1f}s")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None   # loop _lock was created on
_client_loop: Optional[asyncio.AbstractEventLoop] = None  # loop _client was created on

EMPTY_USAGE: Dict[str, Any] = {"prompt": 0, "completion": 0, "total": 0, "model": None, "cost_usd": None}


def _annotate_usage_with_billing_source(
    usage: Optional[Dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach non-secret routing metadata to usage so aggregate billing stays correct."""

    if usage is None or usage is EMPTY_USAGE:
        usage = EMPTY_USAGE.copy()
    if model and not usage.get("model"):
        usage["model"] = model

    route_provider = _llm_route_provider.get("")
    pricing_source = _llm_pricing_source.get("")
    if route_provider and not usage.get("provider"):
        usage["provider"] = route_provider
    if pricing_source and not usage.get("pricing_source"):
        usage["pricing_source"] = pricing_source
        usage["llm_pricing_source"] = pricing_source

    if _is_byok_call.get(False):
        usage["byok"] = True
        usage["billing_mode"] = "byok"
        usage["llm_billing_mode"] = "byok"
        usage["api_key_source"] = "byok"
        usage["llm_api_key_source"] = "byok"
        usage["pricing_source"] = "byok"
        usage["llm_pricing_source"] = "byok"
    return usage


def _usage_has_byok_marker(usage: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(usage, dict):
        return False
    if bool(usage.get("byok")):
        return True
    for key in ("billing_mode", "llm_billing_mode", "api_key_source", "llm_api_key_source"):
        if str(usage.get(key) or "").lower() == "byok":
            return True
    return False
_llm_call_history_var: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "llm_call_history",
    default=None,
)

# ── Billing context — set once per request/task, auto-records every LLM call ──

@dataclass
class LLMBillingContext:
    """Set via ``with llm_billing_context(...)`` to auto-record usage on every LLM call."""
    entity_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    workspace_id: Optional[str] = None
    """Workspace this LLM call is attributed to. Lets us slice spend
    per workspace in admin views — fall through to entity_id when the
    call isn't workspace-scoped (e.g. account-level prompt previews)."""
    conversation_id: Optional[str] = None
    source: str = "system"
    suppress: bool = False  # True = context is set but recording is suppressed (chat handles its own)
    byok: bool = False  # True = user's own API key; log usage for analytics but charge 0 credits
    in_flight_credits: int = 0
    """Credits consumed in this request but not necessarily visible in the
    ledger yet. Chat suppresses per-round billing and writes aggregate usage
    at the end, so this local counter lets preflight gates stop long loops
    as soon as the current request has spent the remaining balance."""

_billing_ctx_var: contextvars.ContextVar[Optional[LLMBillingContext]] = contextvars.ContextVar(
    "llm_billing_ctx", default=None,
)
_entity_in_flight_credits: dict[str, int] = {}
_entity_in_flight_lock = threading.Lock()

@contextlib.asynccontextmanager
async def llm_billing_context(
    entity_id: str,
    *,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    source: str = "system",
    byok: bool = False,
):
    """Context manager: every LLM call within this scope auto-records usage.

    Usage:
        async with llm_billing_context("entity_123", source="strategist"):
            result = await chat_completion(messages)
            # usage auto-recorded to billing
    """
    ctx = LLMBillingContext(
        entity_id=entity_id, user_id=user_id, agent_id=agent_id,
        workspace_id=workspace_id, conversation_id=conversation_id,
        source=source, byok=byok,
    )
    # Restore the *previous* value on exit via set(prev) rather than
    # reset(token). A ContextVar Token is bound to the context that called
    # set(); the Celery worker wraps each task in its own asyncio.run, so the
    # finally can run in a different context than the enter, and reset(token)
    # then raises "Token was created in a different Context". set(prev) is not
    # context-bound, and it correctly restores an outer billing scope so usage
    # after a nested scope is still billed to the right entity.
    prev = _billing_ctx_var.get()
    _billing_ctx_var.set(ctx)
    try:
        yield ctx
    finally:
        _billing_ctx_var.set(prev)

# Retry settings for transient LLM provider errors.
_MAX_RETRIES = 5          # up to 5 retries (6 total attempts)
_BASE_DELAY  = 1.0        # seconds before first retry
_MAX_DELAY   = 30.0       # cap on exponential back-off
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 524})

# Threshold above which a 429 Retry-After is handled cooperatively by
# raising LLMRateLimited instead of sleeping in-process.
_COOPERATIVE_BACKOFF_SECS = max(_env_int("LLM_COOPERATIVE_BACKOFF_SECS", 5), 1)

_BASE64_CHARS_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_BINARY_DATA_URL_PREFIXES = (
    "data:application/pdf;base64,",
    "data:application/octet-stream;base64,",
    "data:image/",
)
_BASE64_SIGNATURE_PREFIXES = (
    "JVBERi0",         # PDF
    "/9j/",            # JPEG
    "iVBORw0KGgo",     # PNG
    "R0lGOD",          # GIF
    "UklGR",           # WEBP/RIFF
    "UEsDB",           # ZIP-based Office docs
)
_MAX_LLM_TOOLS = 128
_StreamHandler = Optional[Callable[[str, Dict[str, Any]], Any]]


_VISION_MODEL_PREFIXES = (
    "openai/gpt-4o",
    "openai/gpt-4.1",
    "google/gemini-",
    "anthropic/claude-",
)
_LLM_ROUTING_METADATA_FIELDS = (
    "llm_api_key",
    "api_key",
    "_resolved_api_key",
    "llm_base_url",
    "base_url",
)


def _messages_include_image(messages: List[Dict[str, Any]]) -> bool:
    def _walk(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type") == "image_url" or "image_url" in value:
                return True
            return any(_walk(v) for v in value.values())
        if isinstance(value, list):
            return any(_walk(v) for v in value)
        return False

    return any(_walk(m.get("content")) for m in messages)


def _model_supports_image_input(model: str) -> bool:
    m = (model or "").strip()
    return any(m.startswith(prefix) for prefix in _VISION_MODEL_PREFIXES)


def _resolve_vision_model_if_needed(model: str, messages: List[Dict[str, Any]]) -> str:
    if not _messages_include_image(messages) or _model_supports_image_input(model):
        return model
    from packages.core.constants.models import DEFAULTS

    system_default = str(DEFAULTS.get("primary") or "").strip()
    fallback = (
        os.getenv("LLM_VISION_MODEL")
        or os.getenv("OPENROUTER_VISION_MODEL")
        or system_default
        or "openai/gpt-4o"
    ).strip()
    if not _model_supports_image_input(fallback):
        fallback = "openai/gpt-4o"
    logger.info("Switching model for image input: %s -> %s", model, fallback)
    return fallback


def _route_metadata_for_resolved_model(
    metadata: Optional[Dict[str, Any]],
    requested_model: str,
    resolved_model: str,
) -> Optional[Dict[str, Any]]:
    if not metadata:
        return metadata

    routed = dict(metadata)
    routed["_resolved_model"] = resolved_model
    if requested_model == resolved_model:
        return routed

    has_routing_metadata = any(routed.get(field) for field in _LLM_ROUTING_METADATA_FIELDS)
    if not has_routing_metadata:
        return routed

    native_key = _native_byok_key_from_metadata(routed)
    explicit_base_url = routed.get("llm_base_url") or routed.get("base_url")
    model_provider = _model_provider_prefix(resolved_model)
    incompatible_reason = ""

    if native_key:
        candidate_base_url = resolve_provider_base_url(
            resolved_model,
            native_key,
            str(explicit_base_url) if explicit_base_url else None,
        )
        try:
            _validate_llm_key_model_compatibility(native_key, candidate_base_url, resolved_model)
        except LLMAuthConfigurationError as exc:
            incompatible_reason = str(exc)
    elif explicit_base_url and model_provider:
        base_provider = _provider_from_base_url(str(explicit_base_url))
        if base_provider and base_provider != "openrouter" and base_provider != model_provider:
            incompatible_reason = (
                f"model {resolved_model} is {model_provider}, but base URL routes to {base_provider}"
            )

    if not incompatible_reason:
        return routed

    sanitized = dict(routed)
    for field in _LLM_ROUTING_METADATA_FIELDS:
        sanitized.pop(field, None)
    sanitized["_resolved_model"] = resolved_model
    logger.warning(
        "Ignoring incompatible LLM BYOK routing after image-input model switch %s -> %s: %s",
        requested_model,
        resolved_model,
        incompatible_reason,
    )
    return sanitized


def _openrouter_provider_block(base_url: str | None = None) -> Optional[Dict[str, Any]]:
    """Return the OpenRouter ``provider`` routing block, or None to skip.

    Some OpenRouter back-end providers (Novita is the recurring offender)
    return HTTP 400 ``invalid_request_error`` on payloads other providers
    accept — different tolerance for tool schemas, message shapes, system
    prompt size. We default to excluding Novita for this reason.

    Override with ``OPENROUTER_IGNORE_PROVIDERS`` (comma-separated). Set
    to ``""`` to disable the block entirely. Only applied when the base
    URL points at openrouter.ai (no-op otherwise so non-OR setups aren't
    affected).
    """
    target_base_url = (base_url or os.getenv("LLM_BASE_URL") or DEFAULT_LLM_BASE_URL or "").lower()
    if "openrouter.ai" not in target_base_url:
        return None
    raw = os.getenv("OPENROUTER_IGNORE_PROVIDERS", "Novita").strip()
    if not raw:
        return None
    ignored = [p.strip() for p in raw.split(",") if p.strip()]
    if not ignored:
        return None
    return {"ignore": ignored, "allow_fallbacks": True}


# ---------------------------------------------------------------------------
# Binary blob detection & message sanitization
# ---------------------------------------------------------------------------

def _looks_like_binary_blob(value: str) -> bool:
    text = (value or "").strip()
    if len(text) < 512:
        return False

    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in _BINARY_DATA_URL_PREFIXES):
        return True

    compact = re.sub(r"\s+", "", text)
    if len(compact) < 512:
        return False
    if any(compact.startswith(prefix) for prefix in _BASE64_SIGNATURE_PREFIXES):
        return True
    if not _BASE64_CHARS_RE.fullmatch(compact):
        return False

    special_ratio = sum(ch in "+/=" for ch in compact) / max(len(compact), 1)
    return len(compact) >= 1500 and special_ratio >= 0.02


def _sanitize_llm_value(value: Any, *, path: Tuple[str, ...] = ()) -> Tuple[Any, int]:
    if isinstance(value, str):
        # Preserve multimodal `image_url.url` blocks; these are intentionally binary.
        if len(path) >= 2 and path[-2:] == ("image_url", "url"):
            return value, 0
        if _looks_like_binary_blob(value):
            return (
                f"[Binary payload omitted before LLM call: suspected base64/PDF/image content, "
                f"{len(value):,} chars removed.]",
                1,
            )
        return value, 0
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        changes = 0
        for k, v in value.items():
            cleaned, count = _sanitize_llm_value(v, path=path + (str(k),))
            sanitized[k] = cleaned
            changes += count
        return sanitized, changes
    if isinstance(value, list):
        sanitized_list: List[Any] = []
        changes = 0
        for idx, item in enumerate(value):
            cleaned, count = _sanitize_llm_value(item, path=path + (str(idx),))
            sanitized_list.append(cleaned)
            changes += count
        return sanitized_list, changes
    return value, 0


def _sanitize_messages_for_llm(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sanitized, changes = _sanitize_llm_value(messages, path=("messages",))
    if changes:
        logger.warning(
            "Sanitized %d binary-looking payload(s) before LLM call",
            changes,
        )
    sanitized = _normalize_message_shapes(sanitized)
    sanitized = _repair_tool_call_message_sequence(sanitized)
    _fix_image_mime_types(sanitized)
    return sanitized


def _normalize_message_shapes(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Patch up message-shape quirks that some OpenRouter-routed providers
    (Novita, parts of Anthropic) reject with HTTP 400 even though the
    OpenAI spec permits them.

    Currently handles:
      * assistant messages where ``content`` is ``None`` but ``tool_calls``
        is present — convert ``None`` → ``""`` (empty string).
      * tool messages where ``content`` is ``None`` — convert to ``""``
        (tool messages must have string content per spec).
      * tool messages missing ``tool_call_id`` — hoist from a stray
        ``tool_calls=[{"id": ...}]`` array if present (legacy callers).
      * assistant ``tool_calls`` in the flat ``{id,name,arguments}`` shape
        — re-wrap to OpenAI's nested ``{id,type,function:{...}}`` form.
    """
    null_content_fixed = 0
    tool_id_fixed = 0
    flat_tool_calls_fixed = 0
    out: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")

        if role == "assistant":
            patched = dict(m)
            if patched.get("content") is None and patched.get("tool_calls"):
                patched["content"] = ""
                null_content_fixed += 1
            tcs = patched.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                rewrapped, changed = _rewrap_tool_calls_to_openai(tcs)
                if changed:
                    patched["tool_calls"] = rewrapped
                    flat_tool_calls_fixed += 1
            out.append(patched)
            continue

        if role == "tool":
            patched = dict(m)
            if patched.get("content") is None:
                patched["content"] = ""
                null_content_fixed += 1
            if not patched.get("tool_call_id"):
                stray = patched.get("tool_calls")
                if isinstance(stray, list) and stray:
                    candidate = (stray[0] or {}).get("id")
                    if candidate:
                        patched["tool_call_id"] = candidate
                        tool_id_fixed += 1
            # tool_calls is invalid on tool messages — strip it.
            patched.pop("tool_calls", None)
            out.append(patched)
            continue

        out.append(m)

    if null_content_fixed or tool_id_fixed or flat_tool_calls_fixed:
        logger.debug(
            "Normalized messages before LLM call: null_content=%d tool_id_hoisted=%d "
            "flat_tool_calls_rewrapped=%d",
            null_content_fixed, tool_id_fixed, flat_tool_calls_fixed,
        )
    return out


def _tool_call_id(tc: Any) -> str:
    if not isinstance(tc, dict):
        return ""
    value = tc.get("id")
    return str(value) if value else ""


def _tool_call_name(tc: Any) -> str:
    if not isinstance(tc, dict):
        return "tool"
    fn = tc.get("function") or {}
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn.get("name"))
    return str(tc.get("name") or "tool")


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _tool_message_history_note(message: Dict[str, Any]) -> str:
    call_id = str(message.get("tool_call_id") or "unknown")
    content = _message_content_to_text(message.get("content"))
    preview = " ".join(content.split())
    if len(preview) > 500:
        preview = preview[:500] + "..."
    return (
        f"[Historical tool result omitted from protocol replay: "
        f"tool_call_id={call_id}]\n{preview}"
    )


def _collapsed_tool_call_turn(
    message: Dict[str, Any],
    tool_messages: List[Dict[str, Any]],
    missing_ids: List[str],
) -> Dict[str, Any]:
    patched = dict(message)
    tool_calls = patched.pop("tool_calls", None) or []
    names = [_tool_call_name(tc) for tc in tool_calls]
    note = (
        "[Historical tool-call transcript omitted before LLM call because it "
        "was incomplete. Called tools: "
        + (", ".join(names) if names else "unknown")
    )
    if missing_ids:
        note += f"; missing tool results for: {', '.join(missing_ids[:8])}"
        if len(missing_ids) > 8:
            note += f", ... {len(missing_ids) - 8} more"
    note += ".]"

    partial_results = [
        _tool_message_history_note(tool_message)
        for tool_message in tool_messages
        if isinstance(tool_message, dict)
    ]
    content = _message_content_to_text(patched.get("content"))
    parts = [part for part in (content, note, *partial_results) if part]
    patched["content"] = "\n\n".join(parts)
    return patched


def _repair_tool_call_message_sequence(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure OpenAI tool-call adjacency before sending to providers.

    The protocol is stricter than a plain chat transcript: an assistant turn
    with ``tool_calls`` must be followed immediately by one ``role="tool"``
    message for every requested ``tool_call_id``. Some persisted Manor chat
    rows store frontend tool previews in ``Message.tool_calls`` without the
    sibling tool rows, and SiliconFlow/DeepSeek rejects those histories with
    HTTP 400. Rather than fail the user's next request, collapse malformed
    historical tool-call turns into ordinary text while leaving valid live
    agentic-loop turns unchanged.
    """
    out: List[Dict[str, Any]] = []
    repaired_turns = 0
    orphan_tools = 0
    i = 0

    while i < len(messages):
        message = messages[i]
        if not isinstance(message, dict):
            out.append(message)
            i += 1
            continue

        role = message.get("role")
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            expected_ids = [_tool_call_id(tc) for tc in tool_calls if _tool_call_id(tc)]
            j = i + 1
            following_tools: List[Dict[str, Any]] = []
            while j < len(messages):
                candidate = messages[j]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                following_tools.append(candidate)
                j += 1

            observed_ids = [
                str(tool_message.get("tool_call_id") or "")
                for tool_message in following_tools
                if isinstance(tool_message, dict)
            ]
            expected_set = set(expected_ids)
            observed_set = set(observed_ids)
            complete = (
                bool(expected_ids)
                and len(observed_ids) == len(expected_ids)
                and observed_set == expected_set
            )
            if complete:
                out.append(message)
                out.extend(following_tools)
            else:
                missing = [tcid for tcid in expected_ids if tcid not in observed_set]
                out.append(_collapsed_tool_call_turn(message, following_tools, missing))
                repaired_turns += 1
            i = j
            continue

        if role == "tool":
            out.append({"role": "user", "content": _tool_message_history_note(message)})
            orphan_tools += 1
            i += 1
            continue

        out.append(message)
        i += 1

    if repaired_turns or orphan_tools:
        logger.warning(
            "Repaired malformed tool-call history before LLM call: "
            "collapsed_assistant_turns=%d orphan_tool_messages=%d",
            repaired_turns,
            orphan_tools,
        )
    return out


# Magic-byte signatures for correcting image MIME in data URLs.
_IMG_SIGS: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _fix_image_mime_types(messages: List[Dict[str, Any]]) -> None:
    """Correct mismatched MIME types in image data URLs in-place.

    The Anthropic API validates that the declared media_type matches the
    actual image bytes. Browsers sometimes send the wrong content_type,
    and the wrong MIME gets persisted in conversation history. This
    inspects the first bytes of base64-decoded image data and corrects
    the declared MIME if it doesn't match.
    """
    import base64 as b64mod

    fixed = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            # OpenAI format: {"type": "image_url", "image_url": {"url": "data:..."}}
            url = ""
            if block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
            if not url.startswith("data:image/"):
                continue
            # Parse data URL: data:<mime>;base64,<data>
            header_end = url.find(",")
            if header_end < 0:
                continue
            header = url[:header_end]  # "data:image/jpeg;base64"
            if ";base64" not in header:
                continue
            declared_mime = header[5:header.index(";")]  # e.g. "image/jpeg"
            # Decode just enough bytes to check signature (12 bytes)
            b64_data = url[header_end + 1:header_end + 1 + 16]  # 16 b64 chars = 12 bytes
            try:
                raw_bytes = b64mod.b64decode(b64_data + "==")  # pad for safety
            except Exception:
                continue
            for sig, actual_mime in _IMG_SIGS:
                if raw_bytes[:len(sig)] == sig:
                    if actual_mime != declared_mime:
                        block["image_url"]["url"] = (
                            f"data:{actual_mime};base64" + url[header.index(";base64") + len(";base64"):]
                        )
                        fixed += 1
                    break
    if fixed:
        logger.info("Fixed %d image data URL MIME type(s) in message history", fixed)


def _rewrap_tool_calls_to_openai(
    tcs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Coerce assistant tool_calls into OpenAI's nested function shape.

    Returns ``(new_list, changed)``. Idempotent.
    """
    out: List[Dict[str, Any]] = []
    changed = False
    for tc in tcs:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("name") and isinstance(fn.get("arguments"), str):
            out.append(tc)  # already wire-correct
            continue
        # Either flat {id, name, arguments} or nested with non-string arguments.
        if isinstance(fn, dict):
            name = fn.get("name") or ""
            args = fn.get("arguments")
        else:
            name = tc.get("name") or ""
            args = tc.get("arguments")
        args_str = args if isinstance(args, str) else json.dumps(args or {})
        out.append({
            "id": tc.get("id"),
            "type": tc.get("type") or "function",
            "function": {"name": name, "arguments": args_str},
        })
        changed = True
    return out, changed


# ---------------------------------------------------------------------------
# Tool normalization
# ---------------------------------------------------------------------------

def _normalize_tools_for_llm(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize tool schemas before sending them to providers.

    OpenAI-compatible providers commonly reject oversized tool lists. Keep the
    most relevant tools first: core/subsystem tools, then entity agents, then
    generic registered agents. Also remove duplicate tool names.
    """
    if not tools:
        return []

    def _tool_name(tool: Dict[str, Any]) -> str:
        return str((tool.get("function") or {}).get("name") or "").strip()

    def _tool_priority(name: str) -> int:
        if name.startswith("agent__"):
            return 2
        if name.startswith("entity_agent__"):
            return 1
        return 0

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    dropped_duplicates: List[str] = []
    for tool in tools:
        name = _tool_name(tool)
        if not name:
            continue
        if name in seen:
            dropped_duplicates.append(name)
            continue
        seen.add(name)
        deduped.append(tool)

    ordered = sorted(
        enumerate(deduped),
        key=lambda item: (_tool_priority(_tool_name(item[1])), item[0]),
    )
    normalized = [tool for _, tool in ordered]

    if dropped_duplicates:
        logger.warning(
            "LLM tool normalization dropped %d duplicate tool schema(s): %s",
            len(dropped_duplicates),
            dropped_duplicates[:20],
        )

    if len(normalized) > _MAX_LLM_TOOLS:
        dropped = normalized[_MAX_LLM_TOOLS:]
        dropped_names = [_tool_name(tool) for tool in dropped]
        logger.warning(
            "LLM tool normalization trimmed tool list from %d to %d; dropped=%s",
            len(normalized),
            _MAX_LLM_TOOLS,
            dropped_names[:30],
        )
        normalized = normalized[:_MAX_LLM_TOOLS]

    return normalized


# ---------------------------------------------------------------------------
# Usage extraction helpers
# ---------------------------------------------------------------------------

def _first_int_usage_value(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _cache_read_tokens_from_usage(usage: Dict[str, Any]) -> int:
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or usage.get("input_token_details") or {}
    return _first_int_usage_value(
        usage.get("cache_read_input_tokens"),
        usage.get("cache_read_tokens"),
        usage.get("prompt_cache_hit_tokens"),
        usage.get("cached_tokens"),
        prompt_details.get("cached_tokens"),
        input_details.get("cached_tokens"),
        input_details.get("cache_read"),
    )


def _cache_creation_tokens_from_usage(usage: Dict[str, Any]) -> int:
    input_details = usage.get("input_tokens_details") or usage.get("input_token_details") or {}
    return _first_int_usage_value(
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_creation_tokens"),
        usage.get("cache_write_input_tokens"),
        usage.get("cache_write_tokens"),
        input_details.get("cache_creation"),
        input_details.get("cache_write"),
    )


def _usage_from_response(data: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    """Extract token usage from LLM response data.

    Captures Anthropic-style prompt-cache split when present:
      - ``cache_read_input_tokens`` — served from cache (cheap)
      - ``cache_creation_input_tokens`` — written to cache (premium)
    Both default to 0 when the provider doesn't report them.
    """
    usage = data.get("usage") or {}
    cache_read = _cache_read_tokens_from_usage(usage)
    cache_creation = _cache_creation_tokens_from_usage(usage)
    reported_cost = (
        usage.get("cost_usd")
        if usage.get("cost_usd") is not None
        else usage.get("cost")
    )
    return {
        "prompt": usage.get("prompt_tokens", 0),
        "completion": usage.get("completion_tokens", 0),
        "total": usage.get("total_tokens", 0),
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "model": model or usage.get("model"),
        "cost_usd": reported_cost,
    }


def _compact_provider_error_text(text: str, *, limit: int = 500) -> str:
    compact = re.sub(r"\s+", " ", (text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(limit - 1, 0)].rstrip() + "…"


def _looks_like_html(text: str) -> bool:
    lowered = (text or "").lstrip().lower()
    return (
        lowered.startswith("<!doctype html")
        or lowered.startswith("<html")
        or "<html" in lowered[:500]
        or "<body" in lowered[:500]
    )


def _strip_html_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _compact_provider_error_text(text)


def _extract_html_tag_text(body: str, tag: str) -> str:
    match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", body or "")
    if not match:
        return ""
    return _strip_html_tags(match.group(1))


def _json_provider_error_text(body: str) -> str:
    try:
        parsed = json.loads(body)
    except Exception:
        return ""
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "type", "code"):
                value = error.get(key)
                if value:
                    return _compact_provider_error_text(str(value))
        if error:
            return _compact_provider_error_text(str(error))
        for key in ("message", "detail", "error_description"):
            value = parsed.get(key)
            if value:
                return _compact_provider_error_text(str(value))
    return ""


def _provider_error_body_preview(body: str, *, limit: int = 500) -> str:
    raw = (body or "").strip()
    if not raw:
        return ""

    json_error = _json_provider_error_text(raw)
    if json_error:
        return _compact_provider_error_text(json_error, limit=limit)

    if _looks_like_html(raw):
        title = _extract_html_tag_text(raw, "title")
        heading = _extract_html_tag_text(raw, "h1")
        parts: list[str] = []
        for part in (title, heading):
            if part and part not in parts:
                parts.append(part)
        if parts:
            return _compact_provider_error_text(
                "HTML error page: " + " | ".join(parts),
                limit=limit,
            )
        stripped = _strip_html_tags(raw)
        if stripped:
            return _compact_provider_error_text(f"HTML error page: {stripped}", limit=limit)
        return "HTML error page"

    return _compact_provider_error_text(raw, limit=limit)


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code if exc.response is not None else "?"
    body = ""
    try:
        body = (exc.response.text or "").strip() if exc.response is not None else ""
    except Exception:
        body = ""
    if body:
        body = _provider_error_body_preview(body, limit=1000)
        return f"HTTP {status}: {body}"
    return f"HTTP {status}: {exc}"


_REASONING_USAGE_KEY = "_reasoning_content"


def _reasoning_piece_to_text(value: Any) -> str:
    """Normalize provider-specific reasoning chunks into an opaque string.

    Some OpenAI-compatible providers (notably DeepSeek through SiliconFlow)
    require this field to be replayed verbatim on later turns. We keep it
    internal and never expose it as normal response content.
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.append(_reasoning_piece_to_text(item))
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "reasoning_content", "reasoning"):
            nested = value.get(key)
            if nested:
                return _reasoning_piece_to_text(nested)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _extract_reasoning_content(message_or_delta: Any) -> str:
    if not isinstance(message_or_delta, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        text = _reasoning_piece_to_text(message_or_delta.get(key))
        if text:
            return text
    return ""


def _attach_reasoning_to_usage(usage: Dict[str, Any], reasoning_content: str) -> None:
    reasoning = (reasoning_content or "").strip()
    if reasoning:
        usage[_REASONING_USAGE_KEY] = reasoning


def _is_reasoning_replay_error(exc: httpx.HTTPStatusError) -> bool:
    try:
        body = exc.response.text or ""
    except Exception:
        body = ""
    text = body.lower()
    return (
        exc.response is not None
        and exc.response.status_code == 400
        and "reasoning_content" in text
        and ("thinking" in text or "passed back" in text or "pass" in text)
    )


def _drop_unreplayable_reasoning_turns(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove old assistant/tool turns that lack provider-required reasoning.

    Older DB rows were saved before we preserved ``reasoning_content``. When
    routed to DeepSeek thinking mode, SiliconFlow rejects those historical
    assistant messages. As a compatibility retry, drop only those assistant
    turns and their dependent tool-result messages; user/system context stays.
    """
    out: list[dict] = []
    skipped_tool_call_ids: set[str] = set()
    dropped = 0
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and not message.get("reasoning_content"):
            for tc in message.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    skipped_tool_call_ids.add(str(tc["id"]))
            dropped += 1
            continue
        if role == "tool" and str(message.get("tool_call_id") or "") in skipped_tool_call_ids:
            dropped += 1
            continue
        out.append(message)
    return out, dropped


async def _post_chat_with_reasoning_retry(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    *,
    call_type: str,
) -> httpx.Response:
    try:
        return await _post_with_retry(url, headers, payload)
    except httpx.HTTPStatusError as exc:
        if _is_reasoning_replay_error(exc) and isinstance(payload.get("messages"), list):
            retry_messages, dropped = _drop_unreplayable_reasoning_turns(payload["messages"])
            if dropped:
                retry_payload = {**payload, "messages": retry_messages}
                logger.warning(
                    "%s retrying after dropping %d historical assistant/tool message(s) "
                    "missing reasoning_content",
                    call_type,
                    dropped,
                )
                return await _post_with_retry(url, headers, retry_payload)
        raise


def _failure_message(detail: str, *, model: Optional[str] = None) -> str:
    """Canned user-facing failure text + the actual underlying detail.

    The "Sorry, the request failed" prefix is what the supervisor and the
    chat UI key off of (TaskRunner._ERROR_MARKERS) so it must stay first.
    Everything after the blank line is the diagnostic surface — provider
    error body, HTTP status, exception type — that previously only lived
    in the worker logs.
    """
    suffix = (detail or "").strip()
    if not suffix:
        return "Sorry, the request failed. Please try again."
    if model:
        suffix = f"model={model}\n{suffix}"
    return (
        "Sorry, the request failed. Please try again.\n\n"
        "── Error detail ──\n"
        f"{suffix}"
    )


def _failure_usage(model: Optional[str], error: str) -> Dict[str, Any]:
    """EMPTY_USAGE clone with the error preserved so callers / dashboards
    that inspect ``usage`` (rather than the response text) can also see
    why the call failed."""
    out = EMPTY_USAGE.copy()
    out["model"] = model
    out["error"] = (error or "")[:1000]
    return _annotate_usage_with_billing_source(out, model=model)


def _usage_total(usage: Optional[Dict[str, Any]]) -> int:
    if not usage:
        return 0
    return int(usage.get("total") or usage.get("total_tokens") or 0)


def _is_empty_provider_response(content: Any, finish_reason: str, usage: Optional[Dict[str, Any]]) -> bool:
    return (
        not str(content or "").strip()
        and not str(finish_reason or "").strip()
        and _usage_total(usage) == 0
    )


# ---------------------------------------------------------------------------
# Anthropic native Messages API adapter
# ---------------------------------------------------------------------------

_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)


def _content_to_plain_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("text"):
                    parts.append(str(block.get("text") or ""))
            elif block is not None:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(content)


def _openai_content_to_anthropic_blocks(content: Any) -> List[Dict[str, Any]]:
    if content is None or content == "":
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            if block is not None:
                blocks.append({"type": "text", "text": str(block)})
            continue

        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_block: Dict[str, Any] = {"type": "text", "text": text}
                if isinstance(block.get("cache_control"), dict):
                    text_block["cache_control"] = block["cache_control"]
                blocks.append(text_block)
            continue

        if block_type == "image_url":
            image_url = (block.get("image_url") or {}).get("url") or ""
            match = _DATA_URL_RE.match(str(image_url))
            if match:
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": match.group(1),
                        "data": match.group(2),
                    },
                })
            elif image_url:
                blocks.append({"type": "text", "text": f"[Image URL omitted for Anthropic native call: {image_url[:200]}]"})
            continue

        if block_type in {"image", "tool_result", "tool_use"}:
            blocks.append(block)
            continue

        text = block.get("text")
        if text:
            blocks.append({"type": "text", "text": str(text)})

    return blocks


def _tool_result_content_for_anthropic(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _parse_tool_arguments(raw_args: Any) -> Dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if raw_args is None or raw_args == "":
        return {}
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw_args}
    return {"value": raw_args}


def _openai_tool_calls_to_anthropic_blocks(tool_calls: Any) -> List[Dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    normalized, _ = _rewrap_tool_calls_to_openai(tool_calls)
    blocks: List[Dict[str, Any]] = []
    for index, tc in enumerate(normalized):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"call_{index}",
            "name": name,
            "input": _parse_tool_arguments(fn.get("arguments")),
        })
    return blocks


def _anthropic_messages_from_openai(messages: List[Dict[str, Any]]) -> Tuple[str | None, List[Dict[str, Any]]]:
    system_parts: List[str] = []
    out: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            text = _content_to_plain_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not tool_call_id:
                stray_calls = message.get("tool_calls")
                if isinstance(stray_calls, list) and stray_calls:
                    tool_call_id = (stray_calls[0] or {}).get("id")
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": str(tool_call_id or "call_0"),
                "content": _tool_result_content_for_anthropic(message.get("content")),
            })
            continue

        flush_tool_results()

        if role not in {"user", "assistant"}:
            continue

        blocks = _openai_content_to_anthropic_blocks(message.get("content"))
        if role == "assistant":
            blocks.extend(_openai_tool_calls_to_anthropic_blocks(message.get("tool_calls")))
        if not blocks:
            blocks = [{"type": "text", "text": " "}]
        out.append({"role": role, "content": blocks})

    flush_tool_results()
    system = "\n\n".join(system_parts).strip() or None
    return system, out


def _openai_tools_to_anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    anthropic_tools: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        converted = {
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": schema,
        }
        cache_control = fn.get("cache_control")
        if cache_control:
            converted["cache_control"] = cache_control
        anthropic_tools.append(converted)
    return anthropic_tools


def _anthropic_tool_choice(tool_choice: Optional[Any]) -> Optional[Dict[str, Any]]:
    if tool_choice is None or tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, str):
        return {"type": "tool", "name": tool_choice}
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type in {"auto", "none", "any"}:
            return {"type": choice_type}
        if choice_type == "tool" and tool_choice.get("name"):
            return {"type": "tool", "name": str(tool_choice["name"])}
        if choice_type == "function":
            name = (tool_choice.get("function") or {}).get("name")
            if name:
                return {"type": "tool", "name": str(name)}
    return {"type": "auto"}


# Anthropic removed sampling parameters (temperature/top_p/top_k) from
# Opus 4.7 onward and the Fable/Mythos tier — sending them to the native
# /v1/messages API is a hard 400. Wire-model prefixes, so this covers
# future point releases (e.g. claude-fable-5.1) without a code change.
# OpenRouter calls are unaffected — the gateway drops unsupported params.
_ANTHROPIC_NO_SAMPLING_PREFIXES = (
    "claude-fable",
    "claude-mythos",
    "claude-opus-4-7",
    "claude-opus-4-8",
)


def _anthropic_accepts_sampling_params(wire_model: str) -> bool:
    model_lower = (wire_model or "").lower()
    return not model_lower.startswith(_ANTHROPIC_NO_SAMPLING_PREFIXES)


def _anthropic_max_tokens(model: str, requested: Optional[int]) -> int:
    default = 4096
    value = requested if requested is not None else default
    try:
        value = int(value)
    except Exception:
        value = default
    model_lower = (model or "").lower()
    if "haiku" in model_lower:
        cap = 8192
    elif "opus" in model_lower:
        cap = 32000
    else:
        cap = 64000
    return max(1, min(value, cap))


def _usage_from_anthropic_response(data: Dict[str, Any], model: Optional[str]) -> Dict[str, Any]:
    raw = data.get("usage") or {}
    prompt = int(raw.get("input_tokens") or 0)
    completion = int(raw.get("output_tokens") or 0)
    cache_read = _cache_read_tokens_from_usage(raw)
    cache_creation = _cache_creation_tokens_from_usage(raw)
    return {
        "prompt": prompt,
        "completion": completion,
        "total": prompt + completion,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "model": model or data.get("model"),
        "cost_usd": None,
    }


def _parse_anthropic_content_blocks(data: Dict[str, Any]) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            name = str(block.get("name") or "").strip()
            if not name:
                continue
            tool_calls.append({
                "id": block.get("id") or f"call_{len(tool_calls)}",
                "name": name,
                "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
            })
    return "".join(text_parts), (tool_calls or None)


def _parse_llm_response_json(response: Any, *, call_type: str) -> Any:
    """``response.json()`` with a clear error on empty/non-JSON bodies.

    Providers occasionally return an empty 200 or an HTML/error body, which
    makes httpx's ``.json()`` raise an opaque
    ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``. Convert
    that into an actionable RuntimeError naming the status and a body preview,
    so the agent loop reports something useful instead of a bare decode error."""
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raw_body = ""
        try:
            raw_body = response.text or ""
        except Exception:  # noqa: BLE001
            pass
        status = getattr(response, "status_code", "?")
        low = raw_body.lower()
        hint = ""
        if "<!doctype html" in low or "<html" in low or "cloudflare" in low:
            hint = (
                " — the endpoint returned an HTML error page (gateway down, "
                "timed out, or wrong base_url), not a model response"
            )
        body = _provider_error_body_preview(raw_body, limit=200)
        raise RuntimeError(
            f"{call_type}: LLM provider returned a non-JSON or empty response "
            f"(status={status}{hint}, body_preview={body!r})"
        ) from exc


async def _anthropic_messages_completion(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: Optional[int],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    stream_handler: _StreamHandler = None,
    call_type: str = "chat_completion",
    started_at: Optional[float] = None,
    prompt_cache: bool = False,
) -> Tuple[str, Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    started = started_at or time.time()
    wire_model = normalize_model_for_provider(model, base_url)
    system, anthropic_messages = _anthropic_messages_from_openai(messages)
    payload: Dict[str, Any] = {
        "model": wire_model,
        "messages": anthropic_messages,
        "max_tokens": _anthropic_max_tokens(wire_model, max_tokens),
    }
    if _anthropic_accepts_sampling_params(wire_model):
        payload["temperature"] = temperature
    if system:
        payload["system"] = system
    if prompt_cache:
        # Native Anthropic supports request-level automatic prompt caching.
        # This preserves caching for BYOK direct-provider calls where the
        # OpenAI-shaped payload mutated by _apply_prompt_cache is not sent.
        payload["cache_control"] = {"type": "ephemeral"}

    anthropic_tools = _openai_tools_to_anthropic_tools(tools or [])
    if anthropic_tools:
        payload["tools"] = anthropic_tools
        choice = _anthropic_tool_choice(tool_choice)
        if choice:
            payload["tool_choice"] = choice

    headers = _anthropic_request_headers(api_key)
    response = await _post_with_retry(
        f"{base_url}/messages",
        headers=headers,
        payload=payload,
    )
    data = _parse_llm_response_json(response, call_type="anthropic_messages_completion")
    content, parsed_tool_calls = _parse_anthropic_content_blocks(data)
    usage = _usage_from_anthropic_response(data, model)
    finish_reason = data.get("stop_reason") or ""
    usage["finish_reason"] = finish_reason

    if stream_handler is not None and content:
        if tools and not parsed_tool_calls:
            usage["_final_response_start_emitted"] = True
            await _emit_stream_event(stream_handler, "final_response_start", {})
        await _emit_stream_event(stream_handler, "text_delta", {"content": content})

    _record_llm_call(
        call_type=call_type,
        model=model,
        usage=usage,
        duration_ms=(time.time() - started) * 1000,
        message_count=len(messages),
        tool_count=len(anthropic_tools),
        finish_reason=finish_reason,
        success=True,
    )
    return content, parsed_tool_calls, usage


# ---------------------------------------------------------------------------
# Stream event emission
# ---------------------------------------------------------------------------

async def _emit_stream_event(
    handler: _StreamHandler,
    event_name: str,
    payload: Dict[str, Any],
) -> None:
    if handler is None:
        return
    maybe_result = handler(event_name, payload)
    if inspect.isawaitable(maybe_result):
        await maybe_result


# ---------------------------------------------------------------------------
# LLM call history tracking (contextvars-based)
# ---------------------------------------------------------------------------

def _record_llm_call(
    *,
    call_type: str,
    model: Optional[str],
    usage: Optional[Dict[str, Any]] = None,
    duration_ms: float = 0,
    message_count: int = 0,
    tool_count: int = 0,
    finish_reason: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None,
) -> None:
    usage = _annotate_usage_with_billing_source(usage, model=model)

    # ── Telemetry history (in-memory) ──
    history = _llm_call_history_var.get()
    if history is not None:
        history.append(
            {
                "call_type": call_type,
                "model": model,
                "prompt_tokens": int(usage.get("prompt", usage.get("prompt_tokens", 0)) or 0),
                "completion_tokens": int(usage.get("completion", usage.get("completion_tokens", 0)) or 0),
                "total_tokens": int(
                    usage.get("total", usage.get("total_tokens", 0))
                    or (
                        int(usage.get("prompt", usage.get("prompt_tokens", 0)) or 0)
                        + int(usage.get("completion", usage.get("completion_tokens", 0)) or 0)
                    )
                ),
                "duration_ms": float(duration_ms or 0),
                "message_count": int(message_count or 0),
                "tool_count": int(tool_count or 0),
                "finish_reason": finish_reason,
                "success": bool(success),
                "error": error,
            }
        )

    # ── Credit guard + auto-record billing if context is set ──
    billing = _billing_ctx_var.get()
    if billing and success:
        is_byok = _usage_has_byok_marker(usage) or _is_byok_call.get(False)
        provider = str(usage.get("provider") or "").strip() or provider_for_model(model)
        pricing_source = str(usage.get("pricing_source") or usage.get("llm_pricing_source") or "").strip()
        usage_for_billing = {k: v for k, v in usage.items() if k != _REASONING_USAGE_KEY}
        billing_payload = {
            **usage_for_billing,
            "model": model,
            "byok": is_byok,
            "provider": provider,
            "pricing_source": pricing_source,
            "llm_pricing_source": pricing_source,
        }
        reserved_credits = _track_in_flight_credits(billing, billing_payload, model, byok=is_byok)
        if not billing.suppress:
            _schedule_billing_record(
                billing,
                billing_payload,
                int(duration_ms),
                reserved_credits=reserved_credits,
            )


def ensure_billing_context(entity_id: str, source: str = "system", **kwargs) -> None:
    """Set billing context if not already set. Safe to call multiple times."""
    if _billing_ctx_var.get() is not None:
        return
    _billing_ctx_var.set(LLMBillingContext(entity_id=entity_id, source=source, **kwargs))


class CreditExhaustedError(Exception):
    """Raised when an entity's AI credit balance is exhausted."""
    def __init__(self, message: str, plan: str = "", limit: float = 0, current: float = 0):
        super().__init__(message)
        self.plan = plan
        self.limit = limit
        self.current = current


def _usage_to_credit_estimate(usage: dict, model: str | None) -> int:
    """Estimate billable credits from a normalized LLM usage payload."""
    if not usage:
        return 0

    try:
        reported_cost = float(usage.get("cost_usd") or 0)
    except Exception:
        reported_cost = 0.0
    if reported_cost > 0:
        try:
            import math
            from packages.core.services.billing_service import AI_MARGIN, CREDITS_PER_USD

            return max(1, math.ceil(reported_cost * (1 + AI_MARGIN) * CREDITS_PER_USD))
        except Exception:
            logger.debug("Failed to convert reported LLM cost to credits", exc_info=True)

    try:
        from packages.core.services.billing_service import tokens_to_credits

        return int(tokens_to_credits(
            int(usage.get("prompt") or usage.get("prompt_tokens") or 0),
            int(usage.get("completion") or usage.get("completion_tokens") or 0),
            model,
            pricing_source=str(usage.get("pricing_source") or usage.get("llm_pricing_source") or ""),
            provider=str(usage.get("provider") or ""),
            cache_read_tokens=int(usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation") or usage.get("cache_creation_input_tokens") or 0),
        ))
    except Exception:
        logger.debug("Failed to estimate LLM usage credits", exc_info=True)
        return 0


def _add_entity_in_flight_credits(entity_id: str | None, credits: int) -> None:
    if not entity_id or credits <= 0:
        return
    with _entity_in_flight_lock:
        _entity_in_flight_credits[entity_id] = (
            int(_entity_in_flight_credits.get(entity_id, 0) or 0) + int(credits)
        )


def _release_entity_in_flight_credits(entity_id: str | None, credits: int) -> None:
    if not entity_id or credits <= 0:
        return
    with _entity_in_flight_lock:
        remaining = int(_entity_in_flight_credits.get(entity_id, 0) or 0) - int(credits)
        if remaining > 0:
            _entity_in_flight_credits[entity_id] = remaining
        else:
            _entity_in_flight_credits.pop(entity_id, None)


def _get_entity_in_flight_credits(entity_id: str | None) -> int:
    if not entity_id:
        return 0
    with _entity_in_flight_lock:
        return int(_entity_in_flight_credits.get(entity_id, 0) or 0)


def _track_in_flight_credits(billing: LLMBillingContext, usage: dict, model: str | None, *, byok: bool) -> int:
    if byok:
        return 0
    credits = _usage_to_credit_estimate(usage, model)
    if credits > 0:
        billing.in_flight_credits += credits
        _add_entity_in_flight_credits(billing.entity_id, credits)
    return credits


def release_billing_in_flight(billing: LLMBillingContext | None = None) -> None:
    billing = billing or _billing_ctx_var.get()
    if not billing:
        return
    credits = int(getattr(billing, "in_flight_credits", 0) or 0)
    if credits <= 0:
        return
    billing.in_flight_credits = 0
    _release_entity_in_flight_credits(billing.entity_id, credits)


async def _preflight_credit_check() -> None:
    """Check credit balance before making an LLM call.

    Reads the billing context (entity_id) set by ensure_billing_context
    and queries the plan gate. Raises CreditExhaustedError if budget is
    exhausted so the caller can abort before burning provider tokens.

    Skipped when:
      - No billing context set (OSS, tests, or caller didn't set it)
      - BYOK=True / native user key (provider charges the user directly)
      - Non-cloud deployment
    """
    billing = _billing_ctx_var.get()
    if not billing:
        return
    if getattr(billing, "byok", False) or _is_byok_call.get(False):
        return

    from packages.core.constants.plans import is_cloud
    if not is_cloud():
        return

    try:
        from packages.core.database import async_session
        from packages.core.services.plan_gate import check
        async with async_session() as db:
            if billing.workspace_id:
                try:
                    from packages.core.budget import check_workspace_budget

                    ws_ok, ws_reason = await check_workspace_budget(db, billing.workspace_id)
                    if not ws_ok:
                        raise CreditExhaustedError(
                            (
                                "Workspace budget exhausted. "
                                f"{ws_reason}. Raise the workspace credit budget "
                                "or disable auto-pause to continue."
                            ),
                        )
                except CreditExhaustedError:
                    raise
                except Exception:
                    logger.warning(
                        "Workspace budget check failed for %s",
                        billing.workspace_id,
                        exc_info=True,
                    )
                    raise CreditExhaustedError(
                        "Unable to verify workspace budget right now. Please try again shortly.",
                    )

            # Auto-recharge is threshold-based, not only "already exhausted".
            # Run it before the gate so balances below the user's configured
            # threshold can top up without making the next LLM call fail first.
            try:
                from packages.core.services.billing_service import (
                    check_and_auto_recharge,
                )
                if await check_and_auto_recharge(db, billing.entity_id):
                    await db.commit()
                else:
                    await db.rollback()
            except Exception:
                logger.debug("auto-recharge threshold check failed (non-blocking)", exc_info=True)

            result = await check(db, billing.entity_id, "ai_budget_usd")
            if result.allowed and result.limit is not None:
                try:
                    from packages.core.services.billing_service import CREDITS_PER_USD

                    minimum_credits = max(_env_int("LLM_PREFLIGHT_MIN_CREDITS", 1), 0)
                    in_flight_credits = max(
                        int(getattr(billing, "in_flight_credits", 0) or 0),
                        _get_entity_in_flight_credits(billing.entity_id),
                    )
                    projected_current = float(result.current or 0) + (
                        float(in_flight_credits + minimum_credits) / max(CREDITS_PER_USD, 1)
                    )
                    if projected_current >= float(result.limit or 0):
                        try:
                            from packages.core.services.billing_service import (
                                check_and_auto_recharge,
                            )
                            if await check_and_auto_recharge(db, billing.entity_id):
                                await db.commit()
                                result = await check(db, billing.entity_id, "ai_budget_usd")
                                if result.limit is None:
                                    return
                                projected_current = float(result.current or 0) + (
                                    float(in_flight_credits + minimum_credits) / max(CREDITS_PER_USD, 1)
                                )
                        except Exception:
                            logger.debug("auto-recharge attempt for in-flight usage failed", exc_info=True)
                        if projected_current < float(result.limit or 0):
                            return
                        raise CreditExhaustedError(
                            (
                                f"You've used all {int(float(result.limit or 0) * CREDITS_PER_USD):,} "
                                f"credits on the {result.plan or 'current'} plan. "
                                "Purchase more credits or upgrade your plan to continue."
                            ),
                            plan=result.plan,
                            limit=int(float(result.limit or 0) * CREDITS_PER_USD),
                            current=int(projected_current * CREDITS_PER_USD),
                        )
                except CreditExhaustedError:
                    raise
                except Exception:
                    logger.debug("in-flight credit projection failed", exc_info=True)

            if not result.allowed:
                # Last chance: if the entity has auto-recharge enabled
                # and a Stripe customer with a default payment method,
                # try to top up before failing the call. Recharge writes
                # credits inline + a PaymentLog row; the webhook later
                # arrives and dedupes on stripe_payment_intent_id.
                recharged = False
                try:
                    from packages.core.services.billing_service import (
                        check_and_auto_recharge,
                    )
                    recharged = await check_and_auto_recharge(db, billing.entity_id)
                    if recharged:
                        await db.commit()
                except Exception:
                    logger.debug("auto-recharge attempt failed (non-blocking)", exc_info=True)

                if recharged:
                    # Re-run the gate against the freshly-credited balance.
                    # If still over, the recharge wasn't enough — fall
                    # through to raise so the caller sees a clean error.
                    result = await check(db, billing.entity_id, "ai_budget_usd")

                if not result.allowed:
                    raise CreditExhaustedError(
                        result.message,
                        plan=result.plan,
                        limit=result.limit or 0,
                        current=result.current or 0,
                    )
    except CreditExhaustedError:
        raise
    except Exception as exc:
        # Cloud mode must fail closed: if we cannot verify balance,
        # do not allow billable AI execution to continue.
        logger.warning("Pre-flight credit check failed (blocking): %s", exc, exc_info=True)
        raise CreditExhaustedError(
            "Unable to verify credit balance right now. Please try again shortly.",
        )


async def assert_credit_available(entity_id: str, *, source: str = "system", **kwargs: Any) -> None:
    """Public preflight gate for non-chat AI paths (image/video/etc.).

    Runs the same credit check used by ``chat_completion`` without
    requiring callers to manually manage billing context lifecycle.
    """
    prev_ctx = _billing_ctx_var.get()
    if prev_ctx is not None:
        await _preflight_credit_check()
        return
    token = _billing_ctx_var.set(LLMBillingContext(entity_id=entity_id, source=source, **kwargs))
    try:
        await _preflight_credit_check()
    finally:
        _billing_ctx_var.reset(token)


# Bare-id (no slash) → provider mapping. Order is irrelevant since
# prefixes are disjoint. Add new entries here when surfacing a model
# without an OpenRouter-style ``provider/model`` id.
_PROVIDER_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude", "anthropic"),                                          "anthropic"),
    (("gpt", "openai", "o1", "o3", "whisper", "tts", "text-embedding"), "openai"),
    (("gemini", "google", "palm"),                                     "google"),
    (("deepseek",),                                                    "deepseek"),
    (("mistral",),                                                     "mistral"),
    (("llama", "meta-"),                                               "meta"),
    (("mxbai", "nomic"),                                               "ollama"),
)


def provider_for_model(model: Optional[str]) -> Optional[str]:
    """Best-effort provider extraction from a model id.

    OpenRouter-style ids carry the provider as the prefix segment
    (``anthropic/claude-...``); bare ids fall back to ``_PROVIDER_PREFIXES``.
    Returns ``None`` when nothing matches — the column is nullable so we
    don't lie when we genuinely don't know.
    """
    if not model:
        return None
    m = str(model).strip().lower()
    if "/" in m:
        return m.split("/", 1)[0] or None
    for prefixes, provider in _PROVIDER_PREFIXES:
        if m.startswith(prefixes):
            return provider
    return None


def _pricing_source_for_route(routing: ResolvedLLMRouting, base_url: str) -> tuple[str, str]:
    """Return ``(route_provider, pricing_source)`` for usage billing metadata."""
    return route_pricing_source(routing, base_url)


# ── Prompt cache (Anthropic-style ``cache_control`` breakpoints) ─────
#
# Anthropic supports explicit ``cache_control`` breakpoints. Other providers
# either do automatic prefix caching (OpenAI) or reject/ignore this shape, so we
# only stamp it onto Claude-family requests.

# Providers that require explicit ``cache_control`` markers. OpenAI prompt
# caching is automatic and surfaced via ``prompt_tokens_details.cached_tokens``;
# sending Anthropic-style markers to OpenAI-compatible native endpoints can
# fail validation, so only Claude-family calls are marked explicitly here.
_CACHEABLE_PROVIDERS = {"anthropic"}

# Minimum tokens a cache breakpoint must cover before the provider
# honours it. Below this, ``cache_control`` is silently a no-op. We
# estimate tokens cheaply as char_count // 4 — within ~10% for English
# text, plenty good enough as a gate.
_CACHE_MIN_TOKENS_DEFAULT = 1024
_CACHE_MIN_TOKENS = {
    # Keep dotted OpenRouter IDs and hyphenated Anthropic-native IDs in sync.
    "claude-fable-5": 2048,
    "claude-opus-4.7": 4096,
    "claude-opus-4-7": 4096,
    "claude-opus-4.6": 4096,
    "claude-opus-4-6": 4096,
    "claude-opus-4.5": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4.5": 4096,
    "claude-haiku-4-5": 4096,
    "claude-sonnet-4.6": 2048,
    "claude-sonnet-4-6": 2048,
    "claude-haiku-3.5": 2048,
    "claude-haiku-3-5": 2048,
}


def _estimate_tokens(text: str) -> int:
    """Cheap char-length-based token estimate. Off by ~10% on English
    text — fine for a "should we even try to cache" gate."""
    return max(0, len(text or "") // 4)


def _estimate_content_tokens(content: Any) -> int:
    """Estimate tokens for OpenAI/Anthropic-ish message content."""
    if content is None:
        return 0
    if isinstance(content, str):
        return _estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += _estimate_tokens(block.get("text") or "")
                # Data URLs are billed as prompt input on several routes; count
                # a little of the URL so oversized image prompts trip the cache
                # threshold instead of disabling cache accidentally.
                image_url = (block.get("image_url") or {}).get("url")
                if image_url:
                    total += _estimate_tokens(str(image_url))
            elif block is not None:
                total += _estimate_tokens(str(block))
        return total
    return _estimate_tokens(str(content))


def _stable_message_prefix_tokens(messages: list[dict]) -> int:
    """Estimate reusable message-prefix tokens, excluding the fresh user turn.

    The system prompt and tool schemas are not enough to decide whether prompt
    caching is worthwhile: a long conversation may be cacheable even when the
    static system prompt is short. Treat the trailing user message as volatile
    and include everything before it.
    """
    if not messages:
        return 0
    end = len(messages)
    if messages[-1].get("role") == "user":
        end -= 1
    return sum(_estimate_content_tokens(m.get("content")) for m in messages[:end])


def _cache_min_for_model(model: Optional[str]) -> int:
    """Resolve the minimum cacheable token count for a model id."""
    if not model:
        return _CACHE_MIN_TOKENS_DEFAULT
    m = str(model).lower()
    for key, threshold in _CACHE_MIN_TOKENS.items():
        if key in m:
            return threshold
    return _CACHE_MIN_TOKENS_DEFAULT


def _wrap_with_cache_control(content: Any) -> list[dict]:
    """Convert a string ``content`` into the ``[{"type": "text", ...,
    "cache_control": ...}]`` shape providers need to pin a cache
    breakpoint. Already-list content gets ``cache_control`` stamped on
    the last text block instead of appending a new one — keeps
    multimodal arrays (image_url + text) intact.
    """
    cache_marker = {"type": "ephemeral"}
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": cache_marker}]
    if isinstance(content, list) and content:
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        # Stamp on the last text-bearing block so cache covers everything before it.
        for block in reversed(new_content):
            if isinstance(block, dict) and block.get("type") in (None, "text"):
                block["cache_control"] = cache_marker
                return new_content
        # No text block found — append a tiny anchor that holds the cache_control.
        new_content.append({"type": "text", "text": "", "cache_control": cache_marker})
        return new_content
    return [{"type": "text", "text": str(content or ""), "cache_control": cache_marker}]


# Map provider-prefix → OpenRouter provider name. Used to pin the
# OpenRouter route when we apply cache_control — without pinning,
# OpenRouter is free to fall through to Bedrock/Vertex which silently
# drop the cache_control field, making every call a cache miss.
_OPENROUTER_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai":    "OpenAI",
}


def _apply_prompt_cache(payload: Dict[str, Any]) -> bool:
    """Mutate ``payload`` in place to add ``cache_control`` breakpoints
    on the stable prefix (system prompt, last tool definition, last
    assistant turn). The new user message is left untouched so each turn
    extends the cached prefix rather than busting it.

    Returns True if breakpoints were actually applied (so callers can
    pin the OpenRouter route to a cache-honoring provider). False when
    no-op.

    No-op when:
      - model isn't on a known cache-supporting provider
      - the stable prefix is below the model's minimum cacheable size
        (provider would silently ignore the breakpoint anyway)

    Layout (4 breakpoints max — Anthropic limit):
      [1] system message
      [2] last tool definition (cache_control inside ``function`` per
          Anthropic's tool-shape — top-level key is ignored on tool
          objects when OpenRouter translates OpenAI-format → Anthropic)
      [3] history through turn N-1 (the assistant turn just before the
          fresh user message)
      [4] new user message — left UN-cached so it busts only its own slot
    """
    model = payload.get("model") or ""
    provider = provider_for_model(model)
    if provider not in _CACHEABLE_PROVIDERS:
        return False

    # Estimate the size of the stable prefix to decide whether the
    # provider will even honour our breakpoints.
    threshold = _cache_min_for_model(model)
    messages = payload.get("messages") or []
    prefix_tokens = _stable_message_prefix_tokens(messages)
    tools = payload.get("tools") or []
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function") or t
            prefix_tokens += _estimate_tokens(
                (fn.get("description") or "") + json.dumps(fn.get("parameters") or {})
            )

    # If the cacheable prefix is too small, the provider will ignore the
    # breakpoint. Return before mutating payload so we don't ship dead weight
    # or trigger validation on short prompts.
    if prefix_tokens < threshold:
        return False

    # ── Mark the system message ──
    sys_idx = next(
        (i for i, m in enumerate(messages) if m.get("role") == "system"),
        None,
    )
    if sys_idx is not None:
        sys_msg = messages[sys_idx]
        sys_text = sys_msg.get("content")
        sys_msg["content"] = _wrap_with_cache_control(sys_text)

    # ── Mark the last tool definition ──
    # Anthropic reads cache_control on the inner ``function`` block when
    # it gets OpenAI-format tools via OpenRouter. Putting it at the
    # outer dict level (next to ``type: function``) silently no-ops.
    if tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict):
            target = last_tool.get("function") if "function" in last_tool else last_tool
            if isinstance(target, dict):
                target["cache_control"] = {"type": "ephemeral"}

    # ── Mark the last assistant turn (history through turn N-1) ──
    # Find the last assistant message that isn't the very last message.
    # If the last message is itself an assistant (e.g. resume), skip
    # this slot — we've already covered enough with system+tools.
    if messages and messages[-1].get("role") == "user":
        for i in range(len(messages) - 2, -1, -1):
            if messages[i].get("role") == "assistant":
                messages[i]["content"] = _wrap_with_cache_control(messages[i].get("content"))
                break

    # ── Pin OpenRouter route to the model's first-party provider ──
    # Without ``order``, OpenRouter will sometimes route to Bedrock or
    # Vertex (cheaper at the moment) — both silently drop cache_control,
    # turning every call into a cache miss. Setting ``order: ["Anthropic"]``
    # asks OpenRouter to prefer Anthropic-direct so cache pass-through
    # works. Keep ``allow_fallbacks: true`` so we still complete the
    # call (uncached) when Anthropic is overloaded — better to miss
    # cache than fail the user request.
    or_provider_name = _OPENROUTER_PROVIDER_NAMES.get(provider)
    if or_provider_name:
        existing_block = payload.get("provider") or {}
        existing_order = list(existing_block.get("order") or [])
        if or_provider_name not in existing_order:
            existing_order = [or_provider_name] + existing_order
        merged = {**existing_block, "order": existing_order}
        merged.setdefault("allow_fallbacks", True)
        payload["provider"] = merged
    # One-shot diag: surface cache-control wiring so a missing cache hit
    # is visible at INFO level instead of having to inspect raw payloads.
    logger.info(
        "[cache] applied breakpoints model=%s provider=%s prefix_tokens~%d threshold=%d",
        model, or_provider_name, prefix_tokens, threshold,
    )
    return True


def fire_and_forget(coro_factory: Callable[[], Awaitable[Any]], *, label: str = "task") -> bool:
    """Schedule a fire-and-forget coroutine on the current event loop.

    Best-effort observability hook — billing writes, tool-call logs, etc.
    Failures are logged at DEBUG and never bubble. No-ops in sync
    contexts (e.g. tests with no running loop).

    Args:
      coro_factory: callable returning a fresh coroutine each invocation.
        Pass a factory not a coroutine so we can build the awaitable
        inside the task wrapper — avoids "coroutine was never awaited"
        warnings if no event loop is running.
      label: short tag used in the failure log line.
    Returns True when the task was scheduled, False when no event loop exists.
    """
    async def _wrapped():
        try:
            await coro_factory()
        except Exception:
            logger.debug("%s failed (best-effort)", label, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_wrapped())
        return True
    except RuntimeError:
        return False  # No running loop — drop the write (sync context)


def _schedule_billing_record(
    billing: LLMBillingContext,
    usage: dict,
    duration_ms: int,
    *,
    reserved_credits: int = 0,
) -> None:
    """Fire-and-forget LLM-usage write. Persists token totals + cost +
    workspace/agent/user dimensions to ``token_usage_logs`` and the
    credit ledger via ``record_llm_usage``."""
    def _release_reserved() -> None:
        if reserved_credits > 0:
            billing.in_flight_credits = max(
                0,
                int(getattr(billing, "in_flight_credits", 0) or 0) - int(reserved_credits),
            )
            _release_entity_in_flight_credits(billing.entity_id, reserved_credits)

    async def _write():
        try:
            from packages.core.database import async_session
            from packages.core.services.usage_service import record_llm_usage
            async with async_session() as db:
                await record_llm_usage(
                    db, entity_id=billing.entity_id,
                    user_id=billing.user_id, agent_id=billing.agent_id,
                    workspace_id=billing.workspace_id,
                    conversation_id=billing.conversation_id,
                    usage=usage, duration_ms=duration_ms, source=billing.source,
                )
                await db.commit()
        finally:
            _release_reserved()

    if not fire_and_forget(_write, label="auto-billing record"):
        _release_reserved()


def record_llm_response_data(
    *,
    call_type: str,
    model: Optional[str],
    data: Dict[str, Any],
    started_at: float,
    message_count: int = 0,
    tool_count: int = 0,
) -> Dict[str, Any]:
    """Record a successful external/provider-specific LLM response and return normalized usage."""
    choice = (data.get("choices") or [{}])[0]
    finish_reason = choice.get("finish_reason") or ""
    usage = _usage_from_response(data, model)
    _record_llm_call(
        call_type=call_type,
        model=model,
        usage=usage,
        duration_ms=(time.time() - started_at) * 1000,
        message_count=message_count,
        tool_count=tool_count,
        finish_reason=finish_reason,
        success=True,
    )
    return usage


def record_llm_failure(
    *,
    call_type: str,
    model: Optional[str],
    started_at: float,
    message_count: int = 0,
    tool_count: int = 0,
    error: Optional[str] = None,
) -> None:
    """Record a failed external/provider-specific LLM call."""
    _record_llm_call(
        call_type=call_type,
        model=model,
        usage=EMPTY_USAGE,
        duration_ms=(time.time() - started_at) * 1000,
        message_count=message_count,
        tool_count=tool_count,
        success=False,
        error=error,
    )


@contextlib.contextmanager
def bind_llm_call_history(history: Optional[List[Dict[str, Any]]]):
    token = _llm_call_history_var.set(history)
    try:
        yield history
    finally:
        _llm_call_history_var.reset(token)


# ---------------------------------------------------------------------------
# HTTP retry logic
# ---------------------------------------------------------------------------

async def _iter_stream_lines_with_idle_timeout(response: httpx.Response):
    """Yield streaming lines, aborting when the provider accepts stream mode but stalls."""

    timeout = get_llm_stream_idle_timeout()
    iterator = response.aiter_lines().__aiter__()
    while True:
        try:
            if timeout > 0:
                line = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            else:
                line = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"LLM streaming response stalled for {timeout:.0f}s without a chunk"
            ) from exc
        yield line

async def _post_with_retry(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> httpx.Response:
    """
    POST to `url` with automatic retry on transient provider/proxy failures,
    connect failures, and ReadTimeout.
    Waits for Retry-After seconds if the header is present, otherwise
    uses exponential back-off: 1 s, 2 s, 4 s ... capped at _MAX_DELAY.
    Retries once on ReadTimeout (common with large tool payloads).
    Raises the last exception if all retries are exhausted.
    """
    if not (headers.get("Authorization") or headers.get("x-api-key") or headers.get("X-API-Key")):
        raise LLMAuthConfigurationError("Refusing to send LLM request without an auth header.")

    # Sanitize headers — httpx encodes header values as ASCII; non-ASCII
    # chars (e.g. from a corrupted API key or env var) cause UnicodeEncodeError.
    for k, v in list(headers.items()):
        try:
            v.encode("ascii")
        except UnicodeEncodeError:
            logger.error(
                "Non-ASCII character in HTTP header %r (len=%d); "
                "stripping non-ASCII bytes. Value preview: %r",
                k, len(v), v[:30],
            )
            headers[k] = v.encode("ascii", errors="ignore").decode("ascii")

    client = await get_llm_client()
    delay = _BASE_DELAY

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.post(url, headers=headers, json=payload)
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            # RemoteProtocolError covers "peer closed connection without sending
            # complete message body" — common when an upstream proxy or the LLM
            # provider tears down the connection mid-response.  Retrying with
            # exponential back-off usually succeeds on the next attempt.
            httpx.RemoteProtocolError,
        ) as e:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "LLM request failed to reach provider (%s, attempt %d/%d); retrying in %.1fs",
                    e.__class__.__name__,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_DELAY)
                continue
            raise
        if response.status_code not in _RETRYABLE_HTTP_STATUSES:
            response.raise_for_status()
            return response

        # --- 429 / transient gateway handling ---
        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else delay

        # Cooperative backoff: if the provider is telling us to wait longer
        # than a worker should idle, raise LLMRateLimited so the goal runner
        # can release the lease and reschedule rather than pinning a worker
        # in asyncio.sleep. Only applies to 429.
        if response.status_code == 429 and wait > _COOPERATIVE_BACKOFF_SECS:
            logger.warning(
                "HTTP 429 from provider with Retry-After=%.1fs > %ds; "
                "raising LLMRateLimited for cooperative backoff",
                wait, _COOPERATIVE_BACKOFF_SECS,
            )
            await response.aclose()
            raise LLMRateLimited(wait)

        if attempt < _MAX_RETRIES:
            logger.warning(
                "HTTP %s from provider (attempt %d/%d); retrying in %.1fs",
                response.status_code, attempt + 1, _MAX_RETRIES + 1, wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, _MAX_DELAY)
        else:
            # Final attempt also failed -- raise it
            response.raise_for_status()

    # Should not be reached; satisfy type checker
    raise RuntimeError("Unexpected exit from retry loop")  # pragma: no cover


# ---------------------------------------------------------------------------
# chat_completion (plain text)
# ---------------------------------------------------------------------------

async def chat_completion(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
    stream_handler: _StreamHandler = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    """
    Call the configured LLM chat-completions API.  Returns (content, usage_dict).

    Retries on 429; on non-retryable failure returns ("", EMPTY_USAGE).
    usage_dict includes "finish_reason" key alongside token counts.
    If metadata is provided, user-level API key and base URL take priority.
    """
    requested_model = model or get_llm_model()
    resolved_model = _resolve_vision_model_if_needed(requested_model, messages)
    metadata_for_call = _route_metadata_for_resolved_model(metadata, requested_model, resolved_model)
    routing = await resolve_llm_routing_for_model(resolved_model, metadata_for_call)
    api_key = routing.api_key
    base_url = routing.base_url
    if not api_key:
        detail = _missing_llm_key_detail()
        logger.warning(detail)
        return "", _failure_usage(resolved_model, detail)

    # Pre-flight after key resolution so native-provider BYOK can bypass
    # Manor credit gates while platform OpenRouter calls remain gated.
    await _preflight_credit_check()
    try:
        _validate_llm_key_model_compatibility(api_key, base_url, resolved_model)
    except LLMAuthConfigurationError as exc:
        # Provider/key mismatch (e.g. an OpenAI key selecting a DeepSeek model).
        # This is raised before the main try below, so surface it as a clean
        # failure_usage rather than letting it propagate unhandled to callers
        # (agentic_loop only catches CreditExhaustedError).
        logger.warning("LLM key/model compatibility check failed: %s", exc)
        return "", _failure_usage(resolved_model, str(exc))

    # Strip provider prefix when calling the provider directly (not OpenRouter)
    wire_model = normalize_model_for_provider(resolved_model, base_url)

    sanitized_messages = _sanitize_messages_for_llm(messages)
    payload: Dict[str, Any] = {
        "model": wire_model,
        "messages": sanitized_messages,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    provider_block = _openrouter_provider_block(base_url)
    if provider_block:
        payload["provider"] = provider_block
    prompt_cache_applied = _apply_prompt_cache(payload)

    _call_source = routing.source or (
        "BYOK-custom"
        if _is_byok_call.get(False)
        else ("OpenRouter" if "openrouter.ai" in base_url.lower() else "custom-endpoint")
    )
    logger.info(
        "LLM call [%s]: model=%s max_tokens=%s base_url=%s",
        _call_source, resolved_model, max_tokens, base_url,
    )
    route_provider, pricing_source = _pricing_source_for_route(routing, base_url)
    route_provider_token = _llm_route_provider.set(route_provider)
    pricing_source_token = _llm_pricing_source.set(pricing_source)
    started_at = time.time()
    try:
        if _is_anthropic_messages_api(base_url):
            content, _tool_calls, usage = await _anthropic_messages_completion(
                api_key=api_key,
                base_url=base_url,
                model=resolved_model,
                messages=sanitized_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream_handler=stream_handler,
                call_type="chat_completion",
                started_at=started_at,
                prompt_cache=prompt_cache_applied,
            )
            return content, usage

        request_headers = _llm_request_headers(api_key, base_url)
        if stream_handler is not None:
            streamed_any_text = False
            try:
                client = await get_llm_client()
                stream_payload = {
                    **payload,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                content_parts: List[str] = []
                reasoning_parts: List[str] = []
                usage = EMPTY_USAGE.copy()
                usage["model"] = resolved_model
                finish_reason = ""
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers=dict(request_headers),
                    json=stream_payload,
                ) as response:
                    response.raise_for_status()
                    async for line in _iter_stream_lines_with_idle_timeout(response):
                        line = (line or "").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_line = line[5:].strip()
                        if not data_line or data_line == "[DONE]":
                            continue
                        chunk = json.loads(data_line)
                        if isinstance(chunk.get("usage"), dict):
                            usage = _usage_from_response(chunk, resolved_model)
                        choice = (chunk.get("choices") or [{}])[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        reasoning_delta = _extract_reasoning_content(delta)
                        if reasoning_delta:
                            reasoning_parts.append(reasoning_delta)
                        delta_content = delta.get("content") or ""
                        if isinstance(delta_content, list):
                            delta_content = "".join(
                                str(part.get("text") or "")
                                for part in delta_content
                                if isinstance(part, dict)
                            )
                        if delta_content:
                            content_parts.append(str(delta_content))
                            streamed_any_text = True
                            await _emit_stream_event(
                                stream_handler,
                                "text_delta",
                                {"content": str(delta_content)},
                            )
                content = "".join(content_parts)
                usage["finish_reason"] = finish_reason
                _attach_reasoning_to_usage(usage, "".join(reasoning_parts))
                if _is_empty_provider_response(content, finish_reason, usage):
                    raise RuntimeError("empty streaming LLM response (no content, finish_reason, or usage)")
                _record_llm_call(
                    call_type="chat_completion",
                    model=resolved_model,
                    usage=usage,
                    duration_ms=(time.time() - started_at) * 1000,
                    message_count=len(sanitized_messages),
                    finish_reason=finish_reason,
                    success=True,
                )
                return content, usage
            except LLMAuthConfigurationError:
                raise
            except Exception as stream_error:
                if streamed_any_text:
                    await _emit_stream_event(stream_handler, "text_reset", {})
                logger.warning(
                    "chat_completion stream path failed, falling back to buffered response: %s",
                    stream_error,
                )

        response = await _post_chat_with_reasoning_retry(
            f"{base_url}/chat/completions",
            headers=dict(request_headers),
            payload=payload,
            call_type="chat_completion",
        )
        data = _parse_llm_response_json(response, call_type="chat_completion")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content", "") or ""
        usage = _usage_from_response(data, resolved_model)
        finish_reason = choice.get("finish_reason") or ""
        usage["finish_reason"] = finish_reason
        _attach_reasoning_to_usage(usage, _extract_reasoning_content(msg))
        if _is_empty_provider_response(content, finish_reason, usage):
            detail = "Empty LLM response: provider returned no content, finish_reason, or usage."
            _record_llm_call(
                call_type="chat_completion",
                model=resolved_model,
                usage=usage,
                duration_ms=(time.time() - started_at) * 1000,
                message_count=len(sanitized_messages),
                finish_reason=finish_reason,
                success=False,
                error=detail,
            )
            return "", _failure_usage(resolved_model, detail)
        _record_llm_call(
            call_type="chat_completion",
            model=resolved_model,
            usage=usage,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            finish_reason=finish_reason,
            success=True,
        )
        return content, usage
    except Exception as e:
        # Use the same actionable detail (status + body) that
        # chat_completion_with_tools already produces for HTTP errors, so a
        # gateway 5xx/524 surfaces as "HTTP 524: ..." rather than a bare
        # "HTTPStatusError: ...".
        detail = _http_error_detail(e) if isinstance(e, httpx.HTTPStatusError) else f"{type(e).__name__}: {e}"
        _record_llm_call(
            call_type="chat_completion",
            model=resolved_model,
            usage=EMPTY_USAGE,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            success=False,
            error=detail,
        )
        logger.exception("chat_completion failed: %s", detail)
        # Return empty content (callers like supervisor prefer empty over
        # canned text) but propagate the error in the usage dict so it
        # can surface in run-detail / debug surfaces.
        return "", _failure_usage(resolved_model, detail)
    finally:
        _llm_route_provider.reset(route_provider_token)
        _llm_pricing_source.reset(pricing_source_token)


# ---------------------------------------------------------------------------
# chat_completion_with_tools (function calling)
# ---------------------------------------------------------------------------

# Default cap on completion tokens when tools are available. Function-call
# arguments count against the completion budget, but the default user-facing
# budget should stay modest so custom BYOK gateways fail fast instead of
# sitting on a very large completion request. Long internal runs can still pass
# an explicit max_tokens value or override the environment variable.
_DEFAULT_TOOL_CALL_MAX_TOKENS = int(
    # Ceiling, not a fixed cost; only billed for tokens actually generated.
    os.environ.get("LLM_TOOL_CALL_MAX_TOKENS", "8192")
)


async def chat_completion_with_tools(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    temperature: float = 0.7,
    tool_choice: Optional[Any] = None,
    model: Optional[str] = None,
    stream_handler: _StreamHandler = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Call the LLM with function/tool definitions (OpenAI-compatible function calling).

    Returns (content, tool_calls, usage):
      - content: text response (may be non-empty alongside tool_calls on some models)
      - tool_calls: list of {"id", "name", "arguments"} dicts when model wants to call tools,
                    or None if the model returned a final text answer.
      - usage: token usage dict

    Falls back to regular text completion (tool_calls=None) if:
      - The model/provider does not return tool_calls (graceful degradation)
    Retries on 429 with exponential back-off.
    """
    requested_model = model or get_llm_model()
    model = _resolve_vision_model_if_needed(requested_model, messages)
    metadata_for_call = _route_metadata_for_resolved_model(metadata, requested_model, model)
    routing = await resolve_llm_routing_for_model(model, metadata_for_call)
    api_key = routing.api_key
    base_url = routing.base_url

    if not api_key:
        detail = _missing_llm_key_detail()
        logger.warning("%s Tool calling unavailable.", detail)
        return "", None, _failure_usage(model, detail)

    # Pre-flight after key resolution so native-provider BYOK can bypass
    # Manor credit gates while platform OpenRouter calls remain gated.
    await _preflight_credit_check()
    try:
        _validate_llm_key_model_compatibility(api_key, base_url, model)
    except LLMAuthConfigurationError as exc:
        # Provider/key mismatch — surface as a clean failure_usage instead of
        # propagating unhandled (raised before the main try below).
        logger.warning("LLM key/model compatibility check failed: %s", exc)
        return "", None, _failure_usage(model, str(exc))

    wire_model = normalize_model_for_provider(model, base_url)

    sanitized_messages = _sanitize_messages_for_llm(messages)
    normalized_tools = _normalize_tools_for_llm(tools)
    effective_max_tokens = max_tokens if max_tokens is not None else _DEFAULT_TOOL_CALL_MAX_TOKENS
    payload: Dict[str, Any] = {
        "model": wire_model,
        "messages": sanitized_messages,
        "temperature": temperature,
        "tools": normalized_tools,
        "tool_choice": tool_choice if tool_choice is not None else "auto",
        "max_tokens": effective_max_tokens,
    }
    provider_block = _openrouter_provider_block(base_url)
    if provider_block:
        payload["provider"] = provider_block
    prompt_cache_applied = _apply_prompt_cache(payload)

    _call_source = routing.source or (
        "BYOK-custom"
        if _is_byok_call.get(False)
        else ("OpenRouter" if "openrouter.ai" in base_url.lower() else "custom-endpoint")
    )
    logger.info(
        "LLM call [%s]: model=%s tools=%d max_tokens=%d base_url=%s provider_routing=%s",
        _call_source, model, len(normalized_tools), effective_max_tokens,
        base_url,
        provider_block.get("ignore") if provider_block else None,
    )
    route_provider, pricing_source = _pricing_source_for_route(routing, base_url)
    route_provider_token = _llm_route_provider.set(route_provider)
    pricing_source_token = _llm_pricing_source.set(pricing_source)
    started_at = time.time()
    try:
        if _is_anthropic_messages_api(base_url):
            return await _anthropic_messages_completion(
                api_key=api_key,
                base_url=base_url,
                model=model,
                messages=sanitized_messages,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                tools=normalized_tools,
                tool_choice=tool_choice,
                stream_handler=stream_handler,
                call_type="chat_completion_with_tools",
                started_at=started_at,
                prompt_cache=prompt_cache_applied,
            )

        request_headers = _llm_request_headers(api_key, base_url)
        if stream_handler is not None:
            streamed_any_text = False
            try:
                client = await get_llm_client()
                stream_payload: Dict[str, Any] = {
                    **payload,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                usage = EMPTY_USAGE.copy()
                usage["model"] = model
                content_parts: List[str] = []
                tool_call_parts: Dict[int, Dict[str, Any]] = {}
                reasoning_parts: List[str] = []
                finish_reason = ""
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers=dict(request_headers),
                    json=stream_payload,
                ) as response:
                    response.raise_for_status()
                    async for line in _iter_stream_lines_with_idle_timeout(response):
                        line = (line or "").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_line = line[5:].strip()
                        if not data_line or data_line == "[DONE]":
                            continue
                        chunk = json.loads(data_line)
                        if isinstance(chunk.get("usage"), dict):
                            usage = _usage_from_response(chunk, model)
                        choice = (chunk.get("choices") or [{}])[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        reasoning_delta = _extract_reasoning_content(delta)
                        if reasoning_delta:
                            reasoning_parts.append(reasoning_delta)

                        streamed_tool_calls = delta.get("tool_calls") or []
                        if streamed_tool_calls:
                            for tc in streamed_tool_calls:
                                index = int(tc.get("index", 0) or 0)
                                entry = tool_call_parts.setdefault(
                                    index,
                                    {
                                        "id": tc.get("id") or f"call_{index}",
                                        "name": "",
                                        "arguments_text": "",
                                    },
                                )
                                if tc.get("id"):
                                    entry["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    entry["name"] = fn["name"]
                                if fn.get("arguments"):
                                    entry["arguments_text"] += str(fn.get("arguments") or "")

                        delta_content = delta.get("content") or ""
                        if isinstance(delta_content, list):
                            delta_content = "".join(
                                str(part.get("text") or "")
                                for part in delta_content
                                if isinstance(part, dict)
                            )
                        if delta_content:
                            text = str(delta_content)
                            content_parts.append(text)
                            streamed_any_text = True
                            await _emit_stream_event(
                                stream_handler,
                                "text_delta",
                                {"content": text},
                            )

                content = "".join(content_parts)
                usage["finish_reason"] = finish_reason
                _attach_reasoning_to_usage(usage, "".join(reasoning_parts))

                parsed_tool_calls: Optional[List[Dict[str, Any]]] = None
                if tool_call_parts:
                    parsed_tool_calls = []
                    for index in sorted(tool_call_parts.keys()):
                        entry = tool_call_parts[index]
                        raw_args = entry.get("arguments_text") or "{}"
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            arguments = {"_raw": raw_args}
                        name = str(entry.get("name") or "").strip()
                        if not name:
                            continue
                        parsed_tool_calls.append(
                            {
                                "id": entry.get("id") or f"call_{index}",
                                "name": name,
                                "arguments": arguments,
                            }
                        )

                if parsed_tool_calls:
                    logger.debug(
                        "chat_completion_with_tools(stream): %d tool call(s) returned (finish_reason=%r): %s",
                        len(parsed_tool_calls), finish_reason, [t["name"] for t in parsed_tool_calls],
                    )
                    _record_llm_call(
                        call_type="chat_completion_with_tools",
                        model=model,
                        usage=usage,
                        duration_ms=(time.time() - started_at) * 1000,
                        message_count=len(sanitized_messages),
                        tool_count=len(normalized_tools),
                        finish_reason=finish_reason,
                        success=True,
                    )
                    return content, parsed_tool_calls, usage

                if _is_empty_provider_response(content, finish_reason, usage):
                    raise RuntimeError("empty streaming LLM response (no content, finish_reason, or usage)")

                logger.info(
                    "chat_completion_with_tools(stream): text-only (no tool_calls). finish_reason=%r content_len=%d content_preview=%s",
                    finish_reason, len(content), (content or "")[:150],
                )
                _record_llm_call(
                    call_type="chat_completion_with_tools",
                    model=model,
                    usage=usage,
                    duration_ms=(time.time() - started_at) * 1000,
                    message_count=len(sanitized_messages),
                    tool_count=len(normalized_tools),
                    finish_reason=finish_reason,
                    success=True,
                )
                return content, None, usage
            except LLMAuthConfigurationError:
                raise
            except Exception as stream_error:
                if streamed_any_text:
                    await _emit_stream_event(stream_handler, "text_reset", {})
                logger.warning(
                    "chat_completion_with_tools stream path failed, falling back to buffered response: %s",
                    stream_error,
                )

        response = await _post_chat_with_reasoning_retry(
            f"{base_url}/chat/completions",
            headers=dict(request_headers),
            payload=payload,
            call_type="chat_completion_with_tools",
        )
        data = _parse_llm_response_json(response, call_type="chat_completion_with_tools")
        usage = _usage_from_response(data, model)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or ""
        usage["finish_reason"] = finish_reason
        _attach_reasoning_to_usage(usage, _extract_reasoning_content(msg))

        # Parse ALL tool calls returned by the model
        raw_tool_calls = msg.get("tool_calls")
        if raw_tool_calls:
            parsed: List[Dict[str, Any]] = []
            for tc in raw_tool_calls:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    arguments = {"_raw": raw_args}
                name = fn.get("name") or ""
                if not name:
                    continue  # skip malformed entries
                parsed.append({
                    "id": tc.get("id") or f"call_{len(parsed)}",
                    "name": name,
                    "arguments": arguments,
                })
            if parsed:
                content = msg.get("content") or ""
                logger.debug(
                    "chat_completion_with_tools: %d tool call(s) returned (finish_reason=%r): %s",
                    len(parsed), finish_reason, [t["name"] for t in parsed],
                )
                _record_llm_call(
                    call_type="chat_completion_with_tools",
                    model=model,
                    usage=usage,
                    duration_ms=(time.time() - started_at) * 1000,
                    message_count=len(sanitized_messages),
                    tool_count=len(normalized_tools),
                    finish_reason=finish_reason,
                    success=True,
                )
                return content, parsed, usage

        # Plain text response -- model finished its turn without calling tools
        content = msg.get("content") or ""
        if _is_empty_provider_response(content, finish_reason, usage):
            detail = "Empty LLM response: provider returned no content, finish_reason, tool calls, or usage."
            logger.warning("chat_completion_with_tools buffered path returned empty provider response")
            _record_llm_call(
                call_type="chat_completion_with_tools",
                model=model,
                usage=usage,
                duration_ms=(time.time() - started_at) * 1000,
                message_count=len(sanitized_messages),
                tool_count=len(normalized_tools),
                finish_reason=finish_reason,
                success=False,
                error=detail,
            )
            return _failure_message(detail, model=model), None, _failure_usage(model, detail)
        logger.info(
            "chat_completion_with_tools: text-only (no tool_calls). finish_reason=%r content_len=%d content_preview=%s",
            finish_reason, len(content), (content or "")[:150],
        )
        _record_llm_call(
            call_type="chat_completion_with_tools",
            model=model,
            usage=usage,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            tool_count=len(normalized_tools),
            finish_reason=finish_reason,
            success=True,
        )
        return content, None, usage
    except httpx.HTTPStatusError as e:
        detail = _http_error_detail(e)
        _record_llm_call(
            call_type="chat_completion_with_tools",
            model=model,
            usage=EMPTY_USAGE,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            tool_count=len(normalized_tools),
            success=False,
            error=detail,
        )

        # Fallback: try a different model if primary fails on tool-calling
        fallback = os.getenv("LLM_FALLBACK_MODEL", "").strip()
        if fallback and fallback != model and e.response.status_code == 500 and not _is_anthropic_messages_api(base_url):
            logger.warning("Primary model %s failed on tool-calling, trying fallback %s", model, fallback)
            try:
                payload["model"] = fallback
                fb_response = await _post_with_retry(
                    f"{base_url}/chat/completions",
                    headers=dict(request_headers),
                    payload=payload,
                )
                fb_data = _parse_llm_response_json(fb_response, call_type="chat_completion_with_tools(fallback)")
                fb_usage = _usage_from_response(fb_data, fallback)
                fb_usage = _annotate_usage_with_billing_source(fb_usage, model=fallback)
                fb_choice = (fb_data.get("choices") or [{}])[0]
                fb_msg = fb_choice.get("message") or {}
                fb_raw_tc = fb_msg.get("tool_calls")
                if fb_raw_tc:
                    fb_parsed = []
                    for tc in fb_raw_tc:
                        fn = tc.get("function") or {}
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            args = {"_raw": raw_args}
                        name = fn.get("name") or ""
                        if name:
                            fb_parsed.append({
                                "id": tc.get("id") or f"call_{len(fb_parsed)}",
                                "name": name,
                                "arguments": args,
                            })
                    if fb_parsed:
                        logger.info("Fallback model %s succeeded with %d tool calls", fallback, len(fb_parsed))
                        return fb_msg.get("content") or "", fb_parsed, fb_usage
                return fb_msg.get("content") or "", None, fb_usage
            except Exception as fb_err:
                logger.warning("Fallback model also failed: %s", fb_err)

        logger.exception("chat_completion_with_tools failed: %s", detail)
        full_detail = (
            f"{detail}\n"
            f"request: messages={len(sanitized_messages)} tools={len(normalized_tools)} "
            f"payload_bytes≈{len(json.dumps(payload, default=str))}"
        )
        return (
            _failure_message(full_detail, model=model),
            None,
            _failure_usage(model, full_detail),
        )

    except httpx.ReadTimeout as e:
        _record_llm_call(
            call_type="chat_completion_with_tools",
            model=model,
            usage=EMPTY_USAGE,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            tool_count=len(normalized_tools),
            success=False,
            error=str(e),
        )
        logger.exception("chat_completion_with_tools timed out: %s", e)
        return (
            "The request timed out. Please try again -- large tool sets can take longer to process.",
            None,
            _failure_usage(model, f"ReadTimeout: {e}"),
        )
    except Exception as e:
        _record_llm_call(
            call_type="chat_completion_with_tools",
            model=model,
            usage=EMPTY_USAGE,
            duration_ms=(time.time() - started_at) * 1000,
            message_count=len(sanitized_messages),
            tool_count=len(normalized_tools),
            success=False,
            error=str(e),
        )
        logger.exception("chat_completion_with_tools failed: %s", e)
        return (
            _failure_message(f"{type(e).__name__}: {e}", model=model),
            None,
            _failure_usage(model, f"{type(e).__name__}: {e}"),
        )
    finally:
        _llm_route_provider.reset(route_provider_token)
        _llm_pricing_source.reset(pricing_source_token)


# ---------------------------------------------------------------------------
# Shared HTTP client (singleton with loop-safety for Celery)
# ---------------------------------------------------------------------------

def _get_lock() -> asyncio.Lock:
    """Return the module-level lock, creating it lazily on the current running loop.

    asyncio.Lock() must be created inside a running event loop. Creating it at
    module-import time binds it to the parent-process loop, which is closed
    after a Celery fork -- causing "Event loop is closed" errors.
    """
    global _lock, _lock_loop
    current_loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not current_loop:
        _lock = asyncio.Lock()
        _lock_loop = current_loop
    return _lock


async def get_llm_client() -> httpx.AsyncClient:
    """Return the shared LLM HTTP client, creating (or recreating) it as needed.

    After a Celery fork the child inherits a stale client whose underlying
    transport is bound to the parent's closed event loop.  We detect this by
    comparing the current running loop against the loop the client was created
    on, and recreate when they differ.
    """
    global _client, _client_loop
    current_loop = asyncio.get_running_loop()
    if _client is not None and _client_loop is current_loop:
        return _client
    async with _get_lock():
        if _client is not None and _client_loop is current_loop:
            return _client
        # Close stale client from a different loop without awaiting (loop is gone)
        if _client is not None and _client_loop is not current_loop:
            try:
                await _client.aclose()
            except Exception:
                pass
        timeout = get_llm_timeout()
        _client = httpx.AsyncClient(timeout=timeout)
        _client_loop = current_loop
        logger.debug("Created shared LLM HTTP client (timeout=%s)", timeout)
        return _client


async def close_llm_client() -> None:
    """Close the shared client. Call on app shutdown."""
    global _client, _client_loop
    async with _get_lock():
        if _client is not None:
            await _client.aclose()
            _client = None
            _client_loop = None
            logger.debug("Closed shared LLM HTTP client")
