"""Resolve send-capable mailboxes owned by the authenticated sender."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import CredentialError, Requester, get_credential_service
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    IntegrationAccountOwnership,
    RuntimeIntegrationAccountPreferenceFactory,
    entity_account_nango_connection_id,
    entity_account_nango_provider_config_key,
    load_runtime_integration_registry,
    nango_connection_matches_runtime_scope,
    set_default_runtime_integration_account,
)
from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens, oauth_account_is_runtime_usable
from packages.core.services.provider_keys import canonical_provider_key


class ConnectedEmailAccountsUnavailable(RuntimeError):
    """The sender's account registry could not be loaded reliably."""


async def available_email_senders(
    db: AsyncSession, *, entity_id: str, user_id: str, reason: str = "email_sender_check",
) -> list[tuple[str, str, str]]:
    from packages.core.services.email_service import connected_oauth_sender, connected_smtp_sender

    registry = await load_runtime_integration_registry(
        db, user_id=user_id, entity_id=entity_id, provider_keys=("email", "gmail", "outlook"),
    )
    if registry.load_errors:
        raise ConnectedEmailAccountsUnavailable("Your connected email accounts are temporarily unavailable")
    available = []
    for provider in ("email", "gmail", "outlook"):
        for item in registry.accounts_for(provider):
            if item.ownership is not IntegrationAccountOwnership.MINE or item.owner_user_id != user_id:
                continue
            sender_id = f"oauth:{item.id}" if item.kind is IntegrationAccountKind.OAUTH_ACCOUNT else item.id
            if provider == "email":
                if item.kind is not IntegrationAccountKind.INTEGRATION:
                    continue
                account = (await db.execute(select(Integration).where(Integration.id == item.id))).scalar_one_or_none()
                if account is None:
                    continue
                try:
                    credentials = get_credential_service().lease_integration(
                        account, requester=Requester(kind="user", id=user_id), reason=reason,
                    )
                    address = connected_smtp_sender(credentials)
                except (CredentialError, ValueError):
                    continue
            else:
                token = await owned_oauth_token(
                    db, sender_id=sender_id, provider=provider, entity_id=entity_id, user_id=user_id, reason=reason,
                )
                address = await connected_oauth_sender(provider, token) if token else None
                if not address:
                    continue
            available.append((sender_id, address, provider))
    return available


async def default_email_sender(
    db: AsyncSession, *, entity_id: str, user_id: str,
    available: list[tuple[str, str, str]],
) -> tuple[str, str, str] | None:
    """Use the common default; bootstrap older accounts from provider ordering.

    An explicitly chosen but disconnected sender never silently becomes another
    mailbox. Signature requests pin the result for later delivery.
    """
    preferences = (await db.execute(select(User.preferences).where(User.id == user_id))).scalar_one_or_none()
    preference = RuntimeIntegrationAccountPreferenceFactory.from_user_preferences(
        preferences, entity_id=entity_id, provider_keys=("email_sender",),
    ).get("email_sender")
    if preference is not None:
        sender_id = f"oauth:{preference.account_id}" if preference.kind is IntegrationAccountKind.OAUTH_ACCOUNT else preference.account_id
        return next((item for item in available if item[0] == sender_id), None)
    return available[0] if available else None


async def email_account_settings(db: AsyncSession, *, entity_id: str, user_id: str) -> dict:
    available = await available_email_senders(db, entity_id=entity_id, user_id=user_id)
    sender = await default_email_sender(db, entity_id=entity_id, user_id=user_id, available=available)
    return {
        "accounts": [
            {"id": sender_id, "email": address, "provider": provider, "is_default": bool(sender and sender_id == sender[0])}
            for sender_id, address, provider in available
        ],
        "default_sender_id": sender[0] if sender else None,
    }


async def set_default_email_sender(
    db: AsyncSession, *, entity_id: str, user_id: str, sender_id: str,
) -> bool:
    available = await available_email_senders(db, entity_id=entity_id, user_id=user_id)
    sender = next((item for item in available if item[0] == sender_id), None)
    if sender is None:
        return False
    return await set_default_runtime_integration_account(
        db, entity_id=entity_id, user_id=user_id, provider=sender[2],
        kind=IntegrationAccountKind.OAUTH_ACCOUNT if sender_id.startswith("oauth:") else IntegrationAccountKind.INTEGRATION,
        account_id=sender_id.removeprefix("oauth:"),
    )


async def owned_oauth_token(
    db: AsyncSession, *, sender_id: str, provider: str, entity_id: str, user_id: str, reason: str,
) -> str | None:
    """Lease one exact owned OAuth connection, including Nango-backed rows."""
    if provider not in {"gmail", "outlook"}:
        return None
    if sender_id.startswith("oauth:"):
        account = (await db.execute(select(OAuthAccount).where(
            OAuthAccount.id == sender_id.removeprefix("oauth:"), OAuthAccount.user_id == user_id,
        ))).scalar_one_or_none()
        if not account or canonical_provider_key(account.provider) != provider or not oauth_account_is_runtime_usable(account):
            return None
        try:
            return lease_oauth_account_tokens(
                account, requester_id=user_id, requester_kind="user", reason=reason,
            ).get("access_token")
        except CredentialError:
            return None
    account = (await db.execute(select(Integration).where(
        Integration.id == sender_id, Integration.entity_id == entity_id,
        Integration.owner_user_id == user_id, Integration.status == "active",
    ))).scalar_one_or_none()
    if not account or canonical_provider_key(account.provider) != provider:
        return None
    connection_id = entity_account_nango_connection_id(account)
    if connection_id:
        config_key = entity_account_nango_provider_config_key(account)
        if not nango_connection_matches_runtime_scope(
            connection_id=connection_id, entity_id=entity_id, owner_user_id=user_id,
            provider=provider, provider_config_key=config_key,
        ):
            return None
        from packages.core.ai.mcp.nango import get_nango_secret
        from packages.core.services.nango_bridge import fetch_nango_access_token

        secret = await get_nango_secret(db, entity_id)
        return await fetch_nango_access_token(
            secret=secret, provider_config_key=config_key, connection_id=connection_id,
        ) if secret else None
    try:
        credentials = get_credential_service().lease_integration(
            account, requester=Requester(kind="user", id=user_id), reason=reason,
        )
    except CredentialError:
        return None
    return credentials.get("access_token")


async def send_email_from_account(
    *, sender: tuple[str, str, str], entity_id: str, user_id: str,
    to: str, subject: str, content: str, sender_name: str,
    reason: str = "connected_email_delivery",
) -> bool:
    """Recheck ownership and release the read transaction before sending."""
    from packages.core import database
    from packages.core.services.email_service import send_connected_email, send_connected_oauth_email

    sender_id, address, provider = sender
    async with database.async_session() as db:
        if provider in {"gmail", "outlook"}:
            token = await owned_oauth_token(
                db, sender_id=sender_id, provider=provider, entity_id=entity_id,
                user_id=user_id, reason=reason,
            )
            credentials = None
        else:
            account = (await db.execute(select(Integration).where(
                Integration.id == sender_id, Integration.entity_id == entity_id,
                Integration.owner_user_id == user_id, Integration.provider == "email",
                Integration.status == "active",
            ))).scalar_one_or_none()
            if account is None:
                return False
            try:
                credentials = get_credential_service().lease_integration(
                    account, requester=Requester(kind="user", id=user_id), reason=reason,
                )
            except CredentialError:
                return False
            token = None
    kwargs = dict(to=to, subject=subject, content=content, sender_name=sender_name, expected_sender=address)
    if provider in {"gmail", "outlook"}:
        return bool(token) and await send_connected_oauth_email(provider, token, **kwargs)
    return await send_connected_email(credentials, **kwargs)
