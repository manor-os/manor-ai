"""ScheduledJob row management for Strategist cadence.

Mirrors the pattern goals.scheduling uses for measurement: derive a
stable job_id (``sr:<workspace_id>``) so install/remove are idempotent.
The scheduler tick routes ``execution_type='strategist_review'`` to
the ``run_strategist_review`` Celery task.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)


_CADENCE_TO_SECONDS = {
    "hourly": 3600.0,
    "daily": 86400.0,
    "weekly": 604800.0,
    "biweekly": 1_209_600.0,
}


def _job_id_for(workspace_id: str) -> str:
    return f"sr:{workspace_id}"


def _schedule_from_cadence(cadence: str) -> tuple[str, dict]:
    cadence = (cadence or "").strip().lower()
    if cadence in _CADENCE_TO_SECONDS:
        return "every", {"every_seconds": _CADENCE_TO_SECONDS[cadence]}
    if " " in cadence:
        return "cron", {"cron_expr": cadence}
    raise ValueError(
        f"unsupported strategist cadence={cadence!r} — expected one of "
        f"{sorted(_CADENCE_TO_SECONDS)} or a cron expression"
    )


async def install_strategist_schedule(
    db: AsyncSession,
    workspace: Workspace,
    *,
    cadence: str,
) -> ScheduledJob:
    """Install / refresh the workspace's Strategist review cron. Caller
    commits."""
    schedule_kind, fields = _schedule_from_cadence(cadence)
    job_id = _job_id_for(workspace.id)

    existing = (await db.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == job_id)
    )).scalar_one_or_none()

    if existing:
        existing.entity_id = workspace.entity_id
        existing.workspace_id = workspace.id
        existing.schedule_kind = schedule_kind
        existing.cron_expr = fields.get("cron_expr")
        existing.every_seconds = fields.get("every_seconds")
        existing.execution_type = "strategist_review"
        existing.execution_target = {"workspace_id": workspace.id}
        existing.enabled = True
        existing.consecutive_errors = 0
        await db.flush()
        return existing

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name=f"Strategist review: {workspace.name}",
        job_type="interval" if schedule_kind in {"every", "interval"} else "cron",
        schedule_kind=schedule_kind,
        cron_expr=fields.get("cron_expr"),
        every_seconds=fields.get("every_seconds"),
        execution_type="strategist_review",
        execution_target={"workspace_id": workspace.id},
        enabled=True,
    )
    db.add(job)
    await db.flush()
    return job


async def remove_strategist_schedule(
    db: AsyncSession, workspace_id: str,
) -> None:
    await db.execute(
        delete(ScheduledJob).where(ScheduledJob.job_id == _job_id_for(workspace_id))
    )
    await db.flush()
