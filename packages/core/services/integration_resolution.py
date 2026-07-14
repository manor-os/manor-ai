"""Workspace integration resolution helpers.

Workspace setup drafts are model-authored, so provider names can be too broad
or simply unsupported by the product. Keep the catalog/connection checks in one
place before surfacing "connect this integration" prompts to users.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.mcp import MCPServer
from packages.core.models.user import OAuthAccount
from packages.core.services.integration_service import coming_soon_servers
from packages.core.services.provider_keys import canonical_provider_key


_BROWSER_PROVIDER = "chrome"
_NON_USER_FACING_PROVIDER_KEYS = {
    "knowledge_local",
    "chrome_knowledge_local",
    "local_browser",
    "nango",
}

_BROWSER_COVERAGE_PROVIDER_KEYS = {
    "browser",
    "browser_use",
    "chrome_browser",
    "local_browser",
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

_PREFERRED_PROVIDER_BY_DUPLICATE = {
    "local_browser": _BROWSER_PROVIDER,
}


@dataclass(frozen=True)
class MissingIntegrationResolution:
    provider: str
    original_provider: str
    covered_provider: str | None = None

    @property
    def changed(self) -> bool:
        return self.provider != self.original_provider


async def supported_integration_provider_keys(db: AsyncSession) -> set[str]:
    rows = (await db.execute(
        select(MCPServer.server_key).where(MCPServer.status == "active")
    )).scalars().all()
    coming_soon = {canonical_provider_key(key) for key in coming_soon_servers()}
    return {
        canonical_provider_key(key)
        for key in rows
        if canonical_provider_key(key)
        and canonical_provider_key(key) not in coming_soon
        and canonical_provider_key(key) not in _NON_USER_FACING_PROVIDER_KEYS
    }


async def connected_integration_provider_keys(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None = None,
) -> set[str]:
    keys: set[str] = set()
    integration_rows = (await db.execute(
        select(Integration.provider).where(
            Integration.entity_id == entity_id,
            Integration.status == "active",
        )
    )).scalars().all()
    keys.update(canonical_provider_key(provider) for provider in integration_rows)

    if user_id:
        oauth_rows = (await db.execute(
            select(OAuthAccount.provider).where(
                OAuthAccount.user_id == user_id,
                OAuthAccount.access_token.is_not(None),
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

    preferred = _PREFERRED_PROVIDER_BY_DUPLICATE.get(original)
    if preferred and preferred in supported_provider_keys:
        if preferred in connected_provider_keys:
            return None
        return MissingIntegrationResolution(
            provider=preferred,
            original_provider=original,
            covered_provider=original,
        )

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
