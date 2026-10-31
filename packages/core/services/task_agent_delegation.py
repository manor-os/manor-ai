"""Durable delegation of a Task agent loop: ``delegate_to_agent`` as child runs.

With the ``task_agent_durable_delegation`` flag on (frozen per root run in its
``delegation_mode`` slot), a Task agent loop's ``delegate_to_agent`` call does
not run the delegated agents inside its own worker. The call's handler checks
the batch with the shared delegation core (the same targets, duplicates,
prompts and answer budgets as chat) and suspends with ``waiting_child``. The
host then parks the call (``TaskLoopHost.park_children``): one transaction
inserts one ``TaskAgentChildRun`` per task, stages each child's wake, moves the
parent to ``waiting_child`` and releases the parent's worker. Child rows are
created only there, never in the handler, so a crash before the park commits
leaves no orphan.

Each child then runs as its own durable run (``workers.task_agent_child_loop``)
and settles itself (``services.task_agent_child_settlement``); the last one of
a batch writes the batch's tool result into the parent's parked call and wakes
the parent, which resumes at that call with chat's result shape.

Two refusals have no chat session to come from, so they are checked here:

* busy: the agent already has a live run in this Task (one live run per agent
  per Task, ``uq_task_agent_child_runs_live_agent``);
* do_not_replay: the agent's latest run in this Task was interrupted and the
  call did not pass ``continue_interrupted=true``.

Both are preflight refusals: nothing ran and the model corrects the call in
place. A busy race that only the park transaction sees is refused through the
host's gate replay instead.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_MAX_DEPTH,
    TASK_AGENT_CHILD_TERMINAL_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import TaskCoordinatorPhase
from packages.core.services.task_agent_child_settlement import (
    CHILDREN_CANCELLED,
    CHILDREN_SETTLED,
)
from packages.core.services.task_agent_child_wakeup import (
    WAITING_CHILD_WAIT_KIND,
    stage_task_agent_child_wake,
)
from packages.core.services.task_agent_loop_state import (
    DELEGATION_MODE_DURABLE,
    DELEGATION_MODE_INLINE,
)


logger = logging.getLogger(__name__)

TASK_AGENT_DURABLE_DELEGATION_FLAG = "task_agent_durable_delegation"
DELEGATION_WAIT_VERSION = 1
# A delegation waits for its children at most this long (the durable-run
# wake bound): the suspension's own deadline, never polled.
WAITING_CHILD_DEADLINE = timedelta(days=90)
BUSY_ERROR = "delegation_busy"
INTERRUPTED_ERROR = "delegation_session_interrupted"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def resolve_delegation_mode(db: AsyncSession, *, entity_id: str, user_id: str | None) -> str:
    """Read the flag once for a root run; any failure keeps delegation inline."""

    from packages.core.services import feature_flags

    try:
        async with db.begin_nested():
            enabled = await feature_flags.is_enabled(
                db, TASK_AGENT_DURABLE_DELEGATION_FLAG,
                entity_id=entity_id or None, user_id=user_id, fallback=False,
            )
    except Exception:
        logger.warning(
            "%s flag evaluation failed; delegation stays inline",
            TASK_AGENT_DURABLE_DELEGATION_FLAG, exc_info=True,
        )
        return DELEGATION_MODE_INLINE
    return DELEGATION_MODE_DURABLE if enabled else DELEGATION_MODE_INLINE


def inline_delegation_started(checkpoint: Any) -> bool:
    """Whether a run with no frozen mode already delegated inline (in flight at rollout).

    Its transcript holds a ``delegate_to_agent`` call, or it waits (or waited)
    on a nested agent's question: it keeps inline delegation, so a delegation
    never changes mode halfway.
    """

    from packages.core.services.task_agent_loop_hitl import DELEGATED_INPUT_WAIT_KIND, HUMAN_NOTE_KEY

    if not isinstance(checkpoint, dict):
        return False
    wait = checkpoint.get("task_loop_wait")
    if isinstance(wait, dict) and wait.get("kind") == DELEGATED_INPUT_WAIT_KIND:
        return True
    if checkpoint.get(HUMAN_NOTE_KEY):
        return True
    parked = checkpoint.get("runtime_checkpoint")
    for state in (checkpoint, parked if isinstance(parked, dict) else {}):
        if "delegate_to_agent" in (state.get("tool_calls_made") or []):
            return True
        for message in state.get("messages") or []:
            for call in (message.get("tool_calls") or []) if isinstance(message, dict) else []:
                function = call.get("function") if isinstance(call, dict) else None
                name = (function or {}).get("name") if isinstance(function, dict) else (call or {}).get("name")
                if name == "delegate_to_agent":
                    return True
    return False


@dataclass(frozen=True)
class DelegationScope:
    """The run that delegates: a Task's root run, or a depth-1 child run."""

    task_id: str
    entity_id: str
    # None for a Task assigned directly to an Agent (it has no targets).
    workspace_id: str | None
    root_run_id: str
    # The delegating run's own id (a child row's ``parent_ref``).
    run_id: str
    # Set when the delegating run is itself a child row (= ``run_id``).
    parent_child_id: str | None
    # The delegating run's depth (0 for the root run).
    depth: int
    # The delegating agent: never one of its own targets by name.
    agent_id: str | None

    @property
    def child_depth(self) -> int:
        return self.depth + 1

    @property
    def can_delegate(self) -> bool:
        return self.child_depth <= TASK_AGENT_CHILD_MAX_DEPTH


def waiting_child_wait(*, call_id: str, child_ids: list[str], parked_at: str | None = None) -> dict[str, Any]:
    """The parent's ``task_loop_wait`` while its call waits on these children."""

    return {
        "version": DELEGATION_WAIT_VERSION,
        "kind": WAITING_CHILD_WAIT_KIND,
        "tool_call_id": call_id,
        "tool_name": "delegate_to_agent",
        "child_ids": list(child_ids),
        "parked_at": parked_at or utcnow().isoformat(),
        "resolution": None,
    }


def unresolved_waiting_child(wait: Any) -> bool:
    return (
        isinstance(wait, dict)
        and wait.get("kind") == WAITING_CHILD_WAIT_KIND
        and wait.get("resolution") is None
    )


def _refusal(payload: dict[str, Any]) -> Any:
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    return RuntimePreflightValidationError(json.dumps(payload, ensure_ascii=False))


def _tool_error(code: str, message: str, **details: Any) -> dict[str, Any]:
    """A refusal in the runtime's tool-error contract (the loop's own refusals' shape)."""

    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    return tool_error_payload(
        tool_name="delegate_to_agent", code=code, message=message,
        details={**details, "action_executed": False},
    )


@dataclass(frozen=True)
class AgentRun:
    """What the handler needs of an agent's latest child run in this Task."""

    id: str
    phase: str
    interrupted: bool


async def _agent_history(
    db: AsyncSession, *, task_id: str, subscription_ids: list[str],
) -> dict[str, AgentRun]:
    """Each agent's latest child run in this Task (any parent)."""

    rows = (await db.execute(
        select(
            TaskAgentChildRun.agent_subscription_id, TaskAgentChildRun.id,
            TaskAgentChildRun.phase, TaskAgentChildRun.interrupted,
        )
        .where(
            TaskAgentChildRun.task_id == task_id,
            TaskAgentChildRun.agent_subscription_id.in_(subscription_ids),
        )
        .order_by(TaskAgentChildRun.created_at.desc(), TaskAgentChildRun.id.desc())
    )).all()
    latest: dict[str, AgentRun] = {}
    for row in rows:
        latest.setdefault(row.agent_subscription_id, AgentRun(row.id, row.phase, bool(row.interrupted)))
    return latest


def _specs(batch: Any, history: dict[str, AgentRun]) -> list[dict[str, Any]]:
    specs = []
    for index, item in enumerate(batch.items):
        params = item["params"]
        subscription_id = str(params["agent_subscription_id"])
        previous = history.get(subscription_id)
        specs.append({
            "item_index": index,
            "agent_label": str(item.get("agent") or "")[:255] or None,
            "agent_subscription_id": subscription_id,
            "agent_id": str(item.get("agent_id") or ""),
            "service_key": item.get("service_key"),
            "prompt": str(params.get("prompt") or ""),
            "answer_max_chars": int(params["answer_max_chars"]),
            "continue_interrupted": params.get("continue_interrupted") is True,
            "previous_child_id": previous.id if previous is not None else None,
        })
    return specs


async def delegate_to_agent_call(
    arguments: dict[str, Any],
    *,
    scope: DelegationScope,
    resumed_wait: Any,
    session_factory: Callable[[], Any],
) -> Any:
    """The durable ``delegate_to_agent`` handler; parks or refuses, never runs an agent.

    Returns the ``waiting_child`` suspension of this exact call, or a
    preflight refusal (nothing ran; the model corrects the call in place).
    A call re-executed while its children still work (a Task resumed while
    it waited) suspends again on the same children without re-checking.
    """

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
    from packages.core.ai.runtime.workspace_delegation_actions import (
        DELEGATE_TO_AGENT_TOOL_NAME,
        runtime_delegation_batch,
        runtime_delegation_tool_arguments,
    )
    from packages.core.services.agent_delegation_session import DELEGATION_INTERRUPTED_MESSAGE
    from packages.core.workers import task_agent_loop_waits as waits

    call_id = waits.tool_call_id(arguments)
    if not call_id:
        return _refusal({
            "error": "delegation_unavailable",
            "message": "Delegation is unavailable in this run; do the work yourself.",
            "action_executed": False,
        })
    public = {key: value for key, value in arguments.items() if key not in RUNTIME_TOOL_CONTEXT_KEYS}
    now = utcnow()

    def suspension(payload: dict[str, Any]) -> Any:
        return RuntimeToolSuspension(
            kind=WAITING_CHILD_WAIT_KIND,
            reservation_id="",
            poll_after_seconds=0,
            deadline_at=now + WAITING_CHILD_DEADLINE,
            reason=DELEGATE_TO_AGENT_TOOL_NAME,
            payload=payload,
        ).with_checkpoint_tool_call(
            tool_call_id=call_id, tool_name=DELEGATE_TO_AGENT_TOOL_NAME, arguments=public,
        )

    if unresolved_waiting_child(resumed_wait) and resumed_wait.get("tool_call_id") == call_id:
        return suspension({"resume": True, "child_ids": list(resumed_wait.get("child_ids") or [])})
    if not scope.can_delegate:
        return _refusal({
            "error": "delegation_depth_limit",
            "message": (
                "This agent was itself delegated to by another agent and cannot delegate "
                "further. Do the work with your own tools or report what is missing."
            ),
            "action_executed": False,
        })
    tasks, shared = runtime_delegation_tool_arguments(arguments)
    batch = await runtime_delegation_batch(
        entity_id=scope.entity_id, workspace_id=scope.workspace_id, tasks=tasks,
        shared=shared, caller_agent_id=scope.agent_id,
    )
    if batch.error is not None:
        return _refusal(json.loads(batch.error))
    subscription_ids = [str(item["params"]["agent_subscription_id"]) for item in batch.items]
    async with session_factory() as db:
        history = await _agent_history(db, task_id=scope.task_id, subscription_ids=subscription_ids)
        await db.rollback()
    for item in batch.items:
        latest = history.get(str(item["params"]["agent_subscription_id"]))
        if latest is None:
            continue
        if latest.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES:
            return _refusal(_tool_error(
                BUSY_ERROR,
                f"{item['agent']} is still working on an earlier delegation in this Task "
                f"(run_id {latest.id}). Wait for that result, or give this work to another agent.",
                run_id=latest.id,
            ))
        if latest.interrupted and item["params"].get("continue_interrupted") is not True:
            return _refusal(_tool_error(
                INTERRUPTED_ERROR, f"{item['agent']}: {DELEGATION_INTERRUPTED_MESSAGE}", run_id=latest.id,
            ))
    try:
        return suspension({"children": _specs(batch, history)})
    except ValueError:
        return _refusal({
            "error": "delegation_too_large",
            "message": (
                "These tasks are too long to hand over in one call. Split them into shorter "
                "tasks, or put the long material in a file and name the file."
            ),
            "action_executed": False,
        })


# ── the park transaction's row work (TaskLoopHost.park_children) ───────


async def insert_child_rows(
    db: AsyncSession,
    *,
    scope: DelegationScope,
    call_id: str,
    specs: list[dict[str, Any]],
    lifecycle_generation: str,
) -> None:
    """Insert one row per task, idempotently per (parent, call, item).

    A replayed park inserts nothing. Another live run of the same agent in
    this Task raises ``IntegrityError`` (``uq_task_agent_child_runs_live_agent``).
    """

    if not specs:
        raise ValueError("A delegation parks on at least one child")
    batch_size = len(specs)
    statement = pg_insert(TaskAgentChildRun).values([
        {
            "id": generate_ulid(),
            "task_id": scope.task_id,
            "entity_id": scope.entity_id,
            "workspace_id": scope.workspace_id,
            "root_run_id": scope.root_run_id,
            "parent_ref": scope.run_id,
            "parent_child_id": scope.parent_child_id,
            "parent_tool_call_id": call_id,
            "item_index": int(spec["item_index"]),
            "batch_size": batch_size,
            "depth": scope.child_depth,
            "agent_subscription_id": spec["agent_subscription_id"],
            "agent_id": spec["agent_id"],
            "service_key": spec.get("service_key"),
            "agent_label": spec.get("agent_label"),
            "previous_child_id": spec.get("previous_child_id"),
            "prompt": spec["prompt"],
            "answer_max_chars": int(spec["answer_max_chars"]),
            "continue_interrupted": bool(spec.get("continue_interrupted")),
            "phase": TaskCoordinatorPhase.READY.value,
            "interrupted": False,
            "lifecycle_generation": lifecycle_generation,
            "state_version": 0,
            "usage": {},
        }
        for spec in specs
    ]).on_conflict_do_nothing(constraint="uq_task_agent_child_runs_call")
    await db.execute(statement)


async def lock_call_children(
    db: AsyncSession, *, parent_ref: str, call_id: str,
) -> list[TaskAgentChildRun]:
    """One call's child rows, locked in (depth, id) order, returned in input order."""

    rows = list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.parent_ref == parent_ref,
            TaskAgentChildRun.parent_tool_call_id == call_id,
        )
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    return sorted(rows, key=lambda row: row.item_index)


def resumed_batch_state(
    checkpoint: dict[str, Any],
    wait: dict[str, Any],
    rows: list[TaskAgentChildRun],
    *,
    lifecycle_generation: str,
) -> str:
    """Where an unresolved delegation stands when its parent wakes.

    ``"running"``: some child is open (the parent parks again);
    ``"all_terminal"``: every child ended (the batch settles now);
    ``"malformed"``: the wait does not match its parked call or its children
    (missing, foreign, or an open one on another lifecycle).
    """

    from packages.core.services.task_agent_child_wakeup import _waiting_child_ids

    child_ids = _waiting_child_ids(wait)
    parked = checkpoint.get("runtime_checkpoint") if isinstance(checkpoint, dict) else None
    pending = parked.get("pending_tool_call") if isinstance(parked, dict) else None
    if (
        child_ids is None
        or not isinstance(pending, dict)
        or str(pending.get("id") or "") != str(wait.get("tool_call_id") or "")
        or [row.id for row in rows] != child_ids
    ):
        return "malformed"
    open_rows = [row for row in rows if row.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES]
    if any(row.lifecycle_generation != lifecycle_generation for row in open_rows):
        return "malformed"
    return "running" if open_rows else "all_terminal"


async def busy_refusal(
    session_factory: Callable[[], Any], *, scope: DelegationScope, specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """The refusal a park replays when another live run of an agent won the race."""

    async with session_factory() as db:
        live = list((await db.execute(
            select(TaskAgentChildRun.id, TaskAgentChildRun.agent_subscription_id)
            .where(
                TaskAgentChildRun.task_id == scope.task_id,
                TaskAgentChildRun.agent_subscription_id.in_([spec["agent_subscription_id"] for spec in specs]),
                TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
            )
        )).all())
        await db.rollback()
    labels = {spec["agent_subscription_id"]: spec.get("agent_label") or "An agent" for spec in specs}
    if live:
        run_id, subscription_id = live[0]
        message = (
            f"{labels.get(subscription_id, 'An agent')} is still working on an earlier delegation "
            f"in this Task (run_id {run_id}). Wait for that result, or give this work to another agent."
        )
    else:
        run_id, message = None, (
            "One of these agents started other work in this Task at the same moment. "
            "Delegate again once it is done."
        )
    return _tool_error(BUSY_ERROR, message, run_id=run_id)


def children_match_specs(rows: list[TaskAgentChildRun], specs: list[dict[str, Any]]) -> bool:
    return len(rows) == len(specs) and all(
        row.item_index == spec["item_index"]
        and row.agent_subscription_id == spec["agent_subscription_id"]
        and row.batch_size == len(specs)
        for row, spec in zip(rows, specs)
    )


async def stage_children(
    db: AsyncSession, rows: list[TaskAgentChildRun], *, now: datetime,
) -> list[TaskAgentChildRun]:
    """Give each runnable child of one call a wake (an existing one is kept).

    Staggered as chat staggers its parallel children. A parked child wakes
    at its own time; one waiting on its own children or on a person is woken
    by them, not here. A child whose person decided while its Task was paused
    (its human wait carries the resolution) runs now, as a ready one. Returns
    the rows that got a new wake: the caller journals them (the parent's
    delegate row names its own call's children).
    """

    from packages.core.ai.runtime.workspace_delegation_actions import PARALLEL_DELEGATION_STAGGER_SECONDS
    from packages.core.services.task_agent_child_wakeup import parked_run_wake_time, run_loop_wait

    staged: list[TaskAgentChildRun] = []
    for index, row in enumerate(rows):
        decided = (
            row.phase == TaskCoordinatorPhase.WAITING_HUMAN.value
            and (run_loop_wait(row) or {}).get("resolution") is not None
        )
        if row.phase in {TaskCoordinatorPhase.READY.value, TaskCoordinatorPhase.RUNNING.value} or decided:
            available_at = now + timedelta(seconds=index * PARALLEL_DELEGATION_STAGGER_SECONDS)
        elif row.phase in {TaskCoordinatorPhase.WAITING_TIMER.value, TaskCoordinatorPhase.WAITING_JOB.value}:
            available_at = await parked_run_wake_time(db, row, now=now)
        else:
            continue
        version = row.state_version
        wake = await stage_task_agent_child_wake(
            db, child_id=row.id, reason="delegated", available_at=available_at, if_pending=True,
        )
        if wake is not None and row.state_version != version:
            # A new wake (a still-pending one is kept as is, unbumped).
            staged.append(row)
    return staged


async def restage_descendants(db: AsyncSession, *, task: Any, root_run_id: str, now: datetime) -> int:
    """After a Task resume, give every runnable child below the root a wake.

    The root's own children are staged by its re-park; this covers deeper
    rows (a depth-1 child waiting on its own children keeps waiting, its
    children need their wakes back) and settles a depth-1 child's batch that
    ended while the Task was paused.
    """

    from packages.core.services.task_agent_child_settlement import settle_waiting_batch

    # Every open row of the tree, in the canonical (depth, id) lock order (the
    # root's own children are already locked by its park).
    rows = list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.root_run_id == root_run_id,
            TaskAgentChildRun.task_id == task.id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        )
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    from packages.core.models.task_run_journal import TaskRunJournalKind
    from packages.core.services.task_agent_child_wakeup import run_loop_wait
    from packages.core.services.task_run_journal import append_run_journal, wake_payload

    restaged = await stage_children(db, [row for row in rows if row.depth > 1], now=now)
    for row in restaged:
        # No delegate row names these (their parent did not re-park): each
        # journals its own wake with the resume.
        await append_run_journal(
            db, run=row, kind=TaskRunJournalKind.WAKE,
            payload=wake_payload(run_loop_wait(row), resolution="task_resumed", task_resumed=True),
        )
    staged = len(restaged)
    for parent in rows:
        if parent.phase == TaskCoordinatorPhase.WAITING_CHILD.value and await settle_waiting_batch(
            db, task=task, parent=parent,
        ):
            staged += 1
    return staged


__all__ = [
    "BUSY_ERROR",
    "CHILDREN_CANCELLED",
    "CHILDREN_SETTLED",
    "DelegationScope",
    "INTERRUPTED_ERROR",
    "TASK_AGENT_DURABLE_DELEGATION_FLAG",
    "busy_refusal",
    "children_match_specs",
    "resumed_batch_state",
    "delegate_to_agent_call",
    "insert_child_rows",
    "lock_call_children",
    "resolve_delegation_mode",
    "restage_descendants",
    "stage_children",
    "unresolved_waiting_child",
    "waiting_child_wait",
]
