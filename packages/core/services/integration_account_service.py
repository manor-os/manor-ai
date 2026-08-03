"""Resolve the connected account used for one integration operation.

The integration catalog supports both personal OAuth accounts and shared
entity-level credential rows.  This module gives discovery, permission checks,
and MCP dispatch one canonical ordering and selector contract.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.permissions import user_has_permission
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
)


@dataclass(frozen=True)
class RuntimeIntegrationAccount:
    id: str
    provider: str
    scope: str
    display_name: str
    is_default: bool
    oauth_account: OAuthAccount | None = None
    integration: Integration | None = None

    def public_option(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "scope": self.scope,
            "is_default": self.is_default,
        }


def _first_text(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def oauth_account_display_name(row: OAuthAccount) -> str:
    profile = row.profile or {}
    return _first_text(
        profile.get("email"),
        profile.get("display_name"),
        profile.get("name"),
        row.provider_user_id,
    ) or f"{row.provider} account {row.id[-6:]}"


def entity_account_display_name(row: Integration) -> str:
    cfg = row.config or {}
    profile = cfg.get("profile") if isinstance(cfg.get("profile"), dict) else {}
    legacy = row.credentials or {}
    return _first_text(
        cfg.get("name"),
        profile.get("email"),
        profile.get("display_name"),
        profile.get("name"),
        cfg.get("display_name"),
        cfg.get("email"),
        cfg.get("from_address"),
        cfg.get("from_email"),
        cfg.get("username"),
        cfg.get("phone_number"),
        cfg.get("url"),
        legacy.get("from_address"),
        legacy.get("from_email"),
        legacy.get("email"),
        legacy.get("username"),
        legacy.get("phone_number"),
        legacy.get("url"),
    ) or f"{row.provider} account {row.id[-6:]}"


def entity_account_has_credentials(row: Integration) -> bool:
    cfg = row.config or {}
    return bool(row.credentials or row.credential_ref or cfg.get("nango"))


async def list_runtime_integration_accounts(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider: str,
) -> list[RuntimeIntegrationAccount]:
    """Return every account the acting user may use for ``provider``.

    Personal OAuth accounts are listed before shared entity accounts. Within
    each scope, the explicit default comes first, followed by newest accounts.
    Secrets are never read or returned here.
    """
    provider = canonical_provider_key(provider)
    aliases = provider_key_aliases(provider)

    oauth_rows = list((await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user_id,
            OAuthAccount.provider.in_(aliases),
            OAuthAccount.access_token.is_not(None),
            OAuthAccount.access_token != "",
        ).order_by(OAuthAccount.created_at.desc())
    )).scalars().all())

    entity_rows = list((await db.execute(
        select(Integration).where(
            Integration.entity_id == entity_id,
            Integration.provider.in_(aliases),
            Integration.status == "active",
        ).order_by(Integration.created_at.desc())
    )).scalars().all())

    accounts: list[RuntimeIntegrationAccount] = []
    for row in oauth_rows:
        profile = row.profile or {}
        accounts.append(RuntimeIntegrationAccount(
            id=row.id,
            provider=provider,
            scope="user",
            display_name=oauth_account_display_name(row),
            is_default=bool(profile.get("is_default")),
            oauth_account=row,
        ))

    for row in entity_rows:
        if not entity_account_has_credentials(row):
            continue
        if row.required_permission and not await user_has_permission(
            db, user_id, entity_id, row.required_permission,
        ):
            continue
        cfg = row.config or {}
        accounts.append(RuntimeIntegrationAccount(
            id=row.id,
            provider=provider,
            scope="entity",
            display_name=entity_account_display_name(row),
            is_default=bool(cfg.get("is_default")),
            integration=row,
        ))

    return sorted(
        accounts,
        key=lambda account: (
            0 if account.scope == "user" else 1,
            0 if account.is_default else 1,
        ),
    )


def select_runtime_integration_account(
    accounts: list[RuntimeIntegrationAccount],
    selector: str | None = None,
) -> tuple[RuntimeIntegrationAccount | None, str | None]:
    """Select by exact connection ID, or use the ordered default."""
    value = str(selector or "").strip()
    if not value:
        return (accounts[0] if accounts else None), None

    for account in accounts:
        if account.id == value:
            return account, None
    return None, "The selected integration account is not connected or is not available to this user."
