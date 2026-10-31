"""
Shared HTTP client and chat-completion helper for LLM API calls.

- get_llm_client(): reuse a single AsyncClient; closed on app shutdown.
- chat_completion(): single function for chat completions (used by orchestrator).
- Retries transient provider failures (no response, dropped stream, 429,
  5xx) within one attempt cap and time budget; see ``provider_retry``.

Ported from manor-multi-agent/apps/llm_client.py with config inlined (no
external config module dependency).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from packages.core.ai import provider_retry
from packages.core.ai.provider_retry import (
    LLMEmptyResponseError,
    LLMRequestDeadlineExceeded,
    LLMStreamProviderError,
    LLMStreamStalled,
    ProviderRetryState,
    classify_provider_failure,
    note_provider_failure,
)
from packages.core.services.model_provider_adapters import native_chat_adapter_for_base_url

from packages.core.services.model_gateway import (
    GENERIC_SK_PROVIDERS,
    ModelGatewayRoute,
    PROVIDER_HANDLERS,
    adapt_native_chat_completion_payload,
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

# Keep in sync with ``packages.core.constants.models.DEFAULTS["primary"]``.
DEFAULT_LLM_MODEL = "openai/gpt-5.6-luna"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_TIMEOUT = 300.0  # user-facing chat should fail loudly instead of hanging for 30 minutes
DEFAULT_LLM_STREAM_IDLE_TIMEOUT = 45.0
DEFAULT_WORKER_LLM_STREAM_IDLE_TIMEOUT = 180.0

LLM_MODEL_ALIASES: Dict[str, str] = {
    # Aliases only point at first-party-served models (Anthropic /
    # OpenAI / Google). Third-party-hosted SKUs (DeepSeek, Moonshot,
    # etc.) are intentionally excluded — see CATALOG inclusion rule
    # in packages/core/constants/models.py for the reasoning.
    "gpt5": "openai/gpt-5",
    "gpt6": "openai/gpt-6-astra",
    "gpt-6": "openai/gpt-6-astra",
    "chatgpt6": "openai/gpt-6-astra",
    "chatgpt-6": "openai/gpt-6-astra",
    "gpt6-astra": "openai/gpt-6-astra",
    "gpt-6-astra": "openai/gpt-6-astra",
    "gpt56": "openai/gpt-5.6-sol",
    "gpt5.6": "openai/gpt-5.6-sol",
    "gpt-5.6": "openai/gpt-5.6-sol",
    "chatgpt56": "openai/gpt-5.6-sol",
    "chatgpt5.6": "openai/gpt-5.6-sol",
    "chatgpt-5.6": "openai/gpt-5.6-sol",
    "gpt56-sol": "openai/gpt-5.6-sol",
    "gpt5.6-sol": "openai/gpt-5.6-sol",
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "gpt56-terra": "openai/gpt-5.6-terra",
    "gpt5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt56-luna": "openai/gpt-5.6-luna",
    "gpt5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
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
    "gpt4": "openai/gpt-4",
    "openai-40": "openai/gpt-4o",
    "claude": "anthropic/claude-sonnet-4",
    "claude-sonnet": "anthropic/claude-sonnet-4",
    # Bare "opus" tracks the newest Opus; pinned aliases stay on their version.
    "claude-opus": "anthropic/claude-opus-5.5",
    "opus": "anthropic/claude-opus-5.5",
    "opus-5.5": "anthropic/claude-opus-5.5",
    "opus-5-5": "anthropic/claude-opus-5.5",
    "claude-opus-5.5": "anthropic/claude-opus-5.5",
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "opus-5": "anthropic/claude-opus-5",
    "claude-opus-5": "anthropic/claude-opus-5",
    "opus-4.7": "anthropic/claude-opus-4.7",
    "claude-opus-4.7": "anthropic/claude-opus-4.7",
    "fable": "anthropic/claude-fable-5.1",
    "claude-fable": "anthropic/claude-fable-5.1",
    "fable-5.1": "anthropic/claude-fable-5.1",
    "fable-5-1": "anthropic/claude-fable-5.1",
    "claude-fable-5.1": "anthropic/claude-fable-5.1",
    "claude-fable-5-1": "anthropic/claude-fable-5.1",
    "fable-5": "anthropic/claude-fable-5",
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


_SLOW_LLM_CALL_WARN_MS = _env_int("LLM_SLOW_CALL_WARN_MS", 20000)


def _resolve_llm_model(raw: str) -> str:
    """Resolve model alias to full OpenRouter model ID."""
    key = (raw or "").strip().lower()
    if key in LLM_MODEL_ALIASES:
        return LLM_MODEL_ALIASES[key]
    return raw.strip() if raw else DEFAULT_LLM_MODEL


# Reasoning effort for calls that carry tools; ``none`` restores single-pass
# tool decisions where the model allows it. ``medium``: on a 21-case graded
# eval (3 runs, gpt-5.6-terra) it answered 20.7/21 vs 18.3 at low, with ~8%
# fewer prompt tokens (fewer wasted rounds) and the same latency.
_TOOL_REASONING_EFFORT = (os.getenv("LLM_TOOL_REASONING_EFFORT") or "medium").strip().lower()
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Thinking budget for tool calls on budget-controlled models.
_TOOL_REASONING_BUDGET_TOKENS = int(os.getenv("LLM_TOOL_REASONING_BUDGET_TOKENS") or 2048)
# Gateways that accept the unified ``reasoning`` request object.
_UNIFIED_REASONING_GATEWAYS = frozenset({"vercel", "openrouter"})


def _apply_tool_reasoning_effort(
    payload: Dict[str, Any], *, model: str, tools: List[Dict[str, Any]], base_url: str,
) -> None:
    """Make a tool call reason when its model could otherwise skip it.

    Without reasoning the model decides in a single pass whether to look
    something up; on staging it answered current-fact and own-document
    questions from memory instead of calling its tools. The effort comes from
    the gateway catalogs (:func:`tool_reasoning_effort`). A route owned by a
    provider-native adapter keeps that provider's own reasoning semantics,
    which the catalogs do not describe (Kimi K3 natively always reasons at
    max). A caller's explicit effort is never replaced. A route that rejects
    reasoning with tools is downgraded to ``none`` once and remembered
    (``_TOOL_REASONING_DOWNGRADED_ROUTES``).
    """
    if (not tools or "reasoning_effort" in payload or "reasoning" in payload
            or native_chat_adapter_for_base_url(base_url)):
        return
    effort = tool_reasoning_effort(model)
    if effort:
        payload["reasoning_effort"] = effort
        return
    if _TOOL_REASONING_EFFORT == "none" or _provider_from_base_url(base_url) not in _UNIFIED_REASONING_GATEWAYS:
        return
    # Models controlled by a switch or a thinking budget rather than effort
    # levels: the gateways translate the unified reasoning object into each
    # provider's native form (an Anthropic thinking budget, a Qwen switch).
    from packages.core.services.model_pricing import cached_reasoning_controls

    controls = cached_reasoning_controls(model)
    if controls["efforts"]:
        return  # effort levels without none: the model always reasons
    budget = controls["budget"]
    if budget:
        payload["reasoning"] = {
            "enabled": True,
            "max_tokens": max(budget["min"], min(budget["max"], _TOOL_REASONING_BUDGET_TOKENS)),
        }
    elif controls["toggle"]:
        payload["reasoning"] = {"enabled": True}


def tool_reasoning_effort(model: str | None) -> str | None:
    """The effort to request for a tool call, from what the model accepts.

    Read from the synced gateway catalogs, so a new model needs no code. Only
    a model whose reasoning can be switched off (its catalog lists ``none``)
    gets an effort: that is where a tool call may otherwise run without
    reasoning. A model that always reasons keeps its provider default (Kimi
    K3 defaults to max; requesting ``low`` would only weaken it). The
    configured effort is used when listed, else the nearest listed one. None
    when no catalog lists an effort control for the model.
    """
    from packages.core.services.model_pricing import cached_reasoning_efforts

    supported = cached_reasoning_efforts(model)
    if not supported or "none" not in supported:
        return None
    wanted = _TOOL_REASONING_EFFORT
    if wanted in supported:
        return wanted
    rank = {effort: index for index, effort in enumerate(_EFFORT_ORDER)}
    known = sorted((effort for effort in supported if effort in rank), key=rank.__getitem__)
    if not known or wanted not in rank:
        return None
    stronger = [effort for effort in known if rank[effort] >= rank[wanted]]
    return stronger[0] if stronger else known[-1]


def _apply_chat_completion_compatibility(
    payload: Dict[str, Any],
    *,
    model: str,
    tools: List[Dict[str, Any]],
) -> None:
    """Apply model-specific Chat Completions compatibility defaults.

    Tool-turn reasoning is applied after the native adapter, see
    :func:`_apply_tool_reasoning_effort`.

    GPT-6 Astra does not accept ``temperature`` on OpenAI, Vercel AI Gateway,
    or OpenRouter. Omit Manor's generic sampling default for both plain and
    tool-using calls, and map unsupported ``none`` reasoning to its lowest
    supported effort.
    """

    bare_model = str(model or "").strip().lower().split("/", 1)[-1]
    if bare_model == "gpt-6-astra":
        payload.pop("temperature", None)
        if payload.get("reasoning_effort") == "none":
            payload["reasoning_effort"] = "low"


# Wall-clock time of the first real model-output SSE frame of the call in
# flight (heartbeats excluded). Set by the stream iterator, consumed and reset
# by _record_llm_call, which every call path ends in.
_stream_first_output_at: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "_stream_first_output_at", default=None
)
_is_byok_call: contextvars.ContextVar[bool] = contextvars.ContextVar("_is_byok_call", default=False)
_llm_route_provider: contextvars.ContextVar[str] = contextvars.ContextVar("_llm_route_provider", default="")
_llm_pricing_source: contextvars.ContextVar[str] = contextvars.ContextVar("_llm_pricing_source", default="")
_official_gateway_override: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_official_gateway_override",
    default="",
)


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
        "not configured for this request, and neither the platform Vercel AI "
        "Gateway token nor the OpenRouter fallback is configured in admin or "
        "the API process environment."
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
    # Resolve historical/gateway-specific namespaces (for example
    # ``alibaba`` -> ``qwen``) through the provider registry before comparing
    # a saved model with a native BYOK endpoint.
    return provider_for_model_id(model_id) or model_id.split("/", 1)[0].strip().lower() or None


def _metadata_model_id(metadata: Optional[Dict[str, Any]]) -> str:
    if not metadata:
        return ""
    return str(metadata.get("_resolved_model") or metadata.get("model") or "").strip()


def _provider_matches_model(provider: str | None, model_provider: str | None) -> bool:
    if not provider or not model_provider:
        return True
    if provider in {"openrouter", "vercel"}:
        return True
    return provider == model_provider


def _base_url_matches_model(base_url: str, model_id: str) -> bool:
    model_provider = _model_provider_prefix(model_id)
    base_provider = _provider_from_base_url(base_url)
    return _provider_matches_model(base_provider, model_provider)


def _byok_owner_pays_bound_call(metadata: Dict[str, Any]) -> bool:
    """Whether a saved key may serve the bound billing context's calls.

    When another company pays (a client delivery trial), only keys resolved
    for that payer (``_byok_entity_id``) may be used; anything else falls
    back to the platform route, charged to the payer.
    """
    bound = _billing_ctx_var.get()
    payer = getattr(bound, "payer_entity_id", None) if bound is not None else None
    if not payer or payer == bound.entity_id:
        return True
    return metadata.get("_byok_entity_id") == payer


def _metadata_allows_native_byok(metadata: Optional[Dict[str, Any]], key: str) -> bool:
    """Return whether a saved native BYOK credential can serve this request.

    Users can switch the selected catalog model back to Manor official routing
    while a stale role-level BYOK key/base URL is still saved. In that case we
    ignore the mismatched native credential instead of routing, for example, an
    Anthropic model to a DeepSeek endpoint.
    """
    if not metadata:
        return True
    if not _byok_owner_pays_bound_call(metadata):
        logger.warning(
            "Ignoring native BYOK key because its owner does not pay for this call; using the platform route."
        )
        return False
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
    if base_provider in {"openrouter", "vercel"}:
        return

    if base_provider and model_provider and model_provider != base_provider:
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
    """Default primary model for calls that don't name one. Supports aliases.

    Mirrors the tail of ``resolve_model_for_role``: an admin default
    override outranks the env pin, so this path can't serve a different
    model than the admin portal reports. Sync callers read the last-known
    cached settings document (no DB round-trip on a hot path), so a fresh
    override converges within the ``model_settings`` TTL.
    """
    override = ""
    try:
        from packages.core.services.model_settings import get_model_settings_sync

        override = str(
            (get_model_settings_sync().get("default_overrides") or {}).get("primary") or ""
        ).strip()
    except Exception:  # settings unavailable (OSS boot, no DB yet) — use env
        override = ""
    raw = (
        override
        or os.getenv("OPENROUTER_MODEL")
        or os.getenv("LLM_MODEL")
        or DEFAULT_LLM_MODEL
    ).strip()
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
      2. Platform Vercel AI Gateway
      3. Platform OpenRouter fallback
      4. Legacy env fallback for bare/custom model ids
    """

    model_id = str(model or "").strip()
    if metadata:
        bound = _billing_ctx_var.get()
        if bound is not None and not bound.payer_entity_id and bound.workspace_id:
            # Pin the payer before choosing a key so the owner check applies.
            await billing_context_payer(bound)
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
                vercel_reason="llm.chat.vercel_gateway_key",
                openrouter_reason="llm.chat.openrouter_fallback_key",
                gateway_provider=_official_gateway_override.get("") or None,
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


async def _retry_manor_official_call_via_openrouter(
    routing: ResolvedLLMRouting,
    *,
    model: str,
    call_type: str,
    retry: Callable[[], Awaitable[Any]],
) -> Any | None:
    """Retry a failed Manor-managed Vercel call through OpenRouter once."""

    if (
        routing.provider != "vercel"
        or routing.source == "byok"
        or _official_gateway_override.get("") == "openrouter"
    ):
        return None

    logger.warning(
        "%s failed through Vercel AI Gateway for model %s; retrying through OpenRouter.",
        call_type,
        model,
    )
    token = _official_gateway_override.set("openrouter")
    try:
        return await retry()
    finally:
        _official_gateway_override.reset(token)


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
    """Return max seconds to wait for the next streaming chunk before fallback.

    User-facing chat stays snappy. Worker/planner roles allow longer grok
    thinking gaps; env still wins when set.
    """
    try:
        raw = os.getenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS") or os.getenv(
            "LLM_STREAM_IDLE_TIMEOUT"
        )
        if raw:
            return float(raw)
        role = (os.getenv("MANOR_SERVICE_ROLE") or "").strip().lower()
        if role.startswith("worker"):
            return float(DEFAULT_WORKER_LLM_STREAM_IDLE_TIMEOUT)
        return float(DEFAULT_LLM_STREAM_IDLE_TIMEOUT)
    except Exception:
        return float(DEFAULT_LLM_STREAM_IDLE_TIMEOUT)


DEFAULT_LLM_STREAM_RESPONSE_TIMEOUT = 60.0
#: Request sent -> first model output (token, reasoning or tool-call delta).
#: Hidden reasoning happens before the first token. Workers keep the bound
#: they had before (60 s to start + 180 s idle); chat gets a shorter one.
DEFAULT_LLM_STREAM_FIRST_TOKEN_TIMEOUT = 120.0
DEFAULT_WORKER_LLM_STREAM_FIRST_TOKEN_TIMEOUT = 240.0
#: Ceiling on one streaming request even while it keeps making progress.
DEFAULT_LLM_STREAM_MAX_DURATION = 900.0
#: Ceiling on one buffered (non-streaming) request, body upload included.
#: The longest successful buffered call in the 2026-10-17 eval took 327 s.
DEFAULT_LLM_REQUEST_DEADLINE = 600.0


def _env_seconds(name: str, default: float) -> float:
    """``name`` as seconds, else ``default``; ``<= 0`` disables the bound."""
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _is_worker_role() -> bool:
    return (os.getenv("MANOR_SERVICE_ROLE") or "").strip().lower().startswith("worker")


def get_llm_stream_first_token_timeout() -> float:
    """Max seconds from sending a streaming request to its first model output."""
    default = (
        DEFAULT_WORKER_LLM_STREAM_FIRST_TOKEN_TIMEOUT if _is_worker_role()
        else DEFAULT_LLM_STREAM_FIRST_TOKEN_TIMEOUT
    )
    return _env_seconds("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", default)


def get_llm_stream_max_duration() -> float:
    """Max seconds one streaming request may run, however steadily it streams."""
    return _env_seconds("LLM_STREAM_MAX_DURATION_SECONDS", default=DEFAULT_LLM_STREAM_MAX_DURATION)


def get_llm_request_deadline() -> float:
    """Max wall-clock seconds for one buffered request.

    httpx's ``LLM_TIMEOUT`` bounds each socket read and write, not the
    request: a body that uploads a little at a time, or a response that
    trickles keep-alive bytes, never trips it. One buffered fallback in the
    2026-10-17 eval ran 52 minutes before a WriteTimeout.
    """
    return _env_seconds("LLM_REQUEST_DEADLINE_SECONDS", default=DEFAULT_LLM_REQUEST_DEADLINE)


def _llm_route_label(url: str) -> str:
    """``<source>:<host>`` for logs, e.g. ``byok:apitokengate.com``."""
    try:
        host = httpx.URL(url).host or "-"
    except Exception:  # noqa: BLE001 - logging only
        host = "-"
    source = "byok" if _is_byok_call.get(False) else (_llm_route_provider.get("") or "platform")
    return f"{source}:{host}"


def get_llm_stream_response_timeout() -> float:
    """Return max seconds to wait for a streaming response to *start*.

    Distinct from the idle timeout, which only runs once chunks flow. A
    gateway that rejects a streaming request upstream can hold the request
    open without sending headers at all (seen with OpenRouter: a 400 that the
    buffered path reports in 4 s took the full 300 s read timeout to surface
    on the stream). Providers send SSE headers immediately, so a bounded wait
    here costs nothing on the happy path and turns that stall into a quick
    fallback to the buffered request.
    """
    try:
        return float(
            os.getenv("LLM_STREAM_RESPONSE_TIMEOUT_SECONDS")
            or DEFAULT_LLM_STREAM_RESPONSE_TIMEOUT
        )
    except Exception:
        return float(DEFAULT_LLM_STREAM_RESPONSE_TIMEOUT)


def _raise_on_stream_error_chunk(chunk: Any) -> None:
    """Surface an SSE error frame at once instead of waiting for the stream to end.

    OpenAI-compatible gateways send ``{"error": {...}}``; the Anthropic
    Messages API sends ``{"type": "error", "error": {...}}``. Either way the
    stream carries no usable content, and the buffered fallback would
    otherwise only run after the idle timeout or an empty-response check.
    """
    if not isinstance(chunk, dict):
        return
    error = chunk.get("error")
    # Some gateways emit an empty error field on healthy chunks; only a
    # populated error (or an explicit error event) is a failure.
    if not error and chunk.get("type") != "error":
        return
    code = None
    error_type = None
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("type") or "provider error")
        code = error.get("code")
        error_type = str(error.get("type") or "") or None
        metadata = error.get("metadata")
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        if raw:
            message = f"{message}: {_compact_provider_error_text(str(raw), limit=600)}"
    else:
        message = str(error or "provider error")
    raise LLMStreamProviderError(message, code=code, error_type=error_type)


def _should_skip_buffered_stream_fallback(stream_error: BaseException) -> bool:
    """Return true when retrying the same call as a buffered POST would only hang.

    An SSE error frame already told us the provider cannot complete this
    generation. An idle stall means the stream stayed open without model
    progress. Falling back to a buffered request then waits for Cloudflare's
    origin timeout and can honour a 524 Retry-After of minutes, which is how
    a failed turn stays open with keepalives instead of ending.
    """
    if isinstance(stream_error, (LLMStreamProviderError, LLMStreamStalled)):
        return True
    return isinstance(stream_error, TimeoutError) and "stalled" in str(stream_error)


class LLMStreamStartTimeout(TimeoutError):
    """The streaming response sent no headers within the start bound.

    Not retried as a stream: gateways have been seen holding a rejected
    streaming request open (an OpenRouter 400 took the full read timeout),
    so the buffered request is what surfaces the real status.
    """


def _defers_to_cooperative_backoff(failure: Any) -> bool:
    """A long 429 is the goal runner's to wait out (LLMRateLimited), not ours."""
    return (
        failure.status == 429
        and failure.retry_after is not None
        and failure.retry_after > _COOPERATIVE_BACKOFF_SECS
    )


def _stream_output_reached_consumer(stream_handler: Any, *, text: bool, tool_drafts: bool) -> bool:
    """Whether a failed stream showed anything to someone.

    Streamed text or a tool-call draft only counts when the handler does not
    withhold it: a handler that discards every event (streaming purely for
    its deadlines) saw nothing a retried call could repeat.
    """
    return (
        text and provider_retry.stream_event_reaches_consumer(stream_handler, "text_delta")
    ) or (
        tool_drafts and provider_retry.stream_event_reaches_consumer(stream_handler, "tool_call_draft")
    )


def _stream_failure_uses_buffered_fallback(stream_error: BaseException, *, emitted: bool) -> bool:
    """Decide what a failed streaming attempt does next.

    ``False`` re-raises: a transient failure before anything reached the
    caller is retried by the call-level loop (``_call_with_provider_retry``)
    as a fresh stream, and anything else surfaces. ``True`` keeps the
    buffered fallback for stream-specific rejections, and for failures after
    streamed text, which the caller has just withdrawn with ``text_reset``.
    """
    if emitted:
        provider_retry.mark_output_committed()
    elif provider_retry.current_retry_state() is not None and not isinstance(
        stream_error, LLMStreamStartTimeout,
    ):
        failure = classify_provider_failure(stream_error)
        if failure.transient and not _defers_to_cooperative_backoff(failure):
            return False
    return not _should_skip_buffered_stream_fallback(stream_error)


@dataclass(frozen=True)
class _StreamWatch:
    """What the stream deadlines and the stall log need about one request."""

    started_at: float  # event-loop clock when the request was sent
    model: str
    route: str


_STREAM_WATCH_ATTR = "_manor_stream_watch"


@asynccontextmanager
async def _open_llm_stream(client: httpx.AsyncClient, url: str, *, headers: Dict[str, str], json: Dict[str, Any]):
    """Open a streaming POST with a bound on how long the response may take to start.

    The yielded response carries a ``_StreamWatch`` (request start, model,
    route) so ``_iter_stream_lines_with_idle_timeout`` measures the
    first-token and total deadlines from when the request was sent.
    """
    watch = _StreamWatch(
        started_at=asyncio.get_running_loop().time(),
        model=str((json or {}).get("model") or "-"),
        route=_llm_route_label(url),
    )
    manager = client.stream("POST", url, headers=headers, json=json)
    timeout = get_llm_stream_response_timeout()
    try:
        if timeout > 0:
            response = await asyncio.wait_for(manager.__aenter__(), timeout=timeout)
        else:
            response = await manager.__aenter__()
    except asyncio.TimeoutError as exc:
        # The request was cancelled mid-flight; make sure httpx releases it.
        try:
            await manager.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 - best-effort release after a cancel
            pass
        raise LLMStreamStartTimeout(
            f"LLM streaming response did not start within {timeout:.0f}s"
        ) from exc
    try:
        setattr(response, _STREAM_WATCH_ATTR, watch)
    except (AttributeError, TypeError):  # test doubles without attributes
        pass
    try:
        yield response
    except BaseException as exc:
        if not await manager.__aexit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        await manager.__aexit__(None, None, None)


def _llm_stream_transport_enabled() -> bool:
    """Use streaming transport even when nobody is watching the tokens.

    A buffered completion holds the connection silent until the provider
    finishes generating — and any proxy between us and the provider is free
    to give up first. Cloudflare's default origin timeout is 100s, so a
    reseller gateway behind it (apitokengate.com in production) kills every
    generation longer than that with an HTTP 524, deterministically; the
    retry loop then re-fails five more times, since retrying cannot make a
    120-second generation fit a 100-second wall.

    Streaming sends the first byte within seconds, which is all such
    proxies actually require. The assembled result, billing recording, and
    the buffered-fallback-on-failure behavior are identical to the
    stream_handler path chat has always used. LLM_STREAM_TRANSPORT=0 turns
    this off (background calls then only stream when a handler asks).
    """
    return (os.getenv("LLM_STREAM_TRANSPORT") or "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _backfill_streamed_usage(
    usage: Dict[str, Any],
    *,
    messages: List[Dict[str, Any]],
    content: str,
) -> None:
    """Estimate tokens when a streamed response carried no usage chunk.

    We ask for ``stream_options.include_usage``, but an OpenAI-compatible
    gateway is free to ignore it — and silently recording zero tokens would
    make every call through such a gateway free. A ~10%-off estimate keeps
    billing honest; ``usage_estimated`` marks the row for anyone auditing.
    """
    if usage.get("prompt") or usage.get("completion"):
        return
    if not content:
        return
    prompt_tokens = sum(
        _estimate_content_tokens(m.get("content")) for m in messages
    )
    completion_tokens = _estimate_tokens(content)
    usage["prompt"] = prompt_tokens
    usage["completion"] = completion_tokens
    usage["total"] = prompt_tokens + completion_tokens
    usage["usage_estimated"] = True


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
    suppress: bool = False  # True = context is set but automatic recording is suppressed
    durable_per_call: bool = False
    """True when the caller settles each call through a durable callback."""
    byok: bool = False  # True = user's own API key; log usage for analytics but charge 0 credits
    in_flight_credits: int = 0
    """Credits consumed in this request but not necessarily visible in the
    ledger yet. Non-durable automatic billing uses this local counter while
    its write is pending."""
    payer_entity_id: Optional[str] = None
    """Who pays for calls in this context; ``entity_id`` stays the data
    owner. Pinned on first use (``billing_context_payer``) so a delivery
    phase change mid-context cannot move later calls to another payer."""
    in_flight_entity_id: Optional[str] = None
    """The entity whose shared in-flight counter holds ``in_flight_credits``."""

_billing_ctx_var: contextvars.ContextVar[Optional[LLMBillingContext]] = contextvars.ContextVar(
    "llm_billing_ctx", default=None,
)
_entity_in_flight_credits: dict[str, int] = {}
# Whether the provider call being gated was routed to a native BYOK key, set
# around the preflight of a routed call. In a sponsored trial the route, not
# the context's byok flag, decides whether the provider's credits are gated.
_routed_call_byok_var: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar(
    "llm_routed_call_byok", default=None,
)
_entity_in_flight_lock = threading.Lock()

# What the agent loop sent in the current provider call, so automatic billing
# records the same prompt-source breakdown the chat path settles explicitly.
_context_attribution_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "llm_context_attribution", default=None,
)


@contextlib.contextmanager
def llm_context_attribution_scope(attribution: Optional[dict]):
    """Attach a prompt-source breakdown to usage recorded inside this scope only."""
    token = _context_attribution_var.set(dict(attribution) if attribution else None)
    try:
        yield
    finally:
        _context_attribution_var.reset(token)


_prompt_cache_key_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_prompt_cache_key", default=None,
)
# Routes that rejected ``prompt_cache_key``; they get requests without it.
_PROMPT_CACHE_KEY_REJECTED_BASE_URLS: set[str] = set()


@contextlib.contextmanager
def llm_prompt_cache_key_scope(key: Optional[str]):
    """Route OpenAI-family calls in this scope to the same prompt cache.

    OpenAI caches prompt prefixes per machine and picks the machine from the
    first tokens plus ``prompt_cache_key``; one key per conversation stream keeps
    its turns on a warm cache.
    """
    token = _prompt_cache_key_var.set(key or None)
    try:
        yield
    finally:
        _prompt_cache_key_var.reset(token)


def _apply_prompt_cache_key(payload: Dict[str, Any], *, model: str, base_url: str) -> None:
    key = _prompt_cache_key_var.get()
    if (
        key
        and provider_for_model(model) == "openai"
        and base_url.rstrip("/") not in _PROMPT_CACHE_KEY_REJECTED_BASE_URLS
    ):
        payload["prompt_cache_key"] = key


# (base_url, model) routes that rejected ``reasoning_effort`` and accepted the
# same request without it. grok-4.5 behind a BYOK gateway answers
# reasoning_effort="none" with a bare "Upstream error: 400", which failed every
# capability-matcher call; later requests on the route leave it out.
_REASONING_EFFORT_REJECTED_ROUTES: set[tuple[str, str]] = set()
# (base_url, model) routes that rejected a reasoning effort with tools and
# accepted the same request at ``none``; later tool calls there go at ``none``.
_TOOL_REASONING_DOWNGRADED_ROUTES: set[tuple[str, str]] = set()


def _reasoning_effort_route(base_url: str, payload: Dict[str, Any]) -> tuple[str, str]:
    return base_url.rsplit("/chat/completions", 1)[0].rstrip("/"), str(payload.get("model") or "")


def _apply_reasoning_effort_route(payload: Dict[str, Any], *, base_url: str) -> None:
    route = _reasoning_effort_route(base_url, payload)
    if route in _REASONING_EFFORT_REJECTED_ROUTES:
        payload.pop("reasoning_effort", None)
        payload.pop("reasoning", None)
    elif payload.get("reasoning_effort") not in {None, "none"} and payload.get("tools") \
            and route in _TOOL_REASONING_DOWNGRADED_ROUTES:
        payload["reasoning_effort"] = "none"


def _tool_reasoning_downgrade(payload: Dict[str, Any]) -> Dict[str, Any] | None:
    """The same tool call at reasoning ``none``, when it reasoned at all."""
    if not payload.get("tools") or payload.get("reasoning_effort") in {None, "none"}:
        return None
    return {**payload, "reasoning_effort": "none"}


def _is_prompt_cache_key_rejection(exc: httpx.HTTPStatusError) -> bool:
    response = exc.response
    if response is None or response.status_code not in {400, 422}:
        return False
    try:
        body = response.text
    except Exception:
        return False
    return "prompt_cache_key" in (body or "")


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
    payer_entity_id: Optional[str] = None,
):
    """Context manager: every LLM call within this scope auto-records usage.

    ``payer_entity_id`` is an already resolved payer; without it the payer
    is resolved and pinned at the first credit check.

    Usage:
        async with llm_billing_context("entity_123", source="strategist"):
            result = await chat_completion(messages)
            # usage auto-recorded to billing
    """
    ctx = LLMBillingContext(
        entity_id=entity_id, user_id=user_id, agent_id=agent_id,
        workspace_id=workspace_id, conversation_id=conversation_id,
        source=source, byok=byok, payer_entity_id=payer_entity_id,
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

# Retry settings for transient LLM provider errors. Classification and the
# total time budget (LLM_TRANSIENT_RETRY_BUDGET_SECONDS) live in
# provider_retry; these cap one model call's attempts and shape its backoff.
_MAX_RETRIES = 5          # up to 5 retries (6 total attempts)
_BASE_DELAY  = 2.0        # backoff ceiling before the first retry (jittered)
_MAX_DELAY   = 30.0       # cap on exponential back-off

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
_MAX_LLM_TOOL_SCHEMA_CHARS = 64_000
_StreamHandler = Optional[Callable[[str, Dict[str, Any]], Any]]


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


def _model_supports_image_input(model: str) -> bool | None:
    from packages.core.constants.models import model_input_modalities

    modalities = model_input_modalities(model)
    if modalities is None:
        return None
    return "image" in modalities


def model_accepts_image_input(model: str | None) -> bool | None:
    """Whether the model a completion call for ``model`` uses accepts images.

    Resolves exactly as the completion entrypoints do (alias, then the default
    primary model when none is named). ``None`` means the model is not
    classified: callers must send the image and let the provider decide, never
    treat an unknown model as text-only.
    """
    return _model_supports_image_input(_resolve_llm_model(str(model or "").strip() or get_llm_model()))


def _log_vision_model_route(
    *,
    requested_model: str,
    canonical_model: str,
    resolved_model: str,
    reason: str,
) -> None:
    logger.info(
        "Vision model route requested_model=%s canonical_model=%s resolved_model=%s "
        "vision_fallback_reason=%s provider_changed=%s",
        requested_model,
        canonical_model,
        resolved_model,
        reason,
        _model_provider_prefix(canonical_model) != _model_provider_prefix(resolved_model),
    )


def _resolve_vision_model_if_needed(model: str, messages: List[Dict[str, Any]]) -> str:
    requested_model = str(model or "").strip()
    canonical_model = _resolve_llm_model(requested_model)
    if not _messages_include_image(messages):
        return canonical_model

    supports_image = _model_supports_image_input(canonical_model)
    if supports_image is not False:
        _log_vision_model_route(
            requested_model=requested_model,
            canonical_model=canonical_model,
            resolved_model=canonical_model,
            reason="native_image_input" if supports_image else "capability_unknown",
        )
        return canonical_model

    from packages.core.constants.models import DEFAULTS

    system_default = str(DEFAULTS.get("primary") or "").strip()
    fallback = (
        os.getenv("LLM_VISION_MODEL")
        or os.getenv("OPENROUTER_VISION_MODEL")
        or system_default
        or "openai/gpt-4o"
    ).strip()
    fallback = _resolve_llm_model(fallback)
    if _model_supports_image_input(fallback) is not True:
        fallback = "openai/gpt-4o"
    _log_vision_model_route(
        requested_model=requested_model,
        canonical_model=canonical_model,
        resolved_model=fallback,
        reason="image_input_explicitly_unsupported",
    )
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

    Google Workspace Limited Use compliance requires that Workspace-derived
    content is never routed to an inference endpoint that may collect inputs
    for model training.  ``data_collection=deny`` is therefore unconditional
    for OpenRouter calls; OpenRouter will fail closed when no compliant
    endpoint is available instead of silently sending data to one that trains.

    Some OpenRouter back-end providers (Novita is the recurring offender)
    return HTTP 400 ``invalid_request_error`` on payloads other providers
    accept — different tolerance for tool schemas, message shapes, system
    prompt size. We default to excluding Novita for this reason.

    Override the provider exclusion with ``OPENROUTER_IGNORE_PROVIDERS``
    (comma-separated). Set it to ``""`` to disable only the exclusion list;
    the data-collection restriction remains active. Only applied when the
    base URL points at openrouter.ai (no-op otherwise so non-OR setups aren't
    affected).
    """
    target_base_url = (base_url or os.getenv("LLM_BASE_URL") or DEFAULT_LLM_BASE_URL or "").lower()
    if "openrouter.ai" not in target_base_url:
        return None
    raw = os.getenv("OPENROUTER_IGNORE_PROVIDERS", "Novita").strip()
    block: Dict[str, Any] = {"data_collection": "deny"}
    if not raw:
        return block
    ignored = [p.strip() for p in raw.split(",") if p.strip()]
    if not ignored:
        return block
    block.update({"ignore": ignored, "allow_fallbacks": True})
    return block


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
        # Preserve multimodal `image_url.url` and `input_audio.data` blocks; these are intentionally binary.
        if len(path) >= 2 and path[-2:] in {("image_url", "url"), ("input_audio", "data")}:
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


def _system_reminder_text(content: Any) -> str:
    return f"<system-reminder>\n{_content_to_plain_text(content)}\n</system-reminder>"


def _portable_late_system_messages(messages: List[Dict[str, Any]], *, model: str) -> List[Dict[str, Any]]:
    """Keep system messages after the history where the provider would move them.

    The runtime sends per-turn context (and the response-language note) as a
    system message just before the current request, after the cacheable
    history. OpenAI models read a late system message in place. Other providers
    (Claude, Gemini, ... directly or through a gateway) fold every system
    message into the leading system prompt, which would put per-turn text back
    in front of the history and defeat prompt caching. For them it travels as a
    user-role ``<system-reminder>`` at the same position, as Claude Code does.
    """
    if provider_for_model(model) == "openai":
        return messages
    seen_conversation = False
    result: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        if role == "system" and seen_conversation:
            result.append({"role": "user", "content": _system_reminder_text(message.get("content"))})
            continue
        if role and role != "system":
            seen_conversation = True
        result.append(message)
    return result


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


def _audio_parts_for_base_url(messages: List[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
    """Messages with OpenAI ``input_audio`` parts in the shape this route accepts.

    OpenRouter and OpenAI-compatible provider endpoints take ``input_audio``
    as is. The Vercel AI Gateway rejects it and takes the same bytes as a
    ``file`` part with a media type instead.
    """
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    if not host.endswith("ai-gateway.vercel.sh"):
        return messages
    adapted: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not any(
            isinstance(part, dict) and part.get("type") == "input_audio" for part in content
        ):
            adapted.append(message)
            continue
        parts: List[Any] = []
        for part in content:
            audio = part.get("input_audio") if isinstance(part, dict) and part.get("type") == "input_audio" else None
            if isinstance(audio, dict):
                audio_format = str(audio.get("format") or "mp3").lower()
                media_type = "audio/mpeg" if audio_format == "mp3" else f"audio/{audio_format}"
                parts.append({"type": "file", "file": {
                    "data": audio.get("data") or "", "media_type": media_type, "filename": f"audio.{audio_format}",
                }})
            else:
                parts.append(part)
        adapted.append({**message, "content": parts})
    return adapted


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

    trimmed = _trim_llm_tools_to_schema_budget(normalized)
    if len(trimmed) < len(normalized):
        dropped_names = [_tool_name(tool) for tool in normalized[len(trimmed):]]
        logger.warning(
            "LLM tool normalization trimmed tool schemas from %d to %d chars; dropped=%s",
            _llm_tool_schema_chars(normalized),
            _MAX_LLM_TOOL_SCHEMA_CHARS,
            dropped_names[:30],
        )
        normalized = trimmed

    return normalized


def _llm_tool_schema_chars(tools: List[Dict[str, Any]]) -> int:
    return len(json.dumps(tools, ensure_ascii=False, default=str))


def _trim_llm_tools_to_schema_budget(
    tools: List[Dict[str, Any]],
    *,
    max_chars: int = _MAX_LLM_TOOL_SCHEMA_CHARS,
) -> List[Dict[str, Any]]:
    """Keep a prefix of tool schemas within a serialized size budget."""
    if not tools:
        return []
    if _llm_tool_schema_chars(tools) <= max_chars:
        return list(tools)
    kept: List[Dict[str, Any]] = []
    for tool in tools:
        candidate = kept + [tool]
        if kept and _llm_tool_schema_chars(candidate) > max_chars:
            break
        kept = candidate
        if _llm_tool_schema_chars(kept) > max_chars:
            break
    return kept


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


def _audio_tokens_from_usage(usage: Dict[str, Any]) -> tuple[int, int]:
    """Return (audio_input_tokens, audio_output_tokens) when reported.

    OpenAI audio-capable chat models report these in
    ``prompt_tokens_details.audio_tokens`` / ``completion_tokens_details.audio_tokens``
    (Responses-API shape: ``input_tokens_details.audio_tokens``). Audio tokens
    bill at their own, much higher rate than text tokens.
    """
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or usage.get("input_token_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or usage.get("output_token_details") or {}
    audio_in = _first_int_usage_value(
        usage.get("audio_input_tokens"),
        prompt_details.get("audio_tokens"),
        input_details.get("audio_tokens"),
    )
    audio_out = _first_int_usage_value(
        usage.get("audio_output_tokens"),
        completion_details.get("audio_tokens"),
        output_details.get("audio_tokens"),
    )
    return audio_in, audio_out


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
    audio_in, audio_out = _audio_tokens_from_usage(usage)
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
        "audio_in": audio_in,
        "audio_out": audio_out,
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


def _unsupported_response_format_retry(
    exc: BaseException, payload: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, str]] | None:
    """Drop a schema only when the provider explicitly rejects that capability."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code not in {400, 422}:
        return None
    response_format = payload.get("response_format") or (payload.get("output_config") or {}).get("format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    message = str(error.get("message") or "") if isinstance(error, dict) else exc.response.text
    parameter = str(error.get("param") or "") if isinstance(error, dict) else ""
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    error_type = str(error.get("type") or "") if isinstance(error, dict) else ""
    if any(term in f"{error_type} {code}".lower() for term in (
        "authentication", "authorization", "permission", "unauthorized", "access_denied", "forbidden",
        "api_key", "billing", "credit", "balance", "insufficient", "quota", "rate_limit",
        "invalid_schema", "invalid_json_schema", "schema_validation",
    )):
        return None
    parameter = parameter.lower()
    if parameter and (
        not parameter.startswith(("response_format", "json_schema", "output_config"))
        or ".schema" in parameter or "/schema" in parameter
        or (parameter.startswith("output_config.") and not parameter.startswith("output_config.format"))
    ):
        return None
    detail = f"{parameter} {message}".lower()
    if not any(name in detail for name in ("response_format", "json_schema", "output_config")):
        return None
    # Schema construction defects and account failures must remain visible.
    if any(term in detail for term in (
        "invalid schema", "invalid json schema", "malformed schema", "schema is invalid",
        "schema is too complex", "schema validation", "schema keyword", "schema constraint", "schema option",
        "schema feature", "oneof", "anyof", "allof", "$ref",
        "unsupported keyword", "unsupported property", "unsupported constraint",
        "additionalproperties", "minlength", "maxlength", "minimum", "maximum", "minitems", "maxitems",
        "authentication", "unauthorized", "api key", "insufficient", "quota", "credit", "balance",
    )):
        return None
    if not any(term in detail for term in (
        "not supported", "unsupported", "does not support", "unknown parameter", "unrecognized parameter",
    )) and code not in {"unsupported_parameter", "unknown_parameter", "unsupported_value"}:
        return None
    retry_payload = dict(payload)
    retry_payload.pop("response_format", None)
    if "output_config" in retry_payload:
        output_config = {key: value for key, value in retry_payload["output_config"].items() if key != "format"}
        if output_config:
            retry_payload["output_config"] = output_config
        else:
            retry_payload.pop("output_config")
    diagnostic = {"type": "json_schema", "reason": "unsupported_format"}
    logger.warning("Provider explicitly rejected json_schema; retrying once with prompt and host validation")
    return retry_payload, diagnostic


async def _post_with_response_format_retry(
    url: str, headers: Dict[str, str], payload: Dict[str, Any],
) -> httpx.Response:
    try:
        return await _post_with_retry(url, headers=headers, payload=payload)
    except httpx.HTTPStatusError as exc:
        fallback = _unsupported_response_format_retry(exc, payload)
        if fallback is None:
            raise
        retry_payload, diagnostic = fallback
        try:
            response = await _post_with_retry(url, headers=headers, payload=retry_payload)
        except Exception as retry_error:
            retry_error.response_format_fallback = diagnostic
            raise
        response.extensions["response_format_fallback"] = diagnostic
        return response


def _attach_response_format_fallback(
    usage: Dict[str, Any], response: httpx.Response,
    stream_fallback: Dict[str, str] | None = None,
) -> None:
    diagnostic = stream_fallback or getattr(response, "extensions", {}).get("response_format_fallback")
    if diagnostic:
        usage["response_format_fallback"] = diagnostic


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
        format_retry = _unsupported_response_format_retry(exc, payload)
        if format_retry is not None:
            retry_payload, diagnostic = format_retry
            try:
                response = await _post_chat_with_reasoning_retry(
                    url, headers, retry_payload, call_type=call_type,
                )
            except Exception as retry_error:
                retry_error.response_format_fallback = diagnostic
                raise
            response.extensions["response_format_fallback"] = diagnostic
            return response
        if "prompt_cache_key" in payload and _is_prompt_cache_key_rejection(exc):
            base_url = url.rsplit("/chat/completions", 1)[0].rstrip("/")
            _PROMPT_CACHE_KEY_REJECTED_BASE_URLS.add(base_url)
            logger.warning("%s: route rejected prompt_cache_key; retrying without it (%s)", call_type, base_url)
            retry_payload = {key: value for key, value in payload.items() if key != "prompt_cache_key"}
            return await _post_chat_with_reasoning_retry(url, headers, retry_payload, call_type=call_type)
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
                return await _post_with_response_format_retry(url, headers, retry_payload)
        if (
            ("reasoning_effort" in payload or "reasoning" in payload)
            and exc.response is not None
            and exc.response.status_code in {400, 422}
        ):
            downgraded = _tool_reasoning_downgrade(payload)
            # Only an error about reasoning is worth a second shape; any other
            # rejection keeps the single existing retry below.
            if downgraded is not None and "reasoning" in (exc.response.text or "").lower():
                # A route that refuses reasoning with tools may still accept
                # the single-pass call; remember it so later calls go straight
                # there. A model without ``none`` falls through to omitting it.
                try:
                    response = await _post_with_retry(url, headers, downgraded)
                except httpx.HTTPStatusError:
                    response = None
                if response is not None:
                    route = _reasoning_effort_route(url, payload)
                    _TOOL_REASONING_DOWNGRADED_ROUTES.add(route)
                    logger.warning(
                        "%s: %s rejected reasoning_effort=%s with tools; using none (%s)",
                        call_type, route[1], payload.get("reasoning_effort"), route[0],
                    )
                    return response
            # Remembered only when the same request succeeds without it: a
            # model that requires the parameter (gpt-5.6 tool calls) failing
            # for another reason must not lose it on later calls.
            retry_payload = {
                key: value for key, value in payload.items() if key not in {"reasoning_effort", "reasoning"}
            }
            try:
                response = await _post_chat_with_reasoning_retry(url, headers, retry_payload, call_type=call_type)
            except httpx.HTTPStatusError as retry_error:
                diagnostic = getattr(retry_error, "response_format_fallback", None)
                if (isinstance(diagnostic, dict) and diagnostic.get("type") == "json_schema"
                        and diagnostic.get("reason") == "unsupported_format"):
                    exc.response_format_fallback = {"type": "json_schema", "reason": "unsupported_format"}
                raise exc
            route = _reasoning_effort_route(url, payload)
            _REASONING_EFFORT_REJECTED_ROUTES.add(route)
            logger.warning("%s: %s rejected reasoning_effort; retried without it (%s)", call_type, route[1], route[0])
            return response
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


def _failure_usage(
    model: Optional[str],
    error: str,
    *,
    error_code: str | None = None,
    public_detail: str | None = None,
) -> Dict[str, Any]:
    """EMPTY_USAGE clone with the error preserved so callers / dashboards
    that inspect ``usage`` (rather than the response text) can also see
    why the call failed."""
    out = EMPTY_USAGE.copy()
    out["model"] = model
    out["error"] = (error or "")[:1000]
    if error_code:
        out["error_code"] = error_code
    if public_detail:
        out["error_public_detail"] = public_detail
    return _annotate_usage_with_billing_source(out, model=model)


def _byok_public_failure_detail(
    routing: Any, exc: BaseException, *, model: Optional[str],
) -> str:
    """Structured, secret-free description of a failure on the user's own route.

    Empty for every non-BYOK route. The shape is a contract with
    ``output_policy._PUBLIC_BYOK_DETAIL_RE``: status (or "no response"), the
    provider host the owner configured, and the model id — never the provider
    body, never request diagnostics.
    """
    if getattr(routing, "source", None) != "byok":
        return ""
    host = urlparse(str(getattr(routing, "base_url", "") or "")).hostname or "provider"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        what = f"HTTP {exc.response.status_code}"
    else:
        what = "no response"
    return f"{what} from {host} (model {model})"


def _byok_failure_usage(
    routing: Any, exc: BaseException, *, model: Optional[str], detail: str,
) -> Dict[str, Any]:
    """``_failure_usage`` that types the failure when the route is the user's own."""
    public_detail = _byok_public_failure_detail(routing, exc, model=model)
    if not public_detail:
        usage = _failure_usage(model, detail)
    else:
        # Lazy: the runtime package pulls this module in at import time.
        from packages.core.ai.runtime.output_policy import BYOK_PROVIDER_ERROR_CODE

        usage = _failure_usage(
            model, detail, error_code=BYOK_PROVIDER_ERROR_CODE, public_detail=public_detail,
        )
    diagnostic = getattr(exc, "response_format_fallback", None)
    if diagnostic:
        usage["response_format_fallback"] = diagnostic
    return usage


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

        if block_type == "input_audio":
            blocks.append({"type": "text", "text": "[Audio omitted: this model cannot listen to audio.]"})
            continue

        text = block.get("text")
        if text:
            blocks.append({"type": "text", "text": str(text)})

    return blocks


def _tool_result_content_for_anthropic(content: Any) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    ):
        # Text parts (a prompt-cache breakpoint wraps a tool result this way)
        # stay content blocks, keeping their cache_control.
        blocks = _openai_content_to_anthropic_blocks(content)
        return blocks or ""
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

    seen_conversation = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system" and seen_conversation:
            # Late system context stays where it is (see
            # _portable_late_system_messages) instead of joining ``system``.
            flush_tool_results()
            out.append({"role": "user", "content": [{"type": "text", "text": _system_reminder_text(message.get("content"))}]})
            continue
        if role == "system":
            text = _content_to_plain_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        seen_conversation = True

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
# Opus 4.7 onward (incl. the Opus 5 line) and the Fable/Mythos tier — sending them to the native
# /v1/messages API is a hard 400. Wire-model prefixes, so this covers
# future point releases (e.g. claude-fable-5.1) without a code change.
# OpenRouter calls are unaffected — the gateway drops unsupported params.
_ANTHROPIC_NO_SAMPLING_PREFIXES = (
    "claude-fable",
    "claude-mythos",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
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
                # Missing provider IDs need a new transport identity, never
                # a per-response index that could borrow an earlier receipt.
                "id": block.get("id") or f"call_{uuid4().hex}",
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
    response_format: Optional[Dict[str, Any]] = None,
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
    if response_format is not None and response_format.get("type") == "json_schema":
        # Native Messages uses output_config.format, without the OpenAI
        # schema name/strict wrapper. JSON object mode has no native equivalent.
        payload["output_config"] = {"format": {
            "type": "json_schema",
            "schema": response_format["json_schema"]["schema"],
        }}
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
    response_format_fallback = None
    if stream_handler is not None:
        streamed_any_text = False
        tool_drafts = _ToolCallDraftEmitter(stream_handler)
        try:
            client = await get_llm_client()
            content_parts: List[str] = []
            tool_call_parts: Dict[int, Dict[str, Any]] = {}
            raw_usage: Dict[str, Any] = {}
            finish_reason = ""
            response_model = wire_model
            async with _open_llm_stream(
                client,
                f"{base_url}/messages",
                headers=dict(headers),
                json={**payload, "stream": True},
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
                    _raise_on_stream_error_chunk(chunk)
                    event_type = chunk.get("type")
                    if event_type == "message_start":
                        message = chunk.get("message") or {}
                        if isinstance(message, dict):
                            response_model = str(message.get("model") or response_model)
                            if isinstance(message.get("usage"), dict):
                                raw_usage.update(message["usage"])
                        continue
                    if event_type == "content_block_start":
                        index = int(chunk.get("index", 0) or 0)
                        block = chunk.get("content_block") or {}
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = str(block.get("text") or "")
                            if text:
                                content_parts.append(text)
                                streamed_any_text = True
                                await _emit_stream_event(
                                    stream_handler,
                                    "text_delta",
                                    {"content": text},
                                )
                        elif block.get("type") == "tool_use":
                            tool_call_parts[index] = {
                                "id": block.get("id") or f"call_{uuid4().hex}",
                                "name": str(block.get("name") or ""),
                                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                "arguments_text": "",
                            }
                            await tool_drafts.update(index, tool_call_parts[index])
                        continue
                    if event_type == "content_block_delta":
                        index = int(chunk.get("index", 0) or 0)
                        delta = chunk.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        if delta.get("type") == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                content_parts.append(text)
                                streamed_any_text = True
                                await _emit_stream_event(
                                    stream_handler,
                                    "text_delta",
                                    {"content": text},
                                )
                        elif delta.get("type") == "input_json_delta":
                            entry = tool_call_parts.setdefault(
                                index,
                                {
                                    "id": f"call_{uuid4().hex}",
                                    "name": "",
                                    "input": {},
                                    "arguments_text": "",
                                },
                            )
                            entry["arguments_text"] += str(delta.get("partial_json") or "")
                            await tool_drafts.update(index, entry)
                        continue
                    if event_type == "message_delta":
                        delta = chunk.get("delta") or {}
                        if isinstance(delta, dict):
                            finish_reason = str(delta.get("stop_reason") or finish_reason)
                        if isinstance(chunk.get("usage"), dict):
                            raw_usage.update(chunk["usage"])

            content = "".join(content_parts)
            parsed_tool_calls: Optional[List[Dict[str, Any]]] = None
            if tool_call_parts:
                parsed_tool_calls = []
                for index in sorted(tool_call_parts):
                    entry = tool_call_parts[index]
                    name = str(entry.get("name") or "").strip()
                    if not name:
                        continue
                    raw_arguments = str(entry.get("arguments_text") or "")
                    if raw_arguments:
                        try:
                            arguments = json.loads(raw_arguments)
                        except json.JSONDecodeError:
                            arguments = {"_raw": raw_arguments}
                    else:
                        arguments = entry.get("input") or {}
                    parsed_tool_calls.append({
                        "id": entry.get("id") or f"call_{uuid4().hex}",
                        "name": name,
                        "arguments": arguments,
                    })
                if not parsed_tool_calls:
                    parsed_tool_calls = None

            usage = _usage_from_anthropic_response(
                {"usage": raw_usage, "model": response_model},
                model,
            )
            usage["finish_reason"] = finish_reason
            streamed_completion_text = content + "".join(
                str(entry.get("arguments_text") or "")
                for entry in tool_call_parts.values()
            )
            _backfill_streamed_usage(
                usage,
                messages=messages,
                content=streamed_completion_text,
            )
            if _is_empty_provider_response(content, finish_reason, usage):
                raise LLMEmptyResponseError(
                    "empty Anthropic streaming response (no content, finish_reason, or usage)"
                )
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
        except LLMAuthConfigurationError:
            raise
        except Exception as stream_error:
            if streamed_any_text:
                await _emit_stream_event(stream_handler, "text_reset", {})
            format_retry = _unsupported_response_format_retry(stream_error, payload)
            if format_retry is not None:
                payload, response_format_fallback = format_retry
            emitted = _stream_output_reached_consumer(
                stream_handler, text=streamed_any_text, tool_drafts=tool_drafts.emitted,
            )
            if not _stream_failure_uses_buffered_fallback(
                stream_error, emitted=emitted,
            ):
                logger.warning(
                    "%s Anthropic stream path failed, skipping buffered fallback: %s",
                    call_type,
                    stream_error,
                )
                raise
            logger.warning(
                "%s Anthropic stream path failed, falling back to buffered response: %s",
                call_type,
                stream_error,
            )

    try:
        response = await _post_with_response_format_retry(
            f"{base_url}/messages",
            headers=headers,
            payload=payload,
        )
    except Exception as buffered_error:
        if response_format_fallback:
            buffered_error.response_format_fallback = response_format_fallback
        raise
    data = _parse_llm_response_json(response, call_type="anthropic_messages_completion")
    content, parsed_tool_calls = _parse_anthropic_content_blocks(data)
    usage = _usage_from_anthropic_response(data, model)
    _attach_response_format_fallback(usage, response, response_format_fallback)
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


# A tool call the model is still writing, shown live: the first event as soon
# as its name streams in, then at most every 160 characters or half second.
_TOOL_DRAFT_MIN_CHARS = 160
_TOOL_DRAFT_MIN_SECONDS = 0.5
_TOOL_DRAFT_MAX_CHARS = 65536


class _ToolCallDraftEmitter:
    """Publish ``tool_call_draft`` for tool calls still streaming in.

    A draft is display only: the call has not run, and the host still
    validates, authorizes and executes the finished call as before.
    """

    def __init__(self, handler: _StreamHandler) -> None:
        self._handler = handler
        self._sent: Dict[int, Tuple[int, float]] = {}

    @property
    def emitted(self) -> bool:
        """A draft reached the caller; a retried call would show a second one."""
        return bool(self._sent)

    async def update(self, index: int, entry: Dict[str, Any]) -> None:
        if self._handler is None or not entry.get("name"):
            return
        text = str(entry.get("arguments_text") or "")
        now = time.monotonic()
        last = self._sent.get(index)
        if last is not None and (
            len(text) - last[0] < _TOOL_DRAFT_MIN_CHARS and now - last[1] < _TOOL_DRAFT_MIN_SECONDS
        ):
            return
        self._sent[index] = (len(text), now)
        try:
            await _emit_stream_event(self._handler, "tool_call_draft", {
                "index": index, "id": str(entry.get("id") or ""), "name": str(entry["name"]),
                "arguments_text": text[:_TOOL_DRAFT_MAX_CHARS],
            })
        except Exception:
            logger.debug("tool_call_draft emit failed", exc_info=True)


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

    # One line per finished call so a slow turn can be attributed to a specific
    # call (classifier vs. main loop) from logs alone. Streamed calls also carry
    # time-to-first-token (request start -> first model output frame) and the
    # streaming phase (first output -> completion).
    first_output_at = _stream_first_output_at.get()
    _stream_first_output_at.set(None)
    total_ms = float(duration_ms or 0)
    first_token_ms: Optional[int] = None
    stream_ms: Optional[int] = None
    if first_output_at is not None:
        started_wall = time.time() - total_ms / 1000
        first_token_ms = max(0, int((first_output_at - started_wall) * 1000))
        stream_ms = max(0, int(total_ms) - first_token_ms)
    logger.log(
        logging.WARNING if total_ms >= _SLOW_LLM_CALL_WARN_MS else logging.INFO,
        "LLM call done [%s]: model=%s duration_ms=%d first_token_ms=%s stream_ms=%s "
        "success=%s finish=%s prompt_tokens=%d completion_tokens=%d messages=%d tools=%d%s",
        call_type,
        model,
        int(total_ms),
        "-" if first_token_ms is None else first_token_ms,
        "-" if stream_ms is None else stream_ms,
        bool(success),
        finish_reason,
        int(usage.get("prompt", usage.get("prompt_tokens", 0)) or 0),
        int(usage.get("completion", usage.get("completion_tokens", 0)) or 0),
        int(message_count or 0),
        int(tool_count or 0),
        f" error={_compact_provider_error_text(error, limit=120)}" if error else "",
    )

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
                "first_token_ms": first_token_ms,
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
        reserved_credits = 0
        if not billing.durable_per_call:
            reserved_credits = _track_in_flight_credits(
                billing,
                billing_payload,
                model,
                byok=is_byok,
            )
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
    """Raised when an entity's AI credit balance is exhausted.

    ``kind`` is ``"trial_credit"`` when the provider paying for a client
    delivery trial cannot cover the call; the customer cannot fix that by
    buying credits or upgrading.
    """
    def __init__(
        self, message: str, plan: str = "", limit: float = 0, current: float = 0,
        *, kind: str = "credit",
    ):
        super().__init__(message)
        self.plan = plan
        self.limit = limit
        self.current = current
        self.kind = kind


def _trial_credit_exhausted(**kwargs: Any) -> CreditExhaustedError:
    from packages.core.services.credit_reservations import TRIAL_CREDIT_MESSAGE

    return CreditExhaustedError(TRIAL_CREDIT_MESSAGE, kind="trial_credit", **kwargs)


async def billing_context_payer(ctx: LLMBillingContext, *, db: Any | None = None) -> str:
    """Return who pays for calls in ``ctx``, pinning it on the context.

    ``db`` is reused for the lookup when given; otherwise a short session is
    opened. A trial whose provider is gone raises a ``trial_credit``
    ``CreditExhaustedError`` rather than billing the customer.
    """
    if ctx.payer_entity_id:
        return ctx.payer_entity_id
    from packages.core.services.billing_payer import (
        TrialSponsorUnavailable,
        resolve_billing_payer,
        trial_provider_pays_enabled,
    )

    if not ctx.workspace_id or not trial_provider_pays_enabled():
        return ctx.entity_id
    try:
        if db is not None:
            payer = await resolve_billing_payer(db, entity_id=ctx.entity_id, workspace_id=ctx.workspace_id)
        else:
            from packages.core.database import async_session

            async with async_session() as own:
                payer = await resolve_billing_payer(
                    own, entity_id=ctx.entity_id, workspace_id=ctx.workspace_id,
                )
    except TrialSponsorUnavailable as exc:
        raise _trial_credit_exhausted() from exc
    ctx.payer_entity_id = payer.entity_id
    return payer.entity_id


class CreditCheckUnavailableError(RuntimeError):
    """Raised when the fail-closed credit gate cannot verify current state."""


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
            audio_input_tokens=int(usage.get("audio_in") or usage.get("audio_input_tokens") or 0),
            audio_output_tokens=int(usage.get("audio_out") or usage.get("audio_output_tokens") or 0),
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


def _in_flight_entity_id(billing: LLMBillingContext) -> str:
    # Credits already counted stay on the entity they were counted for.
    return billing.in_flight_entity_id or billing.payer_entity_id or billing.entity_id


def _track_in_flight_credits(billing: LLMBillingContext, usage: dict, model: str | None, *, byok: bool) -> int:
    if byok:
        return 0
    credits = _usage_to_credit_estimate(usage, model)
    if credits > 0:
        billing.in_flight_entity_id = _in_flight_entity_id(billing)
        billing.in_flight_credits += credits
        _add_entity_in_flight_credits(billing.in_flight_entity_id, credits)
    return credits


def release_billing_in_flight(billing: LLMBillingContext | None = None) -> None:
    billing = billing or _billing_ctx_var.get()
    if not billing:
        return
    credits = int(getattr(billing, "in_flight_credits", 0) or 0)
    if credits <= 0:
        return
    in_flight_entity_id = _in_flight_entity_id(billing)
    billing.in_flight_credits = 0
    billing.in_flight_entity_id = None
    _release_entity_in_flight_credits(in_flight_entity_id, credits)


def _log_credit_check_failure(message: str, *args: Any, exc: BaseException) -> None:
    """Separate database admission/availability outages from genuine credit
    errors in logs; both still fail closed with credit_check_unavailable."""
    from packages.core.database_runtime import DatabaseFailureKind, database_request_id

    kind = DatabaseFailureKind.from_exception(exc)
    if kind is DatabaseFailureKind.BUSY:
        logger.warning(
            "credit_check_database_busy request_id=%s exception_type=%s: " + message,
            database_request_id.get(), type(exc).__name__, *args, exc_info=True,
        )
    elif kind is not None:
        logger.warning(
            "credit_check_database_unavailable request_id=%s exception_type=%s: " + message,
            database_request_id.get(), type(exc).__name__, *args, exc_info=True,
        )
    else:
        logger.warning(message, *args, exc_info=True)


async def _preflight_credit_check(*, session_factory: Any | None = None) -> None:
    """Check credit balance before making an LLM call.

    Reads the billing context (entity_id) set by ensure_billing_context
    and queries the plan gate. Raises CreditExhaustedError if budget is
    exhausted so the caller can abort before burning provider tokens.

    Tenant credit checks are skipped for BYOK and non-cloud deployments, but
    an operator-configured Workspace USD budget is always enforced.
    """
    billing = _billing_ctx_var.get()
    if not billing:
        return

    from packages.core.constants.plans import ai_credit_limits_enabled, is_cloud
    routed_byok = _routed_call_byok_var.get()
    pinned_payer = getattr(billing, "payer_entity_id", None)
    sponsored_context = bool(pinned_payer) and pinned_payer != billing.entity_id
    byok_call = (
        routed_byok
        # A sponsored trial follows the route: the provider's byok flag must
        # not exempt a call that fell back to the platform at its expense.
        if routed_byok is not None and sponsored_context
        else getattr(billing, "byok", False) or _is_byok_call.get(False)
    )
    tenant_credit_check = (
        is_cloud()
        and not byok_call
        and ai_credit_limits_enabled()
    )
    if not billing.workspace_id and not tenant_credit_check:
        return

    try:
        if session_factory is None:
            from packages.core.database import async_session

            session_factory = async_session
        from packages.core.services.plan_gate import check
        async with session_factory() as db:
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
                except Exception as exc:
                    _log_credit_check_failure(
                        "Workspace budget check failed for %s",
                        billing.workspace_id,
                        exc=exc,
                    )
                    raise CreditCheckUnavailableError(
                        "credit_check_unavailable: Unable to verify workspace "
                        "budget right now. Please try again shortly."
                    ) from exc

            if not tenant_credit_check:
                return

            # Credits, auto-recharge and the in-flight projection are the
            # payer's: the provider during a client delivery trial.
            payer_id = await billing_context_payer(billing, db=db)
            sponsored = payer_id != billing.entity_id

            # Auto-recharge is threshold-based, not only "already exhausted".
            # Run it before the gate so balances below the user's configured
            # threshold can top up without making the next LLM call fail first.
            try:
                from packages.core.services.billing_service import (
                    check_and_auto_recharge,
                )
                await check_and_auto_recharge(db, payer_id)
                # Commit either way: a failed attempt's PaymentLog drives the
                # retry cooldown, and rolling it back made every preflight
                # call Stripe again with the declining card.
                await db.commit()
            except Exception:
                logger.debug("auto-recharge threshold check failed (non-blocking)", exc_info=True)

            result = await check(db, payer_id, "ai_budget_usd")
            if result.allowed and result.limit is not None:
                try:
                    from packages.core.services.billing_service import CREDITS_PER_USD

                    minimum_credits = max(_env_int("LLM_PREFLIGHT_MIN_CREDITS", 1), 0)
                    in_flight_credits = max(
                        int(getattr(billing, "in_flight_credits", 0) or 0),
                        _get_entity_in_flight_credits(payer_id),
                    )
                    projected_current = float(result.current or 0) + (
                        float(in_flight_credits + minimum_credits) / max(CREDITS_PER_USD, 1)
                    )
                    if projected_current >= float(result.limit or 0):
                        try:
                            from packages.core.services.billing_service import (
                                check_and_auto_recharge,
                            )
                            if await check_and_auto_recharge(db, payer_id):
                                await db.commit()
                                result = await check(db, payer_id, "ai_budget_usd")
                                if result.limit is None:
                                    return
                                projected_current = float(result.current or 0) + (
                                    float(in_flight_credits + minimum_credits) / max(CREDITS_PER_USD, 1)
                                )
                        except Exception:
                            logger.debug("auto-recharge attempt for in-flight usage failed", exc_info=True)
                        if projected_current < float(result.limit or 0):
                            return
                        if sponsored:
                            raise _trial_credit_exhausted(
                                plan=result.plan,
                                limit=int(float(result.limit or 0) * CREDITS_PER_USD),
                                current=int(projected_current * CREDITS_PER_USD),
                            )
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
                    recharged = await check_and_auto_recharge(db, payer_id)
                    if recharged:
                        await db.commit()
                except Exception:
                    logger.debug("auto-recharge attempt failed (non-blocking)", exc_info=True)

                if recharged:
                    # Re-run the gate against the freshly-credited balance.
                    # If still over, the recharge wasn't enough — fall
                    # through to raise so the caller sees a clean error.
                    result = await check(db, payer_id, "ai_budget_usd")

                if not result.allowed:
                    if sponsored:
                        raise _trial_credit_exhausted(
                            plan=result.plan, limit=result.limit or 0, current=result.current or 0,
                        )
                    raise CreditExhaustedError(
                        result.message,
                        plan=result.plan,
                        limit=result.limit or 0,
                        current=result.current or 0,
                    )
    except (CreditExhaustedError, CreditCheckUnavailableError):
        raise
    except Exception as exc:
        # Cloud mode must fail closed: if we cannot verify balance,
        # do not allow billable AI execution to continue.
        _log_credit_check_failure("Pre-flight credit check failed (blocking): %s", exc, exc=exc)
        raise CreditCheckUnavailableError(
            "credit_check_unavailable: Unable to verify credit balance right "
            "now. Please try again shortly."
        ) from exc


async def _preflight_routed_call(routing: "ResolvedLLMRouting") -> None:
    token = _routed_call_byok_var.set(routing.source == "byok")
    try:
        await _preflight_credit_check()
    finally:
        _routed_call_byok_var.reset(token)


async def assert_credit_available(
    entity_id: str,
    *,
    source: str = "system",
    session_factory: Any | None = None,
    **kwargs: Any,
) -> None:
    """Public preflight gate for non-chat AI paths (image/video/etc.).

    Runs the same credit check used by ``chat_completion`` without
    requiring callers to manually manage billing context lifecycle.
    """
    prev_ctx = _billing_ctx_var.get()
    if prev_ctx is not None:
        await _preflight_credit_check(session_factory=session_factory)
        return
    token = _billing_ctx_var.set(LLMBillingContext(entity_id=entity_id, source=source, **kwargs))
    try:
        await _preflight_credit_check(session_factory=session_factory)
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
    "claude-opus-5.5": 4096,
    "claude-opus-5-5": 4096,
    "claude-opus-5": 4096,
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


def _history_breakpoint_index(messages: list[dict]) -> int | None:
    """The message whose end is the reusable history prefix, if any.

    Skips the system message (marked separately) and messages without text,
    which cannot carry a cache marker.
    """
    if not messages:
        return None
    end = len(messages) - 1
    if messages[-1].get("role") == "user":
        end -= 1
    for index in range(end, -1, -1):
        message = messages[index]
        if message.get("role") == "system":
            return None
        if _estimate_content_tokens(message.get("content")) > 0:
            return index
    return None


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
      [3] history: through the turn before a fresh user message, or
          through the latest tool results inside an agent loop
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
        from packages.core.ai.runtime.workspace_operation_skills import (
            WORKSPACE_OPERATION_GUIDANCE_END, WORKSPACE_OPERATION_GUIDANCE_START,
        )

        # Workspace guides precede actor state. Pin that public prefix rather
        # than a changing Workspace/user snapshot so requests can reuse it.
        if (
            isinstance(sys_text, str)
            and sys_text.startswith(WORKSPACE_OPERATION_GUIDANCE_START)
            and WORKSPACE_OPERATION_GUIDANCE_END in sys_text
        ):
            prefix, suffix = sys_text.split(WORKSPACE_OPERATION_GUIDANCE_END, 1)
            blocks = _wrap_with_cache_control(prefix + WORKSPACE_OPERATION_GUIDANCE_END)
            if suffix:
                blocks.append({"type": "text", "text": suffix})
            sys_msg["content"] = blocks
        else:
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

    # ── Moving breakpoint over the conversation history ──
    # A fresh user message is the only volatile tail: cache through the
    # turn before it. Inside an agent loop the request ends with the latest
    # tool results, which every later round of the run repeats verbatim, so
    # the breakpoint goes on that last message and the next round reads the
    # whole history from cache instead of recomputing it.
    history_idx = _history_breakpoint_index(messages)
    if history_idx is not None:
        messages[history_idx]["content"] = _wrap_with_cache_control(messages[history_idx].get("content"))

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
    in_flight_entity_id = _in_flight_entity_id(billing)

    def _release_reserved() -> None:
        if reserved_credits > 0:
            billing.in_flight_credits = max(
                0,
                int(getattr(billing, "in_flight_credits", 0) or 0) - int(reserved_credits),
            )
            _release_entity_in_flight_credits(in_flight_entity_id, reserved_credits)

    attribution = _context_attribution_var.get()
    if attribution and not any(
        key in usage for key in ("context_attribution", "context_attribution_total", "context_breakdown")
    ):
        usage = {**usage, "context_attribution": attribution}

    async def _write():
        try:
            from packages.core.database import async_session
            from packages.core.services.usage_service import record_llm_usage
            if billing.payer_entity_id is None:
                # A context whose credit check was skipped (BYOK, no Workspace
                # budget) pins its payer on first use, so later calls agree.
                try:
                    await billing_context_payer(billing)
                except CreditExhaustedError:
                    pass  # record_llm_usage refuses and logs the charge
            async with async_session() as db:
                await record_llm_usage(
                    db, entity_id=billing.entity_id,
                    user_id=billing.user_id, agent_id=billing.agent_id,
                    workspace_id=billing.workspace_id,
                    conversation_id=billing.conversation_id,
                    usage=usage, duration_ms=duration_ms, source=billing.source,
                    # None resolves the Workspace's payer at write time.
                    payer_entity_id=billing.payer_entity_id,
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

_SSE_HEARTBEAT_EVENT_TYPES = frozenset(
    {"heartbeat", "keep_alive", "keep-alive", "keepalive", "ping"}
)


def _sse_payload_has_model_progress(decoded: Dict[str, Any]) -> bool:
    """Return true when an SSE JSON payload contains model output or a terminal signal."""

    if decoded.get("error") or str(decoded.get("type") or "").strip().lower() == "error":
        return True
    usage = decoded.get("usage")
    if isinstance(usage, dict) and usage:
        return True
    event_type = str(decoded.get("type") or decoded.get("event") or "").strip().lower()
    if event_type and event_type not in _SSE_HEARTBEAT_EVENT_TYPES:
        return True
    choices = decoded.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta")
        if not isinstance(delta, dict) or not delta:
            message = choice.get("message")
            delta = message if isinstance(message, dict) else {}
        if not delta:
            continue
        content = delta.get("content")
        if content:
            return True
        if delta.get("tool_calls") or delta.get("function_call"):
            return True
        if _extract_reasoning_content(delta):
            return True
    return False


def _sse_data_line_is_heartbeat(line: str) -> bool:
    """Return true for keep-alives and empty OpenAI chunks that are not model progress.

    Gateways often drip ``data: {"choices":[{"delta":{}}]}`` while the origin is
    stuck. Those frames reset a naive idle timer even though no token arrived.
    """

    stripped = str(line or "").strip()
    if not stripped.startswith("data:"):
        return False
    payload = stripped[5:].strip()
    if payload.lower() in _SSE_HEARTBEAT_EVENT_TYPES:
        return True
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(decoded, dict):
        return False
    event_type = str(decoded.get("type") or decoded.get("event") or "").strip().lower()
    if event_type in _SSE_HEARTBEAT_EVENT_TYPES:
        return True
    return not _sse_payload_has_model_progress(decoded)


async def _iter_stream_lines_with_idle_timeout(response: httpx.Response):
    """Yield meaningful SSE data lines; raise ``LLMStreamStalled`` when progress stops.

    Three deadlines, each disabled by a value ``<= 0``:

    - first token: from sending the request to the first model output
      (``LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS``);
    - idle: between model outputs (``LLM_STREAM_IDLE_TIMEOUT_SECONDS``);
    - max duration: the whole stream (``LLM_STREAM_MAX_DURATION_SECONDS``).

    Providers and gateways commonly keep an otherwise stalled stream alive with
    blank lines, ``: keep-alive`` comments or empty deltas. Those heartbeats
    are not model progress and reset none of the deadlines.
    """

    loop = asyncio.get_running_loop()
    watch = getattr(response, _STREAM_WATCH_ATTR, None)
    started = watch.started_at if isinstance(watch, _StreamWatch) else loop.time()
    first_token_timeout = get_llm_stream_first_token_timeout()
    idle_timeout = get_llm_stream_idle_timeout()
    max_duration = get_llm_stream_max_duration()
    ceiling = started + max_duration if max_duration > 0 else None
    progress_at: Optional[float] = None
    iterator = response.aiter_lines().__aiter__()

    def _next_deadline() -> Tuple[Optional[float], str]:
        if progress_at is None:
            candidates = [(started + first_token_timeout, "first_token")] if first_token_timeout > 0 else []
        else:
            candidates = [(progress_at + idle_timeout, "idle")] if idle_timeout > 0 else []
        if ceiling is not None:
            candidates.append((ceiling, "max_duration"))
        if not candidates:
            return None, ""
        return min(candidates, key=lambda item: item[0])

    while True:
        deadline, phase = _next_deadline()
        try:
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            else:
                line = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as exc:
            raise _stream_stalled(response, watch, phase, loop.time() - started, {
                "first_token": first_token_timeout,
                "idle": idle_timeout,
                "max_duration": max_duration,
            }[phase]) from exc
        stripped = (line or "").strip()
        if (
            not stripped.startswith("data:")
            or not stripped[5:].strip()
            or _sse_data_line_is_heartbeat(stripped)
        ):
            continue
        if _stream_first_output_at.get() is None:
            _stream_first_output_at.set(time.time())
        yield line
        progress_at = loop.time()


#: "stalled" stays in every message: older callers match on it.
_STALL_MESSAGES = {
    "first_token": "LLM streaming response stalled: no model output within {limit:.0f}s of the request",
    "idle": "LLM streaming response stalled for {limit:.0f}s without a chunk",
    "max_duration": "LLM streaming response stalled: still open past its {limit:.0f}s ceiling",
}


def _stream_stalled(response: Any, watch: Any, phase: str, elapsed: float, limit: float) -> LLMStreamStalled:
    """Build the stall error and log what is known about the stalled stream."""
    received = getattr(response, "num_bytes_downloaded", None)
    bytes_received = received if isinstance(received, int) else None
    model = watch.model if isinstance(watch, _StreamWatch) else "-"
    route = watch.route if isinstance(watch, _StreamWatch) else "-"
    logger.warning(
        "LLM stream stalled [stalled_stream]: phase=%s model=%s route=%s elapsed_s=%.1f "
        "limit_s=%.0f bytes_received=%s",
        phase, model, route, elapsed, limit, "-" if bytes_received is None else bytes_received,
    )
    return LLMStreamStalled(
        _STALL_MESSAGES[phase].format(limit=limit),
        phase=phase,
        elapsed=elapsed,
        bytes_received=bytes_received,
    )


async def _post_within_deadline(
    client: Any, url: str, *, headers: Dict[str, str], payload: Dict[str, Any],
) -> httpx.Response:
    """One buffered POST, bounded as a whole by ``get_llm_request_deadline``."""
    deadline = get_llm_request_deadline()
    if deadline <= 0:
        return await client.post(url, headers=headers, json=payload)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await asyncio.wait_for(client.post(url, headers=headers, json=payload), timeout=deadline)
    except asyncio.TimeoutError as exc:
        elapsed = loop.time() - started
        logger.warning(
            "LLM request deadline exceeded [request_deadline]: model=%s route=%s elapsed_s=%.1f limit_s=%.0f",
            payload.get("model") or "-", _llm_route_label(url), elapsed, deadline,
        )
        raise LLMRequestDeadlineExceeded(
            f"LLM request did not complete within {deadline:.0f}s",
            request=httpx.Request("POST", url),
        ) from exc


async def _post_with_retry(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> httpx.Response:
    """
    POST to `url`, retrying transient provider/proxy failures.

    What counts as transient, the backoff (exponential with jitter, honouring
    a short 429 Retry-After), the attempt cap and the time budget all come
    from ``provider_retry``, and are shared with the call-level stream retry
    of the same model call, so the two loops never multiply. A long 429
    raises LLMRateLimited for cooperative backoff; Cloudflare 524 is retried
    once, since a generation past the proxy's limit fails the same way again.
    Raises the last exception once the failure is permanent or the budget is
    spent.
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
    state = provider_retry.current_retry_state() or _new_provider_retry_state()

    while True:
        try:
            response = await _post_within_deadline(client, url, headers=headers, payload=payload)
        except httpx.TransportError as e:
            failure = classify_provider_failure(e)
            delay = state.retry_delay(failure)
            if delay is None:
                raise
            logger.warning(
                "LLM request failed to reach provider (%s, attempt %d/%d); retrying in %.1fs",
                failure.reason or e.__class__.__name__,
                state.attempts,
                state.max_attempts,
                delay,
            )
            await state.wait(delay, failure)
            continue
        if response.status_code not in provider_retry.TRANSIENT_HTTP_STATUSES:
            response.raise_for_status()
            return response
        failure = provider_retry.classify_response(response)

        # Cooperative backoff: if the provider is telling us to wait longer
        # than a worker should idle, raise LLMRateLimited so the goal runner
        # can release the lease and reschedule rather than pinning a worker
        # in asyncio.sleep. Only applies to 429.
        if _defers_to_cooperative_backoff(failure):
            logger.warning(
                "HTTP 429 from provider with Retry-After=%.1fs > %ds; "
                "raising LLMRateLimited for cooperative backoff",
                failure.retry_after, _COOPERATIVE_BACKOFF_SECS,
            )
            await response.aclose()
            raise LLMRateLimited(failure.retry_after)

        delay = state.retry_delay(failure)
        if delay is None:
            response.raise_for_status()
        logger.warning(
            "HTTP %s from provider (attempt %d/%d); retrying in %.1fs",
            response.status_code, state.attempts, state.max_attempts, delay,
        )
        await state.wait(delay, failure)


def _new_provider_retry_state() -> ProviderRetryState:
    return ProviderRetryState(
        max_attempts=max(_MAX_RETRIES, 0) + 1,
        base_delay=_BASE_DELAY,
        max_delay=_MAX_DELAY,
    )


def _provider_failure_public_label(failure: Any) -> str:
    return failure.label if failure is not None else "no response"


async def _call_with_provider_retry(attempt: Callable[[], Awaitable[Any]], *, call_type: str) -> Any:
    """Run one model call, re-running it after a transient provider failure.

    ``attempt`` returns the call's normal result tuple, whose last element is
    the usage dict; a failed attempt records its classified failure through
    ``note_provider_failure`` before returning. The loop re-runs an attempt
    only when that failure is transient, nothing from it reached the caller,
    and the shared budget allows; otherwise the failure is finalised so the
    caller can tell an outage (retried, then given up) from a configuration
    problem.
    """
    state = _new_provider_retry_state()
    token = provider_retry.enter_retry_state(state)
    try:
        while True:
            state.failure = None
            result = await attempt()
            usage = result[-1] if isinstance(result, tuple) and result else None
            failure = state.failure
            if failure is None or not isinstance(usage, dict) or not usage.get("error"):
                return result
            delay = None if state.output_committed else state.retry_delay(failure)
            if delay is None:
                return _finalize_provider_failure(result, state, failure)
            logger.warning(
                "%s transient provider failure (%s%s, attempt %d/%d); retrying the call in %.1fs",
                call_type, _provider_failure_public_label(failure),
                f", {failure.reason}" if failure.reason else "", state.attempts,
                state.max_attempts, delay,
            )
            await state.wait(delay, failure)
    finally:
        provider_retry.exit_retry_state(token)


def _finalize_provider_failure(result: Tuple[Any, ...], state: ProviderRetryState, failure: Any) -> Tuple[Any, ...]:
    """Type a transient failure the caller is about to see.

    Permanent failures keep their existing contract (BYOK owners are told to
    check their settings). A transient one says so, with the attempt count
    and the time spent, and never sends the owner to their settings.
    """
    if not failure.transient:
        return result
    usage = dict(result[-1])
    summary = state.summary()
    usage["error_transient"] = True
    usage["error_attempts"] = state.attempts
    usage["error_elapsed_seconds"] = int(round(state.elapsed_seconds()))
    usage["error"] = f"{usage.get('error') or ''}\n[transient provider failure {summary}]".strip()[:1200]
    if usage.get("error_code") == _byok_provider_error_code():
        detail = str(usage.get("error_public_detail") or "").strip()
        if detail:
            usage["error_public_detail"] = f"{detail} {summary}"
    elif not usage.get("error_code"):
        from packages.core.ai.runtime.output_policy import LLM_PROVIDER_UNAVAILABLE_ERROR_CODE

        usage["error_code"] = LLM_PROVIDER_UNAVAILABLE_ERROR_CODE
        usage["error_public_detail"] = f"{_provider_failure_public_label(failure)} {summary}"
    logger.warning(
        "LLM call gave up on transient provider failure (%s%s) %s",
        _provider_failure_public_label(failure),
        f", {failure.reason}" if getattr(failure, "reason", None) else "", summary,
    )
    return (*result[:-1], usage)


def _byok_provider_error_code() -> str:
    from packages.core.ai.runtime.output_policy import BYOK_PROVIDER_ERROR_CODE

    return BYOK_PROVIDER_ERROR_CODE


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
    reasoning_effort: Optional[str] = None,
    stream_handler: _StreamHandler = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    """
    Call the configured LLM chat-completions API.  Returns (content, usage_dict).

    Transient provider failures are retried (see ``_call_with_provider_retry``);
    on a failure that is permanent or outlasts the retry budget returns
    ("", usage) with the error typed in usage. usage_dict includes
    "finish_reason" key alongside token counts. If metadata is provided,
    user-level API key and base URL take priority.
    """
    return await _call_with_provider_retry(
        lambda: _chat_completion_attempt(
            messages,
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            model=model,
            reasoning_effort=reasoning_effort,
            stream_handler=stream_handler,
            metadata=metadata,
        ),
        call_type="chat_completion",
    )


async def _chat_completion_attempt(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    stream_handler: _StreamHandler = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    """One attempt of :func:`chat_completion`."""
    requested_model = _resolve_llm_model(model or get_llm_model())
    resolved_model = _resolve_vision_model_if_needed(requested_model, messages)
    metadata_for_call = _route_metadata_for_resolved_model(metadata, requested_model, resolved_model)
    routing = await resolve_llm_routing_for_model(resolved_model, metadata_for_call)
    api_key = routing.api_key
    base_url = routing.base_url
    if not api_key:
        detail = _missing_llm_key_detail()
        logger.warning(detail)
        return "", _failure_usage(
            resolved_model, detail, error_code="llm_configuration_missing",
        )

    # Pre-flight after key resolution so native-provider BYOK can bypass
    # Manor credit gates while platform OpenRouter calls remain gated.
    await _preflight_routed_call(routing)
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

    sanitized_messages = _audio_parts_for_base_url(
        _portable_late_system_messages(_sanitize_messages_for_llm(messages), model=resolved_model), base_url,
    )
    payload: Dict[str, Any] = {
        "model": wire_model,
        "messages": sanitized_messages,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    _apply_chat_completion_compatibility(
        payload,
        model=resolved_model,
        tools=[],
    )
    adapt_native_chat_completion_payload(
        payload,
        model_id=resolved_model,
        base_url=base_url,
    )
    provider_block = _openrouter_provider_block(base_url)
    if provider_block:
        payload["provider"] = provider_block
    prompt_cache_applied = _apply_prompt_cache(payload)
    _apply_prompt_cache_key(payload, model=resolved_model, base_url=base_url)
    _apply_reasoning_effort_route(payload, base_url=base_url)

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
    response_format_fallback = None
    try:
        if _is_anthropic_messages_api(base_url):
            content, _tool_calls, usage = await _anthropic_messages_completion(
                api_key=api_key,
                base_url=base_url,
                model=resolved_model,
                messages=sanitized_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                stream_handler=stream_handler,
                call_type="chat_completion",
                started_at=started_at,
                prompt_cache=prompt_cache_applied,
            )
            return content, usage

        request_headers = _llm_request_headers(api_key, base_url)
        if stream_handler is not None or _llm_stream_transport_enabled():
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
                async with _open_llm_stream(
                    client,
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
                        _raise_on_stream_error_chunk(chunk)
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
                    raise LLMEmptyResponseError("empty streaming LLM response (no content, finish_reason, or usage)")
                _backfill_streamed_usage(usage, messages=sanitized_messages, content=content)
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
                format_retry = _unsupported_response_format_retry(stream_error, payload)
                if format_retry is not None:
                    payload, response_format_fallback = format_retry
                emitted = _stream_output_reached_consumer(
                    stream_handler, text=streamed_any_text, tool_drafts=False,
                )
                if not _stream_failure_uses_buffered_fallback(
                    stream_error, emitted=emitted,
                ):
                    logger.warning(
                        "chat_completion stream path failed, skipping buffered fallback: %s",
                        stream_error,
                    )
                    raise
                logger.warning(
                    "chat_completion stream path failed, falling back to buffered response: %s",
                    stream_error,
                )

        try:
            response = await _post_chat_with_reasoning_retry(
                f"{base_url}/chat/completions",
                headers=dict(request_headers),
                payload=payload,
                call_type="chat_completion",
            )
        except Exception as buffered_error:
            if response_format_fallback:
                buffered_error.response_format_fallback = response_format_fallback
            raise
        data = _parse_llm_response_json(response, call_type="chat_completion")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content", "") or ""
        usage = _usage_from_response(data, resolved_model)
        _attach_response_format_fallback(usage, response, response_format_fallback)
        finish_reason = choice.get("finish_reason") or ""
        usage["finish_reason"] = finish_reason
        _attach_reasoning_to_usage(usage, _extract_reasoning_content(msg))
        if _is_empty_provider_response(content, finish_reason, usage):
            detail = "Empty LLM response: provider returned no content, finish_reason, or usage."
            if routing.provider == "vercel":
                raise RuntimeError(detail)
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
            note_provider_failure(LLMEmptyResponseError(detail))
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
        fallback_result = await _retry_manor_official_call_via_openrouter(
            routing,
            model=resolved_model,
            call_type="chat_completion",
            retry=lambda: chat_completion(
                messages,
                temperature=temperature,
                response_format=response_format,
                max_tokens=max_tokens,
                model=resolved_model,
                reasoning_effort=reasoning_effort,
                stream_handler=stream_handler,
                metadata=metadata_for_call,
            ),
        )
        if fallback_result is not None:
            return fallback_result
        logger.exception("chat_completion failed: %s", detail)
        note_provider_failure(e)
        # Return empty content (callers like supervisor prefer empty over
        # canned text) but propagate the error in the usage dict so it
        # can surface in run-detail / debug surfaces.
        return "", _byok_failure_usage(routing, e, model=resolved_model, detail=detail)
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
    response_format: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    stream_handler: _StreamHandler = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    """Tool-calling completion; transient provider failures are retried.

    See :func:`_chat_completion_with_tools_attempt` for the result contract
    and :func:`_call_with_provider_retry` for the retry policy.
    """
    return await _call_with_provider_retry(
        lambda: _chat_completion_with_tools_attempt(
            messages,
            tools,
            temperature=temperature,
            tool_choice=tool_choice,
            response_format=response_format,
            model=model,
            stream_handler=stream_handler,
            metadata=metadata,
            max_tokens=max_tokens,
        ),
        call_type="chat_completion_with_tools",
    )


async def _chat_completion_with_tools_attempt(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    temperature: float = 0.7,
    tool_choice: Optional[Any] = None,
    response_format: Optional[Dict[str, Any]] = None,
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
    A failed attempt records its classified failure (``note_provider_failure``)
    so the caller's retry loop can decide whether to run it again.
    """
    requested_model = _resolve_llm_model(model or get_llm_model())
    model = _resolve_vision_model_if_needed(requested_model, messages)
    metadata_for_call = _route_metadata_for_resolved_model(metadata, requested_model, model)
    routing = await resolve_llm_routing_for_model(model, metadata_for_call)
    api_key = routing.api_key
    base_url = routing.base_url

    if not api_key:
        detail = _missing_llm_key_detail()
        logger.warning("%s Tool calling unavailable.", detail)
        return "", None, _failure_usage(
            model, detail, error_code="llm_configuration_missing",
        )

    # Pre-flight after key resolution so native-provider BYOK can bypass
    # Manor credit gates while platform OpenRouter calls remain gated.
    await _preflight_routed_call(routing)
    try:
        _validate_llm_key_model_compatibility(api_key, base_url, model)
    except LLMAuthConfigurationError as exc:
        # Provider/key mismatch — surface as a clean failure_usage instead of
        # propagating unhandled (raised before the main try below).
        logger.warning("LLM key/model compatibility check failed: %s", exc)
        return "", None, _failure_usage(model, str(exc))

    wire_model = normalize_model_for_provider(model, base_url)

    sanitized_messages = _audio_parts_for_base_url(
        _portable_late_system_messages(_sanitize_messages_for_llm(messages), model=model), base_url,
    )
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
    if response_format is not None:
        payload["response_format"] = response_format
    _apply_chat_completion_compatibility(
        payload,
        model=model,
        tools=normalized_tools,
    )
    adapt_native_chat_completion_payload(
        payload,
        model_id=model,
        base_url=base_url,
    )
    _apply_tool_reasoning_effort(payload, model=model, tools=normalized_tools, base_url=base_url)
    provider_block = _openrouter_provider_block(base_url)
    if provider_block:
        payload["provider"] = provider_block
    prompt_cache_applied = _apply_prompt_cache(payload)
    _apply_prompt_cache_key(payload, model=model, base_url=base_url)
    _apply_reasoning_effort_route(payload, base_url=base_url)

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
    response_format_fallback = None
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
                response_format=response_format,
                stream_handler=stream_handler,
                call_type="chat_completion_with_tools",
                started_at=started_at,
                prompt_cache=prompt_cache_applied,
            )

        request_headers = _llm_request_headers(api_key, base_url)
        if stream_handler is not None or _llm_stream_transport_enabled():
            streamed_any_text = False
            tool_drafts = _ToolCallDraftEmitter(stream_handler)
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
                async with _open_llm_stream(
                    client,
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
                        _raise_on_stream_error_chunk(chunk)
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
                                        "id": tc.get("id") or f"call_{uuid4().hex}",
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
                                    entry["argument_fragments"] = entry.get("argument_fragments", 0) + 1
                                await tool_drafts.update(index, entry)

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
                streamed_completion_text = content + "".join(
                    str(entry.get("arguments_text") or "")
                    for entry in tool_call_parts.values()
                )
                _backfill_streamed_usage(
                    usage, messages=sanitized_messages, content=streamed_completion_text,
                )

                parsed_tool_calls: Optional[List[Dict[str, Any]]] = None
                if tool_call_parts:
                    parsed_tool_calls = []
                    argument_diagnostics = []
                    for index in sorted(tool_call_parts.keys()):
                        entry = tool_call_parts[index]
                        raw_args = entry.get("arguments_text") or "{}"
                        argument_state = "parsed"
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            arguments = {"_raw": raw_args}
                            argument_state = "invalid_json"
                        else:
                            if not entry.get("arguments_text"):
                                argument_state = "absent"
                            elif not isinstance(arguments, dict):
                                argument_state = "non_object"
                            elif not arguments:
                                argument_state = "empty_object"
                        name = str(entry.get("name") or "").strip()
                        if not name:
                            continue
                        # Shape only: never copy argument values, prompt text,
                        # credentials, or raw provider frames into diagnostics.
                        argument_diagnostics.append({
                            "tool_index": index,
                            "argument_chars": len(entry.get("arguments_text") or ""),
                            "argument_fragments": entry.get("argument_fragments", 0),
                            "state": argument_state,
                        })
                        parsed_tool_calls.append(
                            {
                                "id": entry.get("id") or f"call_{uuid4().hex}",
                                "name": name,
                                "arguments": arguments,
                            }
                        )
                    usage["tool_argument_diagnostics"] = argument_diagnostics
                    if argument_diagnostics:
                        logger.info(
                            "[tool-arguments] model=%s diagnostics=%s",
                            model, argument_diagnostics,
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
                    raise LLMEmptyResponseError("empty streaming LLM response (no content, finish_reason, or usage)")

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
                format_retry = _unsupported_response_format_retry(stream_error, payload)
                if format_retry is not None:
                    payload, response_format_fallback = format_retry
                emitted = _stream_output_reached_consumer(
                    stream_handler, text=streamed_any_text, tool_drafts=tool_drafts.emitted,
                )
                if not _stream_failure_uses_buffered_fallback(
                    stream_error, emitted=emitted,
                ):
                    logger.warning(
                        "chat_completion_with_tools stream path failed, skipping buffered fallback: %s",
                        stream_error,
                    )
                    raise
                logger.warning(
                    "chat_completion_with_tools stream path failed, falling back to buffered response: %s",
                    stream_error,
                )

        try:
            response = await _post_chat_with_reasoning_retry(
                f"{base_url}/chat/completions",
                headers=dict(request_headers),
                payload=payload,
                call_type="chat_completion_with_tools",
            )
        except Exception as buffered_error:
            if response_format_fallback:
                buffered_error.response_format_fallback = response_format_fallback
            raise
        data = _parse_llm_response_json(response, call_type="chat_completion_with_tools")
        usage = _usage_from_response(data, model)
        _attach_response_format_fallback(usage, response, response_format_fallback)
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
                    "id": tc.get("id") or f"call_{uuid4().hex}",
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
            if routing.provider == "vercel":
                raise RuntimeError(detail)
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
            note_provider_failure(LLMEmptyResponseError(detail))
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
        fallback_result = await _retry_manor_official_call_via_openrouter(
            routing,
            model=model,
            call_type="chat_completion_with_tools",
            retry=lambda: chat_completion_with_tools(
                messages,
                tools,
                temperature=temperature,
                tool_choice=tool_choice,
                response_format=response_format,
                model=model,
                stream_handler=stream_handler,
                metadata=metadata_for_call,
                max_tokens=max_tokens,
            ),
        )
        if fallback_result is not None:
            return fallback_result

        # Fallback: try a different model if primary fails on tool-calling
        fallback = os.getenv("LLM_FALLBACK_MODEL", "").strip()
        if fallback and fallback != model and e.response.status_code == 500 and not _is_anthropic_messages_api(base_url):
            logger.warning("Primary model %s failed on tool-calling, trying fallback %s", model, fallback)
            try:
                fallback_payload = {**payload, "model": fallback}
                fallback_payload.pop("reasoning_effort", None)
                _apply_chat_completion_compatibility(
                    fallback_payload,
                    model=fallback,
                    tools=normalized_tools,
                )
                adapt_native_chat_completion_payload(
                    fallback_payload,
                    model_id=fallback,
                    base_url=base_url,
                )
                _apply_tool_reasoning_effort(
                    fallback_payload, model=fallback, tools=normalized_tools, base_url=base_url,
                )
                fb_response = await _post_with_response_format_retry(
                    f"{base_url}/chat/completions",
                    headers=dict(request_headers),
                    payload=fallback_payload,
                )
                fb_data = _parse_llm_response_json(fb_response, call_type="chat_completion_with_tools(fallback)")
                fb_usage = _usage_from_response(fb_data, fallback)
                _attach_response_format_fallback(fb_usage, fb_response)
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
                                "id": tc.get("id") or f"call_{uuid4().hex}",
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
        note_provider_failure(e)
        full_detail = (
            f"{detail}\n"
            f"request: messages={len(sanitized_messages)} tools={len(normalized_tools)} "
            f"payload_bytes≈{len(json.dumps(payload, default=str))}"
        )
        return (
            _failure_message(full_detail, model=model),
            None,
            _byok_failure_usage(routing, e, model=model, detail=full_detail),
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
        fallback_result = await _retry_manor_official_call_via_openrouter(
            routing,
            model=model,
            call_type="chat_completion_with_tools",
            retry=lambda: chat_completion_with_tools(
                messages,
                tools,
                temperature=temperature,
                tool_choice=tool_choice,
                response_format=response_format,
                model=model,
                stream_handler=stream_handler,
                metadata=metadata_for_call,
                max_tokens=max_tokens,
            ),
        )
        if fallback_result is not None:
            return fallback_result
        logger.exception("chat_completion_with_tools timed out: %s", e)
        note_provider_failure(e)
        return (
            "The request timed out. Please try again -- large tool sets can take longer to process.",
            None,
            _byok_failure_usage(routing, e, model=model, detail=f"ReadTimeout: {e}"),
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
        fallback_result = await _retry_manor_official_call_via_openrouter(
            routing,
            model=model,
            call_type="chat_completion_with_tools",
            retry=lambda: chat_completion_with_tools(
                messages,
                tools,
                temperature=temperature,
                tool_choice=tool_choice,
                response_format=response_format,
                model=model,
                stream_handler=stream_handler,
                metadata=metadata_for_call,
                max_tokens=max_tokens,
            ),
        )
        if fallback_result is not None:
            return fallback_result
        logger.exception("chat_completion_with_tools failed: %s", e)
        note_provider_failure(e)
        return (
            _failure_message(f"{type(e).__name__}: {e}", model=model),
            None,
            _byok_failure_usage(routing, e, model=model, detail=f"{type(e).__name__}: {e}"),
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
