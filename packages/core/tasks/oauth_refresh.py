"""Proactive OAuth token refresh.

Scans ``oauth_accounts`` for rows whose ``token_expires_at`` is within the
near future, exchanges the stored ``refresh_token`` for a new access_token
at the provider's OAuth token endpoint, and updates the row. Entity-scope
tokens in ``integrations`` are refreshed too when their credentials JSON
carries a refresh_token.

Runs on a Celery beat every minute. Each run refreshes at most N tokens
to cap the work per cycle. Failures are logged but don't abort the scan.

Supported providers (refresh endpoint + client credentials from env):

  gmail           GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  google_calendar GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  google_drive    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  linkedin        LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
  github          GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
  twitter_x       X_CLIENT_ID / X_CLIENT_SECRET
  quickbooks      QUICKBOOKS_CLIENT_ID / QUICKBOOKS_CLIENT_SECRET

``stripe`` uses static API keys, not OAuth → skipped here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases

logger = logging.getLogger(__name__)


# ── Provider config ─────────────────────────────────────────────────────────

_PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "gmail": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "google_calendar": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "google_drive": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "linkedin": {
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "client_id_env": "LINKEDIN_CLIENT_ID",
        "client_secret_env": "LINKEDIN_CLIENT_SECRET",
    },
    "github": {
        "token_url": "https://github.com/login/oauth/access_token",
        "client_id_env": "GITHUB_CLIENT_ID",
        "client_secret_env": "GITHUB_CLIENT_SECRET",
    },
    "twitter_x": {
        "token_url": "https://api.x.com/2/oauth2/token",
        "client_id_env": "X_CLIENT_ID",
        "client_secret_env": "X_CLIENT_SECRET",
    },
    "quickbooks": {
        "token_url": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        "client_id_env": "QUICKBOOKS_CLIENT_ID",
        "client_secret_env": "QUICKBOOKS_CLIENT_SECRET",
    },
}

_PROVIDER_LOOKUP_KEYS = {
    alias
    for provider in _PROVIDER_CONFIG
    for alias in provider_key_aliases(provider)
}

# Refresh tokens expiring within this window.
_REFRESH_THRESHOLD = timedelta(minutes=5)

# Cap the per-run batch so a single cycle can't stall the beat.
_MAX_PER_RUN = 50

_PERMANENT_REFRESH_ERROR_KEY = "_permanent_refresh_error"
_PERMANENT_REFRESH_ERROR_CODES = {"invalid_grant", "invalid_token"}
_TOKEN_INVALID_SNIPPETS = (
    "invalid token",
    "token was invalid",
    "token is invalid",
    "token has expired",
    "token expired",
    "revoked",
)


# ── HTTP refresh ────────────────────────────────────────────────────────────

async def refresh_token_via_provider(
    provider: str,
    refresh_token: str,
    db: Optional[AsyncSession] = None,
) -> Optional[dict]:
    """Exchange a refresh_token for a new access_token at the provider's URL.

    Returns a dict with at minimum ``access_token``; usually also
    ``expires_in`` (seconds) and sometimes a new ``refresh_token`` (rotation).
    Returns None on any failure — caller logs + moves on.

    Client credentials resolve through ``resolve_oauth_config`` (DB
    first, env bootstrap fallback). Pass ``db`` from the calling
    coroutine to reuse the session; if omitted, opens a short-lived one.
    """
    from packages.core.services.oauth_provider_config import (
        build_token_request_auth,
        resolve_oauth_config,
    )

    if db is None:
        from packages.core.database import async_session
        async with async_session() as _db:
            return await refresh_token_via_provider(provider, refresh_token, db=_db)

    provider = canonical_provider_key(provider)
    oauth_cfg = await resolve_oauth_config(db, provider)
    if not oauth_cfg:
        logger.debug(
            "oauth_refresh: %s client credentials not configured — skipping",
            provider,
        )
        return None

    headers, body = build_token_request_auth(
        oauth_cfg,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": oauth_cfg.client_id,
            "client_secret": oauth_cfg.client_secret,
        },
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                oauth_cfg.token_url,
                data=body,
                headers=headers,
            )
    except Exception as e:
        logger.warning("oauth_refresh: %s token endpoint unreachable: %s", provider, e)
        return None

    if resp.status_code >= 400:
        permanent_error = _permanent_refresh_error_from_response(resp)
        logger.warning(
            "oauth_refresh: %s refresh returned %d: %s",
            provider, resp.status_code, resp.text[:200],
        )
        if permanent_error:
            return {_PERMANENT_REFRESH_ERROR_KEY: permanent_error}
        return None

    try:
        return resp.json()
    except Exception:
        return None


def _permanent_refresh_error_from_response(resp: httpx.Response) -> dict[str, Any] | None:
    if resp.status_code not in {400, 401}:
        return None
    error = ""
    description = ""
    try:
        body = resp.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        error = str(body.get("error") or "").strip()
        description = str(
            body.get("error_description")
            or body.get("detail")
            or body.get("message")
            or ""
        ).strip()

    combined = f"{error} {description} {resp.text or ''}".lower()
    if error in _PERMANENT_REFRESH_ERROR_CODES:
        pass
    elif error == "invalid_request" and any(
        snippet in combined for snippet in _TOKEN_INVALID_SNIPPETS
    ):
        pass
    elif not error and any(snippet in combined for snippet in _TOKEN_INVALID_SNIPPETS):
        pass
    else:
        return None

    return {
        "error": error or "invalid_token",
        "description": description or (resp.text[:200] if resp.text else ""),
        "status_code": resp.status_code,
    }


def _is_permanent_refresh_error(data: object) -> bool:
    return isinstance(data, dict) and isinstance(
        data.get(_PERMANENT_REFRESH_ERROR_KEY),
        dict,
    )


def _permanent_refresh_error(data: dict[str, Any]) -> dict[str, Any]:
    error = data.get(_PERMANENT_REFRESH_ERROR_KEY)
    return error if isinstance(error, dict) else {}


def _reauth_required_payload(provider: str, error: dict[str, Any]) -> dict[str, Any]:
    return {
        "reauth_required": True,
        "provider": provider,
        "error": error.get("error") or "invalid_token",
        "description": error.get("description") or "Refresh token is no longer valid.",
        "status_code": error.get("status_code"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _mark_oauth_account_reauth_required(
    row: OAuthAccount,
    provider: str,
    error: dict[str, Any],
) -> None:
    refresh_state = _reauth_required_payload(provider, error)
    profile = dict(row.profile or {})
    profile["oauth_refresh"] = refresh_state
    profile["last_health_check"] = {
        "ok": False,
        "detail": "OAuth refresh token rejected by provider; reconnect.",
        "latency_ms": 0.0,
        "checked_at": refresh_state["checked_at"],
    }
    row.profile = profile
    row.access_token = None
    row.refresh_token = None
    row.token_expires_at = None


def _mark_integration_reauth_required(
    row: Integration,
    provider: str,
    error: dict[str, Any],
) -> None:
    refresh_state = _reauth_required_payload(provider, error)
    config = dict(row.config or {})
    config["oauth_refresh"] = refresh_state
    config["last_health_check"] = {
        "ok": False,
        "detail": "OAuth refresh token rejected by provider; reconnect.",
        "latency_ms": 0.0,
        "checked_at": refresh_state["checked_at"],
    }
    row.config = config
    credentials = dict(row.credentials or {})
    for key in ("access_token", "refresh_token", "expires_at"):
        credentials.pop(key, None)
    row.credentials = credentials


# ── DB scan + update ────────────────────────────────────────────────────────

async def _refresh_oauth_accounts(db: AsyncSession) -> int:
    """Refresh user-scope OAuth tokens expiring within the threshold."""
    deadline = datetime.now(timezone.utc) + _REFRESH_THRESHOLD

    rows = (await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider.in_(_PROVIDER_LOOKUP_KEYS),
            OAuthAccount.refresh_token.isnot(None),
            OAuthAccount.token_expires_at.isnot(None),
            OAuthAccount.token_expires_at <= deadline,
        ).limit(_MAX_PER_RUN)
    )).scalars().all()

    refreshed = 0
    changed = False
    for row in rows:
        provider = canonical_provider_key(row.provider)
        data = await refresh_token_via_provider(
            provider,
            row.refresh_token,
            db=db,
        )
        if _is_permanent_refresh_error(data):
            _mark_oauth_account_reauth_required(
                row,
                provider,
                _permanent_refresh_error(data),
            )
            changed = True
            continue
        if not data or not data.get("access_token"):
            continue
        row.access_token = data["access_token"]
        if data.get("refresh_token"):
            row.refresh_token = data["refresh_token"]  # rotated
        expires_in = data.get("expires_in")
        if expires_in:
            row.token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(expires_in)
            )
        refreshed += 1
        changed = True

    if changed:
        await db.flush()
    return refreshed


async def _refresh_integrations(db: AsyncSession) -> int:
    """Refresh entity-scope tokens stored in integrations.credentials.

    Contract: `credentials` may contain ``refresh_token``, ``access_token``,
    and ``expires_at`` (ISO-8601) in its JSON. We refresh when ``expires_at``
    is within the threshold.
    """
    now = datetime.now(timezone.utc)
    deadline = now + _REFRESH_THRESHOLD

    rows = (await db.execute(
        select(Integration).where(
            Integration.provider.in_(_PROVIDER_LOOKUP_KEYS),
            Integration.status == "active",
        ).limit(_MAX_PER_RUN)
    )).scalars().all()

    refreshed = 0
    changed = False
    for row in rows:
        creds = row.credentials or {}
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            continue
        expires_at = creds.get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt > deadline:
                    continue
            except Exception:
                pass  # malformed — attempt refresh anyway

        provider = canonical_provider_key(row.provider)
        data = await refresh_token_via_provider(
            provider,
            refresh_token,
            db=db,
        )
        if _is_permanent_refresh_error(data):
            _mark_integration_reauth_required(
                row,
                provider,
                _permanent_refresh_error(data),
            )
            changed = True
            continue
        if not data or not data.get("access_token"):
            continue
        new_creds = dict(creds)
        new_creds["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            new_creds["refresh_token"] = data["refresh_token"]
        if data.get("expires_in"):
            new_creds["expires_at"] = (
                now + timedelta(seconds=int(data["expires_in"]))
            ).isoformat()
        row.credentials = new_creds
        refreshed += 1
        changed = True

    if changed:
        await db.flush()
    return refreshed


async def _run_once_async() -> dict:
    from packages.core.database import async_session

    async with async_session() as db:
        user_count = await _refresh_oauth_accounts(db)
        entity_count = await _refresh_integrations(db)
        await db.commit()

    return {"user": user_count, "entity": entity_count}


# ── Celery task registration ───────────────────────────────────────────────
#
# Celery is imported lazily so the pure-async helpers above stay importable
# in environments that don't ship celery (e.g. test runners) — the module
# is still useful as a library even when the beat worker isn't running.

try:
    from packages.core.celery_app import celery_app

    @celery_app.task(name="oauth.refresh_tick")
    def oauth_refresh_tick() -> dict:
        """Refresh any OAuth tokens expiring in the next few minutes.

        Called by Celery beat every minute (see celery_app.py::beat_schedule).
        """
        try:
            from packages.core.tasks._runtime import run_in_worker
            result = run_in_worker(_run_once_async())
            logger.info(
                "oauth_refresh: refreshed %d user tokens + %d entity tokens",
                result["user"], result["entity"],
            )
            return result
        except Exception as e:
            logger.exception("oauth_refresh tick failed: %s", e)
            return {"error": str(e)}
except ImportError:
    # Celery not available — only the async helpers are usable.
    logger.debug("celery not installed; oauth.refresh_tick beat task disabled")
