"""End-user channel binding — generate + redeem one-time claim tokens.

Telegram is the only flow fully wired today; the schema and service are
channel-agnostic so adding email / WhatsApp is a matter of plugging the
provider-side deep-link / verification path in.

Why a separate service:
  - keeps the secret-token plumbing out of channel_gateway (which is
    already busy with messaging dispatch)
  - lets tests exercise claim semantics without standing up the whole
    inbound webhook stack
  - the redemption happens at *gateway* time (inside dispatch_inbound's
    pre-step) and the generation happens at *API* time — keeping both
    in one module makes the contract obvious to future maintainers.

Token semantics:
  - random 12 chars, base32-alphabet (no easy-to-confuse glyphs)
  - 15-minute TTL — enough for a user to switch from web to mobile and
    hit /start, not long enough for casual interception to be useful
  - single use: ``claimed_at`` is stamped on success and ``claim_token``
    refuses to mutate a row that's already claimed or expired
  - pinned to user_id + entity_id at generation, so even if the token
    leaks the worst case is binding the leaker's Telegram to the
    intended Manor account (still wrong, but bounded — we never
    elevate the leaker's identity)
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import (
    ChannelConfig,
    ChannelContact,
    ChannelLinkToken,
)

logger = logging.getLogger(__name__)


TOKEN_TTL = timedelta(minutes=15)
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I
_TOKEN_LENGTH = 12
_START_RE = re.compile(r"^\s*/start\s+([A-Z2-9]{6,32})\s*$", re.IGNORECASE)


@dataclass
class StartLinkResult:
    token: str
    expires_at: datetime
    channel_type: str
    deep_link: Optional[str]                # t.me/<bot>?start=<token>
    instructions: str
    bot_username: Optional[str] = None


def _generate_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def extract_start_token(content: str) -> Optional[str]:
    """Pull a /start token out of an inbound message, if present.

    Lives here (not in the gateway) so any channel adapter that mimics
    Telegram's deep-link convention can reuse the same regex.
    """
    if not isinstance(content, str) or not content:
        return None
    match = _START_RE.match(content)
    if match is None:
        return None
    return match.group(1).upper()


async def start_link(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    channel_type: str,
) -> StartLinkResult:
    """Mint a fresh claim token and return everything the UI needs to
    walk the user to the right place. Caller commits."""
    if channel_type != "telegram":
        # Future channels plug in here. We refuse upfront rather than
        # invent a deep-link template the producer can't honour.
        raise ValueError(f"channel_type={channel_type!r} linking not supported yet")

    # Look up an active ChannelConfig so we can build a deep link.
    cc = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.channel_type == channel_type,
            ChannelConfig.status == "active",
        ).order_by(ChannelConfig.updated_at.desc()).limit(1)
    )).scalar_one_or_none()
    if cc is None:
        raise ValueError(
            f"no active {channel_type} ChannelConfig in this entity — "
            "ask an admin to connect the bot first"
        )

    bot_username = (cc.config or {}).get("bot_username") if isinstance(cc.config, dict) else None
    if not bot_username and isinstance(cc.credentials, dict):
        bot_username = cc.credentials.get("bot_username")

    token = _generate_token()
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL

    row = ChannelLinkToken(
        token=token,
        user_id=user_id,
        entity_id=entity_id,
        channel_type=channel_type,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()

    deep_link = None
    if isinstance(bot_username, str) and bot_username:
        deep_link = f"https://t.me/{bot_username.lstrip('@')}?start={token}"
        instructions = (
            "Open the link, tap Start, and we'll bind this Telegram "
            "account to your Manor profile."
        )
    else:
        instructions = (
            "Open Telegram and send the bot this command:\n"
            f"  /start {token}\n"
            "We'll bind your Telegram account to your Manor profile."
        )
    return StartLinkResult(
        token=token,
        expires_at=expires_at,
        channel_type=channel_type,
        deep_link=deep_link,
        instructions=instructions,
        bot_username=bot_username if isinstance(bot_username, str) else None,
    )


@dataclass
class ClaimOutcome:
    ok: bool
    reason: Optional[str] = None
    user_id: Optional[str] = None
    contact_id: Optional[str] = None


async def claim_token(
    db: AsyncSession,
    *,
    token: str,
    contact: ChannelContact,
) -> ClaimOutcome:
    """Attempt to bind ``contact`` to the user that minted ``token``.

    Refuses on TTL expiry, single-use violation, channel_type mismatch,
    or cross-tenant entity mismatch. Returns a structured outcome so the
    gateway can compose a confirmation / error reply to the user without
    re-querying.

    Caller commits — we only ``flush`` so the gateway can roll the claim
    into its own transaction along with the inbound Message persistence.
    """
    row = (await db.execute(
        select(ChannelLinkToken).where(ChannelLinkToken.token == token).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return ClaimOutcome(ok=False, reason="token_not_found")

    if row.claimed_at is not None:
        return ClaimOutcome(ok=False, reason="token_already_used")

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return ClaimOutcome(ok=False, reason="token_expired")

    if row.channel_type != contact.channel_type:
        return ClaimOutcome(ok=False, reason="channel_type_mismatch")
    if row.entity_id != contact.entity_id:
        return ClaimOutcome(ok=False, reason="entity_mismatch")

    # Pull the verified user's role so the contact inherits authority
    # equivalent to the user's web-chat session.
    from packages.core.models.user import User

    user = (await db.execute(
        select(User).where(
            User.id == row.user_id,
            User.entity_id == row.entity_id,
            User.status == "active",
        )
    )).scalar_one_or_none()
    if user is None:
        return ClaimOutcome(ok=False, reason="user_inactive")

    contact.user_id = user.id
    contact.role = user.role or contact.role or "member"
    row.claimed_at = datetime.now(timezone.utc)
    row.claimed_contact_id = contact.id
    await db.flush()
    return ClaimOutcome(
        ok=True, user_id=user.id, contact_id=contact.id,
    )
