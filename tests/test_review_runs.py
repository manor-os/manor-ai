"""M2 ReviewRun / Snapshot / Watermark — review lifecycle service.

Covers:
* begin_review freezes watermark_end = max event id (single-running lock)
* watermark chain: only succeeded reviews advance the watermark
* skipped reviews (裁定 C): row kept, watermark_end forced back to start
* failed reviews: next begin_review re-consumes the same window
* events_in_window boundary semantics (exclusive start, inclusive end)
* ledger facts written for every lifecycle transition
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.governance import GovernancePolicy
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.review import (
    ReviewAlreadyRunning,
    begin_review,
    complete_review,
    events_in_window,
    fail_review,
    latest_succeeded_review,
    mark_review_skipped,
)


ENTITY_ID = "01REVIEWENTITY000000000000"
WORKSPACE_ID = "01REVIEWWORKSPACE000000000"

_seq = 0


async def _emit(db, *, workspace_id: str = WORKSPACE_ID, event_type: str = et.EXECUTION_COMPLETED) -> WorkspaceEvent:
    """Append one ledger event with a unique idempotency key."""
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=ENTITY_ID,
        workspace_id=workspace_id,
        event_type=event_type,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"review-test:{workspace_id}:{_seq}",
    )
    assert event is not None
    # ULID ordering is only guaranteed across distinct milliseconds.
    await asyncio.sleep(0.002)
    return event


async def _begin(db, *, workspace_id: str = WORKSPACE_ID, trigger_kind: str = "scheduled") -> ReviewRun:
    return await begin_review(
        db, entity_id=ENTITY_ID, workspace_id=workspace_id, trigger=trigger_kind,
    )


async def _review_ledger_rows(db, review_id: str) -> list[WorkspaceEvent]:
    return list((
        await db.execute(
            select(WorkspaceEvent)
            .where(
                WorkspaceEvent.source_kind == "review",
                WorkspaceEvent.source_id == review_id,
            )
            .order_by(WorkspaceEvent.id.asc())
        )
    ).scalars().all())


# ── begin_review: freeze + single-running lock ─────────────────────────

async def test_begin_review_freezes_watermark_end_at_max_event_id(db_session):
    e1 = await _emit(db_session)
    e2 = await _emit(db_session)

    review = await _begin(db_session)

    assert review.status == "running"
    assert review.watermark_start is None  # no succeeded review yet → genesis
    assert review.watermark_end == e2.id
    assert review.window_start is None
    assert review.window_end == e2.occurred_at
    assert e1.id < e2.id


async def test_second_begin_while_running_raises_already_running(db_session):
    first = await _begin(db_session)
    assert first.status == "running"

    with pytest.raises(ReviewAlreadyRunning):
        await _begin(db_session, trigger_kind="work_batch_completed")

    # The SAVEPOINT must keep the caller's transaction usable.
    still_there = await db_session.get(ReviewRun, first.id)
    assert still_there is not None and still_there.status == "running"


async def test_begin_succeeds_again_after_fail_review(db_session):
    first = await _begin(db_session)
    await fail_review(db_session, first, error="boom")

    second = await _begin(db_session)
    assert second.id != first.id
    assert second.status == "running"


async def test_other_workspace_running_review_does_not_block(db_session):
    await _begin(db_session)
    other = await _begin(db_session, workspace_id="01REVIEWWORKSPACEOTHER0000")
    assert other.status == "running"


async def test_begin_review_freezes_workspace_and_policy_revisions(db_session):
    workspace = Workspace(entity_id=ENTITY_ID, name="Review WS", operation_revision=7)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(GovernancePolicy(
        workspace_id=workspace.id, entity_id=ENTITY_ID, policy={}, revision=3,
    ))
    await db_session.flush()

    review = await _begin(db_session, workspace_id=workspace.id)
    assert review.workspace_revision == 7
    assert review.policy_revision == 3


async def test_begin_review_without_workspace_or_policy_rows_freezes_none(db_session):
    review = await _begin(db_session)
    assert review.workspace_revision is None
    assert review.policy_revision is None


# ── watermark chain ────────────────────────────────────────────────────

async def test_watermark_chain_across_succeeded_reviews(db_session):
    e1 = await _emit(db_session)

    review_a = await _begin(db_session)
    assert review_a.watermark_end == e1.id
    await complete_review(db_session, review_a)

    e2 = await _emit(db_session)
    e3 = await _emit(db_session)

    review_b = await _begin(db_session)
    assert review_b.watermark_start == e1.id
    assert review_b.watermark_end == e3.id
    assert review_b.window_start == e1.occurred_at
    assert review_b.window_end == e3.occurred_at

    window = await events_in_window(db_session, review_b)
    # Exclusive start, inclusive end: e1 excluded; review A's own ledger
    # facts (recorded after its freeze) fall into B's window by design.
    ids = [event.id for event in window]
    assert e1.id not in ids
    assert e2.id in ids and e3.id in ids
    assert ids == sorted(ids)
    business_ids = [ev.id for ev in window if ev.source_kind == "task"]
    assert business_ids == [e2.id, e3.id]


async def test_events_in_window_is_scoped_to_the_review_workspace(db_session):
    await _emit(db_session, workspace_id="01REVIEWWORKSPACEOTHER0000")
    mine = await _emit(db_session)

    review = await _begin(db_session)
    window = await events_in_window(db_session, review)
    assert [event.id for event in window] == [mine.id]


# ── skipped (裁定 C) ───────────────────────────────────────────────────

async def test_skipped_review_does_not_advance_watermark(db_session):
    e1 = await _emit(db_session)
    review_a = await _begin(db_session)
    await complete_review(db_session, review_a)

    await _emit(db_session)
    review_b = await _begin(db_session)
    assert review_b.watermark_start == e1.id
    skipped = await mark_review_skipped(db_session, review_b, reason="open_proposals")

    assert skipped.status == "skipped"
    assert skipped.skip_reason == "open_proposals"
    assert skipped.watermark_end == skipped.watermark_start == e1.id  # forced back
    assert skipped.completed_at is not None

    # Next begin still chains off the last *succeeded* review (A).
    review_c = await _begin(db_session)
    assert review_c.watermark_start == e1.id


async def test_failed_review_window_is_reconsumed(db_session):
    e1 = await _emit(db_session)
    review_a = await _begin(db_session)
    await complete_review(db_session, review_a)

    e2 = await _emit(db_session)
    review_b = await _begin(db_session)
    frozen_start, frozen_end = review_b.watermark_start, review_b.watermark_end
    failed = await fail_review(db_session, review_b, error="x" * 10_000)

    assert failed.status == "failed"
    assert len(failed.error) == 4000  # truncated
    # Diagnostic freeze is left on the failed row…
    assert failed.watermark_end == frozen_end

    # …but the next review re-consumes the exact same window.
    review_c = await _begin(db_session)
    assert review_c.watermark_start == frozen_start == e1.id
    window = [ev.id for ev in await events_in_window(db_session, review_c)]
    assert e2.id in window


# ── empty ledger ───────────────────────────────────────────────────────

async def test_no_events_workspace_has_none_watermark_and_empty_window(db_session):
    review = await _begin(db_session, workspace_id="01REVIEWWORKSPACEEMPTY0000")
    assert review.watermark_start is None
    assert review.watermark_end is None
    assert review.window_start is None
    assert review.window_end is not None  # falls back to now

    assert await events_in_window(db_session, review) == []


# ── ledger facts ───────────────────────────────────────────────────────

async def test_ledger_rows_written_for_lifecycle_transitions(db_session):
    started = await _begin(db_session)
    await complete_review(db_session, started)
    rows = await _review_ledger_rows(db_session, started.id)
    assert [row.event_type for row in rows] == [et.REVIEW_STARTED, et.REVIEW_SUCCEEDED]
    assert all(row.idempotency_key == f"review:{started.id}:{row.event_type}" for row in rows)

    skipped = await _begin(db_session)
    await mark_review_skipped(db_session, skipped, reason="trigger_condition")
    rows = await _review_ledger_rows(db_session, skipped.id)
    assert [row.event_type for row in rows] == [et.REVIEW_STARTED, et.REVIEW_SKIPPED]
    assert rows[1].payload == {"skip_reason": "trigger_condition"}

    failed = await _begin(db_session)
    await fail_review(db_session, failed, error="boom")
    rows = await _review_ledger_rows(db_session, failed.id)
    assert [row.event_type for row in rows] == [et.REVIEW_STARTED, et.REVIEW_FAILED]
    assert rows[1].payload == {"error": "boom"}


async def test_latest_succeeded_review_ignores_skipped_and_failed(db_session):
    review_a = await _begin(db_session)
    await complete_review(db_session, review_a, briefing={"summary": "ok"})

    review_b = await _begin(db_session)
    await mark_review_skipped(db_session, review_b, reason="open_proposals")
    review_c = await _begin(db_session)
    await fail_review(db_session, review_c, error="boom")

    latest = await latest_succeeded_review(db_session, WORKSPACE_ID)
    assert latest is not None
    assert latest.id == review_a.id
    assert latest.briefing == {"summary": "ok"}


# ── events_in_window filters ───────────────────────────────────────────

async def test_events_in_window_event_types_filter_and_limit(db_session):
    e1 = await _emit(db_session, event_type=et.EXECUTION_COMPLETED)
    e2 = await _emit(db_session, event_type=et.GOAL_MEASURED)
    e3 = await _emit(db_session, event_type=et.EXECUTION_COMPLETED)

    review = await _begin(db_session)

    only_exec = await events_in_window(
        db_session, review, event_types=[et.EXECUTION_COMPLETED],
    )
    assert [ev.id for ev in only_exec] == [e1.id, e3.id]

    only_goal = await events_in_window(
        db_session, review, event_types=[et.GOAL_MEASURED],
    )
    assert [ev.id for ev in only_goal] == [e2.id]

    limited = await events_in_window(db_session, review, limit=2)
    assert [ev.id for ev in limited] == [e1.id, e2.id]
