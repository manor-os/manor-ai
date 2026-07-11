"""E2E tests: scheduled jobs, job runs, agent executions."""

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
