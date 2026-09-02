"""ReviewRun-aware orchestration around the Strategist decision service.

``service.run_review`` owns Strategist business decisions.  This module owns
the outer transaction lifecycle: claim/freeze a ReviewRun, build its evidence
briefing, invoke the service, and persist the terminal review state.  Keeping
that boundary outside the Celery task makes it reusable by tests and future
trigger transports.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from sqlalchemy import select

from packages.core.constants.review import (
    ReviewCycleOutcome,
    ReviewSkipReason,
)
from packages.core.models.base import generate_ulid

logger = logging.getLogger(__name__)
_REVIEW_LEASE_HEARTBEAT_SECONDS = 30


async def _review_lease_heartbeat(review_id: str, lease_owner: str) -> None:
    """Renew a review lease from short independent transactions."""
    from packages.core.database import create_worker_session
    from packages.core.review import renew_review_lease

    session_factory = create_worker_session()
    while True:
        await asyncio.sleep(_REVIEW_LEASE_HEARTBEAT_SECONDS)
        try:
            async with session_factory() as heartbeat_db:
                renewed = await renew_review_lease(
                    heartbeat_db,
                    review_id=review_id,
                    lease_owner=lease_owner,
                )
                await heartbeat_db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Strategist: review lease heartbeat failed review=%s owner=%s",
                review_id,
                lease_owner,
                exc_info=True,
            )
            continue
        if not renewed:
            logger.warning(
                "Strategist: review lease was lost review=%s owner=%s",
                review_id,
                lease_owner,
            )
            return


async def _terminal_delivery_result(
    db,
    *,
    workspace_id: str,
    delivery_id: str | None,
) -> dict[str, Any] | None:
    """Return a durable no-op receipt for an already terminal delivery."""
    if delivery_id is None:
        return None

    from packages.core.constants.review import ReviewRunStatus
    from packages.core.models.review_run import ReviewRun

    review = (
        await db.execute(
            select(ReviewRun)
            .where(
                ReviewRun.workspace_id == workspace_id,
                ReviewRun.delivery_id == delivery_id,
                ReviewRun.status.in_((
                    ReviewRunStatus.SUCCEEDED,
                    ReviewRunStatus.SKIPPED,
                )),
            )
            .order_by(ReviewRun.completed_at.desc().nullslast(), ReviewRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if review is None:
        return None

    review_id = review.id
    review_entity_id = review.entity_id
    review_status = str(review.status)
    await db.commit()

    from packages.core.strategist.service import finish_strategist_review_activity

    await finish_strategist_review_activity(
        entity_id=review_entity_id,
        workspace_id=workspace_id,
        review_id=review_id,
        state=(
            "completed"
            if review_status == ReviewRunStatus.SUCCEEDED.value
            else "skipped"
        ),
    )
    return {
        "workspace_id": workspace_id,
        "review_id": review_id,
        "status": review_status,
        "deduplicated": True,
    }


@dataclass(frozen=True)
class ReviewCycleResult:
    """Typed interpretation of the legacy-compatible result payload."""

    payload: dict[str, Any]
    outcome: ReviewCycleOutcome
    skip_reason: ReviewSkipReason | str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReviewCycleResult":
        if payload.get("needs_decision"):
            outcome = ReviewCycleOutcome.NEEDS_DECISION
        elif payload.get("skipped"):
            outcome = ReviewCycleOutcome.SKIPPED
        else:
            outcome = ReviewCycleOutcome.COMPLETED

        raw_reason = payload.get("reason")
        reason: ReviewSkipReason | str | None = None
        if raw_reason:
            try:
                reason = ReviewSkipReason(str(raw_reason))
            except ValueError:
                # Legacy/custom values remain auditable without becoming new
                # behavior branches. New service code emits the enum.
                reason = str(raw_reason)
        return cls(payload=payload, outcome=outcome, skip_reason=reason)

    @property
    def skipped(self) -> bool:
        return self.outcome in {
            ReviewCycleOutcome.SKIPPED,
            ReviewCycleOutcome.NEEDS_DECISION,
        }


async def run_strategist_review_cycle(
    db,
    workspace_id: str,
    trigger,
    *,
    execution_owner: str | None = None,
) -> dict:
    """Execute one full Strategist review cycle over an open session."""
    from packages.core.models.workspace import Workspace
    from packages.core.strategist import ReviewTrigger

    trigger = ReviewTrigger.coerce(trigger)

    workspace = (
        await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    entity_id = workspace.entity_id if workspace is not None else None
    review_v2_enabled = False
    if entity_id is not None:
        from packages.core.services.feature_flags import is_enabled

        review_v2_enabled = await is_enabled(
            db,
            "strategist_review_v2",
            entity_id=entity_id,
            fallback=False,
        )

    if review_v2_enabled:
        return await _run_strategist_review_cycle(
            db,
            workspace_id,
            trigger,
            workspace=workspace,
            use_review_run=True,
            execution_owner=execution_owner or "review:" + generate_ulid(),
        )

    return await _run_strategist_review_cycle(
        db,
        workspace_id,
        trigger,
        workspace=workspace,
        use_review_run=False,
        execution_owner=None,
    )


async def _run_strategist_review_cycle(
    db,
    workspace_id: str,
    trigger,
    *,
    workspace,
    use_review_run: bool,
    execution_owner: str | None,
) -> dict:
    """Claim and run one review, recovering expiry or the same redelivery."""
    from packages.core.constants.review import ReviewRunStatus
    from packages.core.models.review_run import ReviewRun
    from packages.core.review import (
        ReviewAlreadyRunning,
        begin_review,
        fail_review,
        review_lease_is_expired,
    )
    from packages.core.strategist import run_review

    entity_id = workspace.entity_id if workspace is not None else None
    review = None
    review_id: str | None = None
    if use_review_run and entity_id is not None:
        terminal_delivery = await _terminal_delivery_result(
            db,
            workspace_id=workspace_id,
            delivery_id=execution_owner,
        )
        if terminal_delivery is not None:
            return terminal_delivery

        running_review = (
            await db.execute(
                select(ReviewRun).where(
                    ReviewRun.workspace_id == workspace_id,
                    ReviewRun.status == ReviewRunStatus.RUNNING,
                )
            )
        ).scalar_one_or_none()
        # Celery preserves the task id when it redelivers after worker loss.
        # The broker visibility timeout outlives this task's hard limit, so a
        # matching owner is an idempotent reclaim rather than a live duplicate.
        same_delivery = bool(
            running_review is not None
            and execution_owner is not None
            and running_review.lease_owner == execution_owner
        )
        if (
            running_review is not None
            and not same_delivery
            and not review_lease_is_expired(running_review)
        ):
            logger.info(
                "Strategist: review execution is active for workspace %s; skipping",
                workspace_id,
            )
            return {
                "workspace_id": workspace_id,
                "skipped": True,
                "reason": ReviewSkipReason.REVIEW_ALREADY_RUNNING,
            }
        if running_review is not None:
            # Fence recovery with the row itself and recheck after waiting. A
            # heartbeat may have renewed the candidate since the first read.
            abandoned_review = (
                await db.execute(
                    select(ReviewRun)
                    .execution_options(populate_existing=True)
                    .where(ReviewRun.id == running_review.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                abandoned_review is not None
                and abandoned_review.status == ReviewRunStatus.RUNNING
                and abandoned_review.lease_owner != execution_owner
                and not review_lease_is_expired(abandoned_review)
            ):
                return {
                    "workspace_id": workspace_id,
                    "skipped": True,
                    "reason": ReviewSkipReason.REVIEW_ALREADY_RUNNING,
                }
        else:
            abandoned_review = None
        if abandoned_review is not None and abandoned_review.status == ReviewRunStatus.RUNNING:
            logger.warning(
                "Strategist: recovering abandoned or redelivered review %s for workspace %s",
                abandoned_review.id,
                workspace_id,
            )
            await fail_review(
                db,
                abandoned_review,
                error="Review execution ended before recording a terminal state.",
            )
            await db.commit()

            from packages.core.strategist.service import (
                finish_strategist_review_activity,
            )

            await finish_strategist_review_activity(
                entity_id=entity_id,
                workspace_id=workspace_id,
                review_id=abandoned_review.id,
                state="failed",
            )

        try:
            review = await begin_review(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                trigger=trigger,
                delivery_id=execution_owner,
                lease_owner=execution_owner,
            )
            review_id = review.id
            # Make the partial-unique-index claim visible before the expensive
            # evidence work. The durable lease is renewed independently.
            await db.commit()
        except ReviewAlreadyRunning:
            terminal_delivery = await _terminal_delivery_result(
                db,
                workspace_id=workspace_id,
                delivery_id=execution_owner,
            )
            if terminal_delivery is not None:
                return terminal_delivery
            logger.info(
                "Strategist: review already running for workspace %s; skipping",
                workspace_id,
            )
            return {
                "workspace_id": workspace_id,
                "skipped": True,
                "reason": ReviewSkipReason.REVIEW_ALREADY_RUNNING,
            }

    activity_review_id = review_id or "rv_" + generate_ulid()
    heartbeat = (
        asyncio.create_task(_review_lease_heartbeat(review_id, execution_owner))
        if review_id is not None and execution_owner is not None
        else None
    )
    try:
        initial_activity_message_id: str | None = None
        if workspace is not None and workspace.status == "active":
            from packages.core.strategist.service import _post_strategist_activity

            initial_activity_message_id = await _post_strategist_activity(
                workspace,
                stage="collecting_feedback",
                body=(
                    "🧭 Manor AI is collecting workspace feedback, goals, tasks, "
                    "and recent evidence…"
                ),
                review_id=activity_review_id,
            )

        briefing_markdown: str | None = None
        if review is not None:
            from packages.core.consolidators import run_all
            from packages.core.review.briefing import build_briefing
            from packages.core.review.briefing_render import render_briefing_markdown

            report_rows = await run_all(db, review)
            briefing = await build_briefing(db, review, report_rows)
            review.briefing = briefing.model_dump(mode="json")
            await db.flush()
            briefing_markdown = render_briefing_markdown(briefing)
            # Consolidation writes belong to the frozen review snapshot, not
            # to the later model call. Persist them now so the main worker
            # session never carries this transaction across network I/O.
            await db.commit()

        payload = await run_review(
            db,
            workspace_id,
            trigger=trigger,
            briefing_markdown=briefing_markdown,
            review_run=review,
            review_lease_owner=execution_owner,
            activity_review_id=activity_review_id,
            initial_activity_message_id=initial_activity_message_id,
        )
    except Exception as exc:
        if review_id is not None:
            from packages.core.review import fail_review

            try:
                await db.rollback()
                review = await db.get(
                    ReviewRun,
                    review_id,
                    populate_existing=True,
                    with_for_update=True,
                )
                if (
                    review is not None
                    and review.status == ReviewRunStatus.RUNNING
                    and review.lease_owner == execution_owner
                ):
                    await fail_review(db, review, error=str(exc))
                    await db.commit()
                else:
                    await db.rollback()
            except Exception:
                logger.exception(
                    "Failed to mark review %s failed for workspace %s",
                    review_id,
                    workspace_id,
                )
        if entity_id is not None:
            from packages.core.strategist.service import (
                finish_strategist_review_activity,
            )

            await finish_strategist_review_activity(
                entity_id=entity_id,
                workspace_id=workspace_id,
                review_id=activity_review_id,
                state="failed",
            )
        raise
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    result = ReviewCycleResult.from_payload(payload)
    if review_id is not None:
        from packages.core.review import mark_review_skipped

        review = await db.get(
            ReviewRun,
            review_id,
            populate_existing=True,
            with_for_update=True,
        )
        if result.skipped:
            if (
                review is not None
                and review.status == ReviewRunStatus.RUNNING
                and review.lease_owner == execution_owner
            ):
                await mark_review_skipped(
                    db,
                    review,
                    reason=result.skip_reason or ReviewSkipReason.UNKNOWN,
                )
                await db.commit()
            else:
                await db.rollback()
        elif review is not None and review.status == ReviewRunStatus.SUCCEEDED:
            # Successful Proposal/Task/governance effects and the ReviewRun
            # terminal receipt were committed atomically inside run_review.
            # Close the read transaction without expiring every ORM object in
            # the caller's session (rollback always expires them).
            await db.commit()
        else:
            await db.rollback()
            raise RuntimeError(
                f"Strategist review {review_id} returned success without "
                "an atomic ReviewRun success receipt"
            )
    return result.payload
