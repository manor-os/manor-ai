"""Invitation codes for controlled signup.

Two tables:
  * ``invitation_codes`` — one row per code. The code string IS the
    primary key (no surrogate ULID) — codes are short, human-friendly
    strings shared in announcements / emails / Twitter, and lookup is
    always by the code itself.
  * ``invitation_code_redemptions`` — append-only record of who used
    which code (one row per signup). Lets reporting answer
    "how many signups did the LAUNCH-2026 code drive?".

Codes can carry intent — admins set ``assign_plan_id`` and/or
``bonus_credits`` so a code redemption pre-provisions the new tenant
with the right plan + a starter credit grant. Pure-gating codes leave
those fields empty.

Enforcement of "registration requires a code" lives at the route
layer — checked via the existing feature-flag system (look up
``require_invitation_code`` in ``feature_flags``). No env var needed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class InvitationCode(Base, TimestampMixin):
    """Reusable invite code. ``code`` is the primary key."""
    __tablename__ = "invitation_codes"
    __table_args__ = (
        Index("ix_invitation_codes_status", "status"),
        Index("ix_invitation_codes_expires", "expires_at"),
    )

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    """The actual code string. Case-insensitive at the lookup layer
    (we lowercase before query) but stored as the admin typed it for
    display."""

    description: Mapped[Optional[str]] = mapped_column(Text)
    """Internal note — never shown to the user being invited."""

    max_uses: Mapped[Optional[int]] = mapped_column(Integer)
    """Null = unlimited. Codes for one-off VIP invites typically set 1."""

    uses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    """Counter — incremented on successful redemption inside the same
    transaction as the redemption row insert, so a high-concurrency
    burst can't oversubscribe."""

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_by_admin_id: Mapped[Optional[str]] = mapped_column(String(26))
    """Admin user_id who issued the code. Null only for codes seeded
    via env var or migration."""

    assign_role: Mapped[Optional[str]] = mapped_column(String(20))
    """If set, the new owner User gets this role instead of the default
    'owner'. Mostly unused — kept for forward compat (e.g. future
    'guest' invites)."""

    assign_plan_id: Mapped[Optional[str]] = mapped_column(String(26))
    """If set, the new entity gets ``Entity.plan_id = assign_plan_id``
    immediately on signup. Lets a 'PRO-LAUNCH' code give signers a
    pro plan straight off the bat."""

    bonus_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    """If > 0, a ``credit_grants`` row is written on redemption with
    ``kind='bonus'`` and reason ``Invite code <code> bonus``."""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active",
    )
    # 'active' | 'disabled'


class InvitationCodeRedemption(Base):
    """Append-only ledger of code → user redemptions."""
    __tablename__ = "invitation_code_redemptions"
    __table_args__ = (
        Index("ix_invite_redemptions_code", "code", "redeemed_at"),
        Index("ix_invite_redemptions_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
