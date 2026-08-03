"""Database-level execution claim on a work lease.

A Celery message can be delivered more than once — that is a property of the
transport, not a bug we can configure away. ``broker_transport_options``'
visibility timeout makes it rare; ``task_reject_on_worker_lost=True`` makes it
*deliberate* when a worker dies mid-flight. So the executing side has to be
safe under a second delivery, and "read the lease, check ``status == 'active'``,
then execute" is not: two deliveries can both pass the read before either
writes.

The fix is to stop asking a question and start taking a right. Execution rights
live in one row (``work_leases``) behind one conditional ``UPDATE ... RETURNING``:

    UPDATE work_leases SET execution_claim_id = <mine>
     WHERE id = <lease> AND status = 'active'
       AND (claim is free OR the holder's heartbeat has lapsed)
    RETURNING id

Postgres serialises concurrent updates of the same row and re-evaluates the
WHERE clause after the lock is granted, so of N simultaneous deliveries exactly
one gets a row back. Everyone else exits as a clean no-op without touching the
step.

**When is a held claim reclaimable?** Only when the holder's heartbeat has
lapsed — never on a guessed time window. The heartbeat thread
(``packages/core/workers/internal.py``) advances ``last_heartbeat_at`` and
``lease_until`` in a single statement every interval, so a live holder always
has ``lease_until`` in the future and a dead one does not. ``lease_until`` is
therefore the heartbeat's own liveness deadline, carried per row with that
lease's own TTL rather than a constant hard-coded here: a claim is stale
exactly when the heartbeat that was maintaining it stopped for longer than the
TTL it was maintaining.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


_CLAIM_SQL = text(
    """
    UPDATE work_leases
       SET execution_claim_id = :claim_id,
           execution_claimed_at = :now
     WHERE id = :lease_id
       AND status = 'active'
       AND (
             execution_claim_id IS NULL          -- nobody is executing it
          OR execution_claim_id = :claim_id      -- idempotent re-claim by me
          OR lease_until <= :now                 -- holder's heartbeat lapsed
       )
    RETURNING id
    """
)

_RELEASE_SQL = text(
    """
    UPDATE work_leases
       SET execution_claim_id = NULL,
           execution_claimed_at = NULL
     WHERE id = :lease_id
       AND execution_claim_id = :claim_id
    RETURNING id
    """
)

_INSPECT_SQL = text(
    """
    SELECT status, execution_claim_id
      FROM work_leases
     WHERE id = :lease_id
    """
)


#: The claim was granted.
CLAIM_GRANTED = "granted"
#: The lease row is gone.
CLAIM_DENIED_MISSING = "lease_missing"
#: The lease is no longer ``active`` — completed, failed, expired or released.
CLAIM_DENIED_NOT_ACTIVE = "lease_not_active"
#: Another delivery holds the claim and its heartbeat is still live.
CLAIM_DENIED_HELD = "claim_held_by_live_execution"


@dataclass(frozen=True)
class ExecutionClaim:
    """Outcome of one attempt to take execution rights on a lease."""

    lease_id: str
    claim_id: str
    granted: bool
    reason: str

    def __bool__(self) -> bool:  # `if claim:` reads naturally at call sites
        return self.granted


async def claim_lease_for_execution(
    db: AsyncSession, lease_id: str, *, claim_id: str,
) -> ExecutionClaim:
    """Take execution rights on ``lease_id``, or report why we could not.

    The caller MUST commit — the claim only exists for other deliveries once
    the transaction lands. The denial reason costs one extra SELECT and is
    only paid on the (rare) denial path.
    """
    now = datetime.now(timezone.utc)
    granted = (await db.execute(
        _CLAIM_SQL, {"lease_id": lease_id, "claim_id": claim_id, "now": now},
    )).scalar_one_or_none()
    if granted is not None:
        return ExecutionClaim(lease_id, claim_id, True, CLAIM_GRANTED)

    row = (await db.execute(_INSPECT_SQL, {"lease_id": lease_id})).first()
    if row is None:
        reason = CLAIM_DENIED_MISSING
    elif row[0] != "active":
        reason = CLAIM_DENIED_NOT_ACTIVE
    else:
        reason = CLAIM_DENIED_HELD
    logger.info(
        "execute_lease %s: execution claim denied (%s, holder=%s)",
        lease_id, reason, (row[1] if row else None),
    )
    return ExecutionClaim(lease_id, claim_id, False, reason)


async def release_execution_claim(
    db: AsyncSession, lease_id: str, *, claim_id: str,
) -> bool:
    """Give up execution rights, but only if we still hold them.

    Guarded by ``execution_claim_id = :claim_id`` so a straggler that lost the
    claim to a reclaiming delivery cannot clear the new holder's claim on its
    way out. Called from ``finally`` on every exit path — success, failure,
    deadline, exception — so a Celery retry can immediately re-claim.
    """
    released = (await db.execute(
        _RELEASE_SQL, {"lease_id": lease_id, "claim_id": claim_id},
    )).scalar_one_or_none()
    return released is not None


__all__ = [
    "CLAIM_DENIED_HELD",
    "CLAIM_DENIED_MISSING",
    "CLAIM_DENIED_NOT_ACTIVE",
    "CLAIM_GRANTED",
    "ExecutionClaim",
    "claim_lease_for_execution",
    "release_execution_claim",
]
