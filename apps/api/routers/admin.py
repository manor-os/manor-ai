"""Admin endpoints — audit logs, entity settings, user preferences."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.audit_service import list_audit_logs, log_action
from packages.core.services.settings_service import (
    get_entity_settings,
    get_user_preferences,
    update_entity_settings,
    update_user_preferences,
)
from apps.api.deps import get_current_user, require_permission
from packages.core.permissions import Permission

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── Schemas ──

class AuditLogResponse(BaseModel):
    id: str
    entity_id: str | None = None
    user_id: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict = {}
    ip_address: str | None = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


class AuditLogCreateRequest(BaseModel):
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict = {}


class SettingsResponse(BaseModel):
    settings: dict


class PreferencesResponse(BaseModel):
    preferences: dict


# ── Audit logs ──

@router.get("/audit-logs", response_model=AuditLogListResponse)
async def get_audit_logs(
    action: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission(Permission.ADMIN_AUDIT)),
    db: AsyncSession = Depends(get_db),
):
    """List audit logs for the current entity."""
    items, total = await list_audit_logs(
        db, user.entity_id,
        action=action, resource_type=resource_type,
        limit=limit, offset=offset,
    )
    return AuditLogListResponse(
        items=[
            AuditLogResponse(
                id=e.id, entity_id=e.entity_id, user_id=e.user_id,
                action=e.action, resource_type=e.resource_type,
                resource_id=e.resource_id, details=e.details or {},
                ip_address=e.ip_address, created_at=e.created_at,
            )
            for e in items
        ],
        total=total,
    )


@router.post("/audit-logs", response_model=AuditLogResponse, status_code=201)
async def create_audit_log(
    req: AuditLogCreateRequest,
    user: User = Depends(require_permission(Permission.ADMIN_AUDIT)),
    db: AsyncSession = Depends(get_db),
):
    """Create an audit log entry (also useful for testing)."""
    entry = await log_action(
        db, user.entity_id,
        action=req.action,
        resource_type=req.resource_type,
        resource_id=req.resource_id,
        user_id=user.id,
        details=req.details,
    )
    return AuditLogResponse(
        id=entry.id, entity_id=entry.entity_id, user_id=entry.user_id,
        action=entry.action, resource_type=entry.resource_type,
        resource_id=entry.resource_id, details=entry.details or {},
        ip_address=entry.ip_address, created_at=entry.created_at,
    )


# ── Change history ──

@router.get("/changes")
async def get_change_history(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_permission(Permission.ADMIN_AUDIT)),
    db: AsyncSession = Depends(get_db),
):
    """Get field-level change history for any resource."""
    from packages.core.services.change_tracker import get_change_history as _get_history
    history = await _get_history(db, user.entity_id, resource_type, resource_id, limit=limit)
    return {"items": history, "total": len(history)}


# ── Entity settings ──

@router.get("/settings")
async def get_settings(
    user: User = Depends(require_permission(Permission.ADMIN_SETTINGS)),
    db: AsyncSession = Depends(get_db),
):
    """Get entity-level settings (flat dict)."""
    settings = await get_entity_settings(db, user.entity_id)
    return {**settings, "settings": settings}


@router.put("/settings")
async def put_settings(
    body: dict,
    user: User = Depends(require_permission(Permission.ADMIN_SETTINGS)),
    db: AsyncSession = Depends(get_db),
):
    """Partial merge into entity settings (flat dict)."""
    settings = await update_entity_settings(db, user.entity_id, body)
    return {**settings, "settings": settings}


# ── User preferences ──

@router.get("/preferences")
async def get_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's preferences (flat dict)."""
    preferences = await get_user_preferences(db, user.id)
    return {**preferences, "preferences": preferences}


@router.put("/preferences")
async def put_preferences(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial merge into user preferences (flat dict)."""
    preferences = await update_user_preferences(db, user.id, body)
    return {**preferences, "preferences": preferences}
