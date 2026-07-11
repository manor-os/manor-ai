"""Field-level change tracking — records before/after diffs on entity updates.

Usage in service functions:
    from packages.core.services.change_tracker import track_changes

    changes = track_changes(task, {"status": "completed", "title": "New Title"})
    # Returns: [{"field": "status", "old": "pending", "new": "completed"}, {"field": "title", "old": "Old", "new": "New Title"}]
"""
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def track_changes(obj, updates: dict, *, skip_fields: set = None) -> list[dict]:
    """Compare an object's current values with proposed updates.
    Returns list of {field, old, new} for changed fields only.
    """
    skip = skip_fields or {"updated_at", "created_at", "password_hash", "totp_secret", "key_hash"}
    changes = []
    for field, new_val in updates.items():
        if field in skip or new_val is None:
            continue
        old_val = getattr(obj, field, None)
        if old_val != new_val:
            changes.append({
                "field": field,
                "old": _serialize(old_val),
                "new": _serialize(new_val),
            })
    return changes


def _serialize(val: Any) -> Any:
    """Serialize a value for storage in JSONB."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, (dict, list, str, int, float, bool, type(None))):
        return val
    return str(val)


async def record_change(
    db: AsyncSession, entity_id: str, resource_type: str, resource_id: str,
    changes: list[dict], *, user_id: str = None, action: str = "update",
) -> None:
    """Record field-level changes to the audit log.
    Stores changes in the audit_log.details JSONB field.
    """
    if not changes:
        return

    from packages.core.services.audit_service import log_action
    await log_action(
        db, entity_id,
        action=f"{resource_type}.{action}",
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        details={"changes": changes},
    )


async def get_change_history(
    db: AsyncSession, entity_id: str, resource_type: str, resource_id: str,
    limit: int = 50,
) -> list[dict]:
    """Get the change history for a specific resource.
    Returns: [{action, user_id, changes: [{field, old, new}], created_at}, ...]
    """
    from sqlalchemy import select
    from packages.core.models.audit import AuditLog

    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.entity_id == entity_id,
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "action": r.action,
            "user_id": r.user_id,
            "changes": (r.details or {}).get("changes", []),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
