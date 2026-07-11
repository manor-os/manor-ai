"""Operator-initiated DM channel pairing.

Flow:

  Manor UI                                              DM bot
  --------                                              ------
  POST /channels/pair → create_pairing_code()
       returns code 'A1B2C3'                            …
       displayed to operator
                                                        operator types
                                                        '/pair A1B2C3'
                                                        bot webhook calls
                                                        redeem_pairing_code('A1B2C3', address)
                                                            └── creates Channel row
                                                            └── marks code consumed
                                                        bot replies "✓ paired"

The code itself is 6 ASCII chars (uppercase + digits, no I/0/O/1)
to stay typeable in any keyboard layout. TTL is 5 minutes — long
enough for slow operators, short enough that an exposed code is
unlikely to be useful by the time it's leaked.

We never expose codes via API after creation: the only legitimate
holder is the operator who just generated it. Lookups are by code+TTL
only; we don't index on entity_id (so a bot can redeem without knowing
the workspace ahead of time).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.channel_pairing import ChannelPairingCode
from packages.core.models.document import Channel

logger = logging.getLogger(__name__)


CODE_TTL_SECONDS = 5 * 60
CODE_LENGTH = 6
# Skipped chars: 0/O/1/I — too easy to misread on small phone screens.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ── Errors ──────────────────────────────────────────────────────────

class PairingError(Exception):
    """Base for all pairing failures."""


class PairingExpired(PairingError):
    """Code TTL elapsed or already redeemed."""


class PairingMismatch(PairingError):
    """Code is valid but channel_type doesn't match what the bot reported.
    Surfaced separately so the bot can give a clearer error than 'expired'.
    """


# ── Create ──────────────────────────────────────────────────────────

async def create_pairing_code(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_type: str,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    hint: Optional[str] = None,
    ttl_seconds: int = CODE_TTL_SECONDS,
) -> ChannelPairingCode:
    """Mint a fresh pairing code. Caller commits.

    Retries on collision until we get an unused code (vanishingly rare
    inside the 5-minute TTL window — the alphabet is 32^6 ≈ 1B keys
    against typical low-volume usage)."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    for _ in range(8):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
        existing = (await db.execute(
            select(ChannelPairingCode).where(ChannelPairingCode.code == code)
        )).scalar_one_or_none()
        if existing is not None and existing.consumed_at is None and _as_aware(existing.expires_at) > datetime.now(timezone.utc):
            continue
        if existing is not None:
            # Stale row — overwrite via delete + insert in one txn.
            await db.delete(existing)
            await db.flush()
        row = ChannelPairingCode(
            code=code,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            channel_type=channel_type,
            expires_at=expires_at,
            hint=hint,
        )
        db.add(row)
        await db.flush()
        return row
    raise PairingError("could not allocate a unique pairing code after 8 tries")


# ── Redeem ──────────────────────────────────────────────────────────

async def redeem_pairing_code(
    db: AsyncSession,
    *,
    code: str,
    channel_type: str,
    address: str,
    address_kind: str = "chat_id",
    display_name: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Channel:
    """Look up the code, validate freshness + channel_type, and create
    a ``Channel`` row binding the operator's external address. Caller commits.

    ``address`` is whatever the channel uses to identify the user
    (Telegram chat_id, WhatsApp phone, iMessage handle, etc.).

    Raises ``PairingExpired`` on TTL miss or already-consumed; raises
    ``PairingMismatch`` if the code was generated for a different
    channel_type than the bot is reporting (defends against confused-deputy
    attacks where one bot redeems another bot's code).
    """
    row = (await db.execute(
        select(ChannelPairingCode).where(ChannelPairingCode.code == code)
    )).scalar_one_or_none()
    if row is None:
        raise PairingExpired(f"no such code: {code!r}")
    if row.consumed_at is not None:
        raise PairingExpired(f"code {code!r} already redeemed")
    if _as_aware(row.expires_at) <= datetime.now(timezone.utc):
        raise PairingExpired(f"code {code!r} expired at {row.expires_at.isoformat()}")
    if row.channel_type != channel_type:
        raise PairingMismatch(
            f"code {code!r} was issued for {row.channel_type!r}, "
            f"got {channel_type!r}"
        )

    # Spawn the Channel binding.
    channel = Channel(
        id=generate_ulid(),
        entity_id=row.entity_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        type=channel_type,
        name=display_name or row.hint,
        agent_id=agent_id,
        config={address_kind: address, "paired_via_code": code},
        status="active",
    )
    db.add(channel)
    await db.flush()

    row.consumed_at = datetime.now(timezone.utc)
    row.created_channel_id = channel.id
    return channel


def _as_aware(dt: datetime) -> datetime:
    """Some DB drivers (async sqlite, older psycopg) drop tzinfo on
    read. Treat naive timestamps as UTC since that's how we always write."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
