"""Resolve ChannelConfig credentials through their owned source connection."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import Requester, get_credential_service
from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.services.integration_access import resolve_integration_access
from packages.core.services.whatsapp_business_config import (
    load_whatsapp_business_config,
)


_SOURCE_UNAVAILABLE = "Channel credential source is unavailable"
logger = logging.getLogger(__name__)

_NANGO_RUNTIME_KEYS = {
    "access_token",
    "api_key",
    "waba_id",
    "whatsapp_business_account_id",
    "phone_number_id",
    "phone_id",
    "business_account_id",
}


async def resolve_nango_integration_credentials(
    db: AsyncSession,
    integration: Integration,
    leased_credentials: dict,
) -> dict:
    """Resolve a Nango reference for a provider that needs native fields.

    The result is an in-memory runtime lease only. The connection token and
    provider fields are never copied to ChannelConfig or persisted here.
    """
    if (leased_credentials or {}).get("via") != "nango":
        return leased_credentials or {}
    nango_config = (integration.config or {}).get("nango") or {}
    provider_key = (
        leased_credentials.get("provider_config_key")
        or nango_config.get("provider_config_key")
        or integration.provider
        or ""
    )
    connection_id = (
        leased_credentials.get("connection_id")
        or nango_config.get("connection_id")
        or ""
    )
    if not provider_key or not connection_id:
        return leased_credentials
    try:
        from packages.core.ai.mcp.nango import _NANGO_BASE, get_nango_secret

        secret = await get_nango_secret(db, integration.entity_id)
        if not secret:
            return leased_credentials
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{_NANGO_BASE}/connection/{connection_id}",
                params={"provider_config_key": provider_key},
                headers={"Authorization": f"Bearer {secret}"},
            )
            response.raise_for_status()
            body = response.json() or {}
        resolved = dict(leased_credentials)
        credential_block = body.get("credentials") if isinstance(body, dict) else {}
        if isinstance(credential_block, dict):
            resolved.update(credential_block)
        integration_config = getattr(integration, "config", {})
        whatsapp_config = (
            integration_config.get("whatsapp")
            if isinstance(integration_config, dict)
            else None
        )
        for source in (
            body,
            body.get("metadata") if isinstance(body, dict) else None,
            body.get("profile") if isinstance(body, dict) else None,
            nango_config,
            whatsapp_config,
        ):
            if isinstance(source, dict):
                resolved.update({
                    key: source[key]
                    for key in _NANGO_RUNTIME_KEYS
                    if source.get(key) not in (None, "")
                })
        # Customer-owned access tokens and asset IDs come from the exact Nango
        # connection. Meta app secrets belong to the Manor deployment.
        if str(getattr(integration, "provider", "")).lower() in {
            "whatsapp", "whatsapp_cloud",
        }:
            deployment = load_whatsapp_business_config()
            resolved["app_id"] = deployment.app_id
            resolved["app_secret"] = deployment.app_secret
            resolved["verify_token"] = deployment.verify_token
            resolved["callback_url"] = deployment.callback_url
        return resolved
    except Exception:  # noqa: BLE001
        logger.debug("Nango runtime credential resolution failed", exc_info=True)
        return leased_credentials


async def channel_credential_source_is_available(
    db: AsyncSession,
    channel_config: ChannelConfig,
) -> bool:
    """Check source ownership and runtime state without decrypting a secret."""
    source_kind = channel_config.credential_source_kind
    source_id = channel_config.credential_source_id
    if bool(source_kind) != bool(source_id):
        return False
    if not source_kind:
        return channel_config.status == "active"
    if channel_config.status != "active" or not channel_config.owner_user_id:
        return False
    if source_kind not in {"integration", "oauth_account"}:
        return False

    decision = await resolve_integration_access(
        db,
        kind=source_kind,
        connection_id=source_id,
        entity_id=channel_config.entity_id,
        user_id=channel_config.owner_user_id,
        action="bind_channel",
    )
    if not decision.allowed or decision.owner_user_id != channel_config.owner_user_id:
        return False
    if source_kind == "oauth_account":
        from packages.core.services.oauth_account_credentials import (
            oauth_account_is_runtime_usable,
        )

        account = await db.get(OAuthAccount, source_id)
        return bool(
            account
            and account.user_id == channel_config.owner_user_id
            and oauth_account_is_runtime_usable(account)
        )
    integration = await db.get(Integration, source_id)
    return bool(
        integration
        and integration.owner_user_id == channel_config.owner_user_id
        and integration.status == "active"
        and (integration.credential_ref or integration.credentials)
    )


async def lease_channel_credentials(
    db: AsyncSession,
    channel_config: ChannelConfig,
    *,
    reason: str,
    resolve_nango: bool = False,
) -> dict:
    """Lease credentials from an active source owned by this channel's owner.

    Source-linked configs are intentionally never allowed to read their legacy
    JSONB column. Keeping that column empty prevents a copied token from
    becoming a second, unsynchronised credential store.
    """
    source_kind = channel_config.credential_source_kind
    source_id = channel_config.credential_source_id
    if bool(source_kind) != bool(source_id):
        raise ValueError(_SOURCE_UNAVAILABLE)
    if not source_kind:
        return get_credential_service().lease_channel_config(
            channel_config,
            requester=Requester(kind="channel", id=channel_config.id),
            reason=reason,
        )
    if not await channel_credential_source_is_available(db, channel_config):
        raise ValueError(_SOURCE_UNAVAILABLE)

    requester = Requester(kind="channel", id=channel_config.id)
    if source_kind == "oauth_account":
        account = await db.get(OAuthAccount, source_id)
        if not account or account.user_id != channel_config.owner_user_id:
            raise ValueError(_SOURCE_UNAVAILABLE)
        credentials = get_credential_service().lease_oauth_account(
            account,
            requester=requester,
            reason=reason,
        )
    else:
        integration = await db.get(Integration, source_id)
        if not integration or integration.owner_user_id != channel_config.owner_user_id:
            raise ValueError(_SOURCE_UNAVAILABLE)
        credentials = get_credential_service().lease_integration(
            integration,
            requester=requester,
            reason=reason,
        )
    if resolve_nango and source_kind == "integration":
        credentials = await resolve_nango_integration_credentials(
            db, integration, credentials,
        )
    if not credentials:
        raise ValueError(_SOURCE_UNAVAILABLE)
    return credentials


async def lease_channel_config_credentials(
    channel_config: ChannelConfig,
    *,
    reason: str,
    resolve_nango: bool = False,
) -> dict:
    """Convenience entry point for adapters, which run outside request DB scope."""
    async with async_session() as db:
        stored_config = await db.get(ChannelConfig, channel_config.id)
        if not stored_config:
            raise ValueError(_SOURCE_UNAVAILABLE)
        return await lease_channel_credentials(
            db,
            stored_config,
            reason=reason,
            resolve_nango=resolve_nango,
        )
