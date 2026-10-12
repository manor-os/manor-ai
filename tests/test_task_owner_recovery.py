"""First execution and recovery use the same Workspace team routing contract."""
import asyncio
import json
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.constants.task import TaskRuntimeKind, TaskType
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription


async def _failed_v3_acceptance_fixture(client, db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core import database
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import runtime_tasks

    headers, me, workspace, _ = await _workspace(client, db_session)
    created = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Retry read-only acceptance only",
        "workspace_id": workspace["id"],
        "owner_service_key": "content_creation",
    })
    assert created.status_code == 201, created.text
    task = await db_session.get(Task, created.json()["id"])
    start = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalar_one()
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    assert (await runtime_tasks.consume_task_execution_intent_once(
        event_id=start.id, task_id=task.id,
        intent_generation=start.payload["intent_generation"],
    ))["status"] == "admitted"

    run = (await db_session.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id == task.id,
    ))).scalar_one()
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=me["entity_id"],
        workspace_id=workspace["id"], task_id=task.id,
        status="failed", plan_dag={"steps": []},
        dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: run.id,
        },
        last_error={
            "type": "ActionOutcomeReconciliationRequired",
            "message": "The prior action cannot be replayed in a replacement Plan",
            "reconciliation": None,
        },
        completed_at=datetime.now(timezone.utc),
    )
    effect = {"kind": "tool_effect", "effect": "action", "fields": {"tool_call_id": "file-call"}}
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=task.entity_id,
        workspace_id=task.workspace_id, step_key="write", kind="subagent",
        step_status="done", depends_on=[], params={"prompt": "write"},
        result={"text": "Saved draft"}, evidence_refs=[effect],
    )
    db_session.add_all([plan, step])
    raw = json.dumps({
        "status": "retryable", "code": "review_invalid",
        "reason": "The read-only content assessment was invalid",
    })
    run.current_plan_id = plan.id
    run.phase = TaskCoordinatorPhase.WAITING_HUMAN.value
    run.checkpoint = {"messages": [
        {"role": "assistant", "tool_calls": [{
            "id": "submit-1", "function": {"name": "submit_task", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "submit-1", "content": raw},
        {"role": "assistant", "tool_calls": [{
            "id": "submit-2", "function": {"name": "submit_task", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "submit-2", "content": json.dumps({
            "repeated_tool_result": True, "tool": "submit_task",
            "result_digest": hashlib.sha256(raw.encode()).hexdigest()[:16],
            "preview": raw,
        })},
    ]}
    task.status = "waiting_on_customer"
    task.status_changed_at = datetime.now(timezone.utc)
    await db_session.commit()
    return headers, task, plan, step, run, effect


@pytest.mark.asyncio
async def test_v3_failed_acceptance_retry_reopens_only_original_plan(
    client, db_session, monkeypatch,
):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import runtime_tasks

    headers, task, plan, step, run, effect = await _failed_v3_acceptance_fixture(
        client, db_session, monkeypatch,
    )
    retried = await client.post(
        f"/api/v1/tasks/{task.id}/retry", headers=headers, json={},
    )
    assert retried.status_code == 200, retried.text
    await db_session.refresh(task)
    await db_session.refresh(plan)
    await db_session.refresh(step)
    await db_session.refresh(run)
    assert task.status == "in_progress"
    assert plan.status == "running"
    assert plan.completed_at is None
    assert plan.last_error is None
    assert step.step_status == "done"
    assert step.result == {"text": "Saved draft"}
    assert step.evidence_refs == [effect]
    assert run.current_plan_id == plan.id
    assert run.checkpoint["task_acceptance_retry"] == {
        "version": 1, "plan_id": plan.id,
        "attempts": 0, "code": "manual_recovery",
    }
    assert (await db_session.execute(select(ExecutionPlan.id).where(
        ExecutionPlan.task_id == task.id,
    ))).scalars().all() == [plan.id]

    intent = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalar_one()
    await db_session.commit()
    assert (await runtime_tasks.consume_task_execution_intent_once(
        event_id=intent.id, task_id=task.id,
        intent_generation=intent.payload["intent_generation"],
    ))["status"] == "admitted"
    await db_session.refresh(run)
    assert run.current_plan_id == plan.id
    assert run.checkpoint["task_acceptance_retry"]["code"] == "manual_recovery"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe", ["bad_checkpoint", "bad_digest", "pending_effect", "live_lease"],
)
async def test_v3_failed_acceptance_retry_rejects_unsettled_evidence(
    client, db_session, monkeypatch, unsafe,
):
    from packages.core.models.worker import WorkLease

    headers, task, plan, step, run, _ = await _failed_v3_acceptance_fixture(
        client, db_session, monkeypatch,
    )
    if unsafe == "bad_checkpoint":
        run.checkpoint = {"messages": [{
            "role": "tool", "tool_call_id": "unknown", "content": "{}",
        }]}
    elif unsafe == "bad_digest":
        messages = list(run.checkpoint["messages"])
        messages[-1] = {
            **messages[-1],
            "content": json.dumps({
                **json.loads(messages[-1]["content"]),
                "result_digest": "0" * 16,
            }),
        }
        run.checkpoint = {"messages": messages}
    elif unsafe == "pending_effect":
        step.evidence_refs = [{
            "kind": "tool_effect", "effect": "pending",
            "fields": {"tool_call_id": "file-call"},
        }]
    else:
        db_session.add(WorkLease(
            id=generate_ulid(), step_id=step.id, plan_id=plan.id,
            entity_id=task.entity_id, workspace_id=task.workspace_id,
            worker_id=generate_ulid(), status="active",
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
    await db_session.commit()
    retried = await client.post(
        f"/api/v1/tasks/{task.id}/retry", headers=headers, json={},
    )
    assert retried.status_code == 409, retried.text
    await db_session.refresh(task)
    await db_session.refresh(plan)
    await db_session.refresh(step)
    assert task.status == "waiting_on_customer"
    assert plan.status == "failed"
    assert step.step_status == "done"


@pytest.mark.asyncio
async def test_v3_manual_retry_renews_saved_acceptance_budget(
    client, db_session, monkeypatch,
):
    headers, task, plan, step, run, _ = await _failed_v3_acceptance_fixture(
        client, db_session, monkeypatch,
    )
    plan.status = "running"
    plan.completed_at = None
    plan.last_error = None
    run.checkpoint = {
        **run.checkpoint,
        "task_acceptance_retry": {
            "version": 1, "plan_id": plan.id,
            "attempts": 3, "code": "review_invalid",
            "evidence_digest": "old-content",
        },
    }
    await db_session.commit()
    retried = await client.post(
        f"/api/v1/tasks/{task.id}/retry", headers=headers, json={},
    )
    assert retried.status_code == 200, retried.text
    await db_session.refresh(run)
    await db_session.refresh(step)
    assert run.current_plan_id == plan.id
    assert run.checkpoint["task_acceptance_retry"] == {
        "version": 1, "plan_id": plan.id,
        "attempts": 0, "code": "manual_recovery",
    }
    assert step.step_status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("call_count", [2, 8])
async def test_concurrent_v3_acceptance_retries_admit_one_same_plan_resume(
    client, db_session, monkeypatch, call_count,
):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    headers, task, plan, step, run, effect = await _failed_v3_acceptance_fixture(
        client, db_session, monkeypatch,
    )
    responses = await asyncio.gather(*(
        client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers, json={})
        for _ in range(call_count)
    ))
    assert sorted(response.status_code for response in responses) == [200] + [409] * (call_count - 1)
    await db_session.refresh(plan)
    await db_session.refresh(step)
    await db_session.refresh(run)
    assert plan.status == "running"
    assert step.step_status == "done"
    assert step.evidence_refs == [effect]
    assert run.current_plan_id == plan.id
    assert (await db_session.execute(select(ExecutionPlan.id).where(
        ExecutionPlan.task_id == task.id,
    ))).scalars().all() == [plan.id]
    pending = (await db_session.execute(select(RuntimeOutboxEvent.id).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalars().all()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_v3_task_retry_preserves_active_plan_and_step_evidence(
    client, db_session, monkeypatch,
):
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core import database
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import runtime_tasks

    headers, me, workspace, _subs = await _workspace(client, db_session)
    created = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Resume V3 evidence", "workspace_id": workspace["id"],
        "owner_service_key": "content_creation",
    })
    assert created.status_code == 201, created.text
    task = await db_session.get(Task, created.json()["id"])
    start = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalar_one()
    await db_session.commit()
    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    assert (await runtime_tasks.consume_task_execution_intent_once(
        event_id=start.id, task_id=task.id,
        intent_generation=start.payload["intent_generation"],
    ))["status"] == "admitted"

    run = (await db_session.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id == task.id,
    ))).scalar_one()
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=me["entity_id"],
        workspace_id=workspace["id"], task_id=task.id,
        status="running", plan_dag={"steps": []},
        dispatcher_state={
            TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_RUN_ID_KEY: run.id,
        },
    )
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan.id, entity_id=task.entity_id,
        workspace_id=task.workspace_id, step_key="write", kind="llm",
        step_status="failed", depends_on=[], params={"prompt": "write"},
        error={"type": "OutputSchemaError", "message": "Missing field"},
        evidence_refs=[{"kind": "agent_output", "id": "prior-output"}],
    )
    db_session.add_all([plan, step])
    checkpoint = {"messages": [{"role": "assistant", "content": "Inspect failed Step"}]}
    run.current_plan_id = plan.id
    run.phase = TaskCoordinatorPhase.WAITING_HUMAN.value
    run.checkpoint = checkpoint
    task.status = "blocked"
    task.status_changed_at = datetime.now(timezone.utc)
    await db_session.commit()

    retried = await client.post(
        f"/api/v1/tasks/{task.id}/retry", headers=headers,
        json={"note": "Repair the output shape"},
    )
    assert retried.status_code == 200, retried.text
    await db_session.refresh(step)
    await db_session.refresh(plan)
    assert step.step_status == "failed"
    assert step.error["type"] == "OutputSchemaError"
    assert step.evidence_refs == [{"kind": "agent_output", "id": "prior-output"}]
    assert plan.status == "running"
    retry_event = (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
        RuntimeOutboxEvent.aggregate_id == task.id,
        RuntimeOutboxEvent.delivered_at.is_(None),
    ))).scalar_one()
    await db_session.commit()
    assert (await runtime_tasks.consume_task_execution_intent_once(
        event_id=retry_event.id, task_id=task.id,
        intent_generation=retry_event.payload["intent_generation"],
    ))["status"] == "admitted"
    await db_session.refresh(run)
    assert run.current_plan_id == plan.id
    assert run.checkpoint == checkpoint
    assert run.phase == TaskCoordinatorPhase.READY.value

pytestmark = pytest.mark.oss_regression


@pytest.mark.parametrize(
    ("runtime_kind", "mode"),
    [
        (TaskRuntimeKind.INTERACTIVE_SESSION, "interactive_session"),
        (TaskRuntimeKind.HUMAN_DECISION, "human_decision"),
        (TaskRuntimeKind.MANUAL, "manual"),
    ],
)
def test_non_dispatchable_task_runtimes_fail_closed(runtime_kind, mode):
    from packages.core.services.task_execution import (
        TaskExecutionDispatch,
        dispatch_task_execution,
    )

    route = TaskExecutionDispatch(runtime_kind=runtime_kind, task_id="task-1")
    assert route.mode == mode
    assert route.is_dispatchable is False
    with pytest.raises(ValueError, match="not dispatchable"):
        dispatch_task_execution("task-1", route)


def test_coordinated_workspace_task_cannot_bypass_durable_start_intent():
    from packages.core.services.task_execution import (
        CoordinatorRuntimeSelection,
        TaskExecutionDispatch,
        TaskPlanServiceBinding,
        dispatch_task_execution,
    )

    root = TaskPlanServiceBinding(
        subscription_id="subscription-1", service_key="owner", agent_id="agent-1",
    )
    route = TaskExecutionDispatch(
        runtime_kind=TaskRuntimeKind.COORDINATED_PLAN, task_id="task-1",
        coordinator=CoordinatorRuntimeSelection(root=root, bindings=(root,)),
    )
    with pytest.raises(ValueError, match="transactionally staged Task execution intent"):
        dispatch_task_execution("task-1", route)


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


async def _legacy_task(client, db, headers, me, workspace):
    created = (await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Publish a post", "workspace_id": workspace["id"],
    })).json()
    task = await db.get(Task, created["id"])
    task.status = "waiting_on_customer"
    dag = [{
        "key": key, "kind": "subagent", "service_key": service,
        "params": {"prompt": "Prepare the post"}, "output_shape": "TextResult",
        "depends_on": depends,
    } for key, service, depends in (
        ("prepare_post", "content_creation", []),
        ("publish_post", "product_promotion", ["prepare_post"]),
    )]
    plan = ExecutionPlan(
        id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace["id"],
        task_id=task.id, status="completed", plan_dag={"steps": dag},
    )
    db.add(plan)
    steps = []
    for spec in dag:
        step = ExecutionStep(
            id=generate_ulid(), plan_id=plan.id, entity_id=me["entity_id"],
            workspace_id=workspace["id"], step_key=spec["key"], kind=spec["kind"],
            service_key=spec["service_key"], depends_on=spec["depends_on"],
            params=spec["params"], step_status="done", result={"text": "Draft only; not published."},
        )
        db.add(step)
        steps.append(step)
    await db.commit()
    return task, plan, steps


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["task", "chat"])
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("saved_plan_owner", [False, True])
async def test_completed_unowned_plan_uses_team_and_replans(
    client, db_session, monkeypatch, surface, paused, saved_plan_owner,
):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl
    from packages.core.tasks import ai_tasks
    from packages.core.tasks import runtime_tasks
    from packages.core.models.workspace import Workspace

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    headers, me, workspace, subs = await _workspace(client, db_session)
    task, plan, steps = await _legacy_task(client, db_session, headers, me, workspace)
    expected_owner = "content_creation" if saved_plan_owner else "product_promotion"
    if saved_plan_owner:
        plan.agent_subscription_id = subs[expected_owner].id
    if paused:
        row = await db_session.get(Workspace, workspace["id"])
        row.status = "paused"
    await db_session.commit()
    if surface == "chat":
        card = await ensure_task_recovery_hitl(
            db_session, task, plan_id=plan.id, prompt="Publishing is not confirmed.",
        )
        await db_session.commit()
        response = await client.post(
            f"/api/v1/workspaces/{workspace['id']}/chat/messages/{card.id}/resolve",
            headers=headers, json={"choice": "retry", "note": "Use the prepared draft"},
        )
    else:
        response = await client.post(
            f"/api/v1/tasks/{task.id}/retry", headers=headers, json={"note": "Use the prepared draft"},
        )
    assert response.status_code == 200, response.text
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
    assert dispatched == []
    await db_session.refresh(task)
    await db_session.refresh(plan)
    assert task.status == "in_progress"
    assert task.owner_service_key is None
    assert task.owner_subscription_id is None
    assert task.delegate_service_keys == []
    context = task.details["_replan_context"]
    assert context["prior_plan_id"] == plan.id
    assert context["succeeded_steps"][-1]["result_summary"] == "Draft only; not published."
    assert plan.status == "completed"
    assert all(step.step_status == "done" for step in steps)
    again = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers, json={})
    assert again.status_code == 409
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_route", ["inactive", "foreign", "outside_delegates", "deleted_workspace"])
async def test_team_retry_rejects_unavailable_routes(
    client, db_session, monkeypatch, invalid_route,
):
    from packages.core.models.workspace import Workspace
    from packages.core.tasks import ai_tasks
    from datetime import datetime, timezone

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    headers, me, workspace, subs = await _workspace(client, db_session)
    task, plan, steps = await _legacy_task(client, db_session, headers, me, workspace)
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
    assert dispatched == []
    await db_session.refresh(task)
    assert task.status == "waiting_on_customer"
    assert task.owner_subscription_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "product_promotion"])
async def test_first_execution_and_retry_have_identical_routes(client, db_session, monkeypatch, owner):
    from packages.core.ai.runtime.workspace_task_actions import runtime_workspace_create_task_action
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import ai_tasks
    from packages.core.tasks import runtime_tasks

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
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
    assert dispatched == []
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
    assert dispatched == []


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
    legacy = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        title="Legacy standalone Agent",
        task_type=TaskType.GENERAL.value,
        agent_id=generate_ulid(),
        delegate_service_keys=[],
    )

    interactive_route = await resolve_task_execution(db_session, interactive)
    approval_route = await resolve_task_execution(db_session, approval)
    manual_route = await resolve_task_execution(db_session, manual)
    legacy_route = await resolve_task_execution(db_session, legacy)

    assert interactive_route.runtime_kind is TaskRuntimeKind.INTERACTIVE_SESSION
    assert interactive_route.mode == "interactive_session"
    assert interactive_route.is_dispatchable is False
    assert approval_route.runtime_kind is TaskRuntimeKind.HUMAN_DECISION
    assert approval_route.mode == "human_decision"
    assert approval_route.is_dispatchable is False
    assert manual_route.runtime_kind is TaskRuntimeKind.MANUAL
    assert manual_route.mode == "manual"
    assert manual_route.is_dispatchable is False
    assert legacy_route.runtime_kind is TaskRuntimeKind.LEGACY_AGENT
    assert legacy_route.mode == "agent"
    assert legacy_route.is_dispatchable is True


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
async def test_legacy_transport_only_monitors_an_existing_workspace_plan(
    client,
    db_session,
    monkeypatch,
):
    import packages.core.database as db_module
    from packages.core.services import task_execution
    from packages.core.tasks import ai_tasks

    _headers, me, workspace, _subscriptions = await _workspace(client, db_session)
    task = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Already coordinated work",
        status="in_progress",
        task_type=TaskType.SCHEDULED.value,
        owner_service_key="content_creation",
        delegate_service_keys=[],
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        status="running",
        plan_dag={},
    )
    db_session.add_all([task, plan])
    await db_session.commit()

    async def forbidden_stage(*_args, **_kwargs):
        pytest.fail("an existing Plan must not restage initial Task dispatch")

    monkeypatch.setattr(
        task_execution,
        "stage_task_execution_intent",
        forbidden_stage,
    )

    legacy_dispatch, result = await ai_tasks._prepare_agent_task_runtime(
        db_module.async_session,
        task.id,
    )

    assert legacy_dispatch is None
    assert result == {
        "task_id": task.id,
        "status": "in_progress",
        "runtime_kind": TaskRuntimeKind.COORDINATED_PLAN.value,
        "coordinator_monitor": True,
        "duplicate_suppressed": True,
        "reason": "active_plan_owns_task_execution",
        "plan_id": plan.id,
    }


@pytest.mark.asyncio
async def test_task_runner_final_fence_rejects_workspace_runtime_before_provider(
    client,
    db_session,
):
    import packages.core.database as db_module
    from packages.core.ai.task_runner import TaskRunner

    _headers, me, workspace, _subscriptions = await _workspace(client, db_session)
    task = Task(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        workspace_id=workspace["id"],
        title="Never enter the legacy provider loop",
        status="in_progress",
        task_type=TaskType.GENERAL.value,
        owner_service_key="content_creation",
        delegate_service_keys=[],
    )
    db_session.add(task)
    await db_session.commit()

    class ForbiddenEngine:
        def __getattr__(self, name):
            raise AssertionError(f"legacy provider engine was accessed: {name}")

    result = await TaskRunner(
        engine=ForbiddenEngine(),
        session_factory=db_module.async_session,
    ).run(task.id)

    assert result["reason"] == "task_runtime_requires_factory"
    assert result["runtime_kind"] == TaskRuntimeKind.COORDINATED_PLAN.value
    assert result["runtime_factory_recheck"] is True


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
    from packages.core.tasks import ai_tasks

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    headers, me, workspace, _subs = await _workspace(client, db_session)
    created = (await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Team execution", "workspace_id": workspace["id"],
    })).json()
    nested = await db_session.begin_nested() if transaction == "savepoint_rollback" else None
    await update_task(db_session, created["id"], me["entity_id"], status="in_progress")
    assert dispatched == []
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
    assert dispatched == []


async def test_retry_blocks_unreconciled_effect_instead_of_replaying_failed_step(client, db_session, monkeypatch):
    from packages.core.tasks import ai_tasks

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: pytest.fail("must not replay the side effect"))
    headers, me, workspace, _subs = await _workspace(client, db_session)
    task, plan, steps = await _legacy_task(client, db_session, headers, me, workspace)
    plan.status = "failed"
    steps[-1].step_status = "failed"
    steps[-1].evidence_refs = [{"kind": "tool_effect", "effect": "publish", "fields": {"tweet_id": "123"}}]
    await db_session.commit()
    response = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers, json={})
    assert response.status_code == 409, response.text
    await db_session.refresh(task)
    await db_session.refresh(plan)
    assert dispatched == []
    assert plan.status == "failed"
    assert "not safely replayable" in response.json()["detail"]


async def test_api_creation_with_agent_and_service_uses_same_planner_as_retry(client, db_session, monkeypatch):
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT
    from packages.core.tasks import ai_tasks
    from packages.core.tasks import runtime_tasks

    dispatched = []
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))
    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", lambda *args: pytest.fail("must use the Task's Plan route"))
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
    assert dispatched == []
    await db_session.commit()
    delivered = await runtime_tasks.consume_task_execution_intent_once(
        event_id=event.id,
        task_id=task.id,
        intent_generation=event.payload["intent_generation"],
    )
    assert delivered["status"] == "admitted"
    assert dispatched == []
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
    assert dispatched == []


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
