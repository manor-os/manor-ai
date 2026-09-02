"""Nango credential materialization without database or tool-layer coupling."""

from __future__ import annotations

import logging

import httpx


logger = logging.getLogger(__name__)


async def fetch_nango_access_token(
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
) -> str | None:
    from packages.core.ai.mcp.nango import _NANGO_BASE

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{_NANGO_BASE}/connection/{connection_id}",
                params={"provider_config_key": provider_config_key},
                headers={"Authorization": f"Bearer {secret}"},
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Nango bridge fetch failed for %s/%s: %s",
            provider_config_key,
            connection_id,
            exc,
        )
        return None

    credentials = body.get("credentials") or {}
    return (
        credentials.get("access_token")
        or credentials.get("api_key")
        or body.get("access_token")
    )


__all__ = ["fetch_nango_access_token"]
