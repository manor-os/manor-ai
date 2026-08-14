"""E2E tests: scheduled jobs, job runs, agent executions."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "scheduser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Sched Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_scheduled_job(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "daily-report-001",
            "name": "Daily Report",
            "job_type": "cron",
            "cron_expr": "0 9 * * *",
            "timezone": "America/New_York",
            "agent_id": "agent-abc",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["job_id"] == "daily-report-001"
    assert data["name"] == "Daily Report"
    assert data["cron_expr"] == "0 9 * * *"
    assert data["enabled"] is True
    assert data["timezone"] == "America/New_York"
    assert data["agent_id"] == "agent-abc"


@pytest.mark.asyncio
async def test_list_scheduled_jobs(client: AsyncClient):
    headers = await _auth(client)
    # Create two jobs
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "job-a",
            "name": "Job A",
        },
    )
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "job-b",
            "name": "Job B",
        },
    )

    resp = await client.get("/api/v1/jobs", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2
    assert len(resp.json()["items"]) == 2


@pytest.mark.asyncio
async def test_list_scheduled_jobs_filters_by_workspace(client: AsyncClient):
    headers = await _auth(client, "sched_workspace_filter")
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "workspace-job-a",
            "name": "Workspace Job A",
            "workspace_id": "ws-a",
        },
    )
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "workspace-job-b",
            "name": "Workspace Job B",
            "workspace_id": "ws-b",
        },
    )

    resp = await client.get("/api/v1/jobs?workspace_id=ws-a", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == "workspace-job-a"
    assert data["items"][0]["workspace_id"] == "ws-a"


@pytest.mark.asyncio
async def test_list_scheduled_jobs_supports_filters_and_pagination(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import update

    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "sched_list_controls")
    jobs = [
        {
            "job_id": "alpha-enabled",
            "name": "Alpha enabled",
            "payload_message": "Prepare the alpha report",
            "workspace_id": "ws-controls",
            "agent_id": "agent-one",
        },
        {
            "job_id": "alpha-paused",
            "name": "Alpha paused",
            "workspace_id": "ws-controls",
            "agent_id": "agent-one",
        },
        {
            "job_id": "beta-attention",
            "name": "Beta attention",
            "workspace_id": "ws-controls",
            "agent_id": "agent-two",
        },
        {
            "job_id": "other-workspace",
            "name": "Alpha elsewhere",
            "workspace_id": "ws-other",
            "agent_id": "agent-one",
        },
    ]
    for job in jobs:
        response = await client.post("/api/v1/jobs", headers=headers, json=job)
        assert response.status_code == 201

    await db_session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.job_id == "alpha-paused")
        .values(enabled=False)
    )
    await db_session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.job_id == "beta-attention")
        .values(consecutive_errors=2, last_status="error")
    )
    await db_session.commit()

    first_page = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={
            "workspace_id": "ws-controls",
            "agent_id": "agent-one",
            "search": "alpha",
            "limit": 1,
            "offset": 0,
        },
    )
    second_page = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={
            "workspace_id": "ws-controls",
            "agent_id": "agent-one",
            "search": "alpha",
            "limit": 1,
            "offset": 1,
        },
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert first_page.json()["summary_total"] == 2
    assert first_page.json()["enabled_total"] == 1
    assert first_page.json()["attention_total"] == 0
    assert len(first_page.json()["items"]) == 1
    assert len(second_page.json()["items"]) == 1
    assert first_page.json()["items"][0]["id"] != second_page.json()["items"][0]["id"]

    paused = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": "ws-controls", "status": "paused"},
    )
    attention = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": "ws-controls", "status": "attention"},
    )

    assert paused.status_code == 200
    assert [item["job_id"] for item in paused.json()["items"]] == ["alpha-paused"]
    assert attention.status_code == 200
    assert [item["job_id"] for item in attention.json()["items"]] == ["beta-attention"]


@pytest.mark.asyncio
async def test_list_scheduled_jobs_includes_latest_failure_reason(
    client: AsyncClient,
    db_session,
):
    from packages.core.services.scheduler_service import create_job_run

    headers = await _auth(client, "sched_latest_error")
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "job-with-error",
            "name": "Job With Error",
        },
    )
    await create_job_run(
        db_session,
        "job-with-error",
        "error",
        error="Latest automation failure",
    )
    await db_session.commit()
    await create_job_run(
        db_session,
        "job-with-error",
        "error",
        error="Newest automation failure",
    )
    from packages.core.models.scheduler import ScheduledJob
    from sqlalchemy import update

    await db_session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.job_id == "job-with-error")
        .values(consecutive_errors=2, last_status="error")
    )
    await db_session.commit()

    response = await client.get("/api/v1/jobs", headers=headers)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["last_error"] == "Newest automation failure"


@pytest.mark.asyncio
async def test_successful_workspace_chat_job_delivers_result_once(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import func, select

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.task import Conversation, Message
    from packages.core.services.task_service import create_task
    from packages.core.tasks.ai_tasks import _update_job_run_status_async

    headers = await _auth(client, "sched_workspace_delivery")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Scheduled delivery"},
    )
    workspace = workspace_response.json()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"delivery:{workspace['id']}",
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
        name="Daily public metrics",
        job_type="cron",
        timezone="UTC",
        execution_type="agent",
        default_delivery_mode="workspace_chat",
        enabled=True,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        workspace["entity_id"],
        title="[Auto] Daily public metrics",
        description="Read the public metrics once.",
        task_type="ai_generated",
        workspace_id=workspace["id"],
        details={
            "scheduled_job_id": job.job_id,
            "default_delivery_mode": "workspace_chat",
        },
    )
    task.status = "in_progress"
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    job.manor_task_id = task.id
    db_session.add(run)
    await db_session.commit()

    @asynccontextmanager
    async def session_context():
        yield db_session

    result = {
        "status": "completed",
        "response": "**Daily YouTube public metrics** — views 0, comments unavailable.",
        "duration_ms": 1250,
    }
    await _update_job_run_status_async(session_context, task.id, result)
    await _update_job_run_status_async(session_context, task.id, result)

    messages = (
        await db_session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.workspace_id == workspace["id"],
                Message.meta["scheduled_run_id"].as_string() == run.id,
            )
        )
    ).scalars().all()
    message_count = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.workspace_id == workspace["id"])
    )

    assert message_count == 1
    assert len(messages) == 1
    assert messages[0].content == result["response"]
    assert messages[0].author_kind == "agent"
    assert messages[0].meta["scheduled_job_id"] == job.job_id
    assert run.status == "success"
    assert job.last_status == "success"


@pytest.mark.asyncio
async def test_update_scheduled_job(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "update-me",
            "name": "Original Name",
        },
    )
    job_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/jobs/{job_id}",
        headers=headers,
        json={
            "name": "Updated Name",
            "cron_expr": "0 12 * * *",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"
    assert resp.json()["cron_expr"] == "0 12 * * *"


@pytest.mark.asyncio
async def test_toggle_scheduled_job(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "toggle-me",
            "name": "Toggle Test",
        },
    )
    job_id = create.json()["id"]
    assert create.json()["enabled"] is True

    # Disable
    resp = await client.post(
        f"/api/v1/jobs/{job_id}/toggle",
        headers=headers,
        json={
            "enabled": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # Re-enable
    resp2 = await client.post(
        f"/api/v1/jobs/{job_id}/toggle",
        headers=headers,
        json={
            "enabled": True,
        },
    )
    assert resp2.json()["enabled"] is True


@pytest.mark.asyncio
async def test_delete_scheduled_job(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "delete-me",
            "name": "Delete Test",
        },
    )
    job_id = create.json()["id"]

    resp = await client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 204

    # Verify gone
    resp2 = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_job_runs(client: AsyncClient):
    """Create a job, then verify runs endpoint returns empty initially."""
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "run-test-job",
            "name": "Run Test",
        },
    )
    job_id = create.json()["id"]

    resp = await client.get(f"/api/v1/jobs/{job_id}/runs", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_job_run_occurrence_claim_is_idempotent(db_session):
    from packages.core.services.scheduler_service import claim_job_run

    first, first_claimed = await claim_job_run(
        db_session,
        "occurrence-claim-job",
        "running",
        idempotency_key="cron:2026-08-13T14:07:00+00:00",
        trigger_type="cron",
    )
    duplicate, duplicate_claimed = await claim_job_run(
        db_session,
        "occurrence-claim-job",
        "running",
        idempotency_key="cron:2026-08-13T14:07:00+00:00",
        trigger_type="cron",
    )
    next_run, next_claimed = await claim_job_run(
        db_session,
        "occurrence-claim-job",
        "running",
        idempotency_key="cron:2026-08-13T14:08:00+00:00",
        trigger_type="cron",
    )

    assert first_claimed is True
    assert duplicate_claimed is False
    assert duplicate.id == first.id
    assert next_claimed is True
    assert next_run.id != first.id


@pytest.mark.asyncio
async def test_run_now_propagates_client_idempotency_key(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.tasks.scheduler_tasks import _dispatch_job_task

    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        _dispatch_job_task,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    headers = await _auth(client, "sched_run_now_idempotency")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"job_id": "manual-many-times", "name": "Manual many times"},
    )
    job_id = created.json()["id"]

    first_headers = {**headers, "Idempotency-Key": "operator-request-1"}
    first = await client.post(f"/api/v1/jobs/{job_id}/run_now", headers=first_headers)
    redelivery = await client.post(f"/api/v1/jobs/{job_id}/run_now", headers=first_headers)
    second = await client.post(
        f"/api/v1/jobs/{job_id}/run_now",
        headers={**headers, "Idempotency-Key": "operator-request-2"},
    )

    assert first.status_code == 202
    assert redelivery.status_code == 202
    assert second.status_code == 202
    assert first.json()["idempotency_key"] == "operator-request-1"
    assert redelivery.json()["idempotency_key"] == "operator-request-1"
    assert second.json()["idempotency_key"] == "operator-request-2"
    assert queued[0][1]["occurrence_key"] == "manual:operator-request-1"
    assert queued[1][1]["occurrence_key"] == "manual:operator-request-1"
    assert queued[2][1]["occurrence_key"] == "manual:operator-request-2"


@pytest.mark.asyncio
async def test_create_agent_execution(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/executions",
        headers=headers,
        json={
            "agent_id": "agent-007",
            "input_message": "Summarize today's tasks",
            "max_turns": 3,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] == "agent-007"
    assert data["status"] == "running"
    assert data["turns_used"] == 0
    assert data["max_turns"] == 3
    assert data["input_message"] == "Summarize today's tasks"
    assert data["started_at"] is not None


@pytest.mark.asyncio
async def test_create_agent_execution_defaults_to_50_turns(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/executions",
        headers=headers,
        json={
            "agent_id": "agent-default",
            "input_message": "Use the default turn budget",
        },
    )

    assert resp.status_code == 201
    assert resp.json()["max_turns"] == 50


@pytest.mark.asyncio
async def test_list_agent_executions(client: AsyncClient):
    headers = await _auth(client)
    await client.post(
        "/api/v1/executions",
        headers=headers,
        json={
            "agent_id": "agent-a",
            "input_message": "Hello A",
        },
    )
    await client.post(
        "/api/v1/executions",
        headers=headers,
        json={
            "agent_id": "agent-b",
            "input_message": "Hello B",
        },
    )

    # List all
    resp = await client.get("/api/v1/executions", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Filter by agent_id
    resp2 = await client.get("/api/v1/executions?agent_id=agent-a", headers=headers)
    assert resp2.json()["total"] == 1
    assert resp2.json()["items"][0]["agent_id"] == "agent-a"


@pytest.mark.asyncio
async def test_update_agent_execution(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/executions",
        headers=headers,
        json={
            "agent_id": "agent-upd",
        },
    )
    exec_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/executions/{exec_id}",
        headers=headers,
        json={
            "status": "completed",
            "turns_used": 4,
            "output_message": "Done!",
            "duration_ms": 1234.5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["turns_used"] == 4
    assert data["output_message"] == "Done!"
    assert data["duration_ms"] == 1234.5


@pytest.mark.asyncio
async def test_scheduler_isolation(client: AsyncClient):
    """User A cannot see User B's jobs or executions."""
    headers_a = await _auth(client, "sched_a")
    headers_b = await _auth(client, "sched_b")

    # A creates a job
    create_job = await client.post(
        "/api/v1/jobs",
        headers=headers_a,
        json={
            "job_id": "a-private-job",
            "name": "A's Job",
        },
    )
    job_id = create_job.json()["id"]

    # B cannot see it
    resp = await client.get(f"/api/v1/jobs/{job_id}", headers=headers_b)
    assert resp.status_code == 404

    # B's list is empty
    resp2 = await client.get("/api/v1/jobs", headers=headers_b)
    assert resp2.json()["total"] == 0

    # A creates an execution
    create_exec = await client.post(
        "/api/v1/executions",
        headers=headers_a,
        json={
            "agent_id": "agent-iso",
        },
    )
    assert create_exec.status_code == 201

    # B's execution list is empty
    resp3 = await client.get("/api/v1/executions", headers=headers_b)
    assert resp3.json()["total"] == 0


@pytest.mark.asyncio
async def test_workspace_job_routes_require_workspace_access(client: AsyncClient):
    from tests.test_document_permissions import _create_entity_user
    from tests.test_workspace_write_authz import _make_workspace

    owner_headers = await _auth(client, "sched_scope_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    ws_id = await _make_workspace(me["entity_id"], "Private Scheduler", me.get("user_id") or me.get("id"))
    created = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={"job_id": "private-scheduled-job", "name": "Private job", "workspace_id": ws_id},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    outsider = await _create_entity_user(me["entity_id"], "sched_scope_outsider", "member")

    assert (await client.get("/api/v1/jobs", headers=outsider["headers"])).json()["total"] == 0
    assert (await client.get(f"/api/v1/jobs/{job_id}", headers=outsider["headers"])).status_code == 404
    assert (await client.put(
        f"/api/v1/jobs/{job_id}", headers=outsider["headers"], json={"name": "hijack"}
    )).status_code == 403
    assert (await client.post(
        f"/api/v1/jobs/{job_id}/toggle", headers=outsider["headers"], json={"enabled": False}
    )).status_code == 403
    assert (await client.get(
        f"/api/v1/jobs/{job_id}/runs", headers=outsider["headers"]
    )).status_code == 404


@pytest.mark.asyncio
async def test_viewer_cannot_run_workspace_job_now(client: AsyncClient, monkeypatch):
    """Manual dispatch is a write operation for Workspace-scoped jobs."""
    from tests.test_document_permissions import _create_entity_user
    from tests.test_workspace_write_authz import _add_member, _make_workspace

    owner_headers = await _auth(client, "sched_run_now_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    owner_id = me.get("user_id") or me.get("id")
    ws_id = await _make_workspace(me["entity_id"], "Run Now Private", owner_id)
    created = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={"job_id": "run-now-private", "name": "Run now private", "workspace_id": ws_id},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    viewer = await _create_entity_user(me["entity_id"], "sched_run_now_viewer", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")

    class _UnexpectedDispatch:
        def delay(self, *args, **kwargs):
            raise AssertionError("viewer request reached the scheduler queue")

    import packages.core.tasks.scheduler_tasks as scheduler_tasks
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job_task", _UnexpectedDispatch())

    response = await client.post(
        f"/api/v1/jobs/{job_id}/run_now",
        headers=viewer["headers"],
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_agent_execution_workspace_scope_is_read_write_protected(client: AsyncClient):
    from tests.test_document_permissions import _create_entity_user
    from tests.test_workspace_write_authz import _add_member, _make_workspace

    owner_headers = await _auth(client, "execution_scope_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    owner_id = me.get("user_id") or me.get("id")
    ws_id = await _make_workspace(me["entity_id"], "Execution Private", owner_id)
    other_ws_id = await _make_workspace(me["entity_id"], "Execution Other", owner_id)
    created = await client.post(
        "/api/v1/executions",
        headers=owner_headers,
        json={"agent_id": "workspace-agent", "workspace_id": ws_id},
    )
    assert created.status_code == 201, created.text
    execution_id = created.json()["id"]
    other_created = await client.post(
        "/api/v1/executions",
        headers=owner_headers,
        json={"agent_id": "other-workspace-agent", "workspace_id": other_ws_id},
    )
    assert other_created.status_code == 201, other_created.text

    viewer = await _create_entity_user(me["entity_id"], "execution_scope_viewer", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")

    listed = await client.get("/api/v1/executions", headers=viewer["headers"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == execution_id

    created_by_viewer = await client.post(
        "/api/v1/executions",
        headers=viewer["headers"],
        json={"agent_id": "viewer-agent", "workspace_id": ws_id},
    )
    assert created_by_viewer.status_code == 403, created_by_viewer.text

    updated_by_viewer = await client.put(
        f"/api/v1/executions/{execution_id}",
        headers=viewer["headers"],
        json={"status": "completed"},
    )
    assert updated_by_viewer.status_code == 403, updated_by_viewer.text
