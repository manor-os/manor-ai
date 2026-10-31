from __future__ import annotations

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.media_job import MediaJob
from packages.core.services.media_job_control import RUN_CANCELLED_MESSAGE, stop_open_media_jobs_for_run


@pytest.mark.asyncio
async def test_a_cancelled_runs_unfinished_media_jobs_are_stopped_and_nothing_else(db_session):
    entity_id, run_id = generate_ulid(), generate_ulid()

    def job(status: str, run: str | None = run_id, entity: str | None = None) -> MediaJob:
        return MediaJob(id=generate_ulid(), entity_id=entity or entity_id, kind="video", status=status,
                        prompt="shot", params={"workflow_run_id": run} if run else {})

    queued, sending, done = job("pending"), job("processing"), job("completed")
    other_run, other_entity = job("pending", run=generate_ulid()), job("pending", entity=generate_ulid())
    db_session.add_all([queued, sending, done, other_run, other_entity])
    await db_session.flush()

    assert await stop_open_media_jobs_for_run(db_session, entity_id=entity_id, run_id=run_id) == 2
    for row in (queued, sending, done, other_run, other_entity):
        await db_session.refresh(row)
    assert (queued.status, sending.status) == ("failed", "failed")
    assert queued.error == RUN_CANCELLED_MESSAGE and queued.completed_at is not None
    # A finished clip, another run's and another account's stay as they were; a second call is a no-op.
    assert (done.status, other_run.status, other_entity.status) == ("completed", "pending", "pending")
    assert await stop_open_media_jobs_for_run(db_session, entity_id=entity_id, run_id=run_id) == 0
