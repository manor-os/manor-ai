"""Setup can use the normal Task runtime only through its durable job receipt."""

from copy import deepcopy

import pytest

from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.blueprint_startup_service import blueprint_startup_contract_fingerprint
from packages.core.services.task_execution import resolve_task_execution
from packages.core.services.workspace_readiness import (
    evaluate_workspace_automatic_setup_status,
    task_is_authorized_workspace_setup,
)


async def _setup(db):
    entity_id, workspace_id, agent_id = generate_ulid(), generate_ulid(), generate_ulid()
    contract = {
        "checks": [
            {
                "key": "identity",
                "kind": "workspace_identity_assets",
                "setup_job_id": "prepare-assets",
                "asset_key": "character",
            }
        ]
    }
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Setup test",
        status="active",
        settings={
            "blocking_setup": contract,
            "_blueprint": {
                "blueprint_id": "builtin:setup-test",
                "live_setup_requirements": [],
                "portable_setting_keys": ["blocking_setup"],
                "runtime_contract_fingerprints": {"blocking_setup": blueprint_startup_contract_fingerprint(contract)},
            },
        },
    )
    sub = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        service_key="production",
        status="active",
    )
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=installed_blueprint_job_id("prepare-assets", workspace_id),
        entity_id=entity_id,
        workspace_id=workspace_id,
        job_type="manual",
        enabled=True,
        execution_type="agent",
        agent_id=agent_id,
        revision=1,
        execution_target={"agent_subscription_id": sub.id, "service_key": sub.service_key},
    )
    run = ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running")
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Prepare shared assets",
        status="in_progress",
        agent_id=agent_id,
        owner_subscription_id=sub.id,
        details={"scheduled_job_id": job.job_id, "scheduled_run_id": run.id},
    )
    run.result = {
        "scheduled_child_kind": "agent_task",
        "scheduled_child_id": task.id,
        "dispatch": {
            "kind": "agent_task",
            "args": [task.id, agent_id],
            "kwargs": {"scheduled_job_id": job.job_id, "scheduled_run_id": run.id},
        },
        "dispatch_ledger": {"revision": job.revision},
    }
    db.add_all([workspace, sub, job, run, task])
    await db.flush()
    return workspace, sub, job, run, task


@pytest.mark.asyncio
async def test_setup_child_keeps_coordinated_runtime_and_setup_authority(db_session):
    _workspace, _sub, _job, _run, task = await _setup(db_session)
    route = await resolve_task_execution(db_session, task)
    assert route.runtime_kind.value == "coordinated_plan"
    assert await task_is_authorized_workspace_setup(db_session, task)


@pytest.mark.asyncio
async def test_copied_job_markers_do_not_authorize_an_ordinary_task(db_session):
    workspace, _sub, _job, _run, setup_task = await _setup(db_session)
    ordinary = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Ordinary work",
        status="in_progress",
        agent_id=setup_task.agent_id,
        owner_subscription_id=setup_task.owner_subscription_id,
        details=deepcopy(setup_task.details),
    )
    db_session.add(ordinary)
    await db_session.flush()
    assert not await task_is_authorized_workspace_setup(db_session, ordinary)


@pytest.mark.asyncio
async def test_setup_the_person_asked_for_runs_while_the_workspace_is_paused(db_session):
    workspace, _sub, _job, run, task = await _setup(db_session)
    workspace.status = "paused"
    run.trigger_type = "manual"
    await db_session.flush()
    assert await task_is_authorized_workspace_setup(db_session, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    [
        "job_disabled",
        "revision_changed",
        "run_cancelled",
        "workspace_paused",
        "workspace_deleted",
        "contract_modified",
        "subscription_paused",
        "foreign_job",
        "foreign_agent",
        "forged_child",
        "malformed_dispatch",
        "malformed_kwargs",
        "malformed_ledger",
    ],
)
async def test_setup_authority_is_revalidated_on_every_gate(db_session, revocation):
    workspace, sub, job, run, task = await _setup(db_session)
    assert await task_is_authorized_workspace_setup(db_session, task)
    if revocation == "job_disabled":
        job.enabled = False
    elif revocation == "revision_changed":
        job.revision += 1
    elif revocation == "run_cancelled":
        run.status = "cancelled"
    elif revocation == "workspace_paused":
        workspace.status = "paused"
    elif revocation == "workspace_deleted":
        from datetime import datetime, timezone

        workspace.deleted_at = datetime.now(timezone.utc)
    elif revocation == "contract_modified":
        changed = deepcopy(workspace.settings)
        changed["blocking_setup"]["checks"][0]["setup_job_id"] = "normal-work"
        workspace.settings = changed
    elif revocation == "subscription_paused":
        sub.status = "paused"
    elif revocation == "foreign_job":
        job.workspace_id = generate_ulid()
    elif revocation == "foreign_agent":
        task.agent_id = generate_ulid()
    elif revocation == "forged_child":
        run.result = {**run.result, "scheduled_child_id": generate_ulid()}
    elif revocation == "malformed_dispatch":
        run.result = {**run.result, "dispatch": "invalid"}
    elif revocation == "malformed_kwargs":
        run.result = {**run.result, "dispatch": {**run.result["dispatch"], "kwargs": "invalid"}}
    elif revocation == "malformed_ledger":
        run.result = {**run.result, "dispatch_ledger": "invalid"}
    await db_session.flush()
    assert not await task_is_authorized_workspace_setup(db_session, task)


@pytest.mark.asyncio
async def test_automatic_setup_uses_actual_run_and_scoped_child_when_job_summary_is_empty(db_session):
    workspace, _sub, job, run, task = await _setup(db_session)
    assert job.last_status is None
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("running", task.id)
    run.status = "error"
    await db_session.flush()
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("retrying", task.id)
    job.enabled = False
    await db_session.flush()
    assert await evaluate_workspace_automatic_setup_status(
        db_session, workspace, [{"setup_job_id": "prepare-assets"}]
    ) == ("failed", task.id)
