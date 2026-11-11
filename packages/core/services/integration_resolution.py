"""Workspace integration resolution helpers.

Workspace setup drafts are model-authored, so provider names can be too broad
or simply unsupported by the product. Keep the catalog/connection checks in one
place before surfacing "connect this integration" prompts to users.
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.mcp import MCPServer
from packages.core.models.user import OAuthAccount
from packages.core.services.integration_service import coming_soon_servers
from packages.core.services.oauth_account_credentials import (
    oauth_account_is_runtime_usable_clause,
)
from packages.core.services.provider_keys import canonical_provider_key


_BROWSER_PROVIDER = "chrome"
_NON_USER_FACING_PROVIDER_KEYS = {
    "knowledge_local",
    "chrome_knowledge_local",
    "nango",
}

_BROWSER_COVERAGE_PROVIDER_KEYS = {
    "browser",
    "browser_use",
    "chrome_browser",
    "web_browser",
    "instagram",
    "instagram_browser",
    "threads",
    "threads_browser",
    "douyin",
    "douyin_browser",
    "kuaishou",
    "kuaishou_browser",
    "boss_zhipin",
    "boss",
}

@dataclass(frozen=True)
class MissingIntegrationResolution:
    provider: str
    original_provider: str
    covered_provider: str | None = None

    @property
    def changed(self) -> bool:
        return self.provider != self.original_provider


@dataclass(frozen=True)
class IntegrationProviderReadiness:
    provider: str
    ready: bool
    reason: str
    scope: str
    setup_kind: str | None = None


def is_user_facing_integration_provider(provider: object) -> bool:
    """Whether a provider should create user setup work."""
    key = canonical_provider_key(provider)
    return bool(key) and key not in _NON_USER_FACING_PROVIDER_KEYS


async def integration_provider_readiness(
    db: AsyncSession,
    *,
    entity_id: str,
    provider_keys: list[str] | set[str] | tuple[str, ...],
    user_id: str | None = None,
    require_online: bool = True,
) -> dict[str, IntegrationProviderReadiness]:
    """Resolve runtime readiness without changing MCP capability bindings.

    ``require_online=False`` asks whether each integration is connected, not
    whether it answers right now (a paired Chrome that is closed counts as
    connected): a setup gate must not open and close with a laptop lid.
    """
    from packages.core.services.agent_permission_service import (
        can_use_mcp_server,
        provider_requires_integration_account_registry,
    )
    from packages.core.services.integration_account_service import load_runtime_integration_registry

    raw_providers = [
        str(key or "").strip()
        for key in provider_keys
        if str(key or "").strip()
    ]
    providers = list(dict.fromkeys(
        canonical_provider_key(key) for key in raw_providers if canonical_provider_key(key)
    ))
    account_free_provider_rows = list((await db.execute(
        select(MCPServer.server_key).where(
            MCPServer.server_key.in_([*raw_providers, *providers]),
            MCPServer.status == "active",
            MCPServer.auth_type.in_(("none", "no_auth")),
        )
    )).scalars().all()) if providers else []
    account_free_providers = {
        canonical_provider_key(provider)
        for provider in account_free_provider_rows
    }
    account_providers = [
        provider for provider in providers
        if provider not in account_free_providers
        and provider_requires_integration_account_registry(provider)
    ]
    # Reuse one actor-scoped snapshot within this check. Each provider still
    # passes the runtime gate; no account state survives into another request.
    integration_registry = await load_runtime_integration_registry(
        db,
        user_id=user_id or "",
        entity_id=entity_id,
        provider_keys=account_providers,
    ) if account_providers else None
    out: dict[str, IntegrationProviderReadiness] = {}
    for provider in providers:
        if provider in account_free_providers and provider != "chrome":
            out[provider] = IntegrationProviderReadiness(
                provider=provider,
                ready=True,
                reason="Integration requires no account connection.",
                scope="internal",
            )
            continue
        decision = await can_use_mcp_server(
            db,
            user_id=user_id or "",
            entity_id=entity_id,
            server_key=provider,
            integration_registry=integration_registry,
            allow_env_fallback=False,
            require_online=require_online,
        )
        reason = decision.reason
        setup_kind = None
        out[provider] = IntegrationProviderReadiness(
            provider=provider,
            ready=decision.allowed,
            reason=reason,
            scope=decision.scope or "none",
            setup_kind=setup_kind,
        )
    return out


async def bindable_mcp_provider_keys(db: AsyncSession) -> set[str]:
    """Active MCP providers an Agent binding may name.

    Includes Manor's internal bridges (for example ``chrome_knowledge_local``),
    which Blueprints bind directly but which are never offered to users as
    integrations to connect.
    """
    rows = (await db.execute(
        select(MCPServer.server_key).where(MCPServer.status == "active")
    )).scalars().all()
    coming_soon = {canonical_provider_key(key) for key in coming_soon_servers()}
    return {
        canonical_provider_key(key)
        for key in rows
        if canonical_provider_key(key)
        and canonical_provider_key(key) not in coming_soon
    }


async def supported_integration_provider_keys(db: AsyncSession) -> set[str]:
    """Bindable providers that are user-facing integrations."""
    return {
        key
        for key in await bindable_mcp_provider_keys(db)
        if is_user_facing_integration_provider(key)
    }


async def connected_integration_provider_keys(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None = None,
) -> set[str]:
    keys: set[str] = set()
    integration_rows = (await db.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.status == "active",
        )
    )).scalars().all()
    for integration in integration_rows:
        if canonical_provider_key(integration.provider) == "chrome":
            continue
        config = integration.config or {}
        if integration.credentials or integration.credential_ref or config.get("nango"):
            keys.add(canonical_provider_key(integration.provider))

    if user_id:
        oauth_rows = (await db.execute(
            select(OAuthAccount.provider).where(
                OAuthAccount.user_id == user_id,
                oauth_account_is_runtime_usable_clause(),
            )
        )).scalars().all()
        keys.update(canonical_provider_key(provider) for provider in oauth_rows)

    return {key for key in keys if key}


async def resolve_missing_integration_provider(
    db: AsyncSession,
    *,
    entity_id: str,
    provider: object,
    user_id: str | None = None,
) -> MissingIntegrationResolution | None:
    supported = await supported_integration_provider_keys(db)
    connected = await connected_integration_provider_keys(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    return resolve_missing_integration_provider_key(
        provider,
        supported_provider_keys=supported,
        connected_provider_keys=connected,
    )


async def resolve_missing_integration_flags(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None = None,
    flagged: list[object] | None = None,
) -> list[dict[str, object]]:
    supported = await supported_integration_provider_keys(db)
    connected = await connected_integration_provider_keys(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    out: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for raw in flagged or []:
        if not isinstance(raw, dict):
            continue
        resolution = resolve_missing_integration_provider_key(
            raw.get("provider"),
            supported_provider_keys=supported,
            connected_provider_keys=connected,
        )
        if resolution is None:
            continue
        service_keys = tuple(sorted(str(key) for key in (raw.get("linked_service_keys") or []) if key))
        dedupe_key = (resolution.provider, service_keys, str(raw.get("source") or ""))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item: dict[str, object] = dict(raw)
        item["provider"] = resolution.provider
        if resolution.covered_provider:
            item["covered_provider"] = resolution.covered_provider
        out.append(item)
    return out


def resolve_missing_integration_provider_key(
    provider: object,
    *,
    supported_provider_keys: set[str],
    connected_provider_keys: set[str] | None = None,
) -> MissingIntegrationResolution | None:
    original = canonical_provider_key(provider)
    if not original:
        return None
    connected_provider_keys = connected_provider_keys or set()
    if original in connected_provider_keys:
        return None

    if original in supported_provider_keys:
        return MissingIntegrationResolution(provider=original, original_provider=original)

    if original in _BROWSER_COVERAGE_PROVIDER_KEYS and _BROWSER_PROVIDER in supported_provider_keys:
        if _BROWSER_PROVIDER in connected_provider_keys:
            return None
        return MissingIntegrationResolution(
            provider=_BROWSER_PROVIDER,
            original_provider=original,
            covered_provider=original,
        )

    return None
