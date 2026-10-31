"""Credit reservations for asynchronous work.

Reservations make pending provider work visible to credit gates before the
final provider cost is written to ``credit_usage_logs``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.billing import CreditReservation
from packages.core.models.user import Entity
from packages.core.constants.plans import ai_credit_limits_enabled


RESERVATION_STATUS_ACTIVE = "active"
RESERVATION_STATUS_CONSUMED = "consumed"
RESERVATION_STATUS_RELEASED = "released"
RESERVATION_STATUS_EXPIRED = "expired"

RESERVATION_STATUSES = {
    RESERVATION_STATUS_ACTIVE,
    RESERVATION_STATUS_CONSUMED,
    RESERVATION_STATUS_RELEASED,
    RESERVATION_STATUS_EXPIRED,
}

DEFAULT_RESERVATION_TTL_SECONDS = int(
    os.getenv("CREDIT_RESERVATION_TTL_SECONDS", "7200") or 7200
)


class CreditReservationError(ValueError):
    kind = "credit"


TRIAL_CREDIT_MESSAGE = (
    "The provider's credits for this trial are used up. Ask the provider to add "
    "credits, or accept the delivery to continue on your own plan."
)


class TrialCreditReservationError(CreditReservationError):
    """The provider paying for a client delivery trial cannot cover the spend."""

    kind = "trial_credit"

    def __init__(self, message: str = TRIAL_CREDIT_MESSAGE):
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_expires_at(now: datetime) -> datetime:
    return now + timedelta(seconds=max(60, DEFAULT_RESERVATION_TTL_SECONDS))


async def active_reserved_credits(
    db: AsyncSession,
    entity_id: str,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Return active, non-expired reserved credits for an entity."""
    now = now or _now()
    total = (await db.execute(
        select(func.coalesce(func.sum(CreditReservation.amount_credits), 0))
        .where(
            CreditReservation.entity_id == entity_id,
            CreditReservation.status == RESERVATION_STATUS_ACTIVE,
            (
                (CreditReservation.expires_at.is_(None))
                | (CreditReservation.expires_at > now)
            ),
        )
    )).scalar_one()
    return int(total or 0)


async def _lock_entity(db: AsyncSession, entity_id: str) -> None:
    entity_row = (await db.execute(
        select(Entity.id)
        .where(Entity.id == entity_id)
        .with_for_update()
    )).scalar_one_or_none()
    if not entity_row:
        raise CreditReservationError(f"entity {entity_id!r} not found")


async def _reservation_payer(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str],
    payer_entity_id: Optional[str],
) -> str:
    """Return the entity charged for a reservation by ``entity_id``.

    ``payer_entity_id`` is a payer pinned by the caller; without one the
    Workspace's current payer is resolved.
    """
    from packages.core.services.billing_payer import (
        TrialSponsorUnavailable,
        payer_entity_id as resolve_payer,
        validate_payer,
    )

    if payer_entity_id is None:
        try:
            return await resolve_payer(db, entity_id, workspace_id)
        except TrialSponsorUnavailable as exc:
            raise TrialCreditReservationError() from exc
    if payer_entity_id == entity_id:
        return payer_entity_id
    try:
        await validate_payer(
            db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=payer_entity_id,
        )
    except ValueError as exc:
        raise CreditReservationError(str(exc)) from exc
    payer = await db.get(Entity, payer_entity_id)
    if payer is None or payer.deleted_at is not None:
        raise TrialCreditReservationError()
    return payer_entity_id


def _insufficient_credits(meta: dict, required: int, available: int) -> CreditReservationError:
    # A reservation held for another entity's data is a sponsored trial's.
    if meta.get("data_owner_entity_id"):
        return TrialCreditReservationError()
    return CreditReservationError(
        f"insufficient credits: required {required}, available {available}"
    )


async def reservation_payer_by_source(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
) -> str | None:
    """Return the entity a source's reservation charges (plain read, no lock)."""
    row = await _reservation_by_source(db, source_kind=source_kind, source_id=source_id)
    return row.entity_id if row is not None else None


async def _reservation_by_id(
    db: AsyncSession,
    reservation_id: str,
    *,
    lock: bool = False,
) -> CreditReservation | None:
    stmt = select(CreditReservation).where(CreditReservation.id == reservation_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _reservation_by_source(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    lock: bool = False,
    active_only: bool = False,
) -> CreditReservation | None:
    stmt = select(CreditReservation).where(
        CreditReservation.source_kind == source_kind,
        CreditReservation.source_id == source_id,
    )
    if active_only:
        stmt = stmt.where(CreditReservation.status == RESERVATION_STATUS_ACTIVE)
    stmt = stmt.order_by(
        (CreditReservation.status == RESERVATION_STATUS_ACTIVE).desc(),
        CreditReservation.created_at.desc(),
        CreditReservation.id.desc(),
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


async def reserve_credits(
    db: AsyncSession,
    *,
    entity_id: str,
    amount_credits: int,
    source_kind: str,
    source_id: str,
    reason: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    workspace_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    payer_entity_id: Optional[str] = None,
) -> CreditReservation:
    """Reserve credits for pending async work.

    Caller must commit. ``entity_id`` owns the work's data; the reservation
    is held against the payer (``payer_entity_id``, or the Workspace's
    current payer when omitted), whose Entity row is locked so concurrent
    reservations see a serialized available balance. When the payer differs,
    ``meta.data_owner_entity_id`` records the data owner.
    """
    amount = int(amount_credits or 0)
    if amount <= 0:
        raise CreditReservationError("amount_credits must be positive")
    if not source_kind or not source_id:
        raise CreditReservationError("source_kind and source_id are required")

    payer = await _reservation_payer(
        db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=payer_entity_id,
    )
    meta = dict(metadata or {})
    if payer != entity_id:
        meta["data_owner_entity_id"] = entity_id
    await _lock_entity(db, payer)

    existing = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        lock=True,
    )
    if existing and (existing.meta or {}).get("cancelled_before_reserve"):
        if existing.entity_id != payer or existing.user_id != user_id:
            raise CreditReservationError("reservation owner mismatch")
        return existing
    if existing and existing.status == RESERVATION_STATUS_ACTIVE:
        if int(existing.amount_credits or 0) == amount:
            return existing
        raise CreditReservationError(
            f"active reservation already exists for {source_kind}:{source_id}"
        )

    if ai_credit_limits_enabled():
        pass

    now = _now()
    row = CreditReservation(
        id=generate_ulid(),
        entity_id=payer,
        workspace_id=workspace_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        amount_credits=amount,
        consumed_credits=0,
        source_kind=source_kind,
        source_id=source_id,
        status=RESERVATION_STATUS_ACTIVE,
        reason=reason,
        meta=meta,
        expires_at=expires_at or _default_expires_at(now),
    )
    db.add(row)
    await db.flush()
    return row


async def consume_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    consumed_credits: Optional[int] = None,
) -> CreditReservation:
    row = await _reservation_by_id(db, reservation_id, lock=True)
    if row is None:
        raise CreditReservationError(f"reservation {reservation_id!r} not found")
    return await _consume_row(row, consumed_credits=consumed_credits)


async def cancel_reservation_by_source(
    db: AsyncSession,
    *,
    entity_id: str,
    source_kind: str,
    source_id: str,
    workspace_id: Optional[str],
    user_id: Optional[str] = None,
    payer_entity_id: Optional[str] = None,
) -> CreditReservation:
    """Cancel a durable request, including one whose reserve has not arrived.

    The caller commits and must treat a returned released row as terminal.
    This records a zero-credit tombstone, never a charge or a temporary hold.
    Resolve/pin the same payer as reservation creation before serializing it.
    """
    payer = await _reservation_payer(
        db, entity_id=entity_id, workspace_id=workspace_id, payer_entity_id=payer_entity_id,
    )
    await _lock_entity(db, payer)
    row = await _reservation_by_source(db, source_kind=source_kind, source_id=source_id, lock=True)
    if row is not None:
        if row.entity_id != payer or row.user_id != user_id:
            raise CreditReservationError("reservation owner mismatch")
        return await _release_row(row, reason="provider_did_not_complete")
    meta = {"cancelled_before_reserve": True}
    if payer != entity_id:
        meta["data_owner_entity_id"] = entity_id
    row = CreditReservation(
        id=generate_ulid(), entity_id=payer, workspace_id=workspace_id, user_id=user_id,
        source_kind=source_kind, source_id=source_id, amount_credits=0, consumed_credits=0,
        status=RESERVATION_STATUS_RELEASED, released_at=_now(), meta=meta,
    )
    db.add(row)
    await db.flush()
    return row


async def consume_reservation_by_source(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    consumed_credits: Optional[int] = None,
) -> CreditReservation | None:
    row = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        lock=True,
        active_only=True,
    )
    if row is None:
        return None
    return await _consume_row(row, consumed_credits=consumed_credits)


async def resize_reservation_by_source(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    amount_credits: int,
    allow_overdraft: bool = False,
) -> CreditReservation | None:
    """Update an active reservation after the provider reports actual usage.

    ``allow_overdraft`` is only for work that has already reached the provider.
    Keeping its actual cost reserved is safer than allowing a later request to
    proceed when the final ledger write needs to be retried.
    """
    amount = int(amount_credits or 0)
    if amount <= 0:
        raise CreditReservationError("amount_credits must be positive")

    row = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        active_only=True,
    )
    if row is None:
        return None

    await _lock_entity(db, row.entity_id)
    row = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        lock=True,
        active_only=True,
    )
    if row is None:
        return None

    if not allow_overdraft and ai_credit_limits_enabled():
        pass

    row.amount_credits = amount
    return row


async def _consume_row(
    row: CreditReservation,
    *,
    consumed_credits: Optional[int],
) -> CreditReservation:
    if row.status == RESERVATION_STATUS_CONSUMED:
        return row
    if row.status != RESERVATION_STATUS_ACTIVE:
        return row
    row.status = RESERVATION_STATUS_CONSUMED
    row.consumed_credits = int(
        row.amount_credits if consumed_credits is None else consumed_credits
    )
    row.consumed_at = _now()
    return row


async def release_reservation(
    db: AsyncSession,
    *,
    reservation_id: str,
    reason: Optional[str] = None,
) -> CreditReservation:
    row = await _reservation_by_id(db, reservation_id, lock=True)
    if row is None:
        raise CreditReservationError(f"reservation {reservation_id!r} not found")
    return await _release_row(row, reason=reason)


async def release_reservation_by_source(
    db: AsyncSession,
    *,
    source_kind: str,
    source_id: str,
    reason: Optional[str] = None,
) -> CreditReservation | None:
    row = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        lock=True,
        active_only=True,
    )
    if row is None:
        return None
    return await _release_row(row, reason=reason)


async def _release_row(
    row: CreditReservation,
    *,
    reason: Optional[str],
) -> CreditReservation:
    if row.status != RESERVATION_STATUS_ACTIVE:
        return row
    row.status = RESERVATION_STATUS_RELEASED
    row.released_at = _now()
    if reason:
        meta = dict(row.meta or {})
        meta["release_reason"] = reason
        row.meta = meta
    return row
