"""Resolve send-capable mailboxes the sender owns or was given use of."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import CredentialError, Requester, get_credential_service
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
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
                token = await sender_oauth_token(
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


async def _authorized_sender_row(
    db: AsyncSession, *, kind: str, connection_id: str, entity_id: str, user_id: str,
):
    """Load a mailbox row the sender owns or holds a ``use`` share for."""
    from packages.core.services.integration_access import resolve_integration_access

    decision = await resolve_integration_access(
        db, kind=kind, connection_id=connection_id,
        entity_id=entity_id, user_id=user_id, action="use",
    )
    if not decision.allowed:
        return None
    model = OAuthAccount if kind == "oauth_account" else Integration
    return (await db.execute(select(model).where(model.id == connection_id))).scalar_one_or_none()


async def sender_oauth_token(
    db: AsyncSession, *, sender_id: str, provider: str, entity_id: str, user_id: str, reason: str,
) -> str | None:
    """Lease one exact OAuth mailbox the sender may use, including Nango-backed rows."""
    if provider not in {"gmail", "outlook"}:
        return None
    if sender_id.startswith("oauth:"):
        account = await _authorized_sender_row(
            db, kind="oauth_account", connection_id=sender_id.removeprefix("oauth:"),
            entity_id=entity_id, user_id=user_id,
        )
        if not account or canonical_provider_key(account.provider) != provider or not oauth_account_is_runtime_usable(account):
            return None
        try:
            return lease_oauth_account_tokens(
                account, requester_id=user_id, requester_kind="user", reason=reason,
            ).get("access_token")
        except CredentialError:
            return None
    account = await _authorized_sender_row(
        db, kind="integration", connection_id=sender_id, entity_id=entity_id, user_id=user_id,
    )
    if (
        not account or account.entity_id != entity_id or account.status != "active"
        or canonical_provider_key(account.provider) != provider
    ):
        return None
    connection_id = entity_account_nango_connection_id(account)
    if connection_id:
        config_key = entity_account_nango_provider_config_key(account)
        if not nango_connection_matches_runtime_scope(
            connection_id=connection_id, entity_id=entity_id, owner_user_id=account.owner_user_id,
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


class ConnectedEmailSendResult(str, Enum):
    SENT = "sent"
    #: Use rights, credentials or the mailbox identity no longer match the
    #: pinned sender. Retrying cannot help until the owner reconnects it.
    SENDER_UNAVAILABLE = "sender_unavailable"
    #: The send failed but the mailbox was not shown to be lost: the provider
    #: refused this message, or the mailbox could not be checked (network).
    #: Retrying later can succeed.
    DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class _LeasedSender:
    provider: str
    address: str
    kind: str
    connection_id: str
    owner_user_id: str | None
    token: str | None = field(repr=False)
    credentials: dict | None = field(repr=False)


async def _lease_sender(
    *, sender: tuple[str, str, str], entity_id: str, user_id: str, reason: str,
) -> _LeasedSender | None:
    """Recheck use rights and lease credentials, then release the read transaction."""
    from packages.core import database

    sender_id, address, provider = sender
    kind = "oauth_account" if sender_id.startswith("oauth:") else "integration"
    connection_id = sender_id.removeprefix("oauth:")
    async with database.async_session() as db:
        account = await _authorized_sender_row(
            db, kind=kind, connection_id=connection_id, entity_id=entity_id, user_id=user_id,
        )
        if account is None:
            return None
        owner_user_id = account.user_id if kind == "oauth_account" else account.owner_user_id
        if provider in {"gmail", "outlook"}:
            token = await sender_oauth_token(
                db, sender_id=sender_id, provider=provider, entity_id=entity_id,
                user_id=user_id, reason=reason,
            )
            if not token:
                return None
            return _LeasedSender(provider, address, kind, connection_id, owner_user_id, token, None)
        if (
            kind != "integration" or account.entity_id != entity_id
            or account.provider != "email" or account.status != "active"
        ):
            return None
        try:
            credentials = get_credential_service().lease_integration(
                account, requester=Requester(kind="user", id=user_id), reason=reason,
            )
        except CredentialError:
            return None
    return _LeasedSender(provider, address, kind, connection_id, owner_user_id, None, credentials)


async def _deliver_with(
    leased: _LeasedSender, *, to: str, subject: str, content: str, sender_name: str,
    headers: dict[str, str] | None,
) -> bool:
    from packages.core.services import email_service

    kwargs = dict(to=to, subject=subject, content=content, sender_name=sender_name, expected_sender=leased.address)
    if headers:  # only when non-empty, so fixed-signature fakes keep working
        kwargs["headers"] = headers
    if leased.provider in {"gmail", "outlook"}:
        return await email_service.send_connected_oauth_email(leased.provider, leased.token, **kwargs)
    return await email_service.send_connected_email(leased.credentials, **kwargs)


async def _classify_failed_send(leased: _LeasedSender) -> ConnectedEmailSendResult:
    """Only a mailbox shown to be lost is unavailable; anything else is retryable."""
    from packages.core.services import email_service

    if leased.provider in {"gmail", "outlook"}:
        status = await email_service.connected_oauth_mailbox_status(
            leased.provider, leased.token, leased.address,
        )
    else:
        status = await email_service.connected_smtp_mailbox_status(leased.credentials or {}, leased.address)
    if status == "lost":
        return ConnectedEmailSendResult.SENDER_UNAVAILABLE
    return ConnectedEmailSendResult.DELIVERY_FAILED


def _record_use(leased: _LeasedSender, *, entity_id: str, user_id: str) -> None:
    from packages.core.services.connection_usage import record_shared_connection_use

    record_shared_connection_use(
        entity_id=entity_id, connection_kind=leased.kind, connection_id=leased.connection_id,
        owner_user_id=leased.owner_user_id, actor_user_id=user_id,
        server_key=leased.provider, tool_name="manor_email_send",
    )


async def send_email_from_account(
    *, sender: tuple[str, str, str], entity_id: str, user_id: str,
    to: str, subject: str, content: str, sender_name: str,
    reason: str = "connected_email_delivery",
) -> bool:
    """Recheck use rights and release the read transaction before sending."""
    leased = await _lease_sender(sender=sender, entity_id=entity_id, user_id=user_id, reason=reason)
    if leased is None:
        return False
    sent = await _deliver_with(leased, to=to, subject=subject, content=content, sender_name=sender_name, headers=None)
    if sent:
        _record_use(leased, entity_id=entity_id, user_id=user_id)
    return sent


async def send_email_from_account_result(
    *, sender: tuple[str, str, str], entity_id: str, user_id: str,
    to: str, subject: str, content: str, sender_name: str,
    reason: str = "connected_email_delivery",
    headers: dict[str, str] | None = None,
    send_timeout: float | None = None,
    classify_timeout: float | None = None,
) -> ConnectedEmailSendResult:
    """Send like ``send_email_from_account``, but say why a send did not happen.

    ``send_timeout`` bounds the lease and the send; running out raises
    ``TimeoutError`` (the message may have gone out). ``classify_timeout``
    bounds the check after a failed send; running out is DELIVERY_FAILED.
    """
    async with asyncio.timeout(send_timeout):
        leased = await _lease_sender(sender=sender, entity_id=entity_id, user_id=user_id, reason=reason)
        if leased is None:
            return ConnectedEmailSendResult.SENDER_UNAVAILABLE
        sent = await _deliver_with(
            leased, to=to, subject=subject, content=content, sender_name=sender_name, headers=headers,
        )
    if sent:
        _record_use(leased, entity_id=entity_id, user_id=user_id)
        return ConnectedEmailSendResult.SENT
    try:
        async with asyncio.timeout(classify_timeout):
            return await _classify_failed_send(leased)
    except TimeoutError:
        return ConnectedEmailSendResult.DELIVERY_FAILED
