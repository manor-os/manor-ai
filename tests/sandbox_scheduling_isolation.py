"""Keep a test's sandbox claims to the runners and tickets it created.

``claim_next_sandbox_allocation`` is global: it takes the fairest claimable
ticket in the database and places it on the least-loaded healthy runner. In a
CI shard the database also holds rows other tests committed (a healthy runner
with free slots, a queued ticket), so a test's claim can return a foreign
ticket, or place its own ticket on a foreign runner whose ``base_url`` the
in-process runner does not serve.

``fence_foreign_scheduling`` takes everything that already exists out of
placement: healthy runners are marked draining and claimable tickets are
deferred. Call it before the test creates its own runners and tickets.

Inside an uncommitted test transaction the fence is rolled back with the
test. A test that commits the fence calls ``lift_scheduling_fence`` with the
returned value in its teardown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.runtime_run import (
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)

_CLAIMABLE = (SandboxReservationStatus.QUEUED.value, SandboxReservationStatus.REQUEUED.value)
_DEFERRED_UNTIL = datetime(2999, 1, 1, tzinfo=timezone.utc)


@dataclass
class SchedulingFence:
    runner_ids: list[str] = field(default_factory=list)
    deferred: dict[str, datetime | None] = field(default_factory=dict)


async def fence_foreign_scheduling(db: AsyncSession) -> SchedulingFence:
    fence = SchedulingFence()
    fence.runner_ids = list(
        (
            await db.execute(
                select(SandboxRunner.id).where(SandboxRunner.status == SandboxRunnerStatus.HEALTHY.value)
            )
        ).scalars().all()
    )
    if fence.runner_ids:
        await db.execute(
            update(SandboxRunner)
            .where(SandboxRunner.id.in_(fence.runner_ids))
            .values(status=SandboxRunnerStatus.DRAINING.value)
        )
    rows = (
        await db.execute(
            select(SandboxReservation.id, SandboxReservation.next_attempt_at).where(
                SandboxReservation.status.in_(_CLAIMABLE)
            )
        )
    ).all()
    fence.deferred = {reservation_id: next_attempt_at for reservation_id, next_attempt_at in rows}
    if fence.deferred:
        await db.execute(
            update(SandboxReservation)
            .where(SandboxReservation.id.in_(list(fence.deferred)))
            .values(next_attempt_at=_DEFERRED_UNTIL)
        )
    await db.flush()
    return fence


async def lift_scheduling_fence(db: AsyncSession, fence: SchedulingFence) -> None:
    """Undo a committed fence, leaving rows that changed since untouched."""

    if fence.runner_ids:
        await db.execute(
            update(SandboxRunner)
            .where(
                SandboxRunner.id.in_(fence.runner_ids),
                SandboxRunner.status == SandboxRunnerStatus.DRAINING.value,
            )
            .values(status=SandboxRunnerStatus.HEALTHY.value)
        )
    for reservation_id, next_attempt_at in fence.deferred.items():
        await db.execute(
            update(SandboxReservation)
            .where(
                SandboxReservation.id == reservation_id,
                SandboxReservation.next_attempt_at == _DEFERRED_UNTIL,
            )
            .values(next_attempt_at=next_attempt_at)
        )
