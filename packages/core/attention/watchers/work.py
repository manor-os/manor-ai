"""Work that needs a person: blocked on something only they can give, or late.

A Task the platform stopped (a platform fault) is not here: that is reported
to the operators and recovered without asking anyone.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.attention.contracts import DesiredItem, stage_for_age
from packages.core.attention.watchers.needs_input import STAGE_HOURS as REMINDER_STAGE_HOURS
from packages.core.constants.attention import AttentionKind, AttentionUrgency
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.models.task import Task, TaskLog

# What the Task agent loop records when the agent itself reports it cannot go
# on without something (task_agent_loop._block, "blocked_by": "agent").
_AGENT_REPORTED_BLOCK = "agent_loop_reported_blocked"
_OPEN_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.WAITING_ON_CUSTOMER,
    TaskStatus.ON_HOLD,
)


def _one_line(value: object, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


class WorkBlockedWatcher:
    """A Task the agent stopped because it needs something from a person.

    Moves up a stage at the same ages as a waiting card's reminders.
    """

    key = "work_blocked"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        tasks = {
            task.id: task
            for task in (await db.execute(
                select(Task).where(
                    Task.status == TaskStatus.BLOCKED,
                    Task.workspace_id.is_not(None),
                )
            )).scalars().all()
        }
        if not tasks:
            return []
        latest: dict[str, TaskLog] = {}
        for log in (await db.execute(
            select(TaskLog)
            .where(
                TaskLog.task_id.in_(list(tasks)),
                TaskLog.log_type == TaskLogType.AI_EXECUTION_FAILED.value,
            )
            .order_by(TaskLog.created_at.desc())
        )).scalars().all():
            latest.setdefault(log.task_id, log)

        items: list[DesiredItem] = []
        for task_id, log in latest.items():
            meta = log.meta if isinstance(log.meta, dict) else {}
            if meta.get("error") != _AGENT_REPORTED_BLOCK:
                continue
            outcome = meta.get("coordinator_outcome") if isinstance(meta.get("coordinator_outcome"), dict) else {}
            details = outcome.get("details") if isinstance(outcome.get("details"), dict) else {}
            if details.get("blocked_by") not in (None, "agent"):
                continue
            submission = details.get("submission") if isinstance(details.get("submission"), dict) else {}
            reason = _one_line(submission.get("blocked_reason") or log.content)
            task = tasks[task_id]
            items.append(DesiredItem(
                entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                kind=AttentionKind.WORK_BLOCKED.value,
                subject_type="task",
                subject_id=task.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title=f"Blocked: {task.title}",
                summary=reason or None,
                opened_at=log.created_at,
                stage=stage_for_age(log.created_at, now, REMINDER_STAGE_HOURS),
                evidence={"label": task.title, "task_id": task.id, "task_log_id": log.id},
                assignee_user_id=task.assignee_id,
            ))
        return items


class WorkOverdueWatcher:
    """An open Task past its deadline."""

    key = "work_overdue"

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        from packages.core.services.task_deadlines import task_deadline_overdue_expr

        tasks = (await db.execute(
            select(Task).where(
                Task.workspace_id.is_not(None),
                Task.status.in_(_OPEN_STATUSES),
                Task.deadline.is_not(None),
                task_deadline_overdue_expr(Task.deadline, now_expr=now, current_date_expr=now.date()),
            )
        )).scalars().all()
        return [
            DesiredItem(
                entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                kind=AttentionKind.WORK_OVERDUE.value,
                subject_type="task",
                subject_id=task.id,
                urgency=AttentionUrgency.ACT_TODAY.value,
                title=f"Overdue: {task.title}",
                opened_at=task.deadline,
                evidence={
                    "label": task.title, "task_id": task.id,
                    "deadline": task.deadline.isoformat(), "status": str(task.status),
                },
                assignee_user_id=task.assignee_id,
            )
            for task in tasks
        ]
