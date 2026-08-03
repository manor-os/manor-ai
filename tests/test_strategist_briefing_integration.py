"""M5/M6 — briefing-driven Strategist review cycle (strategist_review_v2).

Flag ON (end-to-end-ish through the celery task's inner cycle):
* ReviewRun row reaches ``succeeded`` with ``briefing`` JSONB populated
* 8 ``consolidation_reports`` rows persisted for the review
* the captured LLM user prompt is briefing-driven ("## Coverage gaps",
  review id, "# Review briefing" section) and the system prompt carries
  the empty-proposal legitimization paragraph
* an EMPTY proposal still succeeds the review and advances the watermark
  across consecutive reviews (空 proposal 一等化)

Flag OFF:
* prompts are the legacy ones — no briefing markers anywhere, and no
  ReviewRun / consolidation rows are written.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.services import feature_flags as feature_flags_service
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_seq = 0


async def _seed_workspace(db) -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Briefing Integration WS",
        status="active",
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
    )
    db.add_all([workspace, goal])
    await db.commit()
    return workspace


async def _emit(db, workspace: Workspace, *, event_type: str = et.EXECUTION_COMPLETED):
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=event_type,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"briefing-integration:{workspace.id}:{_seq}",
    )
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)  # ULID ordering across distinct milliseconds
    return event


async def _set_flag(db, enabled: bool) -> None:
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == FLAG_KEY)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(
            key=FLAG_KEY,
            description="test",
            default_enabled=enabled,
        ))
    else:
        flag.default_enabled = enabled
    await db.commit()
    # is_enabled has a 60s in-process cache; other tests may have primed it.
    feature_flags_service._bump_cache()


def _fake_completion(captured: list):
    async def fake_runtime_execute_strategist_completion(system_prompt, user_prompt, **kwargs):
        captured.append({"system": system_prompt, "user": user_prompt})
        # No review_id key: the parser injects the caller's review_id.
        return SimpleNamespace(content=json.dumps({
            "summary": "All domains healthy; no action needed this cycle.",
            "tasks": [],
        }))
    return fake_runtime_execute_strategist_completion


async def _reviews(db, workspace_id: str) -> list[ReviewRun]:
    return list((await db.execute(
        select(ReviewRun)
        .where(ReviewRun.workspace_id == workspace_id)
        .order_by(ReviewRun.id.asc())
    )).scalars().all())


# ── flag ON ────────────────────────────────────────────────────────────

async def test_flag_on_briefing_driven_review_succeeds_and_advances_watermark(
    db_session, monkeypatch,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _emit(db_session, workspace, event_type=et.EXECUTION_STARTED)
    await _emit(db_session, workspace)
    await _set_flag(db_session, True)

    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(captured),
    )

    result = await _execute_strategist_review_cycle(
        db_session, workspace.id, "scheduled",
    )

    assert not result.get("skipped")
    assert result["task_count"] == 0

    reviews = await _reviews(db_session, workspace.id)
    assert len(reviews) == 1
    review = reviews[0]
    # Empty proposal is a first-class good outcome: the review SUCCEEDED.
    assert review.status == "succeeded"
    assert review.watermark_end is not None

    # The proposal cohort is tagged with the ReviewRun id.
    assert result["review_id"] == review.id

    # Briefing frozen onto the row, one digest per domain.
    assert isinstance(review.briefing, dict)
    assert len(review.briefing["reports"]) == 8
    assert review.briefing["review"]["id"] == review.id

    # 8 consolidation report rows persisted for this review.
    report_rows = list((await db_session.execute(
        select(ConsolidationReport).where(ConsolidationReport.review_id == review.id)
    )).scalars().all())
    assert len(report_rows) == 8

    # Captured prompts: briefing-driven user prompt + v2 system paragraph.
    assert len(captured) == 1
    user_prompt = captured[0]["user"]
    assert "# Review briefing (deterministic domain reports)" in user_prompt
    assert "## Coverage gaps" in user_prompt
    assert "## Review window" in user_prompt
    assert review.id in user_prompt
    # Legacy heavy sections are superseded by the briefing.
    assert "# Recent tasks (last 30d)" not in user_prompt
    assert "# Recent workspace activity" not in user_prompt
    assert "# Your calibration so far" not in user_prompt

    system_prompt = captured[0]["system"]
    assert "Empty proposals are a first-class good outcome" in system_prompt
    assert "Coverage gaps" in system_prompt

    # ── second review: the watermark advances past the first one ──────
    await _emit(db_session, workspace)
    result2 = await _execute_strategist_review_cycle(
        db_session, workspace.id, "scheduled",
    )
    assert not result2.get("skipped")

    reviews = await _reviews(db_session, workspace.id)
    assert len(reviews) == 2
    second = reviews[1]
    assert second.status == "succeeded"
    assert second.watermark_start == review.watermark_end
    assert second.watermark_end is not None
    assert second.watermark_end > review.watermark_end
    assert isinstance(second.briefing, dict)


# ── flag OFF ───────────────────────────────────────────────────────────

async def test_flag_off_keeps_legacy_prompt_and_writes_no_review_rows(
    db_session, monkeypatch,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, False)

    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(captured),
    )

    result = await _execute_strategist_review_cycle(
        db_session, workspace.id, "scheduled",
    )

    assert not result.get("skipped")
    assert result["task_count"] == 0
    # Legacy review ids keep the rv_ prefix (no ReviewRun row backing them).
    assert result["review_id"].startswith("rv_")

    # No ReviewRun / consolidation rows on the legacy path.
    assert await _reviews(db_session, workspace.id) == []
    report_rows = list((await db_session.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.workspace_id == workspace.id
        )
    )).scalars().all())
    assert report_rows == []

    # Prompts carry zero briefing markers and keep the legacy sections.
    assert len(captured) == 1
    user_prompt = captured[0]["user"]
    assert "# Review briefing" not in user_prompt
    assert "## Coverage gaps" not in user_prompt
    assert "## Review window" not in user_prompt
    assert "# Recent tasks (last 30d)" in user_prompt

    system_prompt = captured[0]["system"]
    assert "Empty proposals are a first-class good outcome" not in system_prompt
