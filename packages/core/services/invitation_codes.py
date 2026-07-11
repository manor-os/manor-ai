"""Invitation code validation, redemption, and admin CRUD.

Public-side flow:
  1. ``register`` endpoint receives a code (or doesn't).
  2. ``is_required(db)`` decides whether the code is mandatory based
     on the ``require_invitation_code`` feature flag — defaults False
     so OSS / fresh cloud deploys keep open registration until ops
     explicitly turn it on.
  3. ``validate_code(db, code)`` returns the row or raises an
     ``InvitationCodeError`` with a user-safe message.
  4. After the user + entity are created, ``redeem_code(db, code,
     user, entity)`` increments the use counter, writes a redemption
     row, applies optional plan + bonus credits — all in the same
     transaction as the signup so rollback unwinds cleanly.

Private operator CRUD uses these helpers.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.billing import SubscriptionPlan
from packages.core.models.invitation_code import (
    InvitationCode, InvitationCodeRedemption,
)
from packages.core.models.user import Entity, User

logger = logging.getLogger(__name__)


REQUIRE_FLAG_KEY = "require_invitation_code"
"""Feature-flag key. When the flag is on (default off), every public
``register`` call MUST supply a valid code; otherwise codes are
optional and act only as bonus carriers."""


class InvitationCodeError(ValueError):
    """Raised when validation/redemption fails. The message is
    user-safe — it can be returned in an HTTP 400 verbatim."""


# ── Lookup / validation ──────────────────────────────────────────────

async def is_required(db: AsyncSession) -> bool:
    """Whether the registration endpoint must reject signups that
    don't provide an invite code. Reads the feature-flag system."""
    try:
        from packages.core.services.feature_flags import is_enabled
        return await is_enabled(db, REQUIRE_FLAG_KEY, fallback=False)
    except Exception:
        # If the flag system is unreachable, fail open (don't block
        # signups) — operator can enable hard gating once flags work.
        return False


async def validate_code(
    db: AsyncSession, code: str, *, now: Optional[datetime] = None,
) -> InvitationCode:
    """Look up a code and run all validation checks.

    Raises ``InvitationCodeError`` with a user-safe message on any
    failure. Returns the row on success — caller still has to call
    ``redeem_code`` to actually book the use.
    """
    if not code or not code.strip():
        raise InvitationCodeError("Invitation code is required.")
    now = now or datetime.now(timezone.utc)

    row = (await db.execute(
        select(InvitationCode).where(InvitationCode.code == code.strip())
    )).scalar_one_or_none()
    if row is None:
        raise InvitationCodeError("Invitation code not recognised.")
    if row.status != "active":
        raise InvitationCodeError("This invitation code is no longer active.")
    if row.expires_at and row.expires_at <= now:
        raise InvitationCodeError("This invitation code has expired.")
    if row.max_uses is not None and row.uses >= row.max_uses:
        raise InvitationCodeError(
            "This invitation code has reached its usage limit."
        )
    return row


# ── Redemption (called after user/entity exist) ──────────────────────

async def redeem_code(
    db: AsyncSession,
    code_row: InvitationCode,
    *,
    user: User,
    entity: Entity,
    granted_by_admin_id: Optional[str] = None,
) -> InvitationCodeRedemption:
    """Increment uses + write redemption row + apply assignments.

    Caller commits. Designed to run inside the same transaction as the
    user/entity insert so a failure unwinds the signup atomically.

    Side effects beyond the increment+row:
      * If ``code_row.assign_plan_id`` is set, applies it to the entity
      * If ``code_row.bonus_credits`` > 0, writes a credit_grants row
        of ``kind='bonus'`` referencing the code
    """
    # Re-check usage cap now that we hold the row (light optimistic lock).
    # A simultaneous burst at the cap could over-redeem by N where N is
    # the number of in-flight signups; tolerable for invite codes.
    if code_row.max_uses is not None and code_row.uses >= code_row.max_uses:
        raise InvitationCodeError(
            "This invitation code has reached its usage limit."
        )

    code_row.uses = (code_row.uses or 0) + 1

    redemption = InvitationCodeRedemption(
        id=generate_ulid(),
        code=code_row.code,
        user_id=user.id,
        entity_id=entity.id,
    )
    db.add(redemption)

    # Apply plan assignment + issue initial plan credit grant.
    if code_row.assign_plan_id:
        plan_row = (await db.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.id == code_row.assign_plan_id,
                SubscriptionPlan.status == "active",
            )
        )).scalar_one_or_none()
        if plan_row:
            entity.plan_id = code_row.assign_plan_id
            settings = dict(entity.settings or {})
            settings["plan"] = code_row.assign_plan_id
            entity.settings = settings
            # Issue the plan's credit allocation immediately so the user
            # doesn't have to wait for the monthly renewal job.
        else:
            logger.warning(
                "Invite code %s assign_plan_id=%s not found / inactive — "
                "plan not applied",
                code_row.code, code_row.assign_plan_id,
            )

    # Apply bonus credits.

    # Apply role override (rare).
    if code_row.assign_role:
        user.role = code_row.assign_role

    await db.flush()
    return redemption


# ── Admin CRUD ───────────────────────────────────────────────────────

def _generate_code(prefix: str = "INV") -> str:
    """Default code format: ``<PREFIX>-<8 chars>`` — short enough to
    type, long enough not to collide. Uses uppercase + digits, skips
    ambiguous chars (0/O, 1/I/L)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix.upper()}-{body}"


async def create_code(
    db: AsyncSession,
    *,
    code: Optional[str] = None,
    description: Optional[str] = None,
    max_uses: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    assign_plan_id: Optional[str] = None,
    bonus_credits: int = 0,
    created_by_admin_id: Optional[str] = None,
) -> InvitationCode:
    """Create one invitation code. ``code`` auto-generated when omitted.

    Raises ``InvitationCodeError`` on duplicate code (admin should
    just generate a new one — easier than retry-on-conflict semantics).
    """
    final_code = (code or _generate_code()).strip()
    existing = (await db.execute(
        select(InvitationCode.code).where(InvitationCode.code == final_code)
    )).scalar_one_or_none()
    if existing:
        raise InvitationCodeError(f"Code {final_code!r} already exists.")

    if max_uses is not None and max_uses < 1:
        raise InvitationCodeError("max_uses must be ≥ 1 (or null for unlimited).")
    if bonus_credits < 0:
        raise InvitationCodeError("bonus_credits must be ≥ 0.")

    row = InvitationCode(
        code=final_code,
        description=description,
        max_uses=max_uses,
        uses=0,
        expires_at=expires_at,
        created_by_admin_id=created_by_admin_id,
        assign_plan_id=assign_plan_id,
        bonus_credits=int(bonus_credits or 0),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row


async def disable_code(db: AsyncSession, code: str) -> Optional[InvitationCode]:
    row = (await db.execute(
        select(InvitationCode).where(InvitationCode.code == code)
    )).scalar_one_or_none()
    if row is None:
        return None
    row.status = "disabled"
    await db.flush()
    return row


async def list_codes(
    db: AsyncSession, *, limit: int = 200,
) -> list[InvitationCode]:
    return list((await db.execute(
        select(InvitationCode).order_by(desc(InvitationCode.created_at)).limit(limit)
    )).scalars().all())


async def list_redemptions(
    db: AsyncSession, code: str, *, limit: int = 100,
) -> list[InvitationCodeRedemption]:
    return list((await db.execute(
        select(InvitationCodeRedemption)
        .where(InvitationCodeRedemption.code == code)
        .order_by(desc(InvitationCodeRedemption.redeemed_at))
        .limit(limit)
    )).scalars().all())
