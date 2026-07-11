"""Model gateway facade.

This module is the business-facing boundary for model routing. Provider
registry data, official platform credentials, OpenRouter fallback, and route
pricing source are exposed here so callers do not reach into lower-level
storage modules directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.model_provider_handlers import (  # noqa: F401 — re-exported
    GENERIC_SK_PROVIDERS,
    PROVIDER_HANDLERS,
    ModelProviderHandler,
    detect_provider_from_key,
    handler_for_model,
    handler_for_provider,
    normalize_model_for_provider,
    official_env_key,
    provider_catalog,
    provider_for_model_id,
    provider_from_base_url,
    resolve_provider_base_url,
)
from packages.core.services import platform_model_provider_keys as provider_key_service
from packages.core.services.platform_model_provider_keys import OfficialProviderCredential


@dataclass(frozen=True)
class ModelGatewayRoute:
    api_key: str
    base_url: str
    provider: str | None
    source: str
    source_detail: str = ""

    @property
    def is_byok(self) -> bool:
        return self.source == "byok"

    @property
    def pricing_source(self) -> str:
        if self.is_byok:
            return "byok"
        if self.provider == "openrouter" or "openrouter.ai" in self.base_url.lower():
            return "openrouter"
        return self.source or "official"


def provider_for_model(model_id: str | None) -> str | None:
    return provider_for_model_id(model_id)


def route_pricing_source(route: ModelGatewayRoute, base_url: str | None = None) -> tuple[str, str]:
    """Return ``(route_provider, pricing_source)`` for usage/credit billing."""

    url = (base_url or route.base_url or "").strip()
    route_provider = (
        route.provider
        or provider_from_base_url(url)
        or detect_provider_from_key(route.api_key)
        or ""
    )
    if route.source == "byok":
        return route_provider, "byok"
    if route_provider == "openrouter" or "openrouter.ai" in url.lower():
        return "openrouter", "openrouter"
    return route_provider, (route.source or "official")


async def resolve_official_model_route(
    model_id: str,
    *,
    reason: str = "llm.chat.official_provider_key",
    openrouter_reason: str = "llm.chat.openrouter_fallback_key",
) -> ModelGatewayRoute | None:
    """Resolve official platform routing for a catalog model.

    Native official provider tokens win. If no native token exists, OpenRouter
    remains the broad fallback so catalog models keep working during rollout.
    """

    model_provider = provider_for_model_id(model_id)
    if not model_provider:
        return None

    credential = await provider_key_service.resolve_official_provider_credential(
        model_provider,
        reason=reason,
    )
    if credential and credential.api_key:
        return ModelGatewayRoute(
            api_key=credential.api_key,
            base_url=credential.base_url,
            provider=model_provider,
            source=credential.source,
            source_detail=credential.source_detail,
        )

    if model_provider != "openrouter":
        fallback = await provider_key_service.resolve_official_provider_credential(
            "openrouter",
            reason=openrouter_reason,
        )
        if fallback and fallback.api_key:
            return ModelGatewayRoute(
                api_key=fallback.api_key,
                base_url=fallback.base_url,
                provider="openrouter",
                source=fallback.source,
                source_detail=fallback.source_detail,
            )

    return None


async def resolve_gateway_credential(
    provider: str,
    *,
    reason: str,
) -> OfficialProviderCredential | None:
    return await provider_key_service.resolve_official_provider_credential(provider, reason=reason)


async def list_model_provider_statuses(db: AsyncSession) -> list[dict[str, Any]]:
    return await provider_key_service.list_official_provider_key_statuses(db)


async def upsert_model_provider_token(
    db: AsyncSession,
    *,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    enabled: bool = True,
    actor_user_id: str | None = None,
):
    return await provider_key_service.upsert_official_provider_key(
        db,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        enabled=enabled,
        actor_user_id=actor_user_id,
    )


async def set_model_provider_token_status(
    db: AsyncSession,
    *,
    provider: str,
    enabled: bool,
    actor_user_id: str | None = None,
):
    return await provider_key_service.set_official_provider_key_status(
        db,
        provider=provider,
        enabled=enabled,
        actor_user_id=actor_user_id,
    )


async def delete_model_provider_token(
    db: AsyncSession,
    *,
    provider: str,
    actor_user_id: str | None = None,
):
    return await provider_key_service.delete_official_provider_key(
        db,
        provider=provider,
        actor_user_id=actor_user_id,
    )


def model_provider_catalog() -> list[dict[str, Any]]:
    return provider_catalog()
