"""Task automation service — SLA monitoring, escalation, auto-reassignment, templates."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import (
    Task, TaskSlaPolicy, TaskEscalationRule, TaskChecklist,
)
from packages.core.models.task_template import TaskTemplate
from packages.core.models.staff import Staff

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SLA deadline check
# ---------------------------------------------------------------------------

async def check_sla_deadlines(db: AsyncSession, entity_id: str | None = None) -> int:
    """Check all open tasks for SLA breaches. Called by the scheduler.

    For each task that has a linked SLA policy:
      - If response deadline has passed and task is still 'pending', mark breached.
      - If resolution deadline has passed and task is not completed, mark breached.
    Returns the number of newly breached tasks.
    """
    # Build base query: open tasks with an SLA policy that are not yet breached
    q = (
        select(Task, TaskSlaPolicy)
        .join(TaskSlaPolicy, Task.sla_policy_id == TaskSlaPolicy.id)
        .where(
            Task.sla_breached == False,  # noqa: E712
            Task.status.notin_(["completed", "cancelled", "failed"]),
            TaskSlaPolicy.status == "active",
        )
    )
    if entity_id:
        q = q.where(Task.entity_id == entity_id)

    result = await db.execute(q)
    rows = result.all()

    now = datetime.now(timezone.utc)
    breached_count = 0

    for task, sla in rows:
        created = task.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        # Response SLA: time from creation to first status change out of 'pending'
        response_deadline = created + timedelta(seconds=sla.response_seconds)
        resolution_deadline = created + timedelta(seconds=sla.resolution_seconds)

        is_breached = False

        # Response breach: still pending after response_seconds
        if task.status == "pending" and now > response_deadline:
            is_breached = True

        # Resolution breach: not completed after resolution_seconds
        if now > resolution_deadline:
            is_breached = True

        if is_breached:
            task.sla_breached = True
            breached_count += 1
            logger.info("SLA breach detected for task %s (entity=%s)", task.id, task.entity_id)

            # Trigger first escalation
            await escalate_task(db, task.id, task.entity_id)

            # Send SLA breach notification (writes notification row + pushes via WS)
            await send_task_notification(db, task.id, "sla_breach", task.entity_id)

            # Push a live task_update so the open TaskDetail page flips
            # the SLA chip from teal → red without waiting for the poll.
            try:
                from packages.core.services.realtime import (
                    broadcast_task_update, push_task_update_multi,
                )
                summary = {
                    "id": task.id, "title": task.title, "status": task.status,
                    "priority": task.priority, "sla_breached": True,
                    "event": "sla_breached",
                }
                await push_task_update_multi(
                    [task.creator_id, task.assignee_id], summary,
                )
                await broadcast_task_update(task.entity_id, summary)
            except Exception:
                logger.debug("realtime push failed for SLA breach", exc_info=True)

    if breached_count:
        await db.flush()

    return breached_count


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

async def escalate_task(db: AsyncSession, task_id: str, entity_id: str) -> bool:
    """Apply the next escalation level to a breached task.

    Loads the task's SLA policy, finds the next escalation rule by level,
    and executes the configured action (notify, reassign, or escalate).
    Returns True if an escalation rule was applied.
    """
    task = await _get_task(db, task_id, entity_id)
    if not task or not task.sla_policy_id:
        return False

    current_level = task.escalation_level or 0
    next_level = current_level + 1

    # Find the next escalation rule
    result = await db.execute(
        select(TaskEscalationRule).where(
            TaskEscalationRule.sla_policy_id == task.sla_policy_id,
            TaskEscalationRule.escalation_level == next_level,
            TaskEscalationRule.status == "active",
        )
    )
    rule = result.scalar_one_or_none()
    if not rule:
        logger.debug("No escalation rule at level %d for task %s", next_level, task_id)
        return False

    # Check if delay has elapsed since the SLA was breached
    # (For simplicity, we check delay from task creation + SLA deadline)
    # In production you'd track breach_at timestamp; here we proceed immediately on first call

    # Execute action
    action = (rule.action_type or "notify").lower()

    if action == "reassign":
        await auto_reassign_task(db, task_id, entity_id)

    if action in ("notify", "escalate"):
        # Notify configured users
        if rule.notify_user_ids:
            for user_id in rule.notify_user_ids:
                if user_id:
                    await _create_notification(
                        db, entity_id, user_id,
                        type="sla_escalation",
                        title=f"Task escalated (level {next_level}): {task.title}",
                        body=f"Task #{task_id} has been escalated to level {next_level}.",
                        link=f"/tasks/{task_id}",
                    )

    # Bump escalation level
    task.escalation_level = next_level
    await db.flush()

    # Log
    from packages.core.services.task_service import add_task_log
    await add_task_log(
        db, task_id, "escalation",
        f"Escalated to level {next_level} — action: {action}",
        created_by="system",
    )

    from packages.core.services.event_emitter import emit
    emit(entity_id, "task.escalated", source="task_automation", payload={
        "task_id": task_id, "escalation_level": next_level, "action": action,
    })

    logger.info("Escalated task %s to level %d (action=%s)", task_id, next_level, action)
    return True


# ---------------------------------------------------------------------------
# Auto-reassignment
# ---------------------------------------------------------------------------

async def auto_reassign_task(db: AsyncSession, task_id: str, entity_id: str) -> Optional[str]:
    """Reassign a task to the next available staff member based on skills and workload.

    Strategy: find active staff with matching required_skills, then pick the one
    with the lowest count of active (non-completed) tasks.
    Returns the new assignee_id, or None if no match found.
    """
    task = await _get_task(db, task_id, entity_id)
    if not task:
        return None

    required = task.required_skills or []

    # Find eligible staff
    staff_q = select(Staff).where(
        Staff.entity_id == entity_id,
        Staff.status == "active",
        Staff.deleted_at.is_(None),
    )
    result = await db.execute(staff_q)
    all_staff = list(result.scalars().all())

    if not all_staff:
        logger.warning("No active staff for entity %s — cannot reassign task %s", entity_id, task_id)
        return None

    # Filter by skills if required
    eligible = all_staff
    if required:
        required_set = set(required)
        eligible = [
            s for s in all_staff
            if s.skills and required_set.issubset(set(s.skills))
        ]

    if not eligible:
        # Fall back to all staff if no skill match
        eligible = all_staff

    # Exclude current assignee
    eligible = [s for s in eligible if s.id != task.assignee_id]
    if not eligible:
        eligible = all_staff  # last resort

    # Count active tasks per eligible staff member
    staff_ids = [s.id for s in eligible]
    workload_q = (
        select(Task.assignee_id, func.count(Task.id).label("cnt"))
        .where(
            Task.entity_id == entity_id,
            Task.assignee_id.in_(staff_ids),
            Task.status.notin_(["completed", "cancelled", "failed"]),
        )
        .group_by(Task.assignee_id)
    )
    workload_result = await db.execute(workload_q)
    workload = {row.assignee_id: row.cnt for row in workload_result.all()}

    # Pick staff with lowest workload
    best = min(eligible, key=lambda s: workload.get(s.id, 0))

    old_assignee = task.assignee_id
    task.assignee_id = best.id
    await db.flush()

    from packages.core.services.task_service import add_task_log
    await add_task_log(
        db, task_id, "reassign",
        f"Auto-reassigned from {old_assignee or 'unassigned'} to {best.id} ({best.name})",
        created_by="system",
    )

    from packages.core.services.event_emitter import emit
    emit(entity_id, "task.reassigned", source="task_automation", payload={
        "task_id": task_id, "old_assignee": old_assignee, "new_assignee": best.id,
    })

    # Notify new assignee
    if best.user_id:
        await _create_notification(
            db, entity_id, best.user_id,
            type="task_assigned",
            title=f"Task assigned to you: {task.title}",
            body="You have been auto-assigned this task based on your skills and availability.",
            link=f"/tasks/{task_id}",
        )

    logger.info("Auto-reassigned task %s to staff %s (%s)", task_id, best.id, best.name)
    return best.id


# ---------------------------------------------------------------------------
# Template-based task creation
# ---------------------------------------------------------------------------

async def create_task_from_template(
    db: AsyncSession,
    entity_id: str,
    template_id: str,
    *,
    creator_id: str | None = None,
    **overrides,
) -> Task:
    """Create a task pre-filled from a template, including checklist items from steps."""
    result = await db.execute(
        select(TaskTemplate).where(
            TaskTemplate.id == template_id,
            TaskTemplate.entity_id == entity_id,
            TaskTemplate.status == "active",
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise ValueError(f"Template {template_id} not found or inactive")

    # Resolve title — use title_template, allow override
    title = overrides.pop("title", None) or template.title_template or template.name

    from packages.core.services.task_service import create_task
    task = await create_task(
        db,
        entity_id,
        title=title,
        description=overrides.pop("description", None) or template.description_template or template.description or "",
        priority=overrides.pop("priority", None) or template.priority,
        task_type=overrides.pop("task_type", None) or template.task_type,
        category_id=overrides.pop("category_id", None) or template.category_id,
        assignee_id=overrides.pop("assignee_id", None) or template.default_assignee_id,
        agent_id=overrides.pop("agent_id", None) or template.default_agent_id,
        agent_type=overrides.pop("agent_type", None) or template.agent_type,
        creator_id=creator_id,
        details=overrides.pop("details", None) or template.details_template or {},
        deadline=overrides.pop("deadline", None),
    )

    # Set automation fields on the created task
    task.template_id = template_id
    task.sla_policy_id = template.sla_policy_id
    task.estimated_hours = template.estimated_hours
    task.required_skills = template.required_skills or []
    await db.flush()

    # Create checklist items from template steps
    steps = template.steps or []
    if isinstance(steps, list):
        for idx, step in enumerate(steps):
            content = step if isinstance(step, str) else step.get("content", step.get("name", ""))
            if content:
                item = TaskChecklist(
                    id=generate_ulid(),
                    task_id=task.id,
                    content=content,
                    sort_order=idx,
                )
                db.add(item)
        await db.flush()

    logger.info("Created task %s from template %s", task.id, template_id)
    return task


# ---------------------------------------------------------------------------
# Task notifications
# ---------------------------------------------------------------------------

async def send_task_notification(
    db: AsyncSession,
    task_id: str,
    event_type: str,
    entity_id: str,
) -> None:
    """Send notification for task events (created, assigned, status_changed, sla_breach).

    Notifies the task assignee. For sla_breach events, also flags for email delivery.
    """
    task = await _get_task(db, task_id, entity_id)
    if not task:
        return

    type_titles = {
        "created": f"New task: {task.title}",
        "assigned": f"Task assigned: {task.title}",
        "status_changed": f"Task updated: {task.title}",
        "sla_breach": f"SLA breached: {task.title}",
    }
    title = type_titles.get(event_type, f"Task event ({event_type}): {task.title}")

    type_bodies = {
        "created": "A new task has been created.",
        "assigned": "You have been assigned to this task.",
        "status_changed": f"Task status is now: {task.status}",
        "sla_breach": "This task has exceeded its SLA deadline. Immediate attention required.",
    }
    body = type_bodies.get(event_type, f"Task event: {event_type}")

    # Notify assignee
    notify_user_id = task.assignee_id
    if notify_user_id:
        await _create_notification(
            db, entity_id, notify_user_id,
            type=f"task_{event_type}",
            title=title,
            body=body,
            link=f"/tasks/{task_id}",
        )

    # For SLA breach, also notify the task creator
    if event_type == "sla_breach" and task.creator_id and task.creator_id != notify_user_id:
        await _create_notification(
            db, entity_id, task.creator_id,
            type="task_sla_breach",
            title=title,
            body=body,
            link=f"/tasks/{task_id}",
        )

    await db.flush()


# ---------------------------------------------------------------------------
# Checklist management
# ---------------------------------------------------------------------------

async def manage_task_checklist(
    db: AsyncSession,
    task_id: str,
    items: list[dict],
) -> list[TaskChecklist]:
    """Create/update/delete checklist items for a task.

    Each item dict may contain:
      - id (optional): if present, update existing; if missing, create new
      - content: text content
      - is_completed: bool
      - sort_order: int
      - _delete: if True, delete this item
    Returns the final list of checklist items.
    """
    # Load existing items
    result = await db.execute(
        select(TaskChecklist)
        .where(TaskChecklist.task_id == task_id)
        .order_by(TaskChecklist.sort_order)
    )
    existing = {item.id: item for item in result.scalars().all()}

    final_items = []
    for idx, item_data in enumerate(items):
        item_id = item_data.get("id")

        if item_data.get("_delete") and item_id and item_id in existing:
            await db.delete(existing[item_id])
            continue

        if item_id and item_id in existing:
            # Update
            item = existing[item_id]
            if "content" in item_data:
                item.content = item_data["content"]
            if "is_completed" in item_data:
                item.is_completed = item_data["is_completed"]
            item.sort_order = item_data.get("sort_order", idx)
            final_items.append(item)
        else:
            # Create
            item = TaskChecklist(
                id=generate_ulid(),
                task_id=task_id,
                content=item_data.get("content", ""),
                is_completed=item_data.get("is_completed", False),
                sort_order=item_data.get("sort_order", idx),
            )
            db.add(item)
            final_items.append(item)

    await db.flush()
    return final_items


async def get_task_checklist(db: AsyncSession, task_id: str) -> list[TaskChecklist]:
    """Get all checklist items for a task."""
    result = await db.execute(
        select(TaskChecklist)
        .where(TaskChecklist.task_id == task_id)
        .order_by(TaskChecklist.sort_order)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_task(db: AsyncSession, task_id: str, entity_id: str) -> Optional[Task]:
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def _create_notification(
    db: AsyncSession, entity_id: str, user_id: str,
    *, type: str, title: str, body: str | None = None, link: str | None = None,
):
    """Thin wrapper around notification_service.create_notification."""
    from packages.core.services.notification_service import create_notification
    return await create_notification(db, entity_id, user_id, type, title, body=body, link=link)
