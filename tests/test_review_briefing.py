"""M5 ReviewBriefing — deterministic assembly + markdown rendering.

Covers:
* build_briefing over a seeded workspace + run_all → 8 report digests,
  review meta, strategist_template pass-through
* observations Top-K (K=10, evidence-count ranked) + omitted counter
* coverage_gaps: failed/partial domains + "(reused cached)" entries
* previous_decisions derived from proposal_item_* ledger facts
  (approved + rejected with rejection_reason), newest first
* open_approvals digest (pending only, max 10, oldest first)
* size budget: oversized metric values dropped, then Top-K reduced;
  hard cap raises BriefingTooLarge
* render_briefing_markdown: deterministic, section headers, evidence
  suffixes, coverage-gap warning sentence
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.consolidators import run_all
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.review import begin_review
from packages.core.review.briefing import (
    BriefingTooLarge,
    HARD_LIMIT_CHARS,
    ReviewBriefingModel,
    build_briefing,
)
from packages.core.review.briefing_render import (
    COVERAGE_GAP_WARNING,
    render_briefing_markdown,
)

ENTITY_ID = "01BRIEFENTITY0000000000000"

ALL_DOMAINS = {
    "goal", "execution", "automation_portfolio", "artifact_knowledge",
    "human_participation", "capacity_cost", "risk_governance", "learning_evidence",
}

_seq = 0


async def _workspace(db, *, operating_model: dict | None = None) -> Workspace:
    workspace = Workspace(
        entity_id=ENTITY_ID,
        name="Briefing WS",
        operating_model=operating_model or {},
    )
    db.add(workspace)
    await db.flush()
    return workspace


async def _emit(db, workspace_id: str, **overrides):
    global _seq
    _seq += 1
    kwargs = dict(
        entity_id=ENTITY_ID,
        workspace_id=workspace_id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=f"src_{_seq}",
        idempotency_key=f"briefing-test:{workspace_id}:{_seq}",
    )
    kwargs.update(overrides)
    event = await record_event(db, **kwargs)
    assert event is not None
    await asyncio.sleep(0.002)  # ULID ordering across distinct milliseconds
    return event


async def _begin(db, workspace_id: str) -> ReviewRun:
    return await begin_review(
        db, entity_id=ENTITY_ID, workspace_id=workspace_id, trigger="scheduled",
    )


def _report_row(review: ReviewRun, *, domain: str = "execution", **overrides) -> ConsolidationReport:
    kwargs = dict(
        entity_id=review.entity_id,
        workspace_id=review.workspace_id,
        review_id=review.id,
        domain=domain,
        scope={},
        status="complete",
        summary=f"{domain} ok",
        metrics={},
        observations=[],
        relationships=[],
        uncertainties=[],
        evidence_refs=[],
        coverage={"records_examined": 1, "records_missing_details": 0, "reused": False},
        analyzer_version="test-v1",
        input_hash="0" * 64,
    )
    kwargs.update(overrides)
    return ConsolidationReport(**kwargs)


def _observation(index: int, evidence_count: int) -> dict:
    return {
        "type": f"obs_{index}",
        "description": f"observation number {index}",
        "evidence_refs": [f"ev_{index}_{j}" for j in range(evidence_count)],
        "baseline": False,
    }


# ── build_briefing over a real run_all ─────────────────────────────────

async def test_build_briefing_full_workspace(db_session):
    workspace = await _workspace(
        db_session,
        operating_model={"strategist": {"proposal_shape": {"max_tasks_per_cycle": 3}}},
    )

    # Prior proposal decisions in the ledger (must land inside the window).
    await _emit(
        db_session, workspace.id,
        event_type=et.PROPOSAL_ITEM_APPROVED, source_kind="proposal",
        source_id="task_approved_1", causation_id="rv_prior",
        payload={"title": "Approved task"},
    )
    await _emit(
        db_session, workspace.id,
        event_type=et.PROPOSAL_ITEM_REJECTED, source_kind="proposal",
        source_id="task_rejected_1", causation_id="rv_prior",
        payload={"title": "Rejected task", "rejection_reason": "too risky"},
    )
    await _emit(db_session, workspace.id, event_type=et.EXECUTION_COMPLETED)

    # Open approvals: two pending + one granted (excluded).
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    db_session.add_all([
        HitlRequest(
            entity_id=ENTITY_ID, workspace_id=workspace.id,
            action_key="external.publish", origin_kind="step",
            risk_level="high", status="pending", reason="external side effect",
            dedup_key=f"dk1_{workspace.id}", created_at=old,
        ),
        HitlRequest(
            entity_id=ENTITY_ID, workspace_id=workspace.id,
            action_key="automation.create", origin_kind="step",
            risk_level="medium", status="pending",
            dedup_key=f"dk2_{workspace.id}",
        ),
        HitlRequest(
            entity_id=ENTITY_ID, workspace_id=workspace.id,
            action_key="doc.write", origin_kind="step",
            status="granted", dedup_key=f"dk3_{workspace.id}",
        ),
    ])
    await db_session.flush()

    review = await _begin(db_session, workspace.id)
    report_rows = await run_all(db_session, review)

    briefing = await build_briefing(db_session, review, report_rows)

    assert isinstance(briefing, ReviewBriefingModel)
    assert set(briefing.reports) == ALL_DOMAINS
    assert briefing.review["id"] == review.id
    assert briefing.review["trigger_kind"] == "scheduled"
    assert briefing.review["watermark_end"] == review.watermark_end
    assert briefing.strategist_template == {"proposal_shape": {"max_tasks_per_cycle": 3}}

    # previous_decisions: newest first, rejected carries the reason.
    assert [d.decision for d in briefing.previous_decisions] == ["rejected", "approved"]
    rejected = briefing.previous_decisions[0]
    assert rejected.task_id == "task_rejected_1"
    assert rejected.review_id == "rv_prior"
    assert rejected.reason == "too risky"
    assert rejected.decided_at is not None
    approved = briefing.previous_decisions[1]
    assert approved.task_id == "task_approved_1"
    assert approved.reason is None

    # open approvals: pending only, oldest first, digest fields present.
    assert len(briefing.open_approvals) == 2
    first = briefing.open_approvals[0]
    assert first["action_key"] == "external.publish"
    assert first["risk_level"] == "high"
    assert first["reason"] == "external side effect"
    assert first["age_hours"] == pytest.approx(30.0, abs=0.5)
    assert {a["action_key"] for a in briefing.open_approvals} == {
        "external.publish", "automation.create",
    }

    # Briefing is JSON-serializable as-is (what the caller persists).
    dumped = briefing.model_dump(mode="json")
    import json
    json.dumps(dumped)


# ── top-K + coverage gaps ──────────────────────────────────────────────

async def test_observations_top_k_enforced(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)

    observations = [_observation(i, evidence_count=i) for i in range(15)]
    row = _report_row(review, domain="execution", observations=observations)

    briefing = await build_briefing(db_session, review, [row])

    digest = briefing.reports["execution"]
    assert len(digest.observations) == 10
    assert digest.observations_omitted == 5
    # Ranked by evidence count descending → indexes 14..5 survive.
    kept_types = [obs["type"] for obs in digest.observations]
    assert kept_types == [f"obs_{i}" for i in range(14, 4, -1)]


async def test_coverage_gaps_failed_partial_and_reused(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)

    rows = [
        _report_row(review, domain="goal", status="failed", summary="boom"),
        _report_row(review, domain="execution", status="partial"),
        _report_row(review, domain="capacity_cost", status="complete", coverage={
            "records_examined": 3, "records_missing_details": 0, "reused": True,
        }),
        _report_row(review, domain="risk_governance", status="complete"),
    ]

    briefing = await build_briefing(db_session, review, rows)

    assert briefing.coverage_gaps == [
        "goal", "execution", "capacity_cost (reused cached)",
    ]
    assert briefing.reports["capacity_cost"].reused is True
    assert briefing.reports["risk_governance"].reused is False


# ── size budget ────────────────────────────────────────────────────────

async def test_size_budget_drops_oversized_metrics(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)

    row = _report_row(review, domain="execution", metrics={
        "huge_blob": "x" * 40_000,        # single value > 2000 json chars
        "success_rate": 0.93,             # small value must survive
    })

    briefing = await build_briefing(db_session, review, [row])

    digest = briefing.reports["execution"]
    assert "huge_blob" not in digest.metrics
    assert digest.metrics["success_rate"] == 0.93
    assert len(briefing.model_dump_json()) < 48_000


async def test_size_budget_reduces_top_k_after_metric_drop(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)

    # No single metric is oversized, but 10 observations x ~3.4k chars keep
    # the briefing above the 32k soft limit → the second truncation stage
    # (Top-K 10 → 5) must kick in.
    observations = [
        {
            "type": f"obs_{i}",
            "description": "d" * 3_400,
            "evidence_refs": [f"ev_{i}"],
            "baseline": False,
        }
        for i in range(12)
    ]
    row = _report_row(review, domain="execution", observations=observations)

    briefing = await build_briefing(db_session, review, [row])

    digest = briefing.reports["execution"]
    assert len(digest.observations) == 5
    assert digest.observations_omitted == 7  # 2 from top-K + 5 from reduction
    assert len(briefing.model_dump_json()) < 48_000


async def test_size_budget_hard_cap_raises(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)

    # An untruncatable field (summary) blows past the hard cap.
    row = _report_row(review, domain="execution", summary="s" * (HARD_LIMIT_CHARS + 1000))

    with pytest.raises(BriefingTooLarge):
        await build_briefing(db_session, review, [row])


# ── markdown rendering ─────────────────────────────────────────────────

async def test_render_briefing_markdown_deterministic_and_complete(db_session):
    workspace = await _workspace(db_session)
    await _emit(
        db_session, workspace.id,
        event_type=et.PROPOSAL_ITEM_REJECTED, source_kind="proposal",
        source_id="task_r", causation_id="rv_prev",
        payload={"rejection_reason": "not aligned"},
    )
    db_session.add(HitlRequest(
        entity_id=ENTITY_ID, workspace_id=workspace.id,
        action_key="external.publish", origin_kind="step",
        risk_level="high", status="pending",
        dedup_key=f"dk_render_{generate_ulid()}",
    ))
    await db_session.flush()
    review = await _begin(db_session, workspace.id)

    rows = [
        _report_row(
            review, domain="execution",
            metrics={"tasks_completed": 4, "small_map": {"a": 1}},
            observations=[
                {
                    "type": "failure_streak",
                    "description": "task X failed 3 times",
                    "evidence_refs": ["ev_1", "ev_2"],
                    "baseline": True,
                },
            ],
            uncertainties=[{"code": "missing_details", "description": "2 rows lacked output"}],
        ),
        _report_row(review, domain="goal", status="failed", summary="loader crashed"),
    ]

    briefing = await build_briefing(db_session, review, rows)

    text_a = render_briefing_markdown(briefing)
    text_b = render_briefing_markdown(briefing)
    assert text_a == text_b  # deterministic

    assert "## Review window" in text_a
    assert review.id in text_a
    assert "## Execution report" in text_a
    assert "## Goal report" in text_a
    assert "- tasks_completed: 4" in text_a
    assert '- small_map: {"a": 1}' in text_a
    assert "[evidence: ev_1,ev_2]" in text_a
    assert "(baseline)" in text_a
    assert "[missing_details] 2 rows lacked output" in text_a

    assert "## Coverage gaps" in text_a
    assert "- goal" in text_a
    assert COVERAGE_GAP_WARNING in text_a
    assert "缺失域的数据不可用,不得对其提出高风险变更" in text_a

    assert "## Open approvals" in text_a
    assert "action=external.publish" in text_a
    assert "## Previous proposal decisions" in text_a
    assert "reason: not aligned" in text_a

    # No advice/recommendation vocabulary leaks into the rendering.
    lowered = text_a.lower()
    for banned in ("recommend", "should ", "advice", "suggested_action"):
        assert banned not in lowered


async def test_render_briefing_markdown_no_gaps_placeholder(db_session):
    workspace = await _workspace(db_session)
    await _emit(db_session, workspace.id)
    review = await _begin(db_session, workspace.id)
    briefing = await build_briefing(
        db_session, review, [_report_row(review, domain="execution")],
    )

    text = render_briefing_markdown(briefing)
    assert "## Coverage gaps" in text
    assert COVERAGE_GAP_WARNING not in text  # no gaps → no warning needed
