"""Tests for scripts/backfill_workspace_events.py (M1 回填).

The backfill replays terminal Task / ScheduledJobRun / WorkflowRun /
HitlRequest / GoalMeasurement history into workspace_events under
``backfill:{table}:{row_id}:{event_type}`` keys. All assertions are scoped
to a per-test entity so the shared test DB never bleeds into counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger.service import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent

from scripts.backfill_workspace_events import run_backfill


def _mk_workspace(db) -> Workspace:
    ws = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Backfill WS",
        status="active",
    )
    db.add(ws)
    return ws


async def _events(db, *, entity_id: str) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent)
        .where(WorkspaceEvent.entity_id == entity_id)
        .order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


def _seed_sources(db, ws: Workspace, now: datetime) -> dict:
    """Seed one representative history per source table. Returns handles."""
    eid = ws.entity_id
    completed_task = Task(
        id=generate_ulid(), entity_id=eid, workspace_id=ws.id,
        title="done task", status="completed",
        completed_at=now - timedelta(days=3),
        details={"workspace_work_batch_id": "batch_bf", "scheduled_job_id": "sj_bf"},
    )
    failed_task = Task(
        id=generate_ulid(), entity_id=eid, workspace_id=ws.id,
        title="failed task", status="failed", details={},
    )
    entity_task = Task(  # no workspace -> never backfilled
        id=generate_ulid(), entity_id=eid, workspace_id=None,
        title="entity task", status="completed", details={},
    )
    db.add_all([completed_task, failed_task, entity_task])

    job = ScheduledJob(
        id=generate_ulid(), job_id=f"bf-{generate_ulid()}", entity_id=eid,
        workspace_id=ws.id, name="bf automation", schedule_kind="every",
        every_seconds=86400.0, enabled=True,
        execution_target={"workspace_id": ws.id},
    )
    db.add(job)
    run_ok = ScheduledJobRun(
        id=generate_ulid(), job_id=job.job_id, status="success",
        started_at=now - timedelta(days=2, hours=1),
        completed_at=now - timedelta(days=2),
    )
    run_err = ScheduledJobRun(
        id=generate_ulid(), job_id=job.job_id, status="error",
        started_at=now - timedelta(days=1, hours=1),
        completed_at=now - timedelta(days=1),
    )
    db.add_all([run_ok, run_err])

    wf_run = WorkflowRun(
        id=generate_ulid(), workflow_id=generate_ulid(), entity_id=eid,
        workspace_id=ws.id, status="completed",
        completed_at=now - timedelta(days=4),
        trigger_data={"scheduled_job_id": job.job_id},
    )
    db.add(wf_run)

    granted_req = HitlRequest(
        id=generate_ulid(), entity_id=eid, workspace_id=ws.id,
        action_key="workspace.automation.create", risk_level="medium",
        origin_kind="step", origin_step_id=generate_ulid(),
        status="granted", dedup_key=f"step:{generate_ulid()}",
        decided_by_user_id="user_bf", decided_at=now - timedelta(days=5),
    )
    consumed_req = HitlRequest(
        id=generate_ulid(), entity_id=eid, workspace_id=ws.id,
        action_key="workspace.publish", risk_level="high",
        origin_kind="step", origin_step_id=generate_ulid(),
        status="consumed", dedup_key=f"step:{generate_ulid()}",
        decided_by_user_id="user_bf",
        decided_at=now - timedelta(days=6, hours=1),
        consumed_at=now - timedelta(days=6),
    )
    db.add_all([granted_req, consumed_req])

    goal = Goal(
        id=generate_ulid(), entity_id=eid, workspace_id=ws.id,
        title="bf goal", metric_key="follower_count",
        target_value=Decimal("1000"),
    )
    db.add(goal)
    measurements = [
        GoalMeasurement(
            goal_id=goal.id,
            measured_at=now - timedelta(days=i + 1),
            value=Decimal(100 + i),
            source="manual",
        )
        for i in range(3)
    ]
    db.add_all(measurements)

    return {
        "completed_task": completed_task,
        "failed_task": failed_task,
        "job": job,
        "run_ok": run_ok,
        "run_err": run_err,
        "wf_run": wf_run,
        "granted_req": granted_req,
        "consumed_req": consumed_req,
        "goal": goal,
    }


async def test_backfill_creates_events_then_second_run_skips_all(db_session):
    ws = _mk_workspace(db_session)
    now = datetime.now(timezone.utc)
    handles = _seed_sources(db_session, ws, now)
    await db_session.flush()

    summary = await run_backfill(db_session, days=90, entity_id=ws.entity_id)

    assert summary["tasks"] == {"created": 2, "skipped": 0}
    assert summary["scheduled_job_runs"] == {"created": 2, "skipped": 0}
    assert summary["workflow_runs"] == {"created": 1, "skipped": 0}
    assert summary["approval_requests"] == {"created": 2, "skipped": 0}
    assert summary["goal_measurements"] == {"created": 3, "skipped": 0}

    rows = await _events(db_session, entity_id=ws.entity_id)
    by_key = {row.idempotency_key: row for row in rows}
    assert len(rows) == 10
    assert all(row.idempotency_key.startswith("backfill:") for row in rows)

    task = handles["completed_task"]
    task_row = by_key[f"backfill:tasks:{task.id}:{et.EXECUTION_COMPLETED}"]
    assert task_row.workspace_id == ws.id
    assert task_row.root_execution_id == "batch_bf"   # same mapping as live adapter
    assert task_row.causation_id == "sj_bf"
    assert task_row.status == "completed"
    occurred = task_row.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    assert abs((occurred - task.completed_at).total_seconds()) < 1

    run_row = by_key[
        f"backfill:scheduled_job_runs:{handles['run_ok'].id}:{et.AUTOMATION_RUN_COMPLETED}"
    ]
    assert run_row.source_id == handles["job"].id
    assert run_row.run_id == handles["run_ok"].id
    assert f"backfill:scheduled_job_runs:{handles['run_err'].id}:{et.AUTOMATION_RUN_FAILED}" in by_key

    wf_row = by_key[f"backfill:workflow_runs:{handles['wf_run'].id}:{et.WORKFLOW_RUN_COMPLETED}"]
    assert wf_row.causation_id == handles["job"].job_id

    granted_row = by_key[
        f"backfill:approval_requests:{handles['granted_req'].id}:{et.APPROVAL_GRANTED}"
    ]
    assert granted_row.actor_kind == "user"
    assert granted_row.actor_id == "user_bf"
    consumed_row = by_key[
        f"backfill:approval_requests:{handles['consumed_req'].id}:{et.APPROVAL_CONSUMED}"
    ]
    assert consumed_row.actor_kind == "system"

    goal_rows = [r for r in rows if r.event_type == et.GOAL_MEASURED]
    assert len(goal_rows) == 3
    assert all(r.goal_refs == [handles["goal"].id] for r in goal_rows)

    # Second run: everything is already on the ledger.
    summary2 = await run_backfill(db_session, days=90, entity_id=ws.entity_id)
    assert summary2["tasks"] == {"created": 0, "skipped": 2}
    assert summary2["scheduled_job_runs"] == {"created": 0, "skipped": 2}
    assert summary2["workflow_runs"] == {"created": 0, "skipped": 1}
    assert summary2["approval_requests"] == {"created": 0, "skipped": 2}
    assert summary2["goal_measurements"] == {"created": 0, "skipped": 3}
    assert len(await _events(db_session, entity_id=ws.entity_id)) == 10


async def test_backfill_dry_run_writes_nothing(db_session):
    ws = _mk_workspace(db_session)
    now = datetime.now(timezone.utc)
    _seed_sources(db_session, ws, now)
    await db_session.flush()

    summary = await run_backfill(db_session, days=90, entity_id=ws.entity_id, dry_run=True)

    assert summary["tasks"] == {"created": 2, "skipped": 0}
    assert summary["goal_measurements"] == {"created": 3, "skipped": 0}
    assert await _events(db_session, entity_id=ws.entity_id) == []


async def test_backfill_skips_rows_already_covered_by_live_adapter_events(db_session):
    ws = _mk_workspace(db_session)
    task = Task(
        id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
        title="live-covered task", status="completed",
        completed_at=datetime.now(timezone.utc), details={},
    )
    db_session.add(task)
    await db_session.flush()
    # The live adapter already recorded this completion.
    await record_event(
        db_session,
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=task.id,
        status="completed",
        idempotency_key=f"task:{task.id}:{et.EXECUTION_COMPLETED}",
    )

    summary = await run_backfill(db_session, days=90, entity_id=ws.entity_id)

    assert summary["tasks"] == {"created": 0, "skipped": 1}
    rows = await _events(db_session, entity_id=ws.entity_id)
    assert len(rows) == 1  # only the live event — no backfill duplicate
    assert rows[0].idempotency_key == f"task:{task.id}:{et.EXECUTION_COMPLETED}"


async def test_backfill_respects_window_and_entity_filter(db_session):
    ws = _mk_workspace(db_session)
    other_ws = _mk_workspace(db_session)  # different entity
    now = datetime.now(timezone.utc)
    old_task = Task(
        id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
        title="ancient task", status="completed",
        completed_at=now - timedelta(days=200),
        updated_at=now - timedelta(days=200),
        details={},
    )
    fresh_task = Task(
        id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
        title="fresh task", status="completed",
        completed_at=now - timedelta(days=1), details={},
    )
    foreign_task = Task(
        id=generate_ulid(), entity_id=other_ws.entity_id, workspace_id=other_ws.id,
        title="foreign task", status="completed",
        completed_at=now - timedelta(days=1), details={},
    )
    db_session.add_all([old_task, fresh_task, foreign_task])
    await db_session.flush()

    summary = await run_backfill(db_session, days=90, entity_id=ws.entity_id)

    assert summary["tasks"] == {"created": 1, "skipped": 0}
    rows = await _events(db_session, entity_id=ws.entity_id)
    assert [r.source_id for r in rows] == [fresh_task.id]
    assert await _events(db_session, entity_id=other_ws.entity_id) == []
