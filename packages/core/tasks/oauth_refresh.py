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
  youtube         GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
  linkedin        LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
  github          GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
  twitter_x       X_CLIENT_ID / X_CLIENT_SECRET
  tiktok          TIKTOK_CLIENT_ID / TIKTOK_CLIENT_SECRET
  quickbooks      QUICKBOOKS_CLIENT_ID / QUICKBOOKS_CLIENT_SECRET

``stripe`` uses static API keys, not OAuth → skipped here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import CredentialDecryptError
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.services.oauth_account_credentials import (
    clear_oauth_account_tokens,
    lease_oauth_account_tokens,
    mark_oauth_account_credential_reconnect_required,
    store_oauth_account_tokens,
)
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases

logger = logging.getLogger(__name__)


# ── Provider config ─────────────────────────────────────────────────────────

_PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "manor_pms": {"client_id_env": "PMS_OAUTH_CLIENT_ID", "client_secret_env": "PMS_OAUTH_CLIENT_SECRET"},
    "manor_mcp_minutes": {"client_id_env": "MINUTES_OAUTH_CLIENT_ID", "client_secret_env": "MINUTES_OAUTH_CLIENT_SECRET"},
    "robinhood": {
        "token_url": "https://api.robinhood.com/oauth2/token/",
        "client_id_env": "ROBINHOOD_CLIENT_ID",
        "client_secret_env": "",
    },
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
    "youtube": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "linkedin": {
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "client_id_env": "LINKEDIN_CLIENT_ID",
        "client_secret_env": "LINKEDIN_CLIENT_SECRET",
    },
    "lark": {
        "token_url": "https://open.larksuite.com/open-apis/authen/v2/oauth/token",
        "client_id_env": "LARK_APP_ID",
        "client_secret_env": "LARK_APP_SECRET",
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
    "tiktok": {
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "client_id_env": "TIKTOK_CLIENT_ID",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
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
    *,
    expected_client_id: str | None = None,
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
    from packages.core.services.manor_integration_oauth import is_manor_oauth_provider

    if db is None:
        from packages.core.database import async_session
        async with async_session() as _db:
            return await refresh_token_via_provider(
                provider, refresh_token, db=_db, expected_client_id=expected_client_id,
            )

    provider = canonical_provider_key(provider)
    oauth_cfg = await resolve_oauth_config(db, provider)
    if not oauth_cfg:
        logger.debug(
            "oauth_refresh: %s client credentials not configured — skipping",
            provider,
        )
        return None
    if provider == "lark" and expected_client_id and oauth_cfg.client_id != expected_client_id:
        return {_PERMANENT_REFRESH_ERROR_KEY: {
            "error": "oauth_app_changed",
            "description": "The Lark app configuration changed; connect this account again.",
        }}

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
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, trust_env=False) as client:
            request_body = {"json": body} if provider == "lark" else {"data": body}
            resp = await client.post(oauth_cfg.token_url, headers=headers, **request_body)
    except Exception:
        logger.warning("oauth_refresh: %s token endpoint unreachable", provider)
        from packages.core.services.manor_integration_oauth import is_manor_oauth_provider

        if is_manor_oauth_provider(provider) or provider == "lark":
            return {_PERMANENT_REFRESH_ERROR_KEY: {"error": "refresh_outcome_unknown", "description": "OAuth refresh outcome is unknown; reconnect before further use."}}
        return None

    if not 200 <= resp.status_code < 300:
        permanent_error = _permanent_refresh_error_from_response(resp)
        logger.warning(
            "oauth_refresh: %s refresh returned HTTP %d",
            provider, resp.status_code,
        )
        if provider == "lark":
            return {_PERMANENT_REFRESH_ERROR_KEY: {
                "error": "invalid_grant" if permanent_error else "refresh_outcome_unknown",
                "description": "Lark OAuth refresh was not confirmed; reconnect before further use.",
            }}
        if permanent_error:
            from packages.core.services.manor_integration_oauth import is_manor_oauth_provider

            if is_manor_oauth_provider(provider):
                permanent_error = {"error": "invalid_grant", "description": "OAuth refresh was rejected; reconnect."}
            return {_PERMANENT_REFRESH_ERROR_KEY: permanent_error}
        if is_manor_oauth_provider(provider):
            return {_PERMANENT_REFRESH_ERROR_KEY: {"error": "refresh_outcome_unknown", "description": "OAuth refresh was not confirmed; reconnect before further use."}}
        return None

    try:
        data = resp.json()
    except Exception:
        if is_manor_oauth_provider(provider) or provider == "lark":
            return {_PERMANENT_REFRESH_ERROR_KEY: {"error": "invalid_refresh_response", "description": "OAuth refresh returned an invalid response; reconnect."}}
        return None
    if provider == "lark":
        from packages.core.services.lark_oauth import lark_token_response_is_valid

        if not lark_token_response_is_valid(data, scopes=oauth_cfg.scopes):
            return {_PERMANENT_REFRESH_ERROR_KEY: {
                "error": "invalid_refresh_response",
                "description": "Lark OAuth refresh returned an incomplete or rejected grant; reconnect.",
            }}
    if is_manor_oauth_provider(provider) and (
        not isinstance(data, dict)
        or not isinstance(data.get("access_token"), str) or not data["access_token"]
        or not data["access_token"].isascii() or any(char.isspace() for char in data["access_token"])
        or not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]
        or not isinstance(data.get("expires_in"), int) or isinstance(data["expires_in"], bool)
        or data["expires_in"] <= 0
        or not set(oauth_cfg.scopes.split()).issubset(str(data.get("scope") or "").split())
    ):
        return {_PERMANENT_REFRESH_ERROR_KEY: {"error": "invalid_refresh_response", "description": "OAuth refresh returned an incomplete grant; reconnect."}}
    return data


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
    from packages.core.services.manor_integration_oauth import is_manor_oauth_provider

    # Retain only encrypted grant material for an explicit remote Disconnect.
    # A failed/ambiguous rotating grant cannot execute or automatically refresh.
    if not is_manor_oauth_provider(provider):
        clear_oauth_account_tokens(row)
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
            or_(
                OAuthAccount.credential_ref.isnot(None),
                OAuthAccount.refresh_token.isnot(None),
            ),
            OAuthAccount.token_expires_at.isnot(None),
            OAuthAccount.token_expires_at <= deadline,
        ).limit(_MAX_PER_RUN)
    )).scalars().all()

    refreshed = 0
    changed = False
    for row in rows:
        provider = canonical_provider_key(row.provider)
        from packages.core.services.manor_integration_oauth import (
            is_manor_oauth_provider, manor_oauth_account_matches, manor_oauth_binding,
            verify_manor_oauth_account,
        )

        if provider == "lark":
            from packages.core.services.lark_oauth import lark_oauth_account_matches
            from packages.core.services.oauth_account_credentials import _oauth_account_requires_reconnect

            # Lark refresh tokens can be used only once. Keep the row locked
            # until the rotated encrypted grant is persisted, and recheck it.
            await db.refresh(row, with_for_update=True)
            if (
                row.token_expires_at is None or row.token_expires_at > deadline
                or _oauth_account_requires_reconnect(row)
            ):
                continue
            if not await lark_oauth_account_matches(db, row):
                _mark_oauth_account_reauth_required(row, provider, {
                    "error": "oauth_app_changed",
                    "description": "The Lark app configuration changed; connect this account again.",
                })
                changed = True
                continue

        if is_manor_oauth_provider(provider):
            # Serialize rotating grants with refresh and disconnect. Re-read
            # after the lock: another worker may already have refreshed it.
            await db.refresh(row, with_for_update=True)
            if not manor_oauth_account_matches(row):
                continue
            if row.token_expires_at is None or row.token_expires_at > deadline:
                continue
            from packages.core.services.integration_access import _is_active_entity_member

            if not await _is_active_entity_member(
                db, user_id=row.user_id, entity_id=manor_oauth_binding(row)["entity_id"],
            ):
                continue
        try:
            creds = lease_oauth_account_tokens(
                row,
                requester_id="oauth_refresh_task",
                reason="oauth.token.refresh",
            )
        except CredentialDecryptError:
            # A ciphertext rejected by the credential backend cannot be
            # refreshed. Mark just this account for reconnect and remove it
            # from the due-token scan so one bad row cannot abort every
            # account's minute-by-minute refresh tick.
            mark_oauth_account_credential_reconnect_required(
                row,
                provider=provider,
            )
            logger.warning(
                "oauth_refresh: account %s (%s) credentials could not be decrypted; "
                "marked for reconnect",
                row.id,
                provider,
            )
            changed = True
            continue
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            continue
        data = await refresh_token_via_provider(
            provider,
            refresh_token,
            db=db,
            **({"expected_client_id": (row.profile or {}).get("app_id")} if provider == "lark" else {}),
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
        if provider == "lark":
            from packages.core.services.oauth_flow import OAuthFlowError, TokenSet, resolve_oauth_identity

            try:
                refreshed_id, _ = await resolve_oauth_identity(
                    "lark", TokenSet(data["access_token"], data.get("refresh_token"), None, "", data),
                    application_id=(row.profile or {}).get("app_id"),
                )
                if refreshed_id != row.provider_user_id:
                    raise OAuthFlowError(502, "Lark identity changed")
            except OAuthFlowError:
                _mark_oauth_account_reauth_required(row, provider, {
                    "error": "identity_verification_failed",
                    "description": "The refreshed Lark OAuth grant could not be verified; reconnect.",
                })
                changed = True
                continue
        if is_manor_oauth_provider(provider):
            from packages.core.services.oauth_flow import OAuthFlowError

            try:
                await verify_manor_oauth_account(row, data["access_token"])
            except OAuthFlowError:
                _mark_oauth_account_reauth_required(row, provider, {
                    "error": "identity_verification_failed",
                    "description": "The refreshed OAuth grant could not be verified; reconnect.",
                })
                changed = True
                continue
        store_oauth_account_tokens(
            row,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or refresh_token,
            preserve_existing_refresh=False,
            requester_id="oauth_refresh_task",
        )
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
        # Lark's new user grants live only in encrypted OAuthAccount rows.
        # Legacy Integration rows retain their app/Feishu credentials.
        if canonical_provider_key(row.provider) == "lark":
            continue
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
