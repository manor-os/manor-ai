"""Deployment-owned Discord App configuration and identity validation."""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.oauth_provider_config import resolve_oauth_config


_DISCORD_APPLICATION_URL = "https://discord.com/api/v10/oauth2/applications/@me"


@dataclass(frozen=True)
class DiscordAppConfig:
    application_id: str
    bot_token: str
    public_key: str


async def resolve_discord_app_config(
    db: AsyncSession,
) -> DiscordAppConfig | None:
    oauth = await resolve_oauth_config(db, "discord")
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    public_key = os.getenv("DISCORD_PUBLIC_KEY", "").strip().lower()
    if oauth is None or not bot_token or not public_key:
        return None
    if len(public_key) != 64:
        return None
    try:
        bytes.fromhex(public_key)
    except ValueError:
        return None
    return DiscordAppConfig(
        application_id=oauth.client_id,
        bot_token=bot_token,
        public_key=public_key,
    )


async def resolve_discord_gateway_config(
    db: AsyncSession,
) -> DiscordAppConfig | None:
    """Resolve only the identity and Bot token needed by the Gateway.

    The Gateway never performs OAuth token exchange or signature validation.
    Keeping this path independent from ``resolve_oauth_config`` means its
    singleton Pod does not need Vault access or the deployment's OAuth client
    secret. The deployment environment remains authoritative when present;
    the database client id is a fallback for self-hosted installations.
    """
    application_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    if not application_id:
        from packages.core.models.mcp import MCPServer

        server = (
            await db.execute(
                select(MCPServer).where(MCPServer.server_key == "discord")
            )
        ).scalar_one_or_none()
        config = server.default_config if server and isinstance(
            server.default_config, dict
        ) else {}
        application_id = str(config.get("oauth_client_id") or "").strip()

    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not application_id or not bot_token:
        return None
    return DiscordAppConfig(
        application_id=application_id,
        bot_token=bot_token,
        public_key="",
    )


async def validate_discord_app_config(app: DiscordAppConfig) -> bool:
    """Require the Bot token, Application ID, and Public Key to match."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                _DISCORD_APPLICATION_URL,
                headers={"Authorization": f"Bot {app.bot_token}"},
            )
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("id") or "") == app.application_id
        and str(payload.get("verify_key") or "").lower() == app.public_key
    )
