"""Public task endpoints — no auth required, accessed via session code.

Used for:
  - Staff processing tasks on-site (dispatched via email/SMS link)
  - Customer evaluating completed tasks (feedback link)

URLs:
  GET  /api/v1/public/task?code={session_code}         — view task details
  POST /api/v1/public/task/update-status                — update task status + add comment
  POST /api/v1/public/task/complete                     — mark task finished
  POST /api/v1/public/task/evaluate                     — submit customer rating
  POST /api/v1/public/task/generate-code                — (authenticated) generate a session code for a task
"""
from __future__ import annotations

import json
import os
import secrets
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.task import Task, TaskLog
from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/task", tags=["public-task"])

# Session codes stored in Redis (survive API restarts)
_CODE_TTL = 86400 * 7  # 7 days


def _redis():
    try:
        import redis
        from packages.core.config import get_settings
        r = redis.from_url(get_settings().REDIS_URL.replace("+asyncpg", ""), decode_responses=True)
        return r
    except Exception:
        return None


def _code_key(code: str) -> str:
    return f"manor:task_code:{code}"


# ── Schemas ──

class PublicTaskResponse(BaseModel):
    id: str
    title: str
    description: str | None = None
    status: str
    priority: int
    deadline: str | None = None
    assignee_name: str | None = None
    created_at: str | None = None


class StatusUpdateRequest(BaseModel):
    code: str
    status: str | None = None
    comment: str | None = None


class CompleteRequest(BaseModel):
    code: str
    notes: str | None = None


class EvaluateRequest(BaseModel):
    code: str
    score: int  # 1-5
    review: str | None = None


class GenerateCodeRequest(BaseModel):
    task_id: str
    recipient_email: str | None = None
    recipient_type: str = "staff"  # staff | customer


class GenerateCodeResponse(BaseModel):
    code: str
    url: str
    expires_in: int


# ── Helpers ──

async def _get_task_by_code(code: str, db: AsyncSession) -> Task:
    """Look up a task by session code from Redis."""
    import json
    r = _redis()
    if not r:
        raise HTTPException(503, "Session store unavailable")

    raw = r.get(_code_key(code))
    if not raw:
        raise HTTPException(404, "Invalid or expired session code")

    data = json.loads(raw)
    task_id = data.get("task_id")

    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    return task


# ── Public endpoints (no auth) ──

@router.get("", response_model=PublicTaskResponse)
async def get_public_task(
    code: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """View task details via session code (no auth required)."""
    task = await _get_task_by_code(code, db)
    return PublicTaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        deadline=task.deadline.isoformat() if task.deadline else None,
        created_at=task.created_at.isoformat() if task.created_at else None,
    )


@router.post("/update-status")
async def public_update_status(
    req: StatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update task status and/or add a comment (staff processing)."""
    task = await _get_task_by_code(req.code, db)

    if req.status:
        from packages.core.constants.task import VALID_STATUSES
        from packages.core.services.task_state_machine import TERMINAL_STATUSES, apply_task_status_transition

        if req.status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status: {req.status}")
        apply_task_status_transition(task, req.status)
        if req.status in TERMINAL_STATUSES:
            from packages.core.services.workspace_operation_service import check_work_batch_completion

            await check_work_batch_completion(
                db,
                task,
                trigger_source="public_task.update_status",
            )

    if req.comment:
        log = TaskLog(
            id=generate_ulid(),
            task_id=task.id,
            log_type="comment",
            content=req.comment,
            created_by="staff (public)",
        )
        db.add(log)

    await db.flush()
    return {"status": task.status, "updated": True}


@router.post("/complete")
async def public_complete_task(
    req: CompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mark task as completed (staff finishing on-site)."""
    task = await _get_task_by_code(req.code, db)

    from packages.core.services.task_state_machine import apply_task_status_transition
    from packages.core.services.workspace_operation_service import check_work_batch_completion

    apply_task_status_transition(task, "completed")

    if req.notes:
        log = TaskLog(
            id=generate_ulid(),
            task_id=task.id,
            log_type="status_change",
            content=f"Completed: {req.notes}",
            created_by="staff (public)",
        )
        db.add(log)
    await check_work_batch_completion(
        db,
        task,
        trigger_source="public_task.complete",
    )

    await db.flush()
    return {"status": "completed"}


@router.post("/evaluate")
async def public_evaluate_task(
    req: EvaluateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Submit customer evaluation/rating for a completed task."""
    task = await _get_task_by_code(req.code, db)

    if task.status != "completed":
        raise HTTPException(400, "Can only evaluate completed tasks")

    if req.score < 1 or req.score > 5:
        raise HTTPException(400, "Score must be 1-5")

    # Store evaluation in task details
    details = dict(task.details or {})
    details["evaluation"] = {
        "score": req.score,
        "review": req.review,
        "evaluated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    task.details = details

    log = TaskLog(
        id=generate_ulid(),
        task_id=task.id,
        log_type="evaluation",
        content=f"Rating: {req.score}/5" + (f" — {req.review}" if req.review else ""),
        created_by="customer (public)",
    )
    db.add(log)

    await db.flush()
    return {"evaluated": True, "score": req.score}


# ── Authenticated endpoint to generate codes ──

@router.post("/generate-code", response_model=GenerateCodeResponse)
async def generate_session_code(
    req: GenerateCodeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a session code for public task access (authenticated)."""
    # Verify task exists and belongs to user's entity
    result = await db.execute(
        select(Task).where(Task.id == req.task_id, Task.entity_id == user.entity_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    code = secrets.token_urlsafe(32)
    r = _redis()
    if not r:
        raise HTTPException(503, "Session store unavailable")

    r.setex(_code_key(code), _CODE_TTL, json.dumps({
        "task_id": task.id,
        "entity_id": user.entity_id,
        "recipient_email": req.recipient_email,
        "recipient_type": req.recipient_type,
        "generated_by": user.id,
    }))

    app_url = os.getenv("APP_URL", "http://localhost:3010")
    page = "process" if req.recipient_type == "staff" else "evaluate"
    url = f"{app_url}/task/{page}?code={code}"

    # Send email notification if recipient provided
    if req.recipient_email:
        from packages.core.services.email_service import send_common_email
        subject = f"Task: {task.title}" if page == "process" else f"Rate your experience: {task.title}"
        content = f"<p>You have a task to {'process' if page == 'process' else 'evaluate'}:</p><p><strong>{task.title}</strong></p><p><a href='{url}' style='background:#0f766e;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;display:inline-block;font-weight:700;'>{'Process Task' if page == 'process' else 'Rate Experience'}</a></p>"
        await send_common_email(req.recipient_email, subject, content)

    return GenerateCodeResponse(code=code, url=url, expires_in=_CODE_TTL)
