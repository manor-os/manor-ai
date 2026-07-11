"""
Shared Google OAuth token resolver for in-process MCP servers.

Google access tokens expire in ~1 hour. This module handles:
  1. Fetching the stored OAuth config from Manor backend
  2. Checking expiry and refreshing via refresh_token if needed
  3. Returning a valid access_token

Used by: google_calendar.py, google_drive.py
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Source keys to check (in order of preference)
_SOURCE_KEYS = ("google_workspace", "gmail")


async def get_google_access_token(state: Dict[str, Any]) -> str:
    """Resolve a valid Google OAuth access_token from entity integration config.

    Checks google_workspace and gmail source keys, picks the first active OAuth
    config, refreshes the token if expired, and returns the access_token.
    Returns empty string if no valid token is available.
    """
    import httpx

    meta = state.get("metadata") or {}
    manor_token = (
        meta.get("manor_token") or meta.get("manorToken")
        or meta.get("pms_token") or meta.get("pmsToken")
        or os.getenv("MANOR_API_TOKEN", "")
    )
    if not manor_token:
        return ""

    backend_url = (
        os.getenv("JAVA_HOST")
        or os.getenv("MANOR_BACKEND_URL", "http://localhost:8070")
    ).rstrip("/")

    for source_key in _SOURCE_KEYS:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{backend_url}/sys/source/config/list",
                    params={"sourceKey": source_key},
                    headers={"Authorization": f"Bearer {manor_token}"},
                )
            if resp.status_code != 200:
                continue
            data = resp.json()
            rows = data.get("rows") or data.get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("status", "1")) != "1":
                    continue
                if row.get("authMode") != "oauth":
                    continue
                token = await _resolve_token_from_config(row)
                if token:
                    return token
        except Exception as e:
            logger.warning("google_auth: fetch configs failed for %s: %s", source_key, e)

    return ""


async def _resolve_token_from_config(config: Dict[str, Any]) -> Optional[str]:
    """Extract access_token from a config row, refreshing if expired."""
    creds = config.get("credentials") or {}
    access_token = creds.get("access_token")
    if not access_token:
        return None

    # Check if token is expired (refresh 60s before expiry)
    expires_in = creds.get("expires_in")
    created_at = creds.get("created_at") or 0
    if created_at and expires_in:
        if time.time() > float(created_at) + float(expires_in) - 60:
            refresh_token = creds.get("refresh_token")
            if refresh_token:
                new_token = await _refresh_access_token(refresh_token)
                if new_token:
                    return new_token
            # Token expired and no refresh available
            return None

    return access_token


async def _refresh_access_token(refresh_token: str) -> Optional[str]:
    """Exchange refresh_token for a new access_token.

    Resolves Google client_id/secret via ``resolve_oauth_config``
    (DB-first, env bootstrap fallback). All three Google scopes
    (gmail / google_calendar / google_drive) share the same OAuth app,
    so we look up "gmail" as the canonical row.
    """
    import httpx
    from packages.core.database import async_session
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    async with async_session() as db:
        cfg = await resolve_oauth_config(db, "gmail")
    if not cfg:
        logger.warning("google_auth: GOOGLE_CLIENT_ID/SECRET not configured, cannot refresh token")
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                },
            )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.warning("google_auth: token refresh failed HTTP %s", resp.status_code)
    except Exception as e:
        logger.exception("google_auth: token refresh error: %s", e)
    return None
