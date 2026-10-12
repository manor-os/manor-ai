"""The upgrade gate sees every open execution plane and preserves its state."""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.upgrade_activity import active_runs
from packages.core.constants.execution import ExecutionPlanStatus, PLAN_TERMINAL_STATUSES
from packages.core.constants.workflow import WorkflowRunStatus, WORKFLOW_RUN_OPEN_STATUSES
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.review_run import ReviewRun
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.scheduler import AgentExecution, ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.models.workflow import WorkflowRun
from packages.core.review import begin_review
from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle
from packages.core.services.runtime_run_service import create_runtime_run, RuntimeRunNotFoundError
from packages.core.services.workspace_access import lock_workspace_access_boundary


async def workspace_fixture(db):
    workspace = Workspace(entity_id=generate_ulid(), name="Upgrade activity")
    db.add(workspace)
    await db.flush()
    return workspace


async def test_open_plans_workflows_and_durable_chat_block_including_queued_and_paused(db_session):
    workspace = await workspace_fixture(db_session)
    expected = set()
    rows = []
    for status in ExecutionPlanStatus:
        row = ExecutionPlan(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, status=status)
        rows.append(row)
        if status not in PLAN_TERMINAL_STATUSES:
            expected.add(row.id)
    for status in WorkflowRunStatus:
        row = WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), entity_id=workspace.entity_id,
                          workspace_id=workspace.id, status=status)
        rows.append(row)
        if status in WORKFLOW_RUN_OPEN_STATUSES:
            expected.add(row.id)
    for status in RuntimeRunStatus:
        run_id = generate_ulid()
        row = RuntimeRun(id=run_id, root_run_id=run_id, conversation_id=generate_ulid(),
                         entity_id=workspace.entity_id, user_id=generate_ulid(),
                         workspace_id=workspace.id, status=status)
        rows.append(row)
        if status.value in RuntimeRunStatus.active_root():
            expected.add(row.id)
    db_session.add_all(rows)
    await db_session.flush()
    before = {row.id: getattr(row.status, "value", row.status) for row in rows}
    assert {row["id"] for row in await active_runs(db_session, workspace=workspace)} == expected
    for row in rows:
        await db_session.refresh(row)
        assert row.status == before[row.id]


async def test_manual_reviews_tasks_agents_and_scheduler_use_exact_workspace_and_job_identity(db_session):
    workspace = await workspace_fixture(db_session)
    task = Task(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                title="Video in progress", status="in_progress")
    review = ReviewRun(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                       trigger_kind="human_requested", status="running")
    agent = AgentExecution(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, status="running")
    job = ScheduledJob(id=generate_ulid(), job_id=f"portable-{generate_ulid()}", name="Render schedule",
                       entity_id=workspace.entity_id, workspace_id=workspace.id)
    run = ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running")
    terminal_run = ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="success")
    foreign_review = ReviewRun(entity_id=generate_ulid(), workspace_id=generate_ulid(),
                              trigger_kind="human_requested", status="running")
    foreign_task = Task(entity_id=workspace.entity_id, workspace_id=generate_ulid(),
                        title="Other Workspace task", status="in_progress")
    foreign_entity_task = Task(entity_id=generate_ulid(), workspace_id=workspace.id,
                               title="Foreign entity task", status="in_progress")
    db_session.add_all([task, review, agent, job, run, terminal_run, foreign_review, foreign_task, foreign_entity_task])
    await db_session.flush()
    result = await active_runs(db_session, workspace=workspace)
    assert {(row["id"], row["kind"]) for row in result} == {
        (task.id, "task"), (review.id, "review"), (agent.id, "agent"), (run.id, "scheduled_job"),
    }
    assert next(row for row in result if row["id"] == run.id)["name"] == "Render schedule"
    assert (await db_session.execute(select(ScheduledJobRun.status).where(ScheduledJobRun.id == run.id))).scalar_one() == "running"


async def test_each_activity_plane_has_a_bounded_preview(db_session):
    workspace = await workspace_fixture(db_session)
    db_session.add_all([ExecutionPlan(entity_id=workspace.entity_id, workspace_id=workspace.id,
                                      status="pending") for _ in range(105)])
    await db_session.flush()
    result = await active_runs(db_session, workspace=workspace)
    assert len(result) == 101
    assert all(row["status"] == "pending" for row in result)


@pytest.mark.parametrize("admission", ["review", "master_chat"])
@pytest.mark.parametrize("boundary", ["lifecycle", "workspace"])
async def test_execution_admission_waits_for_upgrade_before_freezing_configuration(db_session, admission, boundary):
    workspace = await workspace_fixture(db_session)
    workspace.operation_revision = 1
    await db_session.commit()
    entity_id, workspace_id = workspace.entity_id, workspace.id

    if boundary == "lifecycle":
        await lock_reusable_resource_lifecycle(db_session, entity_id=entity_id)
    else:
        await lock_workspace_access_boundary(db_session, entity_id=entity_id, workspace_id=workspace_id)
    workspace.operation_revision = 2
    await db_session.flush()

    async def admit():
        async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as execution_db:
            if admission == "review":
                row = await begin_review(execution_db, entity_id=entity_id, workspace_id=workspace_id,
                                         trigger="human_requested", lease_owner="upgrade-regression")
            else:
                row = await create_runtime_run(execution_db, entity_id=entity_id, workspace_id=workspace_id,
                                               conversation_id=generate_ulid(), user_id=generate_ulid(),
                                               agent_id="master", execution_payload={})
            await execution_db.commit()
            return row

    pending = asyncio.create_task(admit())
    try:
        # Without admission fencing this commits a receipt against configuration
        # that is being changed underneath it. A shield leaves the waiter alive.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), timeout=0.2)
        await db_session.commit()
        row = await asyncio.wait_for(pending, timeout=5)
        if admission == "review":
            assert row.workspace_revision == 2
        else:
            assert row.status == "queued"
        assert row.id in {entry["id"] for entry in await active_runs(db_session, workspace=workspace)}
    finally:
        await db_session.rollback()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("status", ["archived", "draft"])
async def test_master_chat_cannot_stage_after_workspace_becomes_inactive(db_session, status):
    workspace = await workspace_fixture(db_session)
    workspace.status = status
    await db_session.flush()
    with pytest.raises(RuntimeRunNotFoundError, match="no longer available"):
        await create_runtime_run(db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
                                 conversation_id=generate_ulid(), user_id=generate_ulid(),
                                 agent_id="master", execution_payload={})
