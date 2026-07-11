"""Deterministic workspace daily summary data.

This service is intentionally data-first: automation skills can call it to get
one structured snapshot, then use the LLM only for wording and delivery.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace
from packages.core.services.task_deadlines import task_deadline_overdue_expr


VISIBLE_TASK_STATUSES = [
    "pending",
    "proposed",
    "scheduled",
    "in_progress",
    "waiting_on_customer",
    "on_hold",
    "blocked",
    "completed",
    "failed",
    "cancelled",
]
OPEN_TASK_STATUSES = {
    "pending",
    "proposed",
    "scheduled",
    "in_progress",
    "waiting_on_customer",
    "on_hold",
    "blocked",
}
TERMINAL_TASK_STATUSES = {"completed", "cancelled", "failed"}


class WorkspaceDailySummaryError(ValueError):
    pass


async def get_workspace_daily_summary(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    date: str | date_type | None = None,
    timezone_name: str = "UTC",
    limit_per_section: int = 8,
) -> dict[str, Any]:
    """Build a reusable workspace daily-summary snapshot.

    ``date`` is the local day being summarized. When omitted, the service uses
    the previous local day in ``timezone_name``. ``today_focus`` is the local day
    immediately after the summary window.
    """
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        raise WorkspaceDailySummaryError("workspace not found")

    tz = _load_timezone(timezone_name)
    summary_date = _coerce_summary_date(date, tz)
    window_start_local = datetime.combine(summary_date, datetime.min.time(), tzinfo=tz)
    window_end_local = window_start_local + timedelta(days=1)
    today_start_local = window_end_local
    today_end_local = today_start_local + timedelta(days=1)

    window_start = window_start_local.astimezone(timezone.utc)
    window_end = window_end_local.astimezone(timezone.utc)
    today_start = today_start_local.astimezone(timezone.utc)
    today_end = today_end_local.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    limit = max(1, min(int(limit_per_section or 8), 25))

    status_counts = await _task_status_counts(db, entity_id, workspace_id)
    open_count = sum(status_counts.get(status, 0) for status in OPEN_TASK_STATUSES)
    overdue_conditions = (
        Task.deadline.isnot(None),
        task_deadline_overdue_expr(
            Task.deadline,
            now_expr=now,
            current_date_expr=summary_date,
        ),
        Task.status.notin_(list(TERMINAL_TASK_STATUSES)),
    )
    overdue_count = await _task_count(db, entity_id, workspace_id, *overdue_conditions)
    overdue_tasks = await _tasks(
        db, entity_id, workspace_id,
        *overdue_conditions,
        limit=limit,
        order_by=Task.deadline.asc(),
    )
    stale_cutoff = now - timedelta(hours=48)
    stalled_conditions = (
        Task.status == "in_progress",
        func.coalesce(Task.updated_at, Task.created_at) < stale_cutoff,
    )
    stalled_count = await _task_count(db, entity_id, workspace_id, *stalled_conditions)
    stalled_tasks = await _tasks(
        db, entity_id, workspace_id,
        *stalled_conditions,
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).asc(),
    )

    completed_conditions = (
        Task.completed_at.isnot(None),
        Task.completed_at >= window_start,
        Task.completed_at < window_end,
    )
    completed_count = await _task_count(db, entity_id, workspace_id, *completed_conditions)
    completed_tasks = await _tasks(
        db, entity_id, workspace_id,
        *completed_conditions,
        limit=limit,
        order_by=Task.completed_at.desc(),
    )
    created_conditions = (
        Task.created_at >= window_start,
        Task.created_at < window_end,
    )
    created_count = await _task_count(db, entity_id, workspace_id, *created_conditions)
    created_tasks = await _tasks(
        db, entity_id, workspace_id,
        *created_conditions,
        limit=limit,
        order_by=Task.created_at.desc(),
    )
    failed_conditions = (
        Task.status == "failed",
        func.coalesce(Task.updated_at, Task.created_at) >= window_start,
        func.coalesce(Task.updated_at, Task.created_at) < window_end,
    )
    failed_count = await _task_count(db, entity_id, workspace_id, *failed_conditions)
    failed_tasks = await _tasks(
        db, entity_id, workspace_id,
        *failed_conditions,
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).desc(),
    )

    proposed_tasks = await _tasks(
        db, entity_id, workspace_id,
        Task.status == "proposed",
        limit=limit,
        order_by=Task.priority.desc(),
    )
    waiting_tasks = await _tasks(
        db, entity_id, workspace_id,
        Task.status == "waiting_on_customer",
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).asc(),
    )
    blocked_tasks = await _tasks(
        db, entity_id, workspace_id,
        Task.status == "blocked",
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).asc(),
    )
    failed_open_tasks = await _tasks(
        db, entity_id, workspace_id,
        Task.status == "failed",
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).desc(),
    )

    due_today_conditions = (
        Task.deadline.isnot(None),
        Task.deadline >= today_start,
        Task.deadline < today_end,
        Task.status.notin_(["completed", "cancelled"]),
    )
    due_today_count = await _task_count(db, entity_id, workspace_id, *due_today_conditions)
    due_today_tasks = await _tasks(
        db, entity_id, workspace_id,
        *due_today_conditions,
        limit=limit,
        order_by=Task.deadline.asc(),
    )
    in_progress_tasks = await _tasks(
        db, entity_id, workspace_id,
        Task.status == "in_progress",
        limit=limit,
        order_by=func.coalesce(Task.updated_at, Task.created_at).desc(),
    )
    priority_conditions = (
        Task.status.in_(["pending", "proposed", "scheduled"]),
        Task.priority >= 4,
    )
    priority_count = await _task_count(db, entity_id, workspace_id, *priority_conditions)
    priority_tasks = await _tasks(
        db, entity_id, workspace_id,
        *priority_conditions,
        limit=limit,
        order_by=Task.priority.desc(),
    )

    automation_health = await _automation_health(db, entity_id, workspace_id)
    execution_health = await _execution_health(db, entity_id, workspace_id)
    action_items = _action_items(
        status_counts=status_counts,
        overdue_count=overdue_count,
        stalled_count=stalled_count,
        automation_health=automation_health,
        execution_health=execution_health,
    )

    return {
        "kind": "workspace_daily_summary",
        "version": "v1",
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "status": workspace.status,
            "category": workspace.category,
        },
        "window": {
            "date": summary_date.isoformat(),
            "timezone": timezone_name,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "today_start": today_start.isoformat(),
            "today_end": today_end.isoformat(),
        },
        "data_quality": {
            "source": "database",
            "simulated": False,
            "llm_generated": False,
            "notes": [
                "Counts and task lists are queried from Manor database rows.",
                "LLMs may summarize this payload, but should not invent unsupported facts.",
            ],
        },
        "yesterday_outcomes": {
            "completed_count": completed_count,
            "created_count": created_count,
            "failed_count": failed_count,
            "completed_tasks": [_task_summary(t) for t in completed_tasks],
            "created_tasks": [_task_summary(t) for t in created_tasks],
            "failed_tasks": [_task_summary(t) for t in failed_tasks],
        },
        "current_health": {
            "open_count": open_count,
            "by_status": status_counts,
            "overdue_count": overdue_count,
            "stalled_count": stalled_count,
            "overdue_tasks": [_task_summary(t) for t in overdue_tasks],
            "stalled_tasks": [_task_summary(t) for t in stalled_tasks],
            "automations": automation_health,
            "executions": execution_health,
        },
        "needs_human_handling": {
            "proposed_count": status_counts.get("proposed", 0),
            "waiting_on_customer_count": status_counts.get("waiting_on_customer", 0),
            "blocked_count": status_counts.get("blocked", 0),
            "failed_count": status_counts.get("failed", 0),
            "proposed_tasks": [_task_summary(t) for t in proposed_tasks],
            "waiting_tasks": [_task_summary(t) for t in waiting_tasks],
            "blocked_tasks": [_task_summary(t) for t in blocked_tasks],
            "failed_tasks": [_task_summary(t) for t in failed_open_tasks],
            "waiting_human_plans": execution_health["waiting_human_plans"],
        },
        "today_focus": {
            "due_today_count": due_today_count,
            "in_progress_count": status_counts.get("in_progress", 0),
            "priority_pending_count": priority_count,
            "due_today_tasks": [_task_summary(t) for t in due_today_tasks],
            "in_progress_tasks": [_task_summary(t) for t in in_progress_tasks],
            "priority_tasks": [_task_summary(t) for t in priority_tasks],
        },
        "recommended_action_items": action_items,
    }


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _coerce_summary_date(value: str | date_type | None, tz: ZoneInfo) -> date_type:
    if value is None:
        return datetime.now(tz).date() - timedelta(days=1)
    if isinstance(value, date_type):
        return value
    return date_type.fromisoformat(str(value)[:10])


async def _task_status_counts(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> dict[str, int]:
    rows = (await db.execute(
        select(Task.status, func.count())
        .where(
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
            _visible_task_condition(),
        )
        .group_by(Task.status)
    )).all()
    counts = {status: 0 for status in VISIBLE_TASK_STATUSES}
    for status, count in rows:
        counts[str(status)] = int(count or 0)
    return counts


async def _task_count(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *conditions: Any,
) -> int:
    count = (await db.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
            _visible_task_condition(),
            *conditions,
        )
    )).scalar_one()
    return int(count or 0)


async def _tasks(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *conditions: Any,
    limit: int,
    order_by: Any,
) -> list[Task]:
    q = (
        select(Task)
        .where(
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
            _visible_task_condition(),
            *conditions,
        )
        .order_by(order_by)
        .limit(limit)
    )
    return list((await db.execute(q)).scalars().all())


def _visible_task_condition() -> Any:
    return Task.details["scheduled_job_id"].astext.is_(None)


async def _automation_health(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    rows = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
            ScheduledJob.enabled.is_(True),
        )
    )).scalars().all())
    errored = [job for job in rows if job.last_status == "error"]
    broken = [job for job in rows if (job.consecutive_errors or 0) >= 3]
    return {
        "enabled_count": len(rows),
        "errored_count": len(errored),
        "broken_count": len(broken),
        "errored_jobs": [_job_summary(job) for job in errored[:5]],
        "broken_jobs": [_job_summary(job) for job in broken[:5]],
    }


async def _execution_health(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    active_statuses = ["running", "pending_approval", "paused", "draft", "needs_attention"]
    active_count = (await db.execute(
        select(func.count())
        .select_from(ExecutionPlan)
        .where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.workspace_id == workspace_id,
            ExecutionPlan.status.in_(active_statuses),
        )
    )).scalar_one()
    waiting_plan_count = (await db.execute(
        select(func.count(func.distinct(ExecutionStep.plan_id))).where(
            ExecutionStep.entity_id == entity_id,
            ExecutionStep.workspace_id == workspace_id,
            ExecutionStep.step_status == "waiting_human",
        )
    )).scalar_one()
    return {
        "active_plans": int(active_count or 0),
        "waiting_human_plans": int(waiting_plan_count or 0),
    }


def _task_summary(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "description": (task.description or "")[:240],
    }


def _job_summary(job: ScheduledJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_id": job.job_id,
        "name": job.name,
        "execution_type": job.execution_type,
        "last_status": job.last_status,
        "consecutive_errors": job.consecutive_errors or 0,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
    }


def _action_items(
    *,
    status_counts: dict[str, int],
    overdue_count: int,
    stalled_count: int,
    automation_health: dict[str, Any],
    execution_health: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    if status_counts.get("waiting_on_customer", 0):
        items.append(f"Respond to {status_counts['waiting_on_customer']} task(s) waiting on input.")
    if status_counts.get("proposed", 0):
        items.append(f"Review {status_counts['proposed']} proposed task(s).")
    if overdue_count:
        items.append(f"Resolve or re-date {overdue_count} overdue task(s).")
    if status_counts.get("blocked", 0):
        items.append(f"Unblock {status_counts['blocked']} blocked task(s).")
    if stalled_count:
        items.append(f"Check {stalled_count} in-progress task(s) with no update in 48 hours.")
    if automation_health.get("broken_count", 0):
        items.append(f"Investigate {automation_health['broken_count']} broken scheduled job(s).")
    if execution_health.get("waiting_human_plans", 0):
        items.append(f"Provide input for {execution_health['waiting_human_plans']} plan(s) waiting on a human step.")
    if not items:
        items.append("No urgent human action detected from current workspace data.")
    return items
