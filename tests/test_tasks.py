"""E2E tests: tasks CRUD, status transitions, logs."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid

pytestmark = pytest.mark.oss_regression


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
    assert data["created_at"]
    assert "updated_at" in data
    assert "status_changed_at" in data


@pytest.mark.asyncio
async def test_historical_agent_creator_projects_original_user_requester(
    client: AsyncClient,
):
    import packages.core.database as dbmod
    from packages.core.models.task import Task
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Agent, WorkspaceActivity

    headers = await _auth(client, "task_legacy_requester")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    historical_headers = await _auth(client, "task_inactive_secondary_member")
    historical_user = (
        await client.get("/api/v1/auth/me", headers=historical_headers)
    ).json()
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Legacy Requester Workspace"},
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]
    agent_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        requester = await db.get(User, historical_user["id"])
        requester.status = "inactive"
        db.add(
            Agent(
                id=agent_id,
                entity_id=me["entity_id"],
                name="Historical Agent Author",
                config={},
                status="inactive",
            )
        )
        db.add(
            Task(
                id=task_id,
                entity_id=me["entity_id"],
                workspace_id=workspace_id,
                title="Historical malformed creator",
                creator_id=agent_id,
                owner_id=agent_id,
                details={
                    "runtime_context": {
                        "captured_from": {"user_id": generate_ulid()},
                    },
                },
            )
        )
        db.add(
            WorkspaceActivity(
                workspace_id=workspace_id,
                entity_id=me["entity_id"],
                event_type="workspace_agent.task_created",
                summary="Workspace Agent created task",
                details={"task_id": task_id},
                user_id=historical_user["id"],
                agent_id=agent_id,
            )
        )
        db.add(
            UserMembership(
                user_id=historical_user["id"],
                entity_id=me["entity_id"],
                role="member",
                status="active",
                is_primary=False,
            )
        )
        await db.commit()

    response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["creator_id"] == historical_user["id"]
    assert body["creator_name"]
    assert body["creator_name"] != "Historical Agent Author"
    assert body["author_agent_id"] == agent_id
    assert body["author_agent_name"] == "Historical Agent Author"


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
async def test_task_runtime_context_cannot_be_set_through_public_crud(client: AsyncClient):
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
            "details": {
                "note": "user-visible metadata",
                "customer_context": {"source": "public_customer_chat"},
                "proposal_external_authorization": {
                    "authorization_id": "forged-authorization",
                },
                "runtime_context": runtime_context,
                "workspace_operation_draft_id": "forged-draft",
                "workspace_work_batch_id": "forged-batch",
            },
        },
    )

    assert resp.status_code == 201
    task = resp.json()
    assert task["details"] == {"note": "user-visible metadata"}

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
            "details": {
                "customer_context": {"source": "public_customer_chat"},
                "proposal_external_authorization": {
                    "authorization_id": "forged-update",
                },
                "runtime_context": updated_context,
                "workspace_operation_draft_id": "forged-update-draft",
                "workspace_work_batch_id": "forged-update-batch",
            },
        },
    )

    assert update.status_code == 200
    assert "runtime_context" not in update.json()["details"]
    assert "customer_context" not in update.json()["details"]
    assert "proposal_external_authorization" not in update.json()["details"]
    assert "workspace_operation_draft_id" not in update.json()["details"]
    assert "workspace_work_batch_id" not in update.json()["details"]


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
    assert resp.json()["updated_at"]
    first_status_changed_at = resp.json()["status_changed_at"]
    assert first_status_changed_at

    # A regular edit gets a reliable updated_at without creating a new status
    # occurrence for attention indicators.
    edit = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"title": "Status Test renamed"},
    )
    assert edit.status_code == 200
    assert edit.json()["updated_at"]
    assert edit.json()["status_changed_at"] == first_status_changed_at

    # in_progress → completed
    resp2 = await client.put(f"/api/v1/tasks/{task_id}", headers=headers, json={"status": "completed"})
    assert resp2.json()["status"] == "completed"
    assert resp2.json()["completed_at"]  # should be set
    assert resp2.json()["updated_at"]
    assert resp2.json()["status_changed_at"] != first_status_changed_at


@pytest.mark.asyncio
async def test_attention_task_filter_uses_status_occurrence_order(client: AsyncClient):
    headers = await _auth(client, "task_attention_order")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Attention order workspace"},
    )
    workspace_id = workspace.json()["id"]
    oldest = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Old task", "workspace_id": workspace_id},
    )
    for index in range(20):
        await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"title": f"New task {index}", "workspace_id": workspace_id},
        )

    failed = await client.put(
        f"/api/v1/tasks/{oldest.json()['id']}",
        headers=headers,
        json={"status": "failed"},
    )
    assert failed.status_code == 200

    attention = await client.get(
        f"/api/v1/tasks?workspace_id={workspace_id}&attention=true&limit=20",
        headers=headers,
    )
    assert attention.status_code == 200
    assert attention.json()["total"] == 1
    assert attention.json()["items"][0]["id"] == oldest.json()["id"]
    assert attention.json()["items"][0]["status_changed_at"]


@pytest.mark.asyncio
async def test_update_workspace_task_status_records_runtime_evidence(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.workspace import WorkspaceActivity

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
    activity = (await db_session.execute(
        select(WorkspaceActivity).where(
            WorkspaceActivity.workspace_id == workspace_id,
            WorkspaceActivity.event_type == "task.status_changed",
        )
    )).scalar_one()
    assert activity.user_id
    assert activity.details == {
        "task_id": task_id,
        "old_status": "pending",
        "new_status": "in_progress",
    }

    activity_response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?event_type=task.status_changed",
        headers=headers,
    )
    assert activity_response.status_code == 200
    status_summary = activity_response.json()[0]["details"]["task_summaries"][0]
    assert status_summary["status"] == "in_progress"


@pytest.mark.asyncio
async def test_cancelled_workspace_task_status_update_is_a_manual_reopen_audit(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.runtime_learning import RuntimeEvidence

    headers = await _auth(client, "taskstatus_manual_reopen")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Manual Reopen Workspace"},
    )
    workspace_id = workspace.json()["id"]
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Manual reopen task", "workspace_id": workspace_id},
    )
    task_id = create.json()["id"]

    cancelled = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "cancelled"},
    )
    assert cancelled.status_code == 200

    reopened = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "in_progress"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "in_progress"

    evidence = (await db_session.execute(
        select(RuntimeEvidence).where(
            RuntimeEvidence.task_id == task_id,
            RuntimeEvidence.evidence_type == "task_status_change",
        ).order_by(RuntimeEvidence.created_at.desc())
    )).scalars().first()
    assert evidence is not None
    assert evidence.status == "succeeded"
    assert evidence.details["old_status"] == "cancelled"
    assert evidence.details["new_status"] == "in_progress"
    assert (await db_session.execute(
        select(ExecutionPlan.id).where(ExecutionPlan.task_id == task_id)
    )).scalar_one_or_none() is None


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
    async def capture_emit_in_session(
        _db,
        entity_id,
        event_type,
        *,
        source=None,
        payload=None,
        **_kwargs,
    ):
        events.append((entity_id, event_type, source, payload))

    monkeypatch.setattr(event_emitter, "emit_in_session", capture_emit_in_session)

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
async def test_concurrent_task_retry_dispatches_once(client: AsyncClient, monkeypatch):
    """The Task row serializes retry entry surfaces before dispatch."""
    from packages.core.tasks import ai_tasks

    calls: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "delay",
        lambda *args: calls.append(args),
    )
    headers = await _auth(client, "taskretry_concurrent")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Retry exactly once", "agent_type": "manor_agent"},
    )
    task_id = create.json()["id"]
    await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "failed"},
    )
    calls.clear()

    responses = await asyncio.gather(
        client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers, json={}),
        client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers, json={}),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_direct_and_chat_task_retry_dispatch_once(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    """Direct Retry and the Workspace recovery card share the Task lock."""
    from sqlalchemy import select as sa_select

    from packages.core.models.task import Task
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl
    from packages.core.tasks import ai_tasks

    calls: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "delay",
        lambda *args: calls.append(args),
    )
    headers = await _auth(client, "taskretry_cross_surface")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Cross-surface retry"},
    )
    workspace_id = workspace.json()["id"]
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Retry from one surface only",
            "workspace_id": workspace_id,
            "agent_type": "manor_agent",
        },
    )
    task_id = create.json()["id"]
    await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "failed"},
    )
    task = (await db_session.execute(
        sa_select(Task).where(Task.id == task_id)
    )).scalar_one()
    card = await ensure_task_recovery_hitl(
        db_session,
        task,
        plan_id=None,
        prompt="The prior run failed.",
        issue="Retry when ready.",
    )
    await db_session.commit()
    assert card is not None
    calls.clear()

    responses = await asyncio.gather(
        client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers, json={}),
        client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{card.id}/resolve",
            headers=headers,
            json={"choice": "retry"},
        ),
    )

    assert all(response.status_code in {200, 409} for response in responses)
    assert any(response.status_code == 200 for response in responses)
    assert len(calls) == 1
    current = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert current.json()["details"]["manual_retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_dispatch_failure_returns_task_to_a_retryable_state(
    client: AsyncClient,
    monkeypatch,
):
    """A broker failure must not strand a committed retry in_progress."""
    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", lambda *_args: None)
    headers = await _auth(client, "taskretry_dispatch_failure")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Retry after queue outage", "agent_type": "manor_agent"},
    )
    task_id = create.json()["id"]
    await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "failed"},
    )

    def fail_dispatch(*_args):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ai_tasks.run_agent_task, "delay", fail_dispatch)
    first = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )
    assert first.status_code == 200, first.text
    assert first.json()["dispatched"] is False
    assert first.json()["task"]["status"] == "waiting_on_customer"

    dispatched: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.run_agent_task,
        "delay",
        lambda *args: dispatched.append(args),
    )
    second = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )
    assert second.status_code == 200, second.text
    assert second.json()["dispatched"] is True
    assert second.json()["task"]["status"] == "in_progress"
    assert dispatched


@pytest.mark.asyncio
async def test_retry_plan_backed_task_records_reset_step_ids(client: AsyncClient, monkeypatch):
    """Task-level retry preserves the exact plan steps it reset."""
    calls = []
    events = []

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
        **_kwargs,
    ):
        events.append((entity_id, event_type, source, payload))

    monkeypatch.setattr(event_emitter, "emit_in_session", capture_emit_in_session)

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
async def test_task_retry_does_not_grant_waiting_plan_approval(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )
    headers = await _auth(client, "taskretry_waiting_approval")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Retry an approval-paused Plan"},
    )
    task_id = created.json()["id"]
    plan_id, step_id, failed_step_id, request_id = (
        generate_ulid(),
        generate_ulid(),
        generate_ulid(),
        generate_ulid(),
    )

    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "waiting_on_customer"
    db_session.add(ExecutionPlan(
        id=plan_id,
        entity_id=me["entity_id"],
        task_id=task_id,
        status="paused",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    ))
    db_session.add(ExecutionStep(
        id=failed_step_id,
        plan_id=plan_id,
        entity_id=me["entity_id"],
        step_key="failed_sibling",
        kind="llm",
        params={},
        depends_on=[],
        step_status="failed",
        error={"type": "ProviderError"},
    ))
    db_session.add(ExecutionStep(
        id=step_id,
        plan_id=plan_id,
        entity_id=me["entity_id"],
        step_key="publish_update",
        kind="action",
        action_key="social.publish",
        params={"message": "Launch update"},
        depends_on=[],
        step_status="waiting_human",
        human_input_prompt="Approve publishing?",
    ))
    db_session.add(HitlRequest(
        id=request_id,
        entity_id=me["entity_id"],
        action_key="social.publish",
        origin_kind="step",
        origin_step_id=step_id,
        origin_plan_id=plan_id,
        origin_task_id=task_id,
        status="pending",
        dedup_key=f"step:{step_id}",
        hitl_type="authorize",
        payload={},
        context={},
    ))
    await db_session.commit()

    response = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )

    assert response.status_code == 409, response.text
    assert dispatched == []
    db_session.expire_all()
    request = await db_session.get(HitlRequest, request_id)
    step = await db_session.get(ExecutionStep, step_id)
    failed_step = await db_session.get(ExecutionStep, failed_step_id)
    assert request is not None and request.status == "pending"
    assert step is not None and step.step_status == "waiting_human"
    assert failed_step is not None and failed_step.step_status == "failed"


@pytest.mark.asyncio
async def test_forged_pending_plan_dispatch_cannot_bypass_plan_approval(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )
    headers = await _auth(client, "taskretry_forged_dispatch_marker")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Do not bypass Plan approval"},
    )
    task_id = created.json()["id"]
    plan_id = generate_ulid()

    filtered_update = await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={
            "details": {
                "_pending_plan_dispatch": {
                    "plan_id": plan_id,
                    "reason": "forged",
                },
                "_replan_context": {"approval_constraints": []},
                "user_note": "keep this",
            },
        },
    )
    assert filtered_update.status_code == 200, filtered_update.text
    assert filtered_update.json()["details"] == {"user_note": "keep this"}

    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "waiting_on_customer"
    # Simulate legacy/corrupt persisted data so Retry must remain fail-closed
    # even when the public API validation is bypassed.
    task.details = {
        "_pending_plan_dispatch": {
            "plan_id": plan_id,
            "reason": "forged",
        },
    }
    db_session.add(ExecutionPlan(
        id=plan_id,
        entity_id=me["entity_id"],
        task_id=task_id,
        status="pending_approval",
        execution_mode="live",
        approval_required=True,
        plan_dag={"steps": []},
    ))
    await db_session.commit()

    response = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )

    assert response.status_code == 409, response.text
    assert dispatched == []
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    plan = await db_session.get(ExecutionPlan, plan_id)
    assert task is not None and task.status == "waiting_on_customer"
    assert plan is not None and plan.status == "pending_approval"
    assert plan.approval_required is True


@pytest.mark.asyncio
async def test_task_contract_replan_cannot_bypass_waiting_human_decision(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.tasks import ai_tasks

    replanned: list[str] = []
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "delay",
        lambda task_id: replanned.append(task_id),
    )
    headers = await _auth(client, "taskretry_contract_waiting")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Retry stale Plan pending review"},
    )
    task_id = created.json()["id"]
    plan_id, failed_step_id, review_step_id = (
        generate_ulid(),
        generate_ulid(),
        generate_ulid(),
    )
    stale_artifact_ref = "${{ steps.prepare.result.outputs.files }}"

    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "waiting_on_customer"
    task.owner_service_key = "content"
    db_session.add(ExecutionPlan(
        id=plan_id,
        entity_id=me["entity_id"],
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
    db_session.add_all([
        ExecutionStep(
            id=failed_step_id,
            plan_id=plan_id,
            entity_id=me["entity_id"],
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
            entity_id=me["entity_id"],
            step_key="review",
            kind="human",
            params={"review_artifacts": stale_artifact_ref},
            depends_on=["prepare"],
            step_status="waiting_human",
            human_input_prompt="Review files.",
        ),
    ])
    await db_session.commit()

    response = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )

    assert response.status_code == 409, response.text
    assert "human decision" in response.json()["detail"].lower()
    assert replanned == []
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    plan = await db_session.get(ExecutionPlan, plan_id)
    failed_step = await db_session.get(ExecutionStep, failed_step_id)
    review_step = await db_session.get(ExecutionStep, review_step_id)
    assert task is not None and task.status == "waiting_on_customer"
    assert plan is not None and plan.status == "needs_attention"
    assert failed_step is not None and failed_step.step_status == "failed"
    assert review_step is not None and review_step.step_status == "waiting_human"


@pytest.mark.asyncio
async def test_retry_unconsumed_proposal_external_task_starts_new_plan(
    client: AsyncClient,
    monkeypatch,
):
    """An unspent external action must not resume a verification-only plan."""
    new_plan_calls = []
    resumed_plan_calls = []

    from packages.core.tasks import ai_tasks

    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "delay",
        lambda task_id: new_plan_calls.append(task_id),
    )
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: resumed_plan_calls.append(plan_id),
    )

    headers = await _auth(client, "taskretry_external_proposal")
    me = await client.get("/api/v1/auth/me", headers=headers)
    entity_id = me.json()["entity_id"]

    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Publish approved video"},
    )
    task_id = create.json()["id"]
    await client.put(
        f"/api/v1/tasks/{task_id}",
        headers=headers,
        json={"status": "failed"},
    )

    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task, TaskLog

    plan_id = generate_ulid()
    failed_step_id = generate_ulid()
    approved_runtime_context = {
        "instructions": "Publish the approved MP4 to YouTube exactly once.",
        "rules": [
            {
                "rule_type": "approval_required",
                "capability_patterns": ["external.social"],
            },
            {
                "rule_type": "deny",
                "action_patterns": ["upload_other_file", "second_upload"],
            },
        ],
        "required_capabilities": ["external.social"],
    }
    verification_only_runtime_context = {
        **approved_runtime_context,
        "instructions": (
            "Publish the approved MP4 to YouTube exactly once.\n"
            "Latest user constraint: verify only; do not upload or publish."
        ),
        "rules": [
            *approved_runtime_context["rules"],
            {
                "rule_type": "deny",
                "action_patterns": ["video.upload_*", "video.publish_*"],
                "capability_patterns": ["external.social"],
            },
        ],
    }
    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        task.owner_service_key = "stickman.distribution"
        task.details = {
            "external_action": {
                "provider": "youtube",
                "action": "publish_video",
                "destination": "studio.youtube.com",
            },
            "proposal_external_authorization": {
                "authorization_id": "proposal-item:publish-task",
                "task_id": task_id,
                "consumed_at": None,
            },
            "_replan_context": {
                "instruction": (
                    "This retry was constrained to verification only: "
                    "no uploads, edits, publishing, or external side effects."
                ),
            },
            "runtime_context": verification_only_runtime_context,
        }
        db.add_all([
            TaskLog(
                task_id=task_id,
                log_type="runtime_context",
                content="Workspace Agent updated task runtime requirements.",
                meta={"runtime_context": approved_runtime_context},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            ),
            TaskLog(
                task_id=task_id,
                log_type="runtime_context",
                content="Workspace Agent updated task runtime requirements.",
                meta={"runtime_context": verification_only_runtime_context},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            ),
        ])
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
                step_key="verify_public_youtube_url",
                kind="llm",
                params={},
                depends_on=[],
                step_status="failed",
                error={"type": "StepResultFailed"},
                attempt_count=3,
                max_attempts=3,
            )
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers=headers,
        json={},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "plan_new"
    assert body["plan_id"] is None
    assert body["reset_steps"] == 0
    assert new_plan_calls == [task_id]
    assert resumed_plan_calls == []

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        failed_step = await db.get(ExecutionStep, failed_step_id)
        assert "_replan_context" not in (task.details or {})
        assert task.details["runtime_context"] == approved_runtime_context
        assert plan.status == "failed"
        assert failed_step.step_status == "failed"


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
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Conversation, Message

    workspace_signals = []
    runtime_updates: list[tuple[str, dict]] = []

    async def fake_process_workspace_task_comment(**kwargs):
        workspace_signals.append(kwargs)

    async def capture_runtime_update(entity_id: str, **payload):
        runtime_updates.append((entity_id, payload))

    monkeypatch.setattr(
        "apps.api.routers.tasks.process_workspace_task_comment",
        fake_process_workspace_task_comment,
    )
    monkeypatch.setattr(
        "packages.core.services.realtime.broadcast_task_runtime_update",
        capture_runtime_update,
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
    assert runtime_updates[-1][1] == {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "event": "task_approval_resolved",
    }

    chat_cards = list((await db_session.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.workspace_id == workspace_id)
    )).scalars().all())
    task_card = next(
        message for message in chat_cards
        if (message.pending_action or {}).get("task_id") == task_id
    )
    await db_session.refresh(task_card)
    assert task_card.resolved_at is not None
    request = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.id == task_card.pending_action["approval_request_id"]
        )
    )).scalar_one()
    assert request.status == ApprovalStatus.CONSUMED.value

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
async def test_plan_hitl_dispatch_failure_returns_task_to_retryable_state(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    headers = await _auth(client, "task_hitl_dispatch_failure")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Resume a paused Plan"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    plan_id, step_id, approval_step_id = (
        generate_ulid(), generate_ulid(), generate_ulid()
    )

    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "waiting_on_customer"
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=me["entity_id"],
        task_id=task_id,
        status="paused",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    step = ExecutionStep(
        id=step_id,
        plan_id=plan_id,
        entity_id=me["entity_id"],
        step_key="await_operator_input",
        kind="subagent",
        params={},
        depends_on=[],
        step_status="waiting_human",
        human_input_prompt="Which option should be used?",
    )
    approval_step = ExecutionStep(
        id=approval_step_id,
        plan_id=plan_id,
        entity_id=me["entity_id"],
        step_key="approve_publish",
        kind="subagent",
        params={},
        depends_on=[],
        step_status="waiting_human",
        requires_approval=True,
        human_input_prompt="Approve publishing?",
    )
    db_session.add_all([plan, step, approval_step])
    await db_session.commit()

    def fail_dispatch(_plan_id: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.run_plan.delay",
        fail_dispatch,
    )
    agent_dispatches: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.run_agent_task.delay",
        lambda task_id, agent_id: agent_dispatches.append((task_id, agent_id)),
    )
    missing_origin = await client.post(
        f"/api/v1/tasks/{task_id}/hitl-response",
        headers=headers,
        json={"response": "Use option A"},
    )
    assert missing_origin.status_code == 409, missing_origin.text
    assert agent_dispatches == []

    response = await client.post(
        f"/api/v1/tasks/{task_id}/hitl-response",
        headers=headers,
        json={"step_id": step_id, "response": "Use option A"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["dispatched"] is False
    assert body["task"]["status"] == "waiting_on_customer"
    db_session.expire_all()
    plan = await db_session.get(ExecutionPlan, plan_id)
    task = await db_session.get(Task, task_id)
    step = await db_session.get(ExecutionStep, step_id)
    approval_step = await db_session.get(ExecutionStep, approval_step_id)
    assert plan is not None and plan.status == "needs_attention"
    assert plan.last_error["type"] == "PlanContinuationDispatchFailed"
    assert task is not None and task.status == "waiting_on_customer"
    assert task.details["_pending_plan_dispatch"] == {
        "plan_id": plan_id,
        "reason": "task_hitl_dispatch_failed",
    }
    assert step is not None and step.step_status == "pending"
    assert approval_step is not None and approval_step.step_status == "waiting_human"


@pytest.mark.asyncio
async def test_pending_approval_plan_never_falls_through_to_legacy_agent_hitl(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task

    headers = await _auth(client, "task_hitl_pending_plan")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Approve this Plan before execution"},
    )
    task_id = created.json()["id"]
    task = await db_session.get(Task, task_id)
    assert task is not None
    task.status = "waiting_on_customer"
    task.agent_type = "manor_agent"
    db_session.add(ExecutionPlan(
        id=generate_ulid(),
        entity_id=me["entity_id"],
        task_id=task_id,
        status="pending_approval",
        execution_mode="live",
        approval_required=True,
        plan_dag={"steps": []},
    ))
    await db_session.commit()

    agent_dispatches: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.run_agent_task.delay",
        lambda task_id, agent_id: agent_dispatches.append((task_id, agent_id)),
    )
    response = await client.post(
        f"/api/v1/tasks/{task_id}/hitl-response",
        headers=headers,
        json={"response": "Continue"},
    )

    assert response.status_code == 409, response.text
    assert "planner execution" in response.json()["detail"].lower()
    assert agent_dispatches == []
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    assert task is not None and task.status == "waiting_on_customer"


@pytest.mark.asyncio
async def test_approval_language_does_not_turn_execution_task_into_approval_task(
    client: AsyncClient,
):
    headers = await _auth(client, "taskapproval_prose")
    create = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Recover the video after approval render failed",
            "description": "Produce the missing MP4, then prepare it for approval.",
            "task_type": "general",
            "details": {
                "runtime_context": {
                    "instructions": "pending_founder_review applies after the artifact exists",
                },
            },
        },
    )
    assert create.status_code == 201
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
async def test_approval_task_is_actionable_from_workspace_chat(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select as sa_select

    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Message

    runtime_updates: list[tuple[str, dict]] = []

    async def capture_runtime_update(entity_id: str, **payload):
        runtime_updates.append((entity_id, payload))

    monkeypatch.setattr(
        "packages.core.services.realtime.broadcast_task_runtime_update",
        capture_runtime_update,
    )

    headers = await _auth(client, "taskapproval_chat")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Chat Approval Workspace"},
    )
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Approve the launch article",
            "task_type": "approval",
            "workspace_id": workspace_id,
            "details": {"review_material": {"headline": "Launch day"}},
        },
    )
    task_id = created.json()["id"]

    messages = list((await db_session.execute(
        sa_select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.workspace_id == workspace_id,
            Message.resolved_at.is_(None),
        )
    )).scalars().all())
    card = next(
        message for message in messages
        if (message.pending_action or {}).get("kind")
        == PendingActionKind.TASK_APPROVAL.value
    )
    assert card.pending_action["task_id"] == task_id
    assert card.pending_action["review"] == {"headline": "Launch day"}
    assert card.pending_action["options"] == ["approve", "request_changes"]
    request_id = card.pending_action["approval_request_id"]

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{card.id}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resolved.status_code == 200
    task = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task.json()["status"] == "completed"
    assert task.json()["details"]["approval_decision"]["approved"] is True
    assert runtime_updates[-1][1] == {
        "task_id": task_id,
        "plan_id": None,
        "workspace_id": workspace_id,
        "event": "workspace_chat_action_resolved",
    }

    request = (await db_session.execute(
        sa_select(HitlRequest).where(HitlRequest.id == request_id)
    )).scalar_one()
    await db_session.refresh(request)
    assert request.status == ApprovalStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_concurrent_direct_and_chat_task_approval_records_one_decision(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select as sa_select

    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message, Task, TaskLog

    headers = await _auth(client, "taskapproval_concurrent")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Concurrent Task Approval"},
    )
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Choose one approval decision",
            "task_type": "approval",
            "workspace_id": workspace_id,
        },
    )
    task_id = created.json()["id"]
    card = next(
        message
        for message in list((await db_session.execute(
            sa_select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.workspace_id == workspace_id,
                Message.resolved_at.is_(None),
            )
        )).scalars().all())
        if (message.pending_action or {}).get("kind")
        == PendingActionKind.TASK_APPROVAL.value
    )
    card_id = card.id

    responses = await asyncio.gather(
        client.post(
            f"/api/v1/tasks/{task_id}/approval",
            headers=headers,
            json={"choice": "approve"},
        ),
        client.post(
            f"/api/v1/workspaces/{workspace_id}/chat/messages/{card_id}/resolve",
            headers=headers,
            json={"choice": "request_changes", "note": "Revise it."},
        ),
    )

    assert all(response.status_code in {200, 409} for response in responses)
    assert any(response.status_code == 200 for response in responses)
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    card = await db_session.get(Message, card_id)
    logs = list((await db_session.execute(
        sa_select(TaskLog).where(
            TaskLog.task_id == task_id,
            TaskLog.log_type == "approval_decision",
        )
    )).scalars().all())
    assert task is not None and task.status == "completed"
    assert card is not None and card.resolved_at is not None
    assert len(logs) == 1
    assert task.details["approval_decision"]["choice"] in {
        "approve",
        "request_changes",
    }


@pytest.mark.asyncio
async def test_task_recovery_hitl_is_visible_and_cancel_closes_task(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select as sa_select

    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Task
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

    runtime_updates: list[tuple[str, dict]] = []

    async def capture_runtime_update(entity_id: str, **payload):
        runtime_updates.append((entity_id, payload))

    monkeypatch.setattr(
        "packages.core.services.realtime.broadcast_task_runtime_update",
        capture_runtime_update,
    )

    headers = await _auth(client, "taskrecovery_chat")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Chat Recovery Workspace"},
    )
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Render the missing MP4",
            "workspace_id": workspace_id,
        },
    )
    task_id = created.json()["id"]
    task = (await db_session.execute(
        sa_select(Task).where(Task.id == task_id)
    )).scalar_one()
    task.status = "waiting_on_customer"
    card = await ensure_task_recovery_hitl(
        db_session,
        task,
        plan_id="plan-recovery-test",
        prompt="The render completed without a saved MP4.",
        issue="No saved file path was recorded.",
    )
    await db_session.commit()

    assert card is not None
    assert card.pending_action["kind"] == PendingActionKind.TASK_RECOVERY.value
    assert card.pending_action["options"] == ["retry", "cancel"]
    request_id = card.pending_action["approval_request_id"]
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{card.id}/resolve",
        headers=headers,
        json={"choice": "cancel"},
    )
    assert resolved.status_code == 200
    task_response = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert task_response.json()["status"] == "cancelled"
    assert runtime_updates[-1][1] == {
        "task_id": task_id,
        "plan_id": "plan-recovery-test",
        "workspace_id": workspace_id,
        "event": "workspace_chat_action_resolved",
    }

    request = (await db_session.execute(
        sa_select(HitlRequest).where(HitlRequest.id == request_id)
    )).scalar_one()
    await db_session.refresh(request)
    assert request.status == ApprovalStatus.DENIED.value


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
        timeout=5,
    )

    assert resp.status_code == 201
    await asyncio.wait_for(started.wait(), timeout=5)
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
