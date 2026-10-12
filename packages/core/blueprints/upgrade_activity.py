"""Bounded, read-only execution preflight for in-place Blueprint changes."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ExecutionPlanStatus,
    PLAN_TERMINAL_STATUSES,
    ScheduledRunStatus,
)
from packages.core.constants.review import ReviewRunStatus
from packages.core.constants.task import TaskStatus
from packages.core.constants.workflow import WORKFLOW_RUN_OPEN_STATUSES
from packages.core.models.execution import ExecutionPlan
from packages.core.models.review_run import ReviewRun
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.scheduler import AgentExecution, ScheduledJob, ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace


async def active_runs(db: AsyncSession, *, workspace: Workspace) -> list[dict]:
    """List open execution identities without cancelling or modifying them.

    RuntimeRun is part of the shared core schema and OSS export. Deployment
    settings determine whether Chat uses it; an empty table adds no blocker.
    The caller holds the Workspace lifecycle boundary during apply and undo.
    """
    result = []
    for model, kind, statuses in (
        (WorkflowRun, "workflow", WORKFLOW_RUN_OPEN_STATUSES),
        (ExecutionPlan, "plan", tuple(set(ExecutionPlanStatus) - PLAN_TERMINAL_STATUSES)),
        (AgentExecution, "agent", ("pending", "running", "paused")),
        (Task, "task", (TaskStatus.IN_PROGRESS,)),
        (ReviewRun, "review", (ReviewRunStatus.RUNNING,)),
        (RuntimeRun, "chat", RuntimeRunStatus.active_root()),
    ):
        rows = (await db.execute(select(model).where(
            model.entity_id == workspace.entity_id,
            model.workspace_id == workspace.id,
            model.status.in_(statuses),
        ).order_by(model.id).limit(101))).scalars().all()
        result.extend({
            "id": row.id, "kind": kind, "status": row.status,
            "name": getattr(row, "title", None) or getattr(row, "name", None) or row.id,
        } for row in rows)
    rows = (await db.execute(select(ScheduledJobRun, ScheduledJob.name).join(
        ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id,
    ).where(
        ScheduledJob.workspace_id == workspace.id,
        ScheduledJob.entity_id == workspace.entity_id,
        ScheduledJobRun.status == ScheduledRunStatus.RUNNING,
    ).order_by(ScheduledJobRun.id).limit(101))).all()
    result.extend({"id": row.id, "kind": "scheduled_job", "status": row.status, "name": name or row.id}
                  for row, name in rows)
    return result
