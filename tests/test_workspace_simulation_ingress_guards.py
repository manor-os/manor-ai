"""Simulation Workspaces must never enter ordinary execution runtimes."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.workspace import Workspace
from tests.test_document_permissions import _auth


async def _sandbox_workspace(client: AsyncClient, name: str) -> tuple[dict[str, str], str]:
    headers = await _auth(client, f"simulation_ingress_{name.lower().replace(' ', '_')}")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": name},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {**dict(workspace.settings or {}), "sandbox": True}
        workspace.heartbeat_enabled = False
        await db.commit()
    return headers, workspace_id


@pytest.mark.asyncio
async def test_simulation_workspace_rejects_normal_chat_before_message_or_dispatch(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_chat

    headers, workspace_id = await _sandbox_workspace(client, "Simulation Chat Guard")
    scheduled: list[dict] = []
    monkeypatch.setattr(
        workspace_chat,
        "_schedule_workspace_chat_processing",
        lambda **kwargs: scheduled.append(kwargs),
    )

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
        json={"body": "ordinary chat must stay outside simulation"},
    )

    assert response.status_code == 409, response.text
    assert scheduled == []


@pytest.mark.asyncio
async def test_simulation_workspace_rejects_autonomous_runtime_resume(
    client: AsyncClient,
):
    headers, workspace_id = await _sandbox_workspace(client, "Simulation Resume Guard")

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/resume",
        headers=headers,
    )

    assert response.status_code == 409, response.text
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.heartbeat_enabled is False


@pytest.mark.asyncio
async def test_simulation_workspace_rejects_normal_workflow_binding_run_before_run_creation(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers, workspace_id = await _sandbox_workspace(client, "Simulation Flow Guard")
    workflow = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Simulation guard flow",
            "steps": [{"id": "start", "type": "trigger"}],
        },
    )
    assert workflow.status_code == 201, workflow.text
    binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow.json()["id"],
            "workspace_id": workspace_id,
            "name": "Simulation guard binding",
            "trigger_type": "manual",
        },
    )
    assert binding.status_code == 201, binding.text
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(lambda *_args, **_kwargs: None))

    response = await client.post(
        f"/api/v1/workflows/bindings/{binding.json()['id']}/run",
        headers=headers,
        json={"execute": False},
    )

    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_simulation_workspace_rejects_manual_automation_before_queue_dispatch(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.tasks.scheduler_tasks import _dispatch_job_task

    headers, workspace_id = await _sandbox_workspace(client, "Simulation Automation Guard")
    created = await client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "job_id": "simulation-guard-job",
            "name": "Simulation guard job",
            "workspace_id": workspace_id,
        },
    )
    assert created.status_code == 201, created.text
    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        _dispatch_job_task,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )

    response = await client.post(
        f"/api/v1/jobs/{created.json()['id']}/run_now",
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert queued == []


@pytest.mark.asyncio
async def test_scheduler_worker_skips_simulation_before_credit_preflight_or_fanout(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.ai.llm_client import CreditExhaustedError
    import packages.core.experiments as experiments
    import packages.core.services.scheduler_service as scheduler_service
    from packages.core.tasks import scheduler_tasks

    now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id="simulation-job-run",
        status="running",
        started_at=now,
        completed_at=None,
        duration_ms=None,
        error=None,
        result=None,
    )
    workspace = SimpleNamespace(
        id="simulation-workspace",
        status="active",
        heartbeat_enabled=False,
        deleted_at=None,
        kind="content",
        settings={"sandbox": True},
    )
    job = SimpleNamespace(
        job_id="simulation-worker-guard",
        schedule_kind="every",
        every_seconds=300,
        cron_expr=None,
        run_at=None,
        delete_after_run=False,
        timezone="UTC",
        entity_id="simulation-entity",
        execution_target={},
        workspace_id=workspace.id,
        execution_type="agent",
        payload_message="must never fan out",
        execution_script=None,
        name="Simulation worker guard",
        goal_id=None,
        conversation_id=None,
        manor_task_id=None,
        agent_id=None,
        user_id=None,
        last_run_at=None,
        last_status=None,
        consecutive_errors=0,
    )
    preflight_calls = 0

    async def fake_claim(*_args, **_kwargs):
        return run, True

    async def fake_effective_config(*_args, **_kwargs):
        return {}, None, None

    async def fake_reconcile(*_args, **_kwargs):
        return False

    async def fake_preflight(*_args, **_kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        raise CreditExhaustedError("should not check simulation credits")

    class FakeRows:
        def scalar_one_or_none(self):
            return workspace

    class FakeDB:
        async def execute(self, _query):
            return FakeRows()

        async def flush(self):
            return None

    monkeypatch.setattr(scheduler_service, "claim_job_run", fake_claim)
    monkeypatch.setattr(
        scheduler_service,
        "reconcile_scheduled_job_run_projection",
        fake_reconcile,
    )
    monkeypatch.setattr(experiments, "effective_dispatch_config", fake_effective_config)
    monkeypatch.setattr(scheduler_tasks, "_preflight_scheduled_job_credits", fake_preflight)

    await scheduler_tasks._dispatch_job(FakeDB(), job, now)

    assert run.status == "skipped"
    assert run.result == {"skipped": True, "reason": "workspace_simulation"}
    assert preflight_calls == 0
