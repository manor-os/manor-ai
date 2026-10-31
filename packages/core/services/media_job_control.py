"""Stopping media generation that nobody is waiting for any more."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.media_job import MediaJob, MediaJobStatus

RUN_CANCELLED_MESSAGE = "Stopped: the run that asked for it was cancelled."


async def stop_open_media_jobs_for_run(db: AsyncSession, *, entity_id: str, run_id: str) -> int:
    """Mark a cancelled Workflow run's unfinished media jobs failed, so workers never start them.

    Workers skip a job already in a final state, so a queued job is never sent to its provider;
    one already sent is no longer waited on (the provider may still finish and bill it). Idempotent.
    """
    if not run_id:
        return 0
    result = await db.execute(
        update(MediaJob)
        .where(
            MediaJob.entity_id == entity_id,
            MediaJob.params["workflow_run_id"].astext == run_id,
            MediaJob.status.in_((MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value)),
        )
        .values(
            status=MediaJobStatus.FAILED.value,
            error=RUN_CANCELLED_MESSAGE,
            completed_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
