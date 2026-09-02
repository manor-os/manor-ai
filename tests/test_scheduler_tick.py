"""Unit tests for scheduler tick logic — no DB required."""

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

try:
    from packages.core.tasks.scheduler_tasks import (
        _agent_task_max_turns_for_target,
        _cron_matches,
        _is_due,
        _tighten_file_deliverable_completion,
    )
except ImportError:
    pytest.skip("Celery not installed — skipping scheduler tests", allow_module_level=True)


def _make_job(**kwargs):
    """Create a mock job object with sensible defaults."""
    defaults = {
        "entity_id": "entity-1",
        "job_id": "test-job-1",
        "schedule_kind": None,
        "cron_expr": None,
        "every_seconds": None,
        "run_at": None,
        "timezone": "UTC",
        "last_run_at": None,
        "last_status": None,
        "enabled": True,
        "delete_after_run": False,
        "consecutive_errors": 0,
        "revision": 1,
        "agent_id": None,
        "goal_id": None,
        "manor_task_id": None,
        "execution_type": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


async def _reuse_scheduled_recovery_candidate(_db, candidate):
    """Unit-test seam; integration tests exercise the real row locks."""
    return SimpleNamespace(
        job_id=getattr(candidate, "job_id", f"job:{candidate.id}"),
    ), candidate


def test_scheduler_uses_the_shared_worker_event_loop_boundary():
    from packages.core.tasks import scheduler_tasks
    from packages.core.tasks._runtime import run_in_worker

    assert scheduler_tasks._run_async is run_in_worker


def test_scheduled_skill_generation_claim_uses_shared_enum_factory():
    from packages.core.constants.execution import ExecutionClaimKind
    from packages.core.services.workflow_run_execution_claim import (
        ExecutionClaimPolicyFactory,
        SCHEDULED_JOB_SKILL_GENERATION_LEASE_TTL_SECONDS,
        ScheduledJobSkillGenerationClaimLost,
    )

    policy = ExecutionClaimPolicyFactory.create(
        ExecutionClaimKind.SCHEDULED_JOB_SKILL
    )

    assert policy.kind is ExecutionClaimKind.SCHEDULED_JOB_SKILL
    assert (
        policy.lease_ttl_seconds
        == SCHEDULED_JOB_SKILL_GENERATION_LEASE_TTL_SECONDS
    )
    assert policy.lost_error_type is ScheduledJobSkillGenerationClaimLost


def test_task_planning_claim_uses_shared_enum_factory():
    from packages.core.constants.execution import ExecutionClaimKind
    from packages.core.services.workflow_run_execution_claim import (
        ExecutionClaimPolicyFactory,
        TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS,
        TaskPlanExecutionClaimLost,
    )

    policy = ExecutionClaimPolicyFactory.create(ExecutionClaimKind.TASK_PLAN)

    assert policy.kind is ExecutionClaimKind.TASK_PLAN
    assert policy.lease_ttl_seconds == TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS
    assert policy.lost_error_type is TaskPlanExecutionClaimLost


@pytest.mark.asyncio
async def test_agent_task_execution_claim_suppresses_concurrent_runner(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.ai.task_runner import TaskRunner
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    @asynccontextmanager
    async def held_claim(_task_id):
        yield claim_service._claim(
            "task-1",
            "other-worker",
            granted=False,
            reason=claim_service.CLAIM_HELD,
        )

    async def unexpected_run(*_args, **_kwargs):
        raise AssertionError("a denied delivery must not enter TaskRunner")

    monkeypatch.setattr(claim_service, "agent_task_execution_claim", held_claim)
    monkeypatch.setattr(TaskRunner, "run", unexpected_run)

    claimed, result = await ai_tasks._run_agent_task_with_execution_claim(
        object(),
        "task-1",
        "agent-1",
    )

    assert claimed is False
    assert result is None


@pytest.mark.asyncio
async def test_agent_task_runner_fences_and_closes_its_terminal_commit(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.ai.task_runner import TaskRunner
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    claim = claim_service._claim(
        "task-1",
        "worker-1",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.AgentTaskExecutionClaimLost,
    )

    @asynccontextmanager
    async def granted_claim(_task_id):
        yield claim

    async def fake_run(self, *_args):
        assert self._execution_claim is claim
        self._before_terminal_commit()
        self._after_terminal_commit()
        claim.lost.set()
        self._before_terminal_commit()
        return {"status": "completed"}

    monkeypatch.setattr(claim_service, "agent_task_execution_claim", granted_claim)
    monkeypatch.setattr(TaskRunner, "run", fake_run)

    claimed, result = await ai_tasks._run_agent_task_with_execution_claim(
        object(),
        "task-1",
        "agent-1",
    )

    assert claimed is True
    assert result == {"status": "completed"}
    assert claim.terminal_committed.is_set() is True


@pytest.mark.asyncio
async def test_agent_task_claim_loss_settlement_uses_fresh_fenced_claim(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import event_emitter
    from packages.core.services import task_state_machine
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    claim = claim_service._claim(
        "task-1",
        "recovery-worker",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.AgentTaskExecutionClaimLost,
    )
    task = SimpleNamespace(
        id="task-1",
        entity_id="entity-1",
        title="Recover me",
        status="in_progress",
        actual_output=None,
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

    class FakeSession:
        commits = 0
        rollbacks = 0

        async def execute(self, _statement):
            return QueryResult()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    db = FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield db

    async def transition(record, status, **_kwargs):
        record.status = status

    emitted = []
    monkeypatch.setattr(claim_service, "agent_task_execution_claim", granted_claim)
    monkeypatch.setattr(task_state_machine, "apply_task_status_transition", transition)
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    result = await ai_tasks._settle_lost_agent_task_execution_claim(
        session_factory,
        "task-1",
        "Agent task execution claim was lost",
    )

    assert result["status"] == "failed"
    assert db.commits == 1
    assert db.rollbacks == 0
    assert claim.terminal_committed.is_set() is True
    assert task.actual_output["error_type"] == "AgentTaskExecutionClaimLost"
    assert emitted[0][0][1] == "task.failed"


@pytest.mark.asyncio
async def test_agent_task_claim_loss_settlement_defers_to_successor(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    @asynccontextmanager
    async def held_claim(_task_id):
        yield claim_service._claim(
            "task-1",
            "successor-worker",
            granted=False,
            reason=claim_service.CLAIM_HELD,
            lost_error_type=claim_service.AgentTaskExecutionClaimLost,
        )

    def unexpected_session():
        raise AssertionError("a denied recovery claim must not lock or mutate Task")

    monkeypatch.setattr(claim_service, "agent_task_execution_claim", held_claim)

    assert await ai_tasks._settle_lost_agent_task_execution_claim(
        unexpected_session,
        "task-1",
        "Agent task execution claim was lost",
    ) is None


def test_agent_task_claim_loss_recovery_factory_defers_complete_retry_chains():
    from packages.core.constants.execution import (
        SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
    )
    from packages.core.services.workflow_run_execution_claim import (
        AGENT_TASK_EXECUTION_RECHECK_SECONDS,
    )
    from packages.core.tasks import ai_tasks

    now = datetime.now(timezone.utc)
    initial_delay = (
        AGENT_TASK_EXECUTION_RECHECK_SECONDS
        + SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS
    )
    initial = ai_tasks.AgentClaimLossRecoveryIntentFactory.persist(
        {
            "next_attempt_at": 0,
            "sweep_attempts": 4,
            "publish_claim_id": "stale-claim",
        },
        scheduled_run_id=None,
        scheduled_job_id=None,
        error="claim lost",
        now=now,
        retry_after_seconds=initial_delay,
        publish_claim_id="fresh-claim",
    )

    assert initial["publish_claim_id"] == "fresh-claim"
    assert initial["sweep_attempts"] == 0
    assert initial["next_attempt_at"] == now.timestamp() + initial_delay

    first_sweep_at = now + timedelta(seconds=initial_delay)
    first = ai_tasks.AgentClaimLossRecoveryIntentFactory.claim(
        initial,
        now=first_sweep_at,
    )
    second_sweep_at = datetime.fromtimestamp(
        first["next_attempt_at"],
        tz=timezone.utc,
    )
    second = ai_tasks.AgentClaimLossRecoveryIntentFactory.claim(
        first,
        now=second_sweep_at,
    )

    assert (
        first["next_attempt_at"] - first_sweep_at.timestamp()
        == SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS
    )
    assert (
        second["next_attempt_at"] - second_sweep_at.timestamp()
        == SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS * 2
    )


@pytest.mark.asyncio
async def test_agent_task_claim_loss_intent_is_durable_and_sweepable(
    db_session,
):
    from contextlib import asynccontextmanager

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Recover durable Agent task",
        status="in_progress",
        details={"existing": "preserved"},
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    @asynccontextmanager
    async def session_factory():
        yield db_session

    state = await ai_tasks._persist_agent_claim_loss_recovery_intent(
        session_factory,
        task_id,
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        error="Agent task execution claim was lost",
    )

    await db_session.refresh(task)
    intent = task.details[ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY]
    assert state is ai_tasks.AgentClaimLossRecoveryIntentState.PERSISTED
    assert task.details["existing"] == "preserved"
    assert intent["version"] == 2
    assert intent["scheduled_run_id"] == "run-1"
    assert intent["next_attempt_at"] > datetime.now(timezone.utc).timestamp()
    assert await ai_tasks._load_agent_claim_loss_recovery_intents(session_factory) == []

    details = dict(task.details)
    details[ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY] = {
        **intent,
        "next_attempt_at": "malformed-legacy-value",
    }
    task.details = details
    await db_session.commit()

    [candidate] = await ai_tasks._load_agent_claim_loss_recovery_intents(
        session_factory
    )
    assert {
        key: candidate[key]
        for key in ("task_id", "scheduled_run_id", "scheduled_job_id")
    } == {
        "task_id": task_id,
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
    }
    assert candidate["_sweep_attempt"] == 1
    assert len(candidate["_publish_claim_id"]) == 32
    assert await ai_tasks._load_agent_claim_loss_recovery_intents(session_factory) == []

    await db_session.refresh(task)
    claimed_intent = task.details[ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY]
    assert claimed_intent["sweep_attempts"] == 1
    assert claimed_intent["next_attempt_at"] > datetime.now(timezone.utc).timestamp()

    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "failed"
    await db_session.commit()
    assert await ai_tasks._load_agent_claim_loss_recovery_intents(
        session_factory
    ) == []


@pytest.mark.asyncio
async def test_agent_task_claim_loss_sweep_skips_undue_oldest_intent(
    db_session,
):
    from contextlib import asynccontextmanager

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    future_task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Older but not due",
        status="in_progress",
        details={
            ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY: {
                "version": 2,
                "requested_at": (now - timedelta(hours=1)).isoformat(),
                "next_attempt_at": (now + timedelta(hours=1)).timestamp(),
                "sweep_attempts": 0,
            },
        },
    )
    due_task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Newer and due",
        status="in_progress",
        details={
            ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY: {
                "version": 2,
                "requested_at": now.isoformat(),
                "next_attempt_at": (now - timedelta(seconds=1)).timestamp(),
                "sweep_attempts": 0,
            },
        },
    )
    db_session.add_all([future_task, due_task])
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    [candidate] = await ai_tasks._load_agent_claim_loss_recovery_intents(
        session_factory,
        now=now,
        limit=1,
    )

    assert candidate["task_id"] == due_task.id


@pytest.mark.asyncio
async def test_agent_task_claim_loss_publish_release_is_uniquely_fenced(
    db_session,
):
    from contextlib import asynccontextmanager

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    now = datetime.now(timezone.utc)
    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        title="Fence recovery publisher",
        status="in_progress",
        details={
            ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY: {
                "version": 2,
                "requested_at": now.isoformat(),
                "next_attempt_at": 0,
                "sweep_attempts": 0,
            },
        },
    )
    db_session.add(task)
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    [first] = await ai_tasks._load_agent_claim_loss_recovery_intents(
        session_factory,
        now=now,
        limit=1,
    )
    released_at = now + timedelta(seconds=1)
    assert await ai_tasks._retry_agent_claim_loss_recovery_intent(
        session_factory,
        task.id,
        expected_sweep_attempt=first["_sweep_attempt"],
        expected_publish_claim_id=first["_publish_claim_id"],
        now=released_at,
    ) is True

    [second] = await ai_tasks._load_agent_claim_loss_recovery_intents(
        session_factory,
        now=released_at + timedelta(seconds=61),
        limit=1,
    )
    assert second["_publish_claim_id"] != first["_publish_claim_id"]
    assert await ai_tasks._retry_agent_claim_loss_recovery_intent(
        session_factory,
        task.id,
        expected_sweep_attempt=first["_sweep_attempt"],
        expected_publish_claim_id=first["_publish_claim_id"],
        now=released_at + timedelta(seconds=62),
    ) is False

    await db_session.refresh(task)
    intent = task.details[ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY]
    assert intent["publish_claim_id"] == second["_publish_claim_id"]


@pytest.mark.asyncio
async def test_agent_task_claim_loss_sweep_rotates_past_one_full_batch(
    db_session,
):
    from contextlib import asynccontextmanager

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    tasks = [
        Task(
            id=generate_ulid(),
            entity_id=entity_id,
            title=f"Recover Agent task {index}",
            status="in_progress",
            details={
                ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_KEY: {
                    "version": 1,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "next_attempt_at": 0,
                    "scheduled_run_id": None,
                    "scheduled_job_id": None,
                    "error": "claim lost",
                },
            },
        )
        for index in range(ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_BATCH + 1)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    first = await ai_tasks._load_agent_claim_loss_recovery_intents(session_factory)
    second = await ai_tasks._load_agent_claim_loss_recovery_intents(session_factory)

    assert len(first) == ai_tasks._AGENT_CLAIM_LOSS_RECOVERY_BATCH
    assert len(second) == 1
    assert {item["task_id"] for item in first}.isdisjoint(
        {item["task_id"] for item in second}
    )
    assert await ai_tasks._load_agent_claim_loss_recovery_intents(session_factory) == []


@pytest.mark.asyncio
async def test_fenced_commit_resolves_before_repeated_cancellation_returns():
    from packages.core.services.workflow_run_execution_claim import (
        commit_fenced_execution_boundary,
    )

    owner = asyncio.current_task()
    assert owner is not None
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    events = []

    async def commit():
        commit_started.set()
        await release_commit.wait()
        events.append("committed")

    async def cancel_owner():
        await commit_started.wait()
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(0)
        release_commit.set()

    canceller = asyncio.create_task(cancel_owner())
    await commit_fenced_execution_boundary(
        commit,
        before_commit=lambda: events.append("before"),
        after_commit=lambda: events.append("after"),
    )
    await canceller

    assert events == ["before", "committed", "after"]
    assert owner.cancelling() == 0


@pytest.mark.asyncio
async def test_fenced_commit_validates_durable_token_in_business_transaction():
    from packages.core.services import workflow_run_execution_claim as claim_service

    events: list[str] = []

    class Result:
        def scalar_one_or_none(self):
            return "worker-1"

    class Db:
        async def execute(self, statement, params):
            assert "FOR UPDATE" in str(statement)
            assert params == {
                "claim_key": "agent-task:task-1",
                "claim_token": "worker-1",
            }
            events.append("fenced")
            return Result()

        async def commit(self):
            events.append("committed")

    claim = claim_service._claim(
        "task-1",
        "worker-1",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        claim_key="agent-task:task-1",
        lost_error_type=claim_service.AgentTaskExecutionClaimLost,
    )
    db = Db()

    await claim_service.commit_fenced_execution_boundary(
        db.commit,
        after_commit=claim.mark_terminal_committed,
        execution_claim=claim,
        session=db,
    )

    assert events == ["fenced", "committed"]
    assert claim.terminal_committed.is_set() is True


@pytest.mark.asyncio
async def test_fenced_commit_rejects_replaced_durable_token():
    from packages.core.services import workflow_run_execution_claim as claim_service

    class MissingResult:
        def scalar_one_or_none(self):
            return None

    class Db:
        committed = False

        async def execute(self, _statement, _params):
            return MissingResult()

        async def commit(self):
            self.committed = True

    claim = claim_service._claim(
        "run-1",
        "stale-worker",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        claim_key="scheduled-run:run-1",
        lost_error_type=claim_service.ScheduledRunExecutionClaimLost,
    )
    db = Db()

    with pytest.raises(claim_service.ScheduledRunExecutionClaimLost):
        await claim_service.commit_fenced_execution_boundary(
            db.commit,
            execution_claim=claim,
            session=db,
        )

    assert db.committed is False
    assert claim.lost.is_set() is True


@pytest.mark.asyncio
async def test_scheduled_live_claim_probe_uses_authoritative_row_lock():
    from packages.core.services import workflow_run_execution_claim as claim_service

    calls = []

    class Result:
        def scalar_one_or_none(self):
            return "live-token"

    class Db:
        async def execute(self, statement, params):
            calls.append((str(statement), params))
            return Result()

    assert await claim_service.scheduled_run_has_live_execution_claim(
        Db(),
        "run-1",
    ) is True
    assert calls[0][1] == {"claim_key": "scheduled-run:run-1"}
    assert "FOR UPDATE" in calls[0][0]


@pytest.mark.asyncio
async def test_execution_claim_uses_one_durable_postgres_fence(monkeypatch):
    from packages.core.services import workflow_run_execution_claim as claim_service

    held: set[str] = set()
    acquisitions: list[str] = []
    releases: list[str] = []

    async def acquire(claim_key, _token, *, lease_ttl_seconds):
        assert lease_ttl_seconds > 0
        acquisitions.append(claim_key)
        granted = claim_key not in held
        if granted:
            held.add(claim_key)
        return granted

    async def release(claim_key, _token):
        releases.append(claim_key)
        held.remove(claim_key)
        return True

    monkeypatch.setattr(
        claim_service,
        "_acquire_postgres_execution_claim",
        acquire,
    )
    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )

    async with claim_service.agent_task_execution_claim("task-1") as first:
        async with claim_service.agent_task_execution_claim("task-1") as second:
            assert first.granted is True
            assert second.granted is False
    async with claim_service.scheduled_run_execution_claim("run-1") as scheduled:
        assert scheduled.granted is True
    async with claim_service.task_plan_execution_claim("task-2") as planning:
        assert planning.granted is True

    assert acquisitions == [
        "agent-task:task-1",
        "agent-task:task-1",
        "scheduled-run:run-1",
        "task-plan:task-2",
    ]
    assert releases == [
        "agent-task:task-1",
        "scheduled-run:run-1",
        "task-plan:task-2",
    ]


@pytest.mark.asyncio
async def test_execution_claim_loss_survives_swallowed_cancellation(monkeypatch):
    from packages.core.services import workflow_run_execution_claim as claim_service

    async def acquire(*_args, **_kwargs):
        return True

    async def lose_claim(*_args, **_kwargs):
        return False

    async def release(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS",
        0,
    )
    monkeypatch.setattr(
        claim_service,
        "_acquire_postgres_execution_claim",
        acquire,
    )
    monkeypatch.setattr(
        claim_service,
        "_extend_postgres_execution_claim",
        lose_claim,
    )
    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )

    effects = []
    with pytest.raises(claim_service.AgentTaskExecutionClaimLost):
        async with claim_service.agent_task_execution_claim("task-1"):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                # Provider wrappers may translate or suppress cancellation.
                # A lost fence keeps canceling so a wrapper cannot reach the
                # next awaited side-effect boundary under stale ownership.
                await asyncio.sleep(0.05)
                effects.append("post-loss side effect")
    assert effects == []


@pytest.mark.asyncio
async def test_execution_claim_stops_heartbeat_after_terminal_commit(monkeypatch):
    from packages.core.services import workflow_run_execution_claim as claim_service

    renewal_calls = 0

    async def acquire(*_args, **_kwargs):
        return True

    async def extend(*_args, **_kwargs):
        nonlocal renewal_calls
        renewal_calls += 1
        return False

    async def release(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS",
        0,
    )
    monkeypatch.setattr(
        claim_service,
        "_acquire_postgres_execution_claim",
        acquire,
    )
    monkeypatch.setattr(
        claim_service,
        "_extend_postgres_execution_claim",
        extend,
    )
    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )

    async with claim_service.agent_task_execution_claim("task-1") as claim:
        claim.mark_terminal_committed()
        await asyncio.sleep(0.01)
        assert claim.lost.is_set() is False

    assert renewal_calls == 0


@pytest.mark.asyncio
async def test_scheduler_lifecycle_lease_reuses_caller_connection():
    from packages.core.services.reusable_resource_locks import (
        reusable_resource_lifecycle_lease,
    )

    statements = []

    class Result:
        @staticmethod
        def scalar_one():
            return True

    class Connection:
        async def execute(self, statement):
            statements.append(str(statement))
            return Result()

    connection = Connection()

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

        async def connect(self):
            raise AssertionError("lifecycle lease must not open another connection")

    class Session:
        bind = Bind()

        def get_bind(self):
            return self.bind

        async def connection(self):
            return connection

    async with reusable_resource_lifecycle_lease(
        Session(),
        entity_id="entity-1",
    ):
        statements.append("narrow-row-locked")

    assert "pg_advisory_lock" in statements[0]
    assert statements[1] == "narrow-row-locked"
    assert "pg_advisory_unlock" in statements[2]


def test_run_agent_task_does_not_finalize_a_suppressed_duplicate(monkeypatch):
    from packages.core.services.workflow_run_execution_claim import (
        AGENT_TASK_EXECUTION_RECHECK_SECONDS,
    )
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    rechecks = []

    def deny_claim(awaitable):
        awaitable.close()
        return False, None

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", deny_claim)
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "apply_async",
        lambda **kwargs: rechecks.append(kwargs),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("the active delivery owns finalization")
        ),
    )

    result = ai_tasks.run_agent_task.run(
        "task-1",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
    )

    assert result == {
        "task_id": "task-1",
        "status": "in_progress",
        "duplicate_suppressed": True,
        "recheck_scheduled": True,
    }
    assert rechecks == [{
        "args": ["task-1", "agent-1"],
        "kwargs": {
            "scheduled_run_id": "run-1",
            "scheduled_job_id": "job-1",
            "claim_recheck": True,
        },
        "countdown": AGENT_TASK_EXECUTION_RECHECK_SECONDS,
    }]


def test_agent_settlement_task_never_reenters_task_runner(monkeypatch):
    from packages.core.constants.execution import (
        ScheduledRunStatus,
        ScheduledSettlementKind,
    )
    from packages.core.services.scheduled_run_lifecycle import ScheduledRunSettlement
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    settled = []
    settlement = ScheduledRunSettlement.create(
        kind=ScheduledSettlementKind.AGENT_TASK,
        child_id="task-1",
        status=ScheduledRunStatus.SUCCESS,
        result={"status": "completed", "response": "done"},
        error=None,
    )
    from packages.core.queues import CeleryQueue

    signature = ai_tasks._scheduled_settlement_signature(
        run_id="run-1",
        job_id_str="job-1",
        settlement=settlement,
    )
    assert signature.task == "scheduler.settle_scheduled_agent_run"
    assert signature.options["queue"] == CeleryQueue.RECOVERY_V2.value

    async def settle(
        received_session_factory,
        task_id,
        result,
        **kwargs,
    ):
        settled.append((received_session_factory, task_id, result, kwargs))

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(ai_tasks, "_update_job_run_status_async", settle)
    monkeypatch.setattr(
        ai_tasks,
        "_run_scheduled_agent_task_if_open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("settlement-only retry must not enter TaskRunner")
        ),
    )

    result = ai_tasks.settle_scheduled_agent_run.run(
        "run-1",
        "job-1",
        settlement.to_payload(),
    )

    assert result == {
        "run_id": "run-1",
        "task_id": "task-1",
        "status": "success",
    }
    assert settled == [(
        session_factory,
        "task-1",
        {"status": "completed", "response": "done"},
        {"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )]


def test_business_task_signatures_remain_compatible_with_previous_workers():
    import inspect

    from packages.core.tasks import ai_tasks

    business_tasks = (
        ai_tasks.run_morning_briefing,
        ai_tasks.run_outcome_evaluation,
        ai_tasks.run_chat_insight_extraction,
        ai_tasks.run_strategist_review,
        ai_tasks.run_goal_measurement,
        ai_tasks.run_workspace_stat_collection,
        ai_tasks.run_agent_task,
        ai_tasks.run_workflow,
    )
    for task in business_tasks:
        assert "_scheduled_settlement" not in inspect.signature(task.run).parameters
    assert "claim_loss_recheck" not in inspect.signature(
        ai_tasks.run_agent_task.run
    ).parameters


def test_settlement_signature_factory_rejects_unknown_kind():
    from packages.core.tasks import ai_tasks

    settlement = SimpleNamespace(kind="future-wire-kind")

    with pytest.raises(ValueError, match="unsupported scheduled settlement kind"):
        ai_tasks._scheduled_settlement_signature(
            run_id="run-1",
            job_id_str="job-1",
            settlement=settlement,
        )


def test_agent_settlement_replacement_is_not_caught_as_business_failure(monkeypatch):
    from celery.exceptions import Ignore

    from packages.core.tasks import ai_tasks

    def completed(awaitable):
        awaitable.close()
        return True, {"status": "completed", "response": "done"}

    monkeypatch.setattr(ai_tasks, "_run_async", completed)
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Ignore("replaced")),
    )
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "retry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("settlement replacement must not retry TaskRunner")
        ),
    )

    with pytest.raises(Ignore):
        ai_tasks.run_agent_task.run(
            "task-1",
            "agent-1",
            scheduled_run_id="run-1",
            scheduled_job_id="job-1",
        )


@pytest.mark.asyncio
async def test_scheduled_agent_child_rechecks_durable_occurrence(monkeypatch):
    from packages.core.constants.execution import ScheduledChildAdmissionStatus
    from packages.core.tasks import ai_tasks

    async def closed_occurrence(*_args, **_kwargs):
        return SimpleNamespace(
            admitted=False,
            status=ScheduledChildAdmissionStatus.CLOSED,
            reason="scheduled_occurrence_missing",
        )

    async def no_artifact(*_args, **_kwargs):
        return False

    async def unexpected_agent_execution(*_args, **_kwargs):
        raise AssertionError("closed scheduled occurrence must not run TaskRunner")

    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", closed_occurrence)
    monkeypatch.setattr(
        ai_tasks,
        "_terminalize_suppressed_scheduled_child",
        no_artifact,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_run_agent_task_with_execution_claim",
        unexpected_agent_execution,
    )

    claimed, result = await ai_tasks._run_scheduled_agent_task_if_open(
        object(),
        "task-1",
        "agent-1",
        scheduled_run_id="run-deleted",
        scheduled_job_id="job-deleted",
    )

    assert claimed is False
    assert result == {
        "task_id": "task-1",
        "status": "already_settled",
        "duplicate_suppressed": True,
        "scheduled_occurrence_closed": True,
        "reason": "scheduled_occurrence_missing",
        "child_terminalized": False,
    }


def test_agent_task_claim_recheck_does_not_schedule_recursively(monkeypatch):
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    def deny_claim(awaitable):
        awaitable.close()
        return False, None

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", deny_claim)
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a recovery delivery gets no recursive recheck")
        ),
    )

    result = ai_tasks.run_agent_task.run(
        "task-1",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        claim_recheck=True,
    )

    assert result == {
        "task_id": "task-1",
        "status": "in_progress",
        "duplicate_suppressed": True,
        "recheck_scheduled": False,
    }


def test_agent_task_claim_loss_defers_to_fresh_claim_without_replay(monkeypatch):
    from packages.core.services.workflow_run_execution_claim import (
        AGENT_TASK_EXECUTION_RECHECK_SECONDS,
        AgentTaskExecutionClaimLost,
    )
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    finalized = []
    scheduled = []
    calls = 0

    def lose_then_defer_then_persist(awaitable):
        nonlocal calls
        awaitable.close()
        calls += 1
        if calls == 1:
            raise AgentTaskExecutionClaimLost("lease heartbeat lost")
        if calls == 2:
            return None
        return ai_tasks.AgentClaimLossRecoveryIntentState.PERSISTED

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(ai_tasks, "_run_async", lose_then_defer_then_persist)
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *args, **kwargs: finalized.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_agent_claim_loss_recovery",
        lambda **kwargs: scheduled.append(kwargs),
    )

    result = ai_tasks.run_agent_task.run(
        "task-1",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
    )

    assert result == {
        "task_id": "task-1",
        "status": "in_progress",
        "duplicate_suppressed": True,
        "claim_loss_settlement_deferred": True,
        "recheck_scheduled": True,
        "recovery_intent": "persisted",
    }
    assert finalized == []
    assert scheduled == [{
        "task_id": "task-1",
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
        "countdown": AGENT_TASK_EXECUTION_RECHECK_SECONDS,
    }]


def test_agent_task_claim_loss_recovery_retries_only_settlement(monkeypatch):
    from celery.exceptions import Retry

    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    retry_kwargs = []

    def defer_settlement(awaitable):
        awaitable.close()
        return ai_tasks.AgentClaimLossRecoveryIntentState.PERSISTED, None

    def retry(**kwargs):
        retry_kwargs.append(kwargs)
        raise Retry()

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(ai_tasks, "_run_async", defer_settlement)
    monkeypatch.setattr(ai_tasks.recover_agent_task_claim_loss, "retry", retry)
    monkeypatch.setattr(
        ai_tasks,
        "_run_scheduled_agent_task_if_open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("claim-loss recovery must not re-enter TaskRunner")
        ),
    )

    signature = ai_tasks._agent_claim_loss_recovery_signature(
        task_id="task-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
    )
    from packages.core.queues import CeleryQueue

    assert signature.task == "agent.recover_claim_loss_v2"
    assert signature.options["queue"] == CeleryQueue.RECOVERY_V2.value

    with pytest.raises(Retry):
        ai_tasks.recover_agent_task_claim_loss.run(
            "task-1",
            scheduled_run_id="run-1",
            scheduled_job_id="job-1",
        )

    assert len(retry_kwargs) == 1
    assert "Agent task claim-loss settlement is still owned" in str(
        retry_kwargs[0]["exc"]
    )


def test_agent_task_claim_loss_broker_failure_uses_durable_intent(monkeypatch):
    from packages.core.services.workflow_run_execution_claim import (
        AgentTaskExecutionClaimLost,
    )
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    calls = 0

    def lose_defer_then_persist(awaitable):
        nonlocal calls
        awaitable.close()
        calls += 1
        if calls == 1:
            raise AgentTaskExecutionClaimLost("lease heartbeat lost")
        if calls == 2:
            return None
        return ai_tasks.AgentClaimLossRecoveryIntentState.PERSISTED

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", lose_defer_then_persist)
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_agent_claim_loss_recovery",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )

    result = ai_tasks.run_agent_task.run("task-1", "agent-1")

    assert result["status"] == "in_progress"
    assert result["recheck_scheduled"] is False
    assert result["recovery_intent"] == "persisted"
    assert calls == 4


def test_agent_task_claim_loss_sweep_republishes_durable_intents(monkeypatch):
    from packages.core.tasks import ai_tasks

    candidates = [
        {
            "task_id": "task-1",
            "scheduled_run_id": "run-1",
            "scheduled_job_id": "job-1",
            "_sweep_attempt": 1,
            "_publish_claim_id": "claim-1",
        },
        {
            "task_id": "task-2",
            "scheduled_run_id": None,
            "scheduled_job_id": None,
            "_sweep_attempt": 2,
            "_publish_claim_id": "claim-2",
        },
    ]
    queued = []
    run_results = iter([candidates, True])

    def load(awaitable):
        awaitable.close()
        return next(run_results)

    def enqueue(**kwargs):
        if kwargs["task_id"] == "task-2":
            raise RuntimeError("broker unavailable")
        queued.append(kwargs)

    monkeypatch.setattr(ai_tasks, "_run_async", load)
    monkeypatch.setattr(ai_tasks, "_enqueue_agent_claim_loss_recovery", enqueue)

    assert ai_tasks.recover_agent_task_claim_loss_sweep.run() == {
        "found": 2,
        "queued": 1,
    }
    assert queued == [{
        "task_id": "task-1",
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
    }]


@pytest.mark.asyncio
async def test_scheduled_occurrence_claim_suppresses_duplicate_child_work(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    @asynccontextmanager
    async def held_claim(_run_id):
        yield claim_service._claim(
            "run-1",
            "other-worker",
            granted=False,
            reason=claim_service.CLAIM_HELD,
        )

    async def unexpected_body():
        raise AssertionError("a duplicate delivery must not enter business work")

    monkeypatch.setattr(claim_service, "scheduled_run_execution_claim", held_claim)

    executed, result = await ai_tasks._run_scheduled_once(
        unexpected_body,
        run_id="run-1",
        job_id_str="job-1",
    )

    assert executed is False
    assert result == {
        "status": "in_progress",
        "duplicate_suppressed": True,
    }


@pytest.mark.asyncio
async def test_scheduled_occurrence_closes_claim_after_durable_settlement(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    claim = claim_service._claim(
        "run-1",
        "worker-1",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.ScheduledRunExecutionClaimLost,
    )

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield claim

    async def admitted(*_args, **_kwargs):
        return SimpleNamespace(admitted=True, reason="admitted")

    async def finalize(**_kwargs):
        return None

    async def business_result():
        return {"status": "completed"}

    monkeypatch.setattr(claim_service, "scheduled_run_execution_claim", granted_claim)
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admitted)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", finalize)

    executed, result = await ai_tasks._run_scheduled_once(
        business_result,
        run_id="run-1",
        job_id_str="job-1",
    )

    assert executed is True
    assert result == {"status": "completed"}
    assert claim.terminal_committed.is_set() is True


@pytest.mark.asyncio
async def test_scheduled_occurrence_claim_loss_never_writes_unfenced_settlement(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    claim = claim_service._claim(
        "run-claim-lost",
        "stale-worker",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.ScheduledRunExecutionClaimLost,
    )

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield claim

    async def admitted(*_args, **_kwargs):
        return SimpleNamespace(admitted=True, reason="admitted")

    async def finalize(**_kwargs):
        claim.lost.set()
        claim.raise_if_lost()

    async def business_result():
        return {"status": "completed"}

    async def unexpected_settlement(**_kwargs):
        raise AssertionError("a stale claim must not write settlement state")

    monkeypatch.setattr(claim_service, "scheduled_run_execution_claim", granted_claim)
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admitted)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", finalize)
    monkeypatch.setattr(
        ai_tasks,
        "_defer_scheduled_run_settlement",
        unexpected_settlement,
    )

    with pytest.raises(claim_service.ScheduledRunExecutionClaimLost):
        await ai_tasks._run_scheduled_once(
            business_result,
            run_id="run-claim-lost",
            job_id_str="job-1",
        )

    assert claim.terminal_committed.is_set() is False


@pytest.mark.asyncio
async def test_scheduled_settlement_fallback_rejects_token_lost_after_finalize_error(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    claim = claim_service._claim(
        "run-fallback-lost",
        "stale-worker",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.ScheduledRunExecutionClaimLost,
    )

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield claim

    async def admitted(*_args, **_kwargs):
        return SimpleNamespace(admitted=True, reason="admitted")

    async def fail_finalize(**_kwargs):
        raise RuntimeError("finalizer transaction failed")

    async def lose_before_settlement_persist(*, execution_claim, **_kwargs):
        assert execution_claim is claim
        claim.lost.set()
        claim.raise_if_lost()

    monkeypatch.setattr(claim_service, "scheduled_run_execution_claim", granted_claim)
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admitted)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", fail_finalize)
    monkeypatch.setattr(
        ai_tasks,
        "_persist_scheduled_run_settlement",
        lose_before_settlement_persist,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_run_settlement",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale settlement must not be enqueued")
        ),
    )

    with pytest.raises(claim_service.ScheduledRunExecutionClaimLost):
        await ai_tasks._run_scheduled_once(
            lambda: asyncio.sleep(0, result={"status": "completed"}),
            run_id="run-fallback-lost",
            job_id_str="job-1",
        )

    assert claim.terminal_committed.is_set() is False


@pytest.mark.asyncio
async def test_settlement_pending_commit_uses_occurrence_claim_fence(monkeypatch):
    import packages.core.database as database
    from packages.core.services import scheduled_run_lifecycle
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    events = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            events.append("commit")

    claim = claim_service._claim(
        "run-fenced-settlement",
        "worker-1",
        granted=True,
        reason=claim_service.CLAIM_GRANTED,
        lost_error_type=claim_service.ScheduledRunExecutionClaimLost,
    )

    async def persist(_db, **_kwargs):
        events.append("persist")
        return True

    async def commit_fenced(
        commit,
        *,
        before_commit,
        after_commit,
        execution_claim,
        session,
    ):
        assert execution_claim is claim
        assert isinstance(session, Session)
        before_commit()
        events.append("fence")
        await commit()
        after_commit()

    monkeypatch.setattr(database, "create_worker_session", lambda: Session)
    monkeypatch.setattr(
        scheduled_run_lifecycle,
        "persist_scheduled_run_settlement_pending",
        persist,
    )
    monkeypatch.setattr(
        claim_service,
        "commit_fenced_execution_boundary",
        commit_fenced,
    )

    settlement = scheduled_run_lifecycle.ScheduledRunSettlement.create(
        result={"status": "completed"},
        error=None,
    )
    persisted = await ai_tasks._persist_scheduled_run_settlement(
        run_id="run-fenced-settlement",
        job_id_str="job-1",
        settlement=settlement,
        execution_claim=claim,
    )

    assert persisted is True
    assert events == ["persist", "fence", "commit"]
    assert claim.terminal_committed.is_set() is True


def test_scheduled_claim_loss_does_not_finalize_from_outer_error_handler(
    monkeypatch,
):
    from packages.core.services.workflow_run_execution_claim import (
        ScheduledRunExecutionClaimLost,
    )
    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks,
        "_run_async",
        lambda awaitable: (
            awaitable.close(),
            (_ for _ in ()).throw(ScheduledRunExecutionClaimLost("token replaced")),
        )[1],
    )
    monkeypatch.setattr(
        ai_tasks,
        "_finalize_scheduled_run_best_effort",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale owner must not finalize the occurrence")
        ),
    )

    result = ai_tasks._run_scheduled(
        SimpleNamespace(),
        "scheduled test",
        "workspace-1",
        lambda: None,
        run_id="run-claim-lost",
        job_id_str="job-1",
        on_terminal_error=lambda _error: (_ for _ in ()).throw(
            AssertionError("a stale owner must not emit terminal side effects")
        ),
    )

    assert result == {
        "status": "failed",
        "error": "token replaced",
        "claim_lost": True,
    }


@pytest.mark.asyncio
async def test_scheduled_agent_occurrences_create_distinct_tasks(monkeypatch):
    from packages.core.constants.execution import ScheduledDispatchKind
    from packages.core.services import task_service
    from packages.core.tasks import scheduler_tasks

    created = []

    async def create_task(_db, _entity_id, **kwargs):
        task = SimpleNamespace(id=f"task-{len(created) + 1}")
        created.append((task, kwargs))
        return task

    class FakeDb:
        async def flush(self):
            return None

    job = _make_job(
        job_id="job-1",
        name="Overlapping automation",
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id="user-1",
        agent_id="agent-1",
        execution_target={},
        execution_script=None,
        payload_message=None,
        default_delivery_mode="workspace_chat",
        conversation_id=None,
    )
    monkeypatch.setattr(task_service, "create_task", create_task)

    first = await scheduler_tasks._dispatch_agent_task(
        FakeDb(),
        job,
        datetime.now(timezone.utc),
        "first occurrence",
        run_id="run-1",
    )
    second = await scheduler_tasks._dispatch_agent_task(
        FakeDb(),
        job,
        datetime.now(timezone.utc),
        "second occurrence",
        run_id="run-2",
    )

    assert [dispatch["kind"] for dispatch in (first, second)] == [
        ScheduledDispatchKind.AGENT_TASK.value,
        ScheduledDispatchKind.AGENT_TASK.value,
    ]
    assert first["args"] == ["task-1", "agent-1"]
    assert second["args"] == ["task-2", "agent-1"]
    assert first["kwargs"] == {
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
    }
    assert second["kwargs"] == {
        "scheduled_run_id": "run-2",
        "scheduled_job_id": "job-1",
    }
    assert created[0][1]["details"]["scheduled_run_id"] == "run-1"
    assert created[1][1]["details"]["scheduled_run_id"] == "run-2"
    assert created[0][1]["creation_logged_by_system"] is True
    assert created[0][1]["creation_log_metadata"] == {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "scheduled_job_id": "job-1",
        "scheduled_run_id": "run-1",
    }
    assert job.manor_task_id == "task-2"


def test_agent_task_claim_recheck_publish_failure_retries_delivery(monkeypatch):
    from celery.exceptions import Retry

    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    retry_calls = []

    def deny_claim(awaitable):
        awaitable.close()
        return False, None

    def fail_recheck(**_kwargs):
        raise RuntimeError("broker unavailable")

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", deny_claim)
    monkeypatch.setattr(ai_tasks.run_agent_task, "apply_async", fail_recheck)
    monkeypatch.setattr(ai_tasks.run_agent_task, "retry", retry)
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a recoverable broker failure must not terminalize the Task")
        ),
    )

    with pytest.raises(Retry):
        ai_tasks.run_agent_task.run("task-1", "agent-1")

    assert retry_calls[0]["countdown"] == 30
    assert isinstance(retry_calls[0]["exc"], RuntimeError)


@pytest.mark.asyncio
async def test_scheduled_dispatch_commits_before_queue_publish(monkeypatch):
    from packages.core.constants.execution import ScheduledDispatchKind
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    events: list[str] = []
    job = _make_job(
        id="job-db-1",
        entity_id="entity-1",
        schedule_kind="at",
        run_at="2026-08-28T12:00:00+00:00",
        last_run_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
    )
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([Result(job.entity_id), Result(job)])

        async def execute(self, _statement):
            return next(self.results)

        async def commit(self):
            events.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def prepare(*_args, **_kwargs):
        events.append("prepare")
        return dispatch, "run-1"

    def publish(prepared):
        assert prepared == dispatch
        events.append("publish")

    async def mark_published(run_id, prepared):
        assert (run_id, prepared) == ("run-1", dispatch)
        events.append("mark_published")

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", prepare)
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        mark_published,
    )

    await scheduler_tasks._async_dispatch_single(
        "job-db-1",
        "2026-08-28T12:00:00+00:00",
        occurrence_key="at:2026-08-28T12:00:00+00:00",
    )

    assert events == ["prepare", "commit", "publish", "mark_published"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_schedule_kind", "current_run_at"),
    [
        ("at", "2026-08-29T12:00:00+00:00"),
        ("cron", None),
        ("interval", None),
    ],
)
async def test_scheduled_dispatch_ignores_stale_one_shot_after_reschedule(
    monkeypatch,
    current_schedule_kind,
    current_run_at,
):
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    job = SimpleNamespace(
        id="job-db-1",
        job_id="one-shot-job",
        entity_id="entity-1",
        enabled=True,
        schedule_kind=current_schedule_kind,
        run_at=current_run_at,
        timezone="UTC",
        every_seconds=60,
        delete_after_run=False,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([Result(job.entity_id), Result(job)])

        async def execute(self, _statement):
            return next(self.results)

        async def commit(self):
            raise AssertionError("a stale occurrence must not commit dispatch state")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("a stale occurrence must not create a ScheduledJobRun")

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", unexpected_dispatch)

    result = await scheduler_tasks._async_dispatch_single(
        job.id,
        "2026-08-28T12:00:00+00:00",
        occurrence_key="at:2026-08-28T12:00:00+00:00",
    )

    assert result is None


@pytest.mark.asyncio
async def test_scheduled_dispatch_ignores_stale_recurring_key_after_future_one_shot_update(
    monkeypatch,
):
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    queued_job = _make_job(
        job_id="rescheduled-job",
        schedule_kind="interval",
        every_seconds=60,
    )
    current_job = _make_job(
        id="job-db-1",
        job_id=queued_job.job_id,
        entity_id="entity-1",
        schedule_kind="at",
        run_at="2026-08-29T12:00:00+00:00",
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([Result(current_job.entity_id), Result(current_job)])

        async def execute(self, _statement):
            return next(self.results)

        async def commit(self):
            raise AssertionError("a stale recurring occurrence must not commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("a stale recurring occurrence must not execute")

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", unexpected_dispatch)

    await scheduler_tasks._async_dispatch_single(
        current_job.id,
        "2026-08-28T12:00:00+00:00",
        occurrence_key="interval:60:123",
    )


def test_one_shot_occurrence_becomes_stale_after_schedule_kind_change():
    from packages.core.tasks import scheduler_tasks

    queued_key = "at:2026-08-28T12:00:00+00:00"
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for job in (
        _make_job(schedule_kind="cron", cron_expr="0 12 * * *"),
        _make_job(schedule_kind="interval", every_seconds=3600),
    ):
        assert scheduler_tasks._is_one_shot_occurrence_key(job, queued_key)
        assert not scheduler_tasks._scheduled_one_shot_occurrence_is_current(
            job,
            now,
            queued_key,
        )


@pytest.mark.asyncio
async def test_published_projection_failure_queues_projection_recovery(monkeypatch):
    from packages.core.constants.execution import ScheduledDispatchKind
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    job = _make_job(
        id="job-db-1",
        entity_id="entity-1",
        schedule_kind="at",
        run_at="2026-08-28T12:00:00+00:00",
        last_run_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
    )
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )
    queued: list[dict] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([Result(job.entity_id), Result(job)])

        async def execute(self, _statement):
            return next(self.results)

        async def commit(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def prepare(*_args, **_kwargs):
        return dispatch, "run-1"

    async def fail_projection(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", prepare)
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", lambda _d: None)
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        fail_projection,
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "apply_async",
        lambda **kwargs: queued.append(kwargs),
    )

    await scheduler_tasks._async_dispatch_single(
        "job-db-1",
        "2026-08-28T12:00:00+00:00",
        occurrence_key="at:2026-08-28T12:00:00+00:00",
    )

    assert queued == [{"args": ["run-1"], "countdown": 60}]


@pytest.mark.asyncio
async def test_stale_prepared_dispatch_is_queued_for_recovery(monkeypatch):
    from packages.core.constants.execution import (
        SCHEDULED_DISPATCH_RECOVERY_DELAY_SECONDS,
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )
    prepared = SimpleNamespace(
        id="run-prepared",
        job_id="job-recovery",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
        },
    )
    published = SimpleNamespace(
        id="run-published",
        job_id="job-recovery",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
        },
    )
    terminal_prepared = SimpleNamespace(
        id="run-terminal-prepared",
        job_id="job-recovery",
        status="completed",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
        },
    )
    queued: list[str] = []

    class Scalars:
        def all(self):
            return [prepared, published, terminal_prepared]

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

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda run_id: queued.append(run_id),
    )

    count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    assert count == 3
    assert queued == [
        "run-prepared",
        "run-published",
        "run-terminal-prepared",
    ]
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.DISPATCH
    )
    expected_next_attempt = now + timedelta(
        seconds=SCHEDULED_DISPATCH_RECOVERY_DELAY_SECONDS
    )
    assert prepared.result[recovery_key] == pytest.approx(
        expected_next_attempt.timestamp()
    )
    assert terminal_prepared.result[recovery_key] == pytest.approx(
        expected_next_attempt.timestamp()
    )
    execution_recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.EXECUTION
    )
    assert published.result[execution_recovery_key] == pytest.approx(
        (
            now
            + timedelta(
                seconds=(
                    scheduler_tasks.SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
                )
            )
        ).timestamp()
    )


def test_recovery_deadline_treats_invalid_calendar_values_as_due():
    from packages.core.constants.execution import ScheduledRecoveryKind
    from packages.core.services.scheduled_run_lifecycle import (
        normalize_scheduled_recovery_deadline,
        scheduled_recovery_is_due,
        scheduled_recovery_next_attempt_at_key,
    )

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    key = scheduled_recovery_next_attempt_at_key(ScheduledRecoveryKind.DISPATCH)
    run = SimpleNamespace(result={key: "9999-99-99T99:99:99+00:00"})

    assert scheduled_recovery_is_due(
        run,
        kind=ScheduledRecoveryKind.DISPATCH,
        now=now,
    ) is True
    assert normalize_scheduled_recovery_deadline(
        run,
        kind=ScheduledRecoveryKind.DISPATCH,
    ) is False
    run.result[key] = 10**20
    assert scheduled_recovery_is_due(
        run,
        kind=ScheduledRecoveryKind.DISPATCH,
        now=now,
    ) is True


def test_legacy_recovery_deadline_is_normalized_to_numeric_epoch():
    from packages.core.constants.execution import ScheduledRecoveryKind
    from packages.core.services.scheduled_run_lifecycle import (
        normalize_scheduled_recovery_deadline,
        scheduled_recovery_is_due,
        scheduled_recovery_next_attempt_at_key,
    )

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(hours=1)
    key = scheduled_recovery_next_attempt_at_key(ScheduledRecoveryKind.SETTLEMENT)
    run = SimpleNamespace(result={key: deadline.isoformat()})

    assert normalize_scheduled_recovery_deadline(
        run,
        kind=ScheduledRecoveryKind.SETTLEMENT,
    ) is True
    assert run.result[key] == pytest.approx(deadline.timestamp())
    assert scheduled_recovery_is_due(
        run,
        kind=ScheduledRecoveryKind.SETTLEMENT,
        now=now,
    ) is False


def test_terminal_prepared_recovery_projects_without_republishing(monkeypatch):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
    )
    from packages.core.tasks import scheduler_tasks

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )
    recovery = {
        "action": ScheduledDispatchRecoveryAction.PROJECT_ONLY.value,
        "dispatch": dispatch,
    }
    projected = []

    async def load(_run_id):
        return recovery

    async def project(run_id, prepared):
        projected.append((run_id, prepared))

    monkeypatch.setattr(
        scheduler_tasks,
        "_load_prepared_scheduled_dispatch",
        load,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        project,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_publish_scheduled_dispatch",
        lambda _dispatch: (_ for _ in ()).throw(
            AssertionError("terminal recovery must not republish child work")
        ),
    )

    result = scheduler_tasks._recover_prepared_scheduled_dispatch_task.run(
        "run-terminal-prepared"
    )

    assert result == {
        "run_id": "run-terminal-prepared",
        "status": "published",
        "recovery_action": ScheduledDispatchRecoveryAction.PROJECT_ONLY.value,
    }
    assert projected == [("run-terminal-prepared", dispatch)]


def test_admitted_prepared_recovery_projects_without_republishing():
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.GOAL_MEASUREMENT,
        args=["goal-1"],
    )
    run = SimpleNamespace(
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
        },
    )

    recovery = scheduler_tasks._prepared_scheduled_dispatch_recovery(run)

    assert recovery == {
        "action": ScheduledDispatchRecoveryAction.PROJECT_ONLY.value,
        "dispatch": dispatch,
    }


def test_published_admitted_generic_recovery_never_replays_business_work():
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    run = SimpleNamespace(
        status="running",
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.MORNING_BRIEFING,
                args=["workspace-1"],
            ),
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
        },
    )

    assert scheduler_tasks._prepared_scheduled_dispatch_recovery(run) == {
        "action": ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value,
        "dispatch": run.result["dispatch"],
    }


def test_queued_dispatch_recovery_does_not_close_running_generic_body(monkeypatch):
    from packages.core.constants.execution import (
        ScheduledDispatchRecoveryAction,
    )
    from packages.core.tasks import scheduler_tasks

    recovery = {
        "action": ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value,
        "dispatch": {
            "kind": "morning_briefing",
            "args": ["workspace-1"],
            "kwargs": {},
        },
    }

    async def load(_run_id):
        return recovery

    monkeypatch.setattr(scheduler_tasks, "_load_prepared_scheduled_dispatch", load)
    monkeypatch.setattr(
        scheduler_tasks,
        "_publish_scheduled_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous generic work must not be replayed")
        ),
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("only the age-gated sweep may close the run")
        ),
    )

    result = scheduler_tasks._recover_prepared_scheduled_dispatch_task.run(
        "run-ambiguous",
    )

    assert result == {
        "run_id": "run-ambiguous",
        "status": "awaiting_execution_reconciliation",
        "recovery_action": (
            ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value
        ),
    }


@pytest.mark.parametrize(
    "dispatch_kind",
    [
        "agent_task",
        "workflow",
    ],
)
def test_published_admitted_durable_child_remains_recoverable(dispatch_kind):
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    kind = ScheduledDispatchKind(dispatch_kind)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        kind,
        args=["child-1"],
    )
    run = SimpleNamespace(
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
        },
    )

    assert scheduler_tasks._prepared_scheduled_dispatch_recovery(run) == {
        "action": ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION.value,
        "dispatch": dispatch,
    }


def test_agent_execution_recovery_publishes_one_non_recursive_claim_recheck(
    monkeypatch,
):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
    )
    from packages.core.tasks import scheduler_tasks

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
        kwargs={
            "scheduled_run_id": "run-1",
            "scheduled_job_id": "job-1",
        },
    )
    recovery = {
        "action": ScheduledDispatchRecoveryAction.REPUBLISH_EXECUTION.value,
        "dispatch": dispatch,
    }
    published = []

    async def load(_run_id):
        return recovery

    async def project(_run_id, _dispatch):
        return None

    monkeypatch.setattr(
        scheduler_tasks,
        "_load_prepared_scheduled_dispatch",
        load,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        project,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_publish_scheduled_dispatch",
        lambda prepared: published.append(prepared),
    )

    scheduler_tasks._recover_prepared_scheduled_dispatch_task.run("run-1")

    assert published == [{
        "kind": ScheduledDispatchKind.AGENT_TASK.value,
        "args": ["task-1", "agent-1"],
        "kwargs": {
            "scheduled_run_id": "run-1",
            "scheduled_job_id": "job-1",
            "claim_recheck": True,
        },
    }]
    assert "claim_recheck" not in dispatch["kwargs"]


def test_workflow_execution_recovery_uses_wire_compatible_header(monkeypatch):
    from packages.core.constants.execution import (
        SCHEDULED_EXECUTION_RECOVERY_HEADER,
        ScheduledDispatchKind,
    )
    from packages.core.tasks import scheduler_tasks

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.WORKFLOW,
        args=["workflow-run-1", "run-1", "job-1"],
    )

    recovered, headers = scheduler_tasks._execution_recovery_dispatch(dispatch)
    published = []

    class WorkflowTask:
        @staticmethod
        def apply_async(**options):
            published.append(options)

    monkeypatch.setattr(
        scheduler_tasks,
        "_scheduled_dispatch_task",
        lambda _kind: WorkflowTask,
    )
    scheduler_tasks._publish_scheduled_dispatch(recovered, headers=headers)

    assert recovered["kwargs"] == {}
    assert headers == {SCHEDULED_EXECUTION_RECOVERY_HEADER: True}
    assert published == [{
        "args": ["workflow-run-1", "run-1", "job-1"],
        "kwargs": {},
        "headers": {SCHEDULED_EXECUTION_RECOVERY_HEADER: True},
    }]
    assert "claim_recheck" not in dispatch["kwargs"]


def test_scheduled_settlement_rejects_nonterminal_outcome():
    from packages.core.constants.execution import ScheduledRunStatus
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunSettlement,
    )

    payload = {
        "version": 1,
        "kind": "generic",
        "child_id": None,
        "outcome": {
            "status": ScheduledRunStatus.RUNNING.value,
            "result": {},
            "error": None,
        },
    }

    assert ScheduledRunSettlement.from_payload(payload) is None
    with pytest.raises(ValueError, match="terminal status"):
        ScheduledRunSettlement.create(
            result={},
            error=None,
            status=ScheduledRunStatus.RUNNING,
        )


@pytest.mark.asyncio
async def test_stale_sweep_terminalizes_ambiguous_generic_without_replay(
    monkeypatch,
):
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.services import (
        scheduler_service,
        workflow_run_execution_claim as claim_service,
    )
    from packages.core.tasks import scheduler_tasks
    import packages.core.ledger.adapters as ledger_adapters

    now = datetime.now(timezone.utc)
    job = SimpleNamespace(job_id="job-ambiguous")
    run = SimpleNamespace(
        id="run-ambiguous",
        job_id=job.job_id,
        status="running",
        created_at=now - timedelta(minutes=20),
        started_at=now - timedelta(minutes=20),
        completed_at=None,
        duration_ms=None,
        error=None,
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.MORNING_BRIEFING,
                args=["workspace-1"],
            ),
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
        },
    )
    recorded = []

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

    async def lock_candidate(_db, _candidate):
        return job, run

    async def reconcile(_db, _job, *, finalized_run_id):
        assert finalized_run_id == run.id
        return False

    async def record_finished(_db, _job, *, run_id, status):
        recorded.append((run_id, status))

    async def no_live_execution_claim(_db, _run_id):
        return False

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        lock_candidate,
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous generic work must never be replayed")
        ),
    )
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        reconcile,
    )
    monkeypatch.setattr(
        claim_service,
        "scheduled_run_has_live_execution_claim",
        no_live_execution_claim,
    )
    monkeypatch.setattr(
        ledger_adapters,
        "record_automation_run_finished",
        record_finished,
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    reason = ScheduledDispatchRecoveryAction.QUARANTINE_AMBIGUOUS.value
    assert handled == 1
    assert run.status == "error"
    assert run.error == reason
    assert run.completed_at is not None
    assert run.result["dispatch_status"] == ScheduledDispatchState.QUARANTINED.value
    assert run.result["scheduled_recovery_error"] == reason
    assert (
        run.result["scheduled_execution_state"]
        == ScheduledChildExecutionState.SETTLED.value
    )
    assert recorded == [(run.id, "error")]


@pytest.mark.asyncio
async def test_stale_sweep_defers_ambiguous_generic_with_live_claim(
    monkeypatch,
):
    from packages.core.constants.execution import (
        SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
        ScheduledRunStatus,
    )
    from packages.core.services import (
        workflow_run_execution_claim as claim_service,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import scheduler_tasks

    now = datetime.now(timezone.utc)
    job = SimpleNamespace(job_id="job-live-generic")
    run = SimpleNamespace(
        id="run-live-generic",
        job_id=job.job_id,
        status="running",
        created_at=now - timedelta(minutes=20),
        started_at=now - timedelta(minutes=20),
        completed_at=None,
        duration_ms=None,
        error=None,
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.MORNING_BRIEFING,
                args=["workspace-1"],
            ),
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.ADMITTED.value,
        },
    )

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

    async def lock_candidate(_db, _candidate):
        return job, run

    async def live_execution_claim(_db, run_id):
        assert run_id == run.id
        return True

    async def unexpected_quarantine(*_args, **_kwargs):
        raise AssertionError("a live execution claim must not be quarantined")

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        lock_candidate,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_quarantine_locked_scheduled_recovery",
        unexpected_quarantine,
    )
    monkeypatch.setattr(
        claim_service,
        "scheduled_run_has_live_execution_claim",
        live_execution_claim,
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    execution_deadline_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.EXECUTION,
    )
    assert handled == 1
    assert run.status == ScheduledRunStatus.RUNNING.value
    assert run.completed_at is None
    assert run.result["dispatch_status"] == ScheduledDispatchState.PUBLISHED.value
    assert run.result[execution_deadline_key] == pytest.approx(
        now.timestamp() + SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
    )


def test_settled_prepared_recovery_projects_without_republishing():
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.GOAL_MEASUREMENT,
        args=["goal-1"],
    )
    run = SimpleNamespace(
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "scheduled_execution_state": ScheduledChildExecutionState.SETTLED.value,
        },
    )

    recovery = scheduler_tasks._prepared_scheduled_dispatch_recovery(run)

    assert recovery == {
        "action": ScheduledDispatchRecoveryAction.PROJECT_ONLY.value,
        "dispatch": dispatch,
    }


def test_unknown_execution_state_never_republishes_prepared_dispatch():
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks

    run = SimpleNamespace(
        status="running",
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.GOAL_MEASUREMENT,
                args=["goal-1"],
            ),
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "scheduled_execution_state": "future_unknown_state",
        },
    )

    assert scheduler_tasks._prepared_scheduled_dispatch_recovery(run) is None


@pytest.mark.asyncio
async def test_stale_sweep_recovers_settlement_without_republishing_child(
    monkeypatch,
):
    from packages.core.constants.execution import (
        SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
        ScheduledChildExecutionState,
        ScheduledRecoveryKind,
        ScheduledSettlementKind,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunSettlement,
        merge_scheduled_run_result,
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    settlement = ScheduledRunSettlement.create(
        kind=ScheduledSettlementKind.GENERIC,
        result={"status": "completed"},
        error=None,
    )
    run = SimpleNamespace(
        id="run-settlement-pending",
        job_id="job-settlement-pending",
        status="running",
        created_at=now - timedelta(minutes=10),
        result=merge_scheduled_run_result(
            {},
            settlement.outcome.result,
            execution_state=ScheduledChildExecutionState.SETTLEMENT_PENDING,
            settlement=settlement,
        ),
    )
    queued = []

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

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_run_settlement",
        lambda **kwargs: queued.append(kwargs),
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("settlement recovery must not republish child work")
        ),
    )

    count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    assert count == 1
    assert queued == [{
        "run_id": run.id,
        "job_id_str": run.job_id,
        "settlement": settlement,
    }]
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.SETTLEMENT
    )
    assert run.result[recovery_key] == pytest.approx(
        (
            now + timedelta(seconds=SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS)
        ).timestamp()
    )


@pytest.mark.asyncio
async def test_stale_sweep_recovers_pending_result_projection(monkeypatch):
    from packages.core.constants.execution import (
        SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS,
        ScheduledRecoveryKind,
        ScheduledResultProjectionState,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-projection-pending",
        job_id="job-projection-pending",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    projection = ScheduledResultProjection.workspace_chat(task_id="task-1")
    assert projection.state is ScheduledResultProjectionState.PENDING
    apply_scheduled_result_projection(run, projection)
    queued = []

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

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda run_id: queued.append(run_id),
    )

    count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    assert count == 1
    assert queued == [run.id]
    assert run.result["scheduled_result_projection"]["recovery_attempts"] == 0
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.RESULT_PROJECTION
    )
    assert run.result[recovery_key] == pytest.approx(
        (
            now
            + timedelta(
                seconds=SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS
            )
        ).timestamp()
    )


@pytest.mark.asyncio
async def test_stale_recovery_commits_claim_before_broker_enqueue(monkeypatch):
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-durable-recovery-claim",
        job_id="job-durable-recovery-claim",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    apply_scheduled_result_projection(
        run,
        ScheduledResultProjection.workspace_chat(task_id="task-1"),
    )
    events = []

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
            events.append("flush")

        async def commit(self):
            events.append("commit")

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    def enqueue(run_id):
        assert run_id == run.id
        events.append("enqueue")

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        enqueue,
    )

    assert await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    ) == 1
    assert events == ["flush", "commit", "enqueue"]


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_resurrect_delivered_projection(monkeypatch):
    from packages.core.constants.execution import ScheduledResultProjectionState
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    stale = SimpleNamespace(
        id="run-stale-projection",
        job_id="job-stale-projection",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    current = SimpleNamespace(
        id=stale.id,
        job_id=stale.job_id,
        status=stale.status,
        created_at=stale.created_at,
        result={},
    )
    projection = ScheduledResultProjection.workspace_chat(task_id="task-1")
    apply_scheduled_result_projection(stale, projection)
    apply_scheduled_result_projection(
        current,
        projection.with_state(ScheduledResultProjectionState.DELIVERED),
    )

    class Scalars:
        def all(self):
            return [stale]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    async def lock_current(_db, candidate):
        assert candidate is stale
        return SimpleNamespace(job_id=current.job_id), current

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        lock_current,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda _run_id: (_ for _ in ()).throw(
            AssertionError("a delivered projection must not be recovered")
        ),
    )

    assert await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    ) == 0
    assert (
        current.result["scheduled_result_projection"]["state"]
        == ScheduledResultProjectionState.DELIVERED.value
    )


@pytest.mark.asyncio
async def test_deferred_settlement_is_not_misclassified_as_invalid_dispatch(
    monkeypatch,
):
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
        ScheduledSettlementKind,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunSettlement,
        defer_scheduled_recovery,
        merge_scheduled_run_result,
    )
    from packages.core.tasks import scheduler_tasks

    now = datetime.now(timezone.utc)
    settlement = ScheduledRunSettlement.create(
        kind=ScheduledSettlementKind.GENERIC,
        result={"status": "completed"},
        error=None,
    )
    run = SimpleNamespace(
        id="run-deferred-settlement",
        job_id="job-deferred-settlement",
        status="running",
        created_at=now - timedelta(minutes=10),
        result=merge_scheduled_run_result(
            {
                "dispatch_status": ScheduledDispatchState.PREPARED.value,
                "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                    ScheduledDispatchKind.WORKFLOW,
                    args=["workflow-run-1", "scheduled-run-1", "job-1"],
                ),
            },
            settlement.outcome.result,
            execution_state=ScheduledChildExecutionState.SETTLEMENT_PENDING,
            settlement=settlement,
        ),
    )
    defer_scheduled_recovery(
        run,
        kind=ScheduledRecoveryKind.SETTLEMENT,
        now=now,
        retry_after=timedelta(hours=1),
    )

    class Scalars:
        def all(self):
            return [run]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_quarantine_locked_scheduled_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a valid deferred settlement must not be quarantined")
        ),
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("settlement pending must not recover dispatch")
        ),
    )

    assert await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    ) == 0


@pytest.mark.asyncio
async def test_projection_publish_failure_does_not_consume_recovery_chain(
    monkeypatch,
):
    from packages.core.constants.execution import (
        SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS,
        SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-projection-broker-failure",
        job_id="job-projection-broker-failure",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    apply_scheduled_result_projection(
        run,
        ScheduledResultProjection.workspace_chat(task_id="task-1"),
    )

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

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    def fail_enqueue(_run_id):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        fail_enqueue,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_quarantine_scheduled_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unaccepted recovery chain must not be quarantined")
        ),
    )

    for attempt in range(SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS + 1):
        handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
            Session(),
            now
            + timedelta(
                seconds=(
                    attempt
                    * (SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS + 1)
                )
            ),
        )
        assert handled == 1

    assert run.result["scheduled_result_projection"]["recovery_attempts"] == 0


@pytest.mark.asyncio
async def test_stale_sweep_quarantines_exhausted_result_projection(monkeypatch):
    from packages.core.constants.execution import (
        SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run-projection-exhausted",
        job_id="job-projection-exhausted",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    projection = ScheduledResultProjection.workspace_chat(task_id="task-1")
    for _ in range(SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS):
        projection = projection.for_retry(error="projection unavailable")
    apply_scheduled_result_projection(run, projection)

    class Scalars:
        def all(self):
            return [run]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    quarantined = []

    async def quarantine(_db, candidate, *, reason):
        quarantined.append((candidate.id, reason))
        return True

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda _run_id: (_ for _ in ()).throw(
            AssertionError("an exhausted projection must not start a new retry chain")
        ),
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_quarantine_locked_scheduled_recovery",
        lambda db, _job, candidate, *, reason: quarantine(
            db,
            candidate,
            reason=reason,
        ),
    )

    count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    assert count == 1
    assert quarantined == [
        (run.id, "result_projection_retry_exhausted"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["running", "completed"])
async def test_published_projection_records_dispatch_ledger_after_accept(
    monkeypatch,
    run_status,
):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database
    import packages.core.ledger.adapters as ledger_adapters

    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )
    run = SimpleNamespace(
        id="run-1",
        job_id="job-key-1",
        status=run_status,
        started_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch_ledger": {
                "occurred_at": "2026-08-28T12:00:00+00:00",
                "revision": 7,
                "experiment_id": "experiment-1",
            },
        },
    )
    job = SimpleNamespace(job_id="job-key-1")
    recorded: list[dict] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = [Result(run), Result(job)]

        async def execute(self, _statement):
            return self.results.pop(0)

        async def commit(self):
            recorded.append({"commit": True})

        async def rollback(self):
            raise AssertionError("published handoff should remain authoritative")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def record_dispatch(_db, current_job, **kwargs):
        assert current_job is job
        assert run.result["dispatch_status"] == ScheduledDispatchState.PUBLISHED.value
        recorded.append(kwargs)

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(
        ledger_adapters,
        "record_automation_dispatched",
        record_dispatch,
    )

    await scheduler_tasks._mark_scheduled_dispatch_published("run-1", dispatch)

    assert recorded == [
        {
            "run_id": "run-1",
            "now": datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            "revision": 7,
            "experiment_id": "experiment-1",
        },
        {"commit": True},
    ]


def test_delete_after_run_occurrence_key_is_stable_within_one_revision():
    from packages.core.tasks import scheduler_tasks

    first = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        schedule_kind="cron",
        cron_expr="* * * * *",
        delete_after_run=True,
    )

    assert scheduler_tasks._is_due(job, first) is True
    first_key = scheduler_tasks._scheduled_occurrence_key(job, first)
    job.last_run_at = first
    assert scheduler_tasks._is_due(job, first + timedelta(minutes=1)) is False
    job.last_run_at = None
    assert scheduler_tasks._scheduled_occurrence_key(
        job,
        first + timedelta(minutes=1),
    ) == first_key
    assert first_key.startswith("scheduled:v2:1:delete-after:")
    assert not scheduler_tasks._is_one_shot_occurrence_key(job, first_key)

    job.revision = 2
    assert scheduler_tasks._scheduled_occurrence_key(job, first) != first_key

    one_shot = _make_job(
        job_id="delete-after-at",
        schedule_kind="at",
        run_at="2026-08-28T12:00:00+00:00",
        delete_after_run=True,
    )
    original_key = scheduler_tasks._scheduled_occurrence_key(one_shot, first)
    assert (
        scheduler_tasks._scheduled_occurrence_key(
            one_shot,
            first + timedelta(hours=1),
        )
        == original_key
    )
    one_shot.run_at = "2026-08-29T12:00:00+00:00"
    one_shot.revision += 1
    assert scheduler_tasks._scheduled_occurrence_key(one_shot, first) != original_key


def test_versioned_occurrence_rejects_aba_schedule_update():
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        schedule_kind="interval",
        every_seconds=60,
        last_run_at=now,
        revision=4,
    )
    queued_key = scheduler_tasks._scheduled_occurrence_key(job, now)

    # The schedule returned to the same value, but its persisted revision did
    # not. A queued A occurrence must not execute against the later A config.
    job.revision = 6
    assert not scheduler_tasks._scheduled_occurrence_is_current(
        job,
        now,
        queued_key,
    )


def test_legacy_recurring_occurrence_requires_the_original_dispatch_clock():
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        schedule_kind="interval",
        every_seconds=60,
        last_run_at=now,
    )
    queued_key = scheduler_tasks._legacy_scheduled_occurrence_key(job, now)
    assert scheduler_tasks._scheduled_occurrence_is_current(
        job,
        now,
        queued_key,
    )

    job.last_run_at = None
    assert not scheduler_tasks._scheduled_occurrence_is_current(
        job,
        now,
        queued_key,
    )


def test_legacy_occurrence_waits_for_old_beat_dispatch_clock():
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        schedule_kind="interval",
        every_seconds=60,
        last_run_at=now - timedelta(minutes=2),
    )
    legacy_key = scheduler_tasks._legacy_scheduled_occurrence_key(job, now)

    assert scheduler_tasks._legacy_scheduled_occurrence_clock_pending(
        job,
        now,
        legacy_key,
    )

    # Once old Beat commits its clock the same delivery is current, not pending.
    job.last_run_at = now
    assert not scheduler_tasks._legacy_scheduled_occurrence_clock_pending(
        job,
        now,
        legacy_key,
    )

    # Versioned keys and legacy keys for another config are conclusively stale.
    versioned_key = scheduler_tasks._scheduled_occurrence_key(job, now)
    assert not scheduler_tasks._legacy_scheduled_occurrence_clock_pending(
        job,
        now,
        versioned_key,
    )
    job.last_run_at = now - timedelta(minutes=2)
    job.every_seconds = 300
    assert not scheduler_tasks._legacy_scheduled_occurrence_clock_pending(
        job,
        now,
        legacy_key,
    )


@pytest.mark.asyncio
async def test_new_worker_retries_legacy_message_until_old_beat_clock_commits(
    monkeypatch,
):
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        id="job-db-1",
        schedule_kind="interval",
        every_seconds=60,
        last_run_at=now - timedelta(minutes=2),
    )
    legacy_key = scheduler_tasks._legacy_scheduled_occurrence_key(job, now)

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([Result(job.entity_id), Result(job)])

        async def execute(self, _statement):
            return next(self.results)

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("worker must wait for the old Beat transaction")

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", unexpected_dispatch)

    with pytest.raises(scheduler_tasks._ScheduledOccurrenceClockPending):
        await scheduler_tasks._async_dispatch_single(
            job.id,
            now.isoformat(),
            occurrence_key=legacy_key,
        )


def test_legacy_clock_uses_dedicated_retry_budget(monkeypatch):
    from celery.exceptions import Retry

    from packages.core.tasks import scheduler_tasks

    retry_calls = []

    async def pending(*_args, **_kwargs):
        raise scheduler_tasks._ScheduledOccurrenceClockPending("clock pending")

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(scheduler_tasks, "_async_dispatch_single", pending)
    monkeypatch.setattr(scheduler_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(scheduler_tasks._dispatch_job_task, "retry", retry)

    scheduler_tasks._dispatch_job_task.push_request(
        retries=scheduler_tasks._dispatch_job_task.max_retries,
        id="legacy-clock-wait",
    )
    try:
        with pytest.raises(Retry):
            scheduler_tasks._dispatch_job_task.run(
                "job-db-legacy",
                "2026-08-28T12:00:00+00:00",
                occurrence_key="interval:test-job-1:20260828T120000Z",
            )
    finally:
        scheduler_tasks._dispatch_job_task.pop_request()

    assert len(retry_calls) == 1
    assert retry_calls[0]["countdown"] == (
        scheduler_tasks._LEGACY_CLOCK_PENDING_RETRY_SECONDS
    )
    assert retry_calls[0]["max_retries"] == (
        scheduler_tasks._LEGACY_CLOCK_PENDING_MAX_RETRIES
    )
    assert isinstance(
        retry_calls[0]["exc"],
        scheduler_tasks._ScheduledOccurrenceClockPending,
    )


def test_legacy_clock_wait_exhaustion_revalidates_and_consumes(monkeypatch):
    from packages.core.tasks import scheduler_tasks

    attempts = []

    async def dispatch(*_args, **kwargs):
        allow_fallback = kwargs.get("allow_legacy_uncommitted_clock", False)
        attempts.append(allow_fallback)
        if not allow_fallback:
            raise scheduler_tasks._ScheduledOccurrenceClockPending(
                "clock still pending"
            )

    async def unexpected_terminal_failure(**_kwargs):
        raise AssertionError("successful fallback must not record exhaustion")

    monkeypatch.setattr(scheduler_tasks, "_async_dispatch_single", dispatch)
    monkeypatch.setattr(
        scheduler_tasks,
        "_record_dispatch_exhaustion",
        unexpected_terminal_failure,
    )
    monkeypatch.setattr(scheduler_tasks, "_run_async", asyncio.run)

    scheduler_tasks._dispatch_job_task.push_request(
        retries=scheduler_tasks._LEGACY_CLOCK_PENDING_MAX_RETRIES,
        id="legacy-clock-fallback",
    )
    try:
        scheduler_tasks._dispatch_job_task.run(
            "job-db-legacy",
            "2026-08-28T12:00:00+00:00",
            occurrence_key="interval:test-job-1:20260828T120000Z",
        )
    finally:
        scheduler_tasks._dispatch_job_task.pop_request()

    assert attempts == [False, True]


def test_legacy_delete_after_occurrence_requires_the_original_dispatch_clock():
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        job_id="legacy-delete-after-at",
        schedule_kind="at",
        run_at="2026-08-28T12:00:00+00:00",
        delete_after_run=True,
        last_run_at=now,
    )
    legacy_key = f"delete-after:{job.job_id}"

    assert scheduler_tasks._scheduled_one_shot_occurrence_is_current(
        job,
        now,
        legacy_key,
    ) is True

    job.last_run_at = now + timedelta(seconds=1)
    assert scheduler_tasks._scheduled_one_shot_occurrence_is_current(
        job,
        now,
        legacy_key,
    ) is False


def test_interim_delete_after_at_key_is_still_accepted_by_current_workers():
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        job_id="interim-delete-after-at",
        schedule_kind="at",
        run_at="2026-08-28T12:00:00+00:00",
        delete_after_run=True,
        last_run_at=now,
    )
    interim_key = (
        f"delete-after:{job.job_id}:at:2026-08-28T12:00:00+00:00"
    )

    assert scheduler_tasks._scheduled_one_shot_occurrence_is_current(
        job,
        now,
        interim_key,
    ) is True

    job.run_at = "2026-08-29T12:00:00+00:00"
    assert scheduler_tasks._scheduled_one_shot_occurrence_is_current(
        job,
        now,
        interim_key,
    ) is False


@pytest.mark.asyncio
async def test_stale_recovery_backoff_rotates_past_oldest_batch(
    client,
    db_session,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    now = datetime.now(timezone.utc)
    jobs = []
    runs = []
    for index in range(scheduler_tasks._PREPARED_DISPATCH_RECOVERY_BATCH + 1):
        job_id = f"projection-backoff-{index}"
        jobs.append(
            ScheduledJob(
                id=generate_ulid(),
                job_id=job_id,
                name=f"Projection recovery {index}",
            )
        )
        run = ScheduledJobRun(
            id=generate_ulid(),
            job_id=job_id,
            status="success",
            created_at=now - timedelta(minutes=10),
            result={},
        )
        apply_scheduled_result_projection(
            run,
            ScheduledResultProjection.workspace_chat(task_id=generate_ulid()),
        )
        runs.append(run)
    db_session.add_all([*jobs, *runs])
    await db_session.commit()

    queued = []
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda run_id: queued.append(run_id),
    )

    first_count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now,
    )
    await db_session.commit()
    second_count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now + timedelta(minutes=1),
    )
    await db_session.commit()

    assert first_count == scheduler_tasks._PREPARED_DISPATCH_RECOVERY_BATCH
    assert second_count == 1
    assert set(queued) == {run.id for run in runs}


@pytest.mark.asyncio
async def test_projection_backoff_does_not_block_due_dispatch_recovery(
    client,
    db_session,
    monkeypatch,
):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
        defer_scheduled_recovery,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    now = datetime.now(timezone.utc)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=[generate_ulid(), generate_ulid()],
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=f"independent-recovery-{generate_ulid()}",
        status="success",
        created_at=now - timedelta(minutes=10),
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
        },
    )
    apply_scheduled_result_projection(
        run,
        ScheduledResultProjection.workspace_chat(task_id=generate_ulid()),
    )
    defer_scheduled_recovery(
        run,
        kind=ScheduledRecoveryKind.RESULT_PROJECTION,
        now=now,
        retry_after=timedelta(hours=1),
    )
    db_session.add_all(
        [
            ScheduledJob(
                id=generate_ulid(),
                job_id=run.job_id,
                name="Independent recovery",
            ),
            run,
        ]
    )
    await db_session.commit()

    queued = []
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the deferred projection phase must remain deferred")
        ),
    )
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda run_id: queued.append(run_id),
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now,
    )

    assert handled == 1
    assert queued == [run.id]


@pytest.mark.asyncio
async def test_idle_tick_commits_terminal_invalid_settlement_quarantine(
    client,
    db_session,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.database as database
    from packages.core.constants.execution import (
        ScheduledChildExecutionState,
        ScheduledDispatchState,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks import scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"invalid-settlement-{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Invalid settlement",
        enabled=False,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        result={
            "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
            "scheduled_execution_state": (
                ScheduledChildExecutionState.SETTLEMENT_PENDING.value
            ),
            "scheduled_settlement": {"version": 999},
        },
    )
    db_session.add_all([job, run])
    await db_session.commit()
    run_id = run.id

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def no_missed_runs(_db, _now):
        return 0

    monkeypatch.setattr(database, "create_worker_session", lambda: sessions)
    monkeypatch.setattr(scheduler_tasks, "_maybe_scan_missed_runs", no_missed_runs)

    await scheduler_tasks._async_tick()

    db_session.expire_all()
    persisted = await db_session.get(ScheduledJobRun, run_id)
    assert persisted.status == "success"
    assert (
        persisted.result["scheduled_execution_state"]
        == ScheduledChildExecutionState.SETTLED.value
    )
    assert "scheduled_settlement" not in persisted.result
    assert (
        persisted.result["dispatch_status"]
        == ScheduledDispatchState.PUBLISHED.value
    )
    assert persisted.result["scheduled_recovery_error"] == "invalid_settlement_payload"


@pytest.mark.asyncio
async def test_tick_scans_missed_runs_after_recovery_transaction(monkeypatch):
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    events: list[str] = []

    class Scalars:
        def all(self):
            return []

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            events.append("load_candidates")
            return Result()

        async def commit(self):
            events.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def recover(_db, _now):
        events.append("recovery")
        return 0

    async def scan_missed(_db, _now):
        events.append("missed")
        return 1

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(
        scheduler_tasks,
        "_queue_stale_prepared_dispatch_recovery",
        recover,
    )
    monkeypatch.setattr(scheduler_tasks, "_maybe_scan_missed_runs", scan_missed)

    await scheduler_tasks._async_tick()

    assert events == ["recovery", "missed", "commit", "load_candidates"]


@pytest.mark.asyncio
async def test_tick_keyset_pages_all_candidates_without_cron_starvation(monkeypatch):
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database

    pages = iter([
        [
            _make_job(id="job-a", schedule_kind="cron", cron_expr="* * * * *"),
            _make_job(id="job-b", schedule_kind="cron", cron_expr="* * * * *"),
        ],
        [_make_job(id="job-c", schedule_kind="cron", cron_expr="* * * * *")],
        [],
    ])
    commits = 0
    prepared_ids: list[str] = []
    published_ids: list[str] = []

    class Scalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return Scalars(self.values)

    class Session:
        async def execute(self, _statement):
            return Result(next(pages))

        async def commit(self):
            nonlocal commits
            commits += 1

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def no_recovery(_db, _now):
        return 0

    async def no_missed(_db, _now):
        return 0

    async def prepare(job_id, _now):
        prepared_ids.append(job_id)
        return {
            "kind": "scheduler_parent",
            "args": [job_id],
            "kwargs": {},
        }, f"run-{job_id}"

    def publish(dispatch):
        published_ids.append(dispatch["args"][0])

    async def mark_published(_run_id, _dispatch):
        return None

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(
        scheduler_tasks,
        "_queue_stale_prepared_dispatch_recovery",
        no_recovery,
    )
    monkeypatch.setattr(scheduler_tasks, "_maybe_scan_missed_runs", no_missed)
    monkeypatch.setattr(
        scheduler_tasks,
        "_prepare_scheduler_parent_dispatch",
        prepare,
    )
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    monkeypatch.setattr(
        scheduler_tasks,
        "_mark_scheduled_dispatch_published",
        mark_published,
    )

    await scheduler_tasks._async_tick()

    assert prepared_ids == ["job-a", "job-b", "job-c"]
    assert published_ids == prepared_ids
    # One durability boundary before scanning, then one per non-empty page.
    assert commits == 3


@pytest.mark.asyncio
async def test_failed_missed_scan_rolls_back_savepoint_and_remains_retryable(
    monkeypatch,
):
    from packages.core.tasks import scheduler_tasks

    events = []
    now = datetime.now(timezone.utc)

    class Savepoint:
        async def __aenter__(self):
            events.append("savepoint")
            return self

        async def __aexit__(self, exc_type, *_args):
            events.append("rollback" if exc_type else "release")
            return False

    class Session:
        def begin_nested(self):
            return Savepoint()

    attempts = 0

    async def scan(_db, _now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("ledger write failed")
        return 2

    monkeypatch.setattr(scheduler_tasks, "_scan_missed_runs", scan)

    assert await scheduler_tasks._maybe_scan_missed_runs(Session(), now) == 0
    assert await scheduler_tasks._maybe_scan_missed_runs(Session(), now) == 2
    assert await scheduler_tasks._maybe_scan_missed_runs(Session(), now) == 2
    assert events == [
        "savepoint",
        "rollback",
        "savepoint",
        "release",
        "savepoint",
        "release",
    ]


@pytest.mark.asyncio
async def test_stale_sweep_quarantines_invalid_recovery_payload(monkeypatch):
    from packages.core.constants.execution import ScheduledDispatchState
    from packages.core.tasks import scheduler_tasks

    now = datetime.now(timezone.utc)
    invalid = SimpleNamespace(
        id="run-invalid",
        job_id="job-invalid",
        status="running",
        created_at=now - timedelta(minutes=10),
        result={
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch": {"kind": "removed_kind", "args": [], "kwargs": {}},
        },
    )
    quarantined = []

    class Scalars:
        def all(self):
            return [invalid]

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    monkeypatch.setattr(
        scheduler_tasks,
        "_lock_scheduled_recovery_candidate",
        _reuse_scheduled_recovery_candidate,
    )

    async def quarantine(_db, run, *, reason):
        quarantined.append((run.id, reason))
        return True

    monkeypatch.setattr(
        scheduler_tasks,
        "_quarantine_locked_scheduled_recovery",
        lambda db, _job, candidate, *, reason: quarantine(
            db,
            candidate,
            reason=reason,
        ),
    )

    count = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        Session(),
        now,
    )

    assert count == 1
    assert quarantined == [("run-invalid", "invalid_dispatch_payload")]


@pytest.mark.asyncio
async def test_duplicate_occurrence_recovers_only_prepared_dispatch(monkeypatch):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks
    import packages.core.services.scheduler_service as scheduler_service

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.WORKFLOW,
        args=["workflow-run-1", "scheduled-run-1", "job-1"],
    )
    run = SimpleNamespace(
        id="scheduled-run-1",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
        },
    )

    async def existing_claim(*_args, **_kwargs):
        return run, False

    monkeypatch.setattr(scheduler_service, "claim_job_run", existing_claim)

    prepared = await scheduler_tasks._dispatch_job(
        object(),
        _make_job(execution_type="workflow"),
        now,
        occurrence_key="cron:2026-08-28T12:00:00+00:00",
    )

    assert prepared == (dispatch, "scheduled-run-1")


@pytest.mark.asyncio
async def test_dispatch_exhaustion_terminalizes_prepared_run(monkeypatch):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.tasks import scheduler_tasks
    import packages.core.database as database
    import packages.core.ledger.adapters as ledger_adapters
    import packages.core.services.scheduler_service as scheduler_service

    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=["task-1", "agent-1"],
    )
    run = SimpleNamespace(
        id="scheduled-run-1",
        status="running",
        result={
            "dispatch": dispatch,
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
        },
        error=None,
        completed_at=None,
        duration_ms=None,
    )
    job = _make_job(
        id="job-db-1",
        job_id="job-1",
        schedule_kind="cron",
        last_run_at=now,
    )
    committed: list[str] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar_one(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter([
                Result(job.entity_id),
                Result(job),
                Result(run),
            ])

        async def execute(self, _statement):
            return next(self.results)

        async def flush(self):
            return None

        async def rollback(self):
            raise AssertionError("a prepared run must be terminalized")

        async def commit(self):
            committed.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    async def existing_claim(*_args, **_kwargs):
        return run, False

    async def reconcile(*_args, **_kwargs):
        return False

    async def record_finished(*_args, **_kwargs):
        return None

    monkeypatch.setattr(database, "create_worker_session", lambda: SessionContext)
    monkeypatch.setattr(scheduler_service, "claim_job_run", existing_claim)
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        reconcile,
    )
    monkeypatch.setattr(
        ledger_adapters,
        "record_automation_run_finished",
        record_finished,
    )

    await scheduler_tasks._record_dispatch_exhaustion(
        job_db_id="job-db-1",
        now_iso=now.isoformat(),
        manual=False,
        occurrence_key="cron:2026-08-28T12:00:00+00:00",
        error="broker unavailable",
    )

    assert run.status == "error"
    assert run.error == "broker unavailable"
    assert run.completed_at is not None
    assert committed == ["commit"]


def test_scheduled_dispatch_factory_uses_the_persisted_arguments(monkeypatch):
    from packages.core.constants.execution import ScheduledDispatchKind
    from packages.core.tasks import scheduler_tasks

    calls: list[dict[str, object]] = []

    class Task:
        def apply_async(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        scheduler_tasks,
        "_scheduled_dispatch_task",
        lambda kind: Task() if kind is ScheduledDispatchKind.AGENT_TASK else None,
    )
    scheduler_tasks._publish_scheduled_dispatch(
        scheduler_tasks._prepared_scheduled_dispatch(
            ScheduledDispatchKind.AGENT_TASK,
            args=["task-1", "agent-1"],
            kwargs={"example": True},
        )
    )

    assert calls == [{
        "args": ["task-1", "agent-1"],
        "kwargs": {"example": True},
    }]


def test_dispatch_job_retry_exhaustion_records_terminal_failure(monkeypatch):
    from packages.core.tasks import scheduler_tasks

    recorded: list[dict[str, str | bool | None]] = []

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("worker database unavailable")

    async def record_failure(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(scheduler_tasks, "_async_dispatch_single", fail_dispatch)
    monkeypatch.setattr(
        scheduler_tasks,
        "_record_dispatch_exhaustion",
        record_failure,
    )
    monkeypatch.setattr(scheduler_tasks, "_run_async", asyncio.run)

    scheduler_tasks._dispatch_job_task.push_request(
        retries=2,
        id="dispatch-attempt-3",
    )
    try:
        with pytest.raises(RuntimeError, match="worker database unavailable"):
            scheduler_tasks._dispatch_job_task.run(
                "job-db-1",
                "2026-08-28T12:00:00+00:00",
                occurrence_key="at:2026-08-28T12:00:00+00:00",
            )
    finally:
        scheduler_tasks._dispatch_job_task.pop_request()

    assert recorded == [{
        "job_db_id": "job-db-1",
        "now_iso": "2026-08-28T12:00:00+00:00",
        "manual": False,
        "occurrence_key": "at:2026-08-28T12:00:00+00:00",
        "error": "worker database unavailable",
    }]


def test_dispatch_exhaustion_retries_when_terminal_persistence_fails(monkeypatch):
    from celery.exceptions import Retry

    from packages.core.tasks import scheduler_tasks

    retry_calls: list[dict[str, object]] = []

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("worker database unavailable")

    async def fail_terminal_persistence(**_kwargs):
        raise RuntimeError("database still unavailable")

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(scheduler_tasks, "_async_dispatch_single", fail_dispatch)
    monkeypatch.setattr(
        scheduler_tasks,
        "_record_dispatch_exhaustion",
        fail_terminal_persistence,
    )
    monkeypatch.setattr(scheduler_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(scheduler_tasks._dispatch_job_task, "retry", retry)

    scheduler_tasks._dispatch_job_task.push_request(
        retries=2,
        id="dispatch-attempt-3",
    )
    try:
        with pytest.raises(Retry):
            scheduler_tasks._dispatch_job_task.run(
                "job-db-1",
                "2026-08-28T12:00:00+00:00",
                occurrence_key="at:2026-08-28T12:00:00+00:00",
            )
    finally:
        scheduler_tasks._dispatch_job_task.pop_request()

    assert retry_calls[0]["countdown"] == 60
    assert retry_calls[0]["max_retries"] == 12
    assert retry_calls[0]["kwargs"] == {
        "_terminal_failure_error": "worker database unavailable",
    }


def test_dispatch_terminal_persistence_retry_skips_business_dispatch(monkeypatch):
    from packages.core.tasks import scheduler_tasks

    dispatch_calls = 0
    recorded: list[dict[str, str | bool | None]] = []

    async def unexpected_dispatch(*_args, **_kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1

    async def record_failure(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        scheduler_tasks,
        "_async_dispatch_single",
        unexpected_dispatch,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_record_dispatch_exhaustion",
        record_failure,
    )
    monkeypatch.setattr(scheduler_tasks, "_run_async", asyncio.run)

    scheduler_tasks._dispatch_job_task.push_request(
        retries=3,
        id="dispatch-terminal-persistence-1",
    )
    try:
        with pytest.raises(RuntimeError, match="worker database unavailable"):
            scheduler_tasks._dispatch_job_task.run(
                "job-db-1",
                "2026-08-28T12:00:00+00:00",
                occurrence_key="at:2026-08-28T12:00:00+00:00",
                _terminal_failure_error="worker database unavailable",
            )
    finally:
        scheduler_tasks._dispatch_job_task.pop_request()

    assert dispatch_calls == 0
    assert recorded == [{
        "job_db_id": "job-db-1",
        "now_iso": "2026-08-28T12:00:00+00:00",
        "manual": False,
        "occurrence_key": "at:2026-08-28T12:00:00+00:00",
        "error": "worker database unavailable",
    }]


# ── _cron_matches tests ──


def test_cron_matches_wildcard():
    """'* * * * *' should always match (when no previous run this minute)."""
    now = datetime(2026, 4, 21, 10, 30, 0, tzinfo=timezone.utc)
    assert _cron_matches("* * * * *", now, None) is True


def test_cron_matches_specific():
    """'30 9 * * *' matches at 09:30 but not at 10:30."""
    at_0930 = datetime(2026, 4, 21, 9, 30, 0, tzinfo=timezone.utc)
    at_1030 = datetime(2026, 4, 21, 10, 30, 0, tzinfo=timezone.utc)

    assert _cron_matches("30 9 * * *", at_0930, None) is True
    assert _cron_matches("30 9 * * *", at_1030, None) is False


def test_cron_every_n():
    """'*/5 * * * *' matches minutes 0, 5, 10, ... but not 3, 7, etc."""
    for minute in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55):
        now = datetime(2026, 4, 21, 12, minute, 0, tzinfo=timezone.utc)
        assert _cron_matches("*/5 * * * *", now, None) is True, f"Should match minute {minute}"

    for minute in (1, 3, 7, 13, 22, 59):
        now = datetime(2026, 4, 21, 12, minute, 0, tzinfo=timezone.utc)
        assert _cron_matches("*/5 * * * *", now, None) is False, f"Should NOT match minute {minute}"


def test_cron_matches_lists_and_ranges():
    """Weekly schedules from the UI use comma lists and weekday ranges."""
    monday = datetime(2026, 4, 20, 9, 0, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 4, 22, 9, 0, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 4, 25, 9, 0, 0, tzinfo=timezone.utc)

    assert _cron_matches("0 9 * * 1,3,5", monday, None) is True
    assert _cron_matches("0 9 * * 1,3,5", wednesday, None) is True
    assert _cron_matches("0 9 * * 1-5", wednesday, None) is True
    assert _cron_matches("0 9 * * 1-5", saturday, None) is False


# ── _is_due tests ──


def test_is_due_interval():
    """every_seconds=300: due when elapsed >= 300, not due when < 300."""
    now = datetime(2026, 4, 21, 12, 10, 0, tzinfo=timezone.utc)

    # Never run before — should be due immediately
    job_never_run = _make_job(schedule_kind="every", every_seconds=300)
    assert _is_due(job_never_run, now) is True

    # Last run 5 minutes ago — exactly due
    job_due = _make_job(
        schedule_kind="every",
        every_seconds=300,
        last_run_at=now - timedelta(seconds=300),
    )
    assert _is_due(job_due, now) is True

    # Last run 2 minutes ago — not yet due
    job_not_due = _make_job(
        schedule_kind="every",
        every_seconds=300,
        last_run_at=now - timedelta(seconds=120),
    )
    assert _is_due(job_not_due, now) is False


def test_is_due_accepts_legacy_interval_alias():
    """Older workspace cadence installers stored fixed intervals as 'interval'."""
    now = datetime(2026, 4, 21, 12, 10, 0, tzinfo=timezone.utc)
    job = _make_job(schedule_kind="interval", every_seconds=300)

    assert _is_due(job, now) is True


def test_is_due_cron_uses_job_timezone_los_angeles_dst():
    """'0 9 * * *' in LA should fire at 16:00 UTC during PDT, not 09:00 UTC."""
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)) is True


def test_is_due_cron_uses_job_timezone_los_angeles_standard_time():
    """The same LA 9am cron should shift to 17:00 UTC outside DST."""
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 1, 5, 16, 59, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc)) is True


def test_is_due_cron_does_not_repeat_same_local_minute():
    job = _make_job(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        timezone="America/Los_Angeles",
        last_run_at=datetime(2026, 5, 1, 16, 0, 5, tzinfo=timezone.utc),
    )

    assert _is_due(job, datetime(2026, 5, 1, 16, 0, 30, tzinfo=timezone.utc)) is False


def test_is_due_one_shot_naive_run_at_uses_job_timezone():
    job = _make_job(
        schedule_kind="at",
        run_at="2026-05-01T09:00",
        timezone="America/Los_Angeles",
    )

    assert _is_due(job, datetime(2026, 5, 1, 15, 59, tzinfo=timezone.utc)) is False
    assert _is_due(job, datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)) is True


def test_agent_task_max_turns_stays_default_for_summary_jobs():
    turns = _agent_task_max_turns_for_target({})

    assert turns == 50


def test_agent_task_max_turns_expands_for_video_deliverables():
    turns = _agent_task_max_turns_for_target({"output_kind": "video"})

    assert turns == 50


def test_agent_task_max_turns_does_not_scan_prompt_text():
    turns = _agent_task_max_turns_for_target({"prompt": "Generate a product video."})

    assert turns == 50


def test_scheduled_execution_target_uses_explicit_fields_only():
    from packages.core.ai.runtime.scheduling import _coerce_scheduled_execution_target

    text_only = _coerce_scheduled_execution_target(
        {"prompt": "Generate a product video.", "notes": "Prompt text is not a contract."},
    )
    structured = _coerce_scheduled_execution_target(
        output_kind="video",
        requires_generated_file=True,
        max_turns="18",
    )

    assert text_only == {
        "prompt": "Generate a product video.",
        "notes": "Prompt text is not a contract.",
    }
    assert structured == {
        "output_kind": "video",
        "requires_generated_file": True,
        "max_turns": 18,
    }


def test_agent_task_max_turns_respects_explicit_budget():
    turns = _agent_task_max_turns_for_target({"max_turns": 20, "output_kind": "video"})

    assert turns == 20


def test_file_deliverable_completion_requires_generation_tool_result():
    done_when, deliverable = _tighten_file_deliverable_completion(
        target={"deliverable": {"kind": "video"}},
        done_when="The requested automation has completed.",
        deliverable="A video recap.",
    )

    assert "generate_file" in done_when
    assert "text-only report" in done_when
    assert "terminal failure reason" in done_when
    assert "generated video" in deliverable


def test_scheduled_agent_credit_exhaustion_closes_task_and_run(monkeypatch):
    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.ai.task_runner import TaskRunner
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    marked = []
    finalized = []

    def fail_run(awaitable):
        awaitable.close()
        raise CreditExhaustedError("no credits")

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(TaskRunner, "run", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(ai_tasks, "_run_async", fail_run)
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *args, **_kwargs: finalized.append(args),
    )

    result = ai_tasks.run_agent_task.run("task-credit", "agent-credit")

    assert result["status"] == "failed"
    assert result["error_type"] == "CreditExhaustedError"
    assert marked[0][0] == ("task-credit", "no credits")
    assert finalized[0][0:2] == (session_factory, "task-credit")
    assert finalized[0][2] == result


def test_scheduled_agent_retry_exhaustion_closes_task_and_run(monkeypatch):
    from packages.core.ai.task_runner import TaskRunner
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    marked = []
    finalized = []

    def fail_run(awaitable):
        awaitable.close()
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(TaskRunner, "run", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(ai_tasks, "_run_async", fail_run)
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *args, **_kwargs: finalized.append(args),
    )

    ai_tasks.run_agent_task.push_request(retries=3, id="task-provider-timeout")
    try:
        result = ai_tasks.run_agent_task.run("task-provider-timeout", "agent-timeout")
    finally:
        ai_tasks.run_agent_task.pop_request()

    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeError"
    assert "4 attempts" in marked[0][0][1]
    assert finalized[0][0:2] == (session_factory, "task-provider-timeout")
    assert finalized[0][2] == result


@pytest.mark.asyncio
async def test_task_runtime_surfaces_transient_provider_usage_for_retry(monkeypatch):
    from packages.core.ai.engine import ChatMessage
    from packages.core.ai.runtime import task_agent

    async def fake_agent_chat(**_kwargs):
        return ChatMessage(
            role="assistant",
            content="",
            usage={"error": "HTTP 503: service temporarily unavailable"},
        )

    monkeypatch.setattr(task_agent, "runtime_execute_task_agent_chat", fake_agent_chat)

    with pytest.raises(task_agent.RuntimeTaskProviderError) as raised:
        await task_agent.runtime_execute_task_agent_turn(
            engine=object(),
            messages=[],
            tools=[],
            loaded_tool_names=set(),
            system_prompt="",
            runtime_envelope=None,
            entity_id="entity-provider-retry",
            agent_id=None,
        )

    assert raised.value.retryable is True
    assert "HTTP 503" in str(raised.value)
    assert task_agent.runtime_task_provider_error_is_retryable("HTTP 401") is False


@pytest.mark.asyncio
async def test_task_supervisor_does_not_downgrade_credit_exhaustion(monkeypatch):
    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.ai.runtime import task_agent

    async def exhausted_supervisor(**_kwargs):
        raise CreditExhaustedError("no credits")

    monkeypatch.setattr(
        task_agent,
        "runtime_execute_task_supervisor_chat",
        exhausted_supervisor,
    )

    with pytest.raises(CreditExhaustedError):
        await task_agent.runtime_review_task_agent_output(
            engine=object(),
            task_title="Credit-gated review",
            agent_response="Draft result",
            done_when="Draft is reviewed",
            turns_used=1,
            max_turns=3,
        )


def test_scheduled_agent_rate_limit_exhaustion_closes_task_and_run(monkeypatch):
    from packages.core.ai.llm_client import LLMRateLimited
    from packages.core.ai.task_runner import TaskRunner
    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    session_factory = object()
    marked = []
    finalized = []

    def fail_run(awaitable):
        awaitable.close()
        raise LLMRateLimited(45)

    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(TaskRunner, "run", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(ai_tasks, "_run_async", fail_run)
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *args, **_kwargs: finalized.append(args),
    )

    ai_tasks.run_agent_task.push_request(retries=3, id="task-rate-limited")
    try:
        result = ai_tasks.run_agent_task.run("task-rate-limited", "agent-rate-limited")
    finally:
        ai_tasks.run_agent_task.pop_request()

    assert result["status"] == "failed"
    assert result["error_type"] == "LLMRateLimited"
    assert "4 attempts" in marked[0][0][1]
    assert finalized[0][0:2] == (session_factory, "task-rate-limited")
    assert finalized[0][2] == result


@pytest.mark.parametrize("task_name", ["run_plan", "plan_and_run_task"])
def test_plan_tasks_cooperatively_retry_provider_rate_limits(monkeypatch, task_name):
    from celery.exceptions import Retry
    from packages.core.ai.llm_client import LLMRateLimited
    from packages.core.tasks import ai_tasks

    task = getattr(ai_tasks, task_name)
    retry_calls = []

    def fail_run(awaitable):
        awaitable.close()
        raise LLMRateLimited(45)

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(ai_tasks, "_run_async", fail_run)
    monkeypatch.setattr(task, "retry", retry)

    with pytest.raises(Retry):
        task.run("task-or-plan-id")

    assert retry_calls[0]["countdown"] == 45


@pytest.mark.parametrize(
    ("task_name", "failure_marker"),
    [
        ("run_plan", "_mark_plan_failed"),
        ("plan_and_run_task", "_mark_task_failed"),
    ],
)
def test_plan_rate_limit_exhaustion_closes_runtime(
    monkeypatch, task_name, failure_marker,
):
    from packages.core.ai.llm_client import LLMRateLimited
    from packages.core.tasks import ai_tasks

    task = getattr(ai_tasks, task_name)
    marked = []

    def fail_run(awaitable):
        awaitable.close()
        raise LLMRateLimited(45)

    monkeypatch.setattr(ai_tasks, "_run_async", fail_run)
    monkeypatch.setattr(
        ai_tasks,
        failure_marker,
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )

    task.push_request(retries=task.max_retries, id=f"{task_name}-rate-limit")
    try:
        result = task.run("task-or-plan-id")
    finally:
        task.pop_request()

    assert result["status"] == "failed"
    assert "attempts" in result["error"]
    assert marked[0][0][0] == "task-or-plan-id"
    assert marked[0][1]["error_type"] == "LLMRateLimited"


def test_plan_dispatch_retry_reuses_the_committed_plan(monkeypatch):
    from celery.exceptions import Retry
    from packages.core.tasks import ai_tasks

    awaited = []
    retry_calls = []

    def run_async(awaitable):
        awaited.append(awaitable.cr_code.co_name)
        awaitable.close()
        return "plan-committed", "draft"

    def fail_dispatch(_plan_id):
        raise RuntimeError("broker unavailable")

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise Retry()

    monkeypatch.setattr(ai_tasks, "_run_async", run_async)
    monkeypatch.setattr(ai_tasks.run_plan, "delay", fail_dispatch)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "retry", retry)

    with pytest.raises(Retry):
        ai_tasks.plan_and_run_task.run("task-1")

    assert awaited == ["_go"]
    assert retry_calls[0]["kwargs"] == {
        "existing_plan_id": "plan-committed",
    }


def test_plan_dispatch_retry_skips_replanning_and_recovers_terminally(monkeypatch):
    from packages.core.tasks import ai_tasks

    awaited = []
    marked = []

    def run_async(awaitable):
        awaited.append(awaitable.cr_code.co_name)
        awaitable.close()
        return "plan-committed", "draft"

    monkeypatch.setattr(ai_tasks, "_run_async", run_async)
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda _plan_id: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )
    monkeypatch.setattr(
        ai_tasks,
        "_mark_initial_plan_dispatch_failed",
        lambda *args, **kwargs: marked.append((args, kwargs)) or True,
    )

    ai_tasks.plan_and_run_task.push_request(retries=2, id="dispatch-attempt-3")
    try:
        result = ai_tasks.plan_and_run_task.run(
            "task-1",
            existing_plan_id="plan-committed",
        )
    finally:
        ai_tasks.plan_and_run_task.pop_request()

    assert awaited == ["_load_existing_plan_for_dispatch"]
    assert result["status"] == "needs_attention"
    assert result["plan_id"] == "plan-committed"
    assert marked[0][0][0:2] == ("plan-committed", "broker unavailable")


@pytest.mark.asyncio
async def test_scheduled_job_credit_preflight_carries_owner_scope(monkeypatch):
    from packages.core.tasks import scheduler_tasks

    seen = {}
    session_factory = object()

    async def fake_assert_credit_available(entity_id, *, source, **kwargs):
        seen.update(entity_id=entity_id, source=source, kwargs=kwargs)

    monkeypatch.setattr(
        scheduler_tasks,
        "runtime_assert_credit_available",
        fake_assert_credit_available,
        raising=False,
    )
    monkeypatch.setattr(
        scheduler_tasks,
        "_scheduled_job_credit_session_factory",
        lambda: session_factory,
    )

    job = _make_job(
        entity_id="ENT-SCHEDULED-CREDITS",
        workspace_id="WS-SCHEDULED-CREDITS",
        user_id="USR-SCHEDULED-CREDITS",
        execution_type="agent",
        execution_target={"complexity": "worker"},
    )

    await scheduler_tasks._preflight_scheduled_job_credits(job)

    assert seen == {
        "entity_id": "ENT-SCHEDULED-CREDITS",
        "source": "scheduled_job",
        "kwargs": {
            "user_id": "USR-SCHEDULED-CREDITS",
            "workspace_id": "WS-SCHEDULED-CREDITS",
            "byok": False,
            "session_factory": session_factory,
        },
    }


@pytest.mark.asyncio
async def test_scheduled_job_credit_preflight_prefers_resolved_workspace_scope(monkeypatch):
    from packages.core.tasks import scheduler_tasks

    seen = {}

    async def fake_assert_credit_available(entity_id, *, source, **kwargs):
        seen.update(entity_id=entity_id, source=source, kwargs=kwargs)

    monkeypatch.setattr(scheduler_tasks, "runtime_assert_credit_available", fake_assert_credit_available)

    job = _make_job(
        entity_id="ENT-SCHEDULED-CREDITS",
        workspace_id="WS-STORED-SCOPE",
        user_id="USR-SCHEDULED-CREDITS",
        execution_type="agent",
        execution_target={},
    )

    await scheduler_tasks._preflight_scheduled_job_credits(
        job,
        workspace_id="WS-RESOLVED-SCOPE",
    )

    assert seen["kwargs"]["workspace_id"] == "WS-RESOLVED-SCOPE"


@pytest.mark.asyncio
async def test_credit_preflight_uses_supplied_worker_session_and_classifies_outage(
    monkeypatch,
):
    from packages.core.ai.llm_client import (
        CreditCheckUnavailableError,
        LLMBillingContext,
        _billing_ctx_var,
        _preflight_credit_check,
    )
    import packages.core.budget as budget
    import packages.core.database as database

    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    def worker_session_factory():
        return SessionContext()

    async def fail_budget_check(db, workspace_id):
        assert db is session
        assert workspace_id == "WS-CREDIT-SESSION"
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        database,
        "async_session",
        lambda: (_ for _ in ()).throw(
            AssertionError("global async pool must not be used")
        ),
    )
    monkeypatch.setattr(budget, "check_workspace_budget", fail_budget_check)

    token = _billing_ctx_var.set(
        LLMBillingContext(
            entity_id="ENT-CREDIT-SESSION",
            workspace_id="WS-CREDIT-SESSION",
        )
    )
    try:
        with pytest.raises(
            CreditCheckUnavailableError,
            match="credit_check_unavailable",
        ):
            await _preflight_credit_check(
                session_factory=worker_session_factory,
            )
    finally:
        _billing_ctx_var.reset(token)


@pytest.mark.asyncio
async def test_scheduled_job_credit_exhaustion_stops_before_fanout(monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    job = _make_job(
        entity_id="ENT-SCHEDULED-CREDITS",
        workspace_id=None,
        execution_type="agent",
        execution_target={},
        payload_message="run this",
    )
    run = SimpleNamespace(
        id="scheduled-run-1",
        status="running",
        started_at=now,
        completed_at=None,
        duration_ms=None,
        error=None,
        result=None,
    )

    async def fake_claim_job_run(*args, **kwargs):
        return run, True

    async def fake_effective_config(*args, **kwargs):
        return {}, None, None

    async def fake_preflight(*args, **kwargs):
        raise CreditExhaustedError("no credits")

    class FakeDB:
        async def flush(self):
            return None

    import packages.core.services.scheduler_service as scheduler_service
    import packages.core.ledger.adapters as ledger_adapters
    import packages.core.experiments as experiments

    recorded = []

    async def record_finished(_db, _job, *, run_id, status):
        recorded.append((run_id, status))

    async def reconcile_projection(_db, current_job, *, finalized_run_id):
        assert finalized_run_id == run.id
        current_job.last_status = run.status
        return False

    monkeypatch.setattr(scheduler_service, "claim_job_run", fake_claim_job_run)
    monkeypatch.setattr(experiments, "effective_dispatch_config", fake_effective_config)
    monkeypatch.setattr(scheduler_tasks, "_preflight_scheduled_job_credits", fake_preflight)
    monkeypatch.setattr(
        ledger_adapters,
        "record_automation_run_finished",
        record_finished,
    )
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        reconcile_projection,
    )

    await scheduler_tasks._dispatch_job(FakeDB(), job, now)

    assert run.status == "error"
    assert run.error == "credits_exhausted: no credits"
    assert job.last_status == "error"
    assert recorded == [(run.id, "error")]


@pytest.mark.parametrize(
    ("workflow_status", "expected_finalizations"),
    [
        ("paused", []),
        ("completed", ["completed"]),
        ("cancelled", ["cancelled"]),
    ],
)
def test_scheduled_workflow_only_finalizes_terminal_statuses(
    monkeypatch,
    workflow_status,
    expected_finalizations,
):
    from packages.core.tasks import ai_tasks

    def execute_workflow(coro):
        coro.close()
        return {
            "workflow_run_id": "workflow-run-1",
            "status": workflow_status,
            "execution_outcome": "executed",
        }

    finalized = []
    monkeypatch.setattr(ai_tasks, "_run_async", execute_workflow)
    monkeypatch.setattr(
        ai_tasks,
        "_finalize_scheduled_run_best_effort",
        lambda **kwargs: finalized.append(kwargs["result"]["status"]),
    )

    result = ai_tasks.run_workflow.run(
        "workflow-run-1",
        "scheduled-run-1",
        "scheduled-job-1",
    )

    assert result["status"] == workflow_status
    assert finalized == expected_finalizations


def test_workflow_settlement_replacement_is_not_caught_as_business_failure(
    monkeypatch,
):
    from celery.exceptions import Ignore

    from packages.core.tasks import ai_tasks

    def completed(coro):
        coro.close()
        return {
            "workflow_run_id": "workflow-run-1",
            "status": "completed",
            "execution_outcome": "executed",
        }

    monkeypatch.setattr(ai_tasks, "_run_async", completed)
    monkeypatch.setattr(
        ai_tasks,
        "_finalize_scheduled_run_best_effort",
        lambda **_kwargs: (_ for _ in ()).throw(Ignore("replaced")),
    )
    monkeypatch.setattr(
        ai_tasks.run_workflow,
        "retry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("settlement replacement must not retry WorkflowRunner")
        ),
    )

    with pytest.raises(Ignore):
        ai_tasks.run_workflow.run(
            "workflow-run-1",
            "scheduled-run-1",
            "scheduled-job-1",
        )


def test_scheduled_workflow_rechecks_durable_occurrence(monkeypatch):
    from packages.core.constants.execution import ScheduledChildAdmissionStatus
    from packages.core.ai import workflow_runner
    from packages.core.tasks import ai_tasks

    async def closed_occurrence(*_args, **_kwargs):
        return SimpleNamespace(
            admitted=False,
            status=ScheduledChildAdmissionStatus.CLOSED,
            reason="scheduled_occurrence_missing",
        )

    async def no_artifact(*_args, **_kwargs):
        return False

    class UnexpectedWorkflowRunner:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError(
                "closed scheduled occurrence must not run WorkflowRunner"
            )

    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", closed_occurrence)
    monkeypatch.setattr(
        ai_tasks,
        "_terminalize_suppressed_scheduled_child",
        no_artifact,
    )
    monkeypatch.setattr(workflow_runner, "WorkflowRunner", UnexpectedWorkflowRunner)
    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)

    result = ai_tasks.run_workflow.run(
        "workflow-run-deleted",
        "scheduled-run-deleted",
        "scheduled-job-deleted",
    )

    assert result == {
        "workflow_run_id": "workflow-run-deleted",
        "status": "already_settled",
        "duplicate_suppressed": True,
        "scheduled_occurrence_closed": True,
        "reason": "scheduled_occurrence_missing",
        "child_terminalized": False,
    }


def test_workflow_claim_recheck_does_not_schedule_recursively(monkeypatch):
    from packages.core.constants.execution import (
        SCHEDULED_EXECUTION_RECOVERY_HEADER,
    )
    from packages.core.tasks import ai_tasks

    def claim_held(awaitable):
        awaitable.close()
        return {
            "workflow_run_id": "workflow-run-1",
            "status": "running",
            "execution_outcome": "claim_held_by_live_execution",
        }

    monkeypatch.setattr(ai_tasks, "_run_async", claim_held)
    monkeypatch.setattr(
        ai_tasks.run_workflow,
        "retry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a scheduler recovery must not create a retry chain")
        ),
    )

    ai_tasks.run_workflow.push_request(
        headers={SCHEDULED_EXECUTION_RECOVERY_HEADER: True},
    )
    try:
        result = ai_tasks.run_workflow.run(
            "workflow-run-1",
            "scheduled-run-1",
            "scheduled-job-1",
        )
    finally:
        ai_tasks.run_workflow.pop_request()

    assert result == {
        "workflow_run_id": "workflow-run-1",
        "status": "in_progress",
        "duplicate_suppressed": True,
        "recheck_scheduled": False,
    }


@pytest.mark.asyncio
async def test_scheduled_job_without_entity_id_fails_closed_for_ai_dispatch(monkeypatch):
    from types import SimpleNamespace

    from packages.core.tasks import scheduler_tasks
    import packages.core.services.scheduler_service as scheduler_service
    import packages.core.experiments as experiments

    now = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    job = _make_job(
        entity_id="",
        workspace_id="WS-MISSING-ENTITY",
        execution_type="agent",
        execution_target={},
        payload_message="run this",
    )
    run = SimpleNamespace(
        id="scheduled-run-empty-entity",
        status="running",
        started_at=now,
        completed_at=None,
        duration_ms=None,
        error=None,
        result=None,
    )

    async def fake_claim_job_run(*args, **kwargs):
        return run, True

    async def fake_effective_config(*args, **kwargs):
        return {}, None, None

    called = {"preflight": 0, "fanout": 0}

    async def fake_preflight(*args, **kwargs):
        called["preflight"] += 1

    async def fake_dispatch_agent_task(*args, **kwargs):
        called["fanout"] += 1

    async def reconcile_projection(_db, current_job, *, finalized_run_id):
        assert finalized_run_id == run.id
        current_job.last_status = run.status
        return False

    class FakeDB:
        async def flush(self):
            return None

    monkeypatch.setattr(scheduler_service, "claim_job_run", fake_claim_job_run)
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        reconcile_projection,
    )
    monkeypatch.setattr(experiments, "effective_dispatch_config", fake_effective_config)
    monkeypatch.setattr(scheduler_tasks, "_preflight_scheduled_job_credits", fake_preflight)
    monkeypatch.setattr(scheduler_tasks, "_dispatch_agent_task", fake_dispatch_agent_task)

    await scheduler_tasks._dispatch_job(FakeDB(), job, now)

    assert called == {"preflight": 0, "fanout": 0}
    assert run.status == "error"
    assert run.error == "scheduled job missing entity_id"
    assert job.last_status == "error"


@pytest.mark.asyncio
async def test_missing_goal_disables_job_through_revisioned_factory(monkeypatch):
    from packages.core.services import scheduler_service
    from packages.core.tasks import scheduler_tasks
    import packages.core.experiments as experiments

    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        id="goal-job-pk",
        execution_type="goal_measurement",
        execution_target={"goal_id": "missing-goal"},
        payload_message=None,
        execution_script=None,
        name="Collect missing goal",
        workspace_id=None,
        user_id=None,
        revision=4,
        updated_at=None,
    )
    run = SimpleNamespace(
        id="goal-run-id",
        status="running",
        started_at=now,
        completed_at=None,
        duration_ms=None,
        result=None,
        error=None,
    )
    audit = []

    class Result:
        def scalar_one_or_none(self):
            return None

    class FakeDB:
        async def execute(self, _statement):
            return Result()

        async def flush(self):
            return None

    async def claim_run(*_args, **_kwargs):
        return run, True

    async def effective_config(_db, target):
        return target, None, None

    async def reconcile(_db, current_job, *, finalized_run_id):
        assert finalized_run_id == run.id
        current_job.last_status = run.status
        return False

    async def bump(_db, current_job, **kwargs):
        current_job.revision += 1
        audit.append(kwargs)
        return current_job.revision

    monkeypatch.setattr(scheduler_service, "claim_job_run", claim_run)
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        reconcile,
    )
    monkeypatch.setattr(scheduler_service, "bump_revision", bump)
    monkeypatch.setattr(experiments, "effective_dispatch_config", effective_config)

    await scheduler_tasks._dispatch_job(FakeDB(), job, now)

    assert run.status == "skipped"
    assert run.result == {"skipped": True, "reason": "goal_not_found"}
    assert job.enabled is False
    assert job.revision == 5
    assert job.updated_at is not None
    assert audit == [{
        "patch": {"enabled": False},
        "changed_by_kind": "system",
        "changed_by_id": None,
        "causation_id": run.id,
    }]


@pytest.mark.asyncio
async def test_tick_parent_dispatch_is_durable_before_broker_publish(
    db_session,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.database as database
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchRecoveryAction,
        ScheduledDispatchState,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks import scheduler_tasks

    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"durable-parent:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Durable scheduler parent",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
        revision=3,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: sessions)

    prepared = await scheduler_tasks._prepare_scheduler_parent_dispatch(
        job_id,
        now,
    )
    assert prepared is not None
    dispatch, run_id = prepared
    assert dispatch["kind"] == ScheduledDispatchKind.SCHEDULER_PARENT.value

    db_session.expire_all()
    persisted_job = await db_session.get(ScheduledJob, job_id)
    persisted_run = await db_session.get(ScheduledJobRun, run_id)
    assert persisted_job.last_run_at == now
    assert persisted_job.last_status == "dispatched"
    assert persisted_run.status == "running"
    assert (
        persisted_run.result["dispatch_status"]
        == ScheduledDispatchState.PREPARED.value
    )
    recovery = scheduler_tasks._prepared_scheduled_dispatch_recovery(
        persisted_run
    )
    assert recovery is not None
    assert (
        recovery["action"]
        == ScheduledDispatchRecoveryAction.REPUBLISH_AND_PROJECT.value
    )
    assert recovery["dispatch"] == dispatch


def test_dedicated_scheduler_settlement_tasks_use_one_bounded_retry_chain():
    from packages.core.tasks import ai_tasks
    from packages.core.constants.execution import SCHEDULED_SETTLEMENT_MAX_RETRIES

    assert (
        ai_tasks.settle_scheduled_run.max_retries
        == SCHEDULED_SETTLEMENT_MAX_RETRIES
    )
    assert (
        ai_tasks.settle_scheduled_agent_run.max_retries
        == SCHEDULED_SETTLEMENT_MAX_RETRIES
    )


def test_skill_generation_worker_uses_shared_attempt_budget():
    from packages.core.constants.execution import (
        SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS,
    )
    from packages.core.tasks import ai_tasks

    assert ai_tasks.generate_job_skill.max_retries == (
        SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS - 1
    )


def test_skill_generation_sweep_reopens_failed_broker_handoff(monkeypatch):
    from packages.core.tasks import ai_tasks, scheduler_tasks

    async_calls = 0
    published: list[str] = []

    def run_async(awaitable):
        nonlocal async_calls
        awaitable.close()
        async_calls += 1
        if async_calls == 1:
            return [
                ("job-ok", "first prompt", "First", 3),
                ("job-failed", "second prompt", "Second", 4),
            ]
        return None

    def publish(*, args):
        published.append(args[0])
        if args[0] == "job-failed":
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(scheduler_tasks, "_run_async", run_async)
    monkeypatch.setattr(ai_tasks.generate_job_skill, "apply_async", publish)

    result = scheduler_tasks.scheduled_job_skill_generation_sweep.run()

    assert result == {"claimed": 2, "published": 1}
    assert published == ["job-ok", "job-failed"]
    assert async_calls == 2


@pytest.mark.asyncio
async def test_skill_generation_sweep_does_not_claim_disabled_job(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.tasks.scheduler_tasks import (
        _claim_due_scheduled_job_skill_generations,
    )

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"disabled-skill-generation:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Disabled generation",
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
        enabled=False,
        revision=4,
        skill_generation_revision=4,
        skill_generation_next_attempt_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    claimed = await _claim_due_scheduled_job_skill_generations(limit=100)

    assert all(item[0] != job.id for item in claimed)
    await db_session.refresh(job)
    assert job.skill_generation_revision == 4


def test_schedule_clock_computes_monthly_cron_without_minute_scanning():
    from packages.core.schedule_clock import next_cron_occurrence

    after = datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc)

    assert next_cron_occurrence(
        "0 0 1 * *",
        timezone_name="UTC",
        after=after,
        inclusive=True,
    ) == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def test_schedule_clock_treats_unflushed_default_enabled_as_active():
    from packages.core.schedule_clock import scheduled_job_next_run_at

    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    job = _make_job(
        enabled=None,
        schedule_kind="every",
        every_seconds=3600,
    )

    assert scheduled_job_next_run_at(
        job,
        now=now,
        inclusive=True,
    ) == now


def test_schedule_clock_does_not_replay_last_fired_cron_minute():
    from packages.core.schedule_clock import scheduled_job_next_run_at

    now = datetime(2026, 8, 30, 12, 0, 30, tzinfo=timezone.utc)
    job = _make_job(
        schedule_kind="cron",
        cron_expr="* * * * *",
        last_run_at=datetime(2026, 8, 30, 12, 0, 5, tzinfo=timezone.utc),
    )

    assert scheduled_job_next_run_at(
        job,
        now=now,
        inclusive=True,
    ) == datetime(2026, 8, 30, 12, 1, tzinfo=timezone.utc)


def test_schedule_clock_skips_nonexistent_dst_cron_minute():
    from packages.core.schedule_clock import next_cron_occurrence

    assert next_cron_occurrence(
        "30 2 * * *",
        timezone_name="America/Los_Angeles",
        after=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        inclusive=True,
    ) == datetime(2026, 3, 9, 9, 30, tzinfo=timezone.utc)
