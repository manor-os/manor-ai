"""Audited encrypted access to personal OAuthAccount tokens."""
from __future__ import annotations

from sqlalchemy import or_

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.user import OAuthAccount


def oauth_account_has_credentials_clause():
    """SQL predicate supporting encrypted rows and rolling legacy backfill."""
    return or_(
        OAuthAccount.credential_ref.is_not(None),
        OAuthAccount.access_token.is_not(None),
    )


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
    ):
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


def clear_oauth_account_tokens(account: OAuthAccount) -> None:
    # Do not encrypt an empty object: a non-null reference would make rolling
    # compatibility queries treat the disconnected account as connected.
    account.credential_ref = None
    account.credential_scheme = "legacy_columns"
    account.access_token = None
    account.refresh_token = None
