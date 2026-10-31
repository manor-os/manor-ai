from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.workspace_health import WorkspaceHealthSnapshot
from packages.core.services import workspace_health_snapshot as snapshot_module
from packages.core.services.workspace_health_metrics import STATUS_DEGRADED, STATUS_FAILING, STATUS_HEALTHY
from packages.core.services.workspace_health_rollup import rollup_workspace_day
from packages.core.services.workspace_health_snapshot import refresh_workspace_health
from workspace_health_factories import _job, _reviews, _tasks, _workspace

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
YESTERDAY_NOON = NOW - timedelta(days=1)


@pytest.mark.asyncio
async def test_snapshot_marks_a_failing_workspace_and_keeps_status_since(db_session):
    ws = await _workspace(db_session)
    await _tasks(db_session, ws, [("failed", None, None)] * 4 + [("completed", None, 10)],
                 finished_at=YESTERDAY_NOON)  # 1/5 succeeded, yesterday
    # Yesterday belongs to the daily beat; the snapshot only re-rolls today.
    await rollup_workspace_day(db_session, day=YESTERDAY_NOON.date(), now=NOW)
    await refresh_workspace_health(db_session, now=NOW, workspace_ids=[ws.id])
    snap = await db_session.get(WorkspaceHealthSnapshot, ws.id)
    assert snap.status == STATUS_FAILING
    assert snap.surfaces["task"]["success_rate_7d"] == 0.2
    first_since = snap.status_since

    await refresh_workspace_health(db_session, now=NOW + timedelta(hours=1), workspace_ids=[ws.id])
    await db_session.refresh(snap)
    assert snap.status_since == first_since          # unchanged status keeps its start
    assert snap.refreshed_at == NOW + timedelta(hours=1)

    # A week on, an enabled job keeps it in scope but the failures have
    # left the 7d window: the status changes, and so does its start.
    await _job(db_session, ws, enabled=True, next_run_at=NOW + timedelta(days=9))
    later = NOW + timedelta(days=8)
    await refresh_workspace_health(db_session, now=later, workspace_ids=[ws.id])
    await db_session.refresh(snap)
    assert snap.status != STATUS_FAILING
    assert snap.status_since == later


@pytest.mark.asyncio
async def test_snapshot_rolls_today_before_deriving(db_session):
    ws = await _workspace(db_session)
    await _tasks(db_session, ws, [("completed", None, 10)] * 5, finished_at=NOW - timedelta(hours=2))
    await refresh_workspace_health(db_session, now=NOW, workspace_ids=[ws.id])
    snap = await db_session.get(WorkspaceHealthSnapshot, ws.id)
    assert snap.surfaces["task"]["success_rate_24h"] == 1.0
    assert snap.status == STATUS_HEALTHY


@pytest.mark.asyncio
async def test_snapshot_counts_missed_schedules_and_backlog(db_session):
    ws = await _workspace(db_session)
    await _job(db_session, ws, enabled=True, next_run_at=NOW - timedelta(hours=2))
    await _job(db_session, ws, enabled=True, next_run_at=NOW - timedelta(minutes=30))
    await _job(db_session, ws, enabled=False, next_run_at=NOW - timedelta(days=3))
    await _reviews(db_session, ws, [("pending", "review", False)], at=NOW - timedelta(hours=25))
    await refresh_workspace_health(db_session, now=NOW, workspace_ids=[ws.id])
    snap = await db_session.get(WorkspaceHealthSnapshot, ws.id)
    assert (snap.missed_schedules, snap.pending_approvals_over_24h) == (1, 1)
    assert snap.status == STATUS_DEGRADED


async def _boom(*_a, **_k):
    raise RuntimeError("query failed")


@pytest.mark.asyncio
async def test_a_failed_stall_query_marks_only_the_stall_count_unknown(db_session, monkeypatch):
    ws = await _workspace(db_session)
    await _tasks(db_session, ws, [("completed", None, 10)] * 5, finished_at=NOW - timedelta(hours=2))
    monkeypatch.setattr(snapshot_module, "_stalled_tasks", _boom)
    result = await refresh_workspace_health(db_session, now=NOW, workspace_ids=[ws.id])
    snap = await db_session.get(WorkspaceHealthSnapshot, ws.id)
    assert snap.surfaces["task"]["stalled"] is None
    assert snap.surfaces["task"]["success_rate_24h"] == 1.0
    assert snap.surfaces["workflow"]["stalled"] == 0
    assert result["unknown_parts"] == ["stalled_tasks"]
    # First snapshot: the status comes from what is known.
    assert snap.status == STATUS_HEALTHY
    assert {"rule": "stalled_tasks_unknown", "surface": None, "value": None} in snap.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("part, target", [
    ("daily_sums", "_daily_sums"),
    ("stalled_tasks", "_stalled_tasks"),
    ("readiness", "evaluate_workspace_blocking_setup"),
])
async def test_an_unknown_part_keeps_the_previous_status(db_session, monkeypatch, part, target):
    ws = await _workspace(db_session)
    await _tasks(db_session, ws, [("failed", None, None)] * 4 + [("completed", None, 10)],
                 finished_at=YESTERDAY_NOON)
    await rollup_workspace_day(db_session, day=YESTERDAY_NOON.date(), now=NOW)
    await _job(db_session, ws, enabled=True, next_run_at=NOW + timedelta(days=30))
    await refresh_workspace_health(db_session, now=NOW, workspace_ids=[ws.id])
    snap = await db_session.get(WorkspaceHealthSnapshot, ws.id)
    assert snap.status == STATUS_FAILING
    first_since = snap.status_since

    # A week on the failures have left the window, which would read as
    # idle — but one part the status depends on cannot be evaluated.
    monkeypatch.setattr(snapshot_module, target, _boom)
    later = NOW + timedelta(days=8)
    result = await refresh_workspace_health(db_session, now=later, workspace_ids=[ws.id])
    await db_session.refresh(snap)
    assert (snap.status, snap.status_since) == (STATUS_FAILING, first_since)
    assert snap.refreshed_at == later
    assert {"rule": f"{part}_unknown", "surface": None, "value": None} in snap.reasons
    assert any(r["rule"] == "success_rate_fail" for r in snap.reasons)
    assert part in result["unknown_parts"]
    assert snap.missed_schedules == 0  # known counts still update


@pytest.mark.asyncio
async def test_archived_and_inactive_workspaces_are_out_of_scope(db_session):
    archived = await _workspace(db_session, status="archived")
    await _tasks(db_session, archived, [("completed", None, 10)], finished_at=NOW - timedelta(hours=1))
    idle = await _workspace(db_session)  # no runs, no jobs
    await refresh_workspace_health(db_session, now=NOW, workspace_ids=[archived.id, idle.id])
    assert await db_session.get(WorkspaceHealthSnapshot, archived.id) is None
    assert await db_session.get(WorkspaceHealthSnapshot, idle.id) is None
