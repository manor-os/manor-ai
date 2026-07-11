"""Backup endpoints — entity data export and download."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.backup_service import export_entity_data, get_export_summary
from apps.api.deps import get_current_user, require_permission
from packages.core.permissions import Permission

router = APIRouter(prefix="/api/v1/backup", tags=["backup"])


# ── Schemas ──

class ExportSummaryResponse(BaseModel):
    entity_id: str
    users: int
    workspaces: int
    tasks: int
    conversations: int
    documents: int
    agents: int
    clients: int
    staff_members: int


# ── Endpoints ──

@router.get("/summary", response_model=ExportSummaryResponse)
async def backup_summary(
    user: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Get a summary of exportable data (counts only). Requires owner role."""
    return await get_export_summary(db, user.entity_id)


@router.get("/export")
async def backup_export(
    user: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Full entity data export as JSON. Requires owner role."""
    data = await export_entity_data(db, user.entity_id)
    return data


@router.post("/export/download")
async def backup_export_download(
    user: User = Depends(require_permission(Permission.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Export entity data and return as a downloadable JSON file. Requires owner role."""
    data = await export_entity_data(db, user.entity_id)
    filename = f"manor-backup-{user.entity_id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    content = json.dumps(data, indent=2, ensure_ascii=False)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
