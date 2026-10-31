"""Durable wake storage and fences for the delegated child runs of a Task.

A ``TaskAgentChildRun`` is a durable run driven by the run-row protocol in
``services.durable_run`` under ``TASK_AGENT_CHILD_RUN_SPEC``: one outbox wake
row per child (``task-agent-child:{child_id}``), delivered on CONTROL by
``runtime.dispatch_task_agent_child`` and executed on HEAVY by
``runtime.run_task_agent_child``.

A child may run only while everything above it is waiting for it:

* its Task is in progress and its lifecycle is the Task's current one;
* the Task's root run is an agent loop, not terminal, on that lifecycle;
* its direct parent (the root run, or a depth-1 child) is ``waiting_child``
  on exactly this child's ``delegate_to_agent`` call, unresolved.

``task_agent_child_delivery_state`` checks that read-only for the publisher
and the CONTROL consumer; the HEAVY worker re-checks it under the Workspace ->
Task -> root Run -> parent child -> child lock order.

This module stages and cancels; it never commits. Child rows are created only
by a durable delegation's park (``TaskLoopHost.park_children``, behind the
``task_agent_durable_delegation`` flag, default off).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskStatus
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_PHASES,
    TASK_AGENT_CHILD_TERMINAL_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services import durable_run
from packages.core.services.durable_run import (
    DurableRunSpec,
    DurableRunWake,
    clear_run_claim_and_wake,
    close_run_wake,
)
from packages.core.services.task_execution import task_execution_intent_generation


TASK_AGENT_CHILD_OUTBOX_EVENT = "runtime.dispatch_task_agent_child"
TASK_AGENT_CHILD_RUN_TASK = "runtime.run_task_agent_child"
TASK_AGENT_CHILD_OUTBOX_VERSION = 1
TASK_AGENT_CHILD_WAKE_PENDING = "task agent child wake pending"
TASK_AGENT_CHILD_AWAITING_ACK = "task agent child execution awaiting acknowledgment"
TASK_AGENT_CHILD_AWAITING_HEAVY_ACK = "task agent child heavy execution awaiting acknowledgment"
TASK_AGENT_CHILD_DEFERRED_PREFIX = "task agent child delivery deferred"
TASK_AGENT_CHILD_CLOSED_PREFIX = "permanent:task_agent_child_intent_closed"
# ``last_error`` of a child wake closed because its Task became terminal.
TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED = "permanent:task_agent_child_closed:terminal_task"

# The parent's ``task_loop_wait`` kind while it waits for delegated children:
# {version, kind, tool_call_id, tool_name, child_ids, parked_at, resolution}.
WAITING_CHILD_WAIT_KIND = "waiting_child"

# At most this many children of one Task hold a live claim at once (each
# holds a HEAVY slot). A child over the cap defers its own wake by
# TASK_AGENT_CHILD_RUNNING_CAP_DEFER_SECONDS: not a failure, no model call.
TASK_AGENT_CHILD_MAX_RUNNING_ENV = "TASK_AGENT_CHILD_MAX_RUNNING"
TASK_AGENT_CHILD_MAX_RUNNING_DEFAULT = 4
TASK_AGENT_CHILD_RUNNING_CAP_DEFER_SECONDS = 20

# Every non-terminal child phase is moved forward by something:
# * the stalled-wake sweep restages a lost wake of these (a waiting_child
#   child only once its own children are all terminal or settled);
TASK_AGENT_CHILD_RECOVERY_PHASES = frozenset({
    TaskCoordinatorPhase.READY.value,
    TaskCoordinatorPhase.RUNNING.value,
    TaskCoordinatorPhase.WAITING_CHILD.value,
    TaskCoordinatorPhase.WAITING_TIMER.value,
    TaskCoordinatorPhase.WAITING_JOB.value,
})
# * a person's answer or decision on the child's card reopens these
#   (``task_agent_loop_hitl``: ``apply_task_agent_loop_input_answer`` and
#   ``stage_task_agent_loop_approval_resolution``; the sweep never wakes a run
#   that waits on a person);
TASK_AGENT_CHILD_PERSON_PHASES = frozenset({TaskCoordinatorPhase.WAITING_HUMAN.value})
#   ... and when the card closes without a decision reaching the child, the
#   closing writer wakes it (``wake_run_for_closed_request``); the sweep
#   (``recover_runs_on_closed_cards_once``) wakes these person phases only
#   in that one case: the request is no longer pending, the wait undecided.
TASK_AGENT_CHILD_CLOSED_CARD_PHASES = frozenset({TaskCoordinatorPhase.WAITING_HUMAN.value})
# * and a terminal Task cancels every one of them (reconcile).
# tests/test_task_agent_child_runs.py keeps the two sets covering every
# non-terminal phase. A child left on an older lifecycle is not recovered
# here: a Task pause/resume rebinds every live child with its root, and a Task
# retry with live children is refused (plan §6.2/§6.3, landing with durable
# delegation); until then its parent's batch reads as malformed.


def task_agent_child_outbox_dedupe_key(child_id: str) -> str:
    return f"task-agent-child:{child_id}"


def _task_agent_child_wake_payload(child_id: str, wake_generation: str, reason: str) -> dict[str, Any]:
    return {
        "version": TASK_AGENT_CHILD_OUTBOX_VERSION,
        "child_id": child_id,
        "wake_generation": wake_generation,
        "reason": reason,
    }


def _task_agent_child_payload_fields(child: TaskAgentChildRun) -> dict[str, Any]:
    return {"task_id": child.task_id}


# The durable run protocol for one delegated child run, keyed by its id.
TASK_AGENT_CHILD_RUN_SPEC = DurableRunSpec(
    model=TaskAgentChildRun,
    key_column=TaskAgentChildRun.id,
    outbox_event=TASK_AGENT_CHILD_OUTBOX_EVENT,
    outbox_version=TASK_AGENT_CHILD_OUTBOX_VERSION,
    dedupe_key=task_agent_child_outbox_dedupe_key,
    payload=_task_agent_child_wake_payload,
    payload_key="child_id",
    label="Task agent child",
    phases=TASK_AGENT_CHILD_PHASES,
    terminal_phases=TASK_AGENT_CHILD_TERMINAL_PHASES,
    wake_pending_error=TASK_AGENT_CHILD_WAKE_PENDING,
    payload_fields=_task_agent_child_payload_fields,
)


def task_agent_child_max_running() -> int:
    """The per-Task cap on children holding a live claim (env, default 4)."""

    try:
        value = int(os.environ.get(TASK_AGENT_CHILD_MAX_RUNNING_ENV, "") or 0)
    except ValueError:
        value = 0
    return value if value > 0 else TASK_AGENT_CHILD_MAX_RUNNING_DEFAULT


async def stage_task_agent_child_wake(
    db: AsyncSession,
    *,
    child_id: str,
    reason: str,
    delay_seconds: float = 0,
    available_at: datetime | None = None,
    if_pending: bool = False,
) -> DurableRunWake | None:
    """Replace one child's wake inside the caller's transaction (no claim)."""

    return await durable_run.stage_run_wake(
        db,
        TASK_AGENT_CHILD_RUN_SPEC,
        key=child_id,
        reason=reason,
        delay_seconds=delay_seconds,
        available_at=available_at,
        if_pending=if_pending,
    )


def run_loop_wait(run: Any) -> dict[str, Any] | None:
    """The run's parked ``task_loop_wait`` (root run or child row)."""

    from packages.core.services.task_coordinator_wakeup import TASK_LOOP_WAIT_KEY

    checkpoint = getattr(run, "checkpoint", None)
    wait = checkpoint.get(TASK_LOOP_WAIT_KEY) if isinstance(checkpoint, dict) else None
    return wait if isinstance(wait, dict) else None


def _waiting_child_ids(wait: dict[str, Any] | None) -> list[str] | None:
    """The child ids of a well-formed waiting_child wait, else None."""

    if not isinstance(wait, dict) or wait.get("kind") != WAITING_CHILD_WAIT_KIND:
        return None
    child_ids = wait.get("child_ids")
    if not isinstance(child_ids, list) or not child_ids or not wait.get("tool_call_id"):
        return None
    ids = [str(item) for item in child_ids if item]
    return ids if len(ids) == len(child_ids) else None


def root_admits_children(root: Any, child: TaskAgentChildRun, task: Task) -> bool:
    """The Task's root run is a live agent loop on the Task's current lifecycle."""

    return bool(
        root is not None
        and root.id == child.root_run_id
        and root.task_id == child.task_id
        and getattr(root, "engine", None) == TaskCoordinatorEngine.AGENT_LOOP.value
        and root.phase not in TaskCoordinatorPhase.terminal()
        and root.lifecycle_generation == task_execution_intent_generation(task)
    )


def parent_awaits_child(parent: Any, child: TaskAgentChildRun) -> bool:
    """The direct parent is parked, unresolved, on exactly this child's call."""

    if parent is None or getattr(parent, "id", None) != child.parent_ref:
        return False
    if parent.phase != TaskCoordinatorPhase.WAITING_CHILD.value:
        return False
    wait = run_loop_wait(parent)
    child_ids = _waiting_child_ids(wait)
    return bool(
        child_ids is not None
        and wait.get("resolution") is None
        and str(wait.get("tool_call_id")) == child.parent_tool_call_id
        and child.id in child_ids
    )


@dataclass(frozen=True)
class WaitingChildBatch:
    """Where a parent's waiting_child batch stands (for recovery)."""

    # "settled": the batch has a resolution (its wake was lost);
    # "all_terminal": every child is terminal but nothing settled the batch;
    # "running": some child is still working (it moves itself forward);
    # "malformed": the wait is missing, of another kind, names children that
    # are not this parent's, or a live child is on another lifecycle than
    # its parent (the worker surfaces it).
    state: str
    wait: dict[str, Any] | None


async def waiting_child_batch(db: AsyncSession, run: Any) -> WaitingChildBatch:
    """Read a ``waiting_child`` parent's batch (root run or child row)."""

    wait = run_loop_wait(run)
    child_ids = _waiting_child_ids(wait)
    if child_ids is None:
        return WaitingChildBatch("malformed", wait)
    if wait.get("resolution") is not None:
        return WaitingChildBatch("settled", wait)
    rows = (await db.execute(
        select(
            TaskAgentChildRun.id, TaskAgentChildRun.phase, TaskAgentChildRun.lifecycle_generation,
        ).where(
            TaskAgentChildRun.id.in_(child_ids),
            TaskAgentChildRun.parent_ref == run.id,
            TaskAgentChildRun.task_id == run.task_id,
        )
    )).all()
    phases = {row.id: row.phase for row in rows}
    if set(phases) != set(child_ids):
        return WaitingChildBatch("malformed", wait)
    if any(
        row.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES
        and row.lifecycle_generation != run.lifecycle_generation
        for row in rows
    ):
        # A live child left on another lifecycle than its parent can never be
        # admitted, so it would never end the batch: wake the parent instead.
        return WaitingChildBatch("malformed", wait)
    if all(phase in TASK_AGENT_CHILD_TERMINAL_PHASES for phase in phases.values()):
        return WaitingChildBatch("all_terminal", wait)
    return WaitingChildBatch("running", wait)


async def load_child_parent(
    db: AsyncSession,
    child: TaskAgentChildRun,
    *,
    lock: bool = False,
) -> Any | None:
    """The direct parent row: the root run, or the parent child row."""

    if child.parent_child_id:
        statement = select(TaskAgentChildRun).where(
            TaskAgentChildRun.id == child.parent_child_id,
            TaskAgentChildRun.task_id == child.task_id,
        )
    else:
        statement = select(TaskCoordinatorRun).where(
            TaskCoordinatorRun.id == child.parent_ref,
            TaskCoordinatorRun.task_id == child.task_id,
        )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(
        statement.execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def awaited_child_refusal(
    db: AsyncSession,
    child: TaskAgentChildRun,
    task: Task,
) -> str | None:
    """Why nothing above this child is waiting for it, or None when it may run.

    The one predicate the publisher, the CONTROL consumer and the stalled-wake
    sweep share (the HEAVY worker re-checks the same rules under its locks):
    the child is on the Task's current lifecycle, the root run is a live agent
    loop on it, and the direct parent is parked, unresolved, on exactly this
    child's call. Reads the root and parent without locking them.
    """

    if child.lifecycle_generation != task_execution_intent_generation(task):
        return "stale_lifecycle"
    root = (await db.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == child.root_run_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not root_admits_children(root, child, task):
        return "root_unavailable"
    parent = root if not child.parent_child_id else await load_child_parent(db, child)
    if not parent_awaits_child(parent, child):
        return "parent_not_waiting"
    return None


async def task_agent_child_delivery_state(
    db: AsyncSession,
    event: RuntimeOutboxEvent,
) -> tuple[TaskAgentChildRun | None, str]:
    """Read-only publisher/consumer check; the HEAVY worker admits under locks."""

    if event.event_type != TASK_AGENT_CHILD_OUTBOX_EVENT:
        return None, "wrong_event_type"
    payload = event.payload if isinstance(event.payload, dict) else {}
    child_id = str(payload.get("child_id") or "")
    task_id = str(payload.get("task_id") or "")
    generation = str(payload.get("wake_generation") or "")
    if (
        payload.get("version") != TASK_AGENT_CHILD_OUTBOX_VERSION
        or not child_id or not task_id or not generation
        or event.aggregate_id != child_id
        or event.dedupe_key != task_agent_child_outbox_dedupe_key(child_id)
    ):
        return None, "invalid_event"
    child = (await db.execute(
        select(TaskAgentChildRun).where(TaskAgentChildRun.id == child_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if child is None or child.task_id != task_id:
        return None, "missing_run"
    if child.phase in TASK_AGENT_CHILD_TERMINAL_PHASES:
        return None, "terminal_run"
    task = (await db.execute(
        select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS.value:
        return None, "inactive_task"
    refusal = await awaited_child_refusal(db, child, task)
    if refusal is not None:
        return None, refusal
    if child.wake_generation != generation:
        return None, "stale_generation"
    return child, "ready"


_PARKED_JOB_POLL_SECONDS = 300


async def parked_run_wake_time(db: AsyncSession, run: Any, *, now: datetime) -> datetime:
    """When a parked agent loop (root run or child row) with no pending wake should wake.

    A timer resumes at its own time; a job wait resumes when its jobs are
    terminal or its deadline passed, and otherwise polls again. A malformed
    wait wakes at once so the worker surfaces it.
    """

    from packages.core.models.media_job import MediaJob, MediaJobStatus

    wait = run_loop_wait(run) or {}

    def _time(value: Any) -> datetime | None:
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


async def advance_deferred_child_wake(db: AsyncSession, *, task_id: str) -> str | None:
    """Deliver the earliest child wake of this Task deferred by the running cap now.

    Called whenever a child of the Task frees its slot (it parked, settled or
    its wake ended), so a deferred sibling does not sit out its retry delay;
    the stalled-wake sweep stays the backstop. Returns the advanced event id.
    """

    now = datetime.now(timezone.utc)
    event = (await db.execute(
        select(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.event_type == TASK_AGENT_CHILD_OUTBOX_EVENT,
            RuntimeOutboxEvent.delivered_at.is_(None),
            RuntimeOutboxEvent.last_error.like(f"{TASK_AGENT_CHILD_DEFERRED_PREFIX}%"),
            RuntimeOutboxEvent.payload["task_id"].as_string() == task_id,
            RuntimeOutboxEvent.available_at > now,
        )
        .order_by(RuntimeOutboxEvent.available_at, RuntimeOutboxEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if event is None:
        return None
    event.available_at = now
    return str(event.id)


async def live_sibling_claims(db: AsyncSession, child: TaskAgentChildRun) -> int:
    """How many other children of this Task hold a live claim right now."""

    return int(await db.scalar(
        select(func.count()).select_from(TaskAgentChildRun).where(
            TaskAgentChildRun.task_id == child.task_id,
            TaskAgentChildRun.id != child.id,
            TaskAgentChildRun.claim_token.is_not(None),
            TaskAgentChildRun.claim_expires_at > datetime.now(timezone.utc),
        )
    ) or 0)


async def cancel_task_agent_children(
    db: AsyncSession, *, task: Task, error_code: str | None = None,
) -> int:
    """Cancel every non-terminal child of a stopped Task or root run.

    Called by reconcile for a terminal Task and by the root run's own
    terminal write. The caller holds the Task -> root Run locks; the children
    are locked here in id order. Each child stops (``interrupted``), loses
    any claim and its pending wake, keeps a cancelled result in the shared
    delegated shape (its ledger is incomplete: its last round is unknown) and
    journals its terminal row. A stale owner's heartbeat then fails and it
    stops writing. Returns how many children were cancelled.
    """

    from packages.core.models.task_run_journal import TaskRunJournalKind
    from packages.core.services.task_agent_child_settlement import mark_child_cancelled
    from packages.core.services.task_run_journal import append_run_journal, terminal_payload

    children = list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.task_id == task.id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        )
        # Parent child before child: a parent is always one level shallower.
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    now = datetime.now(timezone.utc)
    code = error_code or f"task_{task.status}"
    for child in children:
        mark_child_cancelled(child, error_code=code, now=now)
        clear_run_claim_and_wake(child)
        await close_run_wake(
            db, TASK_AGENT_CHILD_RUN_SPEC,
            key=child.id,
            last_error=TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED,
        )
        await append_run_journal(
            db, run=child, kind=TaskRunJournalKind.TERMINAL,
            payload=terminal_payload(
                status=task.status, phase=child.phase, error_code=code,
                files=None, source="task_status",
            ),
        )
    return len(children)


async def wake_task_agent_children_for_media_job(
    db: AsyncSession,
    job: Any,
    *,
    task_id: str,
    jobs_settled: Any,
) -> list[DurableRunWake]:
    """Wake the children of this Task parked on this media job (job hook).

    Called after the root run is locked (MediaJob -> Run -> child rows).
    ``jobs_settled(run, job)`` is the root run's own check: the run's wait
    lists the job, nobody holds its claim and every listed job is terminal.
    """

    from packages.core.models.task_run_journal import TaskRunJournalKind
    from packages.core.services.task_run_journal import append_run_journal, wake_payload

    children = list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.task_id == task_id,
            TaskAgentChildRun.entity_id == job.entity_id,
            TaskAgentChildRun.phase == TaskCoordinatorPhase.WAITING_JOB.value,
        )
        # Parent child before child: a parent is always one level shallower.
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    wakes = []
    for child in children:
        wait = await jobs_settled(child, job)
        if wait is None:
            continue
        wake = await stage_task_agent_child_wake(db, child_id=child.id, reason="media_jobs_settled")
        if wake is None:
            continue
        await append_run_journal(
            db, run=child, kind=TaskRunJournalKind.WAKE,
            payload=wake_payload(wait, resolution="media_jobs_settled", task_resumed=False),
        )
        wakes.append(wake)
    return wakes


__all__ = [
    "TASK_AGENT_CHILD_AWAITING_ACK",
    "TASK_AGENT_CHILD_AWAITING_HEAVY_ACK",
    "TASK_AGENT_CHILD_CLOSED_PREFIX",
    "TASK_AGENT_CHILD_DEFERRED_PREFIX",
    "TASK_AGENT_CHILD_OUTBOX_EVENT",
    "TASK_AGENT_CHILD_CLOSED_CARD_PHASES",
    "TASK_AGENT_CHILD_PERSON_PHASES",
    "TASK_AGENT_CHILD_RECOVERY_PHASES",
    "TASK_AGENT_CHILD_RUN_SPEC",
    "TASK_AGENT_CHILD_RUN_TASK",
    "TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED",
    "WAITING_CHILD_WAIT_KIND",
    "WaitingChildBatch",
    "advance_deferred_child_wake",
    "awaited_child_refusal",
    "cancel_task_agent_children",
    "parked_run_wake_time",
    "live_sibling_claims",
    "load_child_parent",
    "parent_awaits_child",
    "root_admits_children",
    "run_loop_wait",
    "stage_task_agent_child_wake",
    "task_agent_child_delivery_state",
    "task_agent_child_max_running",
    "task_agent_child_outbox_dedupe_key",
    "waiting_child_batch",
    "wake_task_agent_children_for_media_job",
]
