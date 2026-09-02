"""E2E tests: scheduled jobs, job runs, agent executions."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.base import generate_ulid


def _concurrent_session_factory(*, wait_on_execute: int):
    """Return sessions that align one chosen SQL statement across two calls."""
    barrier = asyncio.Event()
    barrier_waiters = 0

    class BarrierSession:
        def __init__(self, session):
            self._session = session
            self._execute_count = 0

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def execute(self, statement, *args, **kwargs):
            nonlocal barrier_waiters
            self._execute_count += 1
            if self._execute_count == wait_on_execute:
                barrier_waiters += 1
                if barrier_waiters == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=5)
            return await self._session.execute(statement, *args, **kwargs)

    @asynccontextmanager
    async def session_context():
        async with db_module.async_session() as session:
            yield BarrierSession(session)

    return session_context


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


async def _create_workspace(
    client: AsyncClient,
    headers: dict,
    name: str,
) -> str:
    response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


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
async def test_create_scheduled_job_rejects_missing_local_agent(
    client: AsyncClient,
):
    headers = await _auth(client, "sched_missing_local_agent")
    resp = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "missing-local-agent",
            "name": "Missing local Agent",
            "agent_id": generate_ulid(),
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Agent is no longer available"


@pytest.mark.asyncio
async def test_create_scheduled_job_rejects_opaque_workspace(
    client: AsyncClient,
):
    headers = await _auth(client, "sched_opaque_workspace")
    response = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "opaque-workspace-job",
            "name": "Opaque workspace job",
            "workspace_id": "legacy-opaque-workspace",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Workspace not found"


@pytest.mark.asyncio
async def test_create_scheduled_job_uses_lifecycle_before_workspace_lock(
    monkeypatch,
):
    from packages.core.services import realtime, scheduler_service

    calls: list[str] = []

    async def lock_lifecycle(_db, *, entity_id):
        assert entity_id == "entity-1"
        calls.append("lifecycle")

    async def lock_workspace(
        _db,
        *,
        entity_id,
        workspace_id,
        require_workspace,
    ):
        assert (entity_id, workspace_id, require_workspace) == (
            "entity-1",
            "workspace-1",
            True,
        )
        calls.append("workspace")
        return True

    async def lock_resources(_db, **_kwargs):
        calls.append("resources")

    async def persist(_db, _job):
        calls.append("persist")

    async def ignore_push(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        scheduler_service,
        "lock_reusable_resource_lifecycle",
        lock_lifecycle,
    )
    monkeypatch.setattr(
        scheduler_service,
        "_lock_workspace_scheduling_admission",
        lock_workspace,
    )
    monkeypatch.setattr(
        scheduler_service,
        "_lock_scheduled_job_resource_references",
        lock_resources,
    )
    monkeypatch.setattr(scheduler_service, "persist_scheduled_job", persist)
    monkeypatch.setattr(realtime, "push_job_update", ignore_push)
    monkeypatch.setattr(realtime, "broadcast_job_update", ignore_push)

    await scheduler_service.create_scheduled_job(
        object(),
        "entity-1",
        "ordered-job",
        "Ordered job",
        workspace_id="workspace-1",
        agent_id=generate_ulid(),
    )

    assert calls == ["lifecycle", "workspace", "resources", "persist"]


@pytest.mark.asyncio
async def test_create_scheduled_workflow_rejects_nested_foreign_workspace(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "sched_nested_workspace_owner")
    foreign_headers = await _auth(client, "sched_nested_workspace_foreign")
    foreign_workspace = await client.post(
        "/api/v1/workspaces",
        headers=foreign_headers,
        json={"name": "Foreign scheduler workspace"},
    )
    assert foreign_workspace.status_code == 201, foreign_workspace.text
    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={
            "name": "Tenant-owned scheduled workflow",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["end"]},
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )
    assert workflow.status_code == 201, workflow.text

    response = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_id": "nested-foreign-workspace",
            "name": "Nested foreign workspace",
            "schedule_kind": "every",
            "every_seconds": 60,
            "execution_type": "workflow",
            "execution_target": {
                "workflow_id": workflow.json()["id"],
                "workspace_id": foreign_workspace.json()["id"],
            },
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "execution_target.workspace_id must match the scheduled job workspace_id"
    )


@pytest.mark.asyncio
async def test_create_scheduled_job_rejects_foreign_workspace_in_service(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.scheduler_service import create_scheduled_job

    await _auth(client, "scheduled_service_workspace_owner")
    foreign_headers = await _auth(client, "scheduled_service_workspace_foreign")
    foreign_workspace = await client.post(
        "/api/v1/workspaces",
        headers=foreign_headers,
        json={"name": "Foreign scheduler service workspace"},
    )
    assert foreign_workspace.status_code == 201, foreign_workspace.text
    owner = (await db_session.execute(
        select(User).where(
            User.email == "scheduled_service_workspace_owner@test.com"
        )
    )).scalar_one()

    with pytest.raises(ValueError, match="Workspace not found"):
        await create_scheduled_job(
            db_session,
            str(owner.entity_id),
            "foreign-workspace-service-job",
            "Foreign workspace service job",
            workspace_id=foreign_workspace.json()["id"],
        )


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
    workspace_a = await _create_workspace(client, headers, "Workspace A")
    workspace_b = await _create_workspace(client, headers, "Workspace B")
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "workspace-job-a",
            "name": "Workspace Job A",
            "workspace_id": workspace_a,
        },
    )
    await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "workspace-job-b",
            "name": "Workspace Job B",
            "workspace_id": workspace_b,
        },
    )

    resp = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": workspace_a},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == "workspace-job-a"
    assert data["items"][0]["workspace_id"] == workspace_a


@pytest.mark.asyncio
async def test_list_scheduled_jobs_supports_filters_and_pagination(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import update

    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "sched_list_controls")
    controls_workspace = await _create_workspace(
        client,
        headers,
        "Controls workspace",
    )
    other_workspace = await _create_workspace(
        client,
        headers,
        "Other workspace",
    )
    jobs = [
        {
            "job_id": "alpha-enabled",
            "name": "Alpha enabled",
            "payload_message": "Prepare the alpha report",
            "workspace_id": controls_workspace,
            "agent_id": "agent-one",
        },
        {
            "job_id": "alpha-paused",
            "name": "Alpha paused",
            "workspace_id": controls_workspace,
            "agent_id": "agent-one",
        },
        {
            "job_id": "beta-attention",
            "name": "Beta attention",
            "workspace_id": controls_workspace,
            "agent_id": "agent-two",
        },
        {
            "job_id": "other-workspace",
            "name": "Alpha elsewhere",
            "workspace_id": other_workspace,
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
            "workspace_id": controls_workspace,
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
            "workspace_id": controls_workspace,
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
        params={"workspace_id": controls_workspace, "status": "paused"},
    )
    attention = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": controls_workspace, "status": "attention"},
    )

    assert paused.status_code == 200
    assert [item["job_id"] for item in paused.json()["items"]] == ["alpha-paused"]
    assert attention.status_code == 200
    assert [item["job_id"] for item in attention.json()["items"]] == ["beta-attention"]


@pytest.mark.asyncio
async def test_list_scheduled_jobs_filters_by_effective_workspace_status(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import update

    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "sched_effective_workspace_status")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Effective scheduler status workspace"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "effective-paused-job",
            "name": "Physically enabled while workspace paused",
            "workspace_id": workspace_id,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    paused = await client.post(
        f"/api/v1/workspaces/{workspace_id}/pause",
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    await db_session.execute(
        update(ScheduledJob).where(ScheduledJob.id == job_id).values(enabled=True)
    )
    await db_session.commit()

    enabled = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": workspace_id, "status": "enabled"},
    )
    paused_jobs = await client.get(
        "/api/v1/jobs",
        headers=headers,
        params={"workspace_id": workspace_id, "status": "paused"},
    )

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["items"] == []
    assert enabled.json()["total"] == 0
    assert enabled.json()["enabled_total"] == 0
    assert paused_jobs.status_code == 200, paused_jobs.text
    assert [item["job_id"] for item in paused_jobs.json()["items"]] == [
        "effective-paused-job"
    ]


@pytest.mark.asyncio
async def test_scheduler_job_responses_project_workspace_status(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import update

    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "sched_workspace_status_projection")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Scheduler response projection workspace"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "response-projection-job",
            "name": "Response projection job",
            "workspace_id": workspace_id,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    paused = await client.post(
        f"/api/v1/workspaces/{workspace_id}/pause",
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    await db_session.execute(
        update(ScheduledJob).where(ScheduledJob.id == job_id).values(enabled=True)
    )
    await db_session.commit()

    detail = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    updated = await client.put(
        f"/api/v1/jobs/{job_id}",
        headers=headers,
        json={"name": "Updated while paused"},
    )
    toggled = await client.post(
        f"/api/v1/jobs/{job_id}/toggle",
        headers=headers,
        json={"enabled": False},
    )

    for response in (detail, updated, toggled):
        assert response.status_code == 200, response.text
        assert response.json()["workspace_status"] == "paused"
        assert response.json()["enabled"] is False


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
async def test_scheduled_run_finalizer_keeps_parent_projection_on_newest_run(
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from sqlalchemy import select

    from packages.core.ledger import event_types as event_types
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace_event import WorkspaceEvent
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"ordering:{generate_ulid()}",
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        name="Ordered status projection",
        execution_type="chat_insight_extraction",
        enabled=True,
    )
    older = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=now - timedelta(minutes=2),
    )
    newer = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=now - timedelta(minutes=1),
    )
    db_session.add_all([job, older, newer])
    await db_session.commit()

    @asynccontextmanager
    async def worker_session():
        yield db_session

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)

    await _finalize_scheduled_run(
        run_id=newer.id,
        job_id_str=job.job_id,
        error="credits_exhausted: unavailable",
    )
    await _finalize_scheduled_run(
        run_id=older.id,
        job_id_str=job.job_id,
        result={"status": "completed"},
    )

    await db_session.refresh(job)
    assert job.last_status == "error"
    assert job.consecutive_errors == 1
    final_events = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.run_id.in_([older.id, newer.id]),
        )
    )).scalars())
    assert {(event.run_id, event.event_type) for event in final_events} == {
        (older.id, event_types.AUTOMATION_RUN_COMPLETED),
        (newer.id, event_types.AUTOMATION_RUN_FAILED),
    }


@pytest.mark.asyncio
async def test_scheduled_run_finalizer_projects_concurrent_delivery_once(
    db_session,
    monkeypatch,
):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services import scheduler_service
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"concurrent-finalize:{generate_ulid()}",
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        name="Concurrent finalization",
        execution_type="chat_insight_extraction",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    session_context = _concurrent_session_factory(wait_on_execute=1)
    monkeypatch.setattr(db_module, "create_worker_session", lambda: session_context)
    real_project = scheduler_service.reconcile_scheduled_job_run_projection
    project_calls = 0

    async def project_once(*args, **kwargs):
        nonlocal project_calls
        project_calls += 1
        return await real_project(*args, **kwargs)

    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        project_once,
    )
    result = {"status": "completed", "summary": "done"}
    await asyncio.gather(
        _finalize_scheduled_run(
            run_id=run.id,
            job_id_str=job.job_id,
            result=result,
        ),
        _finalize_scheduled_run(
            run_id=run.id,
            job_id_str=job.job_id,
            result=result,
        ),
    )

    db_session.expire_all()
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "completed"
    assert job.last_status == "completed"
    assert project_calls == 1


@pytest.mark.asyncio
async def test_scheduled_run_finalizer_ignores_missing_owned_run(
    db_session,
    monkeypatch,
):
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"stale-finalize:{generate_ulid()}",
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        name="Stale status projection",
        execution_type="workflow",
        enabled=True,
        last_status="running",
        consecutive_errors=2,
    )
    db_session.add(job)
    await db_session.commit()

    @asynccontextmanager
    async def worker_session():
        yield db_session

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)

    await _finalize_scheduled_run(
        run_id=generate_ulid(),
        job_id_str=job.job_id,
        error="late worker failure",
    )

    await db_session.refresh(job)
    assert job.last_status == "running"
    assert job.consecutive_errors == 2


@pytest.mark.asyncio
async def test_legacy_runless_auto_pause_uses_job_mutation_factory(
    db_session,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.models.automation_revision import AutomationRevision
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services import scheduler_service
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    original_updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"legacy-runless:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Legacy runless auto pause",
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
        enabled=True,
        consecutive_errors=2,
        revision=4,
        updated_at=original_updated_at,
        skill_generation_revision=4,
        skill_generation_next_attempt_at=original_updated_at,
    )
    db_session.add(job)
    await db_session.commit()

    @asynccontextmanager
    async def worker_session():
        yield db_session

    async def no_notification(*_args, **_kwargs):
        return None

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)
    monkeypatch.setattr(
        scheduler_service,
        "notify_scheduled_job_auto_paused",
        no_notification,
    )

    await _finalize_scheduled_run(
        run_id=None,
        job_id_str=job.job_id,
        error="legacy dispatcher failure",
    )

    await db_session.refresh(job)
    assert job.last_status == "error"
    assert job.consecutive_errors == 3
    assert job.enabled is False
    assert job.updated_at > original_updated_at
    assert job.revision == 5
    assert job.skill_generation_revision == 5
    assert job.skill_generation_next_attempt_at is None
    audit = (await db_session.execute(
        select(AutomationRevision).where(
            AutomationRevision.target_id == job.id,
            AutomationRevision.revision == 5,
        )
    )).scalar_one()
    assert audit.patch == {"enabled": False}
    assert audit.changed_by_kind == "system"


@pytest.mark.asyncio
async def test_scheduled_workflow_pause_stays_open_until_terminal(
    db_session,
):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import (
        finalize_scheduled_workflow_run,
    )

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"workflow-finalize:{generate_ulid()}",
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        name="Scheduled workflow",
        execution_type="workflow",
        enabled=True,
        last_status="running",
    )
    scheduled_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([job, scheduled_run])
    await db_session.commit()

    workflow_run = SimpleNamespace(
        id=generate_ulid(),
        trigger_source="schedule",
        trigger_data={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": scheduled_run.id,
        },
        status="paused",
        error=None,
    )

    assert await finalize_scheduled_workflow_run(db_session, workflow_run) is False
    await db_session.refresh(scheduled_run)
    assert scheduled_run.status == "running"

    workflow_run.status = "completed"
    assert await finalize_scheduled_workflow_run(db_session, workflow_run) is True
    await db_session.commit()
    await db_session.refresh(scheduled_run)
    await db_session.refresh(job)
    assert scheduled_run.status == "completed"
    assert scheduled_run.result == {
        "workflow_run_id": workflow_run.id,
        "status": "completed",
    }
    assert job.last_status == "completed"


@pytest.mark.asyncio
async def test_overlapping_scheduler_failures_auto_pause_independent_of_finish_order(
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"overlap-errors:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Overlapping failures",
        enabled=True,
        consecutive_errors=0,
    )
    runs = [
        ScheduledJobRun(
            id=generate_ulid(),
            job_id=job.job_id,
            status="running",
            started_at=now - timedelta(minutes=offset),
        )
        for offset in (3, 2, 1)
    ]
    db_session.add_all([job, *runs])
    await db_session.commit()

    @asynccontextmanager
    async def worker_session():
        yield db_session

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)

    # Finalize oldest to newest: the parent must be derived from occurrence
    # order, not from whichever worker happens to finish last.
    for run in runs:
        await _finalize_scheduled_run(
            run_id=run.id,
            job_id_str=job.job_id,
            error="provider unavailable",
        )

    await db_session.refresh(job)
    assert job.last_status == "error"
    assert job.consecutive_errors == 3
    assert job.enabled is False


@pytest.mark.asyncio
async def test_scheduler_success_boundary_resets_legacy_parent_error_streak(
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"error-boundary:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Failure after success",
        enabled=True,
        consecutive_errors=2,
    )
    older_success = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="completed",
        started_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
    )
    newest = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=now,
    )
    db_session.add_all([job, older_success, newest])
    await db_session.commit()

    @asynccontextmanager
    async def worker_session():
        yield db_session

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)
    await _finalize_scheduled_run(
        run_id=newest.id,
        job_id_str=job.job_id,
        error="provider unavailable",
    )

    await db_session.refresh(job)
    assert job.last_status == "error"
    assert job.consecutive_errors == 1
    assert job.enabled is True


@pytest.mark.asyncio
async def test_scheduler_finalizer_and_run_delete_use_parent_first_lock_order(
    db_session,
    monkeypatch,
):
    from sqlalchemy import delete, select

    from packages.core.database import async_session
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _finalize_scheduled_run

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"delete-lock-order:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Delete lock order",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    job_lock_attempted = asyncio.Event()

    class SignalledSession:
        def __init__(self, session):
            self._session = session
            self._execute_count = 0

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def execute(self, statement, *args, **kwargs):
            self._execute_count += 1
            if self._execute_count == 1:
                job_lock_attempted.set()
            return await self._session.execute(statement, *args, **kwargs)

    @asynccontextmanager
    async def worker_session():
        async with async_session() as session:
            yield SignalledSession(session)

    monkeypatch.setattr(db_module, "create_worker_session", lambda: worker_session)

    async with async_session() as deleting_db:
        locked_job = (await deleting_db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job.id)
            .with_for_update()
        )).scalar_one()
        assert locked_job.id == job.id

        finalizer = asyncio.create_task(_finalize_scheduled_run(
            run_id=run.id,
            job_id_str=job.job_id,
            error="failed",
        ))
        await asyncio.wait_for(job_lock_attempted.wait(), timeout=2)
        await deleting_db.execute(
            delete(ScheduledJobRun).where(ScheduledJobRun.id == run.id)
        )
        await asyncio.wait_for(deleting_db.commit(), timeout=2)

    await asyncio.wait_for(finalizer, timeout=2)


@pytest.mark.asyncio
async def test_scheduled_job_mutation_locks_lifecycle_before_job_and_resources(
    monkeypatch,
):
    from packages.core.services import scheduler_service

    events = []
    job = SimpleNamespace(
        id="job-pk",
        job_id="job-id",
        entity_id="entity-1",
        execution_target={},
        agent_id=None,
        last_run_at=None,
        last_status=None,
        delete_after_run=False,
        enabled=True,
        consecutive_errors=0,
        updated_at=None,
        revision=1,
    )

    class Result:
        def scalar_one_or_none(self):
            return job

    class DB:
        async def execute(self, _statement):
            events.append("job-row")
            return Result()

        async def flush(self):
            return None

    async def lock_lifecycle(_db, *, entity_id):
        assert entity_id == job.entity_id
        events.append("lifecycle")

    async def lock_resources(_db, **_kwargs):
        events.append("resources")

    async def bump(*_args, **_kwargs):
        events.append("revision")
        return 2

    monkeypatch.setattr(
        scheduler_service,
        "lock_reusable_resource_lifecycle",
        lock_lifecycle,
    )
    monkeypatch.setattr(
        scheduler_service,
        "lock_reusable_resource_references",
        lock_resources,
    )
    monkeypatch.setattr(scheduler_service, "bump_revision", bump)

    result = await scheduler_service.ScheduledJobMutationFactory.apply(
        DB(),
        job,
        {"execution_target": {"skill_id": "01H00000000000000000000000"}},
    )

    assert result is not None
    assert events == ["lifecycle", "job-row", "resources", "revision"]


@pytest.mark.asyncio
async def test_scheduled_job_content_mutation_skips_resource_topology_locks(
    monkeypatch,
):
    from packages.core.services import scheduler_service

    events = []
    job = SimpleNamespace(
        id="job-pk",
        job_id="job-id",
        entity_id="entity-1",
        execution_target={"skill_id": "01H00000000000000000000000"},
        agent_id=None,
        payload_message="old prompt",
        last_run_at=None,
        last_status=None,
        delete_after_run=False,
        enabled=True,
        consecutive_errors=0,
        updated_at=None,
        revision=1,
    )

    class Result:
        def scalar_one_or_none(self):
            return job

    class DB:
        async def execute(self, _statement):
            events.append("job-row")
            return Result()

        async def flush(self):
            return None

    async def unexpected_lock(*_args, **_kwargs):
        raise AssertionError("content-only mutation must not take topology locks")

    async def bump(*_args, **_kwargs):
        events.append("revision")
        job.revision += 1
        return job.revision

    monkeypatch.setattr(
        scheduler_service,
        "lock_reusable_resource_lifecycle",
        unexpected_lock,
    )
    monkeypatch.setattr(
        scheduler_service,
        "lock_reusable_resource_references",
        unexpected_lock,
    )
    monkeypatch.setattr(scheduler_service, "bump_revision", bump)

    result = await scheduler_service.ScheduledJobMutationFactory.apply(
        DB(),
        job,
        {"payload_message": "new prompt"},
    )

    assert result is not None
    assert result.content_patch == {"payload_message": "new prompt"}
    assert events == ["job-row", "revision"]


def test_scheduler_bookkeeping_failure_does_not_replay_successful_body(
    monkeypatch,
):
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    body_calls = 0
    retry_calls = 0
    deferred = []

    async def body():
        nonlocal body_calls
        body_calls += 1
        return {"status": "completed"}

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield claim_service._claim(
            "run-1",
            "worker-1",
            granted=True,
            reason=claim_service.CLAIM_GRANTED,
        )

    async def admit_run(*_args, **_kwargs):
        return SimpleNamespace(admitted=True, reason="scheduled_child_admitted")

    async def fail_finalizer(**_kwargs):
        raise RuntimeError("scheduler bookkeeping unavailable")

    async def defer_settlement(**kwargs):
        deferred.append(kwargs)

    class Task:
        max_retries = 2
        request = SimpleNamespace(retries=0)

        def retry(self, **_kwargs):
            nonlocal retry_calls
            retry_calls += 1
            raise AssertionError("completed business work must not retry")

    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(
        claim_service,
        "scheduled_run_execution_claim",
        granted_claim,
    )
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admit_run)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", fail_finalizer)
    monkeypatch.setattr(
        ai_tasks,
        "_defer_scheduled_run_settlement",
        defer_settlement,
        raising=False,
    )

    result = ai_tasks._run_scheduled(
        Task(),
        "test automation",
        "workspace-1",
        body,
        run_id="run-1",
        job_id_str="job-1",
    )

    assert result == {"status": "completed"}
    assert body_calls == 1
    assert retry_calls == 0
    execution_claim = deferred[0].pop("execution_claim")
    assert execution_claim.run_id == "run-1"
    assert deferred == [{
        "run_id": "run-1",
        "job_id_str": "job-1",
        "result": {"status": "completed"},
        "error": None,
    }]


def test_unfenced_handoff_replaces_business_task_with_settlement(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.tasks import ai_tasks

    body_calls = 0
    finalized = []
    enqueue_calls = 0

    async def body():
        nonlocal body_calls
        body_calls += 1
        return {"status": "completed", "value": 42}

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield claim_service._claim(
            "run-1",
            "worker-1",
            granted=True,
            reason=claim_service.CLAIM_GRANTED,
        )

    async def admit_run(*_args, **_kwargs):
        return SimpleNamespace(admitted=True, reason="scheduled_child_admitted")

    async def fail_finalizer(**_kwargs):
        raise RuntimeError("database unavailable")

    async def fail_persistence(**_kwargs):
        raise RuntimeError("database unavailable")

    def fail_enqueue(**_kwargs):
        nonlocal enqueue_calls
        enqueue_calls += 1
        raise RuntimeError("broker unavailable")

    class ReplaceSignal(RuntimeError):
        def __init__(self, signature):
            super().__init__("replace")
            self.signature = signature

    class Task:
        def replace(self, signature):
            raise ReplaceSignal(signature)

    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(
        claim_service,
        "scheduled_run_execution_claim",
        granted_claim,
    )
    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", admit_run)
    monkeypatch.setattr(ai_tasks, "_finalize_scheduled_run", fail_finalizer)
    monkeypatch.setattr(
        ai_tasks,
        "_persist_scheduled_run_settlement",
        fail_persistence,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_run_settlement",
        fail_enqueue,
    )

    with pytest.raises(ReplaceSignal) as replacement_info:
        ai_tasks._run_scheduled(
            Task(),
            "test automation",
            "workspace-1",
            body,
            run_id="run-1",
            job_id_str="job-1",
        )

    from packages.core.queues import CeleryQueue

    replacement = replacement_info.value.signature
    assert replacement.task == "scheduler.settle_scheduled_run"
    assert replacement.options["queue"] == CeleryQueue.RECOVERY_V2.value
    assert replacement.args[:2] == ("run-1", "job-1")
    settlement_payload = replacement.args[2]
    assert body_calls == 1
    assert enqueue_calls == 0

    async def successful_finalizer(**kwargs):
        finalized.append(kwargs)
        return True

    monkeypatch.setattr(
        ai_tasks,
        "_finalize_scheduled_run_with_claim",
        successful_finalizer,
    )
    result = ai_tasks.settle_scheduled_run.run(
        "run-1",
        "job-1",
        settlement_payload,
    )

    assert result == {
        "run_id": "run-1",
        "status": "completed",
    }
    assert body_calls == 1
    assert finalized[0]["run_id"] == "run-1"
    assert finalized[0]["outcome"].result == {
        "status": "completed",
        "value": 42,
    }


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

    session_context = _concurrent_session_factory(wait_on_execute=2)

    result = {
        "status": "completed",
        "response": "**Daily YouTube public metrics** — views 0, comments unavailable.",
        "duration_ms": 1250,
    }
    await asyncio.gather(
        _update_job_run_status_async(session_context, task.id, result),
        _update_job_run_status_async(session_context, task.id, result),
    )

    db_session.expire_all()
    await db_session.refresh(run)
    await db_session.refresh(job)

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
async def test_scheduled_agent_chat_projection_failure_keeps_terminal_state(
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.constants.execution import (
        SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS,
        ScheduledRecoveryKind,
    )
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.task_service import create_task
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"projection-failure:{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Projection failure",
        execution_type="agent",
        default_delivery_mode="workspace_chat",
        enabled=True,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Projection failure",
        task_type="ai_generated",
        workspace_id=workspace_id,
        details={"scheduled_job_id": job.job_id},
    )
    task.status = "in_progress"
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add(run)
    await db_session.commit()

    queued = []

    async def fail_chat_projection(*_args, **_kwargs):
        raise RuntimeError("message projection rejected")

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr(
        ai_tasks,
        "_deliver_scheduled_agent_result",
        fail_chat_projection,
    )
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda run_id: queued.append(run_id),
    )

    projection_started_at = datetime.now(timezone.utc)
    await ai_tasks._update_job_run_status_async(
        session_context,
        task.id,
        {"status": "completed", "response": "done"},
    )

    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "success"
    assert job.last_status == "success"
    assert run.result["scheduled_result_projection"]["state"] == "pending"
    assert run.result["scheduled_result_projection"]["task_id"] == task.id
    assert run.result["scheduled_result_projection"]["recovery_attempts"] == 0
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.RESULT_PROJECTION
    )
    assert run.result[recovery_key] >= (
        projection_started_at
        + timedelta(seconds=SCHEDULED_RESULT_PROJECTION_RECOVERY_DELAY_SECONDS)
    ).timestamp()
    assert queued == [run.id]

    assert await ai_tasks._ensure_scheduled_result_projection_recovery_chain(
        session_context,
        run.id,
        "projection-chain-1",
    )
    assert await ai_tasks._ensure_scheduled_result_projection_recovery_chain(
        session_context,
        run.id,
        "projection-chain-1",
    )
    await db_session.refresh(run)
    assert run.result["scheduled_result_projection"]["recovery_attempts"] == 1
    assert (
        run.result["scheduled_result_projection"]["recovery_chain_id"]
        == "projection-chain-1"
    )

    async def recover_chat_projection(*_args, **_kwargs):
        return SimpleNamespace(id="message-recovered")

    published = []

    async def publish(*_args, **kwargs):
        published.append(kwargs["message"].id)

    from packages.core.workspace_chat import service as chat_service

    monkeypatch.setattr(
        ai_tasks,
        "_deliver_scheduled_agent_result",
        recover_chat_projection,
    )
    monkeypatch.setattr(chat_service, "publish_workspace_chat_message_event", publish)

    recovered = await ai_tasks._project_scheduled_agent_result_async(
        session_context,
        run.id,
    )

    await db_session.refresh(run)
    assert recovered == {"run_id": run.id, "status": "delivered"}
    assert run.result["scheduled_result_projection"]["state"] == "delivered"
    assert published == ["message-recovered"]


@pytest.mark.asyncio
async def test_scheduled_agent_projection_quarantines_cross_workspace_task(
    db_session,
    monkeypatch,
):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.services.task_service import create_task
    from packages.core.tasks import ai_tasks

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"projection-scope:{generate_ulid()}",
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        name="Projection scope",
        execution_type="agent",
        default_delivery_mode="workspace_chat",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        result={"response": "must stay in the owning workspace"},
    )
    other_task = await create_task(
        db_session,
        generate_ulid(),
        title="Unrelated task",
        task_type="ai_generated",
        workspace_id=generate_ulid(),
        details={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
    )
    apply_scheduled_result_projection(
        run,
        ScheduledResultProjection.workspace_chat(task_id=other_task.id),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    @asynccontextmanager
    async def session_context():
        yield db_session

    async def unexpected_delivery(*_args, **_kwargs):
        raise AssertionError("cross-Workspace output must not be delivered")

    monkeypatch.setattr(
        ai_tasks,
        "_deliver_scheduled_agent_result",
        unexpected_delivery,
    )

    result = await ai_tasks._project_scheduled_agent_result_async(
        session_context,
        run.id,
    )

    await db_session.refresh(run)
    assert result == {"run_id": run.id, "status": "quarantined"}
    assert run.result["scheduled_result_projection"]["state"] == "quarantined"
    assert (
        run.result["scheduled_result_projection"]["error"]
        == "scheduled projection task lineage mismatch"
    )


@pytest.mark.asyncio
async def test_scheduled_agent_chat_projection_publishes_only_after_commit(
    db_session,
    monkeypatch,
):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.task_service import create_task
    from packages.core.tasks import ai_tasks
    from packages.core.workspace_chat import service as chat_service

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"post-commit-projection:{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Post-commit projection",
        execution_type="agent",
        default_delivery_mode="workspace_chat",
        enabled=True,
    )
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Post-commit projection",
        task_type="ai_generated",
        workspace_id=workspace_id,
        details={"scheduled_job_id": job.job_id},
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.status = "in_progress"
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add_all([job, run])
    await db_session.commit()

    committed = False
    published = []

    @asynccontextmanager
    async def session_context():
        class SessionProxy:
            def __getattr__(self, name):
                return getattr(db_session, name)

            async def commit(self):
                nonlocal committed
                await db_session.commit()
                committed = True

        yield SessionProxy()

    async def publish(entity_id_arg, *, workspace_id, message):
        assert committed is True
        published.append((entity_id_arg, workspace_id, message.id))

    original_post_message = chat_service.post_message

    async def post_message(*args, **kwargs):
        assert kwargs["publish_event"] is False
        return await original_post_message(*args, **kwargs)

    monkeypatch.setattr(chat_service, "post_message", post_message)
    monkeypatch.setattr(chat_service, "publish_workspace_chat_message_event", publish)

    await ai_tasks._update_job_run_status_async(
        session_context,
        task.id,
        {"status": "completed", "response": "committed result"},
    )

    await db_session.refresh(run)
    assert run.result["scheduled_result_projection"]["state"] == "delivered"
    assert len(published) == 1


@pytest.mark.asyncio
async def test_scheduled_agent_settlement_survives_missing_task(db_session):
    from packages.core.database import async_session
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _update_job_run_status_async

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"missing-settlement-task:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Missing settlement task",
        execution_type="agent",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    await _update_job_run_status_async(
        async_session,
        generate_ulid(),
        {"status": "completed", "response": "done"},
        scheduled_run_id=run.id,
        scheduled_job_id=job.job_id,
    )

    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "success"
    assert job.last_status == "success"


@pytest.mark.asyncio
async def test_successful_workspace_job_reconciles_blueprint_startup_after_commit(
    db_session,
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace import Workspace
    from packages.core.services import blueprint_startup_service
    from packages.core.services.task_service import create_task
    from packages.core.tasks.ai_tasks import _update_job_run_status_async

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Setup completion",
        status="active",
        settings={},
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"setup-completion:{generate_ulid()}",
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name="Prepare identity",
        execution_type="agent",
        enabled=True,
    )
    task = await create_task(
        db_session,
        workspace.entity_id,
        title="[Auto] Prepare identity",
        task_type="ai_generated",
        workspace_id=workspace.id,
        details={"scheduled_job_id": job.job_id},
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add_all([workspace, job, run])
    await db_session.commit()
    calls: list[tuple[str, str]] = []

    async def reconcile(db, *, workspace_id: str, trigger: str):
        persisted = await db.get(ScheduledJobRun, run.id)
        assert persisted is not None and persisted.status == "success"
        calls.append((workspace_id, trigger))

    monkeypatch.setattr(
        blueprint_startup_service,
        "reconcile_blueprint_startup",
        reconcile,
    )

    @asynccontextmanager
    async def session_context():
        yield db_session

    await _update_job_run_status_async(
        session_context,
        task.id,
        {"status": "completed", "response": "ready"},
    )

    assert calls == [(workspace.id, "scheduled_job_success")]


@pytest.mark.asyncio
async def test_invalid_prepared_dispatch_is_terminally_quarantined(db_session):
    from packages.core.constants.execution import ScheduledDispatchState
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import _quarantine_scheduled_recovery

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"invalid-dispatch:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Invalid dispatch",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
        result={
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch": {"kind": "removed_kind", "args": [], "kwargs": {}},
        },
    )
    db_session.add_all([job, run])
    await db_session.commit()

    await _quarantine_scheduled_recovery(
        db_session,
        run,
        reason="invalid_dispatch_payload",
    )
    await db_session.commit()

    await db_session.refresh(job)
    await db_session.refresh(run)
    assert run.status == "error"
    assert run.result["dispatch_status"] == ScheduledDispatchState.QUARANTINED.value
    assert run.result["scheduled_recovery_error"] == "invalid_dispatch_payload"
    assert job.last_status == "error"

    orphan = ScheduledJobRun(
        id=generate_ulid(),
        job_id=f"missing-parent:{generate_ulid()}",
        status="running",
        started_at=datetime.now(timezone.utc),
        result={
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch": {"kind": "removed_kind", "args": [], "kwargs": {}},
        },
    )
    db_session.add(orphan)
    await db_session.commit()

    await _quarantine_scheduled_recovery(
        db_session,
        orphan,
        reason="invalid_dispatch_payload",
    )
    await db_session.commit()

    await db_session.refresh(orphan)
    assert orphan.status == "error"
    assert orphan.result["dispatch_status"] == ScheduledDispatchState.QUARANTINED.value


@pytest.mark.asyncio
async def test_invalid_result_projection_quarantine_releases_dispatch_recovery(
    db_session,
):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        RESULT_PROJECTION_KEY,
        RESULT_PROJECTION_QUARANTINE_KEY,
    )
    from packages.core.tasks import scheduler_tasks

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"invalid-projection:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Invalid result projection",
        enabled=True,
    )
    dispatch = scheduler_tasks._prepared_scheduled_dispatch(
        ScheduledDispatchKind.AGENT_TASK,
        args=[generate_ulid(), generate_ulid()],
    )
    invalid_projection = {
        "state": "pending",
        "task_id": generate_ulid(),
    }
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        started_at=datetime.now(timezone.utc),
        result={
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch": dispatch,
            RESULT_PROJECTION_KEY: invalid_projection,
        },
    )
    db_session.add_all([job, run])
    await db_session.commit()

    quarantined = await scheduler_tasks._quarantine_scheduled_recovery(
        db_session,
        run,
        reason="invalid_result_projection_payload",
    )
    await db_session.commit()

    await db_session.refresh(run)
    assert quarantined is True
    assert RESULT_PROJECTION_KEY not in run.result
    assert run.result[RESULT_PROJECTION_QUARANTINE_KEY] == {
        "state": "quarantined",
        "error": "invalid_result_projection_payload",
        "payload": invalid_projection,
    }
    assert scheduler_tasks._prepared_scheduled_dispatch_recovery(run) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "projection_case",
    ["missing-state", "unknown-state", "null-payload"],
)
async def test_stale_sweep_quarantines_malformed_projection_state(
    client,
    db_session,
    projection_case,
):
    from datetime import timedelta

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        RESULT_PROJECTION_KEY,
        RESULT_PROJECTION_QUARANTINE_KEY,
    )
    from packages.core.tasks import scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"malformed-projection:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Malformed projection",
        enabled=True,
    )
    projection = None
    if projection_case != "null-payload":
        projection = {
            "kind": "workspace_chat",
            "task_id": generate_ulid(),
        }
        if projection_case == "unknown-state":
            projection["state"] = "unknown"
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        result={RESULT_PROJECTION_KEY: projection},
    )
    db_session.add_all([job, run])
    await db_session.commit()

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        datetime.now(timezone.utc),
    )

    await db_session.refresh(run)
    assert handled == 1
    assert RESULT_PROJECTION_KEY not in run.result
    assert run.result[RESULT_PROJECTION_QUARANTINE_KEY] == {
        "state": "quarantined",
        "error": "invalid_result_projection_payload",
        "payload": projection,
    }


@pytest.mark.asyncio
async def test_stale_sweep_quarantines_valid_orphan_projection_once(
    client,
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        RESULT_PROJECTION_KEY,
        RESULT_PROJECTION_QUARANTINE_KEY,
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=f"missing-parent:{generate_ulid()}",
        status="success",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        result={},
    )
    projection = ScheduledResultProjection.workspace_chat(task_id=generate_ulid())
    apply_scheduled_result_projection(run, projection)
    db_session.add(run)
    await db_session.commit()

    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda _run_id: (_ for _ in ()).throw(
            AssertionError("an orphan recovery must not be queued")
        ),
    )

    now = datetime.now(timezone.utc)
    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now,
    )
    await db_session.refresh(run)

    assert handled == 1
    assert run.status == "success"
    assert RESULT_PROJECTION_KEY not in run.result
    assert run.result[RESULT_PROJECTION_QUARANTINE_KEY] == {
        "state": "quarantined",
        "error": "missing_scheduled_job",
        "payload": projection.to_payload(),
    }
    assert run.result["scheduled_recovery_error"] == "missing_scheduled_job"
    assert await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now,
    ) == 0


@pytest.mark.asyncio
async def test_invalid_calendar_backoff_cannot_hide_due_dispatch_recovery(
    client,
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import scheduler_tasks

    del client  # The fixture gives this integration test a clean database.
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"invalid-recovery-calendar:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Invalid recovery calendar",
        enabled=True,
    )
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.DISPATCH
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.GOAL_MEASUREMENT,
                args=[generate_ulid()],
            ),
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            recovery_key: "9999-99-99T99:99:99+00:00",
        },
    )
    db_session.add_all([job, run])
    await db_session.commit()
    queued = []
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda run_id: queued.append(run_id),
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        datetime.now(timezone.utc),
    )
    await db_session.refresh(run)

    assert handled == 1
    assert queued == [run.id]
    assert isinstance(run.result[recovery_key], float)


@pytest.mark.asyncio
async def test_oversized_numeric_backoff_cannot_abort_postgres_recovery_scan(
    client,
    db_session,
    monkeypatch,
):
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledDispatchState,
        ScheduledRecoveryKind,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.tasks import scheduler_tasks

    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("numeric overflow regression requires PostgreSQL")
    del client
    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"oversized-recovery-epoch:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Oversized recovery epoch",
        enabled=True,
    )
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.DISPATCH
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        created_at=now - timedelta(minutes=10),
        result={
            "dispatch": scheduler_tasks._prepared_scheduled_dispatch(
                ScheduledDispatchKind.GOAL_MEASUREMENT,
                args=[generate_ulid()],
            ),
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            recovery_key: "9" * 1000,
        },
    )
    db_session.add_all([job, run])
    await db_session.commit()
    queued: list[str] = []
    monkeypatch.setattr(
        scheduler_tasks._recover_prepared_scheduled_dispatch_task,
        "delay",
        lambda run_id: queued.append(run_id),
    )

    handled = await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
        db_session,
        now,
    )

    assert handled == 1
    assert queued == [run.id]


@pytest.mark.asyncio
async def test_settlement_fence_starts_with_recovery_backoff(db_session):
    from packages.core.constants.execution import (
        SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS,
        ScheduledRecoveryKind,
        ScheduledRunStatus,
        ScheduledSettlementKind,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunSettlement,
        persist_scheduled_run_settlement_pending,
        scheduled_recovery_next_attempt_at_key,
    )

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"settlement-fence-backoff:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Settlement fence backoff",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status=ScheduledRunStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add_all([job, run])
    await db_session.commit()
    before = datetime.now(timezone.utc)

    persisted = await persist_scheduled_run_settlement_pending(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        settlement=ScheduledRunSettlement.create(
            kind=ScheduledSettlementKind.GENERIC,
            result={"status": "completed"},
            error=None,
        ),
    )
    after = datetime.now(timezone.utc)

    assert persisted is True
    recovery_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.SETTLEMENT
    )
    assert run.result[recovery_key] >= (
        before
        + timedelta(seconds=SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS)
    ).timestamp()
    assert run.result[recovery_key] <= (
        after
        + timedelta(seconds=SCHEDULED_SETTLEMENT_RECOVERY_DELAY_SECONDS)
    ).timestamp()


@pytest.mark.asyncio
async def test_quarantine_revalidates_terminal_projection_under_lock(db_session):
    from packages.core.constants.execution import (
        SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS,
        ScheduledResultProjectionState,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        RESULT_PROJECTION_KEY,
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import scheduler_tasks

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"terminal-projection:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Terminal projection",
        enabled=True,
    )
    projection = ScheduledResultProjection.workspace_chat(task_id=generate_ulid())
    for chain in range(SCHEDULED_RESULT_PROJECTION_MAX_RECOVERY_CHAINS):
        projection = projection.for_retry(recovery_chain_id=f"chain-{chain}")
    projection = projection.with_state(ScheduledResultProjectionState.DELIVERED)
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        started_at=datetime.now(timezone.utc),
        result={},
    )
    apply_scheduled_result_projection(run, projection)
    db_session.add_all([job, run])
    await db_session.commit()

    quarantined = await scheduler_tasks._quarantine_scheduled_recovery(
        db_session,
        run,
        reason="result_projection_retry_exhausted",
    )
    await db_session.commit()

    await db_session.refresh(run)
    assert quarantined is False
    assert (
        run.result[RESULT_PROJECTION_KEY]["state"]
        == ScheduledResultProjectionState.DELIVERED.value
    )


@pytest.mark.asyncio
async def test_concurrent_stale_projection_sweeps_claim_once(
    client,
    db_session,
    monkeypatch,
):
    from datetime import timedelta

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledResultProjection,
        apply_scheduled_result_projection,
    )
    from packages.core.tasks import ai_tasks, scheduler_tasks

    del client  # The fixture configures the shared worker session factory.
    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"concurrent-projection:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Concurrent projection",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="success",
        created_at=now - timedelta(minutes=10),
        result={},
    )
    apply_scheduled_result_projection(
        run,
        ScheduledResultProjection.workspace_chat(task_id=generate_ulid()),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    queued = []
    monkeypatch.setattr(
        ai_tasks,
        "_enqueue_scheduled_agent_result_projection",
        lambda run_id: queued.append(run_id),
    )
    session_context = _concurrent_session_factory(wait_on_execute=1)

    async def sweep():
        async with session_context() as recovery_db:
            return await scheduler_tasks._queue_stale_prepared_dispatch_recovery(
                recovery_db,
                now,
            )

    handled = await asyncio.gather(sweep(), sweep())

    assert sum(handled) == 1
    assert queued == [run.id]


@pytest.mark.asyncio
async def test_disabled_scheduled_agent_admission_cancels_run_and_task(db_session):
    from packages.core.constants.execution import (
        ScheduledChildAdmissionStatus,
        ScheduledDispatchKind,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import admit_scheduled_child
    from packages.core.services.task_service import create_task

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"disabled-admission:{generate_ulid()}",
        entity_id=entity_id,
        name="Disabled admission",
        execution_type="agent",
        enabled=False,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Disabled admission",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add(run)
    await db_session.commit()

    admission = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=task.id,
    )
    await db_session.commit()

    await db_session.refresh(run)
    await db_session.refresh(task)
    assert admission.status is ScheduledChildAdmissionStatus.CANCELLED
    assert run.status == "cancelled"
    assert run.result["reason"] == "scheduled_job_disabled"
    assert task.status == "cancelled"


@pytest.mark.asyncio
async def test_scheduled_child_admission_serializes_with_parent_pause(db_session):
    from sqlalchemy import select

    from packages.core.constants.execution import ScheduledChildAdmissionStatus
    from packages.core.database import async_session
    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import admit_scheduled_child

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"pause-race:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Pause race",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([job, run])
    await db_session.commit()

    async def admit_in_fresh_transaction():
        async with async_session() as admission_db:
            result = await admit_scheduled_child(
                admission_db,
                run_id=run.id,
                job_id=job.job_id,
            )
            await admission_db.commit()
            return result

    async with async_session() as pause_db:
        locked_job = (await pause_db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job.id)
            .with_for_update()
        )).scalar_one()
        locked_job.enabled = False
        admission_task = asyncio.create_task(admit_in_fresh_transaction())
        await asyncio.sleep(0.1)
        assert admission_task.done() is False
        await pause_db.commit()

    admission = await asyncio.wait_for(admission_task, timeout=2)
    assert admission.status is ScheduledChildAdmissionStatus.CANCELLED


@pytest.mark.asyncio
async def test_admitted_child_can_resume_after_later_job_pause(db_session):
    from packages.core.constants.execution import (
        SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
        ScheduledChildAdmissionStatus,
        ScheduledDispatchKind,
        ScheduledRecoveryKind,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        admit_scheduled_child,
        scheduled_recovery_next_attempt_at_key,
    )
    from packages.core.services.task_service import create_task

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"admitted-before-pause:{generate_ulid()}",
        entity_id=entity_id,
        name="Admitted before pause",
        execution_type="agent",
        enabled=True,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Already admitted",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add(run)
    await db_session.commit()

    admitted_after = datetime.now(timezone.utc)
    first = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=task.id,
    )
    await db_session.commit()
    await db_session.refresh(run)
    assert first.status is ScheduledChildAdmissionStatus.ADMITTED
    execution_deadline_key = scheduled_recovery_next_attempt_at_key(
        ScheduledRecoveryKind.EXECUTION,
    )
    first_execution_deadline = run.result[execution_deadline_key]
    assert first_execution_deadline >= (
        admitted_after.timestamp() + SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS
    )

    job.enabled = False
    await db_session.commit()
    second = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=task.id,
    )
    await db_session.commit()

    await db_session.refresh(run)
    await db_session.refresh(task)
    assert second.status is ScheduledChildAdmissionStatus.ADMITTED
    assert second.reason == "scheduled_child_admitted_before_disable"
    assert run.status == "running"
    assert task.status == "pending"
    assert run.result[execution_deadline_key] == first_execution_deadline


@pytest.mark.asyncio
async def test_scheduled_child_admission_rejects_wrong_dispatch_identity(
    db_session,
):
    from packages.core.constants.execution import (
        ScheduledChildAdmissionStatus,
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        admit_scheduled_child,
        scheduled_child_execution_state,
    )
    from packages.core.services.task_service import create_task

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"exact-child:{generate_ulid()}",
        entity_id=entity_id,
        name="Exact child",
        execution_type="agent",
        enabled=True,
        revision=4,
    )
    db_session.add(job)
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.flush()
    expected = await create_task(
        db_session,
        entity_id,
        title="Expected child",
        task_type="ai_generated",
        details={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
    )
    wrong = await create_task(
        db_session,
        entity_id,
        title="Wrong child",
        task_type="ai_generated",
        details={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
    )
    run.result = {
        "dispatch": {
            "kind": ScheduledDispatchKind.AGENT_TASK.value,
            "args": [expected.id, None],
            "kwargs": {},
        },
        "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
        "dispatch_ledger": {"revision": job.revision},
    }
    await db_session.commit()

    rejected = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=wrong.id,
    )
    await db_session.commit()
    await db_session.refresh(run)

    assert rejected.status is ScheduledChildAdmissionStatus.CLOSED
    assert rejected.reason == "scheduled_child_lineage_mismatch"
    assert scheduled_child_execution_state(run) is None

    admitted = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=expected.id,
    )
    await db_session.commit()
    assert admitted.status is ScheduledChildAdmissionStatus.ADMITTED

    rejected_after_admission = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=wrong.id,
    )
    assert rejected_after_admission.status is ScheduledChildAdmissionStatus.CLOSED
    assert rejected_after_admission.reason == "scheduled_child_identity_mismatch"


@pytest.mark.asyncio
async def test_scheduled_child_admission_cancels_stale_revision(db_session):
    from packages.core.constants.execution import (
        ScheduledChildAdmissionStatus,
        ScheduledDispatchKind,
        ScheduledDispatchState,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import admit_scheduled_child
    from packages.core.services.task_service import create_task

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"stale-child:{generate_ulid()}",
        entity_id=entity_id,
        name="Stale child",
        execution_type="agent",
        enabled=True,
        revision=8,
    )
    db_session.add(job)
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.flush()
    task = await create_task(
        db_session,
        entity_id,
        title="Stale prepared child",
        task_type="ai_generated",
        details={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
    )
    run.result = {
        "dispatch": {
            "kind": ScheduledDispatchKind.AGENT_TASK.value,
            "args": [task.id, None],
            "kwargs": {},
        },
        "dispatch_status": ScheduledDispatchState.PUBLISHED.value,
        "dispatch_ledger": {"revision": 7},
    }
    await db_session.commit()

    admission = await admit_scheduled_child(
        db_session,
        run_id=run.id,
        job_id=job.job_id,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=task.id,
    )
    await db_session.commit()
    await db_session.refresh(run)
    await db_session.refresh(task)

    assert admission.status is ScheduledChildAdmissionStatus.CANCELLED
    assert admission.reason == "scheduled_job_revision_changed"
    assert run.status == "cancelled"
    assert run.result["reason"] == "scheduled_job_revision_changed"
    assert task.status == "cancelled"


@pytest.mark.asyncio
async def test_deleted_occurrence_terminalizes_pristine_prepared_children(
    db_session,
):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.constants.execution import ScheduledDispatchKind
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
    from packages.core.services.task_service import create_task
    from packages.core.tasks.ai_tasks import (
        _terminalize_suppressed_scheduled_child,
    )

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"deleted-prepared-child:{generate_ulid()}",
        entity_id=entity_id,
        name="Deleted prepared child",
        enabled=True,
    )
    db_session.add(job)
    await db_session.flush()
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Prepared Task",
        task_type="ai_generated",
        details={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
    )
    workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Prepared Flow",
        steps=[],
    )
    workflow_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow.id,
        entity_id=entity_id,
        status="running",
        trigger_source="schedule",
        trigger_data={
            "scheduled_job_id": job.job_id,
            "scheduled_run_id": run.id,
        },
        definition_snapshot={"steps": []},
    )
    db_session.add_all([run, workflow, workflow_run])
    await db_session.commit()
    task_id = task.id
    workflow_run_id = workflow_run.id
    scheduled_run_id = run.id
    scheduled_job_id = job.job_id

    await db_session.execute(
        delete(ScheduledJobRun).where(ScheduledJobRun.id == scheduled_run_id)
    )
    await db_session.execute(delete(ScheduledJob).where(ScheduledJob.id == job.id))
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    assert await _terminalize_suppressed_scheduled_child(
        sessions,
        child_kind=ScheduledDispatchKind.AGENT_TASK,
        child_id=task_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
    ) is True
    assert await _terminalize_suppressed_scheduled_child(
        sessions,
        child_kind=ScheduledDispatchKind.WORKFLOW,
        child_id=workflow_run_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
    ) is True

    db_session.expire_all()
    assert (await db_session.get(type(task), task_id)).status == "cancelled"
    assert (await db_session.get(WorkflowRun, workflow_run_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_scheduled_agent_finalizes_immutable_occurrence_ids(db_session):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.task_service import create_task
    from packages.core.tasks.ai_tasks import _update_job_run_status_async

    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"immutable-agent:{generate_ulid()}",
        entity_id=entity_id,
        name="Overlapping agent occurrences",
        job_type="cron",
        timezone="UTC",
        execution_type="agent",
        enabled=True,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Immutable occurrence",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
    )
    stale_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    exact_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {
        **task.details,
        "scheduled_run_id": stale_run.id,
    }
    db_session.add_all([stale_run, exact_run])
    await db_session.commit()

    @asynccontextmanager
    async def session_context():
        yield db_session

    await _update_job_run_status_async(
        session_context,
        task.id,
        {"status": "completed", "duration_ms": 10},
        scheduled_run_id=exact_run.id,
        scheduled_job_id=job.job_id,
    )

    await db_session.refresh(stale_run)
    await db_session.refresh(exact_run)
    assert stale_run.status == "running"
    assert exact_run.status == "success"


@pytest.mark.asyncio
async def test_failed_scheduled_agent_run_auto_pauses_and_finalizes_once(
    db_session,
):
    from sqlalchemy import func, select

    from packages.core.models.notification import Notification
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.task_service import create_task
    from packages.core.tasks.ai_tasks import _update_job_run_status_async

    entity_id = generate_ulid()
    user_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"agent-failure:{generate_ulid()}",
        entity_id=entity_id,
        user_id=user_id,
        name="Repeatedly failing agent",
        job_type="cron",
        timezone="UTC",
        execution_type="agent",
        enabled=True,
        consecutive_errors=2,
    )
    db_session.add(job)
    task = await create_task(
        db_session,
        entity_id,
        title="[Auto] Repeatedly failing agent",
        description="Exercise terminal scheduled-run handling.",
        task_type="ai_generated",
        details={"scheduled_job_id": job.job_id},
    )
    task.status = "in_progress"
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    task.details = {**task.details, "scheduled_run_id": run.id}
    db_session.add(run)
    await db_session.commit()

    @asynccontextmanager
    async def session_context():
        yield db_session

    result = {
        "status": "failed",
        "error_type": "ProviderTimeout",
        "error": "provider timed out",
    }
    await _update_job_run_status_async(session_context, task.id, result)
    await _update_job_run_status_async(session_context, task.id, result)

    await db_session.refresh(job)
    await db_session.refresh(run)
    notification_count = await db_session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.entity_id == entity_id,
            Notification.user_id == user_id,
            Notification.type == "automation_paused",
        )
    )

    assert run.status == "error"
    assert run.error == "provider timed out"
    assert job.last_status == "error"
    assert job.consecutive_errors == 3
    assert job.enabled is False
    assert notification_count == 1


@pytest.mark.asyncio
async def test_dispatch_exhaustion_persists_one_terminal_occurrence(
    db_session,
    monkeypatch,
):
    from sqlalchemy import func, select

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import _record_dispatch_exhaustion

    now = datetime.now(timezone.utc)
    occurrence_key = f"at:{now.isoformat()}"
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"dispatch-exhaustion:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="One-shot dispatch exhaustion",
        job_type="at",
        schedule_kind="at",
        run_at=now.isoformat(),
        timezone="UTC",
        execution_type="agent",
        enabled=True,
        last_run_at=now,
        last_status="dispatched",
        consecutive_errors=0,
    )
    db_session.add(job)
    await db_session.commit()
    job_db_id = job.id
    job_id = job.job_id

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr(
        db_module,
        "create_worker_session",
        lambda: session_context,
    )

    kwargs = {
        "job_db_id": job_db_id,
        "now_iso": now.isoformat(),
        "manual": False,
        "occurrence_key": occurrence_key,
        "error": "worker database unavailable",
    }
    await _record_dispatch_exhaustion(**kwargs)
    await _record_dispatch_exhaustion(**kwargs)

    db_session.expire_all()
    run = (
        await db_session.execute(
            select(ScheduledJobRun).where(
                ScheduledJobRun.job_id == job_id,
                ScheduledJobRun.idempotency_key == occurrence_key,
            )
        )
    ).scalar_one()
    run_count = await db_session.scalar(
        select(func.count())
        .select_from(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id)
    )
    persisted_job = await db_session.get(ScheduledJob, job_db_id)

    assert run_count == 1
    assert run.status == "error"
    assert run.error == "worker database unavailable"
    assert run.completed_at is not None
    assert persisted_job is not None
    assert persisted_job.last_status == "error"
    assert persisted_job.consecutive_errors == 1


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
async def test_concurrent_same_prompt_update_queues_one_skill_generation(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.tasks import ai_tasks

    headers = await _auth(client, "scheduler-single-flight")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "single-flight-skill",
            "name": "Single flight Skill",
            "agent_id": "legacy-agent-label",
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    queued: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.generate_job_skill,
        "delay",
        lambda *args: queued.append(args),
    )

    first, second = await asyncio.gather(
        client.put(
            f"/api/v1/jobs/{job_id}",
            headers=headers,
            json={"payload_message": "Prepare the daily report."},
        ),
        client.put(
            f"/api/v1/jobs/{job_id}",
            headers=headers,
            json={"payload_message": "Prepare the daily report."},
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(queued) == 1
    assert queued[0][0] == job_id
    assert queued[0][1] == "Prepare the daily report."


@pytest.mark.asyncio
async def test_adding_agent_to_prompted_job_queues_skill_generation(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.tasks import ai_tasks

    headers = await _auth(client, "scheduler-agent-generation")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "agent-added-generation",
            "name": "Agent added generation",
            "payload_message": "Prepare the daily report.",
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    queued: list[tuple] = []
    monkeypatch.setattr(
        ai_tasks.generate_job_skill,
        "delay",
        lambda *args: queued.append(args),
    )

    response = await client.put(
        f"/api/v1/jobs/{job_id}",
        headers=headers,
        json={"agent_id": "legacy-agent-label"},
    )

    assert response.status_code == 200
    assert len(queued) == 1
    assert queued[0][0] == job_id
    assert queued[0][1] == "Prepare the daily report."
    assert queued[0][3] >= 2


@pytest.mark.asyncio
async def test_changing_one_shot_to_cron_invalidates_queued_occurrence(db_session):
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import update_scheduled_job

    dispatched_at = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"rescheduled-one-shot:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Rescheduled one-shot",
        schedule_kind="at",
        run_at=dispatched_at.isoformat(),
        timezone="UTC",
        enabled=True,
        last_run_at=dispatched_at,
        last_status="dispatched",
    )
    db_session.add(job)
    await db_session.commit()

    updated = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        schedule_kind="cron",
        cron_expr="* * * * *",
    )

    assert updated is not None
    assert updated.schedule_kind == "cron"
    assert updated.last_run_at is None
    assert updated.last_status is None


@pytest.mark.asyncio
async def test_scheduled_job_config_update_bumps_revision_once(db_session):
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import update_scheduled_job

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"revisioned-schedule:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Revisioned schedule",
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        enabled=True,
        revision=3,
    )
    db_session.add(job)
    await db_session.commit()

    updated = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        cron_expr="30 9 * * *",
    )
    assert updated is not None
    assert updated.revision == 4

    unchanged = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        cron_expr="30 9 * * *",
    )
    assert unchanged is not None
    assert unchanged.revision == 4


@pytest.mark.asyncio
async def test_config_update_requeues_unclaimed_dispatched_occurrence(db_session):
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import update_scheduled_job

    dispatched_at = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"unclaimed-config-update:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Unclaimed config update",
        schedule_kind="at",
        run_at=dispatched_at.isoformat(),
        payload_message="old payload",
        enabled=True,
        last_run_at=dispatched_at,
        last_status="dispatched",
        revision=2,
    )
    db_session.add(job)
    await db_session.commit()

    updated = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        payload_message="new payload",
    )

    assert updated is not None
    assert updated.revision == 3
    assert updated.last_run_at is None
    assert updated.last_status is None


@pytest.mark.asyncio
async def test_config_update_preserves_admitted_occurrence_clock(db_session):
    from packages.core.constants.execution import ScheduledChildExecutionState
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import update_scheduled_job

    dispatched_at = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"admitted-config-update:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Admitted config update",
        schedule_kind="at",
        run_at=dispatched_at.isoformat(),
        payload_message="old payload",
        enabled=True,
        last_run_at=dispatched_at,
        last_status="dispatched",
        revision=2,
    )
    db_session.add_all([
        job,
        ScheduledJobRun(
            id=generate_ulid(),
            job_id=job.job_id,
            status="running",
            started_at=dispatched_at,
            result={
                "scheduled_execution_state": (
                    ScheduledChildExecutionState.ADMITTED.value
                )
            },
        ),
    ])
    await db_session.commit()

    updated = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        payload_message="new payload",
    )

    assert updated is not None
    assert updated.revision == 3
    assert updated.last_run_at == dispatched_at
    assert updated.last_status == "dispatched"


@pytest.mark.asyncio
async def test_config_update_requeues_existing_unadmitted_occurrence(db_session):
    from packages.core.constants.execution import ScheduledDispatchState
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import update_scheduled_job

    dispatched_at = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"unadmitted-config-update:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Unadmitted config update",
        schedule_kind="at",
        run_at=dispatched_at.isoformat(),
        payload_message="old payload",
        enabled=True,
        last_run_at=dispatched_at,
        last_status="dispatched",
        revision=2,
    )
    db_session.add_all([
        job,
        ScheduledJobRun(
            id=generate_ulid(),
            job_id=job.job_id,
            status="running",
            started_at=dispatched_at,
            result={"dispatch_status": ScheduledDispatchState.PUBLISHED.value},
        ),
    ])
    await db_session.commit()

    updated = await update_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        payload_message="new payload",
    )

    assert updated is not None
    assert updated.revision == 3
    assert updated.last_run_at is None
    assert updated.last_status is None


@pytest.mark.asyncio
async def test_prompted_job_persists_skill_generation_intent(db_session):
    from packages.core.services.scheduler_service import create_scheduled_job

    before = datetime.now(timezone.utc)
    job = await create_scheduled_job(
        db_session,
        generate_ulid(),
        f"skill-intent:{generate_ulid()}",
        "Skill intent",
        schedule_kind="every",
        every_seconds=3600,
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
    )

    assert job.skill_generation_revision == job.revision
    assert job.skill_generation_next_attempt_at >= before


@pytest.mark.asyncio
async def test_skill_generation_intent_pauses_and_resumes_with_job(db_session):
    from packages.core.services.scheduler_service import (
        create_scheduled_job,
        toggle_scheduled_job,
    )

    job = await create_scheduled_job(
        db_session,
        generate_ulid(),
        f"skill-intent-pause:{generate_ulid()}",
        "Paused Skill intent",
        schedule_kind="every",
        every_seconds=3600,
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
    )
    job.skill_generation_attempts = 1
    await db_session.commit()

    paused = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        False,
    )
    assert paused is not None
    assert paused.enabled is False
    assert paused.skill_generation_revision == paused.revision
    assert paused.skill_generation_next_attempt_at is None
    assert paused.skill_generation_attempts == 1

    resumed = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        True,
    )
    assert resumed is not None
    assert resumed.enabled is True
    assert resumed.skill_generation_revision == resumed.revision
    assert resumed.skill_generation_next_attempt_at is not None
    assert resumed.skill_generation_attempts == 1


@pytest.mark.asyncio
async def test_reenable_restarts_exhausted_skill_generation(db_session):
    from packages.core.constants.execution import (
        SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS,
    )
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import toggle_scheduled_job

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"skill-intent-retry:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Failed Skill intent",
        payload_message="Prepare a report",
        agent_id="legacy-agent-label",
        enabled=True,
        skill_generation_attempts=(
            SCHEDULED_JOB_SKILL_GENERATION_MAX_ATTEMPTS
        ),
        skill_generation_last_error="provider unavailable",
    )
    db_session.add(job)
    await db_session.commit()

    disabled = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        False,
    )
    assert disabled is not None
    resumed = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        True,
    )

    assert resumed is not None
    assert resumed.skill_generation_revision == resumed.revision
    assert resumed.skill_generation_next_attempt_at is not None
    assert resumed.skill_generation_attempts == 0
    assert resumed.skill_generation_last_error is None


@pytest.mark.asyncio
async def test_reenabling_completed_delete_after_job_creates_new_occurrence(
    db_session,
):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import (
        claim_job_run,
        toggle_scheduled_job,
    )
    from packages.core.tasks.scheduler_tasks import _scheduled_occurrence_key

    first_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"repeat-delete-after:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Repeat delete after",
        schedule_kind="cron",
        cron_expr="* * * * *",
        delete_after_run=True,
        enabled=False,
        last_run_at=first_run_at,
        last_status="success",
        revision=7,
    )
    old_key = _scheduled_occurrence_key(job, first_run_at)
    db_session.add_all([
        job,
        ScheduledJobRun(
            id=generate_ulid(),
            job_id=job.job_id,
            idempotency_key=old_key,
            status="success",
            started_at=first_run_at,
            completed_at=first_run_at,
        ),
    ])
    await db_session.commit()

    resumed = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        True,
    )

    assert resumed is not None
    assert resumed.enabled is True
    assert resumed.last_run_at is None
    assert resumed.revision == 8
    next_key = _scheduled_occurrence_key(
        resumed,
        datetime.now(timezone.utc),
    )
    assert next_key != old_key
    _next_run, claimed = await claim_job_run(
        db_session,
        resumed.job_id,
        status="running",
        idempotency_key=next_key,
        trigger_type="cron",
        started_at=datetime.now(timezone.utc),
    )
    assert claimed is True


@pytest.mark.asyncio
async def test_tick_dispatch_clock_does_not_overwrite_rescheduled_one_shot(
    db_session,
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import update_scheduled_job
    from packages.core.tasks.scheduler_tasks import (
        _mark_scheduled_job_dispatched_if_current,
    )

    dispatched_at = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"tick-reschedule-race:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Tick reschedule race",
        schedule_kind="at",
        run_at=dispatched_at.isoformat(),
        timezone="UTC",
        enabled=True,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id
    entity_id = job.entity_id
    replacement_run_at = (dispatched_at + timedelta(days=1)).isoformat()
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessions() as tick_db:
        stale_job = (
            await tick_db.execute(
                select(ScheduledJob).where(ScheduledJob.id == job_id)
            )
        ).scalar_one()
        async with sessions() as update_db:
            updated = await update_scheduled_job(
                update_db,
                job_id,
                entity_id,
                run_at=replacement_run_at,
            )
            assert updated is not None
            await update_db.commit()

        claimed = await _mark_scheduled_job_dispatched_if_current(
            tick_db,
            stale_job,
            dispatched_at,
        )
        await tick_db.commit()

    db_session.expire_all()
    persisted = await db_session.get(ScheduledJob, job_id)
    assert claimed is False
    assert persisted.run_at == replacement_run_at
    assert persisted.last_run_at is None
    assert persisted.last_status is None


@pytest.mark.asyncio
async def test_scheduler_parent_claim_waits_for_locked_due_job(
    db_session,
    monkeypatch,
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.scheduler_tasks import (
        _prepare_scheduler_parent_dispatch,
    )

    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"locked-due-job:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Locked due job",
        schedule_kind="cron",
        cron_expr="* * * * *",
        enabled=True,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id
    durable_job_id = job.job_id
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(db_module, "create_worker_session", lambda: sessions)

    async with sessions() as locker:
        await locker.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job_id)
            .with_for_update()
        )
        prepare_task = asyncio.create_task(
            _prepare_scheduler_parent_dispatch(job_id, now)
        )
        await asyncio.sleep(0.05)
        assert not prepare_task.done()
        await locker.commit()

    prepared = await asyncio.wait_for(prepare_task, timeout=2)
    assert prepared is not None
    dispatch, run_id = prepared
    assert dispatch["kind"] == "scheduler_parent"

    db_session.expire_all()
    persisted_job = await db_session.get(ScheduledJob, job_id)
    run = await db_session.get(ScheduledJobRun, run_id)
    assert persisted_job.last_run_at == now
    assert persisted_job.last_status == "dispatched"
    assert run.job_id == durable_job_id


@pytest.mark.asyncio
async def test_workspace_purge_wins_before_scheduler_parent_claim(
    db_session,
    monkeypatch,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_service import purge_workspace
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_lifecycle,
    )
    from packages.core.tasks.scheduler_tasks import (
        _prepare_scheduler_parent_dispatch,
    )

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Purge before scheduler claim",
        settings={},
        operating_model={},
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"purged-due-job:{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Purged due job",
        schedule_kind="cron",
        cron_expr="* * * * *",
        enabled=True,
    )
    db_session.add_all([workspace, job])
    await db_session.commit()
    workspace_id = workspace.id
    job_id = job.id
    durable_job_id = job.job_id
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(db_module, "create_worker_session", lambda: sessions)

    async with sessions() as purge_db:
        await lock_reusable_resource_lifecycle(purge_db, entity_id=entity_id)
        prepare_task = asyncio.create_task(
            _prepare_scheduler_parent_dispatch(job_id, datetime.now(timezone.utc))
        )
        await asyncio.sleep(0.05)
        assert not prepare_task.done()
        assert await purge_workspace(purge_db, workspace_id) is True
        await purge_db.commit()

    assert await asyncio.wait_for(prepare_task, timeout=2) is None
    db_session.expire_all()
    assert await db_session.get(ScheduledJob, job_id) is None
    run_count = await db_session.scalar(
        select(func.count())
        .select_from(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == durable_job_id)
    )
    assert run_count == 0


@pytest.mark.asyncio
async def test_toggle_scheduled_job(client: AsyncClient, db_session):
    from sqlalchemy import update

    from packages.core.models.scheduler import ScheduledJob

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

    # Simulate the failure streak that caused an automatic pause. Explicitly
    # resuming should grant a fresh failure budget.
    await db_session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id)
        .values(consecutive_errors=3)
    )
    await db_session.commit()

    # Re-enable
    resp2 = await client.post(
        f"/api/v1/jobs/{job_id}/toggle",
        headers=headers,
        json={
            "enabled": True,
        },
    )
    assert resp2.json()["enabled"] is True
    assert resp2.json()["consecutive_errors"] == 0


@pytest.mark.asyncio
async def test_job_api_mutations_audit_the_authenticated_user(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.automation_revision import AutomationRevision
    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, username="scheduler-audit-user")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": f"audit-job:{generate_ulid()}",
            "name": "Audit me",
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/jobs/{job_id}",
        headers=headers,
        json={"payload_message": "Audited update"},
    )
    assert updated.status_code == 200
    toggled = await client.post(
        f"/api/v1/jobs/{job_id}/toggle",
        headers=headers,
        json={"enabled": False},
    )
    assert toggled.status_code == 200

    job = await db_session.get(ScheduledJob, job_id)
    assert job is not None
    audits = list((await db_session.execute(
        select(AutomationRevision)
        .where(AutomationRevision.target_id == job_id)
        .order_by(AutomationRevision.revision.asc())
    )).scalars())
    assert [audit.patch for audit in audits] == [
        {"payload_message": "Audited update"},
        {"enabled": False},
    ]
    assert {audit.changed_by_kind for audit in audits} == {"user"}
    assert {audit.changed_by_id for audit in audits} == {job.user_id}


@pytest.mark.asyncio
async def test_resuming_delete_after_run_restores_due_state(db_session):
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import toggle_scheduled_job

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"resume-delete-after:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Resume delete-after",
        schedule_kind="cron",
        cron_expr="* * * * *",
        delete_after_run=True,
        enabled=False,
        last_run_at=datetime.now(timezone.utc),
        last_status="dispatched",
    )
    db_session.add(job)
    await db_session.commit()

    resumed = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        True,
    )
    await db_session.commit()

    assert resumed is not None
    assert resumed.last_run_at is None

    terminal_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="completed",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(terminal_run)
    resumed.enabled = False
    resumed.last_run_at = datetime.now(timezone.utc)
    resumed.last_status = "completed"
    await db_session.commit()

    resumed_again = await toggle_scheduled_job(
        db_session,
        job.id,
        job.entity_id,
        True,
    )

    assert resumed_again is not None
    assert resumed_again.last_run_at is None


@pytest.mark.asyncio
async def test_delete_scheduled_job_removes_run_history(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.scheduler import ScheduledJobRun

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
    job_run_id = generate_ulid()
    job_run = ScheduledJobRun(
        id=job_run_id,
        job_id=create.json()["job_id"],
        status="completed",
    )
    db_session.add(job_run)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 204

    # Verify gone
    resp2 = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert resp2.status_code == 404
    db_session.expire_all()
    assert await db_session.get(ScheduledJobRun, job_run_id) is None


@pytest.mark.asyncio
async def test_delete_scheduled_job_serializes_with_queued_dispatch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    """A queued worker cannot write a JobRun after its Job is deleted."""
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import (
        claim_job_run,
        delete_scheduled_job,
    )
    import packages.core.tasks.scheduler_tasks as scheduler_tasks

    headers = await _auth(client, "sched_delete_race")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"job_id": "delete-race", "name": "Delete race"},
    )
    assert created.status_code == 201
    job = await db_session.get(ScheduledJob, created.json()["id"])
    assert job is not None

    worker_loaded = asyncio.Event()
    allow_worker_finish = asyncio.Event()
    claimed_run_id: str | None = None

    async def claim_after_delete_attempt(db, queued_job, now, **kwargs):
        nonlocal claimed_run_id
        worker_loaded.set()
        await allow_worker_finish.wait()
        run, claimed = await claim_job_run(
            db,
            queued_job.job_id,
            status="running",
            idempotency_key="delete-race:occurrence",
            trigger_type="manual",
            started_at=now,
        )
        assert claimed is True
        claimed_run_id = run.id

    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", claim_after_delete_attempt)
    worker = asyncio.create_task(
        scheduler_tasks._async_dispatch_single(
            job.id,
            datetime.now(timezone.utc).isoformat(),
            manual=True,
            occurrence_key="delete-race:occurrence",
        )
    )
    await asyncio.wait_for(worker_loaded.wait(), timeout=3)
    deletion = asyncio.create_task(
        delete_scheduled_job(db_session, job.id, job.entity_id)
    )

    try:
        await asyncio.sleep(0.05)
        assert not deletion.done(), "deletion must wait for the queued worker lock"
    finally:
        allow_worker_finish.set()

    await worker
    assert await deletion is True
    await db_session.commit()
    db_session.expire_all()
    assert claimed_run_id is not None
    assert await db_session.get(ScheduledJob, job.id) is None
    assert await db_session.get(ScheduledJobRun, claimed_run_id) is None


@pytest.mark.asyncio
async def test_queued_dispatch_rechecks_disabled_job(
    db_session,
    monkeypatch,
):
    """A pause committed after enqueue suppresses the queued occurrence."""
    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    import packages.core.tasks.scheduler_tasks as scheduler_tasks

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"paused-queue:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Paused queued job",
        enabled=False,
    )
    db_session.add(job)
    await db_session.commit()

    async def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("disabled queued job must not dispatch")

    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", unexpected_dispatch)
    await scheduler_tasks._async_dispatch_single(
        job.id,
        datetime.now(timezone.utc).isoformat(),
        occurrence_key="paused-queue:occurrence",
    )

    runs = (await db_session.execute(
        select(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id)
    )).scalars().all()
    assert runs == []


@pytest.mark.asyncio
async def test_workspace_delete_fences_queued_dispatch(
    db_session,
    monkeypatch,
):
    """A delete that owns the lifecycle fence prevents a later occurrence."""
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("transaction advisory-lock regression requires PostgreSQL")

    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_service import soft_delete_workspace
    from packages.core.services import reusable_resource_locks, scheduler_service
    import packages.core.tasks.scheduler_tasks as scheduler_tasks

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Delete dispatch fence",
        status="active",
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"workspace-delete:{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Workspace delete race",
        enabled=True,
    )
    db_session.add_all([workspace, job])
    await db_session.commit()
    workspace_id = workspace.id
    job_pk = job.id
    job_public_id = job.job_id

    delete_has_lifecycle = asyncio.Event()
    allow_delete = asyncio.Event()
    worker_lock_attempted = asyncio.Event()
    dispatched = False
    original_delete = scheduler_service.delete_workspace_automations
    original_lifecycle_lease = (
        reusable_resource_locks.reusable_resource_lifecycle_lease
    )

    async def pause_after_delete_fence(db, workspace_id, scoped_entity_id):
        delete_has_lifecycle.set()
        await allow_delete.wait()
        return await original_delete(db, workspace_id, scoped_entity_id)

    @asynccontextmanager
    async def signal_worker_lifecycle_lease(db, *, entity_id):
        worker_lock_attempted.set()
        async with original_lifecycle_lease(db, entity_id=entity_id):
            yield

    async def unexpected_dispatch(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("deleted workspace job must not dispatch")

    monkeypatch.setattr(
        scheduler_service,
        "delete_workspace_automations",
        pause_after_delete_fence,
    )
    monkeypatch.setattr(
        reusable_resource_locks,
        "reusable_resource_lifecycle_lease",
        signal_worker_lifecycle_lease,
    )
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", unexpected_dispatch)

    async def delete_workspace() -> bool:
        async with db_module.async_session() as deleting_db:
            deleted = await soft_delete_workspace(
                deleting_db,
                workspace_id,
                entity_id,
            )
            await deleting_db.commit()
            return deleted

    deletion = asyncio.create_task(delete_workspace())
    await asyncio.wait_for(delete_has_lifecycle.wait(), timeout=3)
    worker = asyncio.create_task(scheduler_tasks._async_dispatch_single(
        job_pk,
        datetime.now(timezone.utc).isoformat(),
        occurrence_key="workspace-delete:occurrence",
    ))
    await asyncio.wait_for(worker_lock_attempted.wait(), timeout=3)
    assert not worker.done()

    allow_delete.set()
    assert await deletion is True
    await asyncio.wait_for(worker, timeout=3)

    db_session.expire_all()
    assert dispatched is False
    assert await db_session.get(ScheduledJob, job_pk) is None
    assert (await db_session.execute(
        select(ScheduledJobRun).where(ScheduledJobRun.job_id == job_public_id)
    )).scalars().all() == []


@pytest.mark.asyncio
async def test_same_entity_jobs_dispatch_after_narrow_row_locks(
    db_session,
    monkeypatch,
):
    """The lifecycle fence must not serialize unrelated job execution."""
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("transaction advisory-lock regression requires PostgreSQL")

    from packages.core.models.scheduler import ScheduledJob
    import packages.core.tasks.scheduler_tasks as scheduler_tasks

    entity_id = generate_ulid()
    jobs = [
        ScheduledJob(
            id=generate_ulid(),
            job_id=f"parallel-lifecycle:{generate_ulid()}",
            entity_id=entity_id,
            name=f"Parallel job {index}",
            enabled=True,
        )
        for index in range(2)
    ]
    db_session.add_all(jobs)
    await db_session.commit()

    both_entered = asyncio.Event()
    entered: set[str] = set()

    async def aligned_dispatch(_db, job, *_args, **_kwargs):
        entered.add(job.id)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=3)
        return None

    monkeypatch.setattr(scheduler_tasks, "_dispatch_job", aligned_dispatch)
    now_iso = datetime.now(timezone.utc).isoformat()
    await asyncio.wait_for(
        asyncio.gather(*(
            scheduler_tasks._async_dispatch_single(
                job.id,
                now_iso,
                occurrence_key=f"parallel:{job.id}",
            )
            for job in jobs
        )),
        timeout=5,
    )

    assert entered == {job.id for job in jobs}


@pytest.mark.asyncio
async def test_scheduled_child_gate_requires_enabled_parent(db_session):
    from sqlalchemy import delete

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _scheduled_run_is_open

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"child-gate:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Child lifecycle gate",
        enabled=False,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
    )
    db_session.add_all([job, run])
    await db_session.commit()

    assert await _scheduled_run_is_open(run.id, job.job_id) is False

    job.enabled = True
    await db_session.commit()
    assert await _scheduled_run_is_open(run.id, job.job_id) is True

    # A stale occurrence row alone cannot authorize work after its parent was
    # removed by another lifecycle path.
    await db_session.execute(delete(ScheduledJob).where(ScheduledJob.id == job.id))
    await db_session.commit()
    assert await _scheduled_run_is_open(run.id, job.job_id) is False


@pytest.mark.asyncio
async def test_scheduled_child_gate_waits_for_uncommitted_pause(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.tasks.ai_tasks import _scheduled_run_is_open

    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("row-lock admission regression requires PostgreSQL")

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"child-gate-pause:{generate_ulid()}",
        entity_id=generate_ulid(),
        name="Child pause race",
        enabled=True,
    )
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
    )
    db_session.add_all([job, run])
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with sessions() as pausing_db:
        locked_job = (await pausing_db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.id == job.id)
            .with_for_update()
        )).scalar_one()
        locked_job.enabled = False
        await pausing_db.flush()

        admission = asyncio.create_task(
            _scheduled_run_is_open(run.id, job.job_id)
        )
        await asyncio.sleep(0.1)
        assert not admission.done()

        await pausing_db.commit()
        assert await asyncio.wait_for(admission, timeout=3) is False


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
async def test_scheduled_skill_dispatch_binds_full_skill_to_child_task(
    db_session,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.skill import Skill
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.tasks import scheduler_tasks

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Scheduled Skill Workspace",
        kind="workspace",
        status="active",
        operating_model={},
        settings={},
    )
    skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Packaged report",
        slug=f"packaged-report-{generate_ulid()}",
        system_prompt="Run the complete packaged report.",
        tools=["sandbox_exec"],
        config={"type": "sandbox", "scripts": {"run.py": "print('ok')"}},
        status="active",
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"scheduled-skill:{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Scheduled packaged report",
        execution_type="skill",
        execution_target={"skill_id": skill.id, "complexity": "worker"},
        execution_script="stale copied prompt",
        payload_message="Prepare the report.",
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        enabled=True,
    )
    db_session.add_all([workspace, skill, job])
    await db_session.commit()

    async def allow_credits(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        scheduler_tasks,
        "_preflight_scheduled_job_credits",
        allow_credits,
    )

    dispatch, run_id = await scheduler_tasks._dispatch_job(
        db_session,
        job,
        datetime.now(timezone.utc),
        manual=True,
        occurrence_key=f"manual:{generate_ulid()}",
    )

    task = (await db_session.execute(select(Task).where(
        Task.id == dispatch["args"][0]
    ))).scalar_one()
    assert dispatch["kind"] == "agent_task"
    assert dispatch["kwargs"]["scheduled_run_id"] == run_id
    assert task.description == "Prepare the report."
    assert task.details["scheduled_skill_id"] == skill.id
    assert task.details["execution_script"] is None


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
async def test_run_now_rejects_enabled_job_in_paused_workspace(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Workspace

    headers = await _auth(client, "sched_run_now_paused")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Paused run now workspace"},
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "paused-run-now-job",
            "name": "Paused run now job",
            "workspace_id": workspace_id,
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    paused = await client.post(f"/api/v1/workspaces/{workspace_id}/pause", headers=headers)
    assert paused.status_code == 200
    job = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.id == job_id)
    )).scalar_one()
    job.enabled = True
    await db_session.commit()

    class _UnexpectedDispatch:
        def delay(self, *args, **kwargs):
            raise AssertionError("paused workspace reached the scheduler queue")

    import packages.core.tasks.scheduler_tasks as scheduler_tasks
    monkeypatch.setattr(scheduler_tasks, "_dispatch_job_task", _UnexpectedDispatch())

    response = await client.post(f"/api/v1/jobs/{job_id}/run_now", headers=headers)

    assert response.status_code == 409
    assert "Workspace is not active" in response.json()["detail"]
    workspace_row = (await db_session.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one()
    assert workspace_row.status == "paused"
    listed = await client.get(
        f"/api/v1/jobs?workspace_id={workspace_id}",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["workspace_status"] == "paused"
    assert listed.json()["items"][0]["enabled"] is False
    assert listed.json()["enabled_total"] == 0


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
async def test_workspace_job_detail_projects_viewer_write_capability(client: AsyncClient):
    """The job detail must not advertise controls a Workspace viewer cannot use."""
    from tests.test_document_permissions import _create_entity_user
    from tests.test_workspace_write_authz import _add_member, _make_workspace

    owner_headers = await _auth(client, "sched_detail_capability_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace_id = await _make_workspace(
        me["entity_id"], "Detail Capability", me.get("user_id") or me.get("id")
    )
    created = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_id": "detail-capability-job",
            "name": "Detail capability job",
            "workspace_id": workspace_id,
        },
    )
    assert created.status_code == 201, created.text
    viewer = await _create_entity_user(
        me["entity_id"], "sched_detail_capability_viewer", role="member"
    )
    await _add_member(workspace_id, viewer["id"], "viewer")

    detail = await client.get(f"/api/v1/jobs/{created.json()['id']}", headers=viewer["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_manage"] is False


@pytest.mark.asyncio
async def test_global_job_list_hides_jobs_bound_to_soft_deleted_workspace(
    client: AsyncClient,
):
    """Soft deletion stops Workspace automation visibility without hiding
    unrelated Entity-level automation rows."""
    from sqlalchemy import update

    from packages.core.models.workspace import Workspace
    from tests.test_workspace_write_authz import _make_workspace

    owner_headers = await _auth(client, "sched_deleted_workspace_list_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace_id = await _make_workspace(
        me["entity_id"], "Deleted Scheduler Workspace", me.get("user_id") or me.get("id")
    )
    entity_job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={"job_id": "entity-level-after-delete", "name": "Entity level"},
    )
    workspace_job = await client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "job_id": "workspace-after-delete",
            "name": "Workspace automation",
            "workspace_id": workspace_id,
        },
    )
    assert entity_job.status_code == 201, entity_job.text
    assert workspace_job.status_code == 201, workspace_job.text

    async with db_module.async_session() as db:
        await db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await db.commit()

    listed = await client.get("/api/v1/jobs", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    job_ids = {item["job_id"] for item in listed.json()["items"]}
    assert "entity-level-after-delete" in job_ids
    assert "workspace-after-delete" not in job_ids

    scoped = await client.get(
        "/api/v1/jobs",
        headers=owner_headers,
        params={"workspace_id": workspace_id},
    )
    assert scoped.status_code == 404, scoped.text


@pytest.mark.asyncio
async def test_scheduler_rejects_workspace_id_from_another_entity(client: AsyncClient):
    """A foreign persisted Workspace id cannot use legacy-label compatibility."""
    from tests.test_workspace_write_authz import _make_workspace

    foreign_headers = await _auth(client, "foreign_scheduler_workspace_owner")
    foreign_me = (await client.get("/api/v1/auth/me", headers=foreign_headers)).json()
    foreign_workspace_id = await _make_workspace(
        foreign_me["entity_id"],
        "Foreign scheduler target",
        foreign_me.get("user_id") or foreign_me.get("id"),
    )
    local_headers = await _auth(client, "local_scheduler_workspace_owner")
    created = await client.post(
        "/api/v1/jobs",
        headers=local_headers,
        json={
            "job_id": "foreign-workspace-scheduler-job",
            "name": "Foreign workspace job",
            "workspace_id": foreign_workspace_id,
        },
    )
    assert created.status_code == 403, created.text


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
