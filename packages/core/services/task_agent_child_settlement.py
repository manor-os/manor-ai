"""Settle delegated child runs, and their batch into the parent that waits.

A child run ends in exactly one transaction (``settle_task_agent_child``),
under the Workspace -> Task -> root Run -> parent child -> sibling rows (id
order, itself included) locks:

1. the child's terminal state, its result in the shared delegated shape
   (``runtime_delegated_child_result`` / ``runtime_delegated_child_failure``),
   its usage and final transcript; its wake is ACKed and its claim released;
2. when it was the last open child of its parent's ``waiting_child`` call,
   the batch settles (``settle_waiting_batch``): the call's tool result is
   built with chat's batch function and written into the parent's parked
   call as ``pending_tool_result``, together with every child's ledger
   (``child_evidence``) and usage (``child_usage``); the parent becomes ready
   and its wake is staged. The resolution marker makes this happen once.

The parent resumes at that call: the loop replays the stored result under a
host carrier (``RuntimeDelegatedSettlementToolResult``, no effect of its own)
and merges the children's ledgers into its own. A child whose last round is
unknown (cancelled, crash repair exhausted) adds an effect-floor marker, so
the parent cannot report success over it unchecked.

Usage is attribution only. Every provider call of a child was billed by its
own durable callbacks (``runtime_delegated_child_billing``); the parent's
parked usage gains each child's usage once, with nothing billed again.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskStatus
from packages.core.models.task import Task
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_TERMINAL_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.task_run_journal import TaskRunJournalKind
from packages.core.services import durable_run
from packages.core.services.durable_run import claim_is_live, holds_current_wake
from packages.core.services.task_agent_child_wakeup import (
    TASK_AGENT_CHILD_RUN_SPEC,
    _waiting_child_ids,
    advance_deferred_child_wake,
    run_loop_wait,
    stage_task_agent_child_wake,
)
from packages.core.services.task_run_journal import (
    append_run_journal,
    batch_settled_payload,
    child_settled_payload,
)


logger = logging.getLogger(__name__)

# A child row's checkpoint key: how its run ended (host bookkeeping).
SETTLEMENT_KEY = "child_settlement"
RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
TASK_LOOP_WAIT_KEY = "task_loop_wait"
CHILDREN_SETTLED = "children_settled"
CHILDREN_CANCELLED = "children_cancelled"
# A parent's usage key: the share of its usage rolled up from settled children.
ROLLED_UP_USAGE_KEY = "child_usage"
CANCELLED_CHILD_MESSAGE = (
    "The delegated agent was stopped before it finished; its last action may or "
    "may not have taken effect."
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def child_service(child: TaskAgentChildRun) -> tuple[Any, Any]:
    """The pinned identity as the shared core's (subscription, agent) pair."""

    sub = SimpleNamespace(
        id=child.agent_subscription_id,
        agent_id=child.agent_id,
        service_key=child.service_key,
    )
    return sub, SimpleNamespace(name=child.agent_label or child.service_key or "Agent")


def child_effects_unknown(
    checkpoint: Any,
    *,
    ledger_complete: bool,
    wake_generation: str | None = None,
    took_over: bool = False,
) -> bool:
    """Whether a child's effects are unknown at the point its host ended it.

    Unknown when its last round is unknown or its ledger holds an effect
    nobody confirmed:

    * ``ledger_complete`` false (crash repair exhausted, an in-flight call
      without a receipt);
    * this very wake already crashed once (its checkpoint's wake-failure
      record names ``wake_generation``): the crashed attempt may have acted
      after its last checkpoint;
    * ``took_over``: this owner claimed the run while it was running under a
      lapsed claim (its previous owner died mid-wake, unrecorded);
    * an unconfirmed effect or call in the ledger.

    Only then is a host-decided failure ``interrupted``; a whole, confirmed
    ledger is a plain failure.
    """

    from packages.core.ai.runtime.outcome import ledger_is_unconfirmed
    from packages.core.services.durable_run import DEFAULT_WAKE_FAILURES_KEY

    if not ledger_complete or took_over:
        return True
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    failures = checkpoint.get(DEFAULT_WAKE_FAILURES_KEY)
    if (
        wake_generation
        and isinstance(failures, dict)
        and failures.get("wake_generation") == wake_generation
        and int(failures.get("count") or 0) > 0
    ):
        return True
    return ledger_is_unconfirmed(checkpoint.get("tool_evidence"))


def child_failure_result(
    child: TaskAgentChildRun,
    *,
    status: str,
    error: str,
    message: str,
    rounds: int = 0,
    tool_calls_made: list[str] | None = None,
    usage: dict[str, Any] | None = None,
    interrupted: bool = True,
) -> dict[str, Any]:
    """A child its host ended, in the shared delegated shape.

    ``interrupted`` when its effects are unknown (its last round is unknown
    or its ledger holds an unconfirmed effect): the next delegation to the
    same agent must then pass ``continue_interrupted`` (the parent checks the
    child's last action first). A failure whose outcome is known (nothing
    unconfirmed, a deterministic refusal) is a plain failure.
    """

    from packages.core.ai.runtime.receipts import EffectState
    from packages.core.ai.runtime.workspace_delegation_actions import runtime_delegated_child_failure

    sub, agent = child_service(child)
    return runtime_delegated_child_failure(
        run_id=child.id,
        sub=sub,
        agent=agent,
        status=status,
        error=error,
        message=message,
        rounds=rounds,
        tool_calls_made=tool_calls_made,
        usage=usage,
        replay_fields=(
            {"do_not_replay": True, "effect_state": EffectState.UNCONFIRMED.value}
            if interrupted else {}
        ),
        result_stored=True,
    )


def mark_child_cancelled(child: TaskAgentChildRun, *, error_code: str, now: datetime) -> None:
    """Stop a child its host did not end (the Task or its root stopped).

    The caller locked the row and clears its claim and wake.
    """

    checkpoint = child.checkpoint if isinstance(child.checkpoint, dict) else {}
    child.phase = TaskCoordinatorPhase.CANCELLED.value
    child.outcome_status = "cancelled"
    child.interrupted = True
    child.terminal_at = now
    if not isinstance(child.result, dict):
        child.result = child_failure_result(
            child, status="cancelled", error=error_code, message=CANCELLED_CHILD_MESSAGE,
            rounds=int(checkpoint.get("rounds") or 0),
            tool_calls_made=[str(name) for name in checkpoint.get("tool_calls_made") or []],
            usage=child.usage if isinstance(child.usage, dict) else {},
        )
    child.checkpoint = {
        **checkpoint,
        SETTLEMENT_KEY: {"ledger_complete": False, "error_code": error_code, "source": "cancelled"},
    }


def child_ledger_items(child: TaskAgentChildRun) -> tuple[list[dict[str, Any]], bool]:
    """The child's whole ledger as run-prefixed parent observations, and whether it is whole.

    A child that did not end its own loop (cancelled, crash repair
    exhausted, host-decided) left its last round unknown: an effect-floor
    marker makes the parent's ledger unconfirmed until it checks.
    """

    from packages.core.ai.runtime.tool_evidence import _EFFECT_FLOOR_MARKER
    from packages.core.ai.runtime.workspace_delegation_actions import runtime_child_ledger_items

    checkpoint = child.checkpoint if isinstance(child.checkpoint, dict) else {}
    settlement = checkpoint.get(SETTLEMENT_KEY) if isinstance(checkpoint.get(SETTLEMENT_KEY), dict) else {}
    complete = settlement.get("ledger_complete") is True
    items = runtime_child_ledger_items(child.id, checkpoint.get("tool_evidence"))
    if not complete:
        items.append({
            **_EFFECT_FLOOR_MARKER,
            "call_id": f"{child.id}/{_EFFECT_FLOOR_MARKER['call_id']}",
            "effect_state": "unconfirmed",
        })
    return items, complete


def batch_tool_result(batch: list[TaskAgentChildRun]) -> str:
    """The delegate_to_agent result of a whole batch, exactly as chat builds it."""

    from packages.core.ai.runtime.workspace_delegation_actions import runtime_delegation_batch_result

    payloads = []
    for child in batch:
        if isinstance(child.result, dict):
            payloads.append(child.result)
        else:
            payloads.append(child_failure_result(
                child, status="failed", error="agent_child_result_missing",
                message="The delegated agent ended without a stored result.",
            ))
    items = [{"agent": child.agent_label or child.service_key or "Agent"} for child in batch]
    return runtime_delegation_batch_result(items, payloads)


def batch_pending_result(wait: dict[str, Any], batch: list[TaskAgentChildRun]) -> dict[str, Any]:
    """The parked call's stored result: the batch's tool result and the children's ledgers."""

    from packages.core.ai.runtime.workspace_delegation_actions import _delegation_usage_delta

    evidence: list[dict[str, Any]] = []
    complete = True
    child_usage = []
    for child in batch:
        items, whole = child_ledger_items(child)
        evidence.extend(items)
        complete = complete and whole
        child_usage.append({
            "run_id": child.id,
            "usage": _delegation_usage_delta(child.usage if isinstance(child.usage, dict) else {}, {}),
        })
    return {
        "id": str(wait["tool_call_id"]),
        "content": batch_tool_result(batch),
        "child_evidence": evidence,
        "ledger_complete": complete,
        "child_usage": child_usage,
    }


def _with_pending_result(
    checkpoint: dict[str, Any], wait: dict[str, Any], pending: dict[str, Any], *, resolution: str,
) -> dict[str, Any] | None:
    """The parent's checkpoint with the batch written into its parked call; None if malformed."""

    from packages.core.ai.agentic_loop import _add_usage

    parked = checkpoint.get(RUNTIME_CHECKPOINT_KEY)
    pending_call = parked.get("pending_tool_call") if isinstance(parked, dict) else None
    if not isinstance(pending_call, dict) or str(pending_call.get("id") or "") != str(wait.get("tool_call_id")):
        return None
    parked = copy.deepcopy(parked)
    parked["pending_tool_result"] = pending
    # Attribution only: each child's calls were billed by the child itself.
    usage = dict(parked.get("usage") or {})
    rolled = dict(usage.get(ROLLED_UP_USAGE_KEY) or {})
    for entry in pending["child_usage"]:
        _add_usage(usage, entry["usage"])
        _add_usage(rolled, entry["usage"])
    # The part of this run's usage that its children produced, kept apart so
    # a reader summing runs (the journal's usage_delta) never counts it twice.
    usage[ROLLED_UP_USAGE_KEY] = {
        key: value for key, value in rolled.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    parked["usage"] = usage
    return {
        **checkpoint,
        RUNTIME_CHECKPOINT_KEY: parked,
        TASK_LOOP_WAIT_KEY: {**wait, "resolution": resolution, "resolved_at": utcnow().isoformat()},
    }


CHILDREN_UNAVAILABLE = "children_unavailable"
UNAVAILABLE_BATCH_MESSAGE = (
    "The results of these delegated agents could not be collected: their runs no longer "
    "match this call. They may or may not have done their work; check before relying on "
    "it or delegating again."
)


def resolve_own_batch(
    checkpoint: dict[str, Any],
    wait: dict[str, Any],
    rows: list[TaskAgentChildRun],
    *,
    malformed: bool,
) -> dict[str, Any] | None:
    """The checkpoint a parent woken on its finished (or broken) batch continues from.

    The parent itself holds its claim, so the batch is written straight into
    its parked call. A wait that no longer matches its children gets an
    honest unavailable result: unknown effects (an effect-floor marker), no
    claimed answers. None when there is no parked call to write into.
    """

    from packages.core.ai.runtime.tool_evidence import _EFFECT_FLOOR_MARKER

    if not malformed:
        batch = sorted(rows, key=lambda row: row.item_index)
        return _with_pending_result(
            copy.deepcopy(checkpoint), wait, batch_pending_result(wait, batch), resolution=CHILDREN_SETTLED,
        )
    import json

    pending = {
        "id": str(wait.get("tool_call_id") or ""),
        "content": json.dumps({
            "error": "delegation_results_unavailable",
            "message": UNAVAILABLE_BATCH_MESSAGE,
            "run_ids": [row.id for row in rows],
            "action_executed": "unknown",
        }, ensure_ascii=False),
        "child_evidence": [{**_EFFECT_FLOOR_MARKER, "effect_state": "unconfirmed"}],
        "ledger_complete": False,
        "child_usage": [],
    }
    return _with_pending_result(copy.deepcopy(checkpoint), wait, pending, resolution=CHILDREN_UNAVAILABLE)


async def cancel_call_children(
    db: AsyncSession, rows: list[TaskAgentChildRun], *, error_code: str,
) -> int:
    """Stop the open children of one call whose parent gave up on them, and every
    open run below them (``rows`` are locked; descendants are locked here, a
    level at a time in (depth, id) order)."""

    stopped = await _cancel_rows(db, rows, error_code=error_code)
    frontier = [row.id for row in rows]
    while frontier:
        below = list((await db.execute(
            select(TaskAgentChildRun)
            .where(
                TaskAgentChildRun.parent_ref.in_(frontier),
                TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
            )
            .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        stopped += await _cancel_rows(db, below, error_code=error_code)
        frontier = [row.id for row in below]
    return stopped


async def _cancel_rows(db: AsyncSession, rows: list[TaskAgentChildRun], *, error_code: str) -> int:
    from packages.core.services.durable_run import clear_run_claim_and_wake, close_run_wake
    from packages.core.services.task_agent_child_wakeup import TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED
    from packages.core.services.task_run_journal import terminal_payload

    now = utcnow()
    stopped = 0
    for child in rows:
        if child.phase in TASK_AGENT_CHILD_TERMINAL_PHASES:
            continue
        mark_child_cancelled(child, error_code=error_code, now=now)
        clear_run_claim_and_wake(child)
        await close_run_wake(
            db, TASK_AGENT_CHILD_RUN_SPEC, key=child.id,
            last_error=TASK_AGENT_CHILD_TERMINAL_TASK_CLOSED.replace("terminal_task", error_code),
        )
        await append_run_journal(
            db, run=child, kind=TaskRunJournalKind.TERMINAL,
            payload=terminal_payload(
                status="cancelled", phase=child.phase, error_code=error_code,
                files=None, source="parent",
            ),
        )
        stopped += 1
    return stopped


async def _lock_batch(db: AsyncSession, parent: Any, child_ids: list[str]) -> list[TaskAgentChildRun]:
    return list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.id.in_(child_ids),
            TaskAgentChildRun.parent_ref == parent.id,
            TaskAgentChildRun.task_id == parent.task_id,
        )
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())


async def settle_waiting_batch(
    db: AsyncSession,
    *,
    task: Task,
    parent: Any,
    children: list[TaskAgentChildRun] | None = None,
) -> bool:
    """Write a finished batch into its parent and wake it; False when there is nothing to settle.

    ``parent`` is the locked root run or child row waiting on the batch;
    ``children`` are its locked child rows when the caller already holds
    them (else they are locked here, after the parent). Nothing is written
    while a child is open, once the batch has a resolution, or while the
    parent holds a live claim (its own park settles the batch then). The
    parent's wake is staged only while the Task is in progress; a paused
    Task's resume wakes it.
    """

    wait = run_loop_wait(parent)
    child_ids = _waiting_child_ids(wait)
    if (
        child_ids is None
        or wait.get("resolution") is not None
        or parent.phase in TaskCoordinatorPhase.terminal()
        or claim_is_live(parent)
    ):
        return False
    rows = children if children is not None else await _lock_batch(db, parent, child_ids)
    by_id = {row.id: row for row in rows if row.parent_ref == parent.id}
    if set(child_ids) - set(by_id):
        return False
    batch = sorted((by_id[child_id] for child_id in child_ids), key=lambda row: row.item_index)
    if any(row.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES for row in batch):
        return False
    checkpoint = _with_pending_result(
        copy.deepcopy(parent.checkpoint), wait, batch_pending_result(wait, batch),
        resolution=CHILDREN_SETTLED,
    )
    if checkpoint is None:
        logger.warning("Delegation batch of run %s has no parked call to settle into", parent.id)
        return False
    parent.checkpoint = checkpoint
    parent.phase = TaskCoordinatorPhase.READY.value
    parent.state_version += 1
    if task.status == TaskStatus.IN_PROGRESS.value:
        if isinstance(parent, TaskCoordinatorRun):
            from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

            await stage_task_coordinator_wake(db, task_id=parent.task_id, reason="delegated_children_settled")
        else:
            await stage_task_agent_child_wake(db, child_id=parent.id, reason="delegated_children_settled")
    await append_run_journal(
        db, run=parent, kind=TaskRunJournalKind.BATCH_SETTLED,
        payload=batch_settled_payload(
            tool_call_id=str(wait["tool_call_id"]), resolution=CHILDREN_SETTLED, children=batch,
        ),
    )
    return True


@dataclass(frozen=True)
class ChildOutcome:
    """How one child run ended, as its host decided it."""

    phase: str
    outcome_status: str
    interrupted: bool
    # The shared delegated payload the parent's batch result is built from.
    result: dict[str, Any]
    # The child's final run state (its transcript a later run continues).
    checkpoint: dict[str, Any]
    usage: dict[str, Any]
    # False when the last round is unknown (an effect-floor marker follows).
    ledger_complete: bool
    error_code: str | None = None
    # "loop" when the child's own loop ended it, "host" when its host did.
    source: str = "loop"


def _stored(result: dict[str, Any]) -> dict[str, Any]:
    """The result as stored: the answer is kept up to the chat session's limit."""

    from packages.core.services.agent_delegation_session import DELEGATION_RESULT_STORED_CHARS

    content = result.get("content")
    if isinstance(content, str) and len(content) > DELEGATION_RESULT_STORED_CHARS:
        return {**result, "content": content[:DELEGATION_RESULT_STORED_CHARS]}
    return result


async def settle_task_agent_child(
    db: AsyncSession,
    *,
    child_id: str,
    event_id: str,
    claim_token: str,
    claim_generation: str,
    outcome: ChildOutcome,
) -> bool:
    """The child's terminal write and, when it was the last, its batch; False when superseded.

    One transaction under Workspace -> Task -> root Run -> parent child ->
    sibling rows (id order, the child included). Only the exact owner of the
    child's current wake writes it, while its Task is not terminal (a paused
    Task still takes the result: no provider I/O follows). The caller commits.
    """

    from packages.core.services.workspace_access import lock_workspace_access_boundary

    scope = (await db.execute(
        select(
            TaskAgentChildRun.task_id, TaskAgentChildRun.entity_id, TaskAgentChildRun.workspace_id,
            TaskAgentChildRun.root_run_id, TaskAgentChildRun.parent_child_id,
            TaskAgentChildRun.parent_ref, TaskAgentChildRun.parent_tool_call_id,
        ).where(TaskAgentChildRun.id == child_id)
    )).one_or_none()
    if scope is None:
        return False
    await lock_workspace_access_boundary(db, workspace_id=scope.workspace_id, entity_id=scope.entity_id)
    task = (await db.execute(
        select(Task).where(Task.id == scope.task_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    from packages.core.services.task_state_machine import TERMINAL_STATUSES

    if task is None or task.status in TERMINAL_STATUSES:
        return False
    root = (await db.execute(
        select(TaskCoordinatorRun).where(
            TaskCoordinatorRun.id == scope.root_run_id, TaskCoordinatorRun.task_id == scope.task_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    parent: Any = root
    if scope.parent_child_id:
        parent = (await db.execute(
            select(TaskAgentChildRun).where(
                TaskAgentChildRun.id == scope.parent_child_id,
                TaskAgentChildRun.task_id == scope.task_id,
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
    siblings = list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.parent_ref == scope.parent_ref,
            TaskAgentChildRun.parent_tool_call_id == scope.parent_tool_call_id,
            TaskAgentChildRun.task_id == scope.task_id,
        )
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    child = next((row for row in siblings if row.id == child_id), None)
    if (
        child is None or root is None or parent is None
        or child.phase in TASK_AGENT_CHILD_TERMINAL_PHASES
        or not holds_current_wake(child, claim_token, claim_generation)
    ):
        return False
    child.phase = outcome.phase
    child.outcome_status = outcome.outcome_status
    child.interrupted = bool(outcome.interrupted)
    child.result = _stored(outcome.result)
    child.usage = dict(outcome.usage or {})
    child.terminal_at = utcnow()
    child.checkpoint = {
        **outcome.checkpoint,
        SETTLEMENT_KEY: {
            "ledger_complete": bool(outcome.ledger_complete),
            "error_code": outcome.error_code,
            "source": outcome.source,
            "content_chars": len(str(outcome.result.get("content") or "")),
        },
    }
    if not await durable_run.settle_run_wake(
        db, TASK_AGENT_CHILD_RUN_SPEC,
        key=child_id, event_id=event_id, wake_generation=claim_generation, claim_token=claim_token,
    ):
        return False
    await durable_run.release_run_claim(
        db, TASK_AGENT_CHILD_RUN_SPEC,
        key=child_id, claim_token=claim_token, claim_generation=claim_generation,
    )
    evidence = outcome.checkpoint.get("tool_evidence") if isinstance(outcome.checkpoint, dict) else None
    await append_run_journal(
        db, run=child, kind=TaskRunJournalKind.CHILD_SETTLED,
        payload=child_settled_payload(
            status=outcome.outcome_status, phase=outcome.phase,
            outcome_status=outcome.outcome_status, interrupted=outcome.interrupted,
            error_code=outcome.error_code, source=outcome.source,
            receipts=(evidence or {}).get("receipts") if isinstance(evidence, dict) else None,
        ),
    )
    await settle_waiting_batch(db, task=task, parent=parent, children=siblings)
    # This child's slot is free: a sibling deferred by the running cap goes now.
    await advance_deferred_child_wake(db, task_id=task.id)
    # The last child that could move forward ended while the others wait on
    # a person: the Task now waits on that person (Q3).
    from packages.core.services.task_agent_loop_hitl import sync_task_human_wait_status

    await sync_task_human_wait_status(db, task=task)
    return True


async def cancel_root_delegation(
    db: AsyncSession, *, task: Task, run: TaskCoordinatorRun, checkpoint: dict[str, Any], error_code: str,
) -> dict[str, Any]:
    """Stop the children of a root run that is ending; returns its checkpoint to store.

    The caller holds the Task -> root Run locks and writes the root's
    terminal state. Normally nothing is open (children exist only while the
    root waits on them). When the root still waits on an unsettled batch, its
    parked call gets the cancelled batch result, so a retry of the Task
    continues from what the children did instead of a call that never ran.
    """

    from packages.core.services.task_agent_child_wakeup import cancel_task_agent_children

    await cancel_task_agent_children(db, task=task, error_code=error_code)
    wait = checkpoint.get(TASK_LOOP_WAIT_KEY) if isinstance(checkpoint, dict) else None
    child_ids = _waiting_child_ids(wait)
    if child_ids is None or wait.get("resolution") is not None:
        return checkpoint
    rows = await _lock_batch(db, run, child_ids)
    by_id = {row.id: row for row in rows}
    if set(child_ids) - set(by_id) or any(
        row.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES for row in rows
    ):
        return checkpoint
    batch = sorted(rows, key=lambda row: row.item_index)
    settled = _with_pending_result(
        copy.deepcopy(checkpoint), wait, batch_pending_result(wait, batch), resolution=CHILDREN_CANCELLED,
    )
    return settled if settled is not None else checkpoint


__all__ = [
    "CANCELLED_CHILD_MESSAGE",
    "CHILDREN_CANCELLED",
    "CHILDREN_SETTLED",
    "ChildOutcome",
    "SETTLEMENT_KEY",
    "batch_pending_result",
    "batch_tool_result",
    "CHILDREN_UNAVAILABLE",
    "cancel_call_children",
    "cancel_root_delegation",
    "child_effects_unknown",
    "child_failure_result",
    "resolve_own_batch",
    "child_ledger_items",
    "child_service",
    "mark_child_cancelled",
    "settle_task_agent_child",
    "settle_waiting_batch",
]
