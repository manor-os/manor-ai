"""Recovery of Workspace Architect turns whose process died mid-turn."""
from __future__ import annotations

import logging

from packages.core.celery_app import celery_app
from packages.core.database import async_session
from packages.core.queues import CeleryQueue
from packages.core.services import workspace_draft_service as draft_service
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)


def enqueue_draft_turn_recovery(draft_id: str) -> bool:
    """Best-effort broker handoff; the draft's turn request stays the durable source."""
    try:
        resume_draft_turn_task.apply_async(args=(draft_id,), queue=CeleryQueue.INTERACTIVE.value)
        return True
    except Exception:
        logger.exception("Could not enqueue Architect turn recovery for draft %s; the sweep will retry", draft_id)
        return False


async def resume_draft_turn(draft_id: str) -> bool:
    async with async_session() as db:
        resumed = await draft_service.resume_requested_turn(db, draft_id=draft_id)
        await db.commit()
        return resumed


@celery_app.task(
    name="workspace_draft.resume_turn",
    ignore_result=True,
    # An Architect turn runs for minutes; one that overruns is cancelled and
    # counted against the draft's recovery attempts.
    soft_time_limit=1800,
    time_limit=1860,
)
def resume_draft_turn_task(draft_id: str) -> None:
    # The turn claims the draft before any model work, so a duplicate
    # delivery finds it busy (or already answered) and does nothing.
    run_in_worker(resume_draft_turn(draft_id))


async def recover_draft_turns(*, now: float | None = None) -> int:
    async with async_session() as db:
        draft_ids = await draft_service.hand_off_interrupted_turns(db, now=now)
        await db.commit()
    for draft_id in draft_ids:
        enqueue_draft_turn_recovery(draft_id)
    return len(draft_ids)


@celery_app.task(name="workspace_draft.recover_turns", ignore_result=True)
def recover_draft_turns_task() -> None:
    run_in_worker(recover_draft_turns())
