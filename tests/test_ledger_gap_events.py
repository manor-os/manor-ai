"""Ledger gap events (M1 completion).

Covers the three gaps closed after the main adapter wave:
* automation_run_missed — the scheduler tick's throttled missed-run scan
  (interval jobs >= 1h whose last run is > 2x their interval old, plus
  cron jobs whose previous occurrence went unserved past the grace
  period; see _previous_cron_occurrence and its 24h lookback bound).
* approval_expired — emitted when resolve_origin_requests expires the open
  requests of a terminal origin.
* workflow_definitions revision (M11) — template content changes bump the
  revision and append an automation_revisions audit row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.automation_revision import AutomationRevision
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent

try:
    from packages.core.tasks.scheduler_tasks import _scan_missed_runs
except ImportError:
    pytest.skip("Celery not installed — skipping scheduler tests", allow_module_level=True)


def _mk_workspace(db) -> Workspace:
    ws = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Gap Events WS",
        status="active",
    )
    db.add(ws)
    return ws


def _mk_job(db, ws: Workspace | None, **overrides) -> ScheduledJob:
    defaults = dict(
        id=generate_ulid(),
        job_id=f"gap-{generate_ulid()}",
        entity_id=ws.entity_id if ws else generate_ulid(),
        workspace_id=ws.id if ws else None,
        name="six-hourly digest",
        schedule_kind="every",
        every_seconds=21600.0,  # 6h
        enabled=True,
        execution_type="agent",
        execution_target={"workspace_id": ws.id} if ws else {},
    )
    defaults.update(overrides)
    job = ScheduledJob(**defaults)
    db.add(job)
    return job


async def _events(db, *, entity_id: str, event_type: str) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent)
        .where(
            WorkspaceEvent.entity_id == entity_id,
            WorkspaceEvent.event_type == event_type,
        )
        .order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


# ── automation_run_missed ──────────────────────────────────────────

async def test_missed_scan_emits_one_event_and_dedupes_same_period(db_session):
    ws = _mk_workspace(db_session)
    now = datetime.now(timezone.utc)
    stale_last_run = now - timedelta(hours=13)  # > 2 x 6h
    job = _mk_job(db_session, ws, last_run_at=stale_last_run)
    await db_session.flush()

    await _scan_missed_runs(db_session, now)

    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED)
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == ws.id
    assert row.source_kind == "scheduled_job"
    assert row.source_id == job.id
    assert row.status == "missed"
    expected_by = stale_last_run + timedelta(seconds=21600)
    assert row.period_key == expected_by.strftime("%Y-%m-%dT%H")  # sub-daily -> hourly key
    assert row.idempotency_key == f"sj:{job.id}:missed:{row.period_key}"
    occurred = row.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    assert abs((occurred - expected_by).total_seconds()) < 1

    # Re-running the scan for the same period is a no-op (idempotent).
    await _scan_missed_runs(db_session, now)
    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED)
    assert len(rows) == 1


async def test_missed_scan_skips_subhourly_disabled_and_on_time_jobs(db_session):
    ws = _mk_workspace(db_session)
    now = datetime.now(timezone.utc)
    very_stale = now - timedelta(days=2)

    # Sub-hourly: recovers next tick, would be pure noise.
    _mk_job(db_session, ws, every_seconds=600.0, last_run_at=very_stale)
    # Disabled: not expected to run at all.
    _mk_job(db_session, ws, enabled=False, last_run_at=very_stale)
    # Disabled cron: same — an off job cannot miss a run.
    _mk_job(
        db_session, ws, schedule_kind="cron", cron_expr="0 9 * * *",
        every_seconds=None, enabled=False, last_run_at=very_stale,
    )
    # Late but under the 2x threshold: not yet a missed period.
    _mk_job(db_session, ws, last_run_at=now - timedelta(hours=7))
    # Never ran at all: no reference period to have missed.
    _mk_job(db_session, ws, last_run_at=None)
    await db_session.flush()

    await _scan_missed_runs(db_session, now)

    assert await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED) == []


async def test_missed_scan_skips_jobs_without_workspace(db_session):
    entity_id = generate_ulid()
    now = datetime.now(timezone.utc)
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"gap-{generate_ulid()}",
        entity_id=entity_id,
        workspace_id=None,
        schedule_kind="every",
        every_seconds=21600.0,
        enabled=True,
        execution_target={},
        last_run_at=now - timedelta(days=2),
    )
    db_session.add(job)
    await db_session.flush()

    await _scan_missed_runs(db_session, now)

    assert await _events(db_session, entity_id=entity_id, event_type=et.AUTOMATION_RUN_MISSED) == []


# ── automation_run_missed: cron jobs ───────────────────────────────

# A Friday at noon UTC — fixed so the cron arithmetic below is
# deterministic regardless of when the suite runs.
_CRON_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _mk_cron_job(db, ws: Workspace, **overrides) -> ScheduledJob:
    defaults = dict(
        schedule_kind="cron",
        cron_expr="0 9 * * *",
        every_seconds=None,
        timezone="UTC",
        name="daily 09:00 digest",
    )
    defaults.update(overrides)
    return _mk_job(db, ws, **defaults)


async def test_missed_scan_emits_for_cron_with_stale_last_run(db_session):
    ws = _mk_workspace(db_session)
    job = _mk_cron_job(db_session, ws, last_run_at=_CRON_NOW - timedelta(days=2))
    await db_session.flush()

    await _scan_missed_runs(db_session, _CRON_NOW)

    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED)
    assert len(rows) == 1
    row = rows[0]
    assert row.source_id == job.id
    assert row.status == "missed"
    expected_by = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
    # Cron jobs are not "every" jobs → daily dedupe period.
    assert row.period_key == "2026-07-24"
    assert row.idempotency_key == f"sj:{job.id}:missed:2026-07-24"
    occurred = row.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    assert abs((occurred - expected_by).total_seconds()) < 1

    # Rescanning the same period is idempotent.
    await _scan_missed_runs(db_session, _CRON_NOW)
    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED)
    assert len(rows) == 1


async def test_missed_scan_skips_cron_that_ran_after_its_occurrence(db_session):
    ws = _mk_workspace(db_session)
    # Fired at 09:00:05, right after the 09:00 occurrence.
    _mk_cron_job(
        db_session, ws,
        last_run_at=datetime(2026, 7, 24, 9, 0, 5, tzinfo=timezone.utc),
    )
    await db_session.flush()

    await _scan_missed_runs(db_session, _CRON_NOW)

    assert await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED) == []


async def test_missed_scan_respects_cron_grace_period(db_session):
    ws = _mk_workspace(db_session)
    _mk_cron_job(db_session, ws, last_run_at=_CRON_NOW - timedelta(days=2))
    await db_session.flush()

    # 3 minutes past the 09:00 occurrence — inside the 5-minute grace, so
    # the run is merely late (dispatch is async), not missed.
    await _scan_missed_runs(db_session, datetime(2026, 7, 24, 9, 3, tzinfo=timezone.utc))

    assert await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED) == []


async def test_missed_scan_skips_cron_whose_occurrence_predates_lookback(db_session):
    ws = _mk_workspace(db_session)
    # Fires once a year (Jan 1 09:00) — no occurrence inside the 24h
    # lookback window, so nothing can be claimed as missed.
    _mk_cron_job(
        db_session, ws, cron_expr="0 9 1 1 *",
        last_run_at=_CRON_NOW - timedelta(days=200),
    )
    await db_session.flush()

    await _scan_missed_runs(db_session, _CRON_NOW)

    assert await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED) == []


async def test_missed_scan_emits_for_cron_that_never_ran(db_session):
    ws = _mk_workspace(db_session)
    _mk_cron_job(db_session, ws, last_run_at=None)
    await db_session.flush()

    await _scan_missed_runs(db_session, _CRON_NOW)

    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.AUTOMATION_RUN_MISSED)
    assert len(rows) == 1


# ── _previous_cron_occurrence ──────────────────────────────────────

def test_previous_cron_occurrence_daily():
    from packages.core.tasks.scheduler_tasks import _previous_cron_occurrence

    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    assert _previous_cron_occurrence("0 9 * * *", now) == datetime(
        2026, 7, 24, 9, 0, tzinfo=timezone.utc
    )
    # Before today's 09:00 → yesterday's occurrence (still inside 24h).
    assert _previous_cron_occurrence(
        "0 9 * * *", datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    # The current minute never counts as "previous" — the scan looks at
    # minutes strictly before now.
    assert _previous_cron_occurrence(
        "0 9 * * *", datetime(2026, 7, 24, 9, 0, 30, tzinfo=timezone.utc)
    ) == datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)


def test_previous_cron_occurrence_weekday_restricted():
    from packages.core.tasks.scheduler_tasks import _previous_cron_occurrence

    # 2026-07-24 is a Friday; "0 9 * * 1" fires Mondays only.
    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    # Outside the default 24h bound → deliberately None.
    assert _previous_cron_occurrence("0 9 * * 1", now) is None
    # Widen the window and the Monday occurrence is found.
    assert _previous_cron_occurrence(
        "0 9 * * 1", now, lookback_minutes=7 * 1440,
    ) == datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)

    # Weekday-restricted expression that DID fire today (Friday = cron 5).
    assert _previous_cron_occurrence("0 9 * * 5", now) == datetime(
        2026, 7, 24, 9, 0, tzinfo=timezone.utc
    )


def test_previous_cron_occurrence_unparseable_returns_none():
    from packages.core.tasks.scheduler_tasks import _previous_cron_occurrence

    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    assert _previous_cron_occurrence("not a cron", now) is None
    assert _previous_cron_occurrence("0 9 * *", now) is None  # 4 fields
    assert _previous_cron_occurrence("", now) is None
    assert _previous_cron_occurrence(None, now) is None


# ── approval_expired ───────────────────────────────────────────────

async def test_resolve_origin_requests_emits_approval_expired(db_session):
    from packages.core.governance.approvals import resolve_origin_requests

    ws = _mk_workspace(db_session)
    task_id = generate_ulid()
    req = HitlRequest(
        id=generate_ulid(),
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        action_key="workspace.automation.create",
        risk_level="medium",
        origin_kind="step",
        origin_step_id=generate_ulid(),
        origin_task_id=task_id,
        status="pending",
        dedup_key=f"step:{generate_ulid()}",
    )
    db_session.add(req)
    await db_session.flush()

    closed = await resolve_origin_requests(db_session, task_id=task_id)
    assert closed == 1
    assert req.status == "expired"

    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_EXPIRED)
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == ws.id
    assert row.source_kind == "approval"
    assert row.source_id == req.id
    assert row.status == "expired"
    assert row.actor_kind == "system"
    assert row.causation_id == req.origin_step_id

    # Second sweep finds no open rows -> no duplicate event.
    assert await resolve_origin_requests(db_session, task_id=task_id) == 0
    rows = await _events(db_session, entity_id=ws.entity_id, event_type=et.APPROVAL_EXPIRED)
    assert len(rows) == 1


# ── workflow_definitions revision (M11) ────────────────────────────

async def test_workflow_definition_content_update_bumps_revision_with_audit(db_session):
    from packages.core.services import workflow_service as svc

    entity_id = generate_ulid()
    steps_v1 = [{"id": "s1", "type": "agent", "name": "Draft", "config": {}, "next": []}]
    wf = await svc.create_workflow(db_session, entity_id, "Gap WF", steps_v1)
    assert wf.revision == 1

    steps_v2 = [{"id": "s1", "type": "agent", "name": "Draft v2", "config": {}, "next": []}]
    wf = await svc.update_workflow(
        db_session, wf.id, entity_id, steps=steps_v2, name="Gap WF v2",
    )
    assert wf.revision == 2

    audits = list((await db_session.execute(
        select(AutomationRevision).where(
            AutomationRevision.target_kind == "workflow_definition",
            AutomationRevision.target_id == wf.id,
        )
    )).scalars().all())
    assert len(audits) == 1
    assert audits[0].revision == 2
    assert audits[0].entity_id == entity_id
    assert set(audits[0].patch) == {"steps", "name"}


async def test_workflow_definition_cosmetic_or_noop_update_does_not_bump(db_session):
    from packages.core.services import workflow_service as svc

    entity_id = generate_ulid()
    steps = [{"id": "s1", "type": "agent", "name": "Draft", "config": {}, "next": []}]
    wf = await svc.create_workflow(db_session, entity_id, "Gap WF", steps)

    # Cosmetic change: description is not template content.
    wf = await svc.update_workflow(db_session, wf.id, entity_id, description="new desc")
    assert wf.revision == 1

    # Same-value content "change": no behavioral difference, no bump.
    wf = await svc.update_workflow(db_session, wf.id, entity_id, name="Gap WF", steps=steps)
    assert wf.revision == 1

    audits = list((await db_session.execute(
        select(AutomationRevision).where(
            AutomationRevision.target_kind == "workflow_definition",
            AutomationRevision.target_id == wf.id,
        )
    )).scalars().all())
    assert audits == []
