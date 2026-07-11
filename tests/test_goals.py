"""E2E tests: goal runs and step runs CRUD."""

from decimal import Decimal

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "goaluser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Goal Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.mark.asyncio
async def test_create_goal_run(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Deploy the new billing service",
            "goal_id": "goal-deploy-billing-001",
            "context": {"environment": "staging"},
            "steps": [{"name": "build"}, {"name": "test"}, {"name": "deploy"}],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["goal"] == "Deploy the new billing service"
    assert data["goal_id"] == "goal-deploy-billing-001"
    assert data["status"] == "pending"
    assert data["plan_version"] == 1
    assert data["retry_count"] == 0
    assert len(data["steps"]) == 3
    assert data["context"]["environment"] == "staging"
    assert data["user_id"]  # should be set to current user


@pytest.mark.asyncio
async def test_list_goal_runs(client: AsyncClient):
    headers = await _auth(client)
    await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Goal A",
            "goal_id": "goal-a-001",
        },
    )
    await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Goal B",
            "goal_id": "goal-b-002",
        },
    )

    resp = await client.get("/api/v1/goals", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2
    assert len(resp.json()["items"]) == 2


@pytest.mark.asyncio
async def test_list_goal_runs_by_status(client: AsyncClient):
    headers = await _auth(client)
    await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Pending goal",
            "goal_id": "goal-pending-001",
        },
    )
    r2 = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Running goal",
            "goal_id": "goal-running-002",
        },
    )
    # Update second goal to running
    await client.put(
        f"/api/v1/goals/{r2.json()['id']}",
        headers=headers,
        json={
            "status": "running",
        },
    )

    resp_pending = await client.get("/api/v1/goals?status=pending", headers=headers)
    assert resp_pending.json()["total"] == 1
    assert resp_pending.json()["items"][0]["goal"] == "Pending goal"

    resp_running = await client.get("/api/v1/goals?status=running", headers=headers)
    assert resp_running.json()["total"] == 1
    assert resp_running.json()["items"][0]["goal"] == "Running goal"


@pytest.mark.asyncio
async def test_update_goal_run(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Update test",
            "goal_id": "goal-update-001",
        },
    )
    goal_run_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/goals/{goal_run_id}",
        headers=headers,
        json={
            "status": "running",
            "current_step_id": "step-build-001",
            "current_agent_id": "agent-builder-001",
            "steps": [{"name": "build", "status": "running"}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["current_step_id"] == "step-build-001"
    assert data["current_agent_id"] == "agent-builder-001"
    assert data["steps"][0]["status"] == "running"
    assert data["updated_at"]  # should be refreshed


@pytest.mark.asyncio
async def test_cancel_goal_run(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Cancel test",
            "goal_id": "goal-cancel-001",
        },
    )
    goal_run_id = create.json()["id"]

    resp = await client.post(f"/api/v1/goals/{goal_run_id}/cancel", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Verify it is actually cancelled
    get_resp = await client.get(f"/api/v1/goals/{goal_run_id}", headers=headers)
    assert get_resp.json()["status"] == "cancelled"
    assert get_resp.json()["completed_at"]


@pytest.mark.asyncio
async def test_create_step_run(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Step test",
            "goal_id": "goal-step-001",
        },
    )
    goal_run_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/goals/{goal_run_id}/steps",
        headers=headers,
        json={
            "step_id": "step-build-001",
            "status": "completed",
            "step_name": "Build Docker image",
            "step_type": "tool_call",
            "inputs": {"dockerfile": "Dockerfile.prod"},
            "outputs": {"image_tag": "v1.2.3"},
            "duration_ms": 12500.5,
            "prompt_tokens": 1500,
            "completion_tokens": 300,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["step_id"] == "step-build-001"
    assert data["status"] == "completed"
    assert data["step_name"] == "Build Docker image"
    assert data["inputs"]["dockerfile"] == "Dockerfile.prod"
    assert data["outputs"]["image_tag"] == "v1.2.3"
    assert data["duration_ms"] == 12500.5
    assert data["prompt_tokens"] == 1500


@pytest.mark.asyncio
async def test_list_step_runs(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "goal": "Steps list test",
            "goal_id": "goal-steps-list-001",
        },
    )
    goal_run_id = create.json()["id"]

    # Create multiple steps
    await client.post(
        f"/api/v1/goals/{goal_run_id}/steps",
        headers=headers,
        json={
            "step_id": "step-1",
            "status": "completed",
            "step_name": "Plan",
        },
    )
    await client.post(
        f"/api/v1/goals/{goal_run_id}/steps",
        headers=headers,
        json={
            "step_id": "step-2",
            "status": "running",
            "step_name": "Execute",
        },
    )

    resp = await client.get(f"/api/v1/goals/{goal_run_id}/steps", headers=headers)
    assert resp.status_code == 200
    steps = resp.json()
    assert len(steps) == 2
    # Should be ordered by created_at (ascending)
    assert steps[0]["step_name"] == "Plan"
    assert steps[1]["step_name"] == "Execute"


@pytest.mark.asyncio
async def test_goal_isolation(client: AsyncClient):
    """User A cannot see User B's goal runs."""
    headers_a = await _auth(client, "goal_a")
    headers_b = await _auth(client, "goal_b")

    create = await client.post(
        "/api/v1/goals",
        headers=headers_a,
        json={
            "goal": "A's secret goal",
            "goal_id": "goal-secret-a-001",
        },
    )
    goal_run_id = create.json()["id"]

    # B cannot see it
    resp = await client.get(f"/api/v1/goals/{goal_run_id}", headers=headers_b)
    assert resp.status_code == 404

    # B's list is empty
    resp2 = await client.get("/api/v1/goals", headers=headers_b)
    assert resp2.json()["total"] == 0


@pytest.mark.asyncio
async def test_persistent_goals_include_task_link_progress(client: AsyncClient, db_session):
    from packages.core.models.goal import GoalTaskLink

    headers = await _auth(client, "goal_links")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Link Workspace"},
    )
    workspace_id = workspace.json()["id"]

    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "title": "Book 10 tours",
            "metric_key": "tour_count",
            "target_value": 10,
            "baseline_value": 0,
            "workspace_id": workspace_id,
        },
    )
    assert goal.status_code == 201
    goal_id = goal.json()["id"]

    task_a = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Prepare tour recommendations",
            "workspace_id": workspace_id,
        },
    )
    task_b = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "title": "Send tour follow-up",
            "workspace_id": workspace_id,
        },
    )
    task_a_id = task_a.json()["id"]
    task_b_id = task_b.json()["id"]
    await client.put(f"/api/v1/tasks/{task_a_id}", headers=headers, json={"status": "completed"})

    db_session.add_all(
        [
            GoalTaskLink(
                goal_id=goal_id,
                task_id=task_a_id,
                contribution="direct",
                estimated_impact=Decimal("2.0"),
                actual_impact=Decimal("1.0"),
            ),
            GoalTaskLink(
                goal_id=goal_id,
                task_id=task_b_id,
                contribution="direct",
                estimated_impact=Decimal("3.0"),
            ),
        ]
    )
    await db_session.commit()

    listed = await client.get(f"/api/v1/goals?workspace_id={workspace_id}", headers=headers)
    assert listed.status_code == 200
    goals = listed.json()
    assert isinstance(goals, list)
    row = next(g for g in goals if g["id"] == goal_id)
    assert row["linked_task_ids"] == [task_a_id, task_b_id]
    assert row["task_status_counts"] == {"completed": 1, "pending": 1}
    assert row["task_progress_fraction"] == pytest.approx(0.5)
    assert row["estimated_impact_total"] == pytest.approx(5.0)
    assert row["actual_impact_total"] == pytest.approx(1.0)

    detail = await client.get(f"/api/v1/goals/{goal_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["linked_task_ids"] == [task_a_id, task_b_id]


@pytest.mark.asyncio
async def test_workspace_goal_manual_cadence_does_not_install_schedule(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "goal_manual_cadence")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Manual Cadence Workspace"},
    )
    workspace_id = workspace.json()["id"]

    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "title": "Manual conversion check",
            "metric_key": "qa_conversion",
            "target_value": 10,
            "baseline_value": 0,
            "workspace_id": workspace_id,
            "measurement_source": {"provider": "manual"},
            "measurement_cadence": "manual",
        },
    )

    assert goal.status_code == 201
    body = goal.json()
    assert body["measurement_cadence"] == "manual"
    assert body["measurement_source"] == {
        "provider": "workspace_internal",
        "params": {"mode": "linked_task_impact"},
    }

    row = (await db_session.execute(select(Goal).where(Goal.id == body["id"]))).scalar_one()
    jobs = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{row.id}"))).scalars().all()
    assert jobs == []
