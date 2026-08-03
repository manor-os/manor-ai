"""ReviewRun lifecycle — frozen snapshot + watermark advancement (M2).

``begin_review`` freezes everything a review is allowed to see:

* ``watermark_start`` — the previous *succeeded* review's ``watermark_end``
  (``None`` == ledger genesis). Skipped and failed rows are ignored by this
  lookup, which is exactly how "the watermark does not advance" is enforced:
  a failed review's window is simply re-consumed by the next ``begin_review``.
* ``watermark_end`` — ``max(workspace_events.id)`` for the workspace at begin
  time (``None`` == no events yet). Events appended during the review get a
  larger ULID and naturally fall into the next window — no ledger lock needed.
* ``window_start`` / ``window_end`` — ``occurred_at`` of the boundary events
  (``window_end`` falls back to *now* when the ledger is empty).
* ``workspace_revision`` / ``policy_revision`` — config versions frozen so the
  review's reasoning is attributable to exact configuration.

Mutual exclusion: the partial unique index ``uq_review_runs_one_running``
(``(workspace_id) WHERE status='running'``) makes the ``running`` insert the
lock. The loser of a concurrent race gets ``ReviewAlreadyRunning``.

裁定 C: a suppressed review still writes a row (``status='skipped'``) but
forces ``watermark_end = watermark_start`` so the watermark does not move.

Every transition appends a ledger fact (``review_started`` /
``review_skipped`` / ``review_succeeded`` / ``review_failed``) in the caller's
transaction via ``ledger.service.record_event``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ledger import event_types as et
from packages.core.ledger.service import record_event
from packages.core.models.governance import GovernancePolicy
from packages.core.strategist.triggers import ReviewTrigger, ReviewTriggerKind
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 4000


class ReviewAlreadyRunning(Exception):
    """Another review for this workspace is already ``running``."""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        super().__init__(f"a review is already running for workspace {workspace_id}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _record_review_event(
    db: AsyncSession, review: ReviewRun, event_type: str, **fields
) -> None:
    await record_event(
        db,
        entity_id=review.entity_id,
        workspace_id=review.workspace_id,
        event_type=event_type,
        source_kind="review",
        source_id=review.id,
        idempotency_key=f"review:{review.id}:{event_type}",
        **fields,
    )


async def latest_succeeded_review(
    db: AsyncSession, workspace_id: str
) -> Optional[ReviewRun]:
    """The most recent ``succeeded`` review for this workspace, or ``None``.

    This lookup *is* the watermark: skipped/failed rows never participate,
    so their windows are re-consumed by the next review.
    """
    return (
        await db.execute(
            select(ReviewRun)
            .where(
                ReviewRun.workspace_id == workspace_id,
                ReviewRun.status == "succeeded",
            )
            .order_by(ReviewRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def begin_review(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    trigger: "ReviewTrigger | ReviewTriggerKind | str",
) -> ReviewRun:
    """Claim the workspace's single review slot and freeze the snapshot.

    Inserts a ``status='running'`` row; a concurrent running review makes the
    insert violate ``uq_review_runs_one_running`` and raises
    ``ReviewAlreadyRunning`` (the SAVEPOINT keeps the caller's transaction
    usable). Appends a ``review_started`` ledger fact.
    """
    trigger = ReviewTrigger.coerce(trigger)
    trigger_kind = trigger.kind.value

    previous = await latest_succeeded_review(db, workspace_id)
    watermark_start = previous.watermark_end if previous is not None else None

    watermark_end = (
        await db.execute(
            select(func.max(WorkspaceEvent.id)).where(
                WorkspaceEvent.workspace_id == workspace_id
            )
        )
    ).scalar()

    async def _occurred_at(event_id: str) -> Optional[datetime]:
        return (
            await db.execute(
                select(WorkspaceEvent.occurred_at).where(WorkspaceEvent.id == event_id)
            )
        ).scalar_one_or_none()

    now = _utcnow()
    window_start = await _occurred_at(watermark_start) if watermark_start else None
    window_end = (await _occurred_at(watermark_end)) if watermark_end else now

    workspace_revision = (
        await db.execute(
            select(Workspace.operation_revision).where(Workspace.id == workspace_id)
        )
    ).scalar_one_or_none()
    policy_revision = (
        await db.execute(
            select(GovernancePolicy.revision).where(
                GovernancePolicy.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()

    review = ReviewRun(
        entity_id=entity_id,
        workspace_id=workspace_id,
        trigger_kind=trigger_kind,
        trigger_detail=trigger.detail or None,
        status="running",
        watermark_start=watermark_start,
        watermark_end=watermark_end,
        window_start=window_start,
        window_end=window_end,
        workspace_revision=workspace_revision,
        policy_revision=policy_revision,
        created_at=now,
    )

    try:
        # SAVEPOINT: losing the single-running-review race must not poison
        # the caller's outer transaction.
        async with db.begin_nested():
            db.add(review)
            await db.flush()
    except IntegrityError:
        logger.info(
            "review already running for workspace %s (trigger=%s)",
            workspace_id, trigger_kind,
        )
        raise ReviewAlreadyRunning(workspace_id) from None

    await _record_review_event(
        db, review, et.REVIEW_STARTED,
        status="running",
        occurred_at=now,
        payload={"trigger_kind": trigger_kind, "trigger_detail": trigger.detail},
    )
    return review


async def mark_review_skipped(
    db: AsyncSession, review: ReviewRun, *, reason: str
) -> ReviewRun:
    """Suppressed review (裁定 C): keep the row, do NOT advance the watermark.

    ``watermark_end`` is forced back to ``watermark_start`` so the row itself
    documents "this review consumed nothing".
    """
    review.status = "skipped"
    review.skip_reason = (reason or "")[:64] or None
    review.watermark_end = review.watermark_start
    review.completed_at = _utcnow()
    await db.flush()
    await _record_review_event(
        db, review, et.REVIEW_SKIPPED,
        status="skipped",
        occurred_at=review.completed_at,
        payload={"skip_reason": review.skip_reason},
    )
    return review


async def complete_review(
    db: AsyncSession, review: ReviewRun, *, briefing: Optional[dict] = None
) -> ReviewRun:
    """Success — the only transition that advances the watermark."""
    review.status = "succeeded"
    if briefing is not None:
        review.briefing = briefing
    review.completed_at = _utcnow()
    await db.flush()
    await _record_review_event(
        db, review, et.REVIEW_SUCCEEDED,
        status="succeeded",
        occurred_at=review.completed_at,
    )
    return review


async def fail_review(
    db: AsyncSession, review: ReviewRun, *, error: str
) -> ReviewRun:
    """Failure — the watermark does not advance.

    The frozen ``watermark_end`` is left on the row for diagnostics; because
    ``begin_review`` chains only off the latest *succeeded* review, this row
    is ignored and the same window is re-consumed by the next review.
    """
    review.status = "failed"
    review.error = (error or "")[:MAX_ERROR_CHARS]
    review.completed_at = _utcnow()
    await db.flush()
    await _record_review_event(
        db, review, et.REVIEW_FAILED,
        status="failed",
        occurred_at=review.completed_at,
        payload={"error": review.error[:500]},
    )
    return review


async def events_in_window(
    db: AsyncSession,
    review: ReviewRun,
    *,
    event_types: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> list[WorkspaceEvent]:
    """The review's frozen window: ``watermark_start < id <= watermark_end``.

    ``watermark_start is None`` means ledger genesis (no lower bound);
    ``watermark_end is None`` means the ledger was empty at begin time
    (empty window).
    """
    if review.watermark_end is None:
        return []
    stmt = select(WorkspaceEvent).where(
        WorkspaceEvent.workspace_id == review.workspace_id,
        WorkspaceEvent.id <= review.watermark_end,
    )
    if review.watermark_start is not None:
        stmt = stmt.where(WorkspaceEvent.id > review.watermark_start)
    if event_types:
        stmt = stmt.where(WorkspaceEvent.event_type.in_(list(event_types)))
    stmt = stmt.order_by(WorkspaceEvent.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())
