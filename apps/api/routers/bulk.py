"""Bulk operations — batch update/delete, CSV export/import."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.services.bulk_service import (
    bulk_update_tasks,
    bulk_delete_documents,
    bulk_update_task_status,
    export_tasks_csv,
    export_clients_csv,
    import_tasks_csv,
)
from packages.core.services.task_state_machine import TaskStatusTransitionError
from apps.api.deps import get_current_user, require_workspace_writable

router = APIRouter(prefix="/api/v1/bulk", tags=["bulk"])


# ── Schemas ──

class BulkUpdateTasksRequest(BaseModel):
    task_ids: list[str]
    updates: dict  # {status?, priority?, assignee_id?}


class BulkStatusRequest(BaseModel):
    task_ids: list[str]
    status: str


class BulkDeleteDocsRequest(BaseModel):
    document_ids: list[str]


class BulkResultResponse(BaseModel):
    count: int


# ── Endpoints ──

async def _require_bulk_task_workspaces_writable(
    db: AsyncSession,
    *,
    user: User,
    task_ids: list[str],
) -> None:
    """Authorize the complete batch before mutating any Task."""

    workspace_ids = {
        str(workspace_id)
        for workspace_id in (await db.execute(
            select(Task.workspace_id).where(
                Task.id.in_(task_ids),
                Task.entity_id == user.entity_id,
            )
        )).scalars()
        if workspace_id
    }
    for workspace_id in sorted(workspace_ids):
        await require_workspace_writable(db, user, workspace_id)

@router.post("/tasks/update", response_model=BulkResultResponse)
async def bulk_update_tasks_endpoint(
    req: BulkUpdateTasksRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk update tasks — change status, priority, or assignee for multiple tasks."""
    if not req.task_ids:
        raise HTTPException(400, "task_ids must not be empty")
    await _require_bulk_task_workspaces_writable(
        db,
        user=user,
        task_ids=req.task_ids,
    )
    try:
        count = await bulk_update_tasks(db, user.entity_id, req.task_ids, req.updates)
    except TaskStatusTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"count": count}


@router.post("/tasks/status", response_model=BulkResultResponse)
async def bulk_status_change(
    req: BulkStatusRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk status change for tasks."""
    if not req.task_ids:
        raise HTTPException(400, "task_ids must not be empty")
    await _require_bulk_task_workspaces_writable(
        db,
        user=user,
        task_ids=req.task_ids,
    )
    try:
        count = await bulk_update_task_status(
            db,
            user.entity_id,
            req.task_ids,
            req.status,
        )
    except TaskStatusTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"count": count}


@router.post("/documents/delete", response_model=BulkResultResponse)
async def bulk_delete_docs_endpoint(
    req: BulkDeleteDocsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk delete documents."""
    if not req.document_ids:
        raise HTTPException(400, "document_ids must not be empty")
    count = await bulk_delete_documents(db, user.entity_id, req.document_ids)
    return {"count": count}


@router.get("/export/tasks")
async def export_tasks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(None),
):
    """Export tasks as CSV."""
    csv_data = await export_tasks_csv(db, user.entity_id, status=status)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tasks.csv"},
    )


@router.get("/export/clients")
async def export_clients(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export clients as CSV."""
    csv_data = await export_clients_csv(db, user.entity_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )


@router.post("/import/tasks", response_model=BulkResultResponse)
async def import_tasks(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import tasks from a CSV file."""
    content = await file.read()
    from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content
    try:
        await inspect_upload_content(
            content,
            filename=file.filename,
            declared_content_type=file.content_type,
            allowed_extensions={".csv"},
        )
    except UploadSecurityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    csv_text = content.decode("utf-8")
    count = await import_tasks_csv(db, user.entity_id, csv_text, creator_id=user.id)
    return {"count": count}
