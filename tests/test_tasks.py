"""E2E tests: tasks CRUD, status transitions, logs."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid


async def _auth(client: AsyncClient, username: str = "taskuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Task Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_task(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Fix the leaking faucet",
            "description": "Kitchen sink faucet drips constantly",
            "priority": 2,
            "task_type": "maintenance",
            "details": {"unit": "304", "allow_entry": True},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Fix the leaking faucet"
    assert data["priority"] == 2
    assert data["task_type"] == "maintenance"
    assert data["status"] == "pending"
    assert data["details"]["unit"] == "304"
    assert data["creator_id"]  # should be set to current user
    assert data["owner_id"] == data["creator_id"]
    assert data["visibility"] == "entity"
    assert data["client_visible"] is False


@pytest.mark.asyncio
async def test_task_assignee_display_resolves_entity_people_and_agents(client: AsyncClient):
    headers = await _auth(client, "task_assignee_display")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Simon",
            "email": "simon.assignee@test.com",
        },
    )
    assert staff_resp.status_code == 201
    staff_id = staff_resp.json()["id"]

    staff_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Set up meeting note taker integration",
            "assignee_id": staff_id,
        },
    )
    assert staff_task.status_code == 201
    assert staff_task.json()["assignee_id"] == staff_id
    assert staff_task.json()["assignee_name"] == "Simon"

    owner_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Owner assigned task",
            "assignee_id": me["entity_id"],
        },
    )
    expected_owner_name = me.get("display_name") or me["email"]
    assert owner_task.status_code == 201
    assert owner_task.json()["assignee_name"] == expected_owner_name

    agent_resp = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Research Agent",
            "description": "Researches task context",
            "system_prompt": "You research.",
        },
    )
    assert agent_resp.status_code == 201
    agent_id = agent_resp.json()["id"]

    agent_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Agent assigned task",
            "agent_id": agent_id,
            "agent_type": "agent",
        },
    )
    assert agent_task.status_code == 201
    assert agent_task.json()["agent_name"] == "Research Agent"

    legacy_agent_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Legacy agent assignee task",
            "assignee_id": agent_id,
        },
    )
    assert legacy_agent_task.status_code == 201
    assert legacy_agent_task.json()["assignee_name"] == "Research Agent"

    manor_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Manor AI assigned task",
            "agent_id": "manor-master",
            "agent_type": "manor_agent",
        },
    )
    assert manor_task.status_code == 201
    assert manor_task.json()["agent_name"] == "Manor AI"

    listed = await client.get("/api/v1/tasks", headers=headers)
    assert listed.status_code == 200
    by_title = {item["title"]: item for item in listed.json()["items"]}
    assert by_title["Set up meeting note taker integration"]["assignee_name"] == "Simon"
    assert by_title["Owner assigned task"]["assignee_name"] == expected_owner_name
    assert by_title["Agent assigned task"]["agent_name"] == "Research Agent"
    assert by_title["Legacy agent assignee task"]["assignee_name"] == "Research Agent"
    assert by_title["Manor AI assigned task"]["agent_name"] == "Manor AI"


@pytest.mark.asyncio
async def test_task_runtime_context_round_trips_through_create_and_update(client: AsyncClient):
    headers = await _auth(client, "taskruntime")
    runtime_context = {
        "instructions": "Only create new workspace files.",
        "required_refs": ["doc_brand"],
        "rules": [
            {
                "rule_key": "create_only",
                "rule_type": "deny",
                "description": "Do not edit existing workspace files.",
                "action_patterns": ["workspace.file.modify", "workspace.file.delete", "workspace.file.write"],
                "enabled": True,
            }
        ],
    }
    resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Runtime scoped task",
            "details": {"runtime_context": runtime_context},
        },
    )

    assert resp.status_code == 201
    task = resp.json()
    assert task["details"]["runtime_context"] == runtime_context

    updated_context = {
        **runtime_context,
        "instructions": "Require approval before social posts.",
        "rules": [
            {
                "rule_key": "social_approval",
                "rule_type": "approval_required",
                "description": "Review social posts before publishing.",
                "action_patterns": ["social_post.publish"],
                "enabled": True,
            }
        ],
    }
    update = await client.put(
        f"/api/v1/tasks/{task['id']}",
        headers=headers,
        json={
            "details": {"runtime_context": updated_context},
        },
    )

    assert update.status_code == 200
    assert update.json()["details"]["runtime_context"] == updated_context


@pytest.mark.asyncio
async def test_list_tasks_with_filter(client: AsyncClient):
    headers = await _auth(client)
    # Create tasks with different statuses
    await client.post("/api/v1/tasks", headers=headers, json={"title": "Task 1"})
    t2 = await client.post("/api/v1/tasks", headers=headers, json={"title": "Task 2"})
    # Update one to in_progress
    await client.put(f"/api/v1/tasks/{t2.json()['id']}", headers=headers, json={"status": "in_progress"})

    # List all
    resp = await client.get("/api/v1/tasks", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Filter by status
    resp2 = await client.get("/api/v1/tasks?status=pending", headers=headers)
    assert resp2.json()["total"] == 1
    assert resp2.json()["items"][0]["title"] == "Task 1"

    resp3 = await client.get("/api/v1/tasks?status=in_progress", headers=headers)
    assert resp3.json()["total"] == 1
    assert resp3.json()["items"][0]["title"] == "Task 2"


@pytest.mark.asyncio
async def test_workspace_task_filters_accept_camel_case_alias(client: AsyncClient):
    headers = await _auth(client, "task_workspace_alias")
    workspace_a = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Workspace A"})
    workspace_b = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Workspace B"})
    assert workspace_a.status_code == 201
    assert workspace_b.status_code == 201
    workspace_a_id = workspace_a.json()["id"]
    workspace_b_id = workspace_b.json()["id"]

    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Workspace A task",
            "workspace_id": workspace_a_id,
        },
    )
    await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Workspace B task",
            "workspace_id": workspace_b_id,
        },
    )
    await client.post("/api/v1/tasks", headers=headers, json={"title": "Standalone task"})

    listed = await client.get(f"/api/v1/tasks?workspaceId={workspace_a_id}", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["title"] == "Workspace A task"

    board = await client.get(f"/api/v1/tasks/board?workspaceId={workspace_a_id}", headers=headers)
    assert board.status_code == 200
    board_tasks = [task for status, tasks in board.json().items() if status != "_counts" for task in tasks]
    assert [task["title"] for task in board_tasks] == ["Workspace A task"]


@pytest.mark.asyncio
async def test_update_task_status(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/tasks", headers=headers, json={"title": "Status Test"})
    task_id = create.json()["id"]

    # pending → in_progress
    resp = await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "in_progress"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"
    assert resp.json()["started_at"]  # should be set

    # in_progress → completed
    resp2 = await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "completed"})
    assert resp2.json()["status"] == "completed"
    assert resp2.json()["completed_at"]  # should be set


@pytest.mark.asyncio
async def test_update_workspace_task_status_records_runtime_evidence(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence

    headers = await _auth(client, "taskstatus_evidence")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Status Evidence Workspace"},
    )
    workspace_id = workspace.json()["id"]
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Status evidence task",
            "workspace_id": workspace_id,
        },
    )
    task_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "in_progress"},
    )

    assert resp.status_code == 200
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "task_status_change",
            )
        )
    ).scalar_one()
    assert evidence.source == "task_ui"
    assert evidence.workspace_id == workspace_id
    assert evidence.details["old_status"] == "pending"
    assert evidence.details["new_status"] == "in_progress"


@pytest.mark.asyncio
async def test_independent_task_status_records_runtime_evidence(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence

    headers = await _auth(client, "independent_status_evidence")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Independent status evidence task",
        },
    )
    task_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "in_progress"},
    )

    assert resp.status_code == 200
    assert resp.json()["workspace_id"] is None
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "task_status_change",
            )
        )
    ).scalar_one()
    assert evidence.source == "task_ui"
    assert evidence.workspace_id is None
    assert evidence.details["old_status"] == "pending"
    assert evidence.details["new_status"] == "in_progress"


@pytest.mark.asyncio
async def test_independent_task_runtime_context_is_not_workspace_scoped(
    client: AsyncClient,
    db_session,
):
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    headers = await _auth(client, "independent_task_runtime")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Independent agent task",
            "description": "Do this without a workspace.",
        },
    )
    body = create.json()

    runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=body["entity_id"],
        task_id=body["id"],
    )

    assert runtime.workspace_id is None
    assert runtime.runtime_profile is None
    assert runtime.extra_context
    assert "## Active Task Thread" in runtime.extra_context
    assert "workspace_update_task_runtime" not in runtime.extra_context


@pytest.mark.asyncio
async def test_task_logs(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/tasks", headers=headers, json={"title": "Log Test"})
    task_id = create.json()["id"]

    # Creation log should exist
    logs_resp = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    assert len(logs) >= 1
    assert logs[0]["log_type"] == "create"

    # Add a comment
    await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=headers,
        json={
            "content": "Plumber scheduled for Tuesday",
            "log_type": "comment",
        },
    )

    # Change status
    await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "in_progress"})

    # Should have 3 logs: create, comment, status_change
    logs2 = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    assert len(logs2.json()) == 3
    types = {log["log_type"] for log in logs2.json()}
    assert types == {"create", "comment", "status_change"}


@pytest.mark.asyncio
async def test_task_with_deadline(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Deadline Task",
            "deadline": "2026-05-01T09:00:00",
        },
    )
    assert resp.status_code == 201
    assert "2026-05-01" in resp.json()["deadline"]


@pytest.mark.asyncio
async def test_date_only_deadline_counts_overdue_after_due_date(client: AsyncClient):
    headers = await _auth(client, "task_date_only_deadline")
    today = datetime.now(timezone.utc).date()

    due_today = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Due today should not be overdue",
            "deadline": today.isoformat(),
        },
    )
    assert due_today.status_code == 201

    due_yesterday = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Due yesterday should be overdue",
            "deadline": (today - timedelta(days=1)).isoformat(),
        },
    )
    assert due_yesterday.status_code == 201

    stats = await client.get("/api/v1/dashboard/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["tasks"]["overdue"] == 1


@pytest.mark.asyncio
async def test_task_isolation(client: AsyncClient):
    """User A can't see User B's tasks."""
    headers_a = await _auth(client, "task_a")
    headers_b = await _auth(client, "task_b")

    create = await client.post("/api/v1/tasks", headers=headers_a, json={"title": "A's task"})
    task_id = create.json()["id"]

    # B can't see it
    resp = await client.get(f"/api/v1/tasks/{task_id}", headers=headers_b)
    assert resp.status_code == 404

    # B's list is empty
    resp2 = await client.get("/api/v1/tasks", headers=headers_b)
    assert resp2.json()["total"] == 0


@pytest.mark.asyncio
async def test_assign_to_manor_ai(client: AsyncClient):
    """Assign task to Manor AI agent."""
    headers = await _auth(client)
    create = await client.post("/api/v1/tasks", headers=headers, json={"title": "AI Task"})
    task_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={
            "agent_type": "manor_agent",
            "status": "in_progress",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["agent_type"] == "manor_agent"
    assert resp.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_retry_failed_agent_task(client: AsyncClient, monkeypatch):
    """Manual retry re-dispatches an assigned agent task and records a log."""
    calls = []
    events = []

    from packages.core.tasks import ai_tasks
    from packages.core.services import event_emitter

    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", lambda *args: calls.append(args))
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda entity_id, event_type, source=None, payload=None: events.append(
            (entity_id, event_type, source, payload)
        ),
    )

    headers = await _auth(client, "taskretry")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Retry AI task",
            "agent_type": "manor_agent",
        },
    )
    task_id = create.json()["id"]

    await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "failed"})

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={
            "note": "Credentials were fixed; try again.",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["status"] == "in_progress"
    assert body["mode"] == "agent"
    assert body["dispatched"] is True
    assert len(calls) >= 2  # initial assignment + manual retry

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    retry_log = next(log for log in logs.json() if log["log_type"] == "manual_retry")
    assert retry_log["meta"]["mode"] == "agent"
    assert retry_log["meta"]["retry_count"] == 1
    retry_event = next(event for event in events if event[1] == "task.retried")
    assert retry_event[2] == "tasks_api"
    assert retry_event[3]["task_id"] == task_id
    assert retry_event[3]["mode"] == "agent"
    assert retry_event[3]["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_plan_backed_task_records_reset_step_ids(client: AsyncClient, monkeypatch):
    """Task-level retry preserves the exact plan steps it reset."""
    calls = []
    events = []

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

    headers = await _auth(client, "taskretry_plan")
    me = await client.get("/api/v1/auth/me", headers=headers)
    entity_id = me.json()["entity_id"]

    create = await client.post("/api/v1/tasks", headers=headers, json={"title": "Retry plan task"})
    task_id = create.json()["id"]
    await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "failed"})

    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    plan_id = generate_ulid()
    failed_step_id = generate_ulid()
    done_step_id = generate_ulid()
    async with dbmod.async_session() as db:
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
                error={"type": "ProviderError"},
                result={"stale": True},
                attempt_count=2,
                max_attempts=3,
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
                result={"ok": True},
            )
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={
            "note": "dependency is available now",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "plan"
    assert body["plan_id"] == plan_id
    assert body["reset_steps"] == 1
    assert calls == [plan_id]

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    retry_log = next(log for log in logs.json() if log["log_type"] == "manual_retry")
    assert retry_log["meta"]["mode"] == "plan"
    assert retry_log["meta"]["plan_id"] == plan_id
    assert retry_log["meta"]["step_ids"] == [failed_step_id]
    assert retry_log["meta"]["reset_steps"] == 1

    steps = await client.get(f"/api/v1/plans/{plan_id}/steps", headers=headers)
    by_id = {step["id"]: step for step in steps.json()}
    assert by_id[failed_step_id]["step_status"] == "pending"
    assert by_id[done_step_id]["step_status"] == "done"

    retry_event = next(event for event in events if event[1] == "task.retried")
    assert retry_event[2] == "tasks_api"
    assert retry_event[3]["mode"] == "plan"
    assert retry_event[3]["plan_id"] == plan_id
    assert retry_event[3]["step_ids"] == [failed_step_id]
    assert retry_event[3]["reset_steps"] == 1


@pytest.mark.asyncio
async def test_retry_task_without_executor_returns_409(client: AsyncClient):
    headers = await _auth(client, "taskretry_no_executor")
    create = await client.post("/api/v1/tasks", headers=headers, json={"title": "Manual only"})
    task_id = create.json()["id"]

    await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "failed"})

    resp = await client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers, json={})
    assert resp.status_code == 409
    assert "no plan" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_approval_task_decision_records_output_log_and_workspace_signal(
    client: AsyncClient,
    monkeypatch,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence

    workspace_signals = []

    async def fake_process_workspace_task_comment(**kwargs):
        workspace_signals.append(kwargs)

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )

    headers = await _auth(client, "taskapproval")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Approval Workspace"},
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]

    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Founder approval: weekly calendar",
            "task_type": "approval",
            "workspace_id": workspace_id,
            "details": {
                "runtime_context": {
                    "instructions": "pending_founder_review until the user approves",
                },
            },
        },
    )
    assert create.status_code == 201
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/approval",
        headers=headers,
        json={"choice": "approve", "note": "Looks good. Continue publishing prep."},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["details"]["approval_decision"]["decision"] == "approved"
    assert body["details"]["approval_decision"]["approved"] is True
    assert body["details"]["approval_decision"]["note"] == "Looks good. Continue publishing prep."
    assert body["actual_output"]["approval"]["approved"] is True
    assert "Looks good" in body["actual_output"]["summary"]

    logs = await client.get(f"/api/v1/tasks/{task_id}/logs", headers=headers)
    approval_log = next(log for log in logs.json() if log["log_type"] == "approval_decision")
    assert approval_log["meta"]["decision"] == "approved"
    assert approval_log["meta"]["approved"] is True
    assert "Looks good" in approval_log["content"]

    await asyncio.sleep(0)

    assert workspace_signals
    assert workspace_signals[0]["task_id"] == task_id
    assert workspace_signals[0]["entity_id"]
    assert workspace_signals[0]["comment"].startswith("Approval decision")
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "approval_decision",
            )
        )
    ).scalar_one()
    assert evidence.workspace_id == workspace_id
    assert evidence.source == "task_ui"
    assert evidence.details["decision"] == "approved"
    assert evidence.details["approved"] is True
    assert evidence.details["note"] == "Looks good. Continue publishing prep."

    activity = await client.get(f"/api/v1/workspaces/{workspace_id}/activity", headers=headers)
    assert activity.status_code == 200
    approval_activity = next(row for row in activity.json() if row["event_type"] == "task.approval_decision")
    assert approval_activity["details"]["task_id"] == task_id
    assert approval_activity["details"]["approved"] is True
    assert approval_activity["details"]["task_summaries"][0]["title"] == "Founder approval: weekly calendar"
    assert approval_activity["details"]["task_summaries"][0]["status"] == "completed"
    assert "approved task 'Founder approval: weekly calendar'" in approval_activity["summary"]


@pytest.mark.asyncio
async def test_non_approval_task_rejects_approval_decision(client: AsyncClient):
    headers = await _auth(client, "taskapproval_reject")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Regular execution task",
            "task_type": "general",
        },
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/approval",
        headers=headers,
        json={"choice": "approve"},
    )

    assert resp.status_code == 400
    assert "not an approval task" in resp.json()["detail"].lower()

    task = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_approval_task_request_changes_records_negative_decision(client: AsyncClient):
    headers = await _auth(client, "taskapproval_changes")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Approval: revise launch copy",
            "task_type": "approval",
        },
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/approval",
        headers=headers,
        json={"choice": "request_changes", "note": "Make the CTA less aggressive."},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["details"]["approval_decision"]["decision"] == "changes_requested"
    assert body["details"]["approval_decision"]["approved"] is False
    assert body["actual_output"]["approval"]["note"] == "Make the CTA less aggressive."


@pytest.mark.asyncio
async def test_workspace_task_comment_schedules_agent_without_blocking(
    client: AsyncClient,
    monkeypatch,
    db_session,
):
    from sqlalchemy import select as sa_select
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.workspace import WorkspaceActivity, Agent
    from packages.core.models.task import Task
    import packages.core.database as dbmod

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_process_workspace_task_comment(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )

    headers = await _auth(client, "taskcomment_nonblocking")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]

    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Comment Runtime Workspace"},
    )
    workspace_id = workspace.json()["id"]
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Comment should wake workspace agent",
            "workspace_id": workspace_id,
        },
    )
    task_id = create.json()["id"]

    # Give the task an agent owner so auto-reply fires under the new gating rules.
    # Previously this test relied on the old behavior (workspace task always auto-replied);
    # after the assignee-gating change only agent-owned tasks auto-reply.
    agent = Agent(entity_id=entity_id, name="Nonblocking Test Agent")
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    async with dbmod.async_session() as db:
        task_obj = (await db.execute(
            sa_select(Task).where(Task.id == task_id)
        )).scalar_one()
        task_obj.agent_id = agent.id
        await db.commit()

    resp = await asyncio.wait_for(
        client.post(
            f"/api/v1/tasks/{task_id}/logs",
            headers=headers,
            json={"content": "Please adapt the next work wave.", "log_type": "comment"},
        ),
        timeout=1,
    )

    assert resp.status_code == 201
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)
    assert calls[0]["task_id"] == task_id
    assert calls[0]["comment"] == "Please adapt the next work wave."
    evidence = (
        await db_session.execute(
            sa_select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "task_comment",
            )
        )
    ).scalar_one()
    assert evidence.source == "task_ui"
    assert evidence.workspace_id == workspace_id
    assert evidence.details["comment"] == "Please adapt the next work wave."
    activity = (
        await db_session.execute(
            sa_select(WorkspaceActivity).where(
                WorkspaceActivity.workspace_id == workspace_id,
                WorkspaceActivity.event_type == "task.comment",
            )
        )
    ).scalar_one()
    assert activity.details["task_id"] == task_id
    assert activity.details["comment_preview"] == "Please adapt the next work wave."


@pytest.mark.asyncio
async def test_workspace_task_comment_guidance_creates_learning_candidate(
    client: AsyncClient,
    monkeypatch,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.tasks import ai_tasks

    async def fake_process_workspace_task_comment(**kwargs):
        return None

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )
    enqueued: list[dict] = []

    def fake_apply_async(*, args=None, kwargs=None, countdown=None):
        enqueued.append({"args": args, "kwargs": kwargs, "countdown": countdown})

    monkeypatch.setattr(ai_tasks.apply_learning_candidate_async, "apply_async", fake_apply_async)

    headers = await _auth(client, "taskcomment_learning")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Comment Learning Workspace"},
    )
    workspace_id = workspace.json()["id"]
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Capture durable task guidance",
            "workspace_id": workspace_id,
        },
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=headers,
        json={
            "content": "以后你是这个 workspace 的 lease consultant，负责先总结客户需求再推荐房源。",
            "log_type": "comment",
        },
    )

    assert resp.status_code == 201
    candidate = (
        await db_session.execute(
            select(AgentLearningCandidate).where(
                AgentLearningCandidate.workspace_id == workspace_id,
                AgentLearningCandidate.candidate_type == "agent_profile_patch",
            )
        )
    ).scalar_one()
    assert candidate.status == "accepted"
    assert candidate.resolution["apply_status"] == "queued"
    assert candidate.resolution["approval_mode"] == "auto"
    assert candidate.payload["target_scope"] == "workspace_agent"
    assert enqueued
    assert enqueued[0]["args"][1] == candidate.id


@pytest.mark.asyncio
async def test_independent_task_comment_guidance_creates_global_learning_candidate(
    client: AsyncClient,
    monkeypatch,
    db_session,
):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
    from packages.core.tasks import ai_tasks

    workspace_signals = []

    async def fake_process_workspace_task_comment(**kwargs):
        workspace_signals.append(kwargs)

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )
    enqueued: list[dict] = []

    def fake_apply_async(*, args=None, kwargs=None, countdown=None):
        enqueued.append({"args": args, "kwargs": kwargs, "countdown": countdown})

    monkeypatch.setattr(ai_tasks.apply_learning_candidate_async, "apply_async", fake_apply_async)

    headers = await _auth(client, "independent_task_learning")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Independent learning task",
        },
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=headers,
        json={
            "content": "以后你是我的 research assistant，负责先总结目标再执行。",
            "log_type": "comment",
        },
    )

    assert resp.status_code == 201
    assert workspace_signals == []
    evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.task_id == task_id,
                RuntimeEvidence.evidence_type == "task_comment",
            )
        )
    ).scalar_one()
    assert evidence.workspace_id is None
    candidate = (
        await db_session.execute(
            select(AgentLearningCandidate).where(
                AgentLearningCandidate.workspace_id.is_(None),
                AgentLearningCandidate.candidate_type == "agent_profile_patch",
            )
        )
    ).scalar_one()
    assert candidate.status == "accepted"
    assert candidate.resolution["apply_status"] == "queued"
    assert enqueued
    assert enqueued[0]["args"][1] == candidate.id


async def _wait_until(cond, timeout: float = 1.0) -> None:
    """Poll cond() up to *timeout* seconds (20 × 50 ms)."""
    for _ in range(int(timeout / 0.05)):
        await asyncio.sleep(0.05)
        if cond():
            return


async def test_comment_mentions_dispatch_and_assignee_gating(
    client: AsyncClient, monkeypatch, db_session,
):
    """@agent mentions fan out serially; user/无主 task 不再自动回复."""
    from packages.core.models.workspace import Agent

    processed = []

    async def fake_process_workspace_task_comment(**kwargs):
        processed.append(kwargs)

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )
    notified = []

    async def fake_notify_mentioned_users(**kwargs):
        notified.append(kwargs)

    monkeypatch.setattr(
        "apps.api.routers.tasks.notify_mentioned_users",
        fake_notify_mentioned_users,
    )

    headers = await _auth(client, "commentmention")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]
    user_id = me["id"]

    ws = await client.post("/api/v1/workspaces", headers=headers,
                           json={"name": "Mention WS"})
    workspace_id = ws.json()["id"]

    # entity-owned mentionable agent (create via db_session)
    agent = Agent(entity_id=entity_id, name="Helper Agent")
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)

    # ── Case 1: unassigned task + no mentions → no agent runs ──
    t1 = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Human-only task", "workspace_id": workspace_id,
    })
    task1_id = t1.json()["id"]
    resp = await client.post(f"/api/v1/tasks/{task1_id}/logs", headers=headers,
                             json={"content": "just a human note", "log_type": "comment"})
    assert resp.status_code == 201
    await asyncio.sleep(0.05)
    assert processed == []

    # ── Case 2: unassigned task + @agent → mentioned agent runs ──
    resp = await client.post(f"/api/v1/tasks/{task1_id}/logs", headers=headers, json={
        "content": "please help @Helper",
        "log_type": "comment",
        "mentions": [{"type": "agent", "id": agent.id}],
    })
    assert resp.status_code == 201
    assert resp.json()["meta"]["mentions"] == [
        {"type": "agent", "id": agent.id, "name": "Helper Agent"},
    ]
    await _wait_until(lambda: bool(processed))
    assert [p["responding_agent_id"] for p in processed] == [agent.id]

    # ── Case 3: invalid mention id is dropped → still no run ──
    processed.clear()
    resp = await client.post(f"/api/v1/tasks/{task1_id}/logs", headers=headers, json={
        "content": "ghost @nobody", "log_type": "comment",
        "mentions": [{"type": "agent", "id": "01FAKEAGENT000000000000000"}],
    })
    assert resp.status_code == 201
    assert resp.json()["meta"].get("mentions") in (None, [])
    await asyncio.sleep(0.05)
    assert processed == []

    # ── Case 4: staff mention → notify fan-out regardless of assignee ──
    # mention the authenticated user themselves — excluded INSIDE notify_mentioned_users,
    # which is mocked here — so asserting the mocked call receives the id is correct.
    resp = await client.post(f"/api/v1/tasks/{task1_id}/logs", headers=headers, json={
        "content": "fyi", "log_type": "comment",
        "mentions": [{"type": "user", "id": user_id}],
    })
    assert resp.status_code == 201
    await _wait_until(lambda: bool(notified))
    assert len(notified) == 1
    assert notified[0]["mentioned_user_ids"] == [user_id]


@pytest.mark.asyncio
async def test_comment_agent_owned_task_still_auto_replies_and_stacks_mentions(
    client: AsyncClient, monkeypatch, db_session,
):
    """agent_id 任务保持自动回复; @mention 叠加串行处理; 重复 agent 去重."""
    from packages.core.models.workspace import Agent
    from packages.core.models.task import Task
    import packages.core.database as dbmod
    from sqlalchemy import select as sa_select

    processed = []

    async def fake_process_workspace_task_comment(**kwargs):
        processed.append(kwargs)

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )

    headers = await _auth(client, "commentmention2")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]

    ws = await client.post("/api/v1/workspaces", headers=headers,
                           json={"name": "Mention WS 2"})
    workspace_id = ws.json()["id"]

    owner = Agent(entity_id=entity_id, name="Owner Agent")
    other = Agent(entity_id=entity_id, name="Other Agent")
    db_session.add_all([owner, other])
    await db_session.commit()
    await db_session.refresh(owner)
    await db_session.refresh(other)

    t = await client.post("/api/v1/tasks", headers=headers, json={
        "title": "Agent-owned", "workspace_id": workspace_id,
    })
    task_id = t.json()["id"]
    # POST /tasks doesn't accept agent_id — set it directly in the DB
    async with dbmod.async_session() as db:
        task_obj = (await db.execute(
            sa_select(Task).where(Task.id == task_id)
        )).scalar_one()
        task_obj.agent_id = owner.id
        await db.commit()

    # no mentions → assigned agent auto-replies
    await client.post(f"/api/v1/tasks/{task_id}/logs", headers=headers,
                      json={"content": "status?", "log_type": "comment"})
    # poll up to 1s for the call to arrive
    for _ in range(20):
        await asyncio.sleep(0.05)
        if processed:
            break
    assert [p["responding_agent_id"] for p in processed] == [owner.id]

    # @other stacks on top of the owner, deduped, serial order: owner first
    processed.clear()
    await client.post(f"/api/v1/tasks/{task_id}/logs", headers=headers, json={
        "content": "second opinion @Other",
        "log_type": "comment",
        "mentions": [{"type": "agent", "id": other.id},
                     {"type": "agent", "id": owner.id}],   # owner dup dropped
    })
    # poll up to 1s for both calls to arrive
    for _ in range(20):
        await asyncio.sleep(0.05)
        if len(processed) >= 2:
            break
    assert [p["responding_agent_id"] for p in processed] == [owner.id, other.id]
