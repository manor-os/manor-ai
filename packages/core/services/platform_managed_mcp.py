"""Platform-managed MCP credentials and billing policy.

These providers use Manor-owned API credentials in Cloud. They are callable by
agents without a tenant Integration row and are billed to the invoking entity,
with the acting user retained on the usage record. Self-hosted deployments keep
the existing BYOK Integration behavior.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.plans import is_cloud
from packages.core.models.mcp import MCPServer
from packages.core.services.provider_keys import canonical_provider_key




def platform_managed_mcp_server_keys() -> frozenset[str]:
    """Return providers funded by Manor for the current deployment."""
    if not is_cloud():
        return frozenset()
    return frozenset()


def is_platform_managed_mcp_server(server_key: object) -> bool:
    return canonical_provider_key(server_key) in platform_managed_mcp_server_keys()


def _first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _environment_credentials(server_key: str) -> dict[str, str]:
    return {}


def platform_mcp_server_is_configured(server: MCPServer | None) -> bool:
    """Check readiness without decrypting or exposing a platform secret."""
    if server is None or not is_platform_managed_mcp_server(server.server_key):
        return False
    return bool(server.credential_ref or _environment_credentials(server.server_key))


async def platform_mcp_provider_is_configured(
    db: AsyncSession,
    server_key: object,
) -> bool:
    key = canonical_provider_key(server_key)
    if not is_platform_managed_mcp_server(key):
        return False
    server = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.server_key == key,
                MCPServer.status == "active",
            )
        )
    ).scalar_one_or_none()
    return platform_mcp_server_is_configured(server)




def _normalized_credentials(server_key: str, payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return {}


async def resolve_platform_mcp_bearer_token(
    db: AsyncSession,
    *,
    server_key: object,
    requester_id: str,
) -> str | None:
    """Lease one platform credential and shape it for the existing adapter."""
    key = canonical_provider_key(server_key)
    if not is_platform_managed_mcp_server(key):
        return None

    server = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.server_key == key,
                MCPServer.status == "active",
            )
        )
    ).scalar_one_or_none()
    if server is None:
        return None

    credentials: dict[str, str] = {}
    if server.credential_ref:
        try:
            from packages.core.credentials import Requester, get_credential_service

            leased = get_credential_service().lease_mcp_server(
                server,
                requester=Requester(
                    kind="agent",
                    id=str(requester_id or "platform_mcp_runtime"),
                ),
                reason=f"platform_mcp.{key}",
            )
            credentials = _normalized_credentials(key, leased)
        except Exception:  # noqa: BLE001
            # A deployment env secret remains a safe fallback when a stale DB
            # credential cannot be decrypted after a key rotation.
            credentials = {}
    if not credentials:
        credentials = _environment_credentials(key)
    if not credentials:
        return None

    if key == "alpaca_market_data":
        return json.dumps(
            {
                "api_key": credentials["api_key"],
                "api_secret": credentials["api_secret"],
            }
        )
    return credentials["api_key"]


def platform_mcp_call_credits() -> int:
    """Fixed Manor-credit price for one successful platform provider call."""
    raw = os.getenv("MANOR_PLATFORM_MARKET_DATA_CREDITS_PER_CALL", "1").strip()
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1
