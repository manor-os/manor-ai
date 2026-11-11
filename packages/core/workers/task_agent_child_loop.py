"""The HEAVY worker of a delegated child run (``runtime.run_task_agent_child``).

A child run is one Workspace agent working one delegated task inside a Task,
driven wake by wake as a durable run with the root loop's own host
(``workers.task_loop_host.TaskLoopHost``).

Admission, in one transaction under the Workspace -> Task -> root Run ->
parent child -> child lock order (``admit_task_agent_child``):

1. the Workspace allows work and the Task is in progress;
2. the root run is a live agent loop on the Task's current lifecycle;
3. the direct parent is parked, unresolved, on this child's call;
4. the child is not terminal and is on the Task's current lifecycle;
5. the delivery is the child's current, undelivered wake generation;
6. fewer than ``TASK_AGENT_CHILD_MAX_RUNNING`` other children of the Task
   hold a live claim (otherwise the wake is deferred, never failed);
7. and only then the claim (``durable_run.claim_run_wake``).

The child runs exactly as chat runs a delegated agent: the shared delegation
core builds its identity, context, tools, round budget and billing
(``runtime_delegated_child_setup``, ``runtime_delegated_child_billing``) and
shapes its result (``runtime_delegated_child_result``). What differs is only
the host: it checkpoints every round, yields at the Task loop's slice, parks
on media jobs instead of blocking, can delegate one level further as a
durable parent itself (depth 1 only; a depth-2 child gets no delegation
tool), and ends by settling itself into its parent
(``services.task_agent_child_settlement``).

A child that needs the user parks on its own card, exactly as the root loop
parks (``TaskLoopHost.park_human``): its question (``request_input``) on a
Task input card "Asked by {agent}", its approval-gated call on a Task-thread
approval card signed for this child (a v2 park tag). Only this child waits:
its siblings keep working and the Task waits on the person only once nothing
in the run tree can move forward (``sync_task_human_wait_status``). The
answer is the pending call's result; an approval re-runs the exact call,
which spends the grant once; a rejection is a host-written gate result and
the child continues.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from packages.core.ai.runtime.harness import runtime_execute_chat_agent_loop
from packages.core.ai.runtime.interrupted_tool_calls import runtime_answer_interrupted_calls
from packages.core.ai.runtime.media_job_scope import runtime_media_jobs_detached_scope
from packages.core.ai.runtime.run_state import RuntimeRunState, RuntimeRunStateVersionError
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
from packages.core.ai.runtime.workspace_delegation_actions import (
    DELEGATE_TO_AGENT_TOOL_NAME,
    DelegatedAnswerLengthContract,
    _answer_budget_note,
    runtime_delegated_child_billing,
    runtime_delegated_child_result,
    runtime_delegated_child_setup,
    runtime_delegated_tool_outcome,
    runtime_delegation_depth_scope,
)
from packages.core.constants.task import TaskStatus
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_agent_child import (
    TASK_AGENT_CHILD_TERMINAL_PHASES,
    TaskAgentChildRun,
)
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.services import durable_run
from packages.core.services.billing_payer import resolve_run_payer
from packages.core.services import task_agent_delegation as delegation
from packages.core.services import task_agent_loop_hitl as loop_hitl
from packages.core.services.durable_run import DurableRunClaim, holds_current_wake
from packages.core.services.task_agent_child_settlement import (
    ChildOutcome,
    child_effects_unknown,
    child_failure_result,
    settle_task_agent_child,
)
from packages.core.services.task_agent_child_state import (
    TOOL_OUTCOMES_KEY,
    record_tool_outcome,
    task_agent_child_initial_state,
    task_agent_child_run_state_slots,
)
from packages.core.services.task_agent_child_wakeup import (
    TASK_AGENT_CHILD_DEFERRED_PREFIX,
    TASK_AGENT_CHILD_OUTBOX_EVENT,
    TASK_AGENT_CHILD_RUN_SPEC,
    TASK_AGENT_CHILD_RUNNING_CAP_DEFER_SECONDS,
    WAITING_CHILD_WAIT_KIND,
    advance_deferred_child_wake,
    awaited_child_refusal,
    live_sibling_claims,
    task_agent_child_max_running,
    task_agent_child_outbox_dedupe_key,
)
from packages.core.services.task_requester_identity import (
    TaskRequesterIdentityError,
    resolve_task_run_user_id,
)
from packages.core.workers import task_agent_loop as root_loop
from packages.core.workers import task_agent_loop_waits as waits
from packages.core.workers import task_run_host
from packages.core.workers.task_loop_host import DurableRunWrites, TaskLoopHost
from packages.core.workers.task_run_host import admit_task_scope


logger = logging.getLogger(__name__)

RUNNING_CAP = "running_cap"
# ``_admit_and_claim``: claimed a running child whose previous owner died.
TOOK_OVER = "took_over"
_LABEL = "Task agent child"
_REASON_PREFIX = "task_agent_child_"
_LEASE_SECONDS = task_run_host.TASK_RUN_LEASE_SECONDS
_HEARTBEAT_SECONDS = task_run_host.TASK_RUN_HEARTBEAT_SECONDS
_MAX_WAKE_FAILURES = 3
_WAKE_FAILURES_KEY = durable_run.DEFAULT_WAKE_FAILURES_KEY
RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"

CHILD_QUESTION_UNREADABLE = "agent_child_question_unreadable"
CHILD_APPROVAL_CLOSED_BEFORE_PARK = "agent_child_approval_closed_before_park"
CHILD_REPAIR_REQUIRED = "agent_child_repair_required"
CHILD_REQUESTER_UNAVAILABLE = "agent_child_requester_unavailable"
CHILD_RESOURCE_WAIT_EXPIRED = "agent_child_resource_wait_expired"
CHILD_SUSPENSION_UNSUPPORTED = "agent_child_suspension_unsupported"

# What a resumed slice of a child is told (chat's continuation wording).
_CONTINUE_MESSAGE = (
    "Continue the same delegated checklist item from the durable transcript. "
    "Do not repeat tool work whose successful result is already present. "
    "Finish the original deliverable."
)
# The host-only tools of a Task's root loop: never a child's.
_ROOT_ONLY_TOOLS = frozenset({
    waits.WAIT_UNTIL_TOOL_NAME,
    root_loop.UPDATE_TASK_PLAN_TOOL_NAME,
    root_loop.COMPLETE_TASK_TOOL_NAME,
})


def _tool_name(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    return str((schema.get("function") or {}).get("name") or schema.get("name") or "")


# ── admission and the claim ─────────────────────────────────────────────


async def admit_task_agent_child(
    db: Any,
    *,
    task_id: str,
    child_id: str,
    event_id: str,
    wake_generation: str,
) -> tuple[TaskAgentChildRun | None, str]:
    """Admit one child wake under the canonical locks; no claim is taken.

    Returns the locked child and ``"admitted"``, or None and the reason the
    wake is deferred (``RUNNING_CAP`` when the per-Task cap is reached).
    """

    scope_child = (await db.execute(
        select(TaskAgentChildRun.task_id, TaskAgentChildRun.parent_child_id)
        .where(TaskAgentChildRun.id == child_id)
    )).one_or_none()
    if scope_child is None or scope_child.task_id != task_id:
        return None, "missing_run"
    task, reason = await admit_task_scope(db, task_id)
    if task is None:
        return None, reason
    root = (await db.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    parent = None
    if scope_child.parent_child_id:
        parent = (await db.execute(
            select(TaskAgentChildRun).where(
                TaskAgentChildRun.id == scope_child.parent_child_id,
                TaskAgentChildRun.task_id == task_id,
            )
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
    child = await durable_run.lock_run(db, TASK_AGENT_CHILD_RUN_SPEC, child_id)
    if child is None or child.task_id != task_id:
        return None, "missing_run"
    if child.phase in TASK_AGENT_CHILD_TERMINAL_PHASES:
        return None, "terminal_run"
    if root is None or (child.parent_child_id and parent is None):
        return None, "parent_not_waiting"
    # The shared delivery rule, read here under the root and parent locks.
    refusal = await awaited_child_refusal(db, child, task)
    if refusal is not None:
        return None, refusal
    event = (await db.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.id == event_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    if (
        event is None or event.delivered_at is not None
        or event.event_type != TASK_AGENT_CHILD_OUTBOX_EVENT
        or event.aggregate_id != child_id
        or event.dedupe_key != task_agent_child_outbox_dedupe_key(child_id)
        or payload.get("wake_generation") != wake_generation
        or child.wake_generation != wake_generation
    ):
        return None, "stale_generation"
    if await live_sibling_claims(db, child) >= task_agent_child_max_running():
        return None, RUNNING_CAP
    return child, "admitted"


async def _defer_for_running_cap(db: Any, *, event_id: str, wake_generation: str) -> bool:
    """Push this exact pending wake out by the cap's retry delay."""

    event = (await db.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.id == event_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    if event is None or event.delivered_at is not None or payload.get("wake_generation") != wake_generation:
        return False
    event.available_at = datetime.now(timezone.utc) + timedelta(
        seconds=TASK_AGENT_CHILD_RUNNING_CAP_DEFER_SECONDS,
    )
    event.last_error = f"{TASK_AGENT_CHILD_DEFERRED_PREFIX}: {RUNNING_CAP}"
    return True


async def _admit_and_claim(
    session_factory: Any,
    *,
    event_id: str,
    task_id: str,
    child_id: str,
    wake_generation: str,
) -> tuple[DurableRunClaim | None, str]:
    """Admission, then the claim as its last step, in one transaction."""

    async with session_factory() as db:
        child, admission = await admit_task_agent_child(
            db, task_id=task_id, child_id=child_id, event_id=event_id, wake_generation=wake_generation,
        )
        if admission == RUNNING_CAP:
            deferred = await _defer_for_running_cap(db, event_id=event_id, wake_generation=wake_generation)
            await db.commit()
            return None, RUNNING_CAP if deferred else "stale_generation"
        if child is None:
            await db.rollback()
            return None, admission
        # A running child whose claim lapsed: its previous owner died mid-wake
        # (claim_run_wake refuses a live claim; a yield or park releases it).
        took_over = child.phase == TaskCoordinatorPhase.RUNNING.value and child.claim_token is not None
        claim, reason = await durable_run.claim_run_wake(
            db, TASK_AGENT_CHILD_RUN_SPEC,
            key=child_id,
            event_id=event_id,
            wake_generation=wake_generation,
            claim_token=generate_ulid(),
            lease_seconds=_LEASE_SECONDS,
        )
        if claim is None:
            await db.rollback()
            return None, reason
        await db.commit()
        return claim, TOOK_OVER if took_over else "claimed"


async def child_claim_is_current(claim: DurableRunClaim, *, session_factory: Any) -> bool:
    """Whether this owner may still do provider work for its child run.

    The Task is in progress and its Workspace allows work; the root run and
    the direct parent still wait on this child; the claim is live on the
    current wake and lifecycle; the pinned agent is still active as pinned.
    """

    from packages.core.models.workspace import Workspace

    async with session_factory() as db:
        child = (await db.execute(
            select(TaskAgentChildRun).where(TaskAgentChildRun.id == claim.key)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if child is None:
            return False
        task = (await db.execute(
            select(Task).where(Task.id == child.task_id).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None or task.status != TaskStatus.IN_PROGRESS.value:
            return False
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == task.workspace_id, Workspace.entity_id == task.entity_id)
        )).scalar_one_or_none()
        if not task_run_host.workspace_allows_manual_work(workspace):
            return False
        if (
            child.phase in TASK_AGENT_CHILD_TERMINAL_PHASES
            or not holds_current_wake(child, claim.claim_token, claim.wake_generation)
            # The shared delivery rule: current lifecycle, live root, and a
            # parent still parked on exactly this child.
            or await awaited_child_refusal(db, child, task) is not None
        ):
            return False
        return await _pinned_agent_active(db, child)


async def _pinned_agent_active(db: Any, child: TaskAgentChildRun) -> bool:
    """The subscription and agent pinned at creation are still active and unchanged."""

    from packages.core.models.workspace import Agent, AgentSubscription

    sub = (await db.execute(
        select(AgentSubscription).where(AgentSubscription.id == child.agent_subscription_id)
    )).scalar_one_or_none()
    agent = (await db.execute(select(Agent).where(Agent.id == child.agent_id))).scalar_one_or_none()
    return bool(
        sub is not None and agent is not None
        and sub.status == "active" and sub.agent_id == child.agent_id
        and (not child.service_key or sub.service_key == child.service_key)
        and agent.status == "active" and agent.deleted_at is None
    )


async def _claim_is_current(claim: DurableRunClaim, session_factory: Any) -> bool:
    # Resolved at call time (the name tests patch).
    return await child_claim_is_current(claim, session_factory=session_factory)


async def _renew(db: Any, claim: DurableRunClaim, lease_seconds: float) -> bool:
    return await durable_run.renew_run_claim(
        db, TASK_AGENT_CHILD_RUN_SPEC,
        key=claim.key,
        claim_token=claim.claim_token,
        claim_generation=claim.wake_generation,
        lease_seconds=lease_seconds,
    )


def _child_host(
    claim: DurableRunClaim, *, task_id: str, state: dict[str, Any], session_factory: Any,
) -> TaskLoopHost:
    """The host of one claimed wake of one child run."""

    from packages.core.database import create_worker_session

    async def _free_slot(db: Any) -> None:
        # This child no longer holds a HEAVY slot of its Task.
        await advance_deferred_child_wake(db, task_id=task_id)

    return TaskLoopHost(
        claim=claim,
        writes=DurableRunWrites(TASK_AGENT_CHILD_RUN_SPEC, claim.key),
        task_id=task_id,
        state=state,
        session_factory=session_factory,
        is_current=lambda current: _claim_is_current(current, session_factory),
        heartbeat=lambda current, lost: task_run_host.heartbeat(
            current, lost,
            interval_seconds=_HEARTBEAT_SECONDS,
            lease_seconds=_LEASE_SECONDS,
            session_factory=create_worker_session(),
            label=_LABEL,
            renew=_renew,
        ),
        slice_seconds=lambda: root_loop.TASK_AGENT_LOOP_SLICE_SECONDS,
        label=_LABEL,
        reason_prefix=_REASON_PREFIX,
        on_close=_free_slot,
    )


class _ChildAnswerContract(DelegatedAnswerLengthContract):
    """Chat's one-chance answer contract, its chance kept in the run state slot."""

    def __init__(self, max_chars: int, state: dict[str, Any]):
        self._state = state
        kept = bool(state.get("answer_revised"))
        super().__init__(max_chars)
        self._state["answer_revised"] = kept

    @property
    def revised(self) -> bool:
        return bool(self._state.get("answer_revised"))

    @revised.setter
    def revised(self, value: bool) -> None:
        self._state["answer_revised"] = bool(value)


# ── ending the child ────────────────────────────────────────────────────


async def _settle(host: TaskLoopHost, claim: DurableRunClaim, outcome: ChildOutcome) -> dict[str, Any]:
    async with host.mutation_lock:
        async with host.session_factory() as db:
            if not await settle_task_agent_child(
                db,
                child_id=claim.key,
                event_id=claim.event_id,
                claim_token=claim.claim_token,
                claim_generation=claim.wake_generation,
                outcome=outcome,
            ):
                await db.rollback()
                return {"status": "stale", "child_id": claim.key}
            await db.commit()
    return {"status": outcome.phase, "child_id": claim.key, "outcome": outcome.outcome_status}


def _host_failure(
    host: TaskLoopHost | None,
    child: TaskAgentChildRun,
    *,
    code: str,
    message: str,
    checkpoint: dict[str, Any],
    ledger_complete: bool,
) -> ChildOutcome:
    """A child its host failed, in the shared failure shape.

    Interrupted (the next delegation to the agent needs
    ``continue_interrupted``) only when its effects are unknown: its last
    round is unknown (``ledger_complete`` false) or its ledger holds an
    unconfirmed effect. A failure whose ledger is whole and confirmed (an
    identity change, any deterministic refusal) is a plain failure.
    """

    usage = checkpoint.get("usage") if isinstance(checkpoint.get("usage"), dict) else {}
    # This wake's own history: a crash earlier in it, or a dead predecessor.
    wake_generation = getattr(getattr(host, "claim", None), "wake_generation", None)
    took_over = bool(getattr(host, "took_over", False))
    lost_work = child_effects_unknown(
        {key: value for key, value in checkpoint.items() if key == _WAKE_FAILURES_KEY},
        ledger_complete=True, wake_generation=wake_generation, took_over=took_over,
    )
    # Work this wake lost (after its last checkpoint) is not in the ledger:
    # the parent's floor must not read the ledger as whole.
    ledger_complete = ledger_complete and not lost_work
    interrupted = child_effects_unknown(checkpoint, ledger_complete=ledger_complete)
    result = child_failure_result(
        child, status="failed", error=code, message=message,
        rounds=int(checkpoint.get("rounds") or 0),
        tool_calls_made=[str(name) for name in checkpoint.get("tool_calls_made") or []],
        usage=usage,
        interrupted=interrupted,
    )
    return ChildOutcome(
        phase=TaskCoordinatorPhase.FAILED.value,
        outcome_status="failed",
        interrupted=interrupted,
        result=result,
        checkpoint={key: value for key, value in checkpoint.items() if key != _WAKE_FAILURES_KEY},
        usage=usage,
        ledger_complete=ledger_complete,
        error_code=code,
        source="host",
    )


def _closed_before_park_message(approval: dict[str, Any], *, withdrawn: bool) -> str:
    what = f"`{approval['tool']}`" if approval.get("tool") else "an action"
    request = " The approval request was withdrawn." if withdrawn else ""
    return (
        f"The delegated agent stopped because the approval for {what} was decided or "
        f"closed before it could wait on it. Nothing was run.{request} Delegate again "
        "if the action is still needed; it will ask for approval again."
    )


def _unresumable_approval_message(approval: dict[str, Any], *, withdrawn: bool) -> str:
    what = f"`{approval['tool']}`" if approval.get("tool") else "an action"
    request = " The approval request was withdrawn." if withdrawn else ""
    return (
        f"The delegated agent stopped because {what} needs approval that cannot be resumed "
        "at the exact call that asked for it (it came from inside a skill or browser "
        f"sequence, or from the provider itself). Nothing was run.{request} A standing "
        "Workspace rule that auto-approves the action lets it run without stopping."
    )


# ── one wake ────────────────────────────────────────────────────────────


async def run_task_agent_child_once(
    *,
    event_id: str,
    task_id: str,
    child_id: str,
    wake_generation: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Admit and claim one exact child wake, then run the child until it parks or ends."""

    if not event_id or not task_id or not child_id or not wake_generation:
        return {"status": "closed", "reason": "invalid_delivery"}
    from packages.core.database import create_worker_session

    session_factory = create_worker_session()
    claim, admission = await _admit_and_claim(
        session_factory,
        event_id=event_id, task_id=task_id, child_id=child_id, wake_generation=wake_generation,
    )
    if claim is None:
        return {"status": "deferred", "reason": admission}
    state = task_agent_child_initial_state(claim.state_version)
    slots = task_agent_child_run_state_slots(state)
    host = _child_host(claim, task_id=task_id, state=state, session_factory=session_factory)
    host.took_over = admission == TOOK_OVER
    host.start()
    child: TaskAgentChildRun | None = None
    try:
        async with session_factory() as db:
            child = (await db.execute(
                select(TaskAgentChildRun).where(TaskAgentChildRun.id == child_id)
            )).scalar_one()
            db.expunge(child)
        return await _run_child(
            claim, child, host=host, state=state, slots=slots, session_factory=session_factory,
        )
    except Exception as exc:
        if isinstance(exc, RuntimeRunStateVersionError):
            logger.warning(
                "Task agent child %s checkpoint was saved by a newer runtime; leaving the wake "
                "for a current worker", child_id,
            )
        if root_loop._is_transient_error(exc):
            raise
        if child is None or host.lost.is_set() or not await host.is_current():
            raise
        failures, current = await host.record_wake_failure(exc)
        if failures < _MAX_WAKE_FAILURES:
            raise
        logger.exception("Task agent child %s failed %d times on one wake", child_id, failures)
        return await _settle(host, claim, _host_failure(host,
            child,
            code=CHILD_REPAIR_REQUIRED,
            message=(
                f"The delegated agent failed {failures} times in a row ({type(exc).__name__}) "
                "and was stopped. Its last action may or may not have taken effect."
            ),
            checkpoint=current,
            ledger_complete=False,
        ))
    finally:
        await host.close()


async def _run_child(
    claim: DurableRunClaim,
    child: TaskAgentChildRun,
    *,
    host: TaskLoopHost,
    state: dict[str, Any],
    slots: Any,
    session_factory: Any,
) -> dict[str, Any]:
    task_id = child.task_id
    checkpoint = copy.deepcopy(child.checkpoint or {})
    state["last_checkpoint"] = checkpoint
    run_state = RuntimeRunState.restore(checkpoint, slots)
    slots.load(run_state.host_slots)
    if isinstance(checkpoint.get(waits.TASK_LOOP_WAIT_KEY), dict):
        state["resumed_wait"] = checkpoint[waits.TASK_LOOP_WAIT_KEY]
    host.entity_id = child.entity_id
    host.child_identity(
        root_run_id=child.root_run_id,
        agent_label=child.agent_label or child.service_key,
        agent_subscription_id=child.agent_subscription_id,
    )
    parked = checkpoint.get(RUNTIME_CHECKPOINT_KEY)
    fresh = not run_state.messages and not isinstance(parked, dict)
    async with session_factory() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
        if child.parent_child_id:
            parent_agent_id = (await db.execute(
                select(TaskAgentChildRun.agent_id).where(TaskAgentChildRun.id == child.parent_child_id)
            )).scalar_one_or_none()
        else:
            parent_agent_id = (await db.execute(
                select(TaskCoordinatorRun.root_agent_id).where(TaskCoordinatorRun.id == child.root_run_id)
            )).scalar_one_or_none()
        previous_messages: list[dict[str, Any]] = []
        if fresh and child.previous_child_id:
            previous = (await db.execute(
                select(TaskAgentChildRun.checkpoint).where(
                    TaskAgentChildRun.id == child.previous_child_id,
                    TaskAgentChildRun.task_id == task_id,
                )
            )).scalar_one_or_none()
            if isinstance(previous, dict):
                # The agent's earlier work in this Task, as chat's session
                # history; calls it never finished are answered first.
                previous_messages = [
                    message for message in runtime_answer_interrupted_calls(previous)
                    if message.get("role") != "system"
                ]
        try:
            user_id = await resolve_task_run_user_id(db, task)
        except TaskRequesterIdentityError:
            user_id = None
        conversation_id = task.conversation_id
        entity_id = str(task.entity_id)
        workspace_id = str(task.workspace_id) if task.workspace_id else None
        runtime_metadata = root_loop.task_runtime_metadata(task)
        await db.commit()

    if not user_id:
        return await _settle(host, claim, _host_failure(host,
            child, code=CHILD_REQUESTER_UNAVAILABLE,
            message="No current workspace member can own this delegated agent's model and tool calls.",
            checkpoint=checkpoint, ledger_complete=True,
        ))
    human = checkpoint.get(waits.TASK_LOOP_WAIT_KEY)
    if (
        isinstance(human, dict)
        and human.get("kind") in loop_hitl.HUMAN_WAIT_KINDS
        and human.get("resolution") is None
    ):
        # Woken while its person has not decided (its Task was paused and
        # resumed): wait on the same card again, without a model call.
        if await host.human_wait_still_open(human):
            again = await host.park_human(
                kind=human["kind"],
                base_checkpoint=checkpoint,
                runtime_checkpoint=parked if isinstance(parked, dict) else None,
                tool_call_id=str(human.get("tool_call_id") or ""),
                tool_name=str(human.get("tool_name") or ""),
                summary=str(human.get("summary") or ""),
                reuse_wait=human,
            )
            if again is None:
                return {"status": "stale", "child_id": child.id}
            return {"status": "waiting_human", "child_id": child.id, "kind": human["kind"]}
        # The card was closed without a decision reaching this run: the
        # parked call runs again and asks again if it still needs to.
        checkpoint = {key: value for key, value in checkpoint.items() if key != waits.TASK_LOOP_WAIT_KEY}
        state["resumed_wait"] = None
    scope = delegation.DelegationScope(
        task_id=task_id, entity_id=entity_id, workspace_id=workspace_id,
        root_run_id=child.root_run_id, run_id=child.id, parent_child_id=child.id,
        depth=child.depth, agent_id=child.agent_id,
    )
    waiting = checkpoint.get(waits.TASK_LOOP_WAIT_KEY)
    if delegation.unresolved_waiting_child(waiting):
        # Woken while its own children's batch is open: never a model round.
        resumed = await host.resume_children_wait(scope=scope, checkpoint=checkpoint, wait=waiting)
        if resumed is None:
            return {"status": "stale", "child_id": child.id}
        if resumed["status"] != "continue":
            return {"status": resumed["status"], "child_id": child.id}
        checkpoint = copy.deepcopy(resumed["checkpoint"])
        state["last_checkpoint"] = checkpoint
        state["resumed_wait"] = checkpoint.get(waits.TASK_LOOP_WAIT_KEY)
        parked = checkpoint.get(RUNTIME_CHECKPOINT_KEY)

    async with session_factory() as db:
        setup = await runtime_delegated_child_setup(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=task_id,
            step_id=None,
            raw_params={
                "agent_subscription_id": child.agent_subscription_id,
                "agent_id": child.agent_id,
                "service_key": child.service_key or "",
                "prompt": child.prompt,
            },
            prompt=child.prompt,
            caller_agent_id=parent_agent_id,
            runtime_metadata=runtime_metadata,
        )
        await db.commit()
    if setup.error is not None:
        try:
            refusal = json.loads(setup.error)
        except (TypeError, ValueError):
            refusal = {}
        return await _settle(host, claim, _host_failure(host,
            child,
            code=str(refusal.get("error") or "workspace_service_identity_changed"),
            message=str(refusal.get("message") or "The delegated agent is no longer available."),
            checkpoint=checkpoint, ledger_complete=True,
        ))
    ctx = setup.ctx
    tools = [tool for tool in setup.tools if _tool_name(tool) not in _ROOT_ONLY_TOOLS]
    allowed = (
        [name for name in setup.allowed_tool_names if name not in _ROOT_ONLY_TOOLS]
        if setup.allowed_tool_names is not None
        else None
    )
    can_delegate = scope.can_delegate and any(_tool_name(tool) == DELEGATE_TO_AGENT_TOOL_NAME for tool in tools)
    if not can_delegate:
        # The deepest child gets no delegation tool at all (no placeholder).
        tools = [tool for tool in tools if _tool_name(tool) != DELEGATE_TO_AGENT_TOOL_NAME]
        if allowed is not None:
            allowed = [name for name in allowed if name != DELEGATE_TO_AGENT_TOOL_NAME]

    evidence = RuntimeToolEvidence()

    async def _delegate(arguments: dict[str, Any]) -> Any:
        return await delegation.delegate_to_agent_call(
            arguments, scope=scope, resumed_wait=state["resumed_wait"], session_factory=session_factory,
        )

    async def _wait_media_jobs(arguments: dict[str, Any]) -> Any:
        return await root_loop.wait_media_jobs_call(
            arguments, resumed=state["resumed_wait"], session_factory=session_factory,
            entity_id=entity_id, task_id=task_id,
        )

    def _on_tool_end(name: str, result: Any, _duration: Any = None, arguments: Any = None) -> None:
        record_tool_outcome(state, runtime_delegated_tool_outcome(name, result))
        host.observe_tool_end(name, result, arguments)

    handlers: dict[str, Any] = {waits.WAIT_MEDIA_JOBS_TOOL_NAME: _wait_media_jobs}
    if can_delegate:
        handlers[DELEGATE_TO_AGENT_TOOL_NAME] = _delegate
    answer_params = {"answer_max_chars": child.answer_max_chars}
    loop_kwargs: dict[str, Any] = {
        "runtime_envelope": ctx.runtime_envelope,
        "prompt_context": getattr(ctx, "prompt_context", None),
        "system_prompt": ctx.system_prompt,
        "user_message": child.prompt,
        "tools": tools,
        "entity_id": entity_id,
        "user_id": user_id,
        "agent_id": child.agent_id,
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "step_id": None,
        "active_user_message": child.prompt,
        "tool_profile": ctx.tool_profile,
        "allowed_tool_names": allowed,
        "model": ctx.model,
        "temperature": getattr(ctx, "temperature", None),
        "max_tokens": getattr(ctx, "max_tokens", None),
        "max_rounds": setup.max_rounds,
        "hard_max_rounds": setup.max_rounds,
        "metadata": getattr(ctx, "llm_metadata", None),
        "human_input_suspension": True,
        "hitl_resume_checkpoint": True,
        "tool_evidence": evidence,
        "on_tool_end": _on_tool_end,
        "on_round_end": host.checkpoint,
        "on_round_end_strict": True,
        "run_state_slots": slots,
        "runtime_cancel_checker": host.cancel_requested,
        "dynamic_tool_handlers": handlers,
        "final_response_contract": _ChildAnswerContract(child.answer_max_chars, state),
        "turn_context": _answer_budget_note(answer_params),
    }
    if isinstance(parked, dict):
        loop_kwargs["runtime_checkpoint"] = copy.deepcopy(parked)
    elif run_state.messages:
        loop_kwargs.update(run_state=run_state, user_message=_CONTINUE_MESSAGE)
    else:
        if previous_messages:
            loop_kwargs["initial_messages"] = previous_messages
        if setup.skill_refs:
            loop_kwargs["forced_tool_calls"] = [
                {"name": "invoke_skill", "arguments": {"skill_id": ref, "input": child.prompt}}
                for ref in setup.skill_refs
            ]

    with runtime_delegated_child_billing(
        run_id=child.id,
        # A fresh nonce per wake: real provider work repeated after a crash
        # is billed, never hidden by an earlier process's settlement fence.
        invocation_id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=child.agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        fallback_model=ctx.model,
        payer_entity_id=await resolve_run_payer(
            None, entity_id=entity_id, workspace_id=workspace_id,
        ),
        task_id=task_id,
    ) as (reserve_llm_call, settle_llm_call):

        async def _before_model(*args: Any, **kwargs: Any) -> Any:
            await host.before_model()
            return await reserve_llm_call(*args, **kwargs)

        loop_kwargs["on_llm_call_before"] = _before_model
        loop_kwargs["on_llm_usage_settled"] = settle_llm_call
        try:
            # The registered (nested) delegation sees this run's own depth:
            # the deepest child cannot delegate through it either, even when
            # its tool list was not narrowed.
            with (
                runtime_media_jobs_detached_scope(task_id=task_id, run_id=child.id),
                runtime_delegation_depth_scope(child.depth),
            ):
                result = await runtime_execute_chat_agent_loop(**loop_kwargs)
        except (RuntimeError, ValueError):
            if not await host.is_current():
                return {"status": "superseded", "child_id": child.id}
            raise

    if host.lost.is_set():
        return {"status": "superseded", "child_id": child.id}
    final_run_state = getattr(result, "run_state", None)
    if not isinstance(final_run_state, RuntimeRunState):
        final_run_state = RuntimeRunState(
            messages=list(result.messages or []),
            usage=dict(result.usage or {}),
            rounds=max(0, int(result.rounds or 0)),
            tool_calls_made=[str(item) for item in result.tool_calls_made or []],
            tool_evidence=evidence.checkpoint_state(),
        )
    final_checkpoint = final_run_state.snapshot(slots)
    if state["yield"] and result.stop_reason == "cancelled":
        if not await host.restage(
            reason_code=f"{_REASON_PREFIX}yield", delay_seconds=0, checkpoint=final_checkpoint,
        ):
            return {"status": "superseded", "child_id": child.id}
        return {"status": "yielded", "child_id": child.id}

    control = result.control if isinstance(result.control, dict) else {}
    if result.stop_reason == "waiting_resource":
        return await _park_or_fail(
            host, claim, child,
            control=control, final_checkpoint=final_checkpoint, scope=scope, state=state,
        )
    if result.stop_reason in {"hitl_required", "needs_hitl"}:
        return await _park_on_approval(
            host, claim, child, control=control, final_checkpoint=final_checkpoint,
            session_factory=session_factory,
        )
    finished = runtime_delegated_child_result(
        result,
        run_id=child.id,
        session_id=None,
        idempotency_key=None,
        sub=setup.sub,
        agent=setup.agent,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        tool_results=list(state.get(TOOL_OUTCOMES_KEY) or []),
        parks_when_unconfirmed=True,
        result_stored=True,
    )
    phase = (
        TaskCoordinatorPhase.CANCELLED.value if finished.status == "cancelled"
        else TaskCoordinatorPhase.FAILED.value if finished.status == "failed"
        else TaskCoordinatorPhase.COMPLETED.value
    )
    return await _settle(host, claim, ChildOutcome(
        phase=phase,
        outcome_status=finished.status,
        interrupted=finished.interrupted,
        result=finished.payload,
        checkpoint=final_checkpoint,
        usage=dict(result.usage or {}),
        ledger_complete=True,
        error_code=finished.error,
        source="loop",
    ))


async def _park_on_approval(
    host: TaskLoopHost,
    claim: DurableRunClaim,
    child: TaskAgentChildRun,
    *,
    control: dict[str, Any],
    final_checkpoint: dict[str, Any],
    session_factory: Any,
) -> dict[str, Any]:
    """An approval-gated call: park this child on its own approval card, or fail honestly.

    The request was minted by the guard under the user's own approval mode
    and rules for the guarded operation's declared effect (an automatic
    mode never stops here). Approved: the child's wake re-executes this
    exact call, whose guard spends the grant once. Rejected: the child's
    wake replays a host-written gate result ("did not run") and continues.
    An approval that cannot resume at the call that asked for it (inside a
    skill or browser sequence, or a provider-side approval) withdraws its
    card and fails the child; nothing ran, so the failure is a plain one.
    """

    approval = root_loop._approval_summary(control)
    target = root_loop._approval_park_target(control, durable_delegation=True)
    if not isinstance(target, str):
        hitl_id, envelope, pre_call = target
        pending = pre_call["pending_tool_call"]
        human = await host.park_human(
            kind=loop_hitl.APPROVAL_WAIT_KIND,
            base_checkpoint=final_checkpoint,
            runtime_checkpoint=copy.deepcopy(pre_call),
            tool_call_id=str(pending.get("id") or ""),
            tool_name=str(pending.get("name") or ""),
            summary=str(approval.get("prompt") or ""),
            hitl_id=hitl_id,
            hitl_data=envelope,
        )
        if human is not None:
            return {"status": "waiting_human", "child_id": child.id, "kind": human["kind"]}
        if not await host.is_current():
            return {"status": "stale", "child_id": child.id}
        # The request was decided or closed before this agent could wait on
        # it: nothing ran, and nothing will resume this call.
        withdrawn = await root_loop._withdraw_approval_card(
            session_factory, task_id=child.task_id, control=control,
            reason=CHILD_APPROVAL_CLOSED_BEFORE_PARK,
        )
        # The gated call itself never ran (its guard stopped it): the ledger
        # is the one from just before that call, not one that lists it as
        # unconfirmed.
        before_call = pre_call.get("tool_evidence")
        return await _settle(host, claim, _host_failure(host,
            child, code=CHILD_APPROVAL_CLOSED_BEFORE_PARK,
            message=_closed_before_park_message(approval, withdrawn=withdrawn),
            checkpoint=(
                {**final_checkpoint, "tool_evidence": before_call}
                if isinstance(before_call, dict) else final_checkpoint
            ),
            ledger_complete=True,
        ))
    # Never leave a live card that nothing can resume.
    withdrawn = await root_loop._withdraw_approval_card(
        session_factory, task_id=child.task_id, control=control, reason=target,
    )
    return await _settle(host, claim, _host_failure(host,
        child, code=target,
        message=_unresumable_approval_message(approval, withdrawn=withdrawn),
        checkpoint=final_checkpoint, ledger_complete=True,
    ))


async def _park_or_fail(
    host: TaskLoopHost,
    claim: DurableRunClaim,
    child: TaskAgentChildRun,
    *,
    control: dict[str, Any],
    final_checkpoint: dict[str, Any],
    scope: Any,
    state: dict[str, Any],
) -> dict[str, Any]:
    """A suspended child: park on its jobs or its own children, wait for capacity, or fail."""

    kind = control.get("kind")
    has_checkpoint = isinstance(control.get("checkpoint"), dict)
    if kind in waits.WAIT_KINDS and has_checkpoint:
        wait = waits.parked_wait_state(control, prior=state["resumed_wait"], now=waits.utcnow())
        if wait is not None:
            wake_at = await host.park_wait(
                wait=wait,
                parked_checkpoint={
                    **final_checkpoint,
                    RUNTIME_CHECKPOINT_KEY: copy.deepcopy(control["checkpoint"]),
                    waits.TASK_LOOP_WAIT_KEY: wait,
                },
            )
            if wake_at is None:
                return {"status": "stale", "child_id": child.id}
            return {"status": wait["kind"], "child_id": child.id, "wake_at": wake_at}
    if kind == WAITING_CHILD_WAIT_KIND and has_checkpoint:
        parked = await root_loop._park_delegation(
            host, control=control, final_checkpoint=final_checkpoint, scope=scope,
            task_id=child.task_id, reason_prefix=_REASON_PREFIX,
        )
        return {**parked, "child_id": child.id}
    if kind == "waiting_resource" and has_checkpoint:
        delay = root_loop._resource_poll_delay(control.get("poll_after_seconds"))
        wait = {
            "reason": control.get("reason"),
            "reservation_id": control.get("reservation_id"),
            "deadline_at": str(control.get("deadline_at") or "") or None,
            "poll_after_seconds": delay,
        }
        if not (wait["deadline_at"] and root_loop._deadline_passed(wait["deadline_at"])):
            if not await host.restage(
                reason_code=f"{_REASON_PREFIX}resource_wait",
                delay_seconds=delay,
                checkpoint={
                    **final_checkpoint,
                    RUNTIME_CHECKPOINT_KEY: copy.deepcopy(control["checkpoint"]),
                    "resource_wait": wait,
                },
            ):
                return {"status": "stale", "child_id": child.id}
            return {"status": "waiting_resource", "child_id": child.id}
        return await _settle(host, claim, _host_failure(host,
            child, code=CHILD_RESOURCE_WAIT_EXPIRED,
            message=root_loop._resource_wait_expired_message(wait),
            checkpoint=final_checkpoint, ledger_complete=True,
        ))
    if kind == loop_hitl.INPUT_WAIT_KIND and has_checkpoint:
        # request_input: park the exact pending call on this agent's own
        # Task input card. The answer becomes that call's result.
        parked_call = control["checkpoint"].get("pending_tool_call") or {}
        question = root_loop._question_from_payload(control.get("payload"))
        call_id = str(parked_call.get("id") or "")
        if question is not None and call_id:
            human = await host.park_human(
                kind=loop_hitl.INPUT_WAIT_KIND,
                base_checkpoint=final_checkpoint,
                runtime_checkpoint=copy.deepcopy(control["checkpoint"]),
                tool_call_id=call_id,
                tool_name=root_loop.REQUEST_INPUT_TOOL_NAME,
                summary=question["question"],
                question=question,
            )
            if human is None:
                return {"status": "stale", "child_id": child.id}
            return {"status": "waiting_human", "child_id": child.id, "kind": human["kind"]}
    if kind == waits.WAITING_TIMER_KIND or kind == waits.WAITING_JOB_KIND:
        code, message = CHILD_SUSPENSION_UNSUPPORTED, (
            f"The delegated agent paused on a wait it cannot resume ({kind}). Nothing was run."
        )
    elif kind == loop_hitl.INPUT_WAIT_KIND:
        code, message = CHILD_QUESTION_UNREADABLE, (
            "The delegated agent asked a question that could not be shown to the user "
            "(it had no readable question or call). Nothing further was run."
        )
    else:
        code, message = CHILD_SUSPENSION_UNSUPPORTED, (
            f"The delegated agent paused on a wait it cannot resume ({kind or 'unknown'}). "
            "Nothing was run."
        )
    return await _settle(host, claim, _host_failure(host,
        child, code=code, message=message, checkpoint=final_checkpoint, ledger_complete=True,
    ))


__all__ = [
    "CHILD_APPROVAL_CLOSED_BEFORE_PARK",
    "CHILD_QUESTION_UNREADABLE",
    "CHILD_REPAIR_REQUIRED",
    "RUNNING_CAP",
    "admit_task_agent_child",
    "child_claim_is_current",
    "run_task_agent_child_once",
]
