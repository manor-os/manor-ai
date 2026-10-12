"""Durable and low-latency wakeups for execution plans.

Every lease transition, every cleanup sweep and every cycle that asks to be
re-run is a wake, and each wake used to enqueue its own ``run_plan``. A busy
plan therefore collected a queue of duplicate cycles that each found the
advisory lock taken, returned ``cycle_busy`` and re-enqueued themselves —
the control-plane starvation shape.

``RuntimeOutboxEvent`` is the authoritative recovery surface.  Each Plan owns
one stable outbox row whose payload carries an opaque generation.  A state
transition replaces that generation in the same database transaction; stale
broker deliveries can therefore never acknowledge a newer wake.  There is no
generationless direct-queue fallback.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import ExecutionPlanStatus
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.runtime_run import RuntimeOutboxEvent

logger = logging.getLogger(__name__)

PLAN_CYCLE_OUTBOX_EVENT = "runtime.dispatch_plan_cycle"
PLAN_CYCLE_OUTBOX_VERSION = 1
PLAN_CYCLE_PENDING = "plan cycle wake pending"
PLAN_CYCLE_AWAITING_CONTROL_ACK = "plan cycle control delivery awaiting acknowledgment"
PLAN_CYCLE_AWAITING_HEAVY_ACK = "plan cycle execution awaiting acknowledgment"
PLAN_CYCLE_EXECUTION_CLAIM_KEY = "execution_claim"
# ``run_plan`` inherits Celery's 600 second hard limit.  Keep the durable
# single-flight fence alive past that limit so a worker killed at the deadline
# cannot overlap its redelivery; PostgreSQL recovery takes over after the
# bounded grace period.
PLAN_CYCLE_EXECUTION_CLAIM_SECONDS = 660

_PLAN_TERMINAL_STATUSES = frozenset({
    ExecutionPlanStatus.COMPLETED.value,
    ExecutionPlanStatus.FAILED.value,
    ExecutionPlanStatus.CANCELLED.value,
    ExecutionPlanStatus.REPLANNED.value,
})


@dataclass(frozen=True)
class PlanCycleWake:
    event_id: str
    plan_id: str
    wake_generation: str
    available_at: datetime


@dataclass(frozen=True)
class PlanCycleExecutionClaim:
    event_id: str
    plan_id: str
    wake_generation: str
    claim_token: str
    expires_at: datetime


def plan_cycle_outbox_dedupe_key(plan_id: str) -> str:
    """One mutable, generation-fenced outbox row per Plan."""

    return f"plan-cycle:{plan_id}"


async def stage_plan_cycle_wake(
    db: AsyncSession,
    plan_id: str | None,
    *,
    delay_seconds: float = 0,
    reason: str = "state_transition",
) -> PlanCycleWake | None:
    """Persist the authoritative next cycle inside the caller's transaction.

    The stable dedupe key bounds storage and queue pressure to one pending row
    per Plan.  Every upsert replaces the opaque generation, so an old consumer
    or a cycle already in flight cannot close a wake caused by newer state.
    """

    normalized_plan_id = str(plan_id or "").strip()
    if not normalized_plan_id:
        return None
    status = await db.scalar(
        select(ExecutionPlan.status).where(ExecutionPlan.id == normalized_plan_id)
    )
    if status is None or status in _PLAN_TERMINAL_STATUSES:
        return None

    now = datetime.now(timezone.utc)
    available_at = now + timedelta(seconds=max(0, float(delay_seconds)))
    generation = generate_ulid()
    event_id = generate_ulid()
    payload = {
        "version": PLAN_CYCLE_OUTBOX_VERSION,
        "plan_id": normalized_plan_id,
        "wake_generation": generation,
        "reason": str(reason or "state_transition")[:160],
    }
    insert_statement = pg_insert(RuntimeOutboxEvent).values(
        id=event_id,
        event_type=PLAN_CYCLE_OUTBOX_EVENT,
        aggregate_id=normalized_plan_id,
        dedupe_key=plan_cycle_outbox_dedupe_key(normalized_plan_id),
        payload=payload,
        attempt_count=0,
        available_at=available_at,
        delivered_at=None,
        last_error=PLAN_CYCLE_PENDING,
        created_at=now,
    )
    statement = (
        insert_statement
        .on_conflict_do_update(
            index_elements=[RuntimeOutboxEvent.dedupe_key],
            set_={
                "event_type": PLAN_CYCLE_OUTBOX_EVENT,
                "aggregate_id": normalized_plan_id,
                # A real state transition advances the wake generation, but
                # it must not erase an in-flight Plan-wide execution claim.
                # The current owner will release that claim when its stale
                # generation settles; until then the successor cannot overlap
                # provider/supervisor work.
                "payload": RuntimeOutboxEvent.payload.op("||")(
                    insert_statement.excluded.payload
                ),
                "attempt_count": 0,
                "available_at": available_at,
                "delivered_at": None,
                "last_error": PLAN_CYCLE_PENDING,
            },
        )
        .returning(RuntimeOutboxEvent.id)
    )
    persisted_event_id = str((await db.execute(statement)).scalar_one())
    return PlanCycleWake(
        event_id=persisted_event_id,
        plan_id=normalized_plan_id,
        wake_generation=generation,
        available_at=available_at,
    )


async def pending_plan_cycle_wake(
    db: AsyncSession,
    plan_id: str | None,
) -> PlanCycleWake | None:
    """Load the currently committed, unacknowledged wake for one Plan.

    This is used only for a post-commit latency hint.  The outbox row remains
    authoritative, so racing a newer generation after this read is harmless:
    the CONTROL consumer revalidates the exact generation before handoff.
    """

    normalized_plan_id = str(plan_id or "").strip()
    if not normalized_plan_id:
        return None
    event = (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == normalized_plan_id,
            RuntimeOutboxEvent.dedupe_key
            == plan_cycle_outbox_dedupe_key(normalized_plan_id),
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one_or_none()
    if event is None:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    generation = str(payload.get("wake_generation") or "").strip()
    if not generation:
        return None
    return PlanCycleWake(
        event_id=str(event.id),
        plan_id=normalized_plan_id,
        wake_generation=generation,
        available_at=event.available_at,
    )


def dispatch_plan_cycle_wake_hint(
    wake: PlanCycleWake | None,
    *,
    reason: str = "post_commit_hint",
) -> bool:
    """Best-effort CONTROL publication for an already committed wake.

    The return value describes only whether the latency hint reached the
    broker.  Callers must derive user-facing ``dispatched`` from the durable
    wake itself, never from this value, and must not change Plan/Task business
    state when publication fails.
    """

    if wake is None:
        return False
    available_at = wake.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=timezone.utc)
    countdown = max(
        0,
        math.ceil(
            (available_at - datetime.now(timezone.utc)).total_seconds()
        ),
    )
    try:
        from packages.core.tasks.runtime_tasks import (
            dispatch_plan_cycle_intent_task,
        )

        dispatch_plan_cycle_intent_task.apply_async(
            kwargs={
                "event_id": wake.event_id,
                "plan_id": wake.plan_id,
                "wake_generation": wake.wake_generation,
                "reason": str(reason or "post_commit_hint")[:160],
            },
            countdown=countdown,
        )
        return True
    except Exception:
        logger.warning(
            "Plan %s low-latency wake hint failed; durable outbox remains pending",
            wake.plan_id,
            exc_info=True,
        )
        return False


async def hint_pending_plan_cycle(
    db: AsyncSession,
    plan_id: str | None,
    *,
    reason: str = "post_commit_hint",
) -> bool:
    """Hint the current durable wake and report whether durable intent exists."""

    wake = await pending_plan_cycle_wake(db, plan_id)
    if wake is None:
        return False
    dispatch_plan_cycle_wake_hint(wake, reason=reason)
    return True


def _execution_claim(payload: dict) -> tuple[str, str, datetime | None]:
    raw = payload.get(PLAN_CYCLE_EXECUTION_CLAIM_KEY)
    if not isinstance(raw, dict):
        return "", "", None
    token = str(raw.get("token") or "").strip()
    generation = str(raw.get("wake_generation") or "").strip()
    try:
        expires_at = datetime.fromtimestamp(
            float(raw.get("expires_at") or 0),
            tz=timezone.utc,
        )
    except (OverflowError, TypeError, ValueError):
        expires_at = None
    return token, generation, expires_at


async def claim_plan_cycle_wake(
    db: AsyncSession,
    *,
    plan_id: str,
    claim_token: str,
    event_id: str,
    wake_generation: str,
) -> tuple[PlanCycleExecutionClaim | None, str]:
    """Claim one Plan cycle without holding a database connection during work.

    Every HEAVY delivery must match an exact durable event and generation.
    """

    normalized_plan_id = str(plan_id or "").strip()
    normalized_token = str(claim_token or "").strip()
    normalized_event_id = str(event_id or "").strip()
    normalized_generation = str(wake_generation or "").strip()
    if (
        not normalized_plan_id
        or not normalized_token
        or not normalized_event_id
        or not normalized_generation
    ):
        return None, "invalid_delivery"

    async def _load_event() -> RuntimeOutboxEvent | None:
        conditions = [
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == normalized_plan_id,
            RuntimeOutboxEvent.dedupe_key
            == plan_cycle_outbox_dedupe_key(normalized_plan_id),
        ]
        conditions.append(RuntimeOutboxEvent.id == normalized_event_id)
        return (await db.execute(
            select(RuntimeOutboxEvent)
            .where(*conditions)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()

    event = await _load_event()
    if event is None:
        return None, "missing_event"

    payload = dict(event.payload or {})
    current_generation = str(payload.get("wake_generation") or "").strip()
    stored_token, stored_generation, stored_expires_at = _execution_claim(payload)
    now = datetime.now(timezone.utc)
    if not current_generation:
        return None, "invalid_event"

    if current_generation != normalized_generation:
        # A retry belonging to the displaced owner may release only its own
        # Plan-wide claim.  It cannot ACK or otherwise change the successor's
        # generation.
        if stored_token == normalized_token and stored_generation == normalized_generation:
            payload.pop(PLAN_CYCLE_EXECUTION_CLAIM_KEY, None)
            event.payload = payload
            if event.last_error != PLAN_CYCLE_PENDING:
                event.available_at = now
            event.last_error = PLAN_CYCLE_PENDING
            await db.flush()
        return None, "stale_generation"
    if event.delivered_at is not None:
        return None, "already_acknowledged"

    live_claim = bool(
        stored_token
        and stored_expires_at is not None
        and stored_expires_at > now
    )
    if live_claim:
        # Celery redelivery can reuse the same task id while the original
        # process is still alive.  A matching token is therefore not proof of
        # re-entrancy ownership; every retry explicitly releases its claim
        # before publishing the next attempt.
        return None, "execution_claim_held"

    # An old exact HEAVY delivery may not bypass an intentional future wake.
    # This matters when a busy/supervisor cycle re-arms the *same* generation:
    # delayed duplicates still carry an exact identity, but no authority to
    # erase its new not-before deadline.
    if (
        not live_claim
        and event.last_error == PLAN_CYCLE_PENDING
        and event.available_at is not None
        and event.available_at > now
    ):
        return None, "not_due"

    expires_at = now + timedelta(seconds=PLAN_CYCLE_EXECUTION_CLAIM_SECONDS)
    payload[PLAN_CYCLE_EXECUTION_CLAIM_KEY] = {
        "version": 1,
        "token": normalized_token,
        "wake_generation": current_generation,
        "claimed_at": now.timestamp(),
        "expires_at": expires_at.timestamp(),
    }
    event.payload = payload
    # While the HEAVY owner is alive, the outbox publisher has nothing useful
    # to redeliver.  On worker loss, this deadline makes the same generation
    # recoverable without a separate cleanup write.
    event.available_at = expires_at
    event.last_error = PLAN_CYCLE_AWAITING_HEAVY_ACK
    await db.flush()
    return PlanCycleExecutionClaim(
        event_id=event.id,
        plan_id=normalized_plan_id,
        wake_generation=current_generation,
        claim_token=normalized_token,
        expires_at=expires_at,
    ), "claimed"


async def release_plan_cycle_wake_claim(
    db: AsyncSession,
    *,
    event_id: str,
    plan_id: str,
    wake_generation: str,
    claim_token: str,
    delay_seconds: float = 0,
) -> bool:
    """Release one failed HEAVY owner without ACKing its generation."""

    event = (await db.execute(
        select(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.id == event_id,
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == plan_id,
            RuntimeOutboxEvent.dedupe_key == plan_cycle_outbox_dedupe_key(plan_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if event is None or event.delivered_at is not None:
        return False
    payload = dict(event.payload or {})
    stored_token, stored_generation, _stored_expires_at = _execution_claim(payload)
    if stored_token != claim_token or stored_generation != wake_generation:
        return False

    payload.pop(PLAN_CYCLE_EXECUTION_CLAIM_KEY, None)
    event.payload = payload
    if str(payload.get("wake_generation") or "") == wake_generation:
        event.available_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0, float(delay_seconds))
        )
    elif event.last_error != PLAN_CYCLE_PENDING:
        # A successor already reached CONTROL/HEAVY but could not claim while
        # this owner was live.  Make that exact generation retry immediately.
        event.available_at = datetime.now(timezone.utc)
    event.last_error = PLAN_CYCLE_PENDING
    await db.flush()
    return True


async def settle_plan_cycle_wake(
    db: AsyncSession,
    *,
    event_id: str,
    plan_id: str,
    wake_generation: str,
    claim_token: str,
    next_action: str | None,
    result_status: str | None = None,
    delay_seconds: float = 0,
) -> bool:
    """ACK one exact generation or atomically replace it with the next one."""

    event = (await db.execute(
        select(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.id == event_id,
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == plan_id,
            RuntimeOutboxEvent.dedupe_key == plan_cycle_outbox_dedupe_key(plan_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if event is None:
        return False
    payload = dict(event.payload or {})
    stored_token, stored_generation, _stored_expires_at = _execution_claim(payload)
    if stored_token != claim_token or stored_generation != wake_generation:
        return False
    if str(payload.get("wake_generation") or "") != wake_generation:
        # A lease transition or another cycle staged newer work while this
        # cycle was running.  Release only this exact Plan-wide claim, make the
        # successor immediately due, and never acknowledge its generation.
        payload.pop(PLAN_CYCLE_EXECUTION_CLAIM_KEY, None)
        event.payload = payload
        if event.delivered_at is None:
            if event.last_error != PLAN_CYCLE_PENDING:
                event.available_at = datetime.now(timezone.utc)
            event.last_error = PLAN_CYCLE_PENDING
        await db.flush()
        return False
    if event.delivered_at is not None:
        return False

    plan_status = await db.scalar(
        select(ExecutionPlan.status).where(ExecutionPlan.id == plan_id)
    )
    retry_same_generation = result_status in {
        "cycle_busy",
        "supervisor_review_in_progress",
    }
    if (
        next_action == "schedule_self"
        and plan_status is not None
        and plan_status not in _PLAN_TERMINAL_STATUSES
    ):
        if retry_same_generation:
            payload.pop(PLAN_CYCLE_EXECUTION_CLAIM_KEY, None)
            payload["reason"] = "cycle_single_flight_retry"
            event.payload = payload
        else:
            generation = generate_ulid()
            event.payload = {
                "version": PLAN_CYCLE_OUTBOX_VERSION,
                "plan_id": plan_id,
                "wake_generation": generation,
                "reason": "cycle_schedule_self",
            }
        event.attempt_count = 0
        event.available_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0, float(delay_seconds))
        )
        event.delivered_at = None
        event.last_error = PLAN_CYCLE_PENDING
    else:
        payload.pop(PLAN_CYCLE_EXECUTION_CLAIM_KEY, None)
        event.payload = payload
        event.delivered_at = datetime.now(timezone.utc)
        event.last_error = None
    await db.flush()
    return True
