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
  consolidation rows are written.
* the durable ReviewRun blocks configuration upgrades while the legacy
  model call runs, then records the terminal receipt.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.strategist_helpers import proposal_with_execution_review

from packages.core.blueprints.upgrade_activity import active_runs
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.humans import open_commitment, resolve_commitment
from packages.core.models.proposal import ProposalRecord
from packages.core.models.review_run import ReviewRun
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.review.service import latest_succeeded_review
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist.prompt import StrategistProposalGenerationError
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
        captured.append({"system": system_prompt, "user": user_prompt, **kwargs})
        # No review_id key: the parser injects the caller's review_id.
        return SimpleNamespace(content=json.dumps(proposal_with_execution_review({
            "summary": "All domains healthy; no action needed this cycle.",
            "tasks": [],
        }, user_prompt)))
    return fake_runtime_execute_strategist_completion


async def _reviews(db, workspace_id: str) -> list[ReviewRun]:
    return list((await db.execute(
        select(ReviewRun)
        .where(ReviewRun.workspace_id == workspace_id)
        .order_by(ReviewRun.id.asc())
    )).scalars().all())


@pytest.mark.parametrize("briefing_enabled", [False, True])
async def test_execution_reference_mismatch_fails_review_without_empty_proposal_or_watermark(
    db_session, monkeypatch, briefing_enabled,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, briefing_enabled)
    captured = []

    async def mismatched_completion(system_prompt, user_prompt, **kwargs):
        captured.append(user_prompt)
        payload = proposal_with_execution_review({
            "summary": "No new action proposed.", "tasks": [],
        }, user_prompt)
        payload["execution_review"]["revision"] += "-stale"
        return SimpleNamespace(content=json.dumps(payload))

    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        mismatched_completion,
    )
    with pytest.raises(StrategistProposalGenerationError) as caught:
        await _execute_strategist_review_cycle(db_session, workspace.id, "scheduled")

    assert caught.value.code == "proposal_execution_evidence_mismatch"
    assert caught.value.details["attempts"] == 2
    assert len(captured) == 2
    assert "proposal_execution_evidence_mismatch" in captured[1]
    reviews = await _reviews(db_session, workspace.id)
    assert len(reviews) == 1
    assert reviews[0].status == "failed"
    assert reviews[0].error
    assert await latest_succeeded_review(db_session, workspace.id) is None
    assert not (await db_session.execute(select(ProposalRecord.id).where(
        ProposalRecord.review_id == reviews[0].id,
    ))).all()
    assert not (await db_session.execute(select(Task.id).where(
        Task.workspace_id == workspace.id,
    ))).all()


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
        user_id="review-requester",
    )

    assert not result.get("skipped")
    assert captured[0]["user_id"] == "review-requester"
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


async def test_declined_human_input_is_visible_to_next_review(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    commitment = await open_commitment(
        db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="chat", source_id="optional-artwork",
        expected_input="Do you want optional cover artwork?",
    )
    await resolve_commitment(
        db_session, commitment, status="declined", response={"choice": "skip"},
    )
    await db_session.commit()
    await _set_flag(db_session, True)
    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(captured),
    )

    await _execute_strategist_review_cycle(db_session, workspace.id, "event")

    assert len(captured) == 1
    assert "Do you want optional cover artwork?" in captured[0]["user"]
    assert "declined" in captured[0]["user"]


async def test_fulfilled_human_input_is_visible_to_next_review(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    commitment = await open_commitment(
        db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
        request_kind="input", source_kind="chat", source_id="release-materials",
        expected_input="Provide release materials",
    )
    await resolve_commitment(
        db_session, commitment, status="fulfilled",
        response={"text": "The track is instrumental", "choice": "respond"},
    )
    await db_session.commit()
    await _set_flag(db_session, True)
    captured: list[dict] = []
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(captured),
    )

    await _execute_strategist_review_cycle(db_session, workspace.id, "event")

    assert len(captured) == 1
    assert "Provide release materials" in captured[0]["user"]
    assert "The track is instrumental" in captured[0]["user"]


# ── flag OFF ───────────────────────────────────────────────────────────

async def test_flag_off_keeps_legacy_prompt_but_claims_durable_admission_before_model_call(
    db_session, monkeypatch,
):
    workspace = await _seed_workspace(db_session)
    await _emit(db_session, workspace)
    await _set_flag(db_session, False)

    captured: list[dict] = []
    complete = _fake_completion(captured)

    async def checked_completion(system_prompt, user_prompt, **kwargs):
        async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as observer:
            reviews = await _reviews(observer, workspace.id)
            assert len(reviews) == 1
            assert reviews[0].status == "running"
            assert reviews[0].lease_owner
            assert reviews[0].briefing is None
            assert reviews[0].id in {
                row["id"] for row in await active_runs(observer, workspace=workspace)
            }
        return await complete(system_prompt, user_prompt, **kwargs)

    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        checked_completion,
    )

    result = await _execute_strategist_review_cycle(
        db_session, workspace.id, "human_requested",
    )

    assert not result.get("skipped")
    assert result["task_count"] == 0
    reviews = await _reviews(db_session, workspace.id)
    assert len(reviews) == 1
    assert result["review_id"] == reviews[0].id
    assert reviews[0].status == "succeeded"
    # Legacy mode still has no domain briefing, but the common execution
    # observation receipt is retained for audit on both generation paths.
    assert set(reviews[0].briefing) == {"execution_evidence"}
    assert reviews[0].briefing["execution_evidence"]["source"] == "server_observation"
    assert reviews[0].lease_owner is None
    assert not await active_runs(db_session, workspace=workspace)

    # Consolidation/briefing behavior remains exclusive to the v2 flag.
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
