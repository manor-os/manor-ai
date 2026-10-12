"""Durable initial Workspace Task execution intent and recovery."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.task import TaskRuntimeKind, TaskStatus, TaskType
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.task_execution import (
    TASK_EXECUTION_OUTBOX_EVENT,
    resolve_task_execution,
    stage_task_execution_intent,
    task_execution_intent_dedupe_key,
    task_execution_intent_generation,
)
from packages.core.services.task_state_machine import TaskStatusTransitionError

pytestmark = pytest.mark.oss_regression


async def _coordinated_task(db: AsyncSession) -> tuple[Task, AgentSubscription]:
    # The publisher is intentionally shared by Task starts, Plan cycles,
    # approvals, and Sandbox work. This module asserts exact global delivery
    # counts, so isolate all event categories instead of allowing an older
    # due Plan wake from another test file to make those counts order-dependent.
    await db.execute(delete(RuntimeOutboxEvent))
    await db.commit()
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=generate_ulid(),
        service_key="task-owner",
        status="active",
    )
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Durable coordinated task",
        status=TaskStatus.IN_PROGRESS.value,
        status_changed_at=datetime.now(timezone.utc),
        task_type=TaskType.GENERAL.value,
        owner_service_key=subscription.service_key,
        owner_subscription_id=subscription.id,
        delegate_service_keys=[],
    )
    db.add_all([
        Workspace(id=workspace_id, entity_id=entity_id, name="Task outbox test"),
        subscription,
        task,
    ])
    await db.flush()
    return task, subscription


async def _event_for_generation(
    db: AsyncSession,
    *,
    task_id: str,
    generation: str,
) -> RuntimeOutboxEvent:
    return (await db.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.dedupe_key
        == task_execution_intent_dedupe_key(task_id, generation)
    ))).scalar_one()


@pytest.mark.asyncio
async def test_move_to_in_progress_stages_coordinated_execution_intent(
    db_session: AsyncSession,
) -> None:
    from packages.core.services.task_service import move_task

    task, _subscription = await _coordinated_task(db_session)
    task.status = TaskStatus.PENDING.value
    await db_session.commit()

    moved = await move_task(
        db_session,
        task.id,
        task.entity_id,
        TaskStatus.IN_PROGRESS.value,
    )

    assert moved is not None
    assert moved.status == TaskStatus.IN_PROGRESS.value
    event = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalar_one()
    assert event.payload["task_id"] == task.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type",
    [TaskType.INTERACTIVE.value, TaskType.APPROVAL.value],
)
async def test_move_foreground_task_adapters_do_not_stage_background_execution(
    db_session: AsyncSession,
    task_type: str,
) -> None:
    from packages.core.services.task_service import move_task

    task, _subscription = await _coordinated_task(db_session)
    task.task_type = task_type
    task.status = TaskStatus.PENDING.value
    await db_session.commit()

    moved = await move_task(
        db_session,
        task.id,
        task.entity_id,
        TaskStatus.IN_PROGRESS.value,
    )

    assert moved is not None
    assert moved.status == TaskStatus.IN_PROGRESS.value
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
    )) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk_surface", ["status", "update"])
async def test_bulk_start_uses_canonical_factory_and_outbox_for_every_task(
    db_session: AsyncSession,
    bulk_surface: str,
) -> None:
    from packages.core.services.bulk_service import (
        bulk_update_task_status,
        bulk_update_tasks,
    )

    first, subscription = await _coordinated_task(db_session)
    first.status = TaskStatus.PENDING.value
    second = Task(
        id=generate_ulid(),
        entity_id=first.entity_id,
        workspace_id=first.workspace_id,
        title="Second durable coordinated task",
        status=TaskStatus.PENDING.value,
        status_changed_at=datetime.now(timezone.utc),
        task_type=TaskType.GENERAL.value,
        owner_service_key=subscription.service_key,
        owner_subscription_id=subscription.id,
        delegate_service_keys=[],
    )
    db_session.add(second)
    await db_session.commit()

    task_ids = [second.id, first.id]
    if bulk_surface == "status":
        count = await bulk_update_task_status(
            db_session,
            first.entity_id,
            task_ids,
            TaskStatus.IN_PROGRESS.value,
        )
    else:
        count = await bulk_update_tasks(
            db_session,
            first.entity_id,
            task_ids,
            {"status": TaskStatus.IN_PROGRESS.value},
        )

    assert count == 2
    pending_task_ids = set((await db_session.execute(
        select(RuntimeOutboxEvent.aggregate_id).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id.in_(task_ids),
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalars())
    assert pending_task_ids == set(task_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface",
    ["update", "kanban", "bulk_status", "bulk_update"],
)
async def test_workspace_manual_runtime_cannot_enter_in_progress(
    db_session: AsyncSession,
    surface: str,
) -> None:
    """Every status surface fails before writing a runner-less start."""
    from packages.core.services.bulk_service import (
        bulk_update_task_status,
        bulk_update_tasks,
    )
    from packages.core.services.task_service import move_task, update_task

    task, subscription = await _coordinated_task(db_session)
    task.status = TaskStatus.PENDING.value
    subscription.status = "inactive"
    await db_session.commit()
    task_id = task.id
    entity_id = task.entity_id

    with pytest.raises(
        TaskStatusTransitionError,
        match="without one unambiguous active service scope",
    ):
        if surface == "update":
            await update_task(
                db_session,
                task_id,
                entity_id,
                status=TaskStatus.IN_PROGRESS.value,
            )
        elif surface == "kanban":
            await move_task(
                db_session,
                task_id,
                entity_id,
                TaskStatus.IN_PROGRESS.value,
            )
        elif surface == "bulk_status":
            await bulk_update_task_status(
                db_session,
                entity_id,
                [task_id],
                TaskStatus.IN_PROGRESS.value,
            )
        else:
            await bulk_update_tasks(
                db_session,
                entity_id,
                [task_id],
                {"status": TaskStatus.IN_PROGRESS.value},
            )

    # Prove the failed admission did not merely rely on an outer rollback:
    # committing the caller's still-open transaction must persist no status,
    # lifecycle log, or execution intent.
    assert task.status == TaskStatus.PENDING.value
    await db_session.commit()
    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    assert persisted is not None
    assert persisted.status == TaskStatus.PENDING.value
    assert await db_session.scalar(select(func.count()).select_from(
        TaskLog
    ).where(
        TaskLog.task_id == task_id,
        TaskLog.log_type == "status_change",
    )) == 0
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task_id,
    )) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "expected_runtime"),
    [
        (TaskType.INTERACTIVE.value, TaskRuntimeKind.INTERACTIVE_SESSION),
        (TaskType.APPROVAL.value, TaskRuntimeKind.HUMAN_DECISION),
    ],
)
async def test_update_keeps_workspace_foreground_start_adapters(
    db_session: AsyncSession,
    task_type: str,
    expected_runtime: TaskRuntimeKind,
) -> None:
    from packages.core.services.task_service import update_task

    task, subscription = await _coordinated_task(db_session)
    task.task_type = task_type
    task.status = TaskStatus.PENDING.value
    db_session.add(Agent(
        id=subscription.agent_id,
        entity_id=task.entity_id,
        name="Foreground host",
        status="active",
    ))
    await db_session.commit()

    assert (await resolve_task_execution(
        db_session,
        task,
    )).runtime_kind is expected_runtime
    updated = await update_task(
        db_session,
        task.id,
        task.entity_id,
        status=TaskStatus.IN_PROGRESS.value,
    )

    assert updated is not None
    assert updated.status == TaskStatus.IN_PROGRESS.value
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
    )) == 0


@pytest.mark.asyncio
async def test_update_keeps_standalone_legacy_agent_start_behavior(
    db_session: AsyncSession,
) -> None:
    from packages.core.services.task_service import update_task

    task = Task(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        title="Standalone legacy Agent task",
        status=TaskStatus.PENDING.value,
        status_changed_at=datetime.now(timezone.utc),
        task_type=TaskType.GENERAL.value,
        agent_id=generate_ulid(),
        delegate_service_keys=[],
    )
    db_session.add(task)
    await db_session.commit()

    assert (await resolve_task_execution(
        db_session,
        task,
    )).runtime_kind is TaskRuntimeKind.LEGACY_AGENT
    updated = await update_task(
        db_session,
        task.id,
        task.entity_id,
        status=TaskStatus.IN_PROGRESS.value,
    )

    assert updated is not None
    assert updated.status == TaskStatus.IN_PROGRESS.value
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
    )) == 0


@pytest.mark.asyncio
async def test_concurrent_starts_share_one_locked_lifecycle_generation(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core.services import task_service

    task, _subscription = await _coordinated_task(db_session)
    task.status = TaskStatus.PENDING.value
    await db_session.commit()
    task_id = task.id
    entity_id = task.entity_id

    # Make both independent transactions finish the initial unlocked existence
    # lookup before either reaches the authoritative FOR UPDATE read. This
    # deterministically reproduces the stale-read race the row lock closes.
    original_get_task = task_service.get_task
    both_loaded = asyncio.Event()
    load_count = 0
    load_count_lock = asyncio.Lock()

    async def synchronized_get_task(db, loaded_task_id, loaded_entity_id):
        nonlocal load_count
        loaded = await original_get_task(db, loaded_task_id, loaded_entity_id)
        async with load_count_lock:
            load_count += 1
            if load_count == 2:
                both_loaded.set()
        await asyncio.wait_for(both_loaded.wait(), timeout=5)
        return loaded

    monkeypatch.setattr(task_service, "get_task", synchronized_get_task)
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def start_once() -> datetime:
        async with session_factory() as session:
            updated = await task_service.update_task(
                session,
                task_id,
                entity_id,
                status=TaskStatus.IN_PROGRESS.value,
            )
            assert updated is not None
            await session.commit()
            assert updated.status_changed_at is not None
            return updated.status_changed_at

    first_changed_at, second_changed_at = await asyncio.wait_for(
        asyncio.gather(start_once(), start_once()),
        timeout=10,
    )

    db_session.expire_all()
    final_task = await db_session.get(Task, task_id)
    assert final_task is not None
    assert final_task.status == TaskStatus.IN_PROGRESS.value
    assert first_changed_at == second_changed_at == final_task.status_changed_at

    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task_id,
        )
    )).scalars())
    assert len(events) == 1
    assert events[0].payload["intent_generation"] == task_execution_intent_generation(
        final_task
    )


@pytest.mark.asyncio
async def test_stage_task_execution_intent_is_transactional_and_idempotent(
    db_session: AsyncSession,
) -> None:
    task, _subscription = await _coordinated_task(db_session)
    await db_session.commit()
    task_id = task.id

    dispatch = await resolve_task_execution(db_session, task)
    generation = await stage_task_execution_intent(db_session, task, dispatch)
    staged = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=generation,
    )
    assert staged.delivered_at is None

    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(RuntimeOutboxEvent.aggregate_id == task_id)) == 0

    task = await db_session.get(Task, task_id)
    generation = await stage_task_execution_intent(db_session, task, dispatch)
    await db_session.commit()
    staged = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=generation,
    )
    staged.attempt_count = 4
    staged.delivered_at = datetime.now(timezone.utc)
    staged.last_error = "old delivery"
    await db_session.flush()

    assert await stage_task_execution_intent(db_session, task, dispatch) == generation
    await db_session.flush()
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(RuntimeOutboxEvent.aggregate_id == task.id)) == 1
    await db_session.refresh(staged)
    assert staged.attempt_count == 0
    assert staged.delivered_at is None
    assert staged.last_error is None


@pytest.mark.asyncio
async def test_task_execution_consumer_re_resolves_factory_before_admission(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.tasks import runtime_tasks

    task, subscription = await _coordinated_task(db_session)
    generation = await stage_task_execution_intent(db_session, task)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    result = await runtime_tasks.consume_task_execution_intent_once(
        event_id=(await _event_for_generation(
            db_session,
            task_id=task.id,
            generation=generation,
        )).id,
        task_id=task.id,
        intent_generation=generation,
    )

    assert result["status"] == "admitted"
    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.services.task_coordinator_wakeup import task_coordinator_outbox_dedupe_key

    run = (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one()
    assert run.root_subscription_id == subscription.id
    assert run.lifecycle_generation == generation
    wake = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task.id)
        )
    )).scalar_one()
    assert wake.delivered_at is None
    first_wake_generation = wake.payload["wake_generation"]

    await stage_task_execution_intent(db_session, task)
    subscription.status = "inactive"
    await db_session.commit()
    event = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=generation,
    )
    result = await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id,
        task_id=task.id,
        intent_generation=generation,
    )
    assert result == {
        "status": "deferred",
        "reason": "non_coordinated_runtime",
        "task_id": task.id,
    }
    await db_session.refresh(event)
    assert event.delivered_at is None
    assert event.last_error.endswith("non_coordinated_runtime")

    subscription.status = "active"
    await db_session.commit()
    result = await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id,
        task_id=task.id,
        intent_generation=generation,
    )
    assert result["status"] == "admitted"
    await db_session.refresh(wake)
    assert wake.payload["wake_generation"] == first_wake_generation


@pytest.mark.asyncio
async def test_late_v2_planner_cannot_replace_admitted_task_owner(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.plans.planner import (
        TaskPlanOwnedByCoordinatorError,
        plan_task,
    )
    from packages.core.tasks import runtime_tasks

    task, _subscription = await _coordinated_task(db_session)
    generation = await stage_task_execution_intent(db_session, task)
    await db_session.commit()
    event = await _event_for_generation(
        db_session, task_id=task.id, generation=generation,
    )
    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    assert (await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id, task_id=task.id, intent_generation=generation,
    ))["status"] == "admitted"

    with pytest.raises(TaskPlanOwnedByCoordinatorError):
        await plan_task(db_session, task.id)

    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )

    run = (await db_session.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id == task.id,
    ))).scalar_one()
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=task.entity_id,
        workspace_id=task.workspace_id, task_id=task.id,
        status="running", plan_dag={},
        dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: run.id,
        },
    )
    db_session.add(plan)
    run.current_plan_id = plan.id
    await db_session.flush()
    with pytest.raises(TaskPlanOwnedByCoordinatorError):
        await plan_task(db_session, task.id)


def test_queued_v2_planner_message_does_not_fail_v3_task(monkeypatch) -> None:
    from packages.core.plans.planner import TaskPlanOwnedByCoordinatorError
    from packages.core.tasks import ai_tasks

    def old_delivery(coroutine):
        coroutine.close()
        raise TaskPlanOwnedByCoordinatorError(
            "task-v3", TaskStatus.IN_PROGRESS.value,
            "Task-level coordinator owns this Workspace Task",
        )

    monkeypatch.setattr(ai_tasks, "_run_async", old_delivery)
    monkeypatch.setattr(
        ai_tasks, "_mark_task_failed",
        lambda *_args, **_kwargs: pytest.fail("old V2 message failed a V3 Task"),
    )
    assert ai_tasks.plan_and_run_task.run("task-v3") == {
        "plan_id": None,
        "status": TaskStatus.IN_PROGRESS.value,
        "duplicate_suppressed": True,
        "task_coordinator_owned": True,
    }


@pytest.mark.asyncio
async def test_duplicate_start_consumers_admit_one_task_run_and_one_wake(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.services.task_coordinator_wakeup import task_coordinator_outbox_dedupe_key
    from packages.core.tasks import runtime_tasks

    task, _subscription = await _coordinated_task(db_session)
    generation = await stage_task_execution_intent(db_session, task)
    await db_session.commit()
    event = await _event_for_generation(
        db_session, task_id=task.id, generation=generation,
    )
    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    results = await asyncio.gather(*(
        runtime_tasks.consume_task_execution_intent_once(
            event_id=event.id, task_id=task.id,
            intent_generation=generation,
        )
        for _ in range(2)
    ))
    assert sorted(result["status"] for result in results) == ["admitted", "closed"]
    assert await db_session.scalar(select(func.count()).select_from(
        TaskCoordinatorRun
    ).where(TaskCoordinatorRun.task_id == task.id)) == 1
    assert await db_session.scalar(select(func.count()).select_from(
        RuntimeOutboxEvent
    ).where(
        RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task.id),
        RuntimeOutboxEvent.delivered_at.is_(None),
    )) == 1


@pytest.mark.asyncio
async def test_stale_generation_rearm_cannot_supersede_current_intent(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.tasks import runtime_tasks

    task, _subscription = await _coordinated_task(db_session)
    dispatch = await resolve_task_execution(db_session, task)
    stale_timestamp = task.status_changed_at
    stale_generation = await stage_task_execution_intent(db_session, task, dispatch)
    stale_task = Task(
        id=task.id,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        title=task.title,
        status=task.status,
        status_changed_at=stale_timestamp,
        task_type=task.task_type,
        agent_id=task.agent_id,
        agent_type=task.agent_type,
        owner_service_key=task.owner_service_key,
        owner_subscription_id=task.owner_subscription_id,
        delegate_service_keys=list(task.delegate_service_keys or []),
    )
    task.status_changed_at = stale_timestamp + timedelta(seconds=1)
    current_generation = await stage_task_execution_intent(db_session, task, dispatch)
    assert current_generation != stale_generation

    # A delayed caller can re-stage G1 after G2. G1's newer created_at must
    # never make it authoritative; the persisted Task lifecycle is the source
    # of truth for the current generation.
    assert await stage_task_execution_intent(
        db_session,
        stale_task,
        dispatch,
    ) == stale_generation
    await db_session.commit()

    current_event = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=current_generation,
    )
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    result = await runtime_tasks.consume_task_execution_intent_once(
        event_id=current_event.id,
        task_id=task.id,
        intent_generation=current_generation,
    )

    assert result["status"] == "admitted"
    from packages.core.models.task_coordinator import TaskCoordinatorRun
    run = (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one()
    assert run.lifecycle_generation == current_generation


@pytest.mark.asyncio
async def test_unacknowledged_current_intent_stays_pending_for_redelivery(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.tasks import runtime_tasks

    now = datetime.now(timezone.utc)
    task, _subscription = await _coordinated_task(db_session)
    generation = await stage_task_execution_intent(db_session, task)
    event = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=generation,
    )
    task_id = task.id
    event.available_at = now - timedelta(minutes=5)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: False)
    monkeypatch.setattr(
        runtime_tasks,
        "_recover_task_runtime_resume_outbox",
        AsyncMock(return_value=0),
    )
    published = []
    monkeypatch.setattr(
        runtime_tasks.celery_app,
        "send_task",
        lambda name, *, kwargs, queue: published.append((name, kwargs, queue)),
    )

    assert await runtime_tasks.dispatch_runtime_outbox_once(batch_size=10) == 1
    db_session.expire_all()
    event = await _event_for_generation(
        db_session,
        task_id=task_id,
        generation=generation,
    )
    await db_session.refresh(event)
    assert event.delivered_at is None
    assert event.available_at > now
    assert event.last_error == runtime_tasks._TASK_EXECUTION_AWAITING_ACK
    assert published == [(
        TASK_EXECUTION_OUTBOX_EVENT,
        {
            "task_id": task_id,
            "intent_generation": generation,
            "event_id": event.id,
        },
        "celery",
    )]

    event.available_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert await runtime_tasks.dispatch_runtime_outbox_once(batch_size=10) == 1
    assert len(published) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["active_plan", "terminal_task", "superseded"])
async def test_task_execution_recovery_closes_non_current_delivery(
    db_session: AsyncSession,
    monkeypatch,
    closure: str,
) -> None:
    from packages.core import database
    from packages.core.tasks import runtime_tasks

    now = datetime.now(timezone.utc)
    task, _subscription = await _coordinated_task(db_session)
    generation = await stage_task_execution_intent(db_session, task)
    event = await _event_for_generation(
        db_session,
        task_id=task.id,
        generation=generation,
    )
    task_id = task.id
    event.available_at = now - timedelta(minutes=5)
    if closure == "active_plan":
        db_session.add(ExecutionPlan(
            id=generate_ulid(),
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            task_id=task.id,
            status="running",
            plan_dag={},
        ))
    elif closure == "terminal_task":
        task.status = TaskStatus.COMPLETED.value
        task.status_changed_at = now
    else:
        event.created_at = now - timedelta(minutes=10)
        task.status_changed_at = now + timedelta(seconds=1)
        latest_generation = await stage_task_execution_intent(db_session, task)
        latest_event = await _event_for_generation(
            db_session,
            task_id=task.id,
            generation=latest_generation,
        )
        latest_event.available_at = now + timedelta(hours=1)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: False)
    monkeypatch.setattr(
        runtime_tasks,
        "_recover_task_runtime_resume_outbox",
        AsyncMock(return_value=0),
    )
    published = []
    monkeypatch.setattr(
        runtime_tasks.celery_app,
        "send_task",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    assert await runtime_tasks.dispatch_runtime_outbox_once(batch_size=10) == 0
    db_session.expire_all()
    event = await _event_for_generation(
        db_session,
        task_id=task_id,
        generation=generation,
    )
    await db_session.refresh(event)
    assert event.delivered_at is not None
    assert event.last_error == runtime_tasks._TASK_EXECUTION_CLOSED_SENTINEL
    assert published == []


def test_task_execution_event_uses_control_queue() -> None:
    from packages.core.queues import CeleryQueue, queue_for_task

    assert queue_for_task(TASK_EXECUTION_OUTBOX_EVENT) is CeleryQueue.CONTROL
