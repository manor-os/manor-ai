"""The durable host of one claimed wake of a run driven by the Task agent loop.

A run the Task agent loop drives owns its row the durable-run way
(``services.durable_run``): it holds the claim on exactly one wake, renews it
while provider I/O runs, fences every model call, CAS-checkpoints every round,
parks on a timer, a media job or a person, re-stages its own next wake, and
releases its claim when the wake ends. ``TaskLoopHost`` is those mechanics for
one run row, addressed by its ``DurableRunSpec`` and key; what one round does
and how the run reaches a terminal state stay with the engine that drives it.

Today the only such run is a Task's root ``TaskCoordinatorRun`` with engine
``agent_loop`` (``workers.task_agent_loop``).

Lock order, as admission and the terminal write: approval boundary (advisory)
-> HitlRequest -> Task -> Run -> outbox event. Every write that changes the
run row goes through ``writes`` (one run kind's durable-run calls). While the
loop runs, every such write happens inside the host's ``mutation_lock``, which
also serializes the engine's own fenced writes (``state["version"]`` is the
run's expected ``state_version``). ``record_wake_failure`` and ``close`` run
after the loop has stopped and take no lock.

Journal: every host write that commits a transition of the run (a round, a
plan revision, a park) also appends its ``task_run_journal`` row in the same
transaction, right after the run row's CAS (``services.task_run_journal``).
Writes that only move the claim or count a crash are exempt, each named with
its reason in ``JOURNAL_EXEMPT_METHODS`` (guarded in test_task_run_journal).

Patch points: the run writes are the ``writes`` object's; the Task log and
status writes of a park go through ``add_log`` / ``transition_status`` when the
engine passes them (the root run resolves them in ``workers.task_agent_loop``
at call time), else through this module's ``add_task_log`` /
``apply_task_status_transition``, also looked up at call time.
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from packages.core.ai.runtime.run_state import (
    RUN_STATE_VERSION_KEY,
    runtime_run_state_persistable,
)
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.task import Task
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
)
from packages.core.models.task_run_journal import TaskRunJournalKind
from packages.core.services import durable_run
from packages.core.services import task_agent_loop_hitl as loop_hitl
from packages.core.services import task_run_journal as journal
from packages.core.services.task_agent_loop_state import ROUND_JOURNAL_KEY
from packages.core.services.durable_run import DurableRunSpec
from packages.core.services.task_service import add_task_log
from packages.core.services.task_state_machine import apply_task_status_transition
from packages.core.workers import task_agent_loop_waits as waits
from packages.core.workers import task_run_host


SessionFactory = Callable[[], Any]
# The run kind's own "does this owner still hold an admissible claim" check.
IsCurrent = Callable[[Any], Awaitable[bool]]
# Renews ``claim`` until the event is set; a failed renewal sets it.
Heartbeat = Callable[[Any, asyncio.Event], Awaitable[None]]

RUNTIME_CHECKPOINT_KEY = loop_hitl.RUNTIME_CHECKPOINT_KEY
WAKE_FAILURES_KEY = durable_run.DEFAULT_WAKE_FAILURES_KEY

#: TaskLoopHost methods that bump the run row without a journal row, and why.
JOURNAL_EXEMPT_METHODS = {
    "close": "releases this owner's claim; the run's work did not change",
    "record_wake_failure": "a host-only crash counter; the transcript did not change",
    "park_children": "its CAS is journaled by _park_on_children (delegate) in the same transaction",
}


class DurableRunWrites:
    """One run row's durable-run writes, addressed by its spec and key.

    The owner's token and generation are passed by the host on every call. A
    run kind whose callers patch its named wrappers overrides the methods to
    go through them; the arguments and results are the same.
    """

    def __init__(self, spec: DurableRunSpec, key: str) -> None:
        self.spec = spec
        self.key = key

    async def checkpoint(
        self,
        db: Any,
        *,
        claim_token: str,
        claim_generation: str,
        expected_state_version: int,
        **fields: Any,
    ) -> int | None:
        """CAS ``checkpoint`` and/or ``phase``; None after any claim/version loss."""

        return await durable_run.checkpoint_run(
            db, self.spec,
            key=self.key,
            claim_token=claim_token,
            claim_generation=claim_generation,
            expected_state_version=expected_state_version,
            **fields,
        )

    async def stage(
        self,
        db: Any,
        *,
        reason: str,
        expected_state_version: int,
        claim_token: str,
        claim_generation: str,
        **when: Any,
    ) -> Any | None:
        """The owner stages its own next wake (``delay_seconds`` or ``available_at``)."""

        return await durable_run.stage_run_wake(
            db, self.spec,
            key=self.key,
            reason=reason,
            expected_state_version=expected_state_version,
            claim_token=claim_token,
            claim_generation=claim_generation,
            **when,
        )

    async def settle(
        self, db: Any, *, event_id: str, wake_generation: str, claim_token: str,
    ) -> bool:
        return await durable_run.settle_run_wake(
            db, self.spec,
            key=self.key,
            event_id=event_id,
            wake_generation=wake_generation,
            claim_token=claim_token,
        )

    async def release(self, db: Any, *, claim_token: str, claim_generation: str) -> bool:
        return await durable_run.release_run_claim(
            db, self.spec,
            key=self.key,
            claim_token=claim_token,
            claim_generation=claim_generation,
        )

    async def holds_claim(self, db: Any, *, claim_token: str, claim_generation: str) -> bool:
        return await durable_run.holds_run_claim(
            db, self.spec,
            key=self.key,
            claim_token=claim_token,
            claim_generation=claim_generation,
        )

    async def journal(
        self,
        db: Any,
        *,
        run_id: str,
        kind: TaskRunJournalKind,
        payload: dict[str, Any],
        round: int | None = None,
    ) -> None:
        """Journal the transition just written to this run row (same transaction)."""

        run = await db.get(self.spec.model, run_id)
        if run is None:
            raise RuntimeError(f"{self.spec.label} run {run_id} vanished inside its own CAS")
        await journal.append_run_journal(db, run=run, kind=kind, payload=payload, round=round)

    async def record_wake_failure(
        self,
        db: Any,
        *,
        claim_token: str,
        claim_generation: str,
        error: BaseException,
        failures_key: str,
    ) -> tuple[int, dict[str, Any]]:
        return await durable_run.record_run_wake_failure(
            db, self.spec,
            key=self.key,
            claim_token=claim_token,
            claim_generation=claim_generation,
            error=error,
            failures_key=failures_key,
        )


class TaskLoopHost:
    """Host mechanics of one claimed wake of one Task-loop run row.

    Built from the run kind's claim right after admission. ``start`` begins
    the heartbeat; ``close`` (always, in ``finally``) stops it and releases
    the claim. ``state`` is the engine's live state: the host keeps the run's
    expected ``state_version`` in ``state["version"]`` and the round
    bookkeeping (``phase_running``, ``last_checkpoint``, ``yield``,
    ``round_journal``) there.
    ``entity_id`` is set by the engine once it has read its Task.
    """

    def __init__(
        self,
        *,
        claim: Any,
        writes: DurableRunWrites,
        task_id: str,
        state: dict[str, Any],
        session_factory: SessionFactory,
        is_current: IsCurrent,
        heartbeat: Heartbeat,
        slice_seconds: Callable[[], float],
        label: str,
        add_log: Callable[..., Awaitable[Any]] | None = None,
        transition_status: Callable[..., Awaitable[Any]] | None = None,
        reason_prefix: str = "task_agent_loop_",
        on_close: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> None:
        self.claim = claim
        self.writes = writes
        self.task_id = task_id
        self.entity_id: str | None = None
        # Set by a delegated child's engine (``child_identity``): its run
        # parks on its own cards, asked by its own agent.
        self.child: dict[str, Any] | None = None
        # Set by an engine that claimed a running run whose owner died.
        self.took_over = False
        self.session_factory = session_factory
        self.label = label
        self.lost = asyncio.Event()
        self.mutation_lock = asyncio.Lock()
        self._state = state
        self._is_current = is_current
        self._heartbeat = heartbeat
        self._slice_seconds = slice_seconds
        self._add_log_override = add_log
        self._transition_override = transition_status
        # Prefix of this run kind's wake reasons ("task_agent_loop_yield").
        self._reason_prefix = reason_prefix
        # Runs in close()'s transaction once the claim is released.
        self._on_close = on_close
        self._heartbeat_task: asyncio.Task | None = None
        self._started = time.monotonic()

    def child_identity(
        self, *, root_run_id: str, agent_label: str | None, agent_subscription_id: str | None,
    ) -> None:
        """Mark this host's run as a delegated child run of ``root_run_id``."""

        self.child = {
            "root_run_id": root_run_id,
            "agent_label": str(agent_label or "").strip()[:100] or "Agent",
            "agent_subscription_id": agent_subscription_id,
        }

    @property
    def spec(self) -> DurableRunSpec:
        return self.writes.spec

    @property
    def key(self) -> str:
        return self.writes.key

    async def _add_log(self, *args: Any, **kwargs: Any) -> Any:
        log = self._add_log_override if self._add_log_override is not None else add_task_log
        return await log(*args, **kwargs)

    async def _transition(self, *args: Any, **kwargs: Any) -> Any:
        transition = (
            self._transition_override
            if self._transition_override is not None
            else apply_task_status_transition
        )
        return await transition(*args, **kwargs)

    def _owner(self) -> dict[str, str]:
        return {
            "claim_token": self.claim.claim_token,
            "claim_generation": self.claim.wake_generation,
        }

    async def _journal(
        self, db: Any, kind: TaskRunJournalKind, payload: dict[str, Any], *, round: int | None = None,
    ) -> None:
        await self.writes.journal(db, run_id=self.claim.run_id, kind=kind, payload=payload, round=round)

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Renew the claim in the background; the wake's time slice starts now."""

        self._heartbeat_task = asyncio.create_task(self._heartbeat(self.claim, self.lost))
        self._started = time.monotonic()

    def cancel_requested(self) -> bool:
        """The loop's cancel checker: the claim was lost or the slice is spent."""

        return self.lost.is_set() or bool(self._state["yield"])

    async def is_current(self) -> bool:
        return await self._is_current(self.claim)

    async def before_model(self, *_args: Any) -> None:
        """Refuse provider I/O once this owner's claim is no longer current."""

        await task_run_host.fence_before_model(
            self.claim, self.lost, self._is_current, label=self.label,
        )

    async def holds_claim(self, db: Any) -> bool:
        """Lock the run and confirm this owner still holds its exact live claim.

        Callers that also write the Task lock it first (Task -> Run).
        """

        return await self.writes.holds_claim(db, **self._owner())

    async def close(self) -> None:
        """Stop the heartbeat and release this owner's claim (never a successor's)."""

        self.lost.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        async with self.session_factory() as db:
            await self.writes.release(db, **self._owner())
            if self._on_close is not None:
                await self._on_close(db)
            await db.commit()

    # ── rounds ──────────────────────────────────────────────────────────

    def observe_tool_end(self, name: str, result: Any, arguments: Any = None) -> None:
        """Keep a redacted record of one finished call for this round's journal row."""

        buffer = self._state.get(ROUND_JOURNAL_KEY)
        if not isinstance(buffer, list):
            buffer = []
            self._state[ROUND_JOURNAL_KEY] = buffer
        buffer.append(journal.observed_tool_call(name, result, arguments))

    async def checkpoint(
        self,
        rounds: int,
        messages: list[dict[str, Any]],
        usage: dict[str, Any],
        tool_calls: list[str],
        runtime_state: dict[str, Any] | None = None,
    ) -> None:
        """The strict round observer: CAS the round's run state or stop the loop."""

        async with self.mutation_lock:
            if self.lost.is_set():
                raise RuntimeError(f"{self.label} claim heartbeat was lost")
            # The harness hands over the round's RuntimeRunState snapshot
            # with this loop's slots already captured; persist it without
            # the ephemeral loop signals.
            if not isinstance(runtime_state, dict) or RUN_STATE_VERSION_KEY not in runtime_state:
                raise RuntimeError(f"{self.label} round end came without the harness run state")
            payload = runtime_run_state_persistable(runtime_state)
            previous = self._state["last_checkpoint"] or {}
            entry = journal.round_payload(
                rounds=rounds,
                messages=messages,
                observed=list(self._state.get(ROUND_JOURNAL_KEY) or []),
                run_state=payload,
                previous_usage=previous.get("usage"),
                previous_messages=(
                    previous["messages"] if isinstance(previous.get("messages"), list) else None
                ),
            )
            async with self.session_factory() as db:
                version = await self.writes.checkpoint(
                    db,
                    **self._owner(),
                    expected_state_version=self._state["version"],
                    checkpoint=payload,
                    **(
                        {}
                        if self._state["phase_running"]
                        else {"phase": TaskCoordinatorPhase.RUNNING.value}
                    ),
                )
                if version is None:
                    self.lost.set()
                    raise RuntimeError(f"{self.label} round checkpoint lost its claim")
                await self._journal(db, TaskRunJournalKind.ROUND, entry, round=entry["round"])
                await db.commit()
                self._state["version"] = version
            self._state[ROUND_JOURNAL_KEY] = []
            self._state["phase_running"] = True
            self._state["last_checkpoint"] = payload
            if time.monotonic() - self._started >= self._slice_seconds():
                # Yield at this round boundary; the cancel checker stops
                # the loop before its next model call.
                self._state["yield"] = True

    async def record_plan_revision(
        self, plan: dict[str, Any], *, log: Callable[[Any], Awaitable[Any]],
    ) -> bool:
        """Journal a plan revision with its Task log line, under this exact claim.

        One transaction under the Task -> Run locks: the run's version is
        bumped (the plan itself is persisted with the round's checkpoint),
        the ``plan_revision`` row is appended and ``log(db)`` writes the Task
        log. False (nothing written) once this owner was superseded; the
        round checkpoint then fails on the same fence.
        """

        async with self.mutation_lock:
            async with self.session_factory() as db:
                await db.execute(select(Task.id).where(Task.id == self.task_id).with_for_update())
                version = await self.writes.checkpoint(
                    db, **self._owner(), expected_state_version=self._state["version"],
                )
                if version is None:
                    await db.rollback()
                    return False
                await self._journal(db, TaskRunJournalKind.PLAN_REVISION, journal.plan_revision_payload(plan))
                await log(db)
                await db.commit()
                self._state["version"] = version
                return True

    async def restage(
        self, *, reason_code: str, delay_seconds: float, checkpoint: dict[str, Any] | None,
    ) -> bool:
        """Owner re-stage: optional checkpoint, new wake and release in one commit.

        Journaled as a ``park`` whose wait kind is the reason without its
        ``task_agent_loop_`` prefix (``yield``, ``resource_wait``,
        ``completion_missing``).
        """

        async with self.mutation_lock:
            async with self.session_factory() as db:
                if checkpoint is not None:
                    version = await self.writes.checkpoint(
                        db,
                        **self._owner(),
                        expected_state_version=self._state["version"],
                        checkpoint=checkpoint,
                    )
                    if version is None:
                        await db.rollback()
                        return False
                    self._state["version"] = version
                try:
                    wake = await self.writes.stage(
                        db,
                        reason=reason_code,
                        delay_seconds=delay_seconds,
                        expected_state_version=self._state["version"],
                        **self._owner(),
                    )
                except ValueError:
                    await db.rollback()
                    return False
                if wake is None:
                    await db.rollback()
                    return False
                resource_wait = (checkpoint or {}).get("resource_wait")
                await self._journal(db, TaskRunJournalKind.PARK, journal.park_payload({
                    "kind": reason_code.removeprefix(self._reason_prefix),
                    "wake_at": waits.iso(wake.available_at),
                    "reason": resource_wait.get("reason") if isinstance(resource_wait, dict) else None,
                }))
                # The successor wake can be delivered at once; do not make it
                # wait for this owner's claim to lapse.
                await self.writes.release(db, **self._owner())
                await db.commit()
                return True

    async def record_wake_failure(self, exc: BaseException) -> tuple[int, dict[str, Any]]:
        """Count a crash against this exact wake; 0 when this owner was superseded.

        The counter lives in the checkpoint and is keyed by wake generation, so a
        successful round (which rewrites the checkpoint) or a new wake resets it.
        """

        async with self.session_factory() as db:
            count, checkpoint = await self.writes.record_wake_failure(
                db, **self._owner(), error=exc, failures_key=WAKE_FAILURES_KEY,
            )
            if not count:
                await db.rollback()
                return 0, {}
            await db.commit()
            return count, checkpoint

    # ── parking ─────────────────────────────────────────────────────────

    async def park_wait(self, *, wait: dict[str, Any], parked_checkpoint: dict[str, Any]) -> str | None:
        """Park the pending wait call: checkpoint, phase, delayed wake, release.

        One transaction under the Task -> Run locks. The Task stays
        IN_PROGRESS (every wake gate requires it) and the wake is not settled:
        staging replaces the row this owner claimed. Returns the wake time.
        """

        kind = wait["kind"]
        phase = (
            TaskCoordinatorPhase.WAITING_TIMER.value
            if kind == waits.WAITING_TIMER_KIND
            else TaskCoordinatorPhase.WAITING_JOB.value
        )
        async with self.mutation_lock:
            async with self.session_factory() as db:
                await db.execute(select(Task.id).where(Task.id == self.task_id).with_for_update())
                version = await self.writes.checkpoint(
                    db,
                    **self._owner(),
                    expected_state_version=self._state["version"],
                    checkpoint=parked_checkpoint,
                    phase=phase,
                )
                if version is None:
                    await db.rollback()
                    return None
                self._state["version"] = version
                await self._journal(db, TaskRunJournalKind.PARK, journal.park_payload(wait))
                now = waits.utcnow()
                if kind == waits.WAITING_TIMER_KIND:
                    available_at = waits.parse_time(wait["wake_at"])
                else:
                    # Read under the Run lock: a job that finished just before
                    # this park (its hook saw no WAITING_JOB run) wakes at once.
                    deadline = waits.parse_time(wait["deadline_at"]) or now
                    settled = await waits.media_jobs_settled(
                        db, entity_id=self.entity_id, job_ids=wait["job_ids"],
                    )
                    available_at = (
                        now
                        if settled or deadline <= now
                        else min(deadline, now + timedelta(seconds=waits.JOB_POLL_MAX_SECONDS))
                    )
                try:
                    wake = await self.writes.stage(
                        db,
                        reason=f"{self._reason_prefix}{kind}",
                        available_at=available_at,
                        expected_state_version=self._state["version"],
                        **self._owner(),
                    )
                except ValueError:
                    wake = None
                if wake is None:
                    await db.rollback()
                    return None
                resumed = self._state["resumed_wait"]
                if resumed is None or resumed.get("tool_call_id") != wait["tool_call_id"]:
                    # Only a new wait is logged; an early wake re-parks quietly.
                    await self._add_log(
                        db, self.task_id, TaskLogType.AI_AGENT_TURN, waits.wait_log_line(wait),
                        actor=TaskActor.SYSTEM,
                        metadata={
                            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
                            waits.TASK_LOOP_WAIT_KEY: wait,
                        },
                    )
                await self.writes.release(db, **self._owner())
                await db.commit()
                return waits.iso(wake.available_at)

    async def park_human(
        self,
        *,
        kind: str,
        base_checkpoint: dict[str, Any],
        runtime_checkpoint: dict[str, Any] | None,
        tool_call_id: str,
        tool_name: str,
        summary: str,
        question: dict[str, Any] | None = None,
        hitl_id: str | None = None,
        hitl_data: dict[str, Any] | None = None,
        reuse_wait: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Park on a person: the existing card, WAITING_HUMAN, the Task's status.

        One transaction under the approval-boundary -> HitlRequest -> Task ->
        root Run -> Run locks. The wake is settled: only the person's decision
        (or a Task resume) wakes the run again. ``reuse_wait`` re-parks a run
        woken while its card is still open, without a new card or log line.

        The Task's root run puts its Task on WAITING_ON_CUSTOMER at once. A
        delegated child run asks on its own card ("Asked by {agent}") and
        signs its approval park as a child's (v2 tag); its Task waits on the
        person only once nothing in the run tree can move forward
        (``sync_task_human_wait_status``, after this run released its claim).

        Returns the stored wait, or None when this owner was superseded or
        the request can no longer be parked on.
        """

        from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
        from packages.core.models.hitl_request import HitlRequest

        task_id = self.task_id
        child = self.child
        async with self.mutation_lock:
            async with self.session_factory() as db:
                request = None
                if kind == loop_hitl.APPROVAL_WAIT_KIND:
                    from packages.core.services.task_runtime_approvals import (
                        lock_task_runtime_approval_boundary,
                    )

                    await lock_task_runtime_approval_boundary(db, task_id=task_id)
                    request = (await db.execute(
                        select(HitlRequest).where(
                            HitlRequest.id == (reuse_wait or {}).get("hitl_request_id", hitl_id),
                            HitlRequest.entity_id == self.entity_id,
                            HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL.value,
                            HitlRequest.origin_task_id == task_id,
                        ).with_for_update().execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if request is None or request.status != ApprovalStatus.PENDING.value:
                        await db.rollback()
                        return None
                task_row = (await db.execute(
                    select(Task).where(Task.id == task_id).with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one()
                if task_row.status != TaskStatus.IN_PROGRESS.value or (
                    request is not None and request.origin_conversation_id != task_row.conversation_id
                ):
                    await db.rollback()
                    return None
                if child is not None:
                    from packages.core.models.task_coordinator import TaskCoordinatorRun

                    # Task -> root Run -> this child, as every child write.
                    await db.execute(
                        select(TaskCoordinatorRun.id).where(
                            TaskCoordinatorRun.id == child["root_run_id"],
                            TaskCoordinatorRun.task_id == task_id,
                        ).with_for_update()
                    )
                digest = (
                    loop_hitl.runtime_checkpoint_digest(runtime_checkpoint)
                    if runtime_checkpoint is not None
                    else None
                )
                agent = child["agent_label"] if child is not None else None
                if reuse_wait is not None:
                    wait = reuse_wait
                else:
                    if request is not None:
                        request_id = request.id
                    else:
                        from packages.core.services.task_chat_hitl import ensure_task_input_hitl

                        card = await ensure_task_input_hitl(
                            db, task_row, question=question or {},
                            **(
                                {
                                    # One card per child run and call: two
                                    # agents asking at once get two cards.
                                    "dedup_key": f"task-input:{self.claim.run_id}:{tool_call_id}",
                                    "asked_by": agent,
                                    "run_id": self.claim.run_id,
                                }
                                if child is not None else {}
                            ),
                        )
                        action = card.pending_action if card is not None and isinstance(card.pending_action, dict) else {}
                        request_id = str(action.get("approval_request_id") or "")
                        if not request_id:
                            await db.rollback()
                            return None
                    wait = loop_hitl.human_wait(
                        kind=kind, tool_call_id=tool_call_id, tool_name=tool_name,
                        hitl_request_id=request_id, checkpoint_digest=digest, summary=summary,
                        run_kind=loop_hitl.RUN_KIND_CHILD if child is not None else None,
                        agent_label=agent,
                    )
                parked_checkpoint = {
                    key: value for key, value in base_checkpoint.items()
                    if key not in {RUNTIME_CHECKPOINT_KEY, loop_hitl.HUMAN_NOTE_KEY, WAKE_FAILURES_KEY}
                }
                parked_checkpoint[waits.TASK_LOOP_WAIT_KEY] = wait
                if runtime_checkpoint is not None:
                    parked_checkpoint[RUNTIME_CHECKPOINT_KEY] = runtime_checkpoint
                version = await self.writes.checkpoint(
                    db,
                    **self._owner(),
                    expected_state_version=self._state["version"],
                    checkpoint=parked_checkpoint,
                    phase=TaskCoordinatorPhase.WAITING_HUMAN.value,
                )
                if version is None:
                    await db.rollback()
                    return None
                self._state["version"] = version
                await self._journal(
                    db, TaskRunJournalKind.PARK,
                    journal.park_payload(wait, **({"reparked": True} if reuse_wait is not None else {})),
                )
                if reuse_wait is None and request is not None:
                    from packages.core.services.task_runtime_approvals import (
                        project_task_runtime_approval_card,
                    )

                    request.context = {
                        **(request.context or {}),
                        loop_hitl.TASK_AGENT_LOOP_HITL_CONTEXT_KEY: loop_hitl.approval_tag(
                            run_id=self.claim.run_id, task_id=task_id, tool_call_id=tool_call_id,
                            hitl_request_id=request.id, checkpoint_digest=digest or "",
                            **(
                                {"run_kind": loop_hitl.RUN_KIND_CHILD, "root_run_id": child["root_run_id"]}
                                if child is not None else {}
                            ),
                        ),
                    }
                    await project_task_runtime_approval_card(
                        db, task=task_row, hitl_data=hitl_data or {}, hitl_id=request.id,
                        **(
                            {
                                "requested_by_agent": agent,
                                "author_subscription_id": child["agent_subscription_id"],
                                "run_id": self.claim.run_id,
                            }
                            if child is not None else {}
                        ),
                    )
                if child is None:
                    if question is not None and reuse_wait is None:
                        from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY

                        # What the Task page renders while the Task waits on it.
                        task_row.details = {
                            **(task_row.details or {}),
                            TASK_INPUT_QUESTION_KEY: question,
                            "_hitl_response": None,
                        }
                    await self._transition(
                        task_row, TaskStatus.WAITING_ON_CUSTOMER.value, db=db, actor_kind="system",
                    )
                if reuse_wait is None:
                    from packages.core.services.task_input_resume import task_input_log_meta

                    metadata = {
                        "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
                        waits.TASK_LOOP_WAIT_KEY: wait,
                        **(
                            {"run_id": self.claim.run_id, "run_kind": loop_hitl.RUN_KIND_CHILD, "agent_label": agent}
                            if child is not None else {}
                        ),
                    }
                    if question is not None:
                        await self._add_log(
                            db, task_id, TaskLogType.AI_HITL_REQUESTED,
                            (
                                f"{agent} is waiting for an answer: {question.get('question')}"
                                if child is not None
                                else f"Waiting for an answer: {question.get('question')}"
                            ),
                            actor=TaskActor.SYSTEM,
                            metadata={**task_input_log_meta(question), **metadata},
                        )
                    else:
                        await self._add_log(
                            db, task_id, TaskLogType.AI_AGENT_TURN,
                            (
                                f"{agent} is waiting for approval of {tool_name}: {summary}"
                                if child is not None
                                else f"Waiting for approval of {tool_name}: {summary}"
                            ).rstrip(": "),
                            actor=TaskActor.SYSTEM, metadata=metadata,
                        )
                    from packages.core.services import event_emitter

                    # The person is told once, when this wait is first parked: the in-app
                    # notification, mobile push and external sinks all follow this event,
                    # staged in the park's transaction. A re-park on the same wait does not repeat it.
                    hitl_request_id = wait["hitl_request_id"]
                    await event_emitter.emit_in_session(
                        db,
                        self.entity_id,
                        "task.hitl_requested",
                        source="workers.task_agent_loop",
                        payload={
                            "task_id": task_id,
                            "hitl_request_id": hitl_request_id,
                            "prompt": str((question or {}).get("question") or summary or "")[:1000] or None,
                            "task_status": task_row.status,
                            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
                            "idempotency_key": f"task-hitl:{hitl_request_id}",
                            **({"agent_label": agent} if child is not None else {}),
                        },
                        workspace_id=task_row.workspace_id,
                        deliver_after_commit=True,
                    )
                if not await self.writes.settle(
                    db,
                    event_id=self.claim.event_id,
                    wake_generation=self.claim.wake_generation,
                    claim_token=self.claim.claim_token,
                ):
                    await db.rollback()
                    return None
                await self.writes.release(db, **self._owner())
                if child is not None:
                    # With this claim released: does the whole tree now wait
                    # on a person? (Q3: only then does the Task.)
                    await loop_hitl.sync_task_human_wait_status(db, task=task_row)
                await db.commit()
                return wait

    # ── delegation (services.task_agent_delegation) ─────────────────────

    async def _lock_delegation_scope(self, db: Any, scope: Any) -> Task | None:
        """Task, then the root run when this run is a child (Task -> root -> self)."""

        from packages.core.models.task_coordinator import TaskCoordinatorRun

        task_row = (await db.execute(
            select(Task).where(Task.id == self.task_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task_row is None or task_row.status != TaskStatus.IN_PROGRESS.value:
            return None
        if scope.parent_child_id:
            await db.execute(
                select(TaskCoordinatorRun.id).where(
                    TaskCoordinatorRun.id == scope.root_run_id,
                    TaskCoordinatorRun.task_id == self.task_id,
                ).with_for_update()
            )
        return task_row

    async def _park_on_children(
        self,
        db: Any,
        *,
        scope: Any,
        task_row: Task,
        run: Any,
        rows: list[Any],
        wait: dict[str, Any],
        parked_checkpoint: dict[str, Any],
        reparked: bool,
    ) -> bool:
        """The rest of a waiting_child park, after this owner's CAS: wakes, ACK, release."""

        from packages.core.services import task_agent_delegation as delegation
        from packages.core.services.task_agent_child_settlement import settle_waiting_batch

        run.checkpoint = {**parked_checkpoint, waits.TASK_LOOP_WAIT_KEY: wait}
        run.phase = TaskCoordinatorPhase.WAITING_CHILD.value
        now = waits.utcnow()
        await delegation.stage_children(db, rows, now=now)
        if reparked and scope.depth == 0:
            # A resumed Task: deeper children get their wakes back too.
            await delegation.restage_descendants(
                db, task=task_row, root_run_id=scope.root_run_id, now=now,
            )
        # Only the children wake this run again (as a person wakes a human
        # wait): its own wake is ACKed, not re-staged.
        if not await self.writes.settle(
            db,
            event_id=self.claim.event_id,
            wake_generation=self.claim.wake_generation,
            claim_token=self.claim.claim_token,
        ):
            return False
        await self._journal(
            db, TaskRunJournalKind.DELEGATE,
            journal.delegate_payload(tool_call_id=wait["tool_call_id"], children=rows, reparked=reparked),
        )
        if not reparked:
            names = ", ".join(str(row.agent_label or row.service_key or "Agent") for row in rows)
            await self._add_log(
                db, self.task_id, TaskLogType.AI_AGENT_TURN,
                f"Delegated {len(rows)} task(s): {names}",
                actor=TaskActor.SYSTEM,
                metadata={
                    "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
                    waits.TASK_LOOP_WAIT_KEY: wait,
                },
            )
        await self.writes.release(db, **self._owner())
        # Children that all ended already (a replayed park) settle at once.
        await settle_waiting_batch(db, task=task_row, parent=run, children=rows)
        if reparked:
            # A resumed Task whose delegated agents all still wait on a
            # person waits on that person again.
            await loop_hitl.sync_task_human_wait_status(db, task=task_row)
        return True

    async def park_children(
        self,
        *,
        scope: Any,
        call_id: str,
        specs: list[dict[str, Any]],
        parked_checkpoint: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Park the pending ``delegate_to_agent`` call on one new child run per task.

        One transaction under the Task -> root Run -> this run -> child rows
        -> outbox locks: the CAS to ``waiting_child``, the child rows
        (idempotent per call item), each child's wake, the ACK of this run's
        own wake and the release. The children are created here and nowhere
        else, so a crash before this commit leaves no orphan.

        Returns ``{"status": "waiting_child", "child_ids": [...]}``,
        ``{"status": "busy", "error": {...}}`` when another live run of one
        of these agents won a race (nothing was written; the caller replays
        the refusal to the model), or None when this owner was superseded.
        """

        from sqlalchemy.exc import IntegrityError

        from packages.core.services import task_agent_delegation as delegation
        from packages.core.services.task_execution import task_execution_intent_generation

        async with self.mutation_lock:
            async with self.session_factory() as db:
                task_row = await self._lock_delegation_scope(db, scope)
                if task_row is None:
                    await db.rollback()
                    return None
                version = await self.writes.checkpoint(
                    db, **self._owner(), expected_state_version=self._state["version"],
                )
                if version is None:
                    await db.rollback()
                    return None
                run = await db.get(self.spec.model, self.claim.run_id)
                lifecycle = task_execution_intent_generation(task_row)
                if run is None or run.lifecycle_generation != lifecycle:
                    await db.rollback()
                    return None
                try:
                    async with db.begin_nested():
                        await delegation.insert_child_rows(
                            db, scope=scope, call_id=call_id, specs=specs,
                            lifecycle_generation=lifecycle,
                        )
                except IntegrityError:
                    await db.rollback()
                    return {"status": "busy", "error": await delegation.busy_refusal(
                        self.session_factory, scope=scope, specs=specs,
                    )}
                rows = await delegation.lock_call_children(db, parent_ref=scope.run_id, call_id=call_id)
                if not delegation.children_match_specs(rows, specs):
                    await db.rollback()
                    raise RuntimeError(f"Delegation {call_id} does not match the child runs it made")
                wait = delegation.waiting_child_wait(call_id=call_id, child_ids=[row.id for row in rows])
                if not await self._park_on_children(
                    db, scope=scope, task_row=task_row, run=run, rows=rows, wait=wait,
                    parked_checkpoint=parked_checkpoint, reparked=False,
                ):
                    await db.rollback()
                    return None
                await db.commit()
                self._state["version"] = version
                return {"status": "waiting_child", "child_ids": list(wait["child_ids"])}

    async def resume_children_wait(
        self,
        *,
        scope: Any,
        checkpoint: dict[str, Any],
        wait: dict[str, Any],
    ) -> dict[str, Any] | None:
        """A wake that finds this run still waiting on its children: never a model round.

        One transaction under the Task -> root Run -> this run -> child rows
        locks. Every child ended: the batch settles into this run's parked
        call right here and the run continues (``{"status": "continue",
        "checkpoint": ...}``). The wait no longer matches its children (rows
        missing, moved or on another lifecycle): the call gets an honest
        unavailable result, its open children stop, and the run continues.
        Otherwise the run parks again on the same children, which get their
        wakes back (``{"status": "waiting_child"}``). None when superseded.
        """

        from packages.core.services import task_agent_child_settlement as settlement
        from packages.core.services import task_agent_delegation as delegation
        from packages.core.services.task_execution import task_execution_intent_generation

        call_id = str(wait.get("tool_call_id") or "")
        async with self.mutation_lock:
            async with self.session_factory() as db:
                task_row = await self._lock_delegation_scope(db, scope)
                if task_row is None:
                    await db.rollback()
                    return None
                version = await self.writes.checkpoint(
                    db, **self._owner(), expected_state_version=self._state["version"],
                )
                if version is None:
                    await db.rollback()
                    return None
                run = await db.get(self.spec.model, self.claim.run_id)
                lifecycle = task_execution_intent_generation(task_row)
                if run is None or run.lifecycle_generation != lifecycle:
                    await db.rollback()
                    return None
                rows = await delegation.lock_call_children(db, parent_ref=scope.run_id, call_id=call_id)
                state = delegation.resumed_batch_state(
                    checkpoint, wait, rows, lifecycle_generation=lifecycle,
                )
                if state == "running":
                    parked = {
                        key: value for key, value in checkpoint.items()
                        if key not in {waits.TASK_LOOP_WAIT_KEY, WAKE_FAILURES_KEY}
                    }
                    if not await self._park_on_children(
                        db, scope=scope, task_row=task_row, run=run, rows=rows, wait=wait,
                        parked_checkpoint=parked, reparked=True,
                    ):
                        await db.rollback()
                        return None
                    await db.commit()
                    self._state["version"] = version
                    return {"status": "waiting_child", "child_ids": [row.id for row in rows]}
                settled = settlement.resolve_own_batch(
                    checkpoint, wait, rows, malformed=state == "malformed",
                )
                if settled is None:
                    await db.rollback()
                    raise RuntimeError(f"Run {self.claim.run_id} waits on a delegation it never parked")
                if state == "malformed":
                    await settlement.cancel_call_children(db, rows, error_code="agent_child_wait_malformed")
                run.checkpoint = settled
                run.phase = TaskCoordinatorPhase.RUNNING.value
                await self._journal(
                    db, TaskRunJournalKind.BATCH_SETTLED,
                    journal.batch_settled_payload(
                        tool_call_id=call_id,
                        resolution=settled[waits.TASK_LOOP_WAIT_KEY]["resolution"],
                        children=rows,
                    ),
                )
                await db.commit()
                self._state["version"] = version
            if state == "malformed":
                # The stopped children's open cards close after that commit
                # (the approval-boundary lock order forbids it inside).
                await self.close_ended_child_cards()
            return {"status": "continue", "checkpoint": settled}

    async def close_ended_child_cards(self) -> int:
        """Best effort, own transaction: expire the cards of this Task's ended children.

        The recovery sweep retries anything left open.
        """

        try:
            async with self.session_factory() as db:
                closed = await loop_hitl.close_ended_child_cards(db, task_id=self.task_id)
                await db.commit()
                return closed
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "%s cards of ended delegated runs could not be closed (task %s)", self.label, self.task_id,
            )
            return 0

    async def human_wait_still_open(self, wait: dict[str, Any]) -> bool:
        """Whether the card a woken human wait parked on is still undecided."""

        from packages.core.constants.approvals import ApprovalStatus
        from packages.core.models.hitl_request import HitlRequest

        async with self.session_factory() as db:
            status = (await db.execute(
                select(HitlRequest.status).where(
                    HitlRequest.id == wait.get("hitl_request_id"),
                    HitlRequest.origin_task_id == self.task_id,
                )
            )).scalar_one_or_none()
            await db.rollback()
        return status == ApprovalStatus.PENDING.value


__all__ = ["JOURNAL_EXEMPT_METHODS", "ROUND_JOURNAL_KEY", "DurableRunWrites", "TaskLoopHost"]
