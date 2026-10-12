"""Celery control-plane tasks for durable Runtime and Sandbox coordination."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, literal, or_, select, text

from packages.core.celery_app import celery_app
from packages.core.config import get_settings
from packages.core.constants.advisory_locks import AdvisoryLockNamespace
from packages.core.constants.execution import (
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    PLAN_TERMINAL_STATUSES,
    WorkLeaseStatus,
)
from packages.core.constants.task import TaskRuntimeKind, TaskStatus
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.models.task import Task
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
)
from packages.core.models.worker import WorkLease
from packages.core.plans.wakeup import (
    PLAN_CYCLE_AWAITING_CONTROL_ACK,
    PLAN_CYCLE_AWAITING_HEAVY_ACK,
    PLAN_CYCLE_OUTBOX_EVENT,
    PLAN_CYCLE_PENDING,
    plan_cycle_outbox_dedupe_key,
    stage_plan_cycle_wake,
)
from packages.core.queues import queue_for_task
from packages.core.services.task_execution import (
    TASK_EXECUTION_OUTBOX_EVENT,
    TaskExecutionDispatch,
    resolve_task_execution,
    task_execution_intent_dedupe_key,
    task_execution_intent_generation,
)
from packages.core.services.durable_run import close_run_wake, clear_run_claim_and_wake
from packages.core.services.task_coordinator_wakeup import (
    TASK_COORDINATOR_AWAITING_ACK,
    TASK_COORDINATOR_OUTBOX_EVENT,
    TASK_COORDINATOR_RUN_SPEC,
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
    task_coordinator_outbox_dedupe_key,
    task_coordinator_delivery_state,
)
from packages.core.services.runtime_human_input import (
    expire_runtime_human_input_requests,
)
from packages.core.services.sandbox_queue_service import (
    claim_due_sandbox_runner_health_checks,
    claim_next_sandbox_allocation,
    complete_sandbox_allocation,
    configured_sandbox_runners,
    effective_runner_limits,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    reconciled_runner_active_count,
    reconcile_sandbox_runners,
    recover_stale_sandbox_allocations,
    release_vanished_sandbox_instances,
)
from packages.core.services.runtime_run_terminal import (
    RUNTIME_RUN_TERMINAL_NOTICE_EVENT,
    announce_runtime_run_terminal,
)
from packages.core.services.runtime_event_stream import (
    append_runtime_sse_event,
    parse_sse_frame,
)
from packages.core.tasks._runtime import run_in_worker
from packages.core.services.workspace_access import lock_workspace_access_boundary

logger = logging.getLogger(__name__)

_TASK_RUNTIME_RESUME_REDELIVERY_AFTER_SECONDS = 120
_TASK_RUNTIME_RESUME_INVALID_SENTINEL = (
    "permanent:runtime_approval_resume_delivery_invalid"
)
_TASK_EXECUTION_ACK_TIMEOUT_SECONDS = 120
_PLAN_CYCLE_ACK_TIMEOUT_SECONDS = 120
_TASK_COORDINATOR_ACK_TIMEOUT_SECONDS = 120
_TASK_COORDINATOR_AWAITING_HEAVY_ACK = "task coordinator heavy execution awaiting acknowledgment"
_PLAN_CYCLE_STALE_RECONCILE_SECONDS = 120
_TASK_EXECUTION_CLOSED_SENTINEL = "permanent:task_execution_intent_closed"
_TASK_EXECUTION_AWAITING_ACK = "task execution delivery awaiting consumer acknowledgment"
_TASK_EXECUTION_DEFERRED_PREFIX = "task execution delivery deferred"
_TASK_EXECUTION_RETRYABLE_CLOSURES = frozenset({"non_coordinated_runtime"})
_RUNTIME_OUTBOX_RETRY_BASE_SECONDS = 5
_RUNTIME_OUTBOX_RETRY_MAX_SECONDS = 300
# An un-ACKed delivery is re-sent with exponential backoff from its type's
# base ACK timeout. Re-arming a new generation resets attempt_count to 0, so
# only repeated silence for the *same* generation slows down.
_RUNTIME_OUTBOX_ACK_MAX_SECONDS = 1800
_TASK_TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.FAILED.value,
})
_PLAN_TERMINAL_STATUS_VALUES = tuple(
    status.value for status in PLAN_TERMINAL_STATUSES
)
_PLAN_CYCLE_RUNNABLE_STATUSES = (
    ExecutionPlanStatus.DRAFT.value,
    ExecutionPlanStatus.PENDING.value,
    ExecutionPlanStatus.RUNNING.value,
)


def _runtime_outbox_plan_category_filter(*, plan_cycle: bool):
    """Match the exact partial-index predicate for fair quota scans.

    ``literal_execute`` keeps this server-owned event type out of a generic
    prepared-plan parameter. PostgreSQL can then prove that the query implies
    the corresponding category partial index predicate.
    """

    plan_event_type = literal(PLAN_CYCLE_OUTBOX_EVENT, literal_execute=True)
    if plan_cycle:
        return RuntimeOutboxEvent.event_type == plan_event_type
    return RuntimeOutboxEvent.event_type != plan_event_type


def _runtime_outbox_retry_available_at(
    now: datetime,
    *,
    attempt_count: int,
) -> datetime:
    """Return bounded exponential backoff for one failed broker handoff."""

    exponent = min(max(0, int(attempt_count) - 1), 16)
    delay_seconds = min(
        _RUNTIME_OUTBOX_RETRY_MAX_SECONDS,
        _RUNTIME_OUTBOX_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    return now + timedelta(seconds=delay_seconds)


def _runtime_outbox_ack_available_at(
    now: datetime,
    *,
    base_seconds: int,
    attempt_count: int,
) -> datetime:
    """Return the next redelivery time for a delivery still awaiting its ACK.

    ``attempt_count`` also counts deferrals and broker failures, so the first
    real send after N deferrals may get a longer ACK window than ``base``.
    That only matters if the consumer then stays silent; a consumer that ACKs
    settles the row regardless of the window.
    """

    exponent = min(max(0, int(attempt_count) - 1), 16)
    delay_seconds = min(
        _RUNTIME_OUTBOX_ACK_MAX_SECONDS,
        int(base_seconds) * (2 ** exponent),
    )
    return now + timedelta(seconds=delay_seconds)


async def _recover_task_runtime_resume_outbox(
    db,
    *,
    now: datetime,
    batch_size: int,
) -> int:
    """Re-arm only the still-current signed resume generation.

    ``delivered_at`` proves broker acceptance, not worker ownership.  A broker
    loss after acceptance must therefore be recoverable, while an event for an
    older approval generation must remain a permanent no-op.
    """

    from packages.core.services.task_runtime_approvals import (
        TASK_RUNTIME_RESUME_EVENT,
        validate_task_runtime_resume_delivery,
    )

    cutoff = now - timedelta(
        seconds=_TASK_RUNTIME_RESUME_REDELIVERY_AFTER_SECONDS
    )
    events = list((await db.execute(
        select(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.event_type == TASK_RUNTIME_RESUME_EVENT,
            RuntimeOutboxEvent.delivered_at.is_not(None),
            RuntimeOutboxEvent.delivered_at <= cutoff,
            or_(
                RuntimeOutboxEvent.last_error.is_(None),
                RuntimeOutboxEvent.last_error
                != _TASK_RUNTIME_RESUME_INVALID_SENTINEL,
            ),
        )
        .order_by(RuntimeOutboxEvent.delivered_at.asc())
        .limit(batch_size)
    )).scalars().all())
    rearmed = 0
    for event in events:
        payload = dict(event.payload or {})
        hitl_id = str(payload.get("runtime_hitl_id") or "").strip()
        valid = bool(hitl_id) and await validate_task_runtime_resume_delivery(
            db,
            task_id=str(payload.get("task_id") or ""),
            agent_id=payload.get("agent_id"),
            scheduled_run_id=payload.get("scheduled_run_id"),
            scheduled_job_id=payload.get("scheduled_job_id"),
            runtime_hitl_id=hitl_id,
            runtime_hitl_generation=payload.get("runtime_hitl_generation"),
            runtime_hitl_checkpoint_digest=str(
                payload.get("runtime_hitl_checkpoint_digest") or ""
            ),
            runtime_hitl_checkpoint_signature=str(
                payload.get("runtime_hitl_checkpoint_signature") or ""
            ),
            runtime_hitl_resolution_signature=str(
                payload.get("runtime_hitl_resolution_signature") or ""
            ),
            allow_resuming=True,
        )
        locked_event = (await db.execute(
            select(RuntimeOutboxEvent)
            .where(RuntimeOutboxEvent.id == event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (
            locked_event is None
            or locked_event.delivered_at is None
            or locked_event.delivered_at > cutoff
            or dict(locked_event.payload or {}) != payload
        ):
            continue
        event = locked_event
        if not valid:
            # A stale generation, terminal origin, invalid signature, or an
            # already-claimed continuation can never become valid again.  Keep
            # the broker-acceptance timestamp as audit evidence and mark the
            # row so it cannot monopolize the oldest recovery batch forever.
            event.last_error = _TASK_RUNTIME_RESUME_INVALID_SENTINEL
            continue
        event.delivered_at = None
        event.available_at = now
        event.last_error = "runtime approval resume delivery was not claimed"
        rearmed += 1
    return rearmed


async def _task_execution_delivery_state(
    db,
    event: RuntimeOutboxEvent,
) -> tuple[TaskExecutionDispatch | None, str]:
    """Validate that ``event`` is still the one current Task start intent."""

    payload = dict(event.payload or {})
    task_id = str(payload.get("task_id") or "").strip()
    generation = str(payload.get("intent_generation") or "").strip()
    if (
        not task_id
        or not generation
        or event.event_type != TASK_EXECUTION_OUTBOX_EVENT
        or event.aggregate_id != task_id
        or event.dedupe_key
        != task_execution_intent_dedupe_key(task_id, generation)
    ):
        return None, "invalid_event"

    task = await db.get(Task, task_id)
    if task is None:
        return None, "missing_task"
    if task.status in _TASK_TERMINAL_STATUSES:
        return None, "terminal_task"
    if task.status != TaskStatus.IN_PROGRESS.value:
        return None, "inactive_task_intent"
    if task_execution_intent_generation(task) != generation:
        return None, "stale_task_intent"

    active_plan = (await db.execute(
        select(ExecutionPlan)
        .where(
            ExecutionPlan.task_id == task_id,
            ExecutionPlan.status.not_in(_PLAN_TERMINAL_STATUS_VALUES),
        )
        .limit(1)
    )).scalar_one_or_none()
    if active_plan is not None:
        marker = active_plan.dispatcher_state if isinstance(active_plan.dispatcher_state, dict) else {}
        if marker.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION:
            return None, "active_plan"
        run = (await db.execute(
            select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
        )).scalar_one_or_none()
        if (
            run is None
            or marker.get(TASK_COORDINATOR_RUN_ID_KEY) != run.id
            or run.current_plan_id != active_plan.id
        ):
            return None, "foreign_active_plan"

    dispatch = await resolve_task_execution(db, task)
    if dispatch.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN:
        return None, "non_coordinated_runtime"
    return dispatch, "current"


async def _plan_cycle_delivery_state(
    db,
    event: RuntimeOutboxEvent,
) -> tuple[ExecutionPlan | None, str]:
    """Validate one exact, still-active Plan wake generation."""

    payload = dict(event.payload or {})
    plan_id = str(payload.get("plan_id") or "").strip()
    generation = str(payload.get("wake_generation") or "").strip()
    if (
        not plan_id
        or not generation
        or event.event_type != PLAN_CYCLE_OUTBOX_EVENT
        or event.aggregate_id != plan_id
        or event.dedupe_key != plan_cycle_outbox_dedupe_key(plan_id)
    ):
        return None, "invalid_event"
    plan = await db.get(ExecutionPlan, plan_id)
    if plan is None:
        return None, "missing_plan"
    if plan.status in _PLAN_TERMINAL_STATUS_VALUES:
        return None, "terminal_plan"
    if plan.status not in _PLAN_CYCLE_RUNNABLE_STATUSES:
        return None, "inactive_plan"
    return plan, "current"


async def _recover_stalled_plan_cycle_wakes(
    db,
    *,
    now: datetime,
    batch_size: int,
) -> int:
    """Reconstruct a lost wake from durable non-terminal Plan state.

    This is the backstop for legacy/future writers that commit a Step change
    without staging the outbox row.  It deliberately skips a Plan that already
    has a pending generation, an active worker lease, or only human-paused
    work.  Candidate Plan locks use a stable bounded SKIP LOCKED scan so many
    sweepers cannot manufacture a wake storm.
    """

    plan_cycle_dedupe_key = "plan-cycle:" + ExecutionPlan.id
    pending_event = exists().where(
        RuntimeOutboxEvent.dedupe_key == plan_cycle_dedupe_key,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ).correlate(ExecutionPlan)
    active_lease = exists().where(
        WorkLease.plan_id == ExecutionPlan.id,
        WorkLease.status == WorkLeaseStatus.ACTIVE.value,
    ).correlate(ExecutionPlan)
    unfinished_step = exists().where(
        ExecutionStep.plan_id == ExecutionPlan.id,
        ExecutionStep.step_status.not_in((
            ExecutionStepStatus.DONE.value,
            ExecutionStepStatus.FAILED.value,
            ExecutionStepStatus.SKIPPED.value,
            ExecutionStepStatus.CANCELLED.value,
        )),
    ).correlate(ExecutionPlan)
    runnable_unfinished_step = exists().where(
        ExecutionStep.plan_id == ExecutionPlan.id,
        ExecutionStep.step_status.not_in((
            ExecutionStepStatus.DONE.value,
            ExecutionStepStatus.FAILED.value,
            ExecutionStepStatus.SKIPPED.value,
            ExecutionStepStatus.CANCELLED.value,
            ExecutionStepStatus.WAITING_HUMAN.value,
            ExecutionStepStatus.PAUSED.value,
        )),
    ).correlate(ExecutionPlan)
    waiting_human_step = exists().where(
        ExecutionStep.plan_id == ExecutionPlan.id,
        ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN.value,
    ).correlate(ExecutionPlan)
    last_delivered_at = (
        select(RuntimeOutboxEvent.delivered_at)
        .where(
            RuntimeOutboxEvent.dedupe_key == plan_cycle_dedupe_key,
        )
        .correlate(ExecutionPlan)
        .scalar_subquery()
    )
    changed_step_since_delivery = exists().where(
        ExecutionStep.plan_id == ExecutionPlan.id,
        ExecutionStep.updated_at > last_delivered_at,
    ).correlate(ExecutionPlan)
    plans = list((await db.execute(
        select(ExecutionPlan)
        .where(
            ExecutionPlan.status.in_(_PLAN_CYCLE_RUNNABLE_STATUSES),
            ~pending_event,
            ~active_lease,
            # A parked coordinator root is PAUSED. Wake it when its child is
            # terminal (no WAITING_HUMAN remains), but do not poll a root whose
            # child is still waiting on the user. Ordinary runnable and all-
            # terminal Plans retain their normal recovery paths.
            or_(
                ~unfinished_step,
                runnable_unfinished_step,
                ~waiting_human_step,
            ),
            or_(
                last_delivered_at.is_(None),
                ExecutionPlan.updated_at > last_delivered_at,
                changed_step_since_delivery,
                last_delivered_at
                <= now - timedelta(seconds=_PLAN_CYCLE_STALE_RECONCILE_SECONDS),
            ),
        )
        # Match the runnable partial/expression index. ``updated_at`` is
        # nullable on legacy/create_all rows, so created_at is the stable
        # fallback instead of PostgreSQL's starvation-prone NULLS LAST order.
        # A recovered Plan's pending row excludes it from the next batch.
        .order_by(
            func.coalesce(
                ExecutionPlan.updated_at,
                ExecutionPlan.created_at,
            ).asc(),
            ExecutionPlan.id.asc(),
        )
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )).scalars().all())
    recovered = 0
    terminal_step_statuses = {
        ExecutionStepStatus.DONE.value,
        ExecutionStepStatus.FAILED.value,
        ExecutionStepStatus.SKIPPED.value,
        ExecutionStepStatus.CANCELLED.value,
    }
    human_wait_statuses = {
        ExecutionStepStatus.WAITING_HUMAN.value,
    }
    for plan in plans:
        steps = list((await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.created_at.asc(), ExecutionStep.id.asc())
        )).scalars().all())
        unfinished = [
            step for step in steps if step.step_status not in terminal_step_statuses
        ]
        # SQL already excludes active leases and human-only waits. Keep this
        # defensive recheck for rows that changed after candidate selection.
        if unfinished and all(step.step_status in human_wait_statuses for step in unfinished):
            continue

        delay_seconds = 0.0
        pending = [
            step for step in unfinished
            if step.step_status == ExecutionStepStatus.PENDING.value
        ]
        if pending and all(step.kind == "sleep" for step in pending):
            from packages.core.plans.executor import PlanExecutor

            delays = [
                PlanExecutor._sleep_seconds(
                    step.params if isinstance(step.params, dict) else {}
                )
                for step in pending
                # ``seconds`` is relative to the executor admitting the sleep,
                # so delaying recovery by that duration would apply it twice.
                # Only already-absolute deadlines are safe to honor here.
                if isinstance(step.params, dict)
                and (
                    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY in step.params
                    or "until" in step.params
                )
            ]
            positive_delays = [delay for delay in delays if delay > 0]
            if positive_delays:
                delay_seconds = min(positive_delays)
        wake = await stage_plan_cycle_wake(
            db,
            plan.id,
            delay_seconds=delay_seconds,
            reason="stalled_plan_backstop",
        )
        if wake is not None:
            recovered += 1
    return recovered


async def recover_stalled_plan_cycle_wakes_once(*, batch_size: int = 100) -> int:
    """Run the bounded lost-wake backstop independently of outbox publish.

    Normal Plan mutations stage their generation in the same transaction, so
    this scan is exceptional recovery rather than the delivery fast path.  A
    dedicated lower-frequency task plus a transaction advisory lock prevents
    multiple CONTROL workers from repeating the correlated parked-Plan scan.
    """

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        dialect = getattr(getattr(db, "bind", None), "dialect", None)
        if getattr(dialect, "name", None) == "postgresql":
            claimed = await db.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock(:namespace, :sweep_key)"
                ),
                {
                    "namespace": int(
                        AdvisoryLockNamespace.PLAN_CYCLE_RECOVERY_SWEEP
                    ),
                    "sweep_key": 0,
                },
            )
            if not claimed:
                await db.rollback()
                return 0
        recovered = await _recover_stalled_plan_cycle_wakes(
            db,
            now=datetime.now(timezone.utc),
            batch_size=max(1, int(batch_size)),
        )
        await db.commit()
        return recovered


_PARKED_JOB_POLL_SECONDS = 300


async def _parked_agent_loop_wake_time(db, run, *, now: datetime) -> datetime:
    """When a parked agent loop with no pending wake should wake.

    A timer resumes at its own time; a job wait resumes when its jobs are
    terminal or its deadline passed, and otherwise polls again. A malformed
    wait wakes at once so the worker surfaces it.
    """

    from packages.core.models.media_job import MediaJob, MediaJobStatus
    from packages.core.services.task_coordinator_wakeup import TASK_LOOP_WAIT_KEY

    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    wait = checkpoint.get(TASK_LOOP_WAIT_KEY)
    wait = wait if isinstance(wait, dict) else {}

    def _time(value):
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    if run.phase == TaskCoordinatorPhase.WAITING_TIMER.value:
        wake_at = _time(wait.get("wake_at"))
        return max(now, wake_at) if wake_at is not None else now
    deadline = _time(wait.get("deadline_at"))
    job_ids = [str(item) for item in wait.get("job_ids") or [] if item]
    if deadline is None or not job_ids or deadline <= now:
        return now
    still_open = (await db.execute(
        select(MediaJob.id).where(
            MediaJob.entity_id == run.entity_id,
            MediaJob.id.in_(job_ids),
            MediaJob.status.in_((MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value)),
        ).limit(1)
    )).scalar_one_or_none()
    if still_open is None:
        return now
    return min(deadline, now + timedelta(seconds=_PARKED_JOB_POLL_SECONDS))


async def recover_stalled_task_coordinator_wakes_once(*, batch_size: int = 100) -> int:
    """Bounded backstop for missing V3 Task wakes; never poll live children."""

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        dialect = getattr(getattr(db, "bind", None), "dialect", None)
        if getattr(dialect, "name", None) == "postgresql":
            claimed = await db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :sweep_key)"),
                {
                    "namespace": int(AdvisoryLockNamespace.PLAN_CYCLE_RECOVERY_SWEEP),
                    "sweep_key": 1,
                },
            )
            if not claimed:
                return 0
        now = datetime.now(timezone.utc)
        pending_event = exists().where(
            RuntimeOutboxEvent.dedupe_key == (
                "task-coordinator:" + TaskCoordinatorRun.task_id
            ),
            RuntimeOutboxEvent.delivered_at.is_(None),
            # A retained outbox row from an older wake is not a live pending
            # delivery. Recover the Run's current generation instead.
            RuntimeOutboxEvent.payload["wake_generation"].as_string()
            == TaskCoordinatorRun.wake_generation,
        ).correlate(TaskCoordinatorRun)
        run_tasks = list((await db.execute(
            select(TaskCoordinatorRun, Task)
            .join(Task, Task.id == TaskCoordinatorRun.task_id)
            .where(
                Task.status == TaskStatus.IN_PROGRESS.value,
                TaskCoordinatorRun.phase.in_((
                    TaskCoordinatorPhase.READY.value,
                    TaskCoordinatorPhase.RUNNING.value,
                    TaskCoordinatorPhase.WAITING_PLAN.value,
                    TaskCoordinatorPhase.WAITING_CHILD.value,
                    # A parked agent loop whose wake row was lost. Only an
                    # overdue wait is woken; otherwise its wake is restored.
                    TaskCoordinatorPhase.WAITING_TIMER.value,
                    TaskCoordinatorPhase.WAITING_JOB.value,
                )),
                ~pending_event,
                or_(
                    TaskCoordinatorRun.claim_expires_at.is_(None),
                    TaskCoordinatorRun.claim_expires_at <= now,
                ),
            )
            .order_by(
                func.coalesce(
                    TaskCoordinatorRun.updated_at,
                    TaskCoordinatorRun.created_at,
                ).asc(),
                TaskCoordinatorRun.id.asc(),
            )
            .limit(max(1, min(int(batch_size), 100)))
            .with_for_update(skip_locked=True, of=TaskCoordinatorRun)
        )).all())
        recovered = 0
        terminal = {
            ExecutionStepStatus.DONE.value,
            ExecutionStepStatus.FAILED.value,
            ExecutionStepStatus.SKIPPED.value,
            ExecutionStepStatus.CANCELLED.value,
        }
        for run, task in run_tasks:
            # A Task retry can leave a stale terminal generation on the old
            # Run. Only the exact start intent may manufacture a recovery
            # wake; the retry initializer owns opening its new generation.
            if run.lifecycle_generation != task_execution_intent_generation(task):
                continue
            if run.phase == TaskCoordinatorPhase.WAITING_CHILD.value:
                checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
                control = checkpoint.get("task_coordinator_control")
                if not isinstance(control, dict):
                    # Missing child lineage is a durable error for the Task
                    # worker to surface, not a guessed release.
                    ready = True
                else:
                    step_ids = control.get("step_ids")
                    if not isinstance(step_ids, list) or not step_ids:
                        ready = True
                    else:
                        statuses = list((await db.execute(
                            select(ExecutionStep.id, ExecutionStep.step_status)
                            .where(
                                ExecutionStep.plan_id == run.current_plan_id,
                                ExecutionStep.id.in_(step_ids),
                            )
                        )).all())
                        ready = len(statuses) != len(step_ids) or all(
                            status in terminal for _, status in statuses
                        )
                if not ready:
                    continue
            available_at = None
            if run.phase in {
                TaskCoordinatorPhase.WAITING_TIMER.value,
                TaskCoordinatorPhase.WAITING_JOB.value,
            }:
                available_at = await _parked_agent_loop_wake_time(db, run, now=now)
            wake = await stage_task_coordinator_wake(
                db, task_id=run.task_id, reason="stalled_task_coordinator_backstop",
                available_at=available_at,
            )
            if wake is not None:
                recovered += 1
        await db.commit()
        return recovered


async def recover_task_coordinator_terminal_cleanup_once(*, batch_size: int = 50) -> int:
    """Retry idempotent HITL cleanup after a V3 terminal commit and wake ACK.

    Cleanup locks HITL before Task, so it cannot run inside finalization's
    Task -> Plan transaction. A process death in that gap leaves this marker
    unset; this bounded sweep closes the gap without replaying any Step.
    """

    from packages.core.database import create_worker_session
    from packages.core.plans.executor import PlanExecutor

    session_factory = create_worker_session()
    async with session_factory() as db:
        candidates = list((await db.execute(
            select(TaskCoordinatorRun.id, TaskCoordinatorRun.task_id, TaskCoordinatorRun.current_plan_id)
            .where(
                TaskCoordinatorRun.phase == TaskCoordinatorPhase.COMPLETED.value,
                TaskCoordinatorRun.terminal_cleanup_at.is_(None),
                TaskCoordinatorRun.current_plan_id.is_not(None),
            )
            .order_by(TaskCoordinatorRun.updated_at, TaskCoordinatorRun.id)
            .limit(max(1, min(int(batch_size), 50)))
        )).all())
    cleaned = 0
    for run_id, task_id, plan_id in candidates:
        try:
            async with session_factory() as db:
                await PlanExecutor._cleanup_terminal_approvals_after_commit(
                    db, plan_id=plan_id, task_id=task_id, reason="plan_terminal",
                )
                run = (await db.execute(
                    select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == run_id)
                    .with_for_update().execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if (
                    run is not None
                    and run.phase == TaskCoordinatorPhase.COMPLETED.value
                    and run.current_plan_id == plan_id
                    and run.terminal_cleanup_at is None
                ):
                    run.terminal_cleanup_at = datetime.now(timezone.utc)
                    await db.commit()
                    cleaned += 1
        except Exception:
            logger.exception("Task coordinator terminal cleanup retry failed for plan %s", plan_id)
            # Rotate a repeatedly failing old Plan behind newer candidates;
            # otherwise one hot row can monopolize every bounded sweep.
            try:
                async with session_factory() as retry_db:
                    retry_run = (await retry_db.execute(
                        select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == run_id)
                        .with_for_update().execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if (
                        retry_run is not None
                        and retry_run.phase == TaskCoordinatorPhase.COMPLETED.value
                        and retry_run.current_plan_id == plan_id
                        and retry_run.terminal_cleanup_at is None
                    ):
                        retry_run.updated_at = datetime.now(timezone.utc)
                        await retry_db.commit()
            except Exception:
                logger.exception("Could not rotate terminal cleanup retry for Plan %s", plan_id)
    return cleaned


async def reconcile_terminal_task_coordinator_runs_once(*, batch_size: int = 50) -> int:
    """Stop orphan V3 execution after an external Task terminal transition.

    Normal submit completes Task, Plan, and Run atomically. A user/system
    cancellation or failure can instead terminalize the Task first. Reconcile
    under Task -> Plan/Lease/Step -> Run locks so no stale coordinator wake
    continues to release children after that boundary.
    """

    from packages.core.database import create_worker_session
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.plans.service import cancel_plan

    session_factory = create_worker_session()
    stopped_with_active_plan = exists(
        select(ExecutionPlan.id).where(
            ExecutionPlan.id == TaskCoordinatorRun.current_plan_id,
            ExecutionPlan.task_id == TaskCoordinatorRun.task_id,
            ExecutionPlan.entity_id == TaskCoordinatorRun.entity_id,
            ExecutionPlan.workspace_id == TaskCoordinatorRun.workspace_id,
            ExecutionPlan.status.not_in(_PLAN_TERMINAL_STATUS_VALUES),
            ExecutionPlan.dispatcher_state[TASK_COORDINATOR_PLAN_VERSION_KEY].astext
            == str(TASK_COORDINATOR_PLAN_VERSION),
            ExecutionPlan.dispatcher_state[TASK_COORDINATOR_RUN_ID_KEY].astext
            == TaskCoordinatorRun.id,
        ).correlate(TaskCoordinatorRun)
    )
    async with session_factory() as db:
        candidates = list((await db.execute(
            select(TaskCoordinatorRun.task_id)
            .join(Task, Task.id == TaskCoordinatorRun.task_id)
            .where(
                Task.status.in_(tuple(_TASK_TERMINAL_STATUSES)),
                or_(
                    TaskCoordinatorRun.phase.not_in(tuple(TaskCoordinatorPhase.terminal())),
                    # A coordinator can stop after exhausting its internal
                    # acceptance loop while preserving its active Plan and
                    # DONE Steps for retry. An external Task cancellation must
                    # still close that Plan; do not scan unrelated failed Runs.
                    (TaskCoordinatorRun.phase == TaskCoordinatorPhase.FAILED.value)
                    & stopped_with_active_plan,
                ),
            )
            .order_by(TaskCoordinatorRun.updated_at, TaskCoordinatorRun.id)
            .limit(max(1, min(int(batch_size), 50)))
        )).scalars().all())
    reconciled = 0
    for task_id in candidates:
        async with session_factory() as db:
            task = (await db.execute(
                select(Task).where(Task.id == task_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if task is None or task.status not in _TASK_TERMINAL_STATUSES:
                continue
            run = (await db.execute(
                select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
            )).scalar_one_or_none()
            if run is None or run.phase in {
                TaskCoordinatorPhase.COMPLETED.value,
                TaskCoordinatorPhase.CANCELLED.value,
            }:
                continue
            stopped_failed_run = run.phase == TaskCoordinatorPhase.FAILED.value
            cancelled_active_plan = False
            if run.current_plan_id:
                plan = (await db.execute(
                    select(ExecutionPlan).where(ExecutionPlan.id == run.current_plan_id)
                )).scalar_one_or_none()
                state = plan.dispatcher_state if plan is not None and isinstance(plan.dispatcher_state, dict) else {}
                if (
                    plan is not None
                    and plan.task_id == task.id
                    and state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) == TASK_COORDINATOR_PLAN_VERSION
                    and state.get(TASK_COORDINATOR_RUN_ID_KEY) == run.id
                    and plan.status not in _PLAN_TERMINAL_STATUS_VALUES
                ):
                    await cancel_plan(
                        db, plan.id, reason=f"Task became {task.status}",
                    )
                    cancelled_active_plan = True
            if stopped_failed_run and not cancelled_active_plan:
                # A concurrent finalization or retry may have removed the
                # active Plan after the candidate scan. The already-stopped
                # Run needs no further mutation in this sweep.
                continue
            run = (await db.execute(
                select(TaskCoordinatorRun)
                .where(TaskCoordinatorRun.task_id == task_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if run is None or run.phase in {
                TaskCoordinatorPhase.COMPLETED.value,
                TaskCoordinatorPhase.CANCELLED.value,
            }:
                continue
            run.phase = (
                TaskCoordinatorPhase.FAILED.value
                if task.status == TaskStatus.FAILED.value
                else TaskCoordinatorPhase.CANCELLED.value
            )
            clear_run_claim_and_wake(run)
            # A parked run's wake may be days away; close it now rather than
            # leaving a pending row until it would have been delivered.
            await close_run_wake(
                db, TASK_COORDINATOR_RUN_SPEC,
                key=task_id,
                last_error="permanent:task_coordinator_intent_closed:terminal_task",
            )
            await db.commit()
            reconciled += 1
    await _close_terminal_agent_loop_input_cards(session_factory, batch_size=batch_size)
    return reconciled


async def _close_terminal_agent_loop_input_cards(session_factory: Any, *, batch_size: int) -> int:
    """Backstop for the terminal transition's close of an agent loop's question.

    The terminal transition expires it in its own transaction; any terminal
    agent-loop Task that still has an open input card (a write that bypassed
    that path, or a failed close) is found and closed here, every sweep.
    """

    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.services.task_agent_loop_hitl import expire_task_agent_loop_input_requests
    from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY

    async with session_factory() as db:
        task_ids = list((await db.execute(
            select(HitlRequest.origin_task_id)
            .join(Task, Task.id == HitlRequest.origin_task_id)
            .join(TaskCoordinatorRun, TaskCoordinatorRun.task_id == Task.id)
            .where(
                HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
                HitlRequest.action_key == TASK_INPUT_ACTION_KEY,
                HitlRequest.status == ApprovalStatus.PENDING.value,
                Task.status.in_(tuple(_TASK_TERMINAL_STATUSES)),
                TaskCoordinatorRun.engine == TaskCoordinatorEngine.AGENT_LOOP.value,
            )
            .distinct()
            .limit(max(1, min(int(batch_size), 50)))
        )).scalars().all())
    closed = 0
    for task_id in task_ids:
        try:
            async with session_factory() as db:
                task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
                if task is None or task.status not in _TASK_TERMINAL_STATUSES:
                    continue
                closed += await expire_task_agent_loop_input_requests(
                    db, task=task, reason=f"task_{task.status}",
                )
                await db.commit()
        except Exception:
            logger.exception("Task %s agent loop input card cleanup failed", task_id)
    return closed


async def consume_plan_cycle_intent_once(
    *,
    event_id: str,
    plan_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Handoff one durable Plan wake from CONTROL to the HEAVY queue."""

    from packages.core.database import create_worker_session
    from packages.core.tasks.ai_tasks import run_plan

    normalized_event_id = str(event_id or "").strip()
    normalized_plan_id = str(plan_id or "").strip()
    normalized_generation = str(wake_generation or "").strip()
    if not normalized_event_id or not normalized_plan_id or not normalized_generation:
        return {"status": "closed", "reason": "invalid_delivery"}

    session_factory = create_worker_session()
    async with session_factory() as db:
        event = (await db.execute(
            select(RuntimeOutboxEvent)
            .where(
                RuntimeOutboxEvent.id == normalized_event_id,
                RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == normalized_plan_id,
                RuntimeOutboxEvent.dedupe_key
                == plan_cycle_outbox_dedupe_key(normalized_plan_id),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if event is None:
            return {"status": "closed", "reason": "missing_event"}
        payload = dict(event.payload or {})
        if str(payload.get("wake_generation") or "") != normalized_generation:
            return {"status": "closed", "reason": "stale_generation"}
        if event.delivered_at is not None:
            return {"status": "closed", "reason": "already_acknowledged"}
        now = datetime.now(timezone.utc)
        if (
            event.last_error == PLAN_CYCLE_PENDING
            and event.available_at is not None
            and event.available_at > now
        ):
            # A delayed duplicate CONTROL message may still carry the current
            # generation after cycle_busy/supervisor re-armed it. Exact
            # identity does not authorize crossing its new not-before time.
            return {"status": "suppressed", "reason": "not_due"}
        plan, state = await _plan_cycle_delivery_state(db, event)
        if plan is None:
            event.delivered_at = datetime.now(timezone.utc)
            event.last_error = f"permanent:plan_cycle_intent_closed:{state}"
            await db.commit()
            return {"status": "closed", "reason": state}
        if (
            event.last_error == PLAN_CYCLE_AWAITING_HEAVY_ACK
            and event.available_at is not None
            and event.available_at > now
        ):
            return {"status": "suppressed", "reason": "heavy_handoff_pending"}
        try:
            run_plan.apply_async(
                args=[normalized_plan_id],
                kwargs={
                    "outbox_event_id": normalized_event_id,
                    "wake_generation": normalized_generation,
                },
            )
        except Exception as exc:
            event.attempt_count += 1
            event.available_at = _runtime_outbox_retry_available_at(
                now,
                attempt_count=event.attempt_count,
            )
            event.last_error = str(exc)[:2000]
            await db.commit()
            return {"status": "deferred", "reason": "broker_failure"}
        event.available_at = _runtime_outbox_ack_available_at(
            now,
            base_seconds=_PLAN_CYCLE_ACK_TIMEOUT_SECONDS,
            attempt_count=event.attempt_count,
        )
        event.last_error = PLAN_CYCLE_AWAITING_HEAVY_ACK
        await db.commit()
        return {
            "status": "dispatched",
            "plan_id": normalized_plan_id,
            "reason": reason,
        }


async def consume_task_coordinator_intent_once(
    *,
    event_id: str,
    task_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Handoff one exact Task-loop wake to HEAVY without ACKing business work."""

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        event = (await db.execute(
            select(RuntimeOutboxEvent)
            .where(
                RuntimeOutboxEvent.id == event_id,
                RuntimeOutboxEvent.event_type == TASK_COORDINATOR_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == task_id,
                RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task_id),
            )
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if event is None or event.delivered_at is not None:
            return {"status": "closed", "reason": "missing_or_acknowledged"}
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("wake_generation") != wake_generation:
            return {"status": "closed", "reason": "stale_generation"}
        now = datetime.now(timezone.utc)
        if (
            event.available_at is not None
            and event.available_at > now
            and event.last_error != TASK_COORDINATOR_AWAITING_ACK
        ):
            return {"status": "suppressed", "reason": "not_due"}
        run, state = await task_coordinator_delivery_state(db, event)
        if run is None:
            event.delivered_at = now
            event.last_error = f"permanent:task_coordinator_intent_closed:{state}"
            await db.commit()
            return {"status": "closed", "reason": state}
        from packages.core.models.task_coordinator import TaskCoordinatorEngine

        # The engine was frozen when the run was created; route on it, never
        # on the current flag value.
        consumer_task = (
            run_task_agent_loop_task
            if getattr(run, "engine", None) == TaskCoordinatorEngine.AGENT_LOOP.value
            else run_task_coordinator_task
        )
        try:
            consumer_task.apply_async(
                kwargs={
                    "event_id": event_id,
                    "task_id": task_id,
                    "wake_generation": wake_generation,
                    "reason": reason,
                },
            )
        except Exception as exc:
            event.attempt_count += 1
            event.available_at = _runtime_outbox_retry_available_at(
                now, attempt_count=event.attempt_count,
            )
            event.last_error = str(exc)[:2000]
            await db.commit()
            return {"status": "deferred", "reason": "broker_failure"}
        event.available_at = _runtime_outbox_ack_available_at(
            now,
            base_seconds=_TASK_COORDINATOR_ACK_TIMEOUT_SECONDS,
            attempt_count=event.attempt_count,
        )
        event.last_error = _TASK_COORDINATOR_AWAITING_HEAVY_ACK
        await db.commit()
        return {"status": "dispatched", "task_id": task_id}


async def consume_task_execution_intent_once(
    *,
    event_id: str,
    task_id: str,
    intent_generation: str,
) -> dict[str, Any]:
    """Re-resolve and fan out one durable initial Workspace Task start."""

    from packages.core.database import create_worker_session

    normalized_event_id = str(event_id or "").strip()
    normalized_task_id = str(task_id or "").strip()
    normalized_generation = str(intent_generation or "").strip()
    if not normalized_event_id or not normalized_task_id or not normalized_generation:
        return {"status": "closed", "reason": "invalid_delivery"}

    session_factory = create_worker_session()
    async with session_factory() as db:
        # Admit in the same lock order as lifecycle transitions and Plan
        # materialization: Workspace -> Task -> Plan snapshot -> Run -> outbox.
        # Locking the outbox first would deadlock a concurrent Task retry that
        # stages its new start intent while holding the Task row.
        scope = (await db.execute(
            select(Task.entity_id, Task.workspace_id)
            .where(Task.id == normalized_task_id)
        )).one_or_none()
        if scope is None or not scope.workspace_id:
            return {"status": "closed", "reason": "missing_workspace_task"}
        workspace = await lock_workspace_access_boundary(
            db, workspace_id=scope.workspace_id, entity_id=scope.entity_id,
        )
        if workspace is None:
            return {"status": "deferred", "reason": "workspace_unavailable"}
        task = (await db.execute(
            select(Task).where(Task.id == normalized_task_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            return {"status": "closed", "reason": "missing_task"}
        dedupe_key = task_execution_intent_dedupe_key(
            normalized_task_id,
            normalized_generation,
        )
        # Do not lock this row until all Task/Plan/Run admission checks are
        # resolved. Its generation is rechecked under the lock below.
        event = (await db.execute(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.id == normalized_event_id,
                RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == normalized_task_id,
                RuntimeOutboxEvent.dedupe_key == dedupe_key,
            )
        )).scalar_one_or_none()
        if event is None:
            return {"status": "closed", "reason": "missing_event"}
        if event.delivered_at is not None:
            return {"status": "closed", "reason": "already_acknowledged", "task_id": normalized_task_id}
        dispatch, reason = await _task_execution_delivery_state(db, event)
        if dispatch is not None and dispatch.coordinator is not None:
            # Freeze the factory rows for this short admission transaction.
            # The publisher's earlier read is only a routing hint; a
            # subscription replacement cannot slip between pinning the Run
            # and committing its exact start wake.
            dispatch = await resolve_task_execution(
                db, task, lock_subscriptions=True,
            )
            if dispatch.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN or dispatch.coordinator is None:
                reason = "non_coordinated_runtime"
                dispatch = None
        if dispatch is not None and dispatch.coordinator is not None:
            await ensure_task_coordinator_run(
                db, task=task, selection=dispatch.coordinator,
            )
            wake = await stage_task_coordinator_wake(
                db, task_id=task.id, reason="task_start", if_pending=True,
            )
            if wake is None:
                raise RuntimeError("Task coordinator admission could not stage a wake")
        event = (await db.execute(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.id == normalized_event_id,
                RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == normalized_task_id,
                RuntimeOutboxEvent.dedupe_key == dedupe_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if event is None:
            return {"status": "closed", "reason": "missing_event"}
        if event.delivered_at is not None:
            return {
                "status": "closed",
                "reason": "already_acknowledged",
                "task_id": normalized_task_id,
            }
        if dispatch is not None and task_execution_intent_generation(task) != normalized_generation:
            raise RuntimeError("Task start generation changed under its admission lock")

        if dispatch is None:
            if reason in _TASK_EXECUTION_RETRYABLE_CLOSURES:
                # The Task routing identity is still current, but its exact
                # Workspace subscription is temporarily unavailable or
                # ambiguous.  ACKing here would strand an in-progress Task;
                # retain the same durable intent so restoring the binding
                # automatically resumes admission without manufacturing a new
                # lifecycle generation.
                event.attempt_count += 1
                event.available_at = _runtime_outbox_retry_available_at(
                    datetime.now(timezone.utc),
                    attempt_count=event.attempt_count,
                )
                event.last_error = f"{_TASK_EXECUTION_DEFERRED_PREFIX}: {reason}"
                await db.commit()
                return {
                    "status": "deferred",
                    "reason": reason,
                    "task_id": normalized_task_id,
                }
            event.delivered_at = datetime.now(timezone.utc)
            event.last_error = _TASK_EXECUTION_CLOSED_SENTINEL
            await db.commit()
            return {
                "status": "closed",
                "reason": reason,
                "task_id": normalized_task_id,
            }
        # The TaskRun and its stable wake are durable in this transaction.
        # The regular outbox publisher owns CONTROL -> HEAVY delivery; a
        # broker failure cannot lose this Task start.
        event.delivered_at = datetime.now(timezone.utc)
        event.last_error = None
        await db.commit()
        return {
            "status": "admitted",
            "task_id": normalized_task_id,
            "runtime_kind": dispatch.runtime_kind.value,
        }


def _runtime_terminal_error_from_frame(frame: str) -> dict[str, str] | None:
    """Extract a terminal Chat error so the durable run cannot report success."""

    event, payload = parse_sse_frame(frame)
    if event not in {"error", "stream_end"} or not payload:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    message = data.get("message") if event == "error" else data.get("error")
    if not message:
        return None
    code = str(data.get("stop_reason") or "runtime_execution_failed").strip()
    return {
        "code": (code or "runtime_execution_failed")[:80],
        "message": str(message)[:2000],
    }


def _durable_external_sandbox_enabled() -> bool:
    settings = get_settings()
    return (
        settings.MANOR_RUNTIME_EXECUTION_MODE == "durable"
        and settings.SANDBOX_COORDINATION_MODE == "external-runner"
    )


async def _refresh_runner_health(runner_id: str) -> None:
    from packages.core.database import create_worker_session
    from packages.core.services.sandbox_sdk import SandboxClient

    settings = get_settings()
    session_factory = create_worker_session()
    async with session_factory() as db:
        runner = await db.get(SandboxRunner, runner_id)
        if runner is None or runner.status == SandboxRunnerStatus.DRAINING.value:
            return
        base_url = runner.base_url
    try:
        async with SandboxClient(
            base_url=base_url,
            timeout=10.0,
            api_token=settings.SANDBOX_API_TOKEN,
        ) as client:
            health = await client.health()
            # The listing is the truth about which containers still exist;
            # without it the sweep below is skipped rather than guessed.
            try:
                live_sandbox_ids: list[str] | None = [
                    info.sandbox_id for info in await client.list()
                ]
            except Exception as exc:  # noqa: BLE001 - health still counts
                logger.warning(
                    "Sandbox runner %s listing failed; vanished-instance sweep skipped: %s",
                    runner_id,
                    exc,
                )
                live_sandbox_ids = None
        async with session_factory() as db:
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                runner is None
                or runner.status == SandboxRunnerStatus.DRAINING.value
                or runner.base_url.rstrip("/") != base_url.rstrip("/")
            ):
                return
            if live_sandbox_ids is not None:
                vanished = await release_vanished_sandbox_instances(
                    db, runner_id=runner.id, live_sandbox_ids=live_sandbox_ids,
                )
                if vanished:
                    logger.info(
                        "Sandbox runner %s: released %d instance(s) the runner no longer has: %s",
                        runner_id,
                        len(vanished),
                        ", ".join(vanished),
                    )
            runner.status = SandboxRunnerStatus.HEALTHY.value
            runner.last_health_at = datetime.now(timezone.utc)
            runner.last_error = None
            observed_active = max(
                int(health.get("active_sandboxes") or 0),
                int(health.get("reserved_active") or 0),
            )
            runner.active_count = await reconciled_runner_active_count(
                db,
                runner_id=runner.id,
                observed_active=observed_active,
            )
            runner.executing_count = max(0, int(health.get("executing") or 0))
            runner_config = dict(runner.config or {})
            has_active_limit = health.get("max_active") is not None
            has_executing_limit = health.get("max_executing") is not None
            if has_active_limit != has_executing_limit:
                raise ValueError("Sandbox runner health returned incomplete capacity")
            if has_active_limit:
                observed_active_limit = int(health["max_active"])
                observed_executing_limit = int(health["max_executing"])
                if observed_active_limit <= 0 or observed_executing_limit <= 0:
                    raise ValueError("Sandbox runner health returned invalid capacity")
                runner.active_limit, runner.executing_limit = effective_runner_limits(
                    observed_active=observed_active_limit,
                    observed_executing=observed_executing_limit,
                    active_cap=int(runner_config.get("active_cap") or 0),
                    executing_cap=int(runner_config.get("executing_cap") or 0),
                )
                runner.config = {
                    **runner_config,
                    "observed_active_limit": observed_active_limit,
                    "observed_executing_limit": observed_executing_limit,
                }
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            runner = (
                await db.execute(
                    select(SandboxRunner)
                    .where(SandboxRunner.id == runner_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                runner is not None
                and runner.status != SandboxRunnerStatus.DRAINING.value
                and runner.base_url.rstrip("/") == base_url.rstrip("/")
            ):
                runner.status = SandboxRunnerStatus.UNHEALTHY.value
                runner.last_health_at = datetime.now(timezone.utc)
                runner.last_error = str(exc)[:2000]
                await db.commit()


async def _create_reserved_sandbox(
    *,
    reservation_id: str,
    base_url: str,
    payload: dict[str, Any],
) -> str:
    from packages.core.services.sandbox_sdk import SandboxClient

    settings = get_settings()
    source = str(payload.get("source") or "workspace")
    kwargs = {
        "skill_name": str(payload["skill_name"]),
        "files": dict(payload.get("files") or {}),
        "env": dict(payload.get("env") or {}),
        "allowed_sensitive_keys": list(payload.get("allowed_sensitive_keys") or []),
        "config": payload.get("config"),
        "auto_install": bool(payload.get("auto_install", True)),
        "idempotency_key": reservation_id,
    }
    async with SandboxClient(
        base_url=base_url,
        timeout=180.0,
        api_token=settings.SANDBOX_API_TOKEN,
    ) as client:
        if source == "builtin":
            result = await client.create_from_builtin(**kwargs)
        else:
            result = await client.create_from_files(**kwargs)
    return result.sandbox_id


async def run_sandbox_scheduler_once() -> dict[str, Any]:
    """Reconcile capacity and enqueue at most one external allocation."""

    if not _durable_external_sandbox_enabled():
        return {"enabled": False, "allocated": False}

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await reconcile_sandbox_runners(db, configured_sandbox_runners())
        runner_ids = await claim_due_sandbox_runner_health_checks(db, now=now)
        await db.commit()
    await asyncio.gather(*(_refresh_runner_health(runner_id) for runner_id in runner_ids))

    async with session_factory() as db:
        expired = await expire_sandbox_reservations(db)
        recovered = await recover_stale_sandbox_allocations(db)
        claim = await claim_next_sandbox_allocation(db)
        if claim is None:
            await db.commit()
            return {
                "enabled": True,
                "allocation_queued": False,
                "expired": expired,
                "recovered": recovered,
            }
        reservation, _runner = claim
        await db.commit()
        return {
            "enabled": True,
            "allocation_queued": True,
            "expired": expired,
            "recovered": recovered,
            "reservation_id": reservation.id,
        }


async def allocate_reserved_sandbox_once(
    *,
    reservation_id: str,
    reservation_version: int,
) -> dict[str, Any]:
    """Create one claimed Sandbox without occupying the control worker."""

    if not _durable_external_sandbox_enabled():
        return {"enabled": False, "claimed": False}
    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        reservation = (
            await db.execute(
                select(SandboxReservation)
                .where(SandboxReservation.id == reservation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            reservation is None
            or reservation.status != SandboxReservationStatus.ALLOCATING.value
            or reservation.version != reservation_version
            or not reservation.runner_id
        ):
            await db.commit()
            return {"enabled": True, "claimed": False, "reservation_id": reservation_id}
        runner = await db.get(SandboxRunner, reservation.runner_id)
        if runner is None:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error="sandbox_runner_missing",
            )
            await db.commit()
            return {"enabled": True, "claimed": False, "reservation_id": reservation_id}
        runner_id = runner.id
        base_url = runner.base_url
        payload = dict(reservation.request_payload or {})
        await db.commit()

    try:
        sandbox_id = await _create_reserved_sandbox(
            reservation_id=reservation_id,
            base_url=base_url,
            payload=payload,
        )
    except Exception as exc:
        async with session_factory() as db:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error=exc.__class__.__name__,
            )
            await db.commit()
        logger.warning(
            "Sandbox reservation allocation failed reservation=%s runner=%s type=%s",
            reservation_id,
            runner_id,
            exc.__class__.__name__,
        )
        return {"enabled": True, "claimed": True, "allocated": False}

    try:
        async with session_factory() as db:
            await complete_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                sandbox_id=sandbox_id,
                runner_id=runner_id,
                expected_reservation_version=reservation_version,
            )
            await db.commit()
    except Exception as exc:
        from packages.core.services.sandbox_sdk import SandboxClient

        try:
            async with SandboxClient(
                base_url=base_url,
                timeout=30.0,
                api_token=get_settings().SANDBOX_API_TOKEN,
            ) as client:
                await client.destroy(sandbox_id)
        except Exception:
            logger.warning(
                "Failed to destroy uncommitted Sandbox allocation reservation=%s",
                reservation_id,
                exc_info=True,
            )
        async with session_factory() as db:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error=exc.__class__.__name__,
            )
            await db.commit()
        return {"enabled": True, "claimed": True, "allocated": False}
    return {
        "enabled": True,
        "claimed": True,
        "allocated": True,
        "reservation_id": reservation_id,
    }


async def dispatch_runtime_outbox_once(*, batch_size: int = 20) -> int:
    """Recover stale runs and publish pending versioned outbox rows."""

    from packages.core.database import create_worker_session

    now = datetime.now(timezone.utc)
    session_factory = create_worker_session()
    async with session_factory() as db:
        # Chat lifecycle belongs to Runtime, not to the external Sandbox
        # scheduler. Local deployments also park questions and recover leases.
        from packages.core.services.runtime_run_service import (
            recover_expired_runtime_run_leases,
        )

        recovered, failed = await recover_expired_runtime_run_leases(
            db, now=now, batch_size=batch_size,
        )
        if recovered or failed:
            logger.warning(
                "Runtime lease recovery sweep recovered=%d failed=%d",
                recovered, failed,
            )
        # Each timed-out run stages its own terminal notice (Message settled,
        # SSE frames + realtime refresh delivered by the loop below).
        await expire_runtime_human_input_requests(db, now=now)
        await _recover_task_runtime_resume_outbox(
            db,
            now=now,
            batch_size=batch_size,
        )
        # Recovery may have inserted or re-armed the exact row the broker
        # message names. Commit before publication: a fast CONTROL consumer
        # must never observe ``missing_event`` merely because it beat this
        # transaction's final commit. The following SELECT starts a fresh
        # transaction and claims only committed due rows.
        await db.commit()
        # Resume-recovery timestamps can be a few microseconds after the
        # publisher's original clock. Refresh before selecting due rows so an
        # immediately re-armed event can publish in this tick. Plan lost-wake
        # recovery runs in its own lower-frequency task.
        now = datetime.now(timezone.utc)
        due_filters = (
            RuntimeOutboxEvent.delivered_at.is_(None),
            or_(
                RuntimeOutboxEvent.available_at.is_(None),
                RuntimeOutboxEvent.available_at <= now,
            ),
        )
        due_order = (
            RuntimeOutboxEvent.available_at.asc(),
            RuntimeOutboxEvent.created_at.asc(),
            RuntimeOutboxEvent.id.asc(),
        )
        # Reserve capacity in both directions. A hot fleet of stable Plan rows
        # cannot starve Task/approval/Sandbox traffic, and an older generic
        # backlog cannot prevent Plan cycles from advancing. The final query
        # fills unused quota from either category, preserving throughput.
        category_quota = max(1, batch_size // 2) if batch_size > 1 else 0
        events: list[RuntimeOutboxEvent] = []
        if category_quota:
            events.extend((await db.execute(
                select(RuntimeOutboxEvent)
                .where(
                    *due_filters,
                    _runtime_outbox_plan_category_filter(plan_cycle=False),
                )
                .order_by(*due_order)
                .limit(category_quota)
                .with_for_update(skip_locked=True)
            )).scalars().all())
            events.extend((await db.execute(
                select(RuntimeOutboxEvent)
                .where(
                    *due_filters,
                    _runtime_outbox_plan_category_filter(plan_cycle=True),
                )
                .order_by(*due_order)
                .limit(category_quota)
                .with_for_update(skip_locked=True)
            )).scalars().all())
        remaining = max(0, int(batch_size) - len(events))
        if remaining:
            selected_ids = [event.id for event in events]
            fill_query = select(RuntimeOutboxEvent).where(*due_filters)
            if selected_ids:
                fill_query = fill_query.where(
                    RuntimeOutboxEvent.id.not_in(selected_ids)
                )
            events.extend((await db.execute(
                fill_query
                # Match the partial due index and rotate redeliveries by
                # their next eligible time. Ordering only by the stable
                # ``created_at`` let one hot, old Plan row monopolize each
                # bounded batch and starve newer event categories.
                .order_by(*due_order)
                .limit(remaining)
                .with_for_update(skip_locked=True)
            )).scalars().all())
        delivered = 0
        for event in events:
            try:
                payload = dict(event.payload or {})
                task_execution_event = (
                    event.event_type == TASK_EXECUTION_OUTBOX_EVENT
                )
                task_coordinator_event = (
                    event.event_type == TASK_COORDINATOR_OUTBOX_EVENT
                )
                plan_cycle_event = event.event_type == PLAN_CYCLE_OUTBOX_EVENT
                if event.event_type == RUNTIME_RUN_TERMINAL_NOTICE_EVENT:
                    # Not a Celery task: the committed terminal transition is
                    # announced here, and a failed publish retries like any row.
                    await announce_runtime_run_terminal(
                        db, str(payload.get("run_id") or event.aggregate_id),
                    )
                    event.attempt_count += 1
                    event.delivered_at = now
                    event.last_error = None
                    delivered += 1
                    continue
                if task_execution_event:
                    dispatch, reason = await _task_execution_delivery_state(
                        db,
                        event,
                    )
                    if dispatch is None:
                        if reason in _TASK_EXECUTION_RETRYABLE_CLOSURES:
                            event.attempt_count += 1
                            event.available_at = _runtime_outbox_retry_available_at(
                                now,
                                attempt_count=event.attempt_count,
                            )
                            event.last_error = (
                                f"{_TASK_EXECUTION_DEFERRED_PREFIX}: {reason}"
                            )
                            continue
                        event.delivered_at = now
                        event.last_error = _TASK_EXECUTION_CLOSED_SENTINEL
                        continue
                    payload["event_id"] = event.id
                elif task_coordinator_event:
                    run, reason = await task_coordinator_delivery_state(db, event)
                    if run is None:
                        event.delivered_at = now
                        event.last_error = (
                            f"permanent:task_coordinator_intent_closed:{reason}"
                        )
                        continue
                    payload["event_id"] = event.id
                elif plan_cycle_event:
                    plan, reason = await _plan_cycle_delivery_state(db, event)
                    if plan is None:
                        event.delivered_at = now
                        event.last_error = (
                            f"permanent:plan_cycle_intent_closed:{reason}"
                        )
                        continue
                    payload["event_id"] = event.id
                if task_coordinator_event or plan_cycle_event:
                    # Version belongs to the durable envelope, not the Celery
                    # consumer signature. Consumers reload and fence the row.
                    payload.pop("version", None)
                celery_app.send_task(
                    event.event_type,
                    kwargs=payload,
                    queue=queue_for_task(event.event_type).value,
                )
                event.attempt_count += 1
                if task_execution_event:
                    # Broker acceptance is not execution ownership. Keep the
                    # row in the indexed pending set until the consumer ACKs;
                    # if it crashes before creating a Plan, the same event is
                    # eligible for redelivery after this bounded deadline.
                    event.available_at = _runtime_outbox_ack_available_at(
                        now,
                        base_seconds=_TASK_EXECUTION_ACK_TIMEOUT_SECONDS,
                        attempt_count=event.attempt_count,
                    )
                    event.last_error = _TASK_EXECUTION_AWAITING_ACK
                elif task_coordinator_event:
                    # Broker acceptance is not a Task-loop checkpoint or
                    # completion. The future consumer ACKs only its exact
                    # generation; until then the stable row stays sweepable.
                    event.available_at = _runtime_outbox_ack_available_at(
                        now,
                        base_seconds=_TASK_COORDINATOR_ACK_TIMEOUT_SECONDS,
                        attempt_count=event.attempt_count,
                    )
                    event.last_error = TASK_COORDINATOR_AWAITING_ACK
                elif plan_cycle_event:
                    # Broker acceptance only reached the CONTROL consumer.
                    # Keep the generation pending through its HEAVY handoff
                    # and until the Plan cycle itself settles it.
                    event.available_at = _runtime_outbox_ack_available_at(
                        now,
                        base_seconds=_PLAN_CYCLE_ACK_TIMEOUT_SECONDS,
                        attempt_count=event.attempt_count,
                    )
                    event.last_error = PLAN_CYCLE_AWAITING_CONTROL_ACK
                else:
                    event.delivered_at = now
                    event.last_error = None
                delivered += 1
            except Exception as exc:
                event.attempt_count += 1
                event.last_error = str(exc)[:2000]
                event.available_at = _runtime_outbox_retry_available_at(
                    now,
                    attempt_count=event.attempt_count,
                )
        await db.commit()
        return delivered


async def publish_runtime_run_outbox_now(run_id: str) -> bool:
    """Publish one committed ``runtime.execute_run`` row without the sweep.

    Chat turns otherwise wait for the next ``runtime.outbox_dispatch`` beat
    tick (up to a second). The row is claimed with SKIP LOCKED and only while
    undelivered, so a concurrent sweep publishes it at most once; anything
    that fails here is left for that sweep.
    """
    from packages.core.database import async_session

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        event = (await db.execute(
            select(RuntimeOutboxEvent)
            .where(
                RuntimeOutboxEvent.dedupe_key == f"runtime.execute_run:{run_id}:1",
                RuntimeOutboxEvent.event_type == "runtime.execute_run",
                RuntimeOutboxEvent.delivered_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if event is None:
            return False
        celery_app.send_task(
            event.event_type,
            kwargs=dict(event.payload or {}),
            queue=queue_for_task(event.event_type).value,
        )
        event.attempt_count += 1
        event.delivered_at = now
        event.last_error = None
        await db.commit()
        return True


@celery_app.task(
    bind=True,
    name=TASK_EXECUTION_OUTBOX_EVENT,
    max_retries=3,
)
def dispatch_task_execution_intent_task(
    self,
    event_id: str,
    task_id: str,
    intent_generation: str,
) -> dict[str, Any]:
    try:
        return run_in_worker(consume_task_execution_intent_once(
            event_id=event_id,
            task_id=task_id,
            intent_generation=intent_generation,
        ))
    except Exception as exc:
        countdown = min(60, 5 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(
    bind=True,
    name=TASK_COORDINATOR_OUTBOX_EVENT,
    max_retries=3,
)
def dispatch_task_coordinator_intent_task(
    self,
    event_id: str,
    task_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        return run_in_worker(consume_task_coordinator_intent_once(
            event_id=event_id,
            task_id=task_id,
            wake_generation=wake_generation,
            reason=reason,
        ))
    except Exception as exc:
        countdown = min(60, 5 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(
    bind=True,
    name="runtime.run_task_coordinator",
    max_retries=0,
    soft_time_limit=540,
    time_limit=600,
)
def run_task_coordinator_task(
    self,
    event_id: str,
    task_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    from packages.core.workers.task_coordinator import run_task_coordinator_once

    return run_in_worker(run_task_coordinator_once(
        event_id=event_id,
        task_id=task_id,
        wake_generation=wake_generation,
        reason=reason,
    ))


# A Task agent loop yields at its time slice (well below the soft limit) and
# re-stages its own wake, so one delivery never approaches the broker's
# visibility timeout. Ordering is guarded in test_orchestration_hardening.
TASK_AGENT_LOOP_SOFT_TIME_LIMIT_SECONDS = 3300
TASK_AGENT_LOOP_TIME_LIMIT_SECONDS = 3600


@celery_app.task(
    bind=True,
    name="runtime.run_task_agent_loop",
    max_retries=0,
    soft_time_limit=TASK_AGENT_LOOP_SOFT_TIME_LIMIT_SECONDS,
    time_limit=TASK_AGENT_LOOP_TIME_LIMIT_SECONDS,
)
def run_task_agent_loop_task(
    self,
    event_id: str,
    task_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    from packages.core.workers.task_agent_loop import run_task_agent_loop_once

    return run_in_worker(run_task_agent_loop_once(
        event_id=event_id,
        task_id=task_id,
        wake_generation=wake_generation,
        reason=reason,
    ))


@celery_app.task(
    bind=True,
    name=PLAN_CYCLE_OUTBOX_EVENT,
    max_retries=3,
)
def dispatch_plan_cycle_intent_task(
    self,
    event_id: str,
    plan_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        return run_in_worker(consume_plan_cycle_intent_once(
            event_id=event_id,
            plan_id=plan_id,
            wake_generation=wake_generation,
            reason=reason,
        ))
    except Exception as exc:
        countdown = min(60, 5 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(name="runtime.sandbox_scheduler", max_retries=0)
def sandbox_scheduler_task() -> dict[str, Any]:
    return run_in_worker(run_sandbox_scheduler_once())


@celery_app.task(name="runtime.outbox_dispatch", max_retries=0)
def runtime_outbox_dispatch_task() -> dict[str, int]:
    return {"delivered": run_in_worker(dispatch_runtime_outbox_once())}


@celery_app.task(name="runtime.plan_cycle_recovery", max_retries=0)
def plan_cycle_recovery_task() -> dict[str, int]:
    return {
        "recovered": run_in_worker(recover_stalled_plan_cycle_wakes_once())
    }


@celery_app.task(name="runtime.task_coordinator_recovery", max_retries=0)
def task_coordinator_recovery_task() -> dict[str, int]:
    results: dict[str, int] = {}
    for key, operation in (
        ("recovered", recover_stalled_task_coordinator_wakes_once),
        ("terminal_reconciled", reconcile_terminal_task_coordinator_runs_once),
        ("terminal_cleaned", recover_task_coordinator_terminal_cleanup_once),
    ):
        try:
            results[key] = run_in_worker(operation())
        except Exception:
            logger.exception("Task coordinator %s sweep failed", key)
            results[key] = 0
    return results


@celery_app.task(
    name="runtime.allocate_sandbox",
    max_retries=0,
    soft_time_limit=210,
    time_limit=240,
)
def allocate_sandbox_task(
    reservation_id: str,
    reservation_version: int,
) -> dict[str, Any]:
    return run_in_worker(
        allocate_reserved_sandbox_once(
            reservation_id=reservation_id,
            reservation_version=reservation_version,
        )
    )


async def _runtime_run_lease_heartbeat(run_id: str, worker_id: str) -> None:
    from packages.core.database import create_worker_session
    from packages.core.services.runtime_run_service import renew_runtime_run_lease

    session_factory = create_worker_session()
    delay = 30.0
    while True:
        await asyncio.sleep(delay)
        try:
            async with session_factory() as db:
                renewed = await renew_runtime_run_lease(
                    db,
                    run_id=run_id,
                    worker_id=worker_id,
                    lease_seconds=90,
                )
                await db.commit()
        except Exception:
            # The turn shares this loop's admission permit and may hold it for
            # seconds; a busy or flaky renewal must retry inside the 90s lease
            # instead of silently ending the heartbeat.
            logger.warning("Runtime run lease renewal failed run=%s; retrying", run_id, exc_info=True)
            delay = 5.0
            continue
        delay = 30.0
        if not renewed:
            return


async def execute_runtime_run_once(
    *,
    run_id: str,
    worker_id: str,
    reservation_id: str | None = None,
    reservation_version: int | None = None,
) -> dict[str, Any]:
    """Claim and execute one initial or resumed Chat Runtime run."""

    from packages.core.ai.runtime import runtime_stream_chat_turn
    from packages.core.database import create_worker_session
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
    from packages.core.models.task import Message
    from packages.core.services.runtime_run_service import (
        claim_runtime_run_execution,
        complete_runtime_run_execution,
    )

    session_factory = create_worker_session()
    async with session_factory() as db:
        run = await claim_runtime_run_execution(
            db,
            run_id=run_id,
            worker_id=worker_id,
            lease_seconds=90,
            expected_reservation_id=reservation_id,
            expected_reservation_version=reservation_version,
        )
        if run is None:
            await db.commit()
            return {"claimed": False, "run_id": run_id}
        payload = dict(run.execution_payload or {})
        checkpoint = dict(run.checkpoint or {}) if run.checkpoint else None
        await db.commit()

    owner_task = asyncio.current_task()
    lease_execution_active = True
    heartbeat = asyncio.create_task(_runtime_run_lease_heartbeat(run_id, worker_id))

    def stop_if_lease_lost(task: asyncio.Task) -> None:
        # A recovered run may be claimed by another worker while this one is
        # still inside a slow tool call. The old worker must stop as soon as
        # its lease heartbeat ends, before it can issue another tool action.
        if task.cancelled() or not lease_execution_active:
            return
        try:
            task.result()
        except Exception:
            logger.exception("Runtime lease heartbeat failed run=%s", run_id)
        else:
            logger.warning("Runtime lease lost while executing run=%s", run_id)
        if owner_task is not None and not owner_task.done():
            owner_task.cancel()

    heartbeat.add_done_callback(stop_if_lease_lost)
    try:
        terminal_error: dict[str, str] | None = None
        terminal_frame: str | None = None
        async for frame in runtime_stream_chat_turn(
            payload.get("message") or "",
            run.conversation_id,
            # Every producer records the surface; a missing one fails the run
            # instead of guessing a policy.
            surface=payload.get("surface"),
            entity_id=run.entity_id,
            user_id=run.user_id,
            agent_id=run.agent_id,
            workspace_id=run.workspace_id,
            assistant_message_id=run.assistant_message_id,
            manual_skill_refs=list(payload.get("manual_skill_refs") or []),
            disable_tools=bool(payload.get("disable_tools", False)),
            blocked_tools=list(payload.get("blocked_tools") or []),
            editor_context=payload.get("editor_context"),
            runtime_metadata=payload.get("runtime_metadata"),
            runtime_run_id=run.id,
            runtime_checkpoint=checkpoint,
        ):
            if parse_sse_frame(frame)[0] == "stream_end":
                terminal_frame = frame
            else:
                await append_runtime_sse_event(run.id, frame)
            frame_error = _runtime_terminal_error_from_frame(frame)
            if frame_error is not None:
                terminal_error = frame_error

        lease_execution_active = False

        async with session_factory() as db:
            current = await db.get(RuntimeRun, run.id)
            if current is not None and current.status == RuntimeRunStatus.WAITING_RESOURCE.value:
                await db.commit()
                return {"claimed": True, "waiting": True, "run_id": run.id}
            run_result = {"message_id": run.assistant_message_id}
            assistant_message = await db.get(Message, run.assistant_message_id)
            if assistant_message is not None and assistant_message.conversation_id == run.conversation_id:
                saved_meta = assistant_message.meta or {}
                for key in ("outcome", "capability_hints", "stop_reason"):
                    if key in saved_meta:
                        run_result[key] = saved_meta[key]
            completed = await complete_runtime_run_execution(
                db,
                run_id=run.id,
                worker_id=worker_id,
                result=run_result,
                error=terminal_error,
            )
            await db.commit()
        # A client may submit its next turn as soon as it sees stream_end.
        # Release the conversation's active-root constraint before publishing it.
        if terminal_frame is not None:
            await append_runtime_sse_event(run.id, terminal_frame)
        return {
            "claimed": True,
            "waiting": False,
            "run_id": run.id,
            "status": completed.status,
        }
    except Exception as exc:
        logger.exception("Durable Runtime run failed run=%s", run.id)
        async with session_factory() as db:
            current = await db.get(RuntimeRun, run.id)
            if current is not None and current.status not in RuntimeRunStatus.terminal():
                await complete_runtime_run_execution(
                    db,
                    run_id=run.id,
                    worker_id=worker_id,
                    error={"code": "runtime_execution_failed", "message": str(exc)[:2000]},
                )
                await db.commit()
        raise
    finally:
        lease_execution_active = False
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Runtime lease heartbeat ended with an error run=%s", run_id)


@celery_app.task(
    bind=True,
    name="runtime.execute_run",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=1860,
)
def execute_runtime_run_task(self, run_id: str) -> dict[str, Any]:
    worker_id = str(self.request.id or f"execute:{run_id}")
    try:
        return run_in_worker(execute_runtime_run_once(run_id=run_id, worker_id=worker_id))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)


@celery_app.task(
    bind=True,
    name="runtime.resume_run",
    max_retries=2,
    soft_time_limit=1800,
    time_limit=1860,
)
def resume_runtime_run_task(
    self,
    run_id: str,
    reservation_id: str | None = None,
    reservation_version: int | None = None,
) -> dict[str, Any]:
    # A sandbox resume names the allocated reservation; a human-input resume
    # has no reservation — the answer lives in the run's checkpoint.
    worker_id = str(self.request.id or f"resume:{run_id}")
    try:
        return run_in_worker(
            execute_runtime_run_once(
                run_id=run_id,
                worker_id=worker_id,
                reservation_id=reservation_id,
                reservation_version=reservation_version,
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)
