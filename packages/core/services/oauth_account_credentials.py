"""Audited encrypted access to personal OAuthAccount tokens."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, func, not_, or_

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.user import OAuthAccount


def oauth_account_has_credentials_clause():
    """SQL predicate supporting encrypted rows and rolling legacy backfill."""
    return or_(
        OAuthAccount.credential_ref.is_not(None),
        OAuthAccount.access_token.is_not(None),
    )


def oauth_account_is_runtime_usable_clause():
    """Credentials exist and the account has not been marked for reconnect."""
    return and_(
        oauth_account_has_credentials_clause(),
        or_(
            OAuthAccount.token_expires_at.is_(None),
            OAuthAccount.token_expires_at >= func.now(),
        ),
        not_(
            OAuthAccount.profile.contains(
                {"oauth_refresh": {"reauth_required": True}}
            )
        ),
    )


def oauth_account_is_runtime_usable(account: OAuthAccount) -> bool:
    """In-memory counterpart to ``oauth_account_is_runtime_usable_clause``."""
    return bool(
        (account.credential_ref or account.access_token)
        and _oauth_account_token_is_current(account)
        and not _oauth_account_requires_reconnect(account)
    )


def _oauth_account_token_is_current(account: OAuthAccount) -> bool:
    expires_at = account.token_expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return expires_at >= now


def _oauth_account_requires_reconnect(account: OAuthAccount) -> bool:
    profile = account.profile if isinstance(account.profile, dict) else {}
    refresh_state = profile.get("oauth_refresh")
    return bool(
        isinstance(refresh_state, dict)
        and refresh_state.get("reauth_required")
    )


def mark_oauth_account_credential_reconnect_required(
    account: OAuthAccount,
    *,
    provider: str,
    checked_at: str | None = None,
) -> dict[str, object]:
    """Mark unreadable ciphertext without discarding potentially recoverable data."""
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    detail = "Stored OAuth credentials could not be decrypted; reconnect this account."
    profile = dict(account.profile or {})
    profile["oauth_refresh"] = {
        "reauth_required": True,
        "provider": provider,
        "error": "credential_decrypt_failed",
        "description": detail,
        "status_code": None,
        "checked_at": checked_at,
    }
    health = {
        "ok": False,
        "detail": detail,
        "latency_ms": 0.0,
        "checked_at": checked_at,
    }
    profile["last_health_check"] = health
    account.profile = profile
    account.token_expires_at = None
    return health


def lease_oauth_account_tokens(
    account: OAuthAccount,
    *,
    requester_id: str,
    reason: str,
    requester_kind: str = "system",
) -> dict[str, str]:
    return get_credential_service().lease_oauth_account(
        account,
        requester=Requester(kind=requester_kind, id=requester_id),
        reason=reason,
    )


def store_oauth_account_tokens(
    account: OAuthAccount,
    *,
    access_token: str | None,
    refresh_token: str | None,
    preserve_existing_refresh: bool = True,
    requester_id: str = "oauth_callback",
) -> None:
    existing: dict[str, str] = {}
    if preserve_existing_refresh and not refresh_token and (
        account.credential_ref or account.refresh_token
    ) and not _oauth_account_requires_reconnect(account):
        existing = lease_oauth_account_tokens(
            account,
            requester_id=requester_id,
            reason="oauth.token.rotate_preserve_refresh",
        )
    payload = {
        "access_token": access_token or existing.get("access_token"),
        "refresh_token": refresh_token or existing.get("refresh_token"),
    }
    get_credential_service().store_oauth_account(
        account,
        {key: value for key, value in payload.items() if value},
    )
    if access_token or refresh_token:
        profile = dict(account.profile or {})
        profile.pop("oauth_refresh", None)
        profile.pop("last_health_check", None)
        account.profile = profile


def clear_oauth_account_tokens(account: OAuthAccount) -> None:
    # Do not encrypt an empty object: a non-null reference would make rolling
    # compatibility queries treat the disconnected account as connected.
    account.credential_ref = None
    account.credential_scheme = "legacy_columns"
    account.access_token = None
    account.refresh_token = None
