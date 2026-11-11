"""Scheduled Agent waits are suspensions, not terminal run outcomes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.services.task_service import create_task
from packages.core.tasks import ai_tasks


def test_run_agent_task_does_not_handoff_waiting_result_to_settlement(
    monkeypatch,
) -> None:
    import packages.core.database as database

    waiting_result = {
        "task_id": "task-waiting",
        "status": "waiting_on_customer",
        "turns_used": 1,
    }

    def run_waiting(awaitable):
        awaitable.close()
        return True, waiting_result

    settle = Mock()
    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", run_waiting)
    monkeypatch.setattr(ai_tasks, "_update_job_run_status", settle)

    result = ai_tasks.run_agent_task.run(
        "task-waiting",
        "agent-1",
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
    )

    assert result == waiting_result
    settle.assert_not_called()


@pytest.mark.parametrize("task_ended", [True, False])
def test_terminal_delivery_settles_even_when_the_execution_claim_is_held(
    monkeypatch, task_ended,
) -> None:
    """A person cancels a BLOCKED Task while its execution lease is still held."""
    import packages.core.database as database

    cancelled = {"task_id": "task-blocked", "status": "cancelled", "response": "Cancelled by a person."}

    def run(awaitable):
        name = awaitable.cr_code.co_name
        awaitable.close()
        if name == "_run_scheduled_agent_task_if_open":
            return False, None  # claim denied: the lease is not released yet
        assert name == "_terminal_task_result"
        return cancelled if task_ended else None

    settle = Mock()
    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", run)
    monkeypatch.setattr(ai_tasks, "_update_job_run_status", settle)

    result = ai_tasks.run_agent_task.run(
        "task-blocked", "agent-1",
        scheduled_run_id="run-blocked", scheduled_job_id="job-blocked", claim_recheck=True,
    )

    if task_ended:
        settle.assert_called_once()
        assert settle.call_args.args[2] == cancelled
        assert settle.call_args.kwargs["scheduled_run_id"] == "run-blocked"
        assert result["status"] == "cancelled"
    else:
        # A Task still running keeps its lease owner in charge.
        settle.assert_not_called()
        assert result["duplicate_suppressed"] is True


def test_sync_settlement_boundary_ignores_waiting_result(monkeypatch) -> None:
    async_settle = AsyncMock()
    defer = AsyncMock()
    monkeypatch.setattr(ai_tasks, "_update_job_run_status_async", async_settle)
    monkeypatch.setattr(ai_tasks, "_defer_scheduled_agent_settlement", defer)

    ai_tasks._update_job_run_status(
        object(),
        "task-waiting",
        {"status": "waiting_on_customer"},
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
    )

    async_settle.assert_not_called()
    defer.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_settlement_ignores_waiting_result(monkeypatch) -> None:
    persist = AsyncMock(return_value=True)
    enqueue = Mock()
    monkeypatch.setattr(ai_tasks, "_persist_scheduled_run_settlement", persist)
    monkeypatch.setattr(ai_tasks, "_enqueue_scheduled_run_settlement", enqueue)

    await ai_tasks._defer_scheduled_agent_settlement(
        task_id="task-waiting",
        run_id="run-waiting",
        job_id_str="job-waiting",
        result={"status": "waiting_on_customer"},
    )

    persist.assert_not_awaited()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_waiting_result_keeps_run_and_job_open_and_skips_initiative_settle(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import agent_initiative_decision

    entity_id = generate_ulid()
    owner_id = generate_ulid()
    subscription_id = generate_ulid()
    initiative_id = generate_ulid()
    initiative_run_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"waiting-agent:{generate_ulid()}",
        entity_id=entity_id,
        name="Waiting scheduled agent",
        execution_type="agent_initiative",
        enabled=True,
        last_status="error",
        consecutive_errors=2,
    )
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Waiting scheduled agent",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
        creator_id=owner_id,
        owner_subscription_id=subscription_id,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
        result={
            "scheduled_execution_state": "admitted",
            "sentinel": "preserve-me",
        },
    )
    task.status = "waiting_on_customer"
    task.details = {
        **task.details,
        "scheduled_run_id": run.id,
        "agent_initiative": {
            "version": "1",
            "initiative_id": initiative_id,
            "initiative_run_id": initiative_run_id,
            "initiative_revision": 1,
            "agent_subscription_id": subscription_id,
            "owner_user_id": owner_id,
        },
    }
    db_session.add_all([job, run])
    await db_session.commit()

    initiative_settle = AsyncMock()
    monkeypatch.setattr(
        agent_initiative_decision,
        "settle_initiative_task",
        initiative_settle,
    )

    @asynccontextmanager
    async def session_context():
        yield db_session

    await ai_tasks._update_job_run_status_async(
        session_context,
        task.id,
        {
            "status": "waiting_on_customer",
            "human_gate": {"kind": "approval_required"},
        },
        scheduled_run_id=run.id,
        scheduled_job_id=job.job_id,
    )

    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "running"
    assert run.completed_at is None
    assert run.result == {
        "scheduled_execution_state": "admitted",
        "sentinel": "preserve-me",
    }
    assert job.last_status == "error"
    assert job.consecutive_errors == 2
    assert job.enabled is True
    initiative_settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_result_maps_to_cancelled_without_counting_an_error(
    db_session,
) -> None:
    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"cancelled-agent:{generate_ulid()}",
        entity_id=entity_id,
        name="Cancelled scheduled agent",
        execution_type="agent",
        enabled=True,
        last_status="error",
        consecutive_errors=2,
    )
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Cancelled scheduled agent",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.status = "cancelled"
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add_all([job, run])
    await db_session.commit()

    @asynccontextmanager
    async def session_context():
        yield db_session

    await ai_tasks._update_job_run_status_async(
        session_context,
        task.id,
        {"status": "cancelled", "reason": "approval rejected"},
        scheduled_run_id=run.id,
        scheduled_job_id=job.job_id,
    )

    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "cancelled"
    assert run.error is None
    assert run.completed_at is not None
    assert job.last_status == "cancelled"
    assert job.consecutive_errors == 0
    assert job.enabled is True


@pytest.mark.asyncio
async def test_deferred_cancelled_result_uses_cancelled_outcome(monkeypatch) -> None:
    settlements = []

    async def persist(*, run_id, job_id_str, settlement):
        settlements.append((run_id, job_id_str, settlement))
        return True

    monkeypatch.setattr(ai_tasks, "_persist_scheduled_run_settlement", persist)
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_run_settlement",
        lambda **_kwargs: None,
    )

    await ai_tasks._defer_scheduled_agent_settlement(
        task_id="task-cancelled",
        run_id="run-cancelled",
        job_id_str="job-cancelled",
        result={"status": "cancelled", "reason": "approval rejected"},
    )

    assert len(settlements) == 1
    settlement = settlements[0][2]
    assert settlement.outcome.status.value == "cancelled"
    assert settlement.outcome.error is None


@pytest.mark.asyncio
async def test_scheduler_recognizes_only_exact_waiting_agent_task_suspension() -> None:
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    task = SimpleNamespace(
        status="waiting_on_customer",
        details={
            "scheduled_run_id": "run-waiting",
            "scheduled_job_id": "job-waiting",
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return task

    class Session:
        async def execute(self, _statement):
            return Result()

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-waiting", "agent-1"],
        kwargs={
            "scheduled_run_id": "run-waiting",
            "scheduled_job_id": "job-waiting",
        },
    )
    run = SimpleNamespace(
        id="run-waiting",
        job_id="job-waiting",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
            "scheduled_child_kind": ScheduledDispatchKind.AGENT_TASK.value,
            "scheduled_child_id": "task-waiting",
        },
    )
    recovery = {
        "action": ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION.value,
        "dispatch": dispatch,
    }

    assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
        Session(),
        run,
        recovery,
    ) is True

    task.details["scheduled_run_id"] = "different-run"
    assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
        Session(),
        run,
        recovery,
    ) is False

    task.details["scheduled_run_id"] = run.id
    task.status = "pending"
    assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
        Session(),
        run,
        recovery,
    ) is False


@pytest.mark.asyncio
async def test_scheduler_sweep_defers_waiting_agent_without_republishing(
    monkeypatch,
) -> None:
    from packages.core.constants.execution import (
        SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import scheduler_tasks

    now = datetime.now(timezone.utc)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-waiting", "agent-1"],
        kwargs={
            "scheduled_run_id": "run-waiting",
            "scheduled_job_id": "job-waiting",
        },
    )
    run = SimpleNamespace(
        id="run-waiting",
        job_id="job-waiting",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
            "scheduled_child_kind": ScheduledDispatchKind.AGENT_TASK.value,
            "scheduled_child_id": "task-waiting",
        },
    )
    job = SimpleNamespace(job_id=run.job_id)

    class Scalars:
        def all(self):
            return [run]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def lock_candidate(_db, _candidate):
        return job, run

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        lock_candidate,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_scheduled_agent_execution_recovery_is_suspended",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a waiting Task must not be republished")
        ),
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.EXECUTION,
    )
    assert handled == 1
    assert run.status == "running"
    assert run.result[recovery_key] == pytest.approx(
        now.timestamp() + SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
    )


@pytest.mark.asyncio
async def test_queued_scheduler_recovery_rechecks_waiting_task_before_publish(
    monkeypatch,
) -> None:
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-waiting", "agent-1"],
        kwargs={
            "scheduled_run_id": "run-waiting",
            "scheduled_job_id": "job-waiting",
        },
    )
    run = SimpleNamespace(
        id="run-waiting",
        job_id="job-waiting",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
            "scheduled_child_kind": ScheduledDispatchKind.AGENT_TASK.value,
            "scheduled_child_id": "task-waiting",
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return run

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def session_context():
        yield Session()

    monkeypatch.setattr(database, "create_worker_session", lambda: session_context)
    monkeypatch.setattr(
        scheduler_tasks,
        "_scheduled_agent_execution_recovery_is_suspended",
        AsyncMock(return_value=True),
    )

    assert await scheduler_tasks._load_prepared_scheduled_dispatch(
        run.id
    ) is None


@pytest.mark.asyncio
async def test_recovery_delivery_stops_at_waiting_task_but_normal_resume_runs(
    monkeypatch,
) -> None:
    from packages.core.constants.execution import ScheduledChildAdmissionStatus

    async def admitted(*_args, **_kwargs):
        return SimpleNamespace(
            admitted=True,
            status=ScheduledChildAdmissionStatus.ADMITTED,
            reason="scheduled_child_already_admitted",
        )

    execute = AsyncMock(return_value=(True, {"status": "completed"}))
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admitted)
    monkeypatch.setattr(
        ai_tasks,
        "_run_agent_task_with_execution_claim",
        execute,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_scheduled_agent_recovery_is_suspended",
        AsyncMock(return_value="waiting_on_customer"),
    )

    class EmptyResult:
        def scalar_one_or_none(self):
            return None

    class EmptySession:
        async def execute(self, _statement):
            return EmptyResult()

    @asynccontextmanager
    async def session_factory():
        yield EmptySession()

    claimed, waiting = await ai_tasks._run_scheduled_agent_task_if_open(
        session_factory,
        "task-waiting",
        "agent-1",
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
        claim_recheck=True,
    )

    assert claimed is False
    assert waiting == {
        "task_id": "task-waiting",
        "status": "waiting_on_customer",
        "duplicate_suppressed": True,
        "recovery_suspended": True,
        "reason": "task_human_suspended",
    }
    execute.assert_not_awaited()

    claimed, completed = await ai_tasks._run_scheduled_agent_task_if_open(
        session_factory,
        "task-waiting",
        "agent-1",
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
        claim_recheck=False,
    )

    assert claimed is True
    assert completed == {"status": "completed"}
    execute.assert_awaited_once()


def test_run_agent_task_preserves_recovery_suspension_result(monkeypatch) -> None:
    import packages.core.database as database

    suspension = {
        "task_id": "task-waiting",
        "status": "waiting_on_customer",
        "duplicate_suppressed": True,
        "recovery_suspended": True,
        "reason": "task_human_suspended",
    }

    def suspended(awaitable):
        awaitable.close()
        return False, suspension

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", suspended)
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a suspended recovery must not recurse")
        ),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a suspended recovery must not settle")
        ),
    )

    result = ai_tasks.run_agent_task.run(
        "task-waiting",
        "agent-1",
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
        claim_recheck=True,
    )

    assert result == suspension


@pytest.mark.asyncio
async def test_stale_claim_loss_settlement_cannot_close_scheduled_hitl_task(
    monkeypatch,
) -> None:
    from packages.core.services import task_state_machine
    from packages.core.services import workflow_run_execution_claim as claim_service

    claim = claim_service._claim(
        "task-waiting",
        "recovery-worker",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.AgentTaskExecutionClaimLost,
    )
    task = SimpleNamespace(
        id="task-waiting",
        entity_id="entity-1",
        workspace_id=None,
        title="Scheduled approval",
        status="waiting_on_customer",
        actual_output={"sentinel": "preserved"},
        details={
            "scheduled_run_id": "run-waiting",
            "scheduled_job_id": "job-waiting",
            ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY: {
                "version": 2,
                "next_attempt_at": 0,
            },
        },
    )

    @asynccontextmanager
    async def granted_claim(_task_id):
        yield claim

    class QueryResult:
        def one_or_none(self):
            return SimpleNamespace(
                id=task.id,
                entity_id=task.entity_id,
                workspace_id=None,
            )

        def scalar_one_or_none(self):
            return task

    class Session:
        commits = 0
        rollbacks = 0

        async def execute(self, _statement):
            return QueryResult()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    session = Session()

    @asynccontextmanager
    async def session_factory():
        yield session

    monkeypatch.setattr(claim_service, "agent_task_execution_claim", granted_claim)
    monkeypatch.setattr(
        task_state_machine,
        "apply_task_status_transition",
        AsyncMock(side_effect=AssertionError("HITL Task must not fail")),
    )

    result = await ai_tasks._settle_lost_agent_task_execution_claim(
        session_factory,
        task.id,
        "stale claim-loss message",
    )

    assert result["recovery_suspended"] is True
    assert result["status"] == "waiting_on_customer"
    assert task.status == "waiting_on_customer"
    assert task.actual_output == {"sentinel": "preserved"}
    assert ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY not in task.details
    assert session.commits == 1
    assert claim.terminal_committed.is_set() is True


def test_claim_loss_worker_does_not_settle_suspended_scheduled_task(
    monkeypatch,
) -> None:
    import packages.core.database as database

    suspension = ai_tasks._agent_claim_loss_suspension_result("task-waiting")
    settle = Mock()

    def recover(awaitable):
        awaitable.close()
        return ai_tasks.AgentClaimLossRecoveryIntentState.SUSPENDED, suspension

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", recover)
    monkeypatch.setattr(ai_tasks, "_update_job_run_status", settle)

    result = ai_tasks.recover_agent_task_claim_loss.run(
        "task-waiting",
        scheduled_run_id="run-waiting",
        scheduled_job_id="job-waiting",
    )

    assert result == suspension
    settle.assert_not_called()


class _OneTaskSession:
    def __init__(self, task):
        self._task = task

    async def execute(self, _statement):
        task = self._task
        return SimpleNamespace(scalar_one_or_none=lambda: task)


def _blocked_scheduled_task():
    return SimpleNamespace(
        id="task-1",
        status="blocked",
        details={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )


@pytest.mark.asyncio
async def test_recovery_delivery_treats_blocked_task_as_suspended():
    task = _blocked_scheduled_task()

    @asynccontextmanager
    async def session_factory():
        yield _OneTaskSession(task)

    assert await ai_tasks._scheduled_agent_recovery_is_suspended(
        session_factory,
        task_id="task-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
    ) == "blocked"


@pytest.mark.asyncio
async def test_recovery_delivery_blocked_task_of_another_occurrence_is_not_suspended():
    task = _blocked_scheduled_task()

    @asynccontextmanager
    async def session_factory():
        yield _OneTaskSession(task)

    assert await ai_tasks._scheduled_agent_recovery_is_suspended(
        session_factory,
        task_id="task-1",
        scheduled_run_id="run-OTHER",
        scheduled_job_id="job-1",
    ) is None


@pytest.mark.asyncio
async def test_scheduler_sweep_treats_blocked_task_as_suspended():
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    task = _blocked_scheduled_task()
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
        kwargs={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )
    run = SimpleNamespace(
        id="run-1",
        job_id="job-1",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
            "scheduled_child_kind": ScheduledDispatchKind.AGENT_TASK.value,
            "scheduled_child_id": "task-1",
        },
    )
    recovery = {
        "action": ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION.value,
        "dispatch": dispatch,
    }

    assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
        _OneTaskSession(task),
        run,
        recovery,
    ) is True


@pytest.mark.asyncio
async def test_recovery_suspension_result_reports_the_blocked_task_status(
    monkeypatch,
) -> None:
    from packages.core.constants.execution import ScheduledChildAdmissionStatus

    async def admitted(*_args, **_kwargs):
        return SimpleNamespace(
            admitted=True,
            status=ScheduledChildAdmissionStatus.ADMITTED,
            reason="scheduled_child_already_admitted",
        )

    execute = AsyncMock(return_value=(True, {"status": "completed"}))
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admitted)
    monkeypatch.setattr(ai_tasks, "_run_agent_task_with_execution_claim", execute)
    monkeypatch.setattr(
        ai_tasks,
        "_scheduled_agent_recovery_is_suspended",
        AsyncMock(return_value="blocked"),
    )

    class EmptySession:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    @asynccontextmanager
    async def session_factory():
        yield EmptySession()

    claimed, suspended = await ai_tasks._run_scheduled_agent_task_if_open(
        session_factory,
        "task-blocked",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        claim_recheck=True,
    )

    assert claimed is False
    assert suspended["status"] == "blocked"
    assert suspended["reason"] == "task_human_suspended"
    assert suspended["recovery_suspended"] is True
    execute.assert_not_awaited()


def _terminal_task():
    return SimpleNamespace(
        id="task-1",
        status="completed",
        details={
            "scheduled_run_id": "run-1",
            "scheduled_job_id": "job-1",
        },
    )


@pytest.mark.asyncio
async def test_recovery_delivery_never_treats_terminal_task_as_suspended():
    task = _terminal_task()

    @asynccontextmanager
    async def session_factory():
        yield _OneTaskSession(task)

    for status in ("completed", "failed", "cancelled"):
        task.status = status
        assert await ai_tasks._scheduled_agent_recovery_is_suspended(
            session_factory,
            task_id="task-1",
            scheduled_run_id="run-1",
            scheduled_job_id="job-1",
        ) is None


@pytest.mark.asyncio
async def test_scheduler_sweep_never_treats_terminal_task_as_suspended():
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    task = _terminal_task()
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
        kwargs={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )
    run = SimpleNamespace(
        id="run-1",
        job_id="job-1",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
            "scheduled_child_kind": ScheduledDispatchKind.AGENT_TASK.value,
            "scheduled_child_id": "task-1",
        },
    )
    recovery = {
        "action": ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION.value,
        "dispatch": dispatch,
    }

    # Sanity: the same Task waiting on a person does suspend.
    task.status = "waiting_on_customer"
    assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
        _OneTaskSession(task), run, recovery,
    ) is True

    for status in ("completed", "failed", "cancelled"):
        task.status = status
        assert await scheduler_tasks._scheduled_agent_execution_recovery_is_suspended(
            _OneTaskSession(task), run, recovery,
        ) is False
