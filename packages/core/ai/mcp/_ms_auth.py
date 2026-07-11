"""
Shared Microsoft Graph token resolver for in-process MCP servers.

Microsoft access tokens expire in ~1 hour (configurable per tenant).
This module handles:
  1. Fetching the stored OAuth config from Manor backend
  2. Checking expiry and refreshing via refresh_token if needed
  3. Returning a valid access_token

Used by: outlook.py, onedrive.py, ms_calendar.py, ms_teams.py, ms_excel.py.

Mirrors the design of ``_google_auth.py`` so a deployment can scale
the same operator pattern across both ecosystems.

Microsoft tenancy
─────────────────
The OAuth ``authority`` URL is shaped
``https://login.microsoftonline.com/<tenant>/oauth2/v2.0/{authorize,token}``
where ``<tenant>`` is one of:

* ``common`` — accepts both AAD work/school accounts AND personal
  Microsoft accounts. The default for a multi-tenant SaaS like Manor.
* ``organizations`` — work/school only.
* ``consumers`` — personal MSA only.
* ``<tenant-guid>`` — locked to one Azure AD tenant.

Manor lets the deployment override via ``MS_TENANT`` env (default:
``common``). For most installs ``common`` is right.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _ms_tenant() -> str:
    return os.getenv("MS_TENANT", "common").strip() or "common"


def _ms_token_url() -> str:
    return f"https://login.microsoftonline.com/{_ms_tenant()}/oauth2/v2.0/token"


# Source keys to check (in order of preference) when resolving tokens
# from Manor's per-entity Integration store.
_SOURCE_KEYS = ("microsoft_365", "outlook", "onedrive", "ms_calendar", "ms_teams")


async def get_ms_access_token(state: Dict[str, Any]) -> str:
    """Resolve a valid Microsoft Graph access token from entity
    integration config.

    Checks each ``_SOURCE_KEYS`` in order, picks the first active
    OAuth config, refreshes the token if expired, and returns the
    access_token. Returns empty string if no valid token is available.

    The ``state`` dict shape mirrors what call_tool sees — ``metadata``
    carries the per-call manor_token / pms_token used to talk to
    Manor's backend for credential lookup.
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
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ms_auth: fetch configs failed for %s: %s", source_key, exc,
            )

    return ""


async def _resolve_token_from_config(config: Dict[str, Any]) -> Optional[str]:
    """Pull access_token from a config row, refreshing if expired."""
    creds = config.get("credentials") or {}
    access_token = creds.get("access_token")
    if not access_token:
        return None

    # Refresh 60s before expiry to avoid races where the token expires
    # mid-request.
    expires_in = creds.get("expires_in")
    created_at = creds.get("created_at") or 0
    if created_at and expires_in:
        if time.time() > float(created_at) + float(expires_in) - 60:
            refresh_token = creds.get("refresh_token")
            if refresh_token:
                new_token = await _refresh_access_token(refresh_token)
                if new_token:
                    return new_token
            return None

    return access_token


async def _refresh_access_token(refresh_token: str) -> Optional[str]:
    """Exchange refresh_token for a new access_token at Microsoft.

    Resolves Manor's MS OAuth client_id/secret via
    ``resolve_oauth_config``. All five MS modules (outlook /
    onedrive / ms_calendar / ms_teams / ms_excel) share the same
    OAuth app registration; we look up "outlook" as the canonical
    row to fetch the client credentials.
    """
    import httpx
    from packages.core.database import async_session
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    async with async_session() as db:
        cfg = await resolve_oauth_config(db, "outlook")
    if not cfg:
        logger.warning(
            "ms_auth: MS_CLIENT_ID/SECRET not configured, cannot refresh token"
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _ms_token_url(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    # Including scope helps when the consent screen
                    # was approved with a superset of scopes; AAD
                    # narrows tokens to whatever's requested here.
                    "scope": "offline_access " + (cfg.scopes or ""),
                },
            )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.warning(
            "ms_auth: token refresh failed HTTP %s — %s",
            resp.status_code, resp.text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ms_auth: token refresh error: %s", exc)
    return None
