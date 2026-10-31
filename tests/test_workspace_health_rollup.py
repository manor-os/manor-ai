from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionStep
from packages.core.models.scheduler import ScheduledJobRun
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace_health import MetricsWorkspaceDaily
from packages.core.services.workspace_health_rollup import rollup_workspace_day
from workspace_health_factories import (
    _credits,
    _feedback,
    _job,
    _proposal_items,
    _reviews,
    _tasks,
    _workspace,
)

DAY = date(2026, 10, 27)
NOON = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


async def _row(db, ws_id, surface):
    return (await db.execute(select(MetricsWorkspaceDaily).where(
        MetricsWorkspaceDaily.day == DAY,
        MetricsWorkspaceDaily.workspace_id == ws_id,
        MetricsWorkspaceDaily.surface == surface,
    ))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_task_surface_counts_shared_outcomes(db_session):
    ws = await _workspace(db_session, blueprint=("builtin:weekly-report", "3"), sandbox=False)
    await _tasks(db_session, ws, [
        ("completed", None, 30), ("completed", None, 400),   # status, cancel_source, seconds
        ("blocked", None, None),                             # failed
        ("cancelled", "user", None), ("cancelled", "proposal", None),
        ("in_progress", None, None),                         # not terminal
    ], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "task")
    assert (row.succeeded, row.failed, row.cancelled, row.user_cancelled) == (2, 1, 1, 1)
    assert (row.duration_count, row.duration_sum_s) == (2, 430)
    assert row.duration_buckets == {"le_60": 1, "le_900": 1}
    assert row.top_errors == [{"message": "Task blocked", "count": 1}]
    assert (row.blueprint_key, row.blueprint_version, row.mode) == ("builtin:weekly-report", "3", "live")


@pytest.mark.asyncio
async def test_workspace_surface_counts_quality_signals_and_credits(db_session):
    ws = await _workspace(db_session, sandbox=True)
    await _reviews(db_session, ws, [("granted", "review", True), ("denied", "authorize", True),
                                    ("expired", "review", False), ("granted", "input", True)], at=NOON)
    await _feedback(db_session, ws, ["up", "up", "down"], at=NOON)
    await _proposal_items(db_session, ws, ["approved", "succeeded", "rejected", "proposed"], at=NOON)
    await _credits(db_session, ws, [5, 7], at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "workspace")
    assert (row.reviews_accepted, row.reviews_rejected, row.reviews_ignored) == (1, 1, 1)
    assert (row.feedback_up, row.feedback_down) == (2, 1)
    assert (row.proposals_accepted, row.proposals_rejected) == (2, 1)
    assert row.credits == 12
    assert row.mode == "simulation"


@pytest.mark.asyncio
async def test_run_surfaces_use_each_engine_finish_time_and_classifier(db_session):
    ws = await _workspace(db_session)
    common = {"entity_id": ws.entity_id, "workspace_id": ws.id}
    db_session.add_all([
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="completed",
                    started_at=NOON - timedelta(seconds=5), completed_at=NOON, **common),
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="failed",
                    error="Publisher returned 500", attempt_number=2, completed_at=NOON, **common),
        WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), status="cancelled",
                    cancel_source="system", completed_at=NOON, **common),
        ExecutionStep(id=generate_ulid(), plan_id=generate_ulid(), step_key="s1", kind="subagent",
                      resolved_agent_id=generate_ulid(), step_status="done", attempt_count=2,
                      started_at=NOON - timedelta(seconds=20), finished_at=NOON, **common),
        TaskAgentChildRun(
            id=generate_ulid(), task_id=generate_ulid(), root_run_id="r", parent_ref="r",
            parent_tool_call_id="call-1", item_index=0, batch_size=1, depth=1,
            agent_subscription_id=generate_ulid(), agent_id=generate_ulid(), prompt="Edit.",
            answer_max_chars=4000, lifecycle_generation="g1", phase="failed",
            outcome_status="failed", state_version=0, terminal_at=NOON,
            result={"message": "Browser session expired"}, **common,
        ),
    ])
    job = await _job(db_session, ws, True, None)
    db_session.add_all([
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="success",
                        started_at=NOON - timedelta(seconds=2), completed_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="error", error="timeout",
                        created_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="skipped", created_at=NOON),
        ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running", created_at=NOON),
    ])
    await db_session.flush()

    await rollup_workspace_day(db_session, day=DAY)

    workflow = await _row(db_session, ws.id, "workflow")
    assert (workflow.succeeded, workflow.failed, workflow.cancelled, workflow.retried) == (1, 1, 1, 1)
    assert workflow.user_cancelled == 0
    assert workflow.top_errors == [{"message": "Publisher returned 500", "count": 1}]
    agent = await _row(db_session, ws.id, "agent")
    assert (agent.succeeded, agent.failed, agent.retried) == (1, 1, 1)
    assert agent.top_errors == [{"message": "Browser session expired", "count": 1}]
    automation = await _row(db_session, ws.id, "automation")
    assert (automation.succeeded, automation.failed, automation.cancelled) == (1, 1, 0)


@pytest.mark.asyncio
async def test_rollup_attributes_by_finish_day_and_isolates_workspaces(db_session):
    a, b = await _workspace(db_session), await _workspace(db_session)
    await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON)
    await _tasks(db_session, a, [("completed", None, 10)], finished_at=NOON + timedelta(days=1))
    await _tasks(db_session, b, [("failed", None, None)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    assert (await _row(db_session, a.id, "task")).succeeded == 1
    assert (await _row(db_session, b.id, "task")).failed == 1


@pytest.mark.asyncio
async def test_rerolling_a_day_replaces_its_rows(db_session):
    ws = await _workspace(db_session)
    [task] = await _tasks(db_session, ws, [("completed", None, 10)], finished_at=NOON)
    await rollup_workspace_day(db_session, day=DAY)
    task.status, task.cancel_source = "cancelled", "user"
    await db_session.flush()
    await rollup_workspace_day(db_session, day=DAY)
    row = await _row(db_session, ws.id, "task")
    assert (row.succeeded, row.cancelled) == (0, 1)


@pytest.mark.asyncio
async def test_rollup_prunes_rows_past_retention(db_session):
    ws = await _workspace(db_session)
    db_session.add_all([
        MetricsWorkspaceDaily(day=DAY - timedelta(days=181), workspace_id=ws.id,
                              surface="task", entity_id=ws.entity_id, mode="live"),
        MetricsWorkspaceDaily(day=DAY - timedelta(days=179), workspace_id=ws.id,
                              surface="task", entity_id=ws.entity_id, mode="live"),
    ])
    await db_session.flush()
    result = await rollup_workspace_day(db_session, day=DAY, now=NOON)
    # The test DB is shared, so assert on this Workspace's rows, not the count.
    assert result["pruned"] >= 1
    kept = (await db_session.execute(select(MetricsWorkspaceDaily.day).where(
        MetricsWorkspaceDaily.workspace_id == ws.id,
    ))).scalars().all()
    assert kept == [DAY - timedelta(days=179)]
