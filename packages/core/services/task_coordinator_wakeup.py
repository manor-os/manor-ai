"""Durable, generation-fenced wake storage for one Task coordinator run.

This module owns persistence only. New coordinated Workspace Task starts admit
one run and stage its wake; in-flight V2 Plans retain their own coordinator.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.interrupted_tool_calls import (
    runtime_answer_interrupted_calls as _answer_interrupted_calls,
    runtime_stored_pending_result,
)
from packages.core.ai.runtime.run_state import RuntimeRunState, RuntimeRunStateVersionError
from packages.core.constants.task import TaskStatus
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services import durable_run
from packages.core.services.durable_run import (
    DurableRunSpec,
    DurableRunWake,
    claim_is_live,
    clear_run_claim_and_wake,
)
from packages.core.services.task_agent_loop_state import (
    POSTED_WORK_KEY,
    task_loop_initial_state,
    task_loop_run_state_slots,
)
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    task_execution_intent_generation,
)


logger = logging.getLogger(__name__)

TASK_COORDINATOR_OUTBOX_EVENT = "runtime.dispatch_task_coordinator"
TASK_COORDINATOR_OUTBOX_VERSION = 1
TASK_COORDINATOR_WAKE_PENDING = "task coordinator wake pending"
TASK_COORDINATOR_AWAITING_ACK = "task coordinator execution awaiting acknowledgment"
# A Task agent loop's parked wait (wait_until / wait_media_jobs) in its checkpoint.
TASK_LOOP_WAIT_KEY = "task_loop_wait"
_UNSET = durable_run.UNSET


@dataclass(frozen=True)
class TaskCoordinatorWake:
    event_id: str
    task_id: str
    wake_generation: str
    available_at: datetime


@dataclass(frozen=True)
class TaskCoordinatorClaim:
    run_id: str
    task_id: str
    event_id: str
    wake_generation: str
    claim_token: str
    expires_at: datetime
    state_version: int


def task_coordinator_outbox_dedupe_key(task_id: str) -> str:
    return f"task-coordinator:{task_id}"


def _task_coordinator_wake_payload(task_id: str, wake_generation: str, reason: str) -> dict[str, Any]:
    return {
        "version": TASK_COORDINATOR_OUTBOX_VERSION,
        "task_id": task_id,
        "wake_generation": wake_generation,
        "reason": reason,
    }


# The durable run protocol for the one TaskCoordinatorRun of a Task, keyed by
# task_id (``uq_task_coordinator_runs_task``).
TASK_COORDINATOR_RUN_SPEC = DurableRunSpec(
    model=TaskCoordinatorRun,
    key_column=TaskCoordinatorRun.task_id,
    outbox_event=TASK_COORDINATOR_OUTBOX_EVENT,
    outbox_version=TASK_COORDINATOR_OUTBOX_VERSION,
    dedupe_key=task_coordinator_outbox_dedupe_key,
    payload=_task_coordinator_wake_payload,
    payload_key="task_id",
    label="Task coordinator",
    phases=frozenset(member.value for member in TaskCoordinatorPhase),
    terminal_phases=TaskCoordinatorPhase.terminal(),
    wake_pending_error=TASK_COORDINATOR_WAKE_PENDING,
)


def _wake(wake: DurableRunWake | None) -> TaskCoordinatorWake | None:
    if wake is None:
        return None
    return TaskCoordinatorWake(wake.event_id, wake.key, wake.wake_generation, wake.available_at)


def _selection_matches(run: TaskCoordinatorRun, selection: CoordinatorRuntimeSelection) -> bool:
    return (
        run.root_subscription_id == selection.root.subscription_id
        and run.root_agent_id == selection.root.agent_id
        and run.selection_fingerprint == selection.stable_fingerprint()
    )


def _require_selection(run: TaskCoordinatorRun, selection: CoordinatorRuntimeSelection) -> None:
    if not _selection_matches(run, selection):
        raise ValueError("Task coordinator factory selection changed; existing checkpoint cannot be rebound")


AGENT_LOOP_RETRY_KEY = "retried_after"
# Digest of the work list a host-forced stop of the agent loop last posted.
AGENT_LOOP_POSTED_WORK_KEY = POSTED_WORK_KEY


def agent_loop_retry_checkpoint(checkpoint: Any) -> dict[str, Any] | None:
    """The transcript a retried agent loop continues from.

    What carries over is declared, not listed here: each RuntimeRunState
    field and each Task loop slot (services.task_agent_loop_state) states
    ``on_retry``. A retry keeps the conversation, tool evidence, web sources,
    loaded tools, the plan and the posted-work digest, and resets what
    belonged to the stopped attempt (submission, completion, inspection,
    refusals, round count, continuation budget). Park state (suspension,
    outcome, failure counters) is the host's and never carries. The retry
    marker tells the next wake to re-check external state before acting.
    """

    if not isinstance(checkpoint, dict):
        return None
    outcome = checkpoint.get("coordinator_outcome")
    previous = (
        str(outcome.get("error") or "")
        if isinstance(outcome, dict)
        else ""
    )
    marker: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "after": previous or "unknown_stop",
    }
    slots = task_loop_run_state_slots(task_loop_initial_state(0))
    try:
        stopped = RuntimeRunState.restore(checkpoint, slots)
    except RuntimeRunStateVersionError as exc:
        # Never rewrite a newer runtime's checkpoint with an older shape. The
        # error's message tells the person to retry after the rollout.
        logger.warning(
            "Agent loop retry refused: checkpoint saved by a newer runtime (run state version %r)",
            exc.version,
        )
        raise
    if previous == "agent_loop_repair_required":
        # The transcript itself may be what kept crashing. Start fresh and
        # carry only the slots a retry keeps, with a note naming the crash.
        details = outcome.get("details") if isinstance(outcome.get("details"), dict) else {}
        marker.update({
            "fresh_transcript": True,
            "exception": str(details.get("exception") or "")[:120] or None,
            "error": str(details.get("error") or "")[:300] or None,
        })
        fresh = stopped.for_retry(slots, fresh_transcript=True).snapshot()
        fresh[AGENT_LOOP_RETRY_KEY] = marker
        return fresh
    if not stopped.messages:
        return None
    retried = stopped.for_retry(slots)
    retried.messages = _answer_interrupted_calls(checkpoint)
    stored = runtime_stored_pending_result(checkpoint)
    if stored is not None and stored["child_evidence"]:
        # A settled (or cancelled) delegation's children carry into the
        # retried ledger with their call's result, merged once.
        from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
        from packages.core.ai.runtime.tool_results import RuntimeDelegatedSettlementToolResult

        ledger = RuntimeToolEvidence()
        ledger.restore_checkpoint_state(retried.tool_evidence or {})
        ledger.observe_tool_result(
            stored["name"], stored["id"], RuntimeDelegatedSettlementToolResult(stored["content"]),
            stored["arguments"],
        )
        ledger.merge_child_observations(stored["id"], stored["child_evidence"], observe=False)
        retried.tool_evidence = ledger.checkpoint_state()
    payload = retried.snapshot()
    payload[AGENT_LOOP_RETRY_KEY] = marker
    return payload


# Phases an agent loop can hold across a Task pause: nothing is lost by
# rebinding them to the new lifecycle (a parked wait re-parks on wake; a
# loop waiting on a person waits on the same card again).
_AGENT_LOOP_REBIND_PHASES = frozenset({
    TaskCoordinatorPhase.READY.value,
    TaskCoordinatorPhase.RUNNING.value,
    TaskCoordinatorPhase.WAITING_TIMER.value,
    TaskCoordinatorPhase.WAITING_JOB.value,
    TaskCoordinatorPhase.WAITING_HUMAN.value,
    # Waiting on delegated child runs: they are rebound with it and the
    # resumed loop parks on them again without a model round.
    TaskCoordinatorPhase.WAITING_CHILD.value,
})
AGENT_LOOP_RESUMED_AFTER = "task_resumed"


def task_coordinator_run_rebinds_agent_loop(run: Any) -> bool:
    """An agent loop that a new Task lifecycle continues in place."""

    return (
        getattr(run, "engine", None) == TaskCoordinatorEngine.AGENT_LOOP.value
        and getattr(run, "phase", None) in _AGENT_LOOP_REBIND_PHASES
    )


def task_coordinator_run_reopenable(run: Any) -> bool:
    """Whether a new Task lifecycle may reopen this run (retry or rebind)."""

    return getattr(run, "phase", None) in {
        TaskCoordinatorPhase.WAITING_HUMAN.value,
        TaskCoordinatorPhase.FAILED.value,
    } or task_coordinator_run_rebinds_agent_loop(run)


def agent_loop_rebind_checkpoint(checkpoint: Any) -> dict[str, Any] | None:
    """The checkpoint an agent loop keeps across a Task pause and resume.

    Everything is kept: transcript, plan, ledger, counters and any parked
    call. A transcript that is not parked on a call also gets a resume note,
    so the next model round re-checks external state first.
    """

    if not isinstance(checkpoint, dict):
        return None
    kept = copy.deepcopy(checkpoint)
    if kept.get("messages") and not isinstance(kept.get("runtime_checkpoint"), dict):
        kept[AGENT_LOOP_RETRY_KEY] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "after": AGENT_LOOP_RESUMED_AFTER,
        }
    return kept


AGENT_LOOP_RETRY_NOTE_MAX_CHARS = 2000


def _with_manual_retry_note(checkpoint: Any, task: Task, lifecycle_generation: str) -> Any:
    """Hand the retried run the note its person wrote with this retry.

    Only the retry that started this lifecycle counts: an older retry's note
    stays on the Task's history and never reaches a later run.
    """

    details = task.details if isinstance(task.details, dict) else {}
    retry = details.get("manual_retry")
    if not isinstance(retry, dict) or retry.get("lifecycle_generation") != lifecycle_generation:
        return checkpoint
    note = str(retry.get("note") or "").strip()[:AGENT_LOOP_RETRY_NOTE_MAX_CHARS]
    if not note:
        return checkpoint
    kept = dict(checkpoint) if isinstance(checkpoint, dict) else {}
    marker = kept.get(AGENT_LOOP_RETRY_KEY)
    if not isinstance(marker, dict):
        # A retry with no transcript to continue starts from the brief.
        marker = {"at": datetime.now(timezone.utc).isoformat(), "after": "unknown_stop"}
    kept[AGENT_LOOP_RETRY_KEY] = {**marker, "note": note}
    return kept


async def ensure_task_coordinator_run(
    db: AsyncSession,
    *,
    task: Task,
    selection: CoordinatorRuntimeSelection,
) -> TaskCoordinatorRun:
    """Create or load one Task run for the current Task-start generation.

    The caller admits and locks Workspace -> Task before this write. The Task
    worker must re-resolve the same factory selection both before provider I/O
    and at its fenced terminal commit; this method does not grant authority.
    A manual retry may reopen a stopped run, but never discard an active Plan
    or a live owner. Its replacement wake is committed with the retry reset.
    """

    task_id = str(task.id or "")
    entity_id = str(task.entity_id or "")
    # None for a Task assigned directly to an Agent, outside any Workspace.
    workspace_id = str(task.workspace_id) if task.workspace_id else None
    if not task_id or not entity_id:
        raise ValueError("Task coordinator requires an entity-scoped Task")
    if task.status != TaskStatus.IN_PROGRESS.value:
        raise ValueError("Task coordinator requires an in-progress Task")
    lifecycle_generation = task_execution_intent_generation(task)

    # Every Task runs as one agent loop. A run that cannot execute (no member
    # to act as) stops itself with that reason; there is no other engine.
    statement = pg_insert(TaskCoordinatorRun).values(
        id=generate_ulid(),
        task_id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        root_subscription_id=selection.root.subscription_id,
        root_agent_id=selection.root.agent_id,
        selection_fingerprint=selection.stable_fingerprint(),
        lifecycle_generation=lifecycle_generation,
        engine=TaskCoordinatorEngine.AGENT_LOOP.value,
        phase=TaskCoordinatorPhase.READY.value,
        state_version=0,
    ).on_conflict_do_nothing(constraint="uq_task_coordinator_runs_task")
    await db.execute(statement)
    run = (await db.execute(
        select(TaskCoordinatorRun)
        .where(TaskCoordinatorRun.task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    if run.entity_id != entity_id or run.workspace_id != workspace_id:
        raise ValueError("Task coordinator Task/Workspace scope changed")
    if run.lifecycle_generation == lifecycle_generation:
        _require_selection(run, selection)
        if run.state_version == 0 and not run.checkpoint:
            # A retry that starts the Task's first run (nothing to reopen).
            run.checkpoint = _with_manual_retry_note(run.checkpoint, task, lifecycle_generation)
        return run

    # A paused agent loop (ready, running or parked on a wait) is rebound to
    # the new lifecycle with its checkpoint intact; a stopped one is retried.
    rebind = task_coordinator_run_rebinds_agent_loop(run)
    prior_phase = run.phase
    prior_wait = (run.checkpoint or {}).get(TASK_LOOP_WAIT_KEY) if isinstance(run.checkpoint, dict) else None
    if not task_coordinator_run_reopenable(run):
        raise ValueError("Task coordinator prior lifecycle is not stopped for retry")
    if claim_is_live(run):
        raise ValueError("Task coordinator prior lifecycle still has a live claim")
    from packages.core.services.task_agent_loop_hitl import (
        lock_live_task_children,
        rebind_task_lifecycle,
    )

    # Task -> Run -> child rows. A child still working for the old lifecycle
    # must stop first (the execution intent retries until its owner exits).
    children = await lock_live_task_children(db, task_id) if rebind else []
    if any(claim_is_live(child) for child in children):
        raise ValueError("Task coordinator prior lifecycle still has a delegated child with a live claim")
    run.current_plan_id = None
    run.checkpoint = (
        agent_loop_rebind_checkpoint(run.checkpoint)
        if rebind
        # An agent loop retry continues its own transcript. A run closed out
        # by the plan_dag retirement (migration 20261025_10) has no loop
        # transcript: its retry starts the loop fresh on the same Task.
        else agent_loop_retry_checkpoint(run.checkpoint)
        if run.engine == TaskCoordinatorEngine.AGENT_LOOP.value
        else None
    )
    run.checkpoint = _with_manual_retry_note(run.checkpoint, task, lifecycle_generation)
    run.engine = TaskCoordinatorEngine.AGENT_LOOP.value
    run.root_subscription_id = selection.root.subscription_id
    run.root_agent_id = selection.root.agent_id
    run.selection_fingerprint = selection.stable_fingerprint()

    rebind_task_lifecycle(task, run, children)
    run.phase = TaskCoordinatorPhase.READY.value
    run.terminal_cleanup_at = None
    clear_run_claim_and_wake(run)
    # Replace the stable outbox row with a fresh opaque generation. A queued
    # pre-retry delivery cannot claim or ACK the successor, even if its event
    # row ID is reused by the dedupe key.
    await stage_task_coordinator_wake(
        db, task_id=task_id, reason="task_coordinator_lifecycle_retry",
    )
    # A paused loop resumed with its transcript is woken; a stopped one is
    # retried. Either row carries the new lifecycle.
    from packages.core.models.task_run_journal import TaskRunJournalKind
    from packages.core.services.task_run_journal import (
        append_agent_loop_run_journal,
        retry_payload,
        wake_payload,
    )

    kind, payload = (
        (TaskRunJournalKind.WAKE, wake_payload(prior_wait, resolution="task_resumed", task_resumed=True))
        if rebind
        else (TaskRunJournalKind.RETRY, retry_payload(prior_phase=prior_phase, reason="task_retry"))
    )
    await append_agent_loop_run_journal(db, run=run, kind=kind, payload=payload)
    return run


async def _locked_run(db: AsyncSession, task_id: str) -> TaskCoordinatorRun | None:
    return await durable_run.lock_run(db, TASK_COORDINATOR_RUN_SPEC, task_id)


async def stage_task_coordinator_wake(
    db: AsyncSession,
    *,
    task_id: str,
    reason: str,
    delay_seconds: float = 0,
    expected_state_version: int | None = None,
    claim_token: str | None = None,
    claim_generation: str | None = None,
    if_pending: bool = False,
    available_at: datetime | None = None,
) -> TaskCoordinatorWake | None:
    """Replace one Task wake inside the caller's business transaction.

    A child terminal transition can stage without a parent claim. A Task
    coordinator owner must pass both token and generation; stale owners cannot
    replace a successor wake.  Replacing the wake never clears a live claim.
    ``available_at`` is an absolute alternative to ``delay_seconds``; either is
    bounded to [now, now + 90 days].
    """

    return _wake(await durable_run.stage_run_wake(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        reason=reason,
        delay_seconds=delay_seconds,
        expected_state_version=expected_state_version,
        claim_token=claim_token,
        claim_generation=claim_generation,
        if_pending=if_pending,
        available_at=available_at,
    ))


async def task_coordinator_delivery_state(
    db: AsyncSession,
    event: RuntimeOutboxEvent,
) -> tuple[TaskCoordinatorRun | None, str]:
    """Read-only publisher check; admission belongs to the future consumer."""

    if event.event_type != TASK_COORDINATOR_OUTBOX_EVENT:
        return None, "wrong_event_type"
    payload = event.payload if isinstance(event.payload, dict) else {}
    task_id = str(payload.get("task_id") or "")
    generation = str(payload.get("wake_generation") or "")
    if (
        payload.get("version") != TASK_COORDINATOR_OUTBOX_VERSION
        or not task_id or not generation
        or event.aggregate_id != task_id
        or event.dedupe_key != task_coordinator_outbox_dedupe_key(task_id)
    ):
        return None, "invalid_event"
    run = (await db.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
    )).scalar_one_or_none()
    if run is None:
        return None, "missing_run"
    if run.phase in TaskCoordinatorPhase.terminal():
        return None, "terminal_run"
    task = (await db.execute(
        select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS.value:
        return None, "inactive_task"
    if (
        run.lifecycle_generation != task_execution_intent_generation(task)
    ):
        return None, "stale_lifecycle"
    if run.wake_generation != generation:
        return None, "stale_generation"
    return run, "ready"


async def claim_task_coordinator_wake(
    db: AsyncSession,
    *,
    task_id: str,
    event_id: str,
    wake_generation: str,
    claim_token: str,
    selection: CoordinatorRuntimeSelection,
    lease_seconds: float,
) -> tuple[TaskCoordinatorClaim | None, str]:
    """Claim only the current wake under a pinned factory identity.

    The future worker must renew during provider I/O; a failed renewal poisons
    that worker and prevents checkpoint/ACK. A current Task/Workspace admission
    check is still required before every provider call.
    """

    durable_run.require_claim_request(TASK_COORDINATOR_RUN_SPEC, claim_token, lease_seconds)
    # The caller holds the Task lock; read it before locking Run/Outbox to
    # preserve the canonical Task -> Plan -> Run mutation order.
    task = (await db.execute(
        select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status != TaskStatus.IN_PROGRESS.value:
        return None, "inactive_task"
    lifecycle_generation = task_execution_intent_generation(task)

    def admit(run: TaskCoordinatorRun) -> str | None:
        if run.lifecycle_generation != lifecycle_generation:
            return "stale_lifecycle"
        _require_selection(run, selection)
        return None

    claim, reason = await durable_run.claim_run_wake(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        event_id=event_id,
        wake_generation=wake_generation,
        claim_token=claim_token,
        lease_seconds=lease_seconds,
        admit=admit,
    )
    if claim is None:
        return None, reason
    return TaskCoordinatorClaim(
        claim.run_id, claim.key, claim.event_id, claim.wake_generation,
        claim.claim_token, claim.expires_at, claim.state_version,
    ), reason


async def renew_task_coordinator_claim(
    db: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    claim_generation: str,
    lease_seconds: float,
) -> bool:
    """Heartbeat the exact owner. False means the worker must stop work."""

    return await durable_run.renew_run_claim(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        claim_token=claim_token,
        claim_generation=claim_generation,
        lease_seconds=lease_seconds,
    )


async def checkpoint_task_coordinator_run(
    db: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    claim_generation: str,
    expected_state_version: int,
    checkpoint: dict[str, Any] | None | object = _UNSET,
    phase: str | object = _UNSET,
) -> int | None:
    """CAS a completed AgentLoop round and optional phase transition.

    Returns the new version, or None after any claim/version loss. The caller
    must use strict round checkpointing and cease provider work on None.
    """

    return await durable_run.checkpoint_run(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        claim_token=claim_token,
        claim_generation=claim_generation,
        expected_state_version=expected_state_version,
        checkpoint=checkpoint,
        phase=phase,
    )


async def settle_task_coordinator_wake(
    db: AsyncSession,
    *,
    task_id: str,
    event_id: str,
    wake_generation: str,
    claim_token: str,
) -> bool:
    """ACK only the generation actually owned by this live execution."""

    return await durable_run.settle_run_wake(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        event_id=event_id,
        wake_generation=wake_generation,
        claim_token=claim_token,
    )


async def release_task_coordinator_claim(
    db: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    claim_generation: str,
) -> bool:
    """Release only this owner's claim; an expired/stolen claim is untouched."""

    return await durable_run.release_run_claim(
        db,
        TASK_COORDINATOR_RUN_SPEC,
        key=task_id,
        claim_token=claim_token,
        claim_generation=claim_generation,
    )


async def _media_wait_settled(db: AsyncSession, run: Any, job: Any) -> dict[str, Any] | None:
    """The run's job wait, when it lists this job and every listed job settled.

    ``run`` is a locked root run or child row in WAITING_JOB. None while a
    live owner holds it (the owner re-reads the jobs under its lock when it
    parks), when the wait does not list this job, or while another listed job
    is still pending or processing.
    """

    from packages.core.models.media_job import MediaJob, MediaJobStatus

    wait = (run.checkpoint or {}).get(TASK_LOOP_WAIT_KEY) if isinstance(run.checkpoint, dict) else None
    job_ids = wait.get("job_ids") if isinstance(wait, dict) else None
    if not isinstance(job_ids, list) or job.id not in job_ids:
        return None
    if claim_is_live(run):
        return None
    still_open = (await db.execute(
        select(MediaJob.id).where(
            MediaJob.entity_id == run.entity_id,
            MediaJob.id.in_([str(item) for item in job_ids if item != job.id]),
            MediaJob.status.in_((MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value)),
        ).limit(1)
    )).scalar_one_or_none()
    if still_open is not None:
        return None
    return wait


async def wake_task_coordinator_for_media_job(
    db: AsyncSession,
    job: Any,
) -> TaskCoordinatorWake | None:
    """Wake the Task runs parked on this media job once all their jobs settled.

    Called inside a job's terminal-status transaction, before its commit
    (lock order MediaJob -> Run -> child rows). Stages nothing unless a run of
    the job's Task (its root run, or a delegated child run) is WAITING_JOB on
    a wait that lists this job and every listed job is terminal. A live owner
    is left alone: it re-reads the jobs under its lock when it parks.
    Idempotent; a later re-finalization only re-stages. Returns the root
    run's wake.
    """

    from packages.core.models.media_job import MediaJobStatus
    from packages.core.models.task_run_journal import TaskRunJournalKind
    from packages.core.services.task_agent_child_wakeup import wake_task_agent_children_for_media_job
    from packages.core.services.task_run_journal import append_agent_loop_run_journal, wake_payload

    params = job.params if isinstance(getattr(job, "params", None), dict) else {}
    task_id = str(params.get("task_id") or "").strip()
    status = getattr(job, "status", None)
    if not task_id or str(getattr(status, "value", status) or "") not in MediaJobStatus.terminal():
        return None
    run = await _locked_run(db, task_id)
    if run is None or run.entity_id != job.entity_id:
        return None
    wake = None
    wait = (
        await _media_wait_settled(db, run, job)
        if run.phase == TaskCoordinatorPhase.WAITING_JOB.value
        else None
    )
    if wait is not None:
        wake = await stage_task_coordinator_wake(db, task_id=task_id, reason="media_jobs_settled")
    if wake is not None:
        await append_agent_loop_run_journal(
            db, run=run, kind=TaskRunJournalKind.WAKE,
            payload=wake_payload(wait, resolution="media_jobs_settled", task_resumed=False),
        )
    await wake_task_agent_children_for_media_job(
        db, job, task_id=task_id,
        jobs_settled=lambda child, settled_job: _media_wait_settled(db, child, settled_job),
    )
    return wake
