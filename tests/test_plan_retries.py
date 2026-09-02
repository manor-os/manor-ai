import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid


async def _auth(client: AsyncClient, username: str = "planretry") -> tuple[dict, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Plan Retry Corp",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, me.json()["entity_id"]


async def _create_plan(
    entity_id: str,
    *,
    status: str = "failed",
    with_task: bool = False,
) -> tuple[str, str, str, str | None]:
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    plan_id = generate_ulid()
    failed_step_id = generate_ulid()
    done_step_id = generate_ulid()
    task_id = generate_ulid() if with_task else None
    async with dbmod.async_session() as db:
        if task_id:
            db.add(
                Task(
                    id=task_id,
                    entity_id=entity_id,
                    title="Plan retry task",
                    status="failed",
                    priority=3,
                    task_type="general",
                    details={"existing": True, "manual_retry_count": 4},
                    actual_output={"stale": True},
                )
            )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status=status,
                execution_mode="live",
                approval_required=False,
                plan_dag={
                    "steps": [
                        {
                            "key": "failed",
                            "kind": "llm",
                            "service_key": "content",
                            "params": {"prompt": "Retry the failed step."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "done",
                            "kind": "llm",
                            "service_key": "content",
                            "params": {"prompt": "Keep the completed result."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    ]
                },
                last_error={"type": "boom"},
            )
        )
        db.add(
            ExecutionStep(
                id=failed_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="failed",
                kind="llm",
                params={},
                depends_on=[],
                step_status="failed",
                attempt_count=2,
                max_attempts=3,
                error={"type": "ProviderError"},
                result={"stale": True},
            )
        )
        db.add(
            ExecutionStep(
                id=done_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="done",
                kind="llm",
                params={},
                depends_on=[],
                step_status="done",
                attempt_count=1,
                max_attempts=3,
                result={"ok": True},
            )
        )
        await db.commit()
    return plan_id, failed_step_id, done_step_id, task_id


@pytest.mark.asyncio
async def test_plan_steps_include_workspace_chat_agent_display_fields(client: AsyncClient):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    headers, entity_id = await _auth(client, "plan_step_agent_display")
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    resolved_step_id = generate_ulid()
    pending_step_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Plan step display workspace",
                settings={"access_mode": "members_only"},
            )
        )
        db.add(
            Agent(
                id=agent_id,
                entity_id=entity_id,
                name="Content Publisher",
                slug="content-publisher",
                avatar_url="https://example.test/content.png",
                status="active",
            )
        )
        db.add(
            AgentSubscription(
                id=subscription_id,
                entity_id=entity_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                name="Publishing Desk",
                service_key="content_ops",
                status="active",
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        db.add(
            ExecutionStep(
                id=resolved_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="publish_update",
                kind="action",
                service_key="content_ops",
                resolved_subscription_id=subscription_id,
                resolved_agent_id=agent_id,
                provider="twitter_x",
                action_key="publish_tweet",
                params={},
                depends_on=[],
                step_status="done",
                result={"summary": "Published launch update."},
            )
        )
        db.add(
            ExecutionStep(
                id=pending_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="draft_followup",
                kind="llm",
                service_key="content_ops",
                params={},
                depends_on=[],
                step_status="pending",
            )
        )
        await db.commit()

    resp = await client.get(f"/api/v1/plans/{plan_id}/steps", headers=headers)
    assert resp.status_code == 200
    steps = {step["id"]: step for step in resp.json()}

    resolved = steps[resolved_step_id]
    assert resolved["resolved_subscription_id"] == subscription_id
    assert resolved["resolved_agent_id"] == agent_id
    assert resolved["resolved_subscription_name"] == "Publishing Desk"
    assert resolved["resolved_agent_name"] == "Content Publisher"
    assert resolved["resolved_agent_avatar"] == "https://example.test/content.png"
    assert resolved["result"] == {"summary": "Published launch update."}

    pending = steps[pending_step_id]
    assert pending["resolved_subscription_id"] == subscription_id
    assert pending["resolved_agent_id"] == agent_id
    assert pending["resolved_agent_name"] == "Content Publisher"


@pytest.mark.asyncio
async def test_approval_required_plan_surfaces_waiting_task_state_and_log(client: AsyncClient):
    import packages.core.database as dbmod
    from packages.core.models.task import Task, TaskLog
    from packages.core.plans.schema import Plan

    headers, entity_id = await _auth(client, "plan_pending_approval_visible")
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Write report file",
                status="in_progress",
                priority=3,
                task_type="general",
                details={},
            )
        )
        await db.commit()

    plan = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "write_report_file",
                    "kind": "subagent",
                    "service_key": "content",
                    "capability_id": "file.write",
                    "params": {"prompt": "Use generate_file to save the report."},
                    "output_shape": "ArtifactResult",
                    "risk_level": "medium",
                    "requires_approval": True,
                }
            ],
        }
    )

    resp = await client.post(
        "/api/v1/plans",
        headers=headers,
        json={
            "task_id": task_id,
            "execution_mode": "live",
            "approval_required": True,
            "plan": plan.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 201
    plan_id = resp.json()["id"]

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "waiting_on_customer"
        logs = list(
            (await db.execute(select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at)))
            .scalars()
            .all()
        )
        assert any(log.log_type == "ai_hitl_requested" and log.meta.get("plan_id") == plan_id for log in logs)

    approve = await client.post(f"/api/v1/plans/{plan_id}/approve", headers=headers)
    assert approve.status_code == 200

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_plan_approval_dispatch_failure_projects_workspace_recovery(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as dbmod
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(client, "plan_approval_dispatch_failure")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Plan Dispatch Recovery"},
    )
    workspace_id = workspace.json()["id"]
    task_id, plan_id = generate_ulid(), generate_ulid()
    async with dbmod.async_session() as db:
        db.add(Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Resume only after Plan approval",
            status="waiting_on_customer",
            priority=3,
            task_type="general",
            details={},
        ))
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            status="pending_approval",
            execution_mode="live",
            approval_required=True,
            plan_dag={"steps": []},
        ))
        await db.commit()

    def fail_dispatch(_plan_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ai_tasks.run_plan, "delay", fail_dispatch)
    response = await client.post(
        f"/api/v1/plans/{plan_id}/approve",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_attention"
    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        cards = list((await db.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.workspace_id == workspace_id,
                Message.resolved_at.is_(None),
            )
        )).scalars().all())
        recovery = next(
            card for card in cards
            if (card.pending_action or {}).get("kind")
            == PendingActionKind.TASK_RECOVERY.value
        )
        assert task is not None and task.status == "waiting_on_customer"
        assert task.details["_pending_plan_dispatch"] == {
            "plan_id": plan_id,
            "reason": "plan_approval_dispatch_failed",
        }
        assert plan is not None and plan.status == "needs_attention"
        assert plan.last_error["type"] == "PlanContinuationDispatchFailed"
        assert recovery.pending_action["plan_id"] == plan_id


@pytest.mark.asyncio
@pytest.mark.parametrize("with_task", [False, True])
async def test_concurrent_plan_approval_dispatches_once(
    client: AsyncClient,
    monkeypatch,
    with_task: bool,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(
        client,
        f"plan_approval_concurrent_{'task' if with_task else 'standalone'}",
    )
    plan_id = generate_ulid()
    task_id = generate_ulid() if with_task else None
    async with dbmod.async_session() as db:
        if task_id:
            db.add(Task(
                id=task_id,
                entity_id=entity_id,
                title="Approve this Plan once",
                status="waiting_on_customer",
                priority=3,
                task_type="general",
                details={},
            ))
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            task_id=task_id,
            status="pending_approval",
            execution_mode="live",
            approval_required=True,
            plan_dag={"steps": []},
        ))
        await db.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda current_plan_id: dispatched.append(current_plan_id),
    )
    responses = await asyncio.gather(
        client.post(f"/api/v1/plans/{plan_id}/approve", headers=headers),
        client.post(f"/api/v1/plans/{plan_id}/approve", headers=headers),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert dispatched == [plan_id]


@pytest.mark.asyncio
async def test_retry_failed_plan_steps_resets_only_retryable_steps(client: AsyncClient, monkeypatch):
    calls: list[str] = []
    events: list[tuple[str, str, str | None, dict | None]] = []

    from packages.core.tasks import ai_tasks
    from packages.core.services import event_emitter

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))
    async def capture_emit_in_session(
        _db,
        entity_id,
        event_type,
        *,
        source=None,
        payload=None,
        workspace_id=None,
        notify=True,
        deliver_after_commit=False,
    ):
        assert deliver_after_commit is True
        events.append((entity_id, event_type, source, payload))
        return 0

    monkeypatch.setattr(
        event_emitter,
        "emit_in_session",
        capture_emit_in_session,
    )

    headers, entity_id = await _auth(client, "plan_retry_all")
    plan_id, failed_step_id, done_step_id, task_id = await _create_plan(entity_id, with_task=True)

    resp = await client.post(
        f"/api/v1/plans/{plan_id}/retry-failed-steps",
        headers=headers,
        json={"note": "fixed credentials"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["status"] == "draft"
    assert body["reset_steps"] == 1
    assert body["dispatched"] is True
    assert calls == [plan_id]

    steps = await client.get(f"/api/v1/plans/{plan_id}/steps", headers=headers)
    by_id = {step["id"]: step for step in steps.json()}
    assert by_id[failed_step_id]["step_status"] == "pending"
    assert by_id[failed_step_id]["attempt_count"] == 0
    assert by_id[failed_step_id]["error"] is None
    assert by_id[failed_step_id]["result"] is None
    assert by_id[failed_step_id]["human_input_response"]["response"] == "fixed credentials"
    assert by_id[done_step_id]["step_status"] == "done"
    assert by_id[done_step_id]["result"] == {"ok": True}

    assert task_id
    task = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task.status_code == 200
    assert task.json()["status"] == "in_progress"
    assert task.json()["actual_output"] is None
    assert task.json()["details"]["manual_retry_count"] == 5
    assert task.json()["details"]["manual_retry"]["plan_id"] == plan_id
    assert task.json()["details"]["manual_retry"]["step_ids"] == [failed_step_id]

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    retry_log = next(log for log in logs.json() if log["log_type"] == "manual_retry")
    assert retry_log["meta"]["mode"] == "plan_failed_steps"
    assert retry_log["meta"]["plan_id"] == plan_id
    assert retry_log["meta"]["step_ids"] == [failed_step_id]
    assert retry_log["meta"]["reset_steps"] == 1

    retry_events = [event for event in events if event[1] == "task.retried"]
    assert len(retry_events) == 1
    emitted_entity_id, event_type, source, payload = retry_events[0]
    assert emitted_entity_id == entity_id
    assert event_type == "task.retried"
    assert source == "plans_api"
    assert payload["task_id"] == task_id
    assert payload["plan_id"] == plan_id
    assert payload["step_ids"] == [failed_step_id]
    assert payload["mode"] == "plan_failed_steps"
    assert payload["reset_steps"] == 1
    assert payload["retry_count"] == 5
    assert payload["requested_by"]


@pytest.mark.asyncio
async def test_retry_failed_plan_replans_artifact_write_constraint_conflict(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(client, "plan_retry_artifact_constraint")
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        with_task=True,
    )
    assert task_id is not None

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        assert task is not None and plan is not None and step is not None
        task.owner_service_key = "knowledge"
        task.details = {
            **dict(task.details or {}),
            "runtime_context": {
                "instructions": (
                    "Keep this inspection read-only. Do not create or write files, "
                    "including artifact generation."
                ),
            },
        }
        plan.plan_dag = {
            "steps": [
                {
                    "key": "materialize_closeout_diagnosis",
                    "kind": "subagent",
                    "service_key": "knowledge",
                    "params": {"prompt": "Save the closeout artifact."},
                    "depends_on": [],
                    "output_shape": "ArtifactResult",
                    "expects": ["files"],
                }
            ],
        }
        step.step_key = "materialize_closeout_diagnosis"
        step.kind = "subagent"
        step.service_key = "knowledge"
        step.error = {
            "type": "StepResultFailed",
            "message": "No saved closeout artifact was created because file writes are prohibited.",
        }
        await db.commit()

    replanned_tasks: list[str] = []
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "delay",
        lambda current_task_id: replanned_tasks.append(current_task_id),
    )
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda _plan_id: pytest.fail("The impossible persisted step must not be replayed"),
    )

    response = await client.post(
        f"/api/v1/plans/{plan_id}/retry-failed-steps",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["plan"]["status"] == "replanned"
    assert response.json()["reset_steps"] == 0
    assert response.json()["dispatched"] is True
    assert replanned_tasks == [task_id]

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        assert task is not None and plan is not None and step is not None
        assert task.status == "in_progress"
        assert task.details["_replan_context"]["reason"] == "task_constraint_contract_conflict"
        assert "saved artifact" in task.details["_replan_context"]["issue"]
        assert plan.last_error["type"] == "TaskConstraintContractConflict"
        assert step.step_status == "failed"
        assert step.attempt_count == 2


@pytest.mark.asyncio
async def test_plan_retry_dispatch_failure_returns_task_to_retryable_state(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(client, "plan_retry_dispatch_failure")
    plan_id, _failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        with_task=True,
    )
    assert task_id

    def fail_dispatch(_plan_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ai_tasks.run_plan, "delay", fail_dispatch)
    response = await client.post(
        f"/api/v1/plans/{plan_id}/retry-failed-steps",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["dispatched"] is False
    assert response.json()["plan"]["status"] == "needs_attention"
    assert "human decision" not in response.json()["plan"]["last_error"]["message"].lower()
    assert "retry" in response.json()["plan"]["last_error"]["message"].lower()

    task = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task.status_code == 200
    assert task.json()["status"] == "waiting_on_customer"
    assert task.json()["details"]["_pending_plan_dispatch"]["plan_id"] == plan_id

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    dispatch_failure = next(
        log for log in reversed(logs.json())
        if log["log_type"] == "ai_execution_failed"
    )
    assert "human decision" not in dispatch_failure["content"].lower()
    assert "retry" in dispatch_failure["content"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_retry_surface", ["plan", "step"])
async def test_standalone_plan_dispatch_failure_can_be_retried_without_resetting_steps(
    client: AsyncClient,
    monkeypatch,
    second_retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(
        client,
        f"standalone_plan_dispatch_retry_{second_retry_surface}",
    )
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="needs_attention",
        with_task=False,
    )
    assert task_id is None

    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        assert plan is not None and step is not None
        plan.last_error = {
            "type": "PlanContinuationDispatchFailed",
            "message": "The Plan runner was not queued.",
        }
        step.step_status = "failed"
        step.error = {
            "type": "UserRequestedChanges",
            "message": "Revise the saved review.",
            "human_decision": {"choice": "request_changes"},
        }
        await db.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda current_plan_id: dispatched.append(current_plan_id),
    )
    response = await client.post(
        f"/api/v1/plans/{plan_id}/retry-failed-steps",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["reset_steps"] == 0
    assert response.json()["dispatched"] is True
    assert response.json()["plan"]["status"] == "draft"
    assert response.json()["plan"]["last_error"] is None
    assert dispatched == [plan_id]
    second_endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if second_retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )
    second_response = await client.post(
        second_endpoint,
        headers=headers,
        json={},
    )
    assert second_response.status_code == 409, second_response.text
    assert "active" in second_response.json()["detail"].lower()
    async with dbmod.async_session() as db:
        step = await db.get(ExecutionStep, failed_step_id)
        assert step is not None and step.step_status == "failed"
        assert step.error["type"] == "UserRequestedChanges"
        assert step.error["human_decision"]["choice"] == "request_changes"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_pending_plan_cannot_be_retried_while_executor_owns_it(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(client, f"pending_plan_retry_{retry_surface}")
    plan_id, failed_step_id, _done_step_id, _task_id = await _create_plan(
        entity_id,
        status="pending",
    )
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda _plan_id: pytest.fail("An active pending Plan must not be dispatched again"),
    )
    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )

    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert "active" in response.json()["detail"].lower()
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        assert plan is not None and plan.status == "pending"
        assert plan.last_error == {"type": "boom"}
        assert step is not None and step.step_status == "failed"
        assert step.error == {"type": "ProviderError"}
        assert step.result == {"stale": True}
        assert step.attempt_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_open_task_recovery_owns_generic_plan_retry(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        mint_approval_request,
    )
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(client, f"task_recovery_retry_{retry_surface}")
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="failed",
        with_task=True,
    )
    assert task_id is not None

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        task.status = "waiting_on_customer"
        request = await mint_approval_request(
            db,
            subject=ApprovalSubject(
                entity_id=entity_id,
                action_key="task.recover",
                resource_kind="task",
                resource_id=task_id,
            ),
            origin=ApprovalOrigin(
                kind=ApprovalOriginKind.TASK.value,
                task_id=task_id,
                plan_id=plan_id,
            ),
            reason="The task needs operator guidance before retrying.",
            matched_rule="task.needs_recovery",
            hitl_type=HitlType.ERROR.value,
            payload={
                "what_happened": "The task stopped before it finished.",
                "why": "The task needs operator guidance before retrying.",
                "action_to_take": "Retry or cancel the task.",
                "action_link": f"/tasks/{task_id}",
            },
        )
        request_id = request.id
        await db.commit()

    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda _plan_id: pytest.fail("Generic retry must not bypass Task recovery"),
    )
    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )

    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert "Task recovery" in response.json()["detail"]
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        task = await db.get(Task, task_id)
        request = await db.get(HitlRequest, request_id)
        assert plan is not None and plan.status == "failed"
        assert plan.last_error == {"type": "boom"}
        assert step is not None and step.step_status == "failed"
        assert step.error == {"type": "ProviderError"}
        assert step.result == {"stale": True}
        assert task is not None and task.status == "waiting_on_customer"
        assert request is not None and request.status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_task_bound_plan_dispatch_failure_stays_on_task_recovery_path(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(
        client,
        f"task_plan_dispatch_recovery_{retry_surface}",
    )
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="needs_attention",
        with_task=True,
    )
    assert task_id is not None

    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        task = await db.get(Task, task_id)
        assert plan is not None and step is not None and task is not None
        plan.last_error = {
            "type": "PlanContinuationDispatchFailed",
            "message": "The Plan runner was not queued.",
        }
        task.status = "waiting_on_customer"
        task.details = {
            **dict(task.details or {}),
            "_pending_plan_dispatch": {
                "plan_id": plan_id,
                "reason": "plan_approval_dispatch_failed",
            },
        }
        await db.commit()

    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda _plan_id: pytest.fail("Task-bound recovery must not dispatch here"),
    )
    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )
    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert "Task recovery" in response.json()["detail"]
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        task = await db.get(Task, task_id)
        assert plan is not None and plan.status == "needs_attention"
        assert step is not None and step.step_status == "failed"
        assert task is not None and task.status == "waiting_on_customer"
        assert task.details["_pending_plan_dispatch"]["plan_id"] == plan_id


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_generic_plan_retry_cannot_race_task_dispatch_recovery(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    headers, entity_id = await _auth(
        client,
        f"task_dispatch_retry_race_{retry_surface}",
    )
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="needs_attention",
        with_task=True,
    )
    assert task_id is not None
    human_error = {
        "type": "UserDeniedApproval",
        "message": "The user rejected this action.",
        "human_decision": {"choice": "reject"},
    }

    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        task = await db.get(Task, task_id)
        assert plan is not None and step is not None and task is not None
        plan.last_error = {
            "type": "PlanContinuationDispatchFailed",
            "message": "The Plan runner was not queued.",
        }
        step.error = human_error
        task.status = "waiting_on_customer"
        task.details = {
            **dict(task.details or {}),
            "_pending_plan_dispatch": {
                "plan_id": plan_id,
                "reason": "plan_approval_dispatch_failed",
            },
        }
        await db.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda current_plan_id: dispatched.append(current_plan_id),
    )
    task_retry = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )
    assert task_retry.status_code == 200, task_retry.text
    assert task_retry.json()["mode"] == "plan_dispatch"
    assert task_retry.json()["reset_steps"] == 0

    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )
    competing_retry = await client.post(endpoint, headers=headers, json={})

    assert competing_retry.status_code == 409, competing_retry.text
    assert "active" in competing_retry.json()["detail"].lower()
    assert dispatched == [plan_id]
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, failed_step_id)
        task = await db.get(Task, task_id)
        assert plan is not None and plan.status == "running"
        assert plan.last_error is None
        assert step is not None and step.step_status == "failed"
        assert step.error == human_error
        assert task is not None and task.status == "in_progress"
        assert "_pending_plan_dispatch" not in task.details


@pytest.mark.asyncio
async def test_waiting_step_lock_order_matches_task_bound_plan_mutations(
    client: AsyncClient,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.task import Task
    from packages.core.services.step_resume import lock_waiting_step_for_decision

    _headers, entity_id = await _auth(client, "waiting_step_lock_order")
    plan_id, step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="paused",
        with_task=True,
    )
    assert task_id is not None

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        step = await db.get(ExecutionStep, step_id)
        assert task is not None and step is not None
        task.status = "waiting_on_customer"
        step.step_status = "waiting_human"
        step.error = None
        await db.commit()

    async with dbmod.async_session() as db:
        lock_order: list[str] = []

        class RecordingSession:
            async def execute(self, statement, *args, **kwargs):
                sql = str(statement)
                if "FOR UPDATE" in sql:
                    if "FROM tasks" in sql:
                        lock_order.append("task")
                    elif "FROM execution_plans" in sql:
                        lock_order.append("plan")
                    elif "FROM execution_steps" in sql:
                        lock_order.append("step")
                return await db.execute(statement, *args, **kwargs)

        locked = await lock_waiting_step_for_decision(
            RecordingSession(),  # type: ignore[arg-type]
            entity_id=entity_id,
            task_id=task_id,
            plan_id=plan_id,
            step_id=step_id,
        )

        assert locked is not None and locked.id == step_id
        assert lock_order == ["task", "plan", "step"]


@pytest.mark.asyncio
async def test_contract_replan_dispatch_failure_returns_task_to_retryable_state(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as dbmod
    from apps.api.routers.plans import _dispatch_task_replan
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    _headers, entity_id = await _auth(client, "contract_replan_dispatch_failure")
    task_id, plan_id = generate_ulid(), generate_ulid()

    def fail_dispatch(_task_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", fail_dispatch)
    async with dbmod.async_session() as db:
        task = Task(
            id=task_id,
            entity_id=entity_id,
            title="Replan task",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
        plan = ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            task_id=task_id,
            status="replanned",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        )
        db.add_all([task, plan])
        await db.commit()

        dispatched = await _dispatch_task_replan(db, plan)
        assert dispatched is False
        await db.refresh(task)
        assert task.status == "waiting_on_customer"


@pytest.mark.asyncio
async def test_retry_single_step_rejects_done_step(client: AsyncClient):
    headers, entity_id = await _auth(client, "plan_retry_done")
    _plan_id, _failed_step_id, done_step_id, _task_id = await _create_plan(entity_id)

    resp = await client.post(
        f"/api/v1/plans/steps/{done_step_id}/retry",
        headers=headers,
        json={},
    )

    assert resp.status_code == 409
    assert "done" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_replanned_execution_history_cannot_be_retried(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )
    headers, entity_id = await _auth(
        client,
        f"replanned_history_{retry_surface}",
    )
    plan_id, failed_step_id, _done_step_id, _task_id = await _create_plan(
        entity_id,
        status="replanned",
    )
    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )

    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert "replanned" in response.json()["detail"].lower()
    assert dispatched == []
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        failed_step = await db.get(ExecutionStep, failed_step_id)
        assert plan is not None and plan.status == "replanned"
        assert plan.last_error == {"type": "boom"}
        assert failed_step is not None and failed_step.step_status == "failed"
        assert failed_step.error == {"type": "ProviderError"}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_task", [False, True])
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_concurrent_plan_retry_dispatches_once(
    client: AsyncClient,
    monkeypatch,
    with_task: bool,
    retry_surface: str,
):
    """Task-bound and taskless Plans each expose one retry winner."""
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )
    headers, entity_id = await _auth(
        client,
        (
            f"plan_retry_concurrent_{retry_surface}_"
            f"{'task' if with_task else 'standalone'}"
        ),
    )
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        with_task=with_task,
    )
    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )

    responses = await asyncio.gather(
        client.post(endpoint, headers=headers, json={}),
        client.post(endpoint, headers=headers, json={}),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert dispatched == [plan_id]
    if task_id:
        current = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert current.status_code == 200
        assert current.json()["details"]["manual_retry_count"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_retry_waiting_human_review_requires_explicit_card_decision(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )
    headers, entity_id = await _auth(
        client,
        f"plan_human_retry_{retry_surface}",
    )
    plan_id, step_id = generate_ulid(), generate_ulid()
    async with dbmod.async_session() as db:
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            status="paused",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ))
        db.add(ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="review_output",
            kind="human",
            params={"prompt": "Review the output."},
            depends_on=[],
            step_status="waiting_human",
            human_input_prompt="Review the output.",
        ))
        await db.commit()

    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{step_id}/retry"
    )
    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert dispatched == []
    steps = await client.get(f"/api/v1/plans/{plan_id}/steps", headers=headers)
    review_step = next(row for row in steps.json() if row["id"] == step_id)
    assert review_step["step_status"] == "waiting_human"
    assert review_step["human_input_response"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_contract_replan_cannot_bypass_waiting_human_decision(
    client: AsyncClient,
    monkeypatch,
    retry_surface: str,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    replanned: list[str] = []
    resumed: list[str] = []
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "delay",
        lambda task_id: replanned.append(task_id),
    )
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: resumed.append(plan_id),
    )
    headers, entity_id = await _auth(
        client,
        f"plan_contract_waiting_{retry_surface}",
    )
    task_id, plan_id = generate_ulid(), generate_ulid()
    failed_step_id, review_step_id = generate_ulid(), generate_ulid()
    stale_artifact_ref = "${{ steps.prepare.result.outputs.files }}"
    async with dbmod.async_session() as db:
        db.add(Task(
            id=task_id,
            entity_id=entity_id,
            title="Retry a stale Plan with a pending review",
            status="waiting_on_customer",
            priority=3,
            task_type="general",
            owner_service_key="content",
            details={},
        ))
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            task_id=task_id,
            status="needs_attention",
            execution_mode="live",
            approval_required=False,
            plan_dag={
                "steps": [
                    {
                        "key": "prepare",
                        "kind": "subagent",
                        "service_key": "content",
                        "output_shape": "ArtifactResult",
                        "params": {"prompt": "Prepare files."},
                    },
                    {
                        "key": "review",
                        "kind": "human",
                        "depends_on": ["prepare"],
                        "params": {
                            "prompt": "Review files.",
                            "review_artifacts": stale_artifact_ref,
                        },
                    },
                ],
            },
        ))
        db.add_all([
            ExecutionStep(
                id=failed_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="prepare",
                kind="subagent",
                params={},
                depends_on=[],
                step_status="failed",
                error={"type": "ProviderError"},
            ),
            ExecutionStep(
                id=review_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="review",
                kind="human",
                params={"review_artifacts": stale_artifact_ref},
                depends_on=["prepare"],
                step_status="waiting_human",
                human_input_prompt="Review files.",
            ),
        ])
        await db.commit()

    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{failed_step_id}/retry"
    )
    response = await client.post(endpoint, headers=headers, json={})

    assert response.status_code == 409, response.text
    assert "human decision" in response.json()["detail"].lower()
    assert replanned == []
    assert resumed == []
    async with dbmod.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        failed_step = await db.get(ExecutionStep, failed_step_id)
        review_step = await db.get(ExecutionStep, review_step_id)
        assert plan is not None and plan.status == "needs_attention"
        assert failed_step is not None and failed_step.step_status == "failed"
        assert review_step is not None and review_step.step_status == "waiting_human"


@pytest.mark.asyncio
async def test_retry_skipped_step_on_completed_plan_revives_plan(client: AsyncClient, monkeypatch):
    calls: list[str] = []

    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))

    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionStep

    headers, entity_id = await _auth(client, "plan_retry_completed_skipped")
    plan_id, skipped_step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="completed",
        with_task=True,
    )
    async with dbmod.async_session() as db:
        step = await db.get(ExecutionStep, skipped_step_id)
        assert step is not None
        step.step_status = "skipped"
        step.error = None
        await db.commit()

    resp = await client.post(
        f"/api/v1/plans/steps/{skipped_step_id}/retry",
        headers=headers,
        json={},
    )

    assert resp.status_code == 200
    assert resp.json()["plan"]["status"] == "draft"
    assert resp.json()["step"]["step_status"] == "pending"
    assert calls == [plan_id]

    assert task_id
    task = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task.status_code == 200
    assert task.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_retry_single_step_resets_skipped_downstream_dependents(client: AsyncClient, monkeypatch):
    calls: list[str] = []
    events: list[tuple[str, str, str | None, dict | None]] = []

    from packages.core.tasks import ai_tasks
    from packages.core.services import event_emitter

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))
    async def capture_emit_in_session(
        _db,
        entity_id,
        event_type,
        *,
        source=None,
        payload=None,
        workspace_id=None,
        notify=True,
        deliver_after_commit=False,
    ):
        assert deliver_after_commit is True
        events.append((entity_id, event_type, source, payload))
        return 0

    monkeypatch.setattr(
        event_emitter,
        "emit_in_session",
        capture_emit_in_session,
    )

    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    headers, entity_id = await _auth(client, "plan_retry_downstream")
    task_id = generate_ulid()
    plan_id = generate_ulid()
    root_id = generate_ulid()
    child_id = generate_ulid()
    grandchild_id = generate_ulid()
    unrelated_done_id = generate_ulid()
    unrelated_skipped_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Retry downstream task",
                status="failed",
                priority=3,
                task_type="general",
                details={"manual_retry_count": 1},
                actual_output={"stale": True},
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="failed",
                execution_mode="live",
                approval_required=False,
                plan_dag={
                    "steps": [
                        {
                            "key": "project_deep_dives_doc",
                            "kind": "subagent",
                            "service_key": "content",
                            "params": {"prompt": "Create the project deep dives document."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "behavioral_stories_doc",
                            "kind": "subagent",
                            "service_key": "content",
                            "params": {"prompt": "Create the behavioral stories document."},
                            "depends_on": ["project_deep_dives_doc"],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "system_design_angles_doc",
                            "kind": "subagent",
                            "service_key": "content",
                            "params": {"prompt": "Create the system design document."},
                            "depends_on": ["behavioral_stories_doc"],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "unrelated_done",
                            "kind": "subagent",
                            "service_key": "content",
                            "params": {"prompt": "Complete the unrelated work."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "unrelated_skipped",
                            "kind": "subagent",
                            "service_key": "content",
                            "params": {"prompt": "Complete the unrelated dependent work."},
                            "depends_on": ["unrelated_done"],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    ]
                },
            )
        )
        db.add_all(
            [
                ExecutionStep(
                    id=root_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="project_deep_dives_doc",
                    kind="subagent",
                    params={},
                    depends_on=[],
                    step_status="failed",
                    attempt_count=3,
                    max_attempts=3,
                    error={"type": "RuntimeError"},
                ),
                ExecutionStep(
                    id=child_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="behavioral_stories_doc",
                    kind="subagent",
                    params={},
                    depends_on=["project_deep_dives_doc"],
                    step_status="skipped",
                    attempt_count=0,
                    max_attempts=3,
                ),
                ExecutionStep(
                    id=grandchild_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="system_design_angles_doc",
                    kind="subagent",
                    params={},
                    depends_on=["behavioral_stories_doc"],
                    step_status="skipped",
                    attempt_count=0,
                    max_attempts=3,
                ),
                ExecutionStep(
                    id=unrelated_done_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="unrelated_done",
                    kind="subagent",
                    params={},
                    depends_on=[],
                    step_status="done",
                    attempt_count=1,
                    max_attempts=3,
                    result={"ok": True},
                ),
                ExecutionStep(
                    id=unrelated_skipped_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="unrelated_skipped",
                    kind="subagent",
                    params={},
                    depends_on=["unrelated_done"],
                    step_status="skipped",
                    attempt_count=0,
                    max_attempts=3,
                ),
            ]
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/plans/steps/{root_id}/retry",
        headers=headers,
        json={"note": "retry upstream deliverable"},
    )

    assert resp.status_code == 200
    assert resp.json()["step"]["id"] == root_id
    assert resp.json()["step"]["step_status"] == "pending"
    assert calls == [plan_id]

    steps = await client.get(f"/api/v1/plans/{plan_id}/steps", headers=headers)
    by_id = {step["id"]: step for step in steps.json()}
    assert by_id[root_id]["step_status"] == "pending"
    assert by_id[child_id]["step_status"] == "pending"
    assert by_id[grandchild_id]["step_status"] == "pending"
    assert by_id[unrelated_done_id]["step_status"] == "done"
    assert by_id[unrelated_skipped_id]["step_status"] == "skipped"
    assert by_id[child_id]["human_input_response"]["response"] == "retry upstream deliverable"

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    retry_log = next(log for log in logs.json() if log["log_type"] == "manual_retry")
    assert retry_log["meta"]["mode"] == "plan_step"
    assert set(retry_log["meta"]["step_ids"]) == {root_id, child_id, grandchild_id}
    assert retry_log["meta"]["reset_steps"] == 3

    retry_event = next(event for event in events if event[1] == "task.retried")
    assert retry_event[2] == "plans_api"
    assert set(retry_event[3]["step_ids"]) == {root_id, child_id, grandchild_id}
    assert retry_event[3]["reset_steps"] == 3


@pytest.mark.asyncio
async def test_retry_single_failed_step_dispatches_plan(client: AsyncClient, monkeypatch):
    calls: list[str] = []
    events: list[tuple[str, str, str | None, dict | None]] = []

    from packages.core.tasks import ai_tasks
    from packages.core.services import event_emitter

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))
    async def capture_emit_in_session(
        _db,
        entity_id,
        event_type,
        *,
        source=None,
        payload=None,
        workspace_id=None,
        notify=True,
        deliver_after_commit=False,
    ):
        assert deliver_after_commit is True
        events.append((entity_id, event_type, source, payload))
        return 0

    monkeypatch.setattr(
        event_emitter,
        "emit_in_session",
        capture_emit_in_session,
    )

    headers, entity_id = await _auth(client, "plan_retry_one")
    plan_id, failed_step_id, _done_step_id, task_id = await _create_plan(entity_id, with_task=True)

    resp = await client.post(
        f"/api/v1/plans/steps/{failed_step_id}/retry",
        headers=headers,
        json={},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["id"] == plan_id
    assert body["step"]["id"] == failed_step_id
    assert body["step"]["step_status"] == "pending"
    assert body["dispatched"] is True
    assert calls == [plan_id]

    assert task_id
    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    retry_log = next(log for log in logs.json() if log["log_type"] == "manual_retry")
    assert retry_log["meta"]["mode"] == "plan_step"
    assert retry_log["meta"]["plan_id"] == plan_id
    assert retry_log["meta"]["step_ids"] == [failed_step_id]
    retry_events = [event for event in events if event[1] == "task.retried"]
    assert len(retry_events) == 1
    assert retry_events[0][2] == "plans_api"
    assert retry_events[0][3]["mode"] == "plan_step"
    assert retry_events[0][3]["plan_id"] == plan_id
    assert retry_events[0][3]["step_ids"] == [failed_step_id]
