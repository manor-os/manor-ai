"""Resume Strategist after a committed Human queue response clears its wait."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def workspace_awaits_proposal_decision(
    db: AsyncSession, workspace_id: str,
) -> bool:
    """True when a review would skip because proposals still need a decision."""
    from packages.core.strategist.context import (
        _open_proposed_items,
        _open_proposed_tasks,
    )

    if await _open_proposed_tasks(db, workspace_id):
        return True
    return bool(await _open_proposed_items(db, workspace_id))


def dispatch_strategist_for_cleared_human_queue(
    *,
    workspace_id: str,
    user_id: str,
    commitment_id: str,
    status: str,
) -> None:
    """Wake Strategist after the last waiting Human queue item closes.

    Callers must commit the response first and skip this when proposal tasks
    or items still await a decision. Failure must not fail the response; the
    operator already recorded the answer.
    """
    try:
        from packages.core.strategist import ReviewTrigger, ReviewTriggerKind
        from packages.core.tasks.ai_tasks import run_strategist_review

        run_strategist_review.apply_async(
            args=[workspace_id],
            kwargs={
                **ReviewTrigger(
                    kind=ReviewTriggerKind.EVENT,
                    detail=f"human commitment {status}: {commitment_id}",
                ).celery_kwargs(),
                "user_id": user_id,
            },
            countdown=1,
        )
    except Exception:
        logger.warning(
            "Failed to dispatch strategist review after human commitment %s",
            commitment_id,
            exc_info=True,
        )
