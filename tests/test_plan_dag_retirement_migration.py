"""Migration 20261025_10 closes the retired Plan/Step engine's in-flight work once."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.worker import WorkLease, Worker


def _migration():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/migrations"))
    return ScriptDirectory.from_config(config).get_revision("20261025_10").module


async def _task(db, entity_id, workspace_id, *, engine, phase, status="in_progress"):
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title="t", status=status)
    db.add(task)
    db.add(TaskCoordinatorRun(
        task_id=task.id, entity_id=entity_id, workspace_id=workspace_id, root_subscription_id="sub",
        root_agent_id="agent", selection_fingerprint="f", lifecycle_generation="g", engine=engine,
        phase=phase, claim_token="tok", claim_generation="gen",
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), wake_generation="w",
    ))
    db.add(HitlRequest(entity_id=entity_id, origin_kind="task", origin_task_id=task.id,
                       dedup_key=f"card:{task.id}", status="pending"))
    db.add(RuntimeOutboxEvent(event_type="runtime.dispatch_task_coordinator", aggregate_id=task.id,
                              dedupe_key=f"task-coordinator:{task.id}", payload={}))
    return task


async def _plan(db, entity_id, worker_id, *, task_id, source=None):
    plan = ExecutionPlan(id=generate_ulid(), entity_id=entity_id, task_id=task_id, status="running",
                         execution_mode="live", approval_required=False, plan_dag={"steps": []},
                         dispatcher_state={"source": source} if source else {})
    db.add(plan)
    step = ExecutionStep(id=generate_ulid(), plan_id=plan.id, entity_id=entity_id, step_key="s",
                         kind="subagent", params={}, depends_on=[], step_status="running",
                         attempt_count=1, max_attempts=3)
    db.add(step)
    lease = WorkLease(id=generate_ulid(), step_id=step.id, plan_id=plan.id, entity_id=entity_id,
                      worker_id=worker_id, status="active", execution_claim_id="claim",
                      lease_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(lease)
    step.current_lease_id = lease.id
    return plan, step, lease


@pytest.mark.asyncio
async def test_retirement_closes_plan_dag_work_and_leaves_agent_loops_and_worker_jobs(db_session):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    worker = Worker(id=generate_ulid(), entity_id=entity_id, kind="internal", display_name="w",
                    capabilities={}, monthly_spent_usd=Decimal("0"), status="active")
    db_session.add(worker)
    dag_task = await _task(db_session, entity_id, workspace_id, engine="plan_dag", phase="waiting_human")
    dag_plan, dag_step, dag_lease = await _plan(db_session, entity_id, worker.id, task_id=dag_task.id)
    loop_task = await _task(db_session, entity_id, workspace_id, engine="agent_loop", phase="waiting_human",
                            status="waiting_on_customer")
    job_plan, job_step, job_lease = await _plan(db_session, entity_id, worker.id, task_id=None, source="cli_worker")
    await db_session.flush()

    def migrate(sync_session):
        with Operations.context(MigrationContext.configure(sync_session.connection())):
            _migration().upgrade()

    await db_session.run_sync(migrate)
    for row in (dag_task, loop_task, dag_plan, dag_step, dag_lease, job_plan, job_step, job_lease):
        await db_session.refresh(row)
    runs = {run.task_id: run for run in (await db_session.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id.in_([dag_task.id, loop_task.id]),
    ))).scalars()}
    cards = {card.origin_task_id: card for card in (await db_session.execute(select(HitlRequest).where(
        HitlRequest.entity_id == entity_id,
    ))).scalars()}
    wakes = {event.aggregate_id: event for event in (await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id.in_([dag_task.id, loop_task.id]),
    ))).scalars()}

    # The retired engine's work: closed, fenced and reported once.
    assert dag_task.status == "blocked"
    assert (dag_plan.status, dag_step.step_status, dag_lease.status) == ("cancelled", "cancelled", "cancelled")
    assert dag_step.current_lease_id is None and dag_lease.execution_claim_id is None
    dag_run = runs[dag_task.id]
    assert dag_run.phase == "failed" and dag_run.claim_token is None and dag_run.wake_generation is None
    assert dag_run.checkpoint["coordinator_outcome"]["error"] == "runtime_migrated"
    assert cards[dag_task.id].status == "expired"
    assert wakes[dag_task.id].delivered_at is not None
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == dag_task.id))).scalars().all()
    assert [log.meta["error"] for log in logs] == ["runtime_migrated"]
    # An agent loop and a worker job are not the retired engine's.
    assert loop_task.status == "waiting_on_customer"
    assert runs[loop_task.id].phase == "waiting_human" and runs[loop_task.id].claim_token == "tok"
    assert cards[loop_task.id].status == "pending" and wakes[loop_task.id].delivered_at is None
    assert (job_plan.status, job_step.step_status, job_lease.status) == ("running", "running", "active")

    # Idempotent: nothing is left to close, no second log line.
    await db_session.run_sync(migrate)
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == dag_task.id))).scalars().all()
    assert len(logs) == 1
