"""Task domain-event notification fan-out."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.staff import Staff
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.services.notification_service import create_notification

logger = logging.getLogger(__name__)


_TASK_EVENT_COPY: dict[str, tuple[str, str]] = {
    "task.created": ("task_created", "New task"),
    "task.assigned": ("task_assigned", "Task assigned"),
    "task.status_changed": ("task_status_changed", "Task updated"),
    "task.retried": ("task_retried", "Task retry started"),
    "task.failed": ("task_failed", "Task failed"),
    "task.succeeded": ("task_succeeded", "Task completed"),
    "task.hitl_requested": ("task_hitl_requested", "Input needed"),
    "task.hitl_reminder": ("task_hitl_reminder", "Input still needed"),
}


def task_event_title(event_type: str) -> str:
    copy = _TASK_EVENT_COPY.get(event_type)
    return copy[1] if copy else "Task updated"


def task_event_message(event_type: str, payload: dict[str, Any], task: Task | None) -> str:
    title = (task.title if task else None) or payload.get("title") or "this task"
    if event_type == "task.created":
        return f"{title} was created."
    if event_type == "task.assigned":
        return f"You were assigned to {title}."
    if event_type == "task.status_changed":
        new_status = payload.get("new_status")
        if new_status:
            return f"{title} status changed to {new_status}."
        return f"{title} was updated."
    if event_type == "task.retried":
        reset_steps = int(payload.get("reset_steps") or 0)
        if reset_steps > 0:
            return f"Retry restarted {reset_steps} step(s) for {title}."
        return f"Retry restarted {title}."
    if event_type == "task.failed":
        return f"{title} failed and may need review."
    if event_type == "task.succeeded":
        return f"{title} completed successfully."
    if event_type == "task.hitl_requested":
        return f"{title} is waiting for your input."
    if event_type == "task.hitl_reminder":
        wait_minutes = payload.get("wait_minutes")
        prompt = (payload.get("prompt") or "").strip()
        if isinstance(wait_minutes, int) and wait_minutes > 0:
            base = f"{title} has been waiting for human input for about {wait_minutes} minute(s)."
        else:
            base = f"{title} is still waiting for human input."
        return f"{base}\n\n{prompt}" if prompt else base
    return f"{title} was updated."


async def task_event_recipient_users(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> tuple[Task | None, list[User]]:
    """Resolve active users who should receive a user-facing task event."""
    event_payload = dict(payload or {})

    task: Task | None = None
    task_id = event_payload.get("task_id")
    if task_id:
        task = (await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.entity_id == entity_id,
            )
        )).scalar_one_or_none()

    recipient_ids = {
        event_payload.get("requested_by"),
        event_payload.get("user_id"),
        event_payload.get("creator_id"),
        event_payload.get("assignee_id"),
        event_payload.get("assigned_by"),
        event_payload.get("changed_by"),
        task.creator_id if task else None,
        task.assignee_id if task else None,
    }
    recipient_ids = {rid for rid in recipient_ids if rid}
    admin_fallback_events = {"task.hitl_requested", "task.hitl_reminder"}
    if not recipient_ids and event_type not in admin_fallback_events:
        return task, []

    users = await _resolve_recipient_users(db, entity_id, recipient_ids)

    if not users and event_type in admin_fallback_events:
        users = list((await db.execute(
            select(User).where(
                User.entity_id == entity_id,
                User.status == "active",
                User.deleted_at.is_(None),
                User.role.in_(("owner", "admin")),
            )
        )).scalars().all())

    return task, users


async def _resolve_recipient_users(
    db: AsyncSession,
    entity_id: str,
    identity_ids: set[str],
) -> list[User]:
    """Resolve task participant ids into active login users.

    ``Task.assignee_id`` is intentionally broad: older and workspace flows may
    store a ``users.id`` directly, while staff-aware assignment stores a
    ``staff.id``. Notifications must fan out to real login users, so this
    helper accepts both forms and dedupes the final User rows.
    """
    if not identity_ids:
        return []

    resolved_user_ids: set[str] = set(identity_ids)

    staff_rows = list((await db.execute(
        select(Staff).where(
            Staff.id.in_(identity_ids),
            Staff.entity_id == entity_id,
            Staff.deleted_at.is_(None),
            Staff.status == "active",
        )
    )).scalars().all())
    staff_ids = {staff.id for staff in staff_rows}
    resolved_user_ids.update(staff.user_id for staff in staff_rows if staff.user_id)

    if staff_ids:
        membership_rows = list((await db.execute(
            select(UserMembership).where(
                UserMembership.entity_id == entity_id,
                UserMembership.staff_id.in_(staff_ids),
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )).scalars().all())
        resolved_user_ids.update(row.user_id for row in membership_rows if row.user_id)

    resolved_user_ids = {uid for uid in resolved_user_ids if uid}
    if not resolved_user_ids:
        return []

    membership_user_ids = {
        row.user_id for row in (await db.execute(
            select(UserMembership).where(
                UserMembership.entity_id == entity_id,
                UserMembership.user_id.in_(resolved_user_ids),
                UserMembership.status == "active",
                UserMembership.deleted_at.is_(None),
            )
        )).scalars().all()
    }
    users = list((await db.execute(
        select(User).where(
            User.id.in_(resolved_user_ids),
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )).scalars().all())
    return [
        user
        for user in users
        if user.entity_id == entity_id or user.id in membership_user_ids
    ]


async def notify_task_event(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> int:
    """Create in-app notifications for user-facing task domain events.

    The event log remains the source of truth. This helper only fans out a
    small whitelist of task events to directly involved users.
    """
    event_payload = dict(payload or {})
    copy = _TASK_EVENT_COPY.get(event_type)
    if not copy:
        return 0

    task, users = await task_event_recipient_users(db, entity_id, event_type, event_payload)
    if not users:
        return 0

    notification_type, _title = copy
    task_id = event_payload.get("task_id")
    link = f"/tasks/{task_id}" if task_id else None
    meta = {
        "event_type": event_type,
        **event_payload,
    }
    delivered = 0
    for user in users:
        try:
            await create_notification(
                db,
                entity_id=entity_id,
                user_id=user.id,
                type=notification_type,
                title=task_event_title(event_type),
                body=task_event_message(event_type, event_payload, task),
                link=link,
                meta=meta,
            )
            delivered += 1
        except Exception:
            logger.debug(
                "task_event_notifications: failed for event=%s user=%s",
                event_type,
                user.id,
                exc_info=True,
            )
    return delivered
