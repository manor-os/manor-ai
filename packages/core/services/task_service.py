"""Task service — CRUD, status changes, processing logs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task, TaskLog, TaskCategory, TaskSlaPolicy
from packages.core.services.task_dependencies import dependency_ids_from_details, details_with_dependency_state
from packages.core.services.task_state_machine import (
    TERMINAL_STATUSES,
    TaskStatusTransitionError,
    apply_task_status_transition,
)

logger = logging.getLogger(__name__)


# ── Tasks ──

async def list_tasks(
    db: AsyncSession, entity_id: str, *,
    status: str | None = None,
    workspace_id: str | None = None,
    category_id: str | None = None,
    assignee_id: str | None = None,
    completed_after: str | datetime | None = None,
    completed_before: str | datetime | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_automations: bool = False,
) -> tuple[list[Task], int]:
    """List tasks for the Tasks page.

    Automation-linked tasks (rows the scheduler lazily created for a
    ScheduledJob, identified by ``details.scheduled_job_id``) are hidden
    by default — their run history is surfaced on the Automations page
    via ``scheduled_job_runs`` instead, so they shouldn't pollute the
    user's Kanban. Pass ``include_automations=True`` to opt in.

    ``parent_task_id`` lets the Task Detail page query for subtasks
    (children of a parent task). Pass the parent's id to get just its
    direct children. Subtasks are NOT excluded by the automation filter
    — they're explicitly user-relevant in this scope.
    """
    q = select(Task).where(Task.entity_id == entity_id)
    count_q = select(func.count()).select_from(Task).where(Task.entity_id == entity_id)

    if status:
        q = q.where(Task.status == status)
        count_q = count_q.where(Task.status == status)
    if workspace_id:
        q = q.where(Task.workspace_id == workspace_id)
        count_q = count_q.where(Task.workspace_id == workspace_id)
    if category_id:
        q = q.where(Task.category_id == category_id)
        count_q = count_q.where(Task.category_id == category_id)
    if assignee_id:
        q = q.where(Task.assignee_id == assignee_id)
        count_q = count_q.where(Task.assignee_id == assignee_id)
    if completed_after:
        after_dt = _coerce_datetime(completed_after)
        q = q.where(Task.completed_at.isnot(None), Task.completed_at >= after_dt)
        count_q = count_q.where(Task.completed_at.isnot(None), Task.completed_at >= after_dt)
    if completed_before:
        before_dt = _coerce_datetime(completed_before)
        q = q.where(Task.completed_at.isnot(None), Task.completed_at <= before_dt)
        count_q = count_q.where(Task.completed_at.isnot(None), Task.completed_at <= before_dt)
    if parent_task_id:
        q = q.where(Task.parent_task_id == parent_task_id)
        count_q = count_q.where(Task.parent_task_id == parent_task_id)
    if not include_automations and not parent_task_id:
        automation_filter = Task.details["scheduled_job_id"].astext.is_(None)
        q = q.where(automation_filter)
        count_q = count_q.where(automation_filter)

    q = q.order_by(Task.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _enforce_dependency_gate_for_start(
    db: AsyncSession,
    task: Task,
    fields: dict,
) -> None:
    """Prevent manual/API starts from bypassing predecessor task outputs."""
    if fields.get("status") != "in_progress":
        return
    details = fields.get("details") if isinstance(fields.get("details"), dict) else task.details
    dep_ids = dependency_ids_from_details(details)
    if not dep_ids:
        return
    gated_details = await details_with_dependency_state(db, task, dict(details or {}))
    fields["details"] = gated_details
    if gated_details.get("dependency_status") != "completed":
        raise TaskStatusTransitionError(
            task.status,
            "in_progress",
            "Task dependencies are not completed yet; waiting for predecessor outputs.",
        )


async def get_task(db: AsyncSession, task_id: str, entity_id: str) -> Optional[Task]:
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def create_task(
    db: AsyncSession, entity_id: str, *,
    title: str,
    description: str = "",
    priority: int = 3,
    task_type: str = "general",
    workspace_id: str | None = None,
    category_id: str | None = None,
    assignee_id: str | None = None,
    agent_id: str | None = None,
    agent_type: str | None = None,
    creator_id: str | None = None,
    conversation_id: str | None = None,
    details: dict | None = None,
    deadline: str | None = None,
    scheduled_at: str | None = None,
    duration_minutes: int | None = None,
) -> Task:
    merged_details = dict(details or {})
    if scheduled_at:
        merged_details["scheduled_at"] = scheduled_at
    if duration_minutes:
        merged_details["duration_minutes"] = duration_minutes
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title=title,
        description=description or None,
        priority=priority,
        task_type=task_type,
        workspace_id=workspace_id,
        category_id=category_id,
        assignee_id=assignee_id,
        agent_id=agent_id,
        agent_type=agent_type,
        creator_id=creator_id,
        owner_id=creator_id,
        conversation_id=conversation_id,
        details=merged_details,
        deadline=_coerce_datetime(deadline) if deadline else None,
    )
    db.add(task)
    await db.flush()

    # Log creation
    await add_task_log(db, task.id, "create", f"Task created: {title}", created_by=creator_id or "system")

    from packages.core.services.event_emitter import emit
    emit(entity_id, "task.created", source="task_service", payload={
        "task_id": task.id,
        "title": title,
        "creator_id": creator_id,
        "assignee_id": assignee_id,
        "workspace_id": workspace_id,
    })
    if task.workspace_id:
        from packages.core.workspace_chat.context import invalidate
        invalidate(task.workspace_id)

    # Real-time push — surfaces the new task in everyone's list without
    # waiting for the next poll. Fans out to creator + assignee (deduped)
    # plus an entity-wide broadcast so admins on /tasks see it too.
    from packages.core.services.realtime import (
        broadcast_task_update, push_task_update_multi,
    )
    summary = {
        "id": task.id, "title": task.title, "status": task.status,
        "priority": task.priority, "event": "created",
    }
    await push_task_update_multi([creator_id, assignee_id], summary)
    await broadcast_task_update(entity_id, summary)

    return task


async def update_task(db: AsyncSession, task_id: str, entity_id: str, *, user_id: str | None = None, **fields) -> Optional[Task]:
    task = await get_task(db, task_id, entity_id)
    if not task:
        return None

    # Field-level change tracking
    from packages.core.services.change_tracker import track_changes, record_change
    changes = track_changes(task, fields)

    # Nullable fields that can be explicitly cleared (set to None)
    _clearable = {"assignee_id", "agent_id", "agent_type", "deadline", "category_id", "vendor_id", "parent_task_id", "template_id", "actual_output"}

    old_status = task.status
    old_assignee_id = task.assignee_id
    new_status = fields.get("status")
    if new_status is not None and new_status != old_status:
        await _enforce_dependency_gate_for_start(db, task, fields)
        apply_task_status_transition(task, new_status)

    for k, v in fields.items():
        if not hasattr(task, k):
            continue
        if k == "status":
            continue
        if v is None and k not in _clearable:
            continue
        # Parse date/datetime strings for datetime columns
        if k == "deadline" and isinstance(v, str):
            v = _coerce_datetime(v)
        setattr(task, k, v)

    # Track status transitions
    if new_status is not None and new_status != old_status:
        await add_task_log(db, task_id, "status_change", f"Status: {old_status} → {fields['status']}")

        from packages.core.services.event_emitter import emit
        emit(entity_id, "task.status_changed", source="task_service", payload={
            "task_id": task_id, "title": task.title,
            "old_status": old_status, "new_status": new_status,
            "creator_id": task.creator_id,
            "assignee_id": task.assignee_id,
            "changed_by": user_id,
            "workspace_id": task.workspace_id,
        })

        # Plan-driven dispatch: when a task with an owner_subscription
        # transitions to in_progress, hand it to the Planner asynchronously.
        # The legacy ``run_agent_task`` Celery path stays available for
        # plain TaskRunner-driven tasks (no owner_subscription), so this
        # hook is purely additive — old call sites unaffected.
        if (
            new_status == "in_progress"
            and old_status != "in_progress"
            and (task.owner_subscription_id or task.owner_service_key)
        ):
            try:
                from packages.core.tasks.ai_tasks import plan_and_run_task
                plan_and_run_task.delay(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "task %s: failed to dispatch plan_and_run_task: %s",
                    task_id, exc,
                )
        if new_status in TERMINAL_STATUSES:
            try:
                from packages.core.services.workspace_operation_service import check_work_batch_completion

                await check_work_batch_completion(
                    db,
                    task,
                    trigger_source="task_service.update_task",
                )
            except Exception:
                logger.warning(
                    "task %s: failed to evaluate workspace work batch completion",
                    task_id,
                    exc_info=True,
                )

    new_assignee_id = fields.get("assignee_id") if "assignee_id" in fields else old_assignee_id
    if new_assignee_id and new_assignee_id != old_assignee_id:
        await add_task_log(
            db,
            task_id,
            "assignment_change",
            f"Assigned to {new_assignee_id}",
            created_by=user_id or "system",
        )
        from packages.core.services.event_emitter import emit
        emit(entity_id, "task.assigned", source="task_service", payload={
            "task_id": task_id,
            "title": task.title,
            "creator_id": task.creator_id,
            "assignee_id": new_assignee_id,
            "previous_assignee_id": old_assignee_id,
            "assigned_by": user_id,
            "workspace_id": task.workspace_id,
        })

    await db.flush()

    # Record field-level changes to audit log
    await record_change(db, entity_id, "task", task_id, changes, user_id=user_id)
    if task.workspace_id:
        from packages.core.workspace_chat.context import invalidate
        invalidate(task.workspace_id)

    # Real-time push — status / assignee / priority changes should
    # reflect immediately in Kanban boards + task detail pages.
    from packages.core.services.realtime import (
        broadcast_task_update, push_task_update_multi,
    )
    summary = {
        "id": task.id, "title": task.title, "status": task.status,
        "priority": task.priority, "event": "updated",
    }
    await push_task_update_multi([task.creator_id, task.assignee_id, user_id], summary)
    await broadcast_task_update(entity_id, summary)

    return task


# ── Task Logs ──

async def add_task_log(
    db: AsyncSession, task_id: str, log_type: str, content: str,
    *, created_by: str = "system", metadata: dict | None = None,
) -> TaskLog:
    log = TaskLog(
        id=generate_ulid(),
        task_id=task_id,
        log_type=log_type,
        content=content,
        created_by=created_by,
        meta=metadata or {},
    )
    db.add(log)
    await db.flush()
    return log


async def agent_log_authorship(
    db: AsyncSession,
    agent_id: str | None,
    *,
    fallback: str | None = None,
) -> tuple[str, dict | None]:
    """Resolve ``(created_by, metadata)`` for a task log/comment authored by
    an agent.

    Stamps the running agent's id (and display name, best-effort) into the
    log metadata so the activity UI renders the specific agent persona
    instead of a generic ``workspace-agent`` label. The task-log serializer
    reads ``author_agent_id``/``author_agent_name`` back out of this metadata,
    and the frontend resolves the id against the workspace's agent list.
    """
    resolved = (agent_id or "").strip() or None
    if not resolved:
        return (fallback or "workspace-agent"), None
    meta: dict = {"agent_id": resolved}
    try:
        from packages.core.services.agent_service import get_agent

        agent = await get_agent(db, resolved) or {}
        if agent.get("name"):
            meta["agent_name"] = agent["name"]
        if agent.get("agent_type"):
            meta["agent_type"] = agent["agent_type"]
    except Exception:  # pragma: no cover - name is a display nicety, never fatal
        pass
    return resolved, meta


async def get_task_logs(db: AsyncSession, task_id: str) -> list[TaskLog]:
    result = await db.execute(
        select(TaskLog).where(TaskLog.task_id == task_id).order_by(TaskLog.created_at.desc())
    )
    return list(result.scalars().all())


# ── Categories ──

async def list_categories(db: AsyncSession, entity_id: str) -> list[TaskCategory]:
    result = await db.execute(
        select(TaskCategory).where(TaskCategory.entity_id == entity_id).order_by(TaskCategory.sort_order)
    )
    return list(result.scalars().all())


async def create_category(
    db: AsyncSession, entity_id: str, *,
    name: str, icon: str | None = None, color: str | None = None, sort_order: int = 0,
) -> TaskCategory:
    cat = TaskCategory(
        id=generate_ulid(), entity_id=entity_id, name=name,
        icon=icon, color=color, sort_order=sort_order,
    )
    db.add(cat)
    await db.flush()
    return cat


async def update_category(
    db: AsyncSession, category_id: str, entity_id: str, **fields,
) -> Optional[TaskCategory]:
    result = await db.execute(
        select(TaskCategory).where(
            TaskCategory.id == category_id, TaskCategory.entity_id == entity_id,
        )
    )
    cat = result.scalar_one_or_none()
    if not cat:
        return None
    for k, v in fields.items():
        if hasattr(cat, k) and v is not None:
            setattr(cat, k, v)
    await db.flush()
    return cat


async def get_tasks_by_status(
    db: AsyncSession, entity_id: str,
    workspace_id: str | None = None,
    include_automations: bool = False,
    limit_per_status: int = 50,
) -> dict[str, list]:
    """Get tasks grouped by status for Kanban board.

    Automation-linked tasks (``details.scheduled_job_id`` set) are
    hidden by default — their per-run history lives on the Automations
    page via ``scheduled_job_runs``, so they don't belong on the
    user's Kanban. Pass ``include_automations=True`` to opt in.

    ``limit_per_status`` caps how many tasks load per status group.
    The total count badge still shows the real count; the UI paginates.

    Returns: ``{"pending": [...], "in_progress": [...], ...}``
    """
    q = select(Task).where(Task.entity_id == entity_id)
    if workspace_id:
        q = q.where(Task.workspace_id == workspace_id)
    if not include_automations:
        q = q.where(Task.details["scheduled_job_id"].astext.is_(None))
    q = q.order_by(Task.priority.desc(), Task.created_at.desc())
    result = await db.execute(q)
    tasks = list(result.scalars().all())

    board: dict[str, list] = {}
    status_counts: dict[str, int] = {}
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1
        bucket = board.setdefault(task.status, [])
        if len(bucket) < limit_per_status:
            bucket.append(task)
    # Attach total counts so the UI can show "20 of 150" without loading all
    board["_counts"] = status_counts  # type: ignore[assignment]
    return board


async def move_task(db: AsyncSession, task_id: str, entity_id: str, new_status: str, position: int | None = None) -> Optional[Task]:
    """Move a task to a new status (Kanban column move).
    Sets started_at/completed_at automatically based on status transitions.
    """
    task = await get_task(db, task_id, entity_id)
    if not task:
        return None

    old_status = task.status
    if new_status != old_status:
        fields: dict = {"status": new_status}
        await _enforce_dependency_gate_for_start(db, task, fields)
        if "details" in fields:
            task.details = fields["details"]
    apply_task_status_transition(task, new_status)

    await db.flush()
    if new_status in TERMINAL_STATUSES:
        try:
            from packages.core.services.workspace_operation_service import check_work_batch_completion

            await check_work_batch_completion(
                db,
                task,
                trigger_source="task_service.move_task",
            )
        except Exception:
            logger.warning(
                "task %s: failed to evaluate workspace work batch completion",
                task_id,
                exc_info=True,
            )
    await db.refresh(task)

    # Emit event
    from packages.core.services.event_emitter import emit
    emit(entity_id, "task.moved", payload={
        "task_id": task_id, "old_status": old_status, "new_status": new_status,
    })

    return task


async def delete_category(db: AsyncSession, category_id: str, entity_id: str) -> bool:
    result = await db.execute(
        select(TaskCategory).where(
            TaskCategory.id == category_id, TaskCategory.entity_id == entity_id,
        )
    )
    cat = result.scalar_one_or_none()
    if not cat:
        return False
    await db.delete(cat)
    await db.flush()
    return True


# ── SLA Policies ──────────────────────────────────────────────────────────
#
# Tasks reference an SLA policy via ``Task.sla_policy_id``. The policy
# defines response and resolution time targets; ``task_automation_service``
# reads these to compute breach state and run escalation rules. Until
# now there were no CRUD endpoints for these — admins had to insert
# rows by SQL. The functions below back the new ``/sla-policies`` API.

async def list_sla_policies(
    db: AsyncSession, entity_id: str,
) -> list[TaskSlaPolicy]:
    result = await db.execute(
        select(TaskSlaPolicy)
        .where(
            TaskSlaPolicy.entity_id == entity_id,
            TaskSlaPolicy.status == "active",
        )
        .order_by(TaskSlaPolicy.name.asc())
    )
    return list(result.scalars().all())


async def get_sla_policy(
    db: AsyncSession, policy_id: str, entity_id: str,
) -> Optional[TaskSlaPolicy]:
    result = await db.execute(
        select(TaskSlaPolicy).where(
            TaskSlaPolicy.id == policy_id,
            TaskSlaPolicy.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def create_sla_policy(
    db: AsyncSession, entity_id: str, *,
    name: str,
    response_seconds: int = 3600,
    resolution_seconds: int = 86400,
    priority: str | None = None,
    category_id: str | None = None,
) -> TaskSlaPolicy:
    policy = TaskSlaPolicy(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        response_seconds=response_seconds,
        resolution_seconds=resolution_seconds,
        priority=priority,
        category_id=category_id,
    )
    db.add(policy)
    await db.flush()
    return policy


async def update_sla_policy(
    db: AsyncSession, policy_id: str, entity_id: str, **fields,
) -> Optional[TaskSlaPolicy]:
    policy = await get_sla_policy(db, policy_id, entity_id)
    if not policy:
        return None
    _allowed = {"name", "response_seconds", "resolution_seconds",
                "priority", "category_id", "status"}
    for k, v in fields.items():
        if k in _allowed and hasattr(policy, k):
            setattr(policy, k, v)
    await db.flush()
    return policy


async def delete_sla_policy(
    db: AsyncSession, policy_id: str, entity_id: str,
) -> bool:
    """Soft-delete by flipping status to ``inactive`` so any tasks still
    pointing at this policy don't crash. Hard delete would orphan tasks."""
    policy = await get_sla_policy(db, policy_id, entity_id)
    if not policy:
        return False
    policy.status = "inactive"
    await db.flush()
    return True
