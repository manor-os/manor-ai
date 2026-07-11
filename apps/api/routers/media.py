"""Media generation API — video/image job management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, get_db
from packages.core.models.media_job import MediaJob
from packages.core.models.user import User

router = APIRouter(prefix="/api/v1/media", tags=["media"])


class VideoJobResponse(BaseModel):
    id: str
    status: str
    kind: str
    prompt: str
    model: str | None = None
    params: dict = {}
    result_url: str | None = None
    error: str | None = None
    duration_seconds: int | None = None
    credits: int | None = None
    byok: bool = False
    created_at: str | None = None
    completed_at: str | None = None


@router.get("/jobs/{job_id}", response_model=VideoJobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the status of a media generation job."""
    job = (await db.execute(
        select(MediaJob).where(
            MediaJob.id == job_id,
            MediaJob.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    return VideoJobResponse(
        id=job.id,
        status=job.status,
        kind=job.kind,
        prompt=job.prompt,
        model=job.model,
        params=job.params or {},
        result_url=job.result_url,
        error=job.error,
        duration_seconds=job.duration_seconds,
        credits=job.credits,
        byok=job.byok,
        created_at=job.created_at.isoformat() if job.created_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


@router.get("/jobs", response_model=list[VideoJobResponse])
async def list_jobs(
    kind: str | None = None,
    status: str | None = None,
    conversation_id: str | None = None,
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List media generation jobs for the current entity."""
    q = select(MediaJob).where(MediaJob.entity_id == user.entity_id)
    if kind:
        q = q.where(MediaJob.kind == kind)
    if status:
        q = q.where(MediaJob.status == status)
    if conversation_id:
        q = q.where(MediaJob.conversation_id == conversation_id)
    q = q.order_by(MediaJob.created_at.desc()).limit(min(limit, 50))

    rows = (await db.execute(q)).scalars().all()
    return [
        VideoJobResponse(
            id=j.id,
            status=j.status,
            kind=j.kind,
            prompt=j.prompt,
            model=j.model,
            params=j.params or {},
            result_url=j.result_url,
            error=j.error,
            duration_seconds=j.duration_seconds,
            credits=j.credits,
            byok=j.byok,
            created_at=j.created_at.isoformat() if j.created_at else None,
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
        )
        for j in rows
    ]
