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
from packages.core.constants.plans import is_cloud


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
    pass


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
) -> CreditReservation:
    """Reserve credits for pending async work.

    Caller must commit. The entity row is locked so concurrent reservations for
    the same entity see a serialized available balance.
    """
    amount = int(amount_credits or 0)
    if amount <= 0:
        raise CreditReservationError("amount_credits must be positive")
    if not source_kind or not source_id:
        raise CreditReservationError("source_kind and source_id are required")

    await _lock_entity(db, entity_id)

    existing = await _reservation_by_source(
        db,
        source_kind=source_kind,
        source_id=source_id,
        lock=True,
    )
    if existing and existing.status == RESERVATION_STATUS_ACTIVE:
        if int(existing.amount_credits or 0) == amount:
            return existing
        raise CreditReservationError(
            f"active reservation already exists for {source_kind}:{source_id}"
        )

    if is_cloud():
        pass

    now = _now()
    row = CreditReservation(
        id=generate_ulid(),
        entity_id=entity_id,
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
        meta=dict(metadata or {}),
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
