"""E2E tests: workspace soft-delete / restore / purge lifecycle."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


async def _register(
    client: AsyncClient,
    username: str = "lifecycle_user",
) -> tuple[str, dict]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Lifecycle Corp",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_delete_is_soft_default(client: AsyncClient):
    """DELETE /workspaces/{id} should soft-delete (not hard-delete) — the
    workspace must still be findable in /workspaces/trash/list."""
    _, headers = await _register(client, "lifecycle_soft")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Doomed"},
    )
    ws_id = create.json()["id"]

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    # Default list should not see it
    listed = await client.get("/api/v1/workspaces", headers=headers)
    assert listed.status_code == 200
    assert all(w["id"] != ws_id for w in listed.json())

    # Trash should
    trash = await client.get("/api/v1/workspaces/trash/list", headers=headers)
    assert trash.status_code == 200
    trashed_ids = [w["id"] for w in trash.json()]
    assert ws_id in trashed_ids
    assert trash.json()[0]["deleted_at"] is not None


@pytest.mark.asyncio
async def test_deleted_workspace_chat_is_not_accessible(client: AsyncClient):
    _, headers = await _register(client, "lifecycle_chat_deleted")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Muted"},
    )
    ws_id = create.json()["id"]

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    listed = await client.get(f"/api/v1/workspaces/{ws_id}/chat/messages", headers=headers)
    posted = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages",
        headers=headers,
        json={"body": "should not revive deleted workspace"},
    )

    assert listed.status_code == 404
    assert posted.status_code == 404


@pytest.mark.asyncio
async def test_restore_brings_workspace_back(client: AsyncClient):
    _, headers = await _register(client, "lifecycle_restore")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Saved"},
    )
    ws_id = create.json()["id"]

    await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)

    restore = await client.post(
        f"/api/v1/workspaces/{ws_id}/restore",
        headers=headers,
    )
    assert restore.status_code == 200
    assert restore.json()["id"] == ws_id
    assert restore.json()["deleted_at"] is None

    # Now visible in default list, gone from trash
    listed = await client.get("/api/v1/workspaces", headers=headers)
    assert any(w["id"] == ws_id for w in listed.json())
    trash = await client.get("/api/v1/workspaces/trash/list", headers=headers)
    assert all(w["id"] != ws_id for w in trash.json())


@pytest.mark.asyncio
async def test_delete_restore_syncs_runtime_schedules(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob

    _, headers = await _register(client, "lifecycle_runtime")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Runtime Restored",
            "heartbeat_enabled": True,
            "heartbeat_cadence": "daily",
        },
    )
    ws_id = create.json()["id"]

    before_delete = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    after_delete = (
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id))).scalars().all()
    )
    restore = await client.post(f"/api/v1/workspaces/{ws_id}/restore", headers=headers)
    after_restore = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == ws_id)))
        .scalars()
        .all()
    )

    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(before_delete)
    assert delete.status_code == 204
    assert after_delete == []
    assert restore.status_code == 200
    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(after_restore)


@pytest.mark.asyncio
async def test_pause_pauses_workspace_automations(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding

    _, headers = await _register(client, "lifecycle_pause_automations")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Pause Automations"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]

    job_pk = generate_ulid()
    unrelated_job_pk = generate_ulid()
    automation_binding_id = generate_ulid()
    manual_binding_id = generate_ulid()
    job = ScheduledJob(
        id=job_pk,
        job_id=f"custom:{ws_id}",
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Workspace automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    unrelated_job = ScheduledJob(
        id=unrelated_job_pk,
        job_id=f"global:{ws_id}",
        entity_id=entity_id,
        workspace_id=None,
        name="Entity automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    automation_binding = WorkflowBinding(
        id=automation_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Workspace event automation",
        trigger_type="workspace_event",
        enabled=True,
        status="active",
    )
    manual_binding = WorkflowBinding(
        id=manual_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Attached flow",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    db_session.add_all([job, unrelated_job, automation_binding, manual_binding])
    await db_session.commit()

    before_ids = set((await db_session.execute(
        select(ScheduledJob.id).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    pause = await client.post(f"/api/v1/workspaces/{ws_id}/pause", headers=headers)
    assert pause.status_code == 200

    db_session.expire_all()
    workspace_jobs = list((await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    refreshed_automation_binding = await db_session.get(WorkflowBinding, automation_binding_id)
    refreshed_manual_binding = await db_session.get(WorkflowBinding, manual_binding_id)
    refreshed_unrelated_job = await db_session.get(ScheduledJob, unrelated_job_pk)

    assert {row.id for row in workspace_jobs} == before_ids
    assert workspace_jobs and all(row.enabled is False for row in workspace_jobs)
    assert refreshed_automation_binding is not None
    assert refreshed_automation_binding.enabled is False
    assert refreshed_automation_binding.status == "paused"
    assert refreshed_manual_binding is not None
    assert refreshed_manual_binding.enabled is True
    assert refreshed_manual_binding.status == "active"
    assert refreshed_unrelated_job is not None
    assert refreshed_unrelated_job.enabled is True


@pytest.mark.asyncio
async def test_delete_removes_workspace_automations(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding

    _, headers = await _register(client, "lifecycle_delete_automations")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Delete Automations"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]

    job_pk = generate_ulid()
    unrelated_job_pk = generate_ulid()
    automation_binding_id = generate_ulid()
    job = ScheduledJob(
        id=job_pk,
        job_id=f"delete-custom:{ws_id}",
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Delete with workspace",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    unrelated_job = ScheduledJob(
        id=unrelated_job_pk,
        job_id=f"keep-global:{ws_id}",
        entity_id=entity_id,
        workspace_id=None,
        name="Keep entity automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    automation_binding = WorkflowBinding(
        id=automation_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Delete event automation",
        trigger_type="workspace_event",
        enabled=True,
        status="active",
    )
    db_session.add_all([job, unrelated_job, automation_binding])
    await db_session.commit()

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    db_session.expire_all()
    workspace_jobs = list((await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    deleted_binding = await db_session.get(WorkflowBinding, automation_binding_id)
    kept_job = await db_session.get(ScheduledJob, unrelated_job_pk)

    assert workspace_jobs == []
    assert deleted_binding is None
    assert kept_job is not None
    assert kept_job.enabled is True


@pytest.mark.asyncio
async def test_restore_404_when_not_in_trash(client: AsyncClient):
    """POST /restore on a workspace that isn't in the trash returns 404."""
    _, headers = await _register(client, "lifecycle_404")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Fresh"},
    )
    ws_id = create.json()["id"]

    restore = await client.post(
        f"/api/v1/workspaces/{ws_id}/restore",
        headers=headers,
    )
    assert restore.status_code == 404


@pytest.mark.asyncio
async def test_grace_days_endpoint(client: AsyncClient):
    """The UI hits this to render the "X days left" copy."""
    _, headers = await _register(client, "lifecycle_grace")
    resp = await client.get(
        "/api/v1/workspaces/trash/grace-days",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["grace_days"], int)
    assert body["grace_days"] > 0


@pytest.mark.asyncio
async def test_purge_only_after_grace_window(
    client: AsyncClient,
    db_session,
):
    """``list_workspaces_due_for_purge`` should respect the cutoff —
    workspaces deleted recently are NOT yet eligible, ones deleted
    long ago ARE."""
    from packages.core.services.entity_service import (
        list_workspaces_due_for_purge,
        soft_delete_workspace,
    )

    _, headers = await _register(client, "lifecycle_purge")
    create_recent = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Recent"},
    )
    create_old = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Old"},
    )
    recent_id = create_recent.json()["id"]
    old_id = create_old.json()["id"]
    entity_id = create_recent.json()["entity_id"]

    await soft_delete_workspace(db_session, recent_id, entity_id)
    await soft_delete_workspace(db_session, old_id, entity_id)

    # Backdate the "old" workspace's deleted_at past the grace window.
    from sqlalchemy import update as sa_update
    from packages.core.models.workspace import Workspace

    await db_session.execute(
        sa_update(Workspace)
        .where(Workspace.id == old_id)
        .values(deleted_at=datetime.now(timezone.utc) - timedelta(days=45))
    )
    await db_session.flush()

    due = await list_workspaces_due_for_purge(db_session, grace_days=30)
    due_ids = {ws.id for ws in due}
    assert old_id in due_ids, "30+ day old soft-delete should be purgeable"
    assert recent_id not in due_ids, "Just-deleted workspace should NOT be purgeable yet"


@pytest.mark.asyncio
async def test_purge_workspace_cascades(client: AsyncClient, db_session):
    """``purge_workspace`` (the hard delete) should remove the row and
    all workspace-scoped tasks/conversations/etc."""
    from packages.core.services.entity_service import (
        purge_workspace,
        soft_delete_workspace,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workflow import WorkflowBinding
    from packages.core.models.workspace import Workspace
    from sqlalchemy import select

    _, headers = await _register(client, "lifecycle_cascade")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Cascade"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]

    # Create a Task scoped to this workspace
    task = Task(
        entity_id=entity_id,
        workspace_id=ws_id,
        title="Pre-purge task",
        status="pending",
    )
    binding_id = generate_ulid()
    binding = WorkflowBinding(
        id=binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Pre-purge flow attachment",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    db_session.add_all([task, binding])
    await db_session.commit()

    await soft_delete_workspace(db_session, ws_id, entity_id)
    purged = await purge_workspace(db_session, ws_id)
    assert purged is True

    # Workspace row gone
    ws_check = await db_session.execute(select(Workspace).where(Workspace.id == ws_id))
    assert ws_check.scalar_one_or_none() is None

    # Task gone
    task_check = await db_session.execute(select(Task).where(Task.workspace_id == ws_id))
    assert task_check.scalar_one_or_none() is None

    # Workspace workflow deployments must not dangle after the hard purge.
    assert await db_session.get(WorkflowBinding, binding_id) is None
