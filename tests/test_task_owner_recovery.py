"""First execution and recovery use the same Workspace team routing contract."""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from packages.core.constants.task import TaskRuntimeKind, TaskType
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription


@pytest.mark.parametrize(
    ("runtime_kind", "mode"),
    [
        (TaskRuntimeKind.INTERACTIVE_SESSION, "interactive_session"),
        (TaskRuntimeKind.HUMAN_DECISION, "human_decision"),
        (TaskRuntimeKind.MANUAL, "manual"),
    ],
)
def test_non_dispatchable_task_runtimes_fail_closed(runtime_kind, mode):
    from packages.core.services.task_execution import TaskExecutionDispatch

    route = TaskExecutionDispatch(runtime_kind=runtime_kind, task_id="task-1")
    assert route.mode == mode
    assert route.is_dispatchable is False


async def _workspace(client, db):
    registered = await client.post("/api/v1/auth/register", json={
        "username": "task_owner_recovery", "email": "owner_recovery@test.com",
        "password": "pass123", "entity_name": "Recovery tests",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "Publishing team",
    })).json()
    subscriptions = {}
    for key in ("content_creation", "product_promotion", "analytics"):
        agent = Agent(id=generate_ulid(), entity_id=me["entity_id"], name=key, status="active")
        sub = AgentSubscription(
            id=generate_ulid(), entity_id=me["entity_id"], agent_id=agent.id,
            workspace_id=workspace["id"], service_key=key, status="active",
        )
        db.add_all([agent, sub])
        subscriptions[key] = sub
    await db.commit()
    return headers, me, workspace, subscriptions


async def _waiting_task(client, db, headers, workspace):
    created = (await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Publish a post", "workspace_id": workspace["id"],
    })).json()
    task = await db.get(Task, created["id"])
    task.status = "waiting_on_customer"
    await db.commit()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_route", ["inactive", "foreign", "outside_delegates", "deleted_workspace"])
async def test_team_retry_rejects_unavailable_routes(
    client, db_session, monkeypatch, invalid_route,
):
    from packages.core.models.workspace import Workspace

    headers, me, workspace, subs = await _workspace(client, db_session)
    task = await _waiting_task(client, db_session, headers, workspace)
    if invalid_route == "inactive":
        for sub in subs.values():
            sub.status = "inactive"
    elif invalid_route == "foreign":
        for sub in subs.values():
            sub.entity_id = generate_ulid()
    elif invalid_route == "outside_delegates":
        task.delegate_service_keys = ["unavailable_service"]
    else:
        row = await db_session.get(Workspace, workspace["id"])
        row.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    response = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers, json={})
    assert response.status_code == (403 if invalid_route == "deleted_workspace" else 409), response.text
    await db_session.refresh(task)
    assert task.status == "waiting_on_customer"
    assert task.owner_subscription_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "product_promotion"])
async def test_first_execution_and_retry_have_identical_routes(client, db_session, monkeypatch, owner):
    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import runtime_tasks

    _headers, me, workspace, subs = await _workspace(client, db_session)
    kwargs = {"entity_id": me["entity_id"], "workspace_id": workspace["id"], "user_id": me["id"]}
    accepted = json.loads(await runtime_workspace_create_task_action(
        **kwargs, params={
            "title": "Publish a post", "start": True,
            "owner_service_key": owner, "delegate_service_keys": ["content_creation"],
        },
    ))
    assert accepted["created"] is True
    assert accepted["owner_resolution"]["owner_subscription_id"] == (subs[owner].id if owner else None)
    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == accepted["task"]["id"],
        )
    )).scalar_one()
    await db_session.commit()
    delivered = await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id,
        task_id=accepted["task"]["id"],
        intent_generation=event.payload["intent_generation"],
    )
    assert delivered["status"] == "admitted"
    from packages.core.services.task_execution import resolve_task_execution, task_plan_subscriptions

    task = await db_session.get(Task, accepted["task"]["id"])
    first_route = await resolve_task_execution(db_session, task)
    first_scope = {s.service_key for s in await task_plan_subscriptions(db_session, task)}
    assert first_scope == {"content_creation", *([owner] if owner else [])}
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun

    run = (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one()
    run.phase = TaskCoordinatorPhase.FAILED.value
    task.status = "failed"
    await db_session.commit()
    response = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=_headers, json={})
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert await resolve_task_execution(db_session, task) == first_route
    assert {s.service_key for s in await task_plan_subscriptions(db_session, task)} == first_scope
    retry_event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one()
    await db_session.commit()
    retry_delivery = await runtime_tasks.consume_task_execution_intent_once(
        event_id=retry_event.id,
        task_id=task.id,
        intent_generation=retry_event.payload["intent_generation"],
    )
    assert retry_delivery["status"] == "admitted"


@pytest.mark.asyncio
async def test_task_runtime_factory_separates_business_type_from_execution_surface(
    client,
    db_session,
):
    from packages.core.services.task_execution import resolve_task_execution

    _headers, me, workspace, _subs = await _workspace(client, db_session)
    background_types = [
        TaskType.GENERAL.value,
        TaskType.AI_GENERATED.value,
        TaskType.SCHEDULED.value,
        TaskType.CUSTOMER_REQUEST.value,
        TaskType.INCIDENT.value,
        TaskType.INSPECTION.value,
        TaskType.FOLLOW_UP.value,
        "integration_custom_type",
    ]
    for task_type in background_types:
        task = Task(
            id=generate_ulid(),
            entity_id=me["entity_id"],
            workspace_id=workspace["id"],
            title=f"Route {task_type}",
            task_type=task_type,
            owner_service_key="content_creation",
            delegate_service_keys=[],
        )
        route = await resolve_task_execution(db_session, task)
        assert route.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
        assert route.mode == "plan_new"
        assert route.is_dispatchable is True

    interactive = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Interactive session",
        task_type=TaskType.INTERACTIVE.value,
        owner_service_key="content_creation",
        delegate_service_keys=[],
    )
    approval = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Human approval",
        task_type=TaskType.APPROVAL.value,
        owner_service_key="content_creation",
        delegate_service_keys=[],
    )
    manual = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        title="Manual standalone",
        task_type=TaskType.GENERAL.value,
        delegate_service_keys=[],
    )
    standalone = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        title="Standalone Agent",
        task_type=TaskType.GENERAL.value,
        agent_id=generate_ulid(),
        delegate_service_keys=[],
    )

    interactive_route = await resolve_task_execution(db_session, interactive)
    approval_route = await resolve_task_execution(db_session, approval)
    manual_route = await resolve_task_execution(db_session, manual)
    standalone_route = await resolve_task_execution(db_session, standalone)

    assert interactive_route.runtime_kind is TaskRuntimeKind.INTERACTIVE_SESSION
    assert interactive_route.mode == "interactive_session"
    assert interactive_route.is_dispatchable is False
    assert approval_route.runtime_kind is TaskRuntimeKind.HUMAN_DECISION
    assert approval_route.mode == "human_decision"
    assert approval_route.is_dispatchable is False
    assert manual_route.runtime_kind is TaskRuntimeKind.MANUAL
    assert manual_route.mode == "manual"
    assert manual_route.is_dispatchable is False
    # A Task outside any Workspace runs as its Agent alone, on the same run.
    assert standalone_route.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
    assert standalone_route.mode == "plan_new"
    assert standalone_route.coordinator.is_agent_only
    assert standalone_route.coordinator.root.agent_id == standalone.agent_id


@pytest.mark.asyncio
async def test_legacy_transport_routes_workspace_task_through_coordinator_factory(
    client,
    db_session,
):
    import packages.core.database as db_module
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import ai_tasks

    _headers, me, workspace, _subscriptions = await _workspace(client, db_session)
    task = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Scheduled Workspace work",
        status="pending",
        task_type=TaskType.SCHEDULED.value,
        owner_service_key="content_creation",
        delegate_service_keys=[],
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

    legacy_dispatch, result = await ai_tasks._prepare_agent_task_runtime(
        db_module.async_session,
        task_id,
    )

    assert legacy_dispatch is None
    assert result["runtime_kind"] == TaskRuntimeKind.COORDINATED_PLAN.value
    assert result["coordinator_handoff"] is True
    assert result["coordinator_handoff_staged"] is True
    db_session.expire_all()
    assert (await db_session.get(Task, task_id)).status == "in_progress"
    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task_id,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one()
    assert event.payload["task_id"] == task_id


@pytest.mark.asyncio
async def test_assigned_workspace_agent_requires_an_exact_service_subscription(
    client,
    db_session,
):
    from packages.core.services.task_execution import (
        resolve_task_execution,
        task_plan_subscriptions,
    )

    _headers, me, workspace, subscriptions = await _workspace(client, db_session)
    assigned = subscriptions["product_promotion"]
    task = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Assigned Workspace Agent",
        task_type=TaskType.GENERAL.value,
        agent_id=assigned.agent_id,
        delegate_service_keys=[],
    )

    exact_scope = await task_plan_subscriptions(db_session, task)
    assert [subscription.id for subscription in exact_scope] == [assigned.id]
    execution = await resolve_task_execution(db_session, task)
    assert execution.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
    assert execution.coordinator is not None
    assert execution.coordinator.root.subscription_id == assigned.id
    assert execution.coordinator.root.service_key == "product_promotion"
    assert execution.coordinator.service_keys == frozenset({"product_promotion"})

    second_role = AgentSubscription(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        agent_id=assigned.agent_id,
        workspace_id=workspace["id"],
        service_key="secondary_promotion",
        status="active",
    )
    db_session.add(second_role)
    await db_session.commit()

    assert await task_plan_subscriptions(db_session, task) == []
    assert (
        await resolve_task_execution(db_session, task)
    ).runtime_kind is TaskRuntimeKind.MANUAL

    task.owner_service_key = "product_promotion"
    task.owner_subscription_id = assigned.id
    task.delegate_service_keys = ["analytics"]
    pinned_scope = await task_plan_subscriptions(db_session, task)
    assert [subscription.id for subscription in pinned_scope] == [
        assigned.id,
        subscriptions["analytics"].id,
    ]
    execution = await resolve_task_execution(db_session, task)
    assert execution.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
    assert execution.coordinator is not None
    assert execution.coordinator.root.subscription_id == assigned.id
    assert execution.coordinator.service_keys == frozenset({
        "product_promotion", "analytics",
    })

    task.owner_subscription_id = subscriptions["content_creation"].id
    assert await task_plan_subscriptions(db_session, task) == []
    assert (
        await resolve_task_execution(db_session, task)
    ).runtime_kind is TaskRuntimeKind.MANUAL


@pytest.mark.parametrize("transaction", ["commit", "rollback", "savepoint_rollback"])
async def test_initial_status_dispatch_waits_for_owning_transaction(client, db_session, monkeypatch, transaction):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.services.task_service import update_task

    headers, me, workspace, _subs = await _workspace(client, db_session)
    created = (await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Team execution", "workspace_id": workspace["id"],
    })).json()
    nested = await db_session.begin_nested() if transaction == "savepoint_rollback" else None
    await update_task(db_session, created["id"], me["entity_id"], status="in_progress")
    if nested:
        await nested.rollback()
    if transaction == "rollback":
        await db_session.rollback()
    await db_session.commit()
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == created["id"],
        )
    )).scalars().all())
    assert len(events) == (1 if transaction == "commit" else 0)


async def test_api_creation_with_agent_and_service_uses_same_planner_as_retry(client, db_session, monkeypatch):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import ai_tasks
    from packages.core.tasks import runtime_tasks

    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", lambda *args: pytest.fail("must use the Task's agent-loop route"))
    headers, _me, workspace, subs = await _workspace(client, db_session)
    response = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Owner and assigned Agent", "workspace_id": workspace["id"],
        "agent_id": subs["product_promotion"].agent_id, "owner_service_key": "product_promotion",
    })
    assert response.status_code == 201, response.text
    task = await db_session.get(Task, response.json()["id"])
    assert task.status == "in_progress"
    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
        )
    )).scalar_one()
    await db_session.commit()
    delivered = await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id,
        task_id=task.id,
        intent_generation=event.payload["intent_generation"],
    )
    assert delivered["status"] == "admitted"
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
    run = (await db_session.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task.id)
    )).scalar_one()
    run.phase = TaskCoordinatorPhase.FAILED.value
    task.status = "failed"
    await db_session.commit()
    retried = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers, json={})
    assert retried.status_code == 200, retried.text
    retry_event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one()
    await db_session.commit()
    retry_delivery = await runtime_tasks.consume_task_execution_intent_once(
        event_id=retry_event.id,
        task_id=task.id,
        intent_generation=retry_event.payload["intent_generation"],
    )
    assert retry_delivery["status"] == "admitted"


@pytest.mark.parametrize("assignment", ["owner_service_key", "owner_subscription_id"])
async def test_api_creation_with_explicit_workspace_owner_starts_factory_runtime(
    client,
    db_session,
    assignment,
):
    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    headers, _me, workspace, subscriptions = await _workspace(client, db_session)
    owner = subscriptions["product_promotion"]
    payload = {
        "title": "Explicit Workspace owner",
        "workspace_id": workspace["id"],
        assignment: (
            owner.service_key
            if assignment == "owner_service_key"
            else owner.id
        ),
    }

    response = await client.post("/api/v1/tasks", headers=headers, json=payload)

    assert response.status_code == 201, response.text
    task = await db_session.get(Task, response.json()["id"])
    assert task.status == "in_progress"
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
        )
    )).scalars().all())
    assert len(events) == 1
    assert events[0].payload["task_id"] == task.id
    assert events[0].payload["intent_generation"]


async def test_api_creation_without_explicit_execution_intent_stays_pending(
    client,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    headers, _me, workspace, _subscriptions = await _workspace(client, db_session)

    response = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Unstarted Workspace Task",
        "workspace_id": workspace["id"],
    })

    assert response.status_code == 201, response.text
    task = await db_session.get(Task, response.json()["id"])
    assert task.status == "pending"
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
        )
    )).scalars().all())
    assert events == []


async def test_api_owner_assignment_update_starts_factory_runtime(
    client,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    headers, _me, workspace, subscriptions = await _workspace(client, db_session)
    created = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Assign later",
        "workspace_id": workspace["id"],
    })
    task_id = created.json()["id"]

    response = await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={
        "owner_service_key": subscriptions["product_promotion"].service_key,
    })

    assert response.status_code == 200, response.text
    task = await db_session.get(Task, task_id)
    assert task.status == "in_progress"
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
        )
    )).scalars().all())
    assert len(events) == 1


async def test_api_waiting_team_task_resume_uses_factory_without_owner_switch(
    client,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    headers, _me, workspace, _subscriptions = await _workspace(client, db_session)
    created = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Resume team work",
        "workspace_id": workspace["id"],
    })
    task = await db_session.get(Task, created.json()["id"])
    task.status = "waiting_on_customer"
    await db_session.commit()

    response = await client.put(f"/api/v1/tasks/{task.id}", headers=headers, json={
        "status": "pending",
    })

    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert task.status == "in_progress"
    events = list((await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task.id,
        )
    )).scalars().all())
    assert len(events) == 1
