"""Celery control-plane tasks for durable Runtime and Sandbox coordination."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.orm import aliased

from packages.core.celery_app import celery_app
from packages.core.config import get_settings
from packages.core.constants.advisory_locks import AdvisoryLockNamespace
from packages.core.constants.task import TaskRuntimeKind, TaskStatus
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.models.task import Task
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.queues import queue_for_task
from packages.core.services.task_execution import (
    TASK_EXECUTION_OUTBOX_EVENT,
    TaskExecutionDispatch,
    resolve_task_execution,
    task_execution_intent_dedupe_key,
    task_execution_intent_generation,
)
from packages.core.models.task_run_journal import TaskRunJournalKind
from packages.core.services import durable_run
from packages.core.services.durable_run import close_run_wake, clear_run_claim_and_wake
from packages.core.services.task_run_journal import (
    append_agent_loop_run_journal,
    append_run_journal,
    terminal_payload,
    wake_payload,
)
from packages.core.services.task_agent_child_settlement import (
    cancel_root_delegation,
    settle_waiting_batch,
)
from packages.core.services.task_agent_child_wakeup import (
    TASK_AGENT_CHILD_AWAITING_ACK,
    TASK_AGENT_CHILD_AWAITING_HEAVY_ACK,
    TASK_AGENT_CHILD_CLOSED_CARD_PHASES,
    TASK_AGENT_CHILD_CLOSED_PREFIX,
    TASK_AGENT_CHILD_OUTBOX_EVENT,
    TASK_AGENT_CHILD_RECOVERY_PHASES,
    TASK_AGENT_CHILD_RUN_TASK,
    awaited_child_refusal,
    cancel_task_agent_children,
    parked_run_wake_time,
    stage_task_agent_child_wake,
    task_agent_child_delivery_state,
    task_agent_child_outbox_dedupe_key,
    waiting_child_batch,
)
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
    adopt_follower_reservations,
    list_exec_slot_waits,
    redeliver_exec_slot_resumes,
    wake_exec_slot_wait,
    claim_due_sandbox_runner_health_checks,
    claim_next_sandbox_allocation,
    classify_allocation_error,
    complete_sandbox_allocation,
    configured_sandbox_runners,
    count_waiting_sandbox_reservations,
    effective_runner_limits,
    expire_sandbox_reservations,
    fail_sandbox_allocation,
    load_pool_ceiling,
    next_waiting_ticket_need,
    normalize_runner_pool,
    parse_memory_mb,
    reconciled_runner_active_count,
    reconcile_sandbox_runners,
    record_runner_memory,
    recover_stale_sandbox_allocations,
    recover_waiting_sandbox_runs,
    sandbox_create_idempotency_key,
    release_vanished_sandbox_instances,
    runner_memory_deficit_mb,
    runner_memory_from_health,
    sandboxes_free_for_handover,
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

_TASK_EXECUTION_ACK_TIMEOUT_SECONDS = 120
_TASK_COORDINATOR_ACK_TIMEOUT_SECONDS = 120
_TASK_COORDINATOR_AWAITING_HEAVY_ACK = "task coordinator heavy execution awaiting acknowledgment"
_TASK_AGENT_CHILD_ACK_TIMEOUT_SECONDS = 120
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
# A Task agent loop's root run waiting on a person is woken by the sweep only
# once its card closed without a decision (recover_runs_on_closed_cards_once).
TASK_AGENT_ROOT_CLOSED_CARD_PHASES = frozenset({TaskCoordinatorPhase.WAITING_HUMAN.value})
_TASK_TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.FAILED.value,
})
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

    dispatch = await resolve_task_execution(db, task)
    if dispatch.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN:
        return None, "non_coordinated_runtime"
    return dispatch, "current"


# When a parked agent loop (root or child) with no pending wake should wake.
_parked_agent_loop_wake_time = parked_run_wake_time


async def recover_stalled_task_coordinator_wakes_once(*, batch_size: int = 100) -> int:
    """Bounded backstop for missing Task run wakes; never poll live children."""

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
        for run, task in run_tasks:
            # A Task retry can leave a stale terminal generation on the old
            # Run. Only the exact start intent may manufacture a recovery
            # wake; the retry initializer owns opening its new generation.
            if run.lifecycle_generation != task_execution_intent_generation(task):
                continue
            if run.phase == TaskCoordinatorPhase.WAITING_CHILD.value:
                # An agent loop waits on its delegated child runs, which move
                # themselves forward. A batch whose children all ended but
                # that nothing settled is settled here, in the run's lock
                # (Run -> child rows), which also stages its wake. A batch
                # that settled (its wake was lost) or whose wait is malformed
                # (the worker surfaces it) is woken.
                batch = await waiting_child_batch(db, run)
                if batch.state == "running":
                    continue
                if batch.state == "all_terminal" and await settle_waiting_batch(db, task=task, parent=run):
                    recovered += 1
                    continue
                # Nothing to settle into (no parked call): the woken worker
                # gives the call an honest unavailable result.
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
                checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
                # A lost wake restored by the backstop; a parked run names its wait.
                await append_agent_loop_run_journal(
                    db, run=run, kind=TaskRunJournalKind.WAKE,
                    payload=wake_payload(
                        checkpoint.get("task_loop_wait") if run.phase in {
                            TaskCoordinatorPhase.WAITING_TIMER.value,
                            TaskCoordinatorPhase.WAITING_JOB.value,
                            TaskCoordinatorPhase.WAITING_CHILD.value,
                        } else None,
                        resolution="recovered", task_resumed=False,
                    ),
                )
        await db.commit()
        return recovered


async def recover_stalled_task_agent_child_wakes_once(*, batch_size: int = 100) -> int:
    """Backstop for lost wakes of delegated child runs; never poll live ones.

    A candidate is a non-terminal child in a recoverable phase with no live
    claim and no pending wake of its current generation, whose Task is in
    progress, whose lifecycle is the Task's current one and whose direct
    parent (root run or child row) is waiting_child. A ready/running child is
    restaged now; a timer/job wait at its own time; a waiting_child child only
    once its own batch settled, ended or is malformed. A child waiting on a
    person is never woken here (the person's answer reopens it).
    """

    from packages.core.database import create_worker_session

    parent_child = aliased(TaskAgentChildRun)
    session_factory = create_worker_session()
    async with session_factory() as db:
        dialect = getattr(getattr(db, "bind", None), "dialect", None)
        if getattr(dialect, "name", None) == "postgresql":
            claimed = await db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :sweep_key)"),
                {
                    "namespace": int(AdvisoryLockNamespace.PLAN_CYCLE_RECOVERY_SWEEP),
                    "sweep_key": 2,
                },
            )
            if not claimed:
                return 0
        now = datetime.now(timezone.utc)
        pending_event = exists().where(
            RuntimeOutboxEvent.dedupe_key == (
                "task-agent-child:" + TaskAgentChildRun.id
            ),
            RuntimeOutboxEvent.delivered_at.is_(None),
            RuntimeOutboxEvent.payload["wake_generation"].as_string()
            == TaskAgentChildRun.wake_generation,
        ).correlate(TaskAgentChildRun)
        root_waiting = exists().where(
            TaskCoordinatorRun.id == TaskAgentChildRun.parent_ref,
            TaskCoordinatorRun.task_id == TaskAgentChildRun.task_id,
            TaskCoordinatorRun.phase == TaskCoordinatorPhase.WAITING_CHILD.value,
        ).correlate(TaskAgentChildRun)
        parent_child_waiting = exists().where(
            parent_child.id == TaskAgentChildRun.parent_child_id,
            parent_child.task_id == TaskAgentChildRun.task_id,
            parent_child.phase == TaskCoordinatorPhase.WAITING_CHILD.value,
        ).correlate(TaskAgentChildRun)
        child_tasks = list((await db.execute(
            select(TaskAgentChildRun, Task)
            .join(Task, Task.id == TaskAgentChildRun.task_id)
            .where(
                Task.status == TaskStatus.IN_PROGRESS.value,
                TaskAgentChildRun.phase.in_(tuple(sorted(TASK_AGENT_CHILD_RECOVERY_PHASES))),
                ~pending_event,
                or_(
                    TaskAgentChildRun.claim_expires_at.is_(None),
                    TaskAgentChildRun.claim_expires_at <= now,
                ),
                or_(
                    TaskAgentChildRun.parent_child_id.is_(None) & root_waiting,
                    TaskAgentChildRun.parent_child_id.is_not(None) & parent_child_waiting,
                ),
            )
            .order_by(
                func.coalesce(
                    TaskAgentChildRun.updated_at,
                    TaskAgentChildRun.created_at,
                ).asc(),
                TaskAgentChildRun.id.asc(),
            )
            .limit(max(1, min(int(batch_size), 100)))
            .with_for_update(skip_locked=True, of=TaskAgentChildRun)
        )).all())
        recovered = 0
        for child, task in child_tasks:
            # The same rule as delivery: a wake the publisher would close as
            # unusable is never staged (it would be restaged every tick).
            if await awaited_child_refusal(db, child, task) is not None:
                continue
            if child.phase == TaskCoordinatorPhase.WAITING_CHILD.value:
                batch = await waiting_child_batch(db, child)
                if batch.state == "running":
                    continue
                # Settled in the child's lock, which stages its wake.
                if batch.state == "all_terminal" and await settle_waiting_batch(db, task=task, parent=child):
                    recovered += 1
                    continue
            available_at = None
            if child.phase in {
                TaskCoordinatorPhase.WAITING_TIMER.value,
                TaskCoordinatorPhase.WAITING_JOB.value,
            }:
                available_at = await _parked_agent_loop_wake_time(db, child, now=now)
            wake = await stage_task_agent_child_wake(
                db, child_id=child.id, reason="stalled_task_agent_child_backstop",
                available_at=available_at,
            )
            if wake is not None:
                recovered += 1
                checkpoint = child.checkpoint if isinstance(child.checkpoint, dict) else {}
                await append_run_journal(
                    db, run=child, kind=TaskRunJournalKind.WAKE,
                    payload=wake_payload(
                        checkpoint.get("task_loop_wait") if child.phase in {
                            TaskCoordinatorPhase.WAITING_TIMER.value,
                            TaskCoordinatorPhase.WAITING_JOB.value,
                            TaskCoordinatorPhase.WAITING_CHILD.value,
                        } else None,
                        resolution="recovered", task_resumed=False,
                    ),
                )
        await db.commit()
        return recovered


async def reconcile_terminal_task_coordinator_runs_once(*, batch_size: int = 50) -> int:
    """Stop an agent loop after an external Task terminal transition.

    A user/system cancellation or failure terminalizes the Task first; its run
    and delegated child runs stop here, under Task -> Run -> child locks, so
    no stale wake continues the loop after that boundary.
    """

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    limit = max(1, min(int(batch_size), 50))
    async with session_factory() as db:
        candidates = list((await db.execute(
            select(TaskCoordinatorRun.task_id)
            .join(Task, Task.id == TaskCoordinatorRun.task_id)
            .where(
                Task.status.in_(tuple(_TASK_TERMINAL_STATUSES)),
                TaskCoordinatorRun.phase.not_in(tuple(TaskCoordinatorPhase.terminal())),
            )
            .order_by(TaskCoordinatorRun.updated_at, TaskCoordinatorRun.id)
            .limit(limit)
        )).scalars().all())
        # Terminal Tasks whose delegated child runs still live (their root
        # may already have stopped). Scanned from the live child rows, so a
        # tick costs nothing while no child is live.
        child_task_ids = (await db.execute(
            select(TaskAgentChildRun.task_id)
            .join(Task, Task.id == TaskAgentChildRun.task_id)
            .where(
                Task.status.in_(tuple(_TASK_TERMINAL_STATUSES)),
                TaskAgentChildRun.phase.not_in(tuple(TaskCoordinatorPhase.terminal())),
            )
            .group_by(TaskAgentChildRun.task_id)
            .order_by(
                func.min(func.coalesce(TaskAgentChildRun.updated_at, TaskAgentChildRun.created_at)),
                TaskAgentChildRun.task_id,
            )
            .limit(limit)
        )).scalars().all()
        seen = set(candidates)
        candidates.extend(task_id for task_id in child_task_ids if task_id not in seen)
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
            if run is None or run.phase in TaskCoordinatorPhase.terminal():
                reconciled += await _reconcile_terminal_task_children(db, task, run)
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
                reconciled += await _reconcile_terminal_task_children(db, task, run)
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
            # The run stops mid-work because its Task was cancelled or failed
            # (who did it is on the Task's status-change log and ledger event).
            await append_agent_loop_run_journal(
                db, run=run, kind=TaskRunJournalKind.TERMINAL,
                payload=terminal_payload(
                    status=task.status, phase=run.phase, error_code=f"task_{task.status}",
                    files=None, source="task_status",
                ),
            )
            await _stop_task_children(db, task, run)
            await db.commit()
            reconciled += 1
    await _close_terminal_agent_loop_input_cards(session_factory, batch_size=batch_size)
    return reconciled


async def _reconcile_terminal_task_children(db, task: Task, run: Any) -> int:
    """Cancel a terminal Task's live child runs when its root run is already stopped.

    Locks the root run (Task -> Run -> child order) and commits only when a
    child was cancelled. Returns 1 when it did, else 0.
    """

    if run is None:
        return 0
    root = await durable_run.lock_run(db, TASK_COORDINATOR_RUN_SPEC, task.id)
    if not await _stop_task_children(db, task, root):
        return 0
    await db.commit()
    return 1


async def _stop_task_children(db, task: Task, root: Any) -> int:
    """Cancel a terminal Task's open child runs (the caller holds Task -> root Run).

    An agent-loop root still parked on their batch gets the cancelled batch
    in its parked call, so a retry continues from what the children did
    (their receipts, an unknown last round) instead of "did not run".
    """

    from packages.core.models.task_agent_child import TASK_AGENT_CHILD_TERMINAL_PHASES

    open_ids = list((await db.execute(
        select(TaskAgentChildRun.id).where(
            TaskAgentChildRun.task_id == task.id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        )
    )).scalars().all())
    if not open_ids:
        return 0
    if (
        root is not None
        and root.engine == TaskCoordinatorEngine.AGENT_LOOP.value
        and isinstance(root.checkpoint, dict)
    ):
        root.checkpoint = await cancel_root_delegation(
            db, task=task, run=root, checkpoint=root.checkpoint, error_code=f"task_{task.status}",
        )
    else:
        await cancel_task_agent_children(db, task=task)
    return len(open_ids)


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
                TaskCoordinatorRun.engine.in_((TaskCoordinatorEngine.AGENT_LOOP.value, TaskCoordinatorEngine.PLAN_DAG.value)),
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


async def recover_ended_child_cards_once(*, batch_size: int = 50) -> int:
    """Backstop: close the open cards of delegated child runs that have ended.

    A child stopped by its parent (or any path that skipped the post-commit
    close) can leave its question or approval card open; nothing can resume
    it any more. Each Task found is cleaned in its own transaction under the
    approval-boundary -> HitlRequest -> Task order
    (``close_ended_child_cards``).
    """

    from packages.core.constants.approvals import (
        APPROVAL_LIVE_STATUSES,
        ApprovalOriginKind,
    )
    from packages.core.database import create_worker_session
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task_agent_child import TASK_AGENT_CHILD_TERMINAL_PHASES
    from packages.core.services.task_agent_loop_hitl import (
        TASK_AGENT_LOOP_HITL_CONTEXT_KEY,
        close_ended_child_cards,
    )

    session_factory = create_worker_session()
    owner = func.coalesce(
        HitlRequest.context["run_id"].astext,
        HitlRequest.context[(TASK_AGENT_LOOP_HITL_CONTEXT_KEY, "run_id")].astext,
    )
    async with session_factory() as db:
        task_ids = list((await db.execute(
            select(HitlRequest.origin_task_id)
            .join(
                TaskAgentChildRun,
                (TaskAgentChildRun.id == owner)
                & (TaskAgentChildRun.task_id == HitlRequest.origin_task_id),
            )
            .where(
                HitlRequest.origin_kind.in_((ApprovalOriginKind.TASK.value, ApprovalOriginKind.TOOL_CALL.value)),
                HitlRequest.status.in_(tuple(status.value for status in APPROVAL_LIVE_STATUSES)),
                TaskAgentChildRun.phase.in_(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
                # An external action crossed its linearization point: it is
                # reconciled, never expired (and would be picked forever).
                ~HitlRequest.context.has_key("provider_action_claim"),
            )
            .distinct()
            .order_by(HitlRequest.origin_task_id)
            .limit(max(1, min(int(batch_size), 50)))
        )).scalars().all())
    closed = 0
    for task_id in task_ids:
        try:
            async with session_factory() as db:
                closed += await close_ended_child_cards(db, task_id=str(task_id))
                await db.commit()
        except Exception:
            logger.exception("Task %s ended delegated run card cleanup failed", task_id)
    return closed


async def recover_runs_on_closed_cards_once(*, batch_size: int = 50) -> int:
    """Backstop: wake a Task agent loop run still parked on a card that is no longer pending.

    The one person-phase case the sweep handles, for the Task's root run and
    its delegated children alike. Every writer that closes a request
    undecided wakes its run in the same transaction
    (``wake_run_for_closed_request``); a close path that did not (or a
    decision made on a surface that does not route to the park) would leave
    the run waiting on nothing. A candidate is a ``waiting_human`` run of a
    non-terminal Task whose wait has no resolution and whose request is not
    pending. Each is settled in its own transaction under the approval
    boundary -> HitlRequest -> Task -> root Run -> child order, exactly as
    the card would have (``settle_closed_park``).
    """

    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.database import create_worker_session
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.services.task_agent_loop_hitl import wake_run_for_closed_request
    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    session_factory = create_worker_session()
    limit = max(1, min(int(batch_size), 50))

    def _closed(model, *, phases, extra=()):
        wait = model.checkpoint["task_loop_wait"]
        return (
            select(HitlRequest.id, model.task_id, model.id)
            .join(
                model,
                (HitlRequest.id == wait["hitl_request_id"].astext)
                & (HitlRequest.origin_task_id == model.task_id),
            )
            .join(Task, Task.id == model.task_id)
            .where(
                model.phase.in_(tuple(sorted(phases))),
                wait["resolution"].astext.is_(None),
                HitlRequest.status != ApprovalStatus.PENDING.value,
                Task.status.not_in(tuple(_TASK_TERMINAL_STATUSES)),
                *extra,
            )
            .order_by(model.id)
            .limit(limit)
        )

    async with session_factory() as db:
        candidates = list((await db.execute(_closed(
            TaskCoordinatorRun,
            phases=TASK_AGENT_ROOT_CLOSED_CARD_PHASES,
            extra=(TaskCoordinatorRun.engine == TaskCoordinatorEngine.AGENT_LOOP.value,),
        ))).all())
        candidates += list((await db.execute(_closed(
            TaskAgentChildRun, phases=TASK_AGENT_CHILD_CLOSED_CARD_PHASES,
        ))).all())
    woken = 0
    for request_id, task_id, _run_id in candidates[:limit]:
        try:
            async with session_factory() as db:
                await lock_task_runtime_approval_boundary(db, task_id=str(task_id))
                request = (await db.execute(
                    select(HitlRequest).where(HitlRequest.id == request_id)
                    .with_for_update().execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if request is not None and await wake_run_for_closed_request(db, request):
                    woken += 1
                await db.commit()
        except Exception:
            logger.exception("Task %s run on a closed card could not be woken", task_id)
    return woken


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
        try:
            # Every Task run is an agent loop (the retired engine's runs were
            # closed out by migration 20261025_10 and switch on retry).
            run_task_agent_loop_task.apply_async(
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


async def consume_task_agent_child_intent_once(
    *,
    event_id: str,
    task_id: str,
    child_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Handoff one exact child-run wake to HEAVY without ACKing business work."""

    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    async with session_factory() as db:
        event = (await db.execute(
            select(RuntimeOutboxEvent)
            .where(
                RuntimeOutboxEvent.id == event_id,
                RuntimeOutboxEvent.event_type == TASK_AGENT_CHILD_OUTBOX_EVENT,
                RuntimeOutboxEvent.aggregate_id == child_id,
                RuntimeOutboxEvent.dedupe_key == task_agent_child_outbox_dedupe_key(child_id),
            )
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if event is None or event.delivered_at is not None:
            return {"status": "closed", "reason": "missing_or_acknowledged"}
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            payload.get("wake_generation") != wake_generation
            or payload.get("task_id") != task_id
        ):
            return {"status": "closed", "reason": "stale_generation"}
        now = datetime.now(timezone.utc)
        if (
            event.available_at is not None
            and event.available_at > now
            and event.last_error != TASK_AGENT_CHILD_AWAITING_ACK
        ):
            return {"status": "suppressed", "reason": "not_due"}
        child, state = await task_agent_child_delivery_state(db, event)
        if child is None:
            event.delivered_at = now
            event.last_error = f"{TASK_AGENT_CHILD_CLOSED_PREFIX}:{state}"
            await db.commit()
            return {"status": "closed", "reason": state}
        try:
            run_task_agent_child_task.apply_async(
                kwargs={
                    "event_id": event_id,
                    "task_id": task_id,
                    "child_id": child_id,
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
            base_seconds=_TASK_AGENT_CHILD_ACK_TIMEOUT_SECONDS,
            attempt_count=event.attempt_count,
        )
        event.last_error = TASK_AGENT_CHILD_AWAITING_HEAVY_ACK
        await db.commit()
        return {"status": "dispatched", "child_id": child_id}


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
        if scope is None:
            return {"status": "closed", "reason": "missing_task"}
        if scope.workspace_id:
            # A Task assigned directly to an Agent has no Workspace boundary.
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
    code = str(data.get("error_code") or data.get("stop_reason") or "runtime_execution_failed").strip()
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
            # Memory placement needs the runner's free memory; an older runner
            # without it is placed by slots alone (mixed-version rollout).
            record_runner_memory(
                runner,
                runner_memory_from_health(health, now=runner.last_health_at),
            )
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
        # Attempts of one ticket share the runner's idempotent create, so a
        # lease retry gets the container its predecessor is still creating.
        # A ticket requeued because its container was lost bumps the
        # generation and asks for a new one.
        "idempotency_key": sandbox_create_idempotency_key(reservation_id, payload),
    }
    if payload.get("isolation_ref"):
        # Resolved only here, where the container is created: what it stands
        # for (credentials, for one) never sits in the ticket. Raises
        # SandboxIsolationUnavailable rather than create without it.
        from packages.core.services.sandbox_isolation import resolve_isolation_ref

        kwargs.update(await resolve_isolation_ref(str(payload["isolation_ref"])))
    from packages.core.services import sandbox_session_service

    async with SandboxClient(
        base_url=base_url,
        timeout=180.0,
        api_token=settings.SANDBOX_API_TOKEN,
    ) as client:
        # A create a new-runner feature shapes (egress) is refused unless the
        # runner's /health proves it supports that: without a loader the
        # client knows nothing of this runner and refuses every such create.
        client.capability_loader = functools.partial(sandbox_session_service.runner_capabilities, base_url)
        if source == "builtin":
            from packages.core.services.sandbox_baked_skills import (
                BakedSkillRef,
                create_builtin_sandbox,
            )

            # The image's copy when this runner holds the ticket's digest; the
            # packaged files (read here if the ticket carries none) otherwise.
            result = await create_builtin_sandbox(
                client,
                baked=BakedSkillRef.from_payload(payload.get("baked_skill")),
                **kwargs,
            )
        else:
            result = await client.create_from_files(**kwargs)
    return result.sandbox_id


async def _hand_finished_sandboxes_to_queue(session_factory: Any) -> int:
    """Give queued tickets the warm containers of conversations that ended.

    A finished run leaves its container warm for the conversation's next
    turn, holding a runner slot until the idle reaper (300-600 s). While
    tickets wait and a runner is full, that slot belongs to the queue: the
    least recently used containers whose conversations have no active run are
    destroyed, so the next claim (this tick) gets the slot. A container any
    run of its conversation still uses, or one running a command, is never
    taken. Returns the number of containers handed over.
    """

    from packages.core.services.sandbox_sdk import SandboxClient
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
    from packages.core.services.sandbox_session_service import forget_vanished_sandbox

    settings = get_settings()
    async with session_factory() as db:
        if not await count_waiting_sandbox_reservations(db):
            return 0
        # Per runner pool: only a ticket of the runner's own pool can take a
        # container it frees, so another pool's queue never costs it warm work.
        demand: dict[str, tuple[int, Any, Any]] = {}
        runners = []
        for runner in (
            await db.execute(
                select(SandboxRunner).where(
                    SandboxRunner.status == SandboxRunnerStatus.HEALTHY.value,
                )
            )
        ).scalars().all():
            pool = normalize_runner_pool(getattr(runner, "pool", None))
            if pool not in demand:
                waiting = await count_waiting_sandbox_reservations(db, pool=pool)
                need = await next_waiting_ticket_need(db, pool=pool) if waiting else None
                ceiling = await load_pool_ceiling(db, pool) if need is not None else None
                demand[pool] = (waiting, need, ceiling)
            waiting, need, ceiling = demand[pool]
            if not waiting:
                continue
            need_mb, need_cpus = need if need is not None else (None, None)
            slots_full = runner.active_count >= runner.active_limit
            # A runner with free slots can still be too full in memory for the
            # next ticket: then idle warm containers are freed until it fits.
            deficit_mb = 0 if slots_full else runner_memory_deficit_mb(
                runner, need_mb, need_cpus=need_cpus, ceiling=ceiling,
            )
            if slots_full or deficit_mb > 0:
                runners.append((runner.id, runner.base_url, slots_full, deficit_mb, pool))
    handed_over = 0
    handed_by_pool: dict[str, int] = {}
    for runner_id, base_url, slots_full, deficit_mb, pool in runners:
        waiting = demand[pool][0]
        if handed_by_pool.get(pool, 0) >= waiting:
            continue
        try:
            async with SandboxClient(
                base_url=base_url, timeout=15.0, api_token=settings.SANDBOX_API_TOKEN,
            ) as client:
                idle = [
                    info for info in await client.list()
                    if info.status == "ready" and not info.active_execution_id
                ]
                async with session_factory() as db:
                    free = await sandboxes_free_for_handover(
                        db, runner_id=runner_id, sandbox_ids=[info.sandbox_id for info in idle],
                    )
                for info in sorted(idle, key=lambda item: item.last_used_at or 0.0):
                    if slots_full and handed_by_pool.get(pool, 0) >= waiting:
                        break
                    if not slots_full and deficit_mb <= 0:
                        break
                    if info.sandbox_id not in free:
                        continue
                    try:
                        await client.destroy(info.sandbox_id)
                    except SandboxNotFoundError:
                        pass
                    # Drops the warm registration and releases the instance
                    # row, which returns the runner slot to the scheduler.
                    await forget_vanished_sandbox(info.sandbox_id)
                    handed_over += 1
                    handed_by_pool[pool] = handed_by_pool.get(pool, 0) + 1
                    if not slots_full:
                        # Unknown size: stop after this one rather than guess.
                        config = info.config if isinstance(info.config, dict) else {}
                        deficit_mb -= parse_memory_mb(config.get("memory")) or deficit_mb
                    logger.info(
                        "Sandbox runner %s: handed idle warm sandbox %s to the queue (%d waiting)",
                        runner_id,
                        info.sandbox_id,
                        waiting,
                    )
        except Exception:  # noqa: BLE001 - the next tick retries; the reaper is the backstop
            logger.warning("Sandbox runner %s: handover to the queue failed", runner_id, exc_info=True)
    return handed_over


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
        recovered += await recover_waiting_sandbox_runs(db)
        recovered += await adopt_follower_reservations(db)
        recovered += await redeliver_exec_slot_resumes(db)
        claim = await claim_next_sandbox_allocation(db)
        claimed_id = claim[0].id if claim is not None else None
        await db.commit()
    handed_over = 0
    if claimed_id is None:
        handed_over = await _hand_finished_sandboxes_to_queue(session_factory)
        if handed_over:
            async with session_factory() as db:
                claim = await claim_next_sandbox_allocation(db)
                claimed_id = claim[0].id if claim is not None else None
                await db.commit()
    woken = await _wake_exec_slot_waiters(session_factory)
    result = {
        "enabled": True,
        "allocation_queued": claimed_id is not None,
        "expired": expired,
        "recovered": recovered,
        "handed_over": handed_over,
        "exec_slot_woken": woken,
    }
    if claimed_id is not None:
        result["reservation_id"] = claimed_id
    return result


async def _wake_exec_slot_waiters(session_factory: Any) -> int:
    """Resume runs whose command left its sandbox's slot queue.

    One status call per sandbox with parked runs: a command no longer in the
    runner's ``queued_execution_ids`` has started (or finished, or was
    cancelled), and a sandbox the runner no longer has lost it -- either way
    the run resumes and its re-executed tool call collects the outcome by
    execution id. A ticket past its deadline wakes too, so a runner that
    stays unreachable never strands a run. A runner that cannot answer right
    now is asked again on the next tick.
    """

    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
    from packages.core.services.sandbox_session_service import sandbox_client_for

    async with session_factory() as db:
        tickets = await list_exec_slot_waits(db)
        waiting = [
            (
                ticket.id,
                str((ticket.request_payload or {}).get("sandbox_id") or ""),
                str((ticket.request_payload or {}).get("execution_id") or ""),
                ticket.deadline_at,
            )
            for ticket in tickets
        ]
    if not waiting:
        return 0
    now = datetime.now(timezone.utc)
    by_sandbox: dict[str, list[tuple[str, str, Any]]] = {}
    for ticket_id, sandbox_id, execution_id, deadline in waiting:
        by_sandbox.setdefault(sandbox_id, []).append((ticket_id, execution_id, deadline))

    async def probe(sandbox_id: str) -> tuple[set[str] | None, bool]:
        """(queued execution ids, gone) of one sandbox; (None, False) when unknown."""
        if not sandbox_id:
            return None, True
        try:
            async with await sandbox_client_for(sandbox_id, timeout=5.0) as client:
                info = await client.status(sandbox_id)
            return set(info.queued_execution_ids or []), False
        except SandboxNotFoundError:
            return None, True
        except Exception as exc:  # noqa: BLE001 - unknown: ask again next tick
            if type(exc).__name__ == "SandboxRouteNotFoundError":
                return None, True
            logger.info("Exec-slot sweep could not reach sandbox %s: %s", sandbox_id, exc)
            return None, False

    # One probe per sandbox, concurrently: a runner that does not answer
    # costs one short timeout per tick, never one per parked run.
    sandbox_ids = list(by_sandbox)
    probes = dict(zip(sandbox_ids, await asyncio.gather(*(probe(sandbox_id) for sandbox_id in sandbox_ids))))
    wake: list[tuple[str, str]] = []
    for sandbox_id, entries in by_sandbox.items():
        queued, gone = probes[sandbox_id]
        for ticket_id, execution_id, deadline in entries:
            if gone:
                wake.append((ticket_id, "sandbox_gone"))
            elif queued is not None and execution_id not in queued:
                wake.append((ticket_id, "left_queue"))
            elif deadline is not None and deadline <= now:
                wake.append((ticket_id, "deadline"))
    woken = 0
    for ticket_id, reason in wake:
        async with session_factory() as db:
            if await wake_exec_slot_wait(db, reservation_id=ticket_id, reason=reason):
                woken += 1
            await db.commit()
    return woken


async def _container_shared_with_newer_attempt(
    session_factory: Any,
    *,
    reservation_id: str,
    runner_id: str,
    sandbox_id: str,
) -> bool:
    """Does a newer allocation attempt of this ticket hold this container?

    Every attempt creates with the ticket id as the runner's idempotency key,
    so a later attempt on the same runner gets the same container while the
    ticket is still being allocated or was just allocated with it.
    """

    async with session_factory() as db:
        current = await db.get(SandboxReservation, reservation_id)
        return bool(
            current is not None
            and current.runner_id == runner_id
            and current.status in {
                SandboxReservationStatus.ALLOCATING.value,
                SandboxReservationStatus.ALLOCATED.value,
            }
            and current.sandbox_id in {None, sandbox_id}
        )


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
        # A full runner, or one restarting or unreachable, is not a failure
        # (the ticket waits for it until its deadline); a request the runner
        # rejects can never succeed (fail at once); anything else is retried
        # a bounded number of times.
        outcome, error = classify_allocation_error(exc)
        runner_report = None
        if outcome == "capacity":
            body = getattr(exc, "body", None)
            if isinstance(body, dict) and body.get("available_memory_mb") is not None:
                runner_report = runner_memory_from_health(
                    {
                        "memory_available_mb": body.get("available_memory_mb"),
                        "max_container_memory_mb": body.get("max_container_memory_mb"),
                        "memory_budget_mb": body.get("memory_budget_mb"),
                        "largest_allocatable_mb": body.get("largest_allocatable_mb"),
                    },
                    now=datetime.now(timezone.utc),
                )
        async with session_factory() as db:
            await fail_sandbox_allocation(
                db,
                reservation_id=reservation_id,
                expected_reservation_version=reservation_version,
                error=error,
                outcome=outcome,
                runner_report=runner_report,
            )
            await db.commit()
        logger.warning(
            "Sandbox reservation allocation failed reservation=%s runner=%s outcome=%s error=%s",
            reservation_id,
            runner_id,
            outcome,
            error[:300],
        )
        return {"enabled": True, "claimed": True, "allocated": False, "outcome": outcome}

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

        if await _container_shared_with_newer_attempt(
            session_factory, reservation_id=reservation_id, runner_id=runner_id, sandbox_id=sandbox_id,
        ):
            # Lease recovery requeued this ticket while our create was still
            # running, and a newer attempt on the same runner received the
            # same container (the create is idempotent on the ticket id). The
            # container is that attempt's to record; destroying it here would
            # hand the run a dead sandbox.
            logger.info(
                "Stale Sandbox allocation leaves the shared container to the newer attempt "
                "reservation=%s sandbox=%s",
                reservation_id,
                sandbox_id,
            )
            return {"enabled": True, "claimed": True, "allocated": False}
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
        # Recovery may have inserted the exact row the broker message names.
        # Commit before publication: a fast CONTROL consumer must never
        # observe ``missing_event`` merely because it beat this transaction's
        # final commit. The following SELECT starts a fresh transaction and
        # claims only committed due rows.
        await db.commit()
        # Recovery timestamps can be a few microseconds after the
        # publisher's original clock. Refresh before selecting due rows so an
        # immediately re-armed event can publish in this tick.
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
        events = list((await db.execute(
            select(RuntimeOutboxEvent)
            .where(*due_filters)
            # Rotate redeliveries by their next eligible time so one hot,
            # old row cannot monopolize each bounded batch.
            .order_by(*due_order)
            .limit(max(1, int(batch_size)))
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
                task_agent_child_event = (
                    event.event_type == TASK_AGENT_CHILD_OUTBOX_EVENT
                )
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
                elif task_agent_child_event:
                    child, reason = await task_agent_child_delivery_state(db, event)
                    if child is None:
                        event.delivered_at = now
                        event.last_error = f"{TASK_AGENT_CHILD_CLOSED_PREFIX}:{reason}"
                        continue
                    payload["event_id"] = event.id
                if task_coordinator_event or task_agent_child_event:
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
                    # if it crashes before creating its run, the same event is
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
                elif task_agent_child_event:
                    # Same as the Task run: the child's worker ACKs only its
                    # exact generation; until then the row stays sweepable.
                    event.available_at = _runtime_outbox_ack_available_at(
                        now,
                        base_seconds=_TASK_AGENT_CHILD_ACK_TIMEOUT_SECONDS,
                        attempt_count=event.attempt_count,
                    )
                    event.last_error = TASK_AGENT_CHILD_AWAITING_ACK
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
    name=TASK_AGENT_CHILD_OUTBOX_EVENT,
    max_retries=3,
)
def dispatch_task_agent_child_intent_task(
    self,
    event_id: str,
    task_id: str,
    child_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        return run_in_worker(consume_task_agent_child_intent_once(
            event_id=event_id,
            task_id=task_id,
            child_id=child_id,
            wake_generation=wake_generation,
            reason=reason,
        ))
    except Exception as exc:
        countdown = min(60, 5 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown)


# A delegated child run is an agent loop too: the same slice and limits.
@celery_app.task(
    bind=True,
    name=TASK_AGENT_CHILD_RUN_TASK,
    max_retries=0,
    soft_time_limit=TASK_AGENT_LOOP_SOFT_TIME_LIMIT_SECONDS,
    time_limit=TASK_AGENT_LOOP_TIME_LIMIT_SECONDS,
)
def run_task_agent_child_task(
    self,
    event_id: str,
    task_id: str,
    child_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    from packages.core.workers.task_agent_child_loop import run_task_agent_child_once

    return run_in_worker(run_task_agent_child_once(
        event_id=event_id,
        task_id=task_id,
        child_id=child_id,
        wake_generation=wake_generation,
        reason=reason,
    ))


@celery_app.task(name="runtime.sandbox_scheduler", max_retries=0)
def sandbox_scheduler_task() -> dict[str, Any]:
    return run_in_worker(run_sandbox_scheduler_once())


@celery_app.task(name="runtime.outbox_dispatch", max_retries=0)
def runtime_outbox_dispatch_task() -> dict[str, int]:
    return {"delivered": run_in_worker(dispatch_runtime_outbox_once())}


@celery_app.task(name="runtime.task_coordinator_recovery", max_retries=0)
def task_coordinator_recovery_task() -> dict[str, int]:
    results: dict[str, int] = {}
    for key, operation in (
        ("recovered", recover_stalled_task_coordinator_wakes_once),
        ("children_recovered", recover_stalled_task_agent_child_wakes_once),
        ("terminal_reconciled", reconcile_terminal_task_coordinator_runs_once),
        ("child_cards_closed", recover_ended_child_cards_once),
        ("runs_on_closed_cards", recover_runs_on_closed_cards_once),
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


async def _reevaluate_pending_approvals_once(**payload: Any) -> dict[str, int]:
    from packages.core.database import create_worker_session
    from packages.core.governance.approval_policy_reevaluation import reevaluate_pending_approvals

    async with create_worker_session()() as db:
        return await reevaluate_pending_approvals(db, **payload)


@celery_app.task(bind=True, name="approval.reevaluate_pending", max_retries=3)
def reevaluate_pending_approvals_task(
    self,
    entity_id: str,
    scope_kind: str,
    scope_id: str,
    revision: int,
    actor_id: str | None = None,
) -> dict[str, int]:
    """Re-decide pending policy approvals after a committed policy write."""
    try:
        return run_in_worker(_reevaluate_pending_approvals_once(
            entity_id=entity_id, scope_kind=scope_kind, scope_id=scope_id,
            revision=revision, actor_id=actor_id,
        ))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)
