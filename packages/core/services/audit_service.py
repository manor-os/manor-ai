"""Audit log service — create and query audit entries."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid


async def log_action(
    db: AsyncSession,
    entity_id: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Insert a new audit log entry."""
    entry = AuditLog(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()
    return entry


async def list_audit_logs(
    db: AsyncSession,
    entity_id: str,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """Return paginated audit logs for an entity, plus total count."""
    base = select(AuditLog).where(AuditLog.entity_id == entity_id)
    count_q = select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == entity_id)

    if action:
        base = base.where(AuditLog.action == action)
        count_q = count_q.where(AuditLog.action == action)
    if resource_type:
        base = base.where(AuditLog.resource_type == resource_type)
        count_q = count_q.where(AuditLog.resource_type == resource_type)

    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return list(rows), total
