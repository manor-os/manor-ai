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
                plan_dag={"steps": []},
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
    from packages.core.models.workspace import Agent, AgentSubscription

    headers, entity_id = await _auth(client, "plan_step_agent_display")
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    resolved_step_id = generate_ulid()
    pending_step_id = generate_ulid()

    async with dbmod.async_session() as db:
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
async def test_retry_failed_plan_steps_resets_only_retryable_steps(client: AsyncClient, monkeypatch):
    calls: list[str] = []
    events: list[tuple[str, str, str | None, dict | None]] = []

    from packages.core.tasks import ai_tasks
    from packages.core.services import event_emitter

    monkeypatch.setattr(ai_tasks.run_plan, "delay", lambda plan_id: calls.append(plan_id))
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda entity_id, event_type, source=None, payload=None: events.append(
            (entity_id, event_type, source, payload)
        ),
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
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda entity_id, event_type, source=None, payload=None: events.append(
            (entity_id, event_type, source, payload)
        ),
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
                plan_dag={"steps": []},
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
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda entity_id, event_type, source=None, payload=None: events.append(
            (entity_id, event_type, source, payload)
        ),
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
