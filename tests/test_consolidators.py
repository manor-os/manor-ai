"""M3/M4 — ConsolidationReport contract, registry, and L0 consolidators.

Covers:
* contract blacklists (FORBIDDEN_KEYS everywhere; FORBIDDEN_HUMAN_METRICS
  only for the human_participation domain; extra attrs forbidden)
* run_all over a seeded workspace → 8 rows, all complete, hash + coverage
* failure isolation: one raising consolidator → its row failed, rest complete
* input_hash cache: identical snapshot reuses the previous succeeded
  review's reports without re-running (coverage.reused = true)
* read-only discipline: consolidator.run never flushes new/dirty/deleted
  objects (before_flush listener)
* targeted observations: failure_streak / pace_degraded / approval_bottleneck
* unique (review_id, domain)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.consolidators import (
    ConsolidationReportModel,
    Coverage,
    Observation,
    REGISTRY,
    SnapshotContext,
    run_all,
)
from packages.core.consolidators.registry import compute_input_hash
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.goal import Goal
from packages.core.models.review_run import ReviewRun
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.review import begin_review, complete_review, events_in_window

ENTITY_ID = "01CONSENTITY00000000000000"

ALL_DOMAINS = {
    "goal", "execution", "automation_portfolio", "artifact_knowledge",
    "human_participation", "capacity_cost", "risk_governance", "learning_evidence",
}

_seq = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _workspace(db) -> Workspace:
    workspace = Workspace(entity_id=ENTITY_ID, name="Consolidator WS")
    db.add(workspace)
    await db.flush()
    return workspace


async def _emit(
    db,
    workspace_id: str,
    *,
    event_type: str = et.EXECUTION_COMPLETED,
    source_kind: str = "task",
    source_id: str | None = None,
    payload: dict | None = None,
    status: str | None = None,
):
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=ENTITY_ID,
        workspace_id=workspace_id,
        event_type=event_type,
        source_kind=source_kind,
        source_id=source_id or f"src_{_seq}",
        idempotency_key=f"consolidator-test:{workspace_id}:{_seq}",
        payload=payload,
        status=status,
    )
    assert event is not None
    await asyncio.sleep(0.002)  # ULID ordering across distinct milliseconds
    return event


async def _begin(db, workspace_id: str) -> ReviewRun:
    return await begin_review(
        db, entity_id=ENTITY_ID, workspace_id=workspace_id, trigger="scheduled",
    )


async def _ctx(db, review: ReviewRun) -> SnapshotContext:
    workspace = await db.get(Workspace, review.workspace_id)
    events = await events_in_window(db, review)
    return SnapshotContext(review=review, workspace=workspace, events=events)


def _report_kwargs(**overrides) -> dict:
    kwargs = dict(
        domain="execution",
        status="complete",
        summary="ok",
        metrics={},
        observations=[],
        coverage=Coverage(records_examined=0),
        analyzer_version="test-v1",
        input_hash="0" * 64,
    )
    kwargs.update(overrides)
    return kwargs


# ── contract ───────────────────────────────────────────────────────────

def test_forbidden_key_in_metrics_raises():
    with pytest.raises(ValidationError, match="forbidden key"):
        ConsolidationReportModel(**_report_kwargs(
            metrics={"stats": {"Recommendation": "do X"}},
        ))


def test_forbidden_key_in_observation_nested_payload_raises():
    # Observations are strict models; the blacklist still guards nested
    # payloads reached through relationships.
    with pytest.raises(ValidationError, match="forbidden key"):
        ConsolidationReportModel(**_report_kwargs(
            relationships=[{"kind": "x", "meta": {"next_step": "pause it"}}],
        ))


def test_forbidden_human_metric_raises_only_for_human_domain():
    metrics = {"per_role": {"response_time": 12.5}}
    with pytest.raises(ValidationError, match="forbidden key"):
        ConsolidationReportModel(**_report_kwargs(
            domain="human_participation", metrics=metrics,
        ))
    # Any other domain may legitimately use e.g. system response_time.
    report = ConsolidationReportModel(**_report_kwargs(
        domain="execution", metrics=metrics,
    ))
    assert report.metrics == metrics


def test_extra_attribute_forbidden():
    with pytest.raises(ValidationError):
        ConsolidationReportModel(**_report_kwargs(recommendation="be bold"))
    with pytest.raises(ValidationError):
        Observation(type="x", description="y", advice="do it")


# ── run_all: seeded workspace → 8 complete rows ────────────────────────

async def _seed_business_rows(db, workspace: Workspace) -> None:
    goal = Goal(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        title="Followers", metric_key="follower_count",
        target_value=1000, status="active",
    )
    task = Task(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        title="Write post", status="completed",
        owner_service_key="content_creator",
    )
    job = ScheduledJob(
        job_id=f"job_{workspace.id}", entity_id=ENTITY_ID,
        workspace_id=workspace.id, name="Daily digest",
        schedule_kind="every", every_seconds=3600, enabled=True,
    )
    approval = HitlRequest(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        action_key="external.publish", origin_kind="step",
        status="pending", dedup_key=f"dk_{workspace.id}",
    )
    db.add_all([goal, task, job, approval])
    await db.flush()

    await _emit(db, workspace.id, event_type=et.EXECUTION_STARTED, source_id=task.id)
    await _emit(db, workspace.id, event_type=et.EXECUTION_COMPLETED, source_id=task.id)
    await _emit(
        db, workspace.id, event_type=et.GOAL_MEASURED, source_kind="goal",
        source_id=goal.id, payload={"value": 120.0},
    )
    await _emit(
        db, workspace.id, event_type=et.AUTOMATION_RUN_DISPATCHED,
        source_kind="scheduled_job", source_id=job.id,
    )
    await _emit(
        db, workspace.id, event_type=et.APPROVAL_REQUESTED, source_kind="approval",
        source_id=approval.id, payload={"action_key": approval.action_key},
    )
    await _emit(
        db, workspace.id, event_type=et.ARTIFACT_CREATED, source_kind="artifact",
        source_id="doc_1",
    )


async def test_run_all_produces_eight_complete_reports(db_session):
    workspace = await _workspace(db_session)
    await _seed_business_rows(db_session, workspace)
    review = await _begin(db_session, workspace.id)

    rows = await run_all(db_session, review)

    assert {row.domain for row in rows} == ALL_DOMAINS
    assert len(rows) == 8
    for row in rows:
        assert row.status == "complete", f"{row.domain}: {row.summary}"
        assert row.review_id == review.id
        assert row.workspace_id == workspace.id
        assert row.input_hash == compute_input_hash(
            row.domain, row.analyzer_version, review,
        )
        assert row.coverage is not None
        assert row.coverage["records_examined"] >= 0
        assert row.coverage["reused"] is False
        # human_participation: v2 with M9 (real commitment counts), v3 with
        # the opt-in L1 edit-pattern layer, v4 with the governance /
        # information HITL split; execution: v2 with the opt-in L1
        # failure-cluster layer; risk_governance: v2 with the same HITL
        # split. Everything else is still pure L0 v1.
        expected_version = {
            "human_participation": "-consolidator-v4",
            "execution": "-consolidator-v2",
            "risk_governance": "-consolidator-v2",
        }.get(row.domain, "-consolidator-v1")
        assert row.analyzer_version.endswith(expected_version)
        assert row.scope == {}

    # Rows are actually persisted.
    persisted = list((await db_session.execute(
        select(ConsolidationReport).where(ConsolidationReport.review_id == review.id)
    )).scalars().all())
    assert len(persisted) == 8


# ── failure isolation ──────────────────────────────────────────────────

async def test_raising_consolidator_persists_failed_row_others_complete(
    db_session, monkeypatch,
):
    workspace = await _workspace(db_session)
    await _seed_business_rows(db_session, workspace)
    review = await _begin(db_session, workspace.id)

    async def boom(db, ctx):
        raise RuntimeError("learning blew up")

    monkeypatch.setattr(REGISTRY["learning_evidence"], "run", boom)

    rows = await run_all(db_session, review)
    by_domain = {row.domain: row for row in rows}
    assert by_domain["learning_evidence"].status == "failed"
    assert "learning blew up" in by_domain["learning_evidence"].summary
    assert by_domain["learning_evidence"].coverage["records_examined"] == 0
    for domain in ALL_DOMAINS - {"learning_evidence"}:
        assert by_domain[domain].status == "complete"


# ── input_hash cache ───────────────────────────────────────────────────

async def test_cache_reuses_previous_succeeded_reports_without_running(
    db_session, monkeypatch,
):
    workspace = await _workspace(db_session)
    event = await _emit(db_session, workspace.id)

    # First review over the frozen snapshot — runs for real, then succeeds.
    # Rows are built manually (not via begin/complete_review) so the second
    # review can share the exact same watermarks: lifecycle ledger facts
    # would otherwise grow the window and change the hash.
    review_a = ReviewRun(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger_kind="scheduled", status="running",
        watermark_start=None, watermark_end=event.id,
        workspace_revision=0, policy_revision=None,
        created_at=_utcnow(),
    )
    db_session.add(review_a)
    await db_session.flush()
    rows_a = await run_all(db_session, review_a)
    assert all(row.status == "complete" for row in rows_a)
    review_a.status = "succeeded"
    await db_session.flush()

    # Identical snapshot → identical input_hash → cache hit; running any
    # consolidator now is a test failure.
    review_b = ReviewRun(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        trigger_kind="manual_retry", status="running",
        watermark_start=None, watermark_end=event.id,
        workspace_revision=0, policy_revision=None,
        created_at=_utcnow(),
    )
    db_session.add(review_b)
    await db_session.flush()

    async def must_not_run(db, ctx):
        raise AssertionError("consolidator ran despite cache hit")

    for consolidator in REGISTRY.values():
        monkeypatch.setattr(consolidator, "run", must_not_run)

    rows_b = await run_all(db_session, review_b)
    assert {row.domain for row in rows_b} == ALL_DOMAINS
    for row in rows_b:
        assert row.review_id == review_b.id
        assert row.status == "complete"
        assert row.coverage["reused"] is True


# ── read-only discipline ───────────────────────────────────────────────

async def test_consolidator_run_is_read_only(db_session):
    workspace = await _workspace(db_session)
    await _seed_business_rows(db_session, workspace)
    review = await _begin(db_session, workspace.id)
    ctx = await _ctx(db_session, review)
    await db_session.flush()  # start from a clean session

    violations: list[str] = []

    def before_flush(session, flush_context, instances):
        if session.new or session.dirty or session.deleted:
            violations.append(
                f"flush with pending changes during consolidator.run: "
                f"new={list(session.new)} dirty={list(session.dirty)} "
                f"deleted={list(session.deleted)}"
            )

    sync_session = db_session.sync_session
    sa_event.listen(sync_session, "before_flush", before_flush)
    try:
        for domain in ("goal", "execution"):
            report = await REGISTRY[domain].run(db_session, ctx)
            assert report.status == "complete"
    finally:
        sa_event.remove(sync_session, "before_flush", before_flush)

    assert violations == []
    assert not db_session.new and not db_session.dirty and not db_session.deleted


# ── targeted observations ──────────────────────────────────────────────

async def test_failure_streak_observation_fires(db_session):
    workspace = await _workspace(db_session)
    job = ScheduledJob(
        job_id=f"job_streak_{workspace.id}", entity_id=ENTITY_ID,
        workspace_id=workspace.id, name="Flaky sync",
        schedule_kind="every", every_seconds=60,
        enabled=True, consecutive_errors=4,
    )
    db_session.add(job)
    await db_session.flush()
    await _emit(
        db_session, workspace.id, event_type=et.AUTOMATION_RUN_FAILED,
        source_kind="scheduled_job", source_id=job.id, status="failed",
    )

    review = await _begin(db_session, workspace.id)
    report = await REGISTRY["automation_portfolio"].run(
        db_session, await _ctx(db_session, review),
    )

    streaks = [o for o in report.observations if o.type == "failure_streak"]
    assert len(streaks) == 1
    assert f"scheduled_job:{job.id}" in streaks[0].evidence_refs


async def test_pace_degraded_observation_fires(db_session):
    workspace = await _workspace(db_session)
    goal = Goal(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        title="MRR", metric_key="mrr", target_value=5000,
        status="active", pace_status="behind",
    )
    db_session.add(goal)
    await db_session.flush()
    pace_event = await _emit(
        db_session, workspace.id, event_type=et.GOAL_PACE_CHANGED,
        source_kind="goal", source_id=goal.id,
        payload={"old_pace": "on_track", "new_pace": "behind"},
    )

    review = await _begin(db_session, workspace.id)
    report = await REGISTRY["goal"].run(db_session, await _ctx(db_session, review))

    degraded = [o for o in report.observations if o.type == "pace_degraded"]
    assert len(degraded) == 1
    assert degraded[0].evidence_refs == [pace_event.id]


async def test_approval_bottleneck_observation_fires(db_session):
    workspace = await _workspace(db_session)
    stale = HitlRequest(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        action_key="external.publish", origin_kind="step",
        status="pending", dedup_key=f"dk_stale_{workspace.id}",
        created_at=_utcnow() - timedelta(hours=48),
    )
    fresh = HitlRequest(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        action_key="external.reply", origin_kind="step",
        status="pending", dedup_key=f"dk_fresh_{workspace.id}",
    )
    db_session.add_all([stale, fresh])
    await db_session.flush()

    review = await _begin(db_session, workspace.id)
    report = await REGISTRY["human_participation"].run(
        db_session, await _ctx(db_session, review),
    )

    bottlenecks = [o for o in report.observations if o.type == "approval_bottleneck"]
    assert len(bottlenecks) == 1
    assert bottlenecks[0].evidence_refs == [f"approval_request:{stale.id}"]
    assert report.metrics["open_approvals"] == 2


# ── unique (review_id, domain) ─────────────────────────────────────────

async def test_unique_review_domain_enforced(db_session):
    workspace = await _workspace(db_session)
    review = await _begin(db_session, workspace.id)

    def _row():
        return ConsolidationReport(
            entity_id=ENTITY_ID, workspace_id=workspace.id, review_id=review.id,
            domain="goal", scope={}, status="complete", summary="x",
            analyzer_version="goal-consolidator-v1", input_hash="a" * 64,
        )

    db_session.add(_row())
    await db_session.flush()
    db_session.add(_row())
    with pytest.raises(IntegrityError):
        await db_session.flush()
