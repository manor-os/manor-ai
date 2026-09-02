"""Shared state transition for approval-type Tasks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import (
    TaskApprovalChoice,
    TaskLogType,
    TaskStatus,
    TaskType,
)
from packages.core.constants.task_actors import TaskActor
from packages.core.models.task import Task, TaskLog
from packages.core.services.task_service import add_task_log, update_task
from packages.core.services.task_state_machine import TERMINAL_STATUSES


class TaskApprovalDecisionError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class TaskApprovalDecisionResult:
    task: Task
    log: TaskLog
    decision: str
    choice: str
    approved: bool
    note: str
    actor: str


async def apply_task_approval_decision(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    actor: str,
    choice: str,
    note: Optional[str] = None,
) -> TaskApprovalDecisionResult:
    """Apply one approval decision without committing the caller's transaction."""
    locked_task = (await db.execute(
        select(Task)
        .where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_task is None:
        raise TaskApprovalDecisionError(404, "Task not found")
    task = locked_task
    if task.task_type != TaskType.APPROVAL.value:
        raise TaskApprovalDecisionError(400, "Task is not an approval task")
    if task.status in TERMINAL_STATUSES:
        raise TaskApprovalDecisionError(409, "Approval task is already closed")

    normalized = (choice or "").strip().lower()
    allowed = {member.value for member in TaskApprovalChoice}
    if normalized not in allowed:
        raise TaskApprovalDecisionError(
            400,
            "choice must be approve, reject, or request_changes",
        )
    approved = normalized in {
        TaskApprovalChoice.APPROVE.value,
        TaskApprovalChoice.APPROVED.value,
        TaskApprovalChoice.YES.value,
        TaskApprovalChoice.ACCEPT.value,
    }
    decision = "approved" if approved else "changes_requested"
    clean_note = (note or "").strip()
    decided_at = datetime.now(timezone.utc).isoformat()
    details = dict(task.details or {})
    details["approval_decision"] = {
        "decision": decision,
        "choice": normalized,
        "approved": approved,
        "note": clean_note,
        "decided_by": user_id,
        "decided_by_label": actor,
        "decided_at": decided_at,
    }
    actual_output = {
        "summary": (
            f"Approval task {decision.replace('_', ' ')}"
            + (f": {clean_note}" if clean_note else "")
        ),
        "approval": details["approval_decision"],
    }
    updated = await update_task(
        db,
        task.id,
        task.entity_id,
        user_id=user_id,
        status=TaskStatus.COMPLETED.value,
        details=details,
        actual_output=actual_output,
    )
    if updated is None:  # defensive: the caller loaded this row already
        raise TaskApprovalDecisionError(404, "Task not found")
    log = await add_task_log(
        db,
        task.id,
        TaskLogType.APPROVAL_DECISION,
        (
            f"{actor} approved this task."
            if approved
            else f"{actor} requested changes for this approval task."
        ) + (f"\n\n{clean_note}" if clean_note else ""),
        actor=TaskActor.USER,
        created_by=actor,
        metadata=details["approval_decision"],
    )
    return TaskApprovalDecisionResult(
        task=updated,
        log=log,
        decision=decision,
        choice=normalized,
        approved=approved,
        note=clean_note,
        actor=actor,
    )
