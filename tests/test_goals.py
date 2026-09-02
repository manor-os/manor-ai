"""E2E tests: goal runs and step runs CRUD."""

import asyncio
from decimal import Decimal

import pytest
from httpx import AsyncClient

from packages.core.constants.goals import GoalStatus
from packages.core.goals.commands import (
    GoalUpdateCommand,
    GoalUpdateValidationError,
    validate_goal_status_transition,
)


def test_goal_update_factory_and_enum_policy_own_status_changes() -> None:
    command = GoalUpdateCommand.from_fields({"status": GoalStatus.PAUSED})
    assert command.values == {"status": GoalStatus.PAUSED.value}
    validate_goal_status_transition(
        GoalStatus.ACTIVE.value,
        command.values["status"],
    )
    with pytest.raises(GoalUpdateValidationError):
        validate_goal_status_transition(
            GoalStatus.ABANDONED.value,
            GoalStatus.ACTIVE.value,
        )


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
async def test_persistent_goal_rejects_invalid_status(client: AsyncClient):
    headers = await _auth(client, "goal_invalid_status")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal status validation"},
    )
    created = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace.json()["id"],
            "title": "Keep a valid lifecycle",
            "target_value": 1,
        },
    )

    response = await client.put(
        f"/api/v1/goals/{created.json()['id']}",
        headers=headers,
        json={"status": "completed"},
    )

    assert response.status_code == 422, response.text
    assert "invalid Goal status" in response.json()["detail"]
    detail = await client.get(
        f"/api/v1/goals/{created.json()['id']}",
        headers=headers,
    )
    assert detail.json()["status"] == "active"

    achieved = await client.put(
        f"/api/v1/goals/{created.json()['id']}",
        headers=headers,
        json={"status": "achieved"},
    )
    assert achieved.status_code == 422, achieved.text
    assert "measurement-derived" in achieved.json()["detail"]
    detail = await client.get(
        f"/api/v1/goals/{created.json()['id']}",
        headers=headers,
    )
    assert detail.json()["status"] == "active"
    assert detail.json()["achieved_at"] is None


@pytest.mark.asyncio
async def test_goal_service_rejects_protected_field_updates(db_session):
    from packages.core.goals.commands import GoalUpdateValidationError
    from packages.core.goals.service import create_goal, update_goal
    from packages.core.models.base import generate_ulid

    entity_id = generate_ulid()
    goal = await create_goal(
        db_session,
        entity_id=entity_id,
        title="Keep Goal ownership stable",
        metric_key="ownership_count",
        target_value=1,
    )
    original_workspace_id = goal.workspace_id

    with pytest.raises(GoalUpdateValidationError, match="workspace_id"):
        await update_goal(
            db_session,
            goal.id,
            entity_id,
            workspace_id=generate_ulid(),
        )

    await db_session.refresh(goal)
    assert goal.workspace_id == original_workspace_id
    assert goal.entity_id == entity_id
    assert goal.revision == 1


@pytest.mark.asyncio
async def test_delete_persistent_goal_preserves_evidence_and_removes_schedule(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.goals.service import record_measurement
    from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "goal_delete_cleanup")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Delete Cleanup Workspace", "heartbeat_enabled": True},
    )
    workspace_id = workspace.json()["id"]
    created_goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "title": "Archive Goal without losing history",
            "metric_key": "cleanup_count",
            "target_value": 10,
            "workspace_id": workspace_id,
        },
    )
    assert created_goal.status_code == 201, created_goal.text
    goal_id = created_goal.json()["id"]
    created_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"title": "Linked cleanup task", "workspace_id": workspace_id},
    )
    assert created_task.status_code == 201, created_task.text

    goal = (await db_session.execute(select(Goal).where(Goal.id == goal_id))).scalar_one()
    await record_measurement(db_session, goal, value=2, source="manual")
    db_session.add(GoalTaskLink(
        goal_id=goal_id,
        task_id=created_task.json()["id"],
        contribution="direct",
    ))
    await db_session.commit()

    assert (await db_session.execute(
        select(GoalMeasurement).where(GoalMeasurement.goal_id == goal_id)
    )).scalar_one_or_none() is not None
    assert (await db_session.execute(
        select(GoalTaskLink).where(GoalTaskLink.goal_id == goal_id)
    )).scalar_one_or_none() is not None
    assert (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
    )).scalar_one_or_none() is not None

    deleted = await client.delete(f"/api/v1/goals/{goal_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    db_session.expire_all()
    archived_goal = (await db_session.execute(
        select(Goal).where(Goal.id == goal_id)
    )).scalar_one()
    assert archived_goal.status == "abandoned"
    assert (await db_session.execute(
        select(GoalMeasurement).where(GoalMeasurement.goal_id == goal_id)
    )).scalar_one_or_none() is not None
    assert (await db_session.execute(
        select(GoalTaskLink).where(GoalTaskLink.goal_id == goal_id)
    )).scalar_one_or_none() is not None
    assert (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
    )).scalar_one_or_none() is None

    rejected_measurement = await client.post(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=headers,
        json={"value": 3},
    )
    assert rejected_measurement.status_code == 409, rejected_measurement.text

    rejected_restore = await client.put(
        f"/api/v1/goals/{goal_id}",
        headers=headers,
        json={"status": "active"},
    )
    assert rejected_restore.status_code == 422, rejected_restore.text
    await db_session.refresh(archived_goal)
    assert archived_goal.status == "abandoned"
    assert (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_goal_serializes_with_in_flight_measurement(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.goals.service import create_goal, delete_goal, record_measurement
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal, GoalMeasurement

    entity_id = generate_ulid()
    goal = await create_goal(
        db_session,
        entity_id=entity_id,
        title="Concurrent measurement cleanup",
        metric_key="concurrent_count",
        target_value=10,
    )
    goal_id = goal.id
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as writer, session_factory() as deleter:
        locked_goal = (await writer.execute(
            select(Goal).where(Goal.id == goal_id).with_for_update()
        )).scalar_one()
        await record_measurement(writer, locked_goal, value=4, source="manual")

        async def delete_and_commit() -> bool:
            deleted = await delete_goal(deleter, goal_id, entity_id)
            await deleter.commit()
            return deleted

        delete_task = asyncio.create_task(delete_and_commit())
        await asyncio.sleep(0.05)
        assert not delete_task.done()

        await writer.commit()
        assert await asyncio.wait_for(delete_task, timeout=2) is True

    async with session_factory() as verifier:
        stored_goal = (await verifier.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one()
        assert stored_goal.status == "abandoned"
        assert (await verifier.execute(
            select(GoalMeasurement).where(GoalMeasurement.goal_id == goal_id)
        )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_goal_serializes_with_in_flight_task_link(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.goals.service import create_goal, delete_goal, link_task_to_goal
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal, GoalTaskLink

    entity_id = generate_ulid()
    goal = await create_goal(
        db_session,
        entity_id=entity_id,
        title="Concurrent task-link cleanup",
        metric_key="linked_count",
        target_value=10,
    )
    goal_id = goal.id
    task_id = generate_ulid()
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as writer, session_factory() as deleter:
        await link_task_to_goal(writer, goal_id=goal_id, task_id=task_id)

        async def delete_and_commit() -> bool:
            deleted = await delete_goal(deleter, goal_id, entity_id)
            await deleter.commit()
            return deleted

        delete_task = asyncio.create_task(delete_and_commit())
        await asyncio.sleep(0.05)
        assert not delete_task.done()

        await writer.commit()
        assert await asyncio.wait_for(delete_task, timeout=2) is True

    async with session_factory() as verifier:
        stored_goal = (await verifier.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one()
        assert stored_goal.status == "abandoned"
        assert (await verifier.execute(
            select(GoalTaskLink).where(GoalTaskLink.goal_id == goal_id)
        )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_goal_serializes_with_in_flight_schedule_install(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.goals.scheduling import install_measurement_schedule
    from packages.core.goals.service import create_goal, delete_goal
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob

    entity_id = generate_ulid()
    goal = await create_goal(
        db_session,
        entity_id=entity_id,
        title="Concurrent schedule cleanup",
        metric_key="scheduled_count",
        target_value=10,
        measurement_source={"provider": "workspace_internal"},
        measurement_cadence="daily",
        install_schedule=False,
    )
    goal_id = goal.id
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as writer, session_factory() as deleter:
        writer_goal = (await writer.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one()
        await install_measurement_schedule(writer, writer_goal)

        async def delete_and_commit() -> bool:
            deleted = await delete_goal(deleter, goal_id, entity_id)
            await deleter.commit()
            return deleted

        delete_task = asyncio.create_task(delete_and_commit())
        await asyncio.sleep(0.05)
        assert not delete_task.done()

        await writer.commit()
        assert await asyncio.wait_for(delete_task, timeout=2) is True

    async with session_factory() as verifier:
        stored_goal = (await verifier.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one()
        assert stored_goal.status == "abandoned"
        assert (await verifier.execute(
            select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
        )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_soft_deleted_workspace_blocks_in_flight_goal_schedule_install(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.goals.scheduling import install_measurement_schedule
    from packages.core.goals.service import create_goal
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_service import soft_delete_workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Delete versus Goal schedule",
        status="active",
        heartbeat_enabled=True,
        settings={},
        operating_model={},
    )
    db_session.add(workspace)
    await db_session.flush()
    goal = await create_goal(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Never resurrect this schedule",
        metric_key="safe_schedule_count",
        target_value=10,
    )
    workspace_id = workspace.id
    goal_id = goal.id
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as deleter, session_factory() as writer:
        writer_goal = (await writer.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one()
        assert await soft_delete_workspace(deleter, workspace_id, entity_id) is True

        async def install_and_commit() -> None:
            try:
                await install_measurement_schedule(writer, writer_goal)
                await writer.commit()
            except Exception:
                await writer.rollback()
                raise

        install_task = asyncio.create_task(install_and_commit())
        await asyncio.sleep(0.05)
        assert not install_task.done()

        await deleter.commit()
        with pytest.raises(ValueError, match="no longer exists"):
            await asyncio.wait_for(install_task, timeout=2)

    async with session_factory() as verifier:
        assert (await verifier.execute(
            select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{goal_id}")
        )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_agent_runtime_goal_create_respects_disabled_workspace_runtime(db_session):
    import json

    from sqlalchemy import select

    from packages.core.ai.runtime.goal_actions import runtime_create_goal_action
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Runtime-disabled Goal creation",
        status="active",
        heartbeat_enabled=False,
        settings={},
        operating_model={},
    )
    db_session.add(workspace)
    await db_session.commit()

    result = json.loads(await runtime_create_goal_action(
        entity_id=entity_id,
        params={
            "workspace_id": workspace.id,
            "title": "Create without starting autonomy",
            "metric_key": "created_count",
            "target_value": 1,
        },
    ))

    assert "error" not in result
    db_session.expire_all()
    assert (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == f"gm:{result['goal_id']}")
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_goal_mutations_sync_workspace_use_goals_from_active_set(client: AsyncClient):
    headers = await _auth(client, "goal_runtime_mode_sync")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Runtime Mode Sync", "heartbeat_enabled": True},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    async def workspace_uses_goals() -> bool | None:
        response = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=headers)
        assert response.status_code == 200, response.text
        return (response.json().get("operating_model") or {}).get("strategist", {}).get("use_goals")

    first = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "First active Goal",
            "target_value": 1,
        },
    )
    assert first.status_code == 201, first.text
    assert await workspace_uses_goals() is True

    second = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Second active Goal",
            "target_value": 1,
        },
    )
    assert second.status_code == 201, second.text

    paused_first = await client.put(
        f"/api/v1/goals/{first.json()['id']}",
        headers=headers,
        json={"status": "paused"},
    )
    assert paused_first.status_code == 200, paused_first.text
    assert await workspace_uses_goals() is True

    paused_second = await client.put(
        f"/api/v1/goals/{second.json()['id']}",
        headers=headers,
        json={"status": "paused"},
    )
    assert paused_second.status_code == 200, paused_second.text
    assert await workspace_uses_goals() is False

    reactivated_first = await client.put(
        f"/api/v1/goals/{first.json()['id']}",
        headers=headers,
        json={"status": "active"},
    )
    assert reactivated_first.status_code == 200, reactivated_first.text
    assert await workspace_uses_goals() is True

    deleted_first = await client.delete(
        f"/api/v1/goals/{first.json()['id']}",
        headers=headers,
    )
    assert deleted_first.status_code == 204, deleted_first.text
    assert await workspace_uses_goals() is False


@pytest.mark.asyncio
async def test_workspace_goal_crud_syncs_portable_contract_and_survives_repair(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.goals import service as goal_service
    from packages.core.models.goal import Goal
    from packages.core.models.workspace import Workspace

    headers = await _auth(client, "goal_contract_sync")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Contract Sync"},
    )
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Reach qualified users",
            "target_value": 25,
        },
    )
    assert created.status_code == 201, created.text
    goal_id = created.json()["id"]
    goal_key = created.json()["goal_key"]
    metric_key = created.json()["metric_key"]
    assert metric_key.startswith("goal_reach_qualified_users")

    workspace_detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    contract_goals = workspace_detail.json()["operating_model"]["goals"]
    assert [(row["metric_key"], row["title"]) for row in contract_goals] == [
        (metric_key, "Reach qualified users"),
    ]
    db_session.expire_all()
    contract_revision = (await db_session.execute(
        select(Workspace.operation_revision).where(Workspace.id == workspace_id)
    )).scalar_one()

    updated = await client.put(
        f"/api/v1/goals/{goal_id}",
        headers=headers,
        json={"title": "Reach activated users"},
    )
    assert updated.status_code == 200, updated.text
    workspace_detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    updated_contract_goal = workspace_detail.json()["operating_model"]["goals"][0]
    assert updated_contract_goal["title"] == "Reach activated users"
    assert updated_contract_goal["metric_key"] == metric_key
    assert updated_contract_goal["goal_key"] == goal_key

    changed_identity = await goal_service.update_goal(
        db_session,
        goal_id,
        created.json()["entity_id"],
        metric_key="activated_users",
    )
    assert changed_identity is not None
    await db_session.commit()
    workspace_detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    updated_contract_goal = workspace_detail.json()["operating_model"]["goals"][0]
    assert updated_contract_goal["metric_key"] == "activated_users"
    assert updated_contract_goal["goal_key"] == goal_key
    db_session.expire_all()
    updated_contract_revision = (await db_session.execute(
        select(Workspace.operation_revision).where(Workspace.id == workspace_id)
    )).scalar_one()
    assert updated_contract_revision > contract_revision

    deleted = await client.delete(f"/api/v1/goals/{goal_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    workspace_detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    assert workspace_detail.json()["operating_model"]["goals"] == []

    repaired = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/repair",
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text
    db_session.expire_all()
    stored_goal = (await db_session.execute(
        select(Goal).where(Goal.id == goal_id)
    )).scalar_one()
    assert stored_goal.status == "abandoned"
    assert (await db_session.execute(
        select(Goal).where(
            Goal.workspace_id == workspace_id,
            Goal.status == "active",
        )
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_workspace_goal_api_preserves_exact_decimal_values_through_repair(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.goal import Goal

    headers = await _auth(client, "goal_exact_decimals")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Exact Goal Values"},
    )
    workspace_id = workspace.json()["id"]

    created = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Preserve exact values",
            "target_value": "9007199254740992.0001",
            "baseline_value": "1234567890123456.7890",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["target_value"] == "9007199254740992.0001"
    assert created.json()["baseline_value"] == "1234567890123456.7890"

    goal_id = created.json()["id"]

    detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    contract_goal = detail.json()["operating_model"]["goals"][0]
    assert contract_goal["target_value"] == "9007199254740992.0001"
    assert contract_goal["baseline_value"] == "1234567890123456.7890"

    repaired = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/repair",
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text

    fetched = await client.get(f"/api/v1/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["target_value"] == "9007199254740992.0001"
    assert fetched.json()["baseline_value"] == "1234567890123456.7890"
    assert fetched.json()["current_value"] is None

    updated = await client.put(
        f"/api/v1/goals/{goal_id}",
        headers=headers,
        json={"target_value": "9007199254740991.9999"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["target_value"] == "9007199254740991.9999"

    measured = await client.post(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=headers,
        json={"value": "2234567890123456.7891", "source": "manual"},
    )
    assert measured.status_code == 201, measured.text
    assert measured.json()["value"] == "2234567890123456.7891"

    measurements = await client.get(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=headers,
    )
    assert measurements.status_code == 200, measurements.text
    assert measurements.json()[0]["value"] == "2234567890123456.7891"

    fetched = await client.get(f"/api/v1/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["current_value"] == "2234567890123456.7891"

    db_session.expire_all()
    stored = (
        await db_session.execute(select(Goal).where(Goal.id == goal_id))
    ).scalar_one()
    assert stored.target_value == Decimal("9007199254740991.9999")
    assert stored.baseline_value == Decimal("1234567890123456.7890")
    assert stored.current_value == Decimal("2234567890123456.7891")


@pytest.mark.asyncio
async def test_goal_api_rejects_values_outside_numeric_contract(client: AsyncClient):
    headers = await _auth(client, "goal_numeric_contract")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Numeric Contract"},
    )
    workspace_id = workspace.json()["id"]

    for payload in (
        {"title": "Too precise", "target_value": "0.00001"},
        {"title": "Too large", "target_value": "10000000000000000"},
        {
            "title": "Invalid cadence",
            "target_value": 1,
            "measurement_cadence": "fortnightly",
        },
        {
            "title": "Invalid cron fields",
            "target_value": 1,
            "measurement_cadence": "x x x x x",
        },
        {
            "title": "Unsupported seconds cron",
            "target_value": 1,
            "measurement_cadence": "0 0 0 * * *",
        },
        {
            "title": "Cadence exceeds storage",
            "target_value": 1,
            "measurement_cadence": "manual_" + "x" * 64,
        },
        {
            "title": "Bad baseline",
            "target_value": 1,
            "baseline_value": "0.00001",
        },
    ):
        response = await client.post(
            "/api/v1/goals",
            headers=headers,
            json={"workspace_id": workspace_id, **payload},
        )
        assert response.status_code == 422, response.text

    created = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Valid numeric Goal",
            "target_value": 1,
        },
    )
    goal_id = created.json()["id"]
    updated = await client.put(
        f"/api/v1/goals/{goal_id}",
        headers=headers,
        json={"target_value": "10000000000000000"},
    )
    measured = await client.post(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=headers,
        json={"value": "0.00001", "source": "manual"},
    )
    invalid_cadence = await client.put(
        f"/api/v1/goals/{goal_id}",
        headers=headers,
        json={"measurement_cadence": "fortnightly"},
    )

    assert updated.status_code == 422, updated.text
    assert measured.status_code == 422, measured.text
    assert invalid_cadence.status_code == 422, invalid_cadence.text


@pytest.mark.asyncio
async def test_goal_created_from_stat_projects_six_decimal_baseline(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workspace_stat import WorkspaceStat

    headers = await _auth(client, "goal_stat_baseline_precision")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Stat Baseline Precision"},
    )
    workspace = workspace_response.json()
    stat = WorkspaceStat(
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
        key="sales.conversion_rate",
        name="Conversion rate",
        collector_type="manual",
        collector_config={},
        current_value=Decimal("0.123456"),
        status="active",
        goal_eligible=True,
    )
    db_session.add(stat)
    await db_session.commit()

    created = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace["id"],
            "stat_id": stat.id,
            "title": "Improve conversion",
            "target_value": 1,
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["baseline_value"] == 0.1235
    assert created.json()["metric_key"] == stat.key


@pytest.mark.asyncio
async def test_goal_detail_and_measurements_enforce_workspace_access(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from tests.test_document_permissions import _create_entity_user

    owner_headers = await _auth(client, "goal_acl_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    viewer = await _create_entity_user(owner["entity_id"], "goal_acl_viewer", role="member")
    outsider = await _create_entity_user(owner["entity_id"], "goal_acl_outsider", role="member")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private Goal Measurements"},
    )
    workspace_id = workspace_response.json()["id"]
    created = await client.post(
        "/api/v1/goals",
        headers=owner_headers,
        json={
            "workspace_id": workspace_id,
            "title": "Private Goal",
            "target_value": 10,
        },
    )
    goal_id = created.json()["id"]
    assert (await client.post(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=owner_headers,
        json={"value": 1, "source": "manual"},
    )).status_code == 201

    workspace = await db_session.get(Workspace, workspace_id)
    assert workspace is not None
    workspace.settings = {**(workspace.settings or {}), "access_mode": "members_only"}
    db_session.add(WorkspaceStaff(
        workspace_id=workspace_id,
        staff_id=None,
        user_id=viewer["id"],
        role="viewer",
        status="active",
    ))
    await db_session.commit()

    assert (await client.get(
        f"/api/v1/goals/{goal_id}",
        headers=viewer["headers"],
    )).status_code == 200
    assert (await client.get(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=viewer["headers"],
    )).status_code == 200
    assert (await client.post(
        f"/api/v1/goals/{goal_id}/measurements",
        headers=viewer["headers"],
        json={"value": 2, "source": "manual"},
    )).status_code == 403

    for method, path, json_body, expected_status in (
        ("get", f"/api/v1/goals/{goal_id}", None, 404),
        ("get", f"/api/v1/goals/{goal_id}/measurements", None, 404),
        ("post", f"/api/v1/goals/{goal_id}/measurements", {"value": 2}, 403),
    ):
        response = await getattr(client, method)(
            path,
            headers=outsider["headers"],
            **({"json": json_body} if json_body is not None else {}),
        )
        assert response.status_code == expected_status, response.text


@pytest.mark.asyncio
async def test_workspace_goals_can_share_metric_without_sharing_identity(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.goal import Goal

    headers = await _auth(client, "goal_shared_metric")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Shared Goal Metric"},
    )
    workspace_id = workspace.json()["id"]

    first = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "goal_key": "trial_signups",
            "title": "Reach 100 trial signups",
            "metric_key": "signup_count",
            "target_value": 100,
        },
    )
    second = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "goal_key": "paid_signups",
            "title": "Reach 25 paid signups",
            "metric_key": "signup_count",
            "target_value": 25,
        },
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    contract_by_key = {
        row["goal_key"]: row
        for row in detail.json()["operating_model"]["goals"]
    }
    assert set(contract_by_key) == {"trial_signups", "paid_signups"}
    assert {
        row["metric_key"] for row in contract_by_key.values()
    } == {"signup_count"}

    renamed = await client.put(
        f"/api/v1/goals/{first.json()['id']}",
        headers=headers,
        json={"title": "Reach 120 trial signups"},
    )
    assert renamed.status_code == 200, renamed.text
    detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    contract_by_key = {
        row["goal_key"]: row
        for row in detail.json()["operating_model"]["goals"]
    }
    assert contract_by_key["trial_signups"]["title"] == "Reach 120 trial signups"
    assert contract_by_key["paid_signups"]["title"] == "Reach 25 paid signups"

    deleted = await client.delete(
        f"/api/v1/goals/{first.json()['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    detail = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    assert [
        row["goal_key"] for row in detail.json()["operating_model"]["goals"]
    ] == ["paid_signups"]

    repaired = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/repair",
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text
    db_session.expire_all()
    stored = list((await db_session.execute(
        select(Goal).where(Goal.workspace_id == workspace_id)
    )).scalars().all())
    by_key = {goal.goal_key: goal for goal in stored}
    assert by_key["trial_signups"].status == "abandoned"
    assert by_key["paid_signups"].status == "active"


@pytest.mark.asyncio
async def test_create_goal_returns_conflict_for_duplicate_goal_key(
    client: AsyncClient,
):
    headers = await _auth(client, "goal_identity_conflict")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Identity Conflict"},
    )
    workspace_id = workspace.json()["id"]
    payload = {
        "workspace_id": workspace_id,
        "goal_key": "signup_target",
        "title": "Reach signup target",
        "metric_key": "signup_count",
        "target_value": 100,
    }

    first = await client.post("/api/v1/goals", headers=headers, json=payload)
    duplicate = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={**payload, "title": "Reach another signup target"},
    )

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 409, duplicate.text
    assert "already exists" in duplicate.json()["detail"]


@pytest.mark.asyncio
async def test_concurrent_entity_goal_key_conflict_is_a_domain_error(db_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.goals.factory import GoalKeyConflictError
    from packages.core.goals.service import create_goal
    from packages.core.models.base import generate_ulid

    entity_id = generate_ulid()
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def create_and_commit() -> str:
        async with session_factory() as session:
            try:
                await create_goal(
                    session,
                    entity_id=entity_id,
                    title="Concurrent Goal identity",
                    goal_key="shared_identity",
                    metric_key="shared_metric",
                    target_value=1,
                )
                await session.commit()
                return "created"
            except GoalKeyConflictError:
                await session.rollback()
                return "conflict"

    assert sorted(await asyncio.gather(
        create_and_commit(),
        create_and_commit(),
    )) == ["conflict", "created"]


@pytest.mark.asyncio
async def test_goal_mode_change_invalidates_an_open_operation_draft(client: AsyncClient):
    headers = await _auth(client, "goal_mode_revision")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Mode Revision"},
    )
    workspace_id = workspace.json()["id"]
    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Keep the runtime goal-aware",
            "target_value": 1,
        },
    )
    assert goal.status_code == 201, goal.text

    draft = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/drafts",
        headers=headers,
        json={
            "patches": [{
                "op": "heartbeat_policy.update",
                "payload": {"enabled": True, "cadence": "daily"},
            }],
        },
    )
    assert draft.status_code == 200, draft.text

    paused = await client.put(
        f"/api/v1/goals/{goal.json()['id']}",
        headers=headers,
        json={"status": "paused"},
    )
    assert paused.status_code == 200, paused.text
    stale_apply = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/drafts/{draft.json()['id']}/apply",
        headers=headers,
        json={"user_confirmation": True},
    )
    assert stale_apply.status_code == 409, stale_apply.text


@pytest.mark.asyncio
async def test_goal_achievement_syncs_workspace_use_goals(client: AsyncClient):
    headers = await _auth(client, "goal_achievement_mode_sync")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Goal Achievement Mode Sync", "heartbeat_enabled": True},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    goal = await client.post(
        "/api/v1/goals",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "title": "Reach the final active Goal",
            "target_value": 1,
            "baseline_value": 0,
        },
    )
    assert goal.status_code == 201, goal.text

    measured = await client.post(
        f"/api/v1/goals/{goal.json()['id']}/measurements",
        headers=headers,
        json={"value": 1},
    )
    assert measured.status_code == 201, measured.text
    assert measured.json()["pace_status"] == "achieved"

    rejected_late_measurement = await client.post(
        f"/api/v1/goals/{goal.json()['id']}/measurements",
        headers=headers,
        json={"value": 0},
    )
    assert rejected_late_measurement.status_code == 409, rejected_late_measurement.text

    refreshed_workspace = await client.get(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    assert refreshed_workspace.status_code == 200, refreshed_workspace.text
    assert (
        refreshed_workspace.json()["operating_model"]["strategist"]["use_goals"]
        is False
    )


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
