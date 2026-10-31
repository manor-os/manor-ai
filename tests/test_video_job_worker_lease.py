"""One delivery of media.process_video_job owns a video job at a time."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.media_job import MediaJob
from packages.core.tasks import media_tasks


async def _job(db_session, **params) -> str:
    job = MediaJob(
        id=generate_ulid(), entity_id=generate_ulid(), kind="video", status="pending",
        prompt="A clip", model="bytedance/seedance-2.0", params=params,
    )
    db_session.add(job)
    await db_session.commit()
    return job.id


@pytest.mark.asyncio
async def test_a_duplicate_delivery_cannot_process_a_job_another_worker_holds(db_session):
    job_id = await _job(db_session)

    assert await media_tasks._mark_video_job_processing(job_id) is True
    # A recovery sweep re-enqueued the job while the first worker is polling.
    assert await media_tasks._mark_video_job_processing(job_id) is False

    # Once the first worker hands the job back, the next delivery may claim it.
    await media_tasks._release_media_worker_lease(job_id)
    assert await media_tasks._mark_video_job_processing(job_id) is True


@pytest.mark.asyncio
async def test_an_expired_lease_lets_recovery_take_over(db_session):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=media_tasks.VIDEO_WORKER_LEASE_SECONDS + 60)).isoformat()
    job_id = await _job(db_session, worker_lease_at=stale)
    job = (await db_session.execute(select(MediaJob).where(MediaJob.id == job_id))).scalar_one()
    job.status = "processing"
    await db_session.commit()

    assert await media_tasks._mark_video_job_processing(job_id) is True


@pytest.mark.asyncio
async def test_a_finished_job_is_never_claimed_again(db_session, monkeypatch):
    job_id = await _job(db_session)
    job = (await db_session.execute(select(MediaJob).where(MediaJob.id == job_id))).scalar_one()
    job.status, job.error, job.byok = "failed", "Temporary provider poll failure", True
    await db_session.commit()

    async def no_external_notification(*_args):
        pass

    monkeypatch.setattr(media_tasks, "_push_notification", no_external_notification)
    monkeypatch.setattr(media_tasks, "_project_group_media_if_ready", no_external_notification)
    assert await media_tasks._finalize_video_job(job_id, {"result_url": "/recovered.mp4"}, force=True)
    await db_session.refresh(job)
    assert job.status == "completed" and job.error is None
    assert await media_tasks._mark_video_job_processing(job_id) is False


@pytest.mark.asyncio
async def test_the_recovery_sweep_leaves_a_job_a_worker_is_processing(db_session, monkeypatch):
    job_id = await _job(db_session, provider_poll={"poll_url": "https://provider.test/poll/1"})
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    job = (await db_session.execute(select(MediaJob).where(MediaJob.id == job_id))).scalar_one()
    job.status, job.started_at, job.created_at = "processing", old, old
    await db_session.commit()

    resumed = []

    async def provider_poll(swept):
        resumed.append(swept.id)
        return {"status": "pending"}

    monkeypatch.setattr(media_tasks, "_resume_video_job_from_provider", provider_poll)
    # The worker holds a live lease (it took the job and is polling).
    assert await media_tasks._mark_video_job_processing(job_id) is True

    await media_tasks.recover_stale_video_jobs(stale_after_seconds=0)
    # Other stale jobs may be swept; this one is left to its worker.
    assert job_id not in resumed
    await db_session.refresh(job)
    assert job.status == "processing" and job.error is None
