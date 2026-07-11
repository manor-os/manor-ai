"""Schedule installers for the workspace evolution loop.

Two ScheduledJob rows are installed per workspace:

  * ``oe:<workspace_id>`` — outcome evaluation, daily at 02:15 UTC
  * ``cie:<workspace_id>`` — chat insight extraction, every 6 hours

Both are idempotent — re-running ``install_evolution_schedules`` updates
the existing rows in place. Use ``remove_evolution_schedules`` when a
workspace is deleted or paused.

Designed to be called from ``workspace_setup_service.finalize_setup``
right after ``install_strategist_schedule`` so a fresh workspace gets
the full evolution loop wired up automatically.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)


def _outcome_job_id(workspace_id: str) -> str:
    return f"oe:{workspace_id}"


def _extract_job_id(workspace_id: str) -> str:
    return f"cie:{workspace_id}"


async def install_evolution_schedules(
    db: AsyncSession, workspace: Workspace,
) -> tuple[ScheduledJob, ScheduledJob]:
    """Install (or refresh) the outcome-evaluation + chat-extraction
    schedules. Caller commits."""
    outcome_job = await _upsert(
        db,
        workspace=workspace,
        job_id=_outcome_job_id(workspace.id),
        name=f"Outcome eval: {workspace.name}",
        execution_type="outcome_evaluation",
        # Daily at 02:15 UTC — quiet hour for most timezones; outside the
        # Strategist's typical morning cadence so the calibration block
        # picks up fresh labels before the next review.
        schedule_kind="cron",
        cron_expr="15 2 * * *",
        every_seconds=None,
    )
    extract_job = await _upsert(
        db,
        workspace=workspace,
        job_id=_extract_job_id(workspace.id),
        name=f"Chat insight extraction: {workspace.name}",
        execution_type="chat_insight_extraction",
        # Every 6h — tight enough that operator preferences land in
        # memory the same day they're typed, loose enough that the
        # extractor LLM cost stays trivial.
        schedule_kind="every",
        cron_expr=None,
        every_seconds=6 * 3600.0,
    )
    return outcome_job, extract_job


async def remove_evolution_schedules(
    db: AsyncSession, workspace_id: str,
) -> None:
    await db.execute(
        delete(ScheduledJob).where(ScheduledJob.job_id.in_([
            _outcome_job_id(workspace_id),
            _extract_job_id(workspace_id),
        ]))
    )
    await db.flush()


# ── Internal ────────────────────────────────────────────────────────

async def _upsert(
    db: AsyncSession,
    *,
    workspace: Workspace,
    job_id: str,
    name: str,
    execution_type: str,
    schedule_kind: str,
    cron_expr: str | None,
    every_seconds: float | None,
) -> ScheduledJob:
    existing = (await db.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == job_id)
    )).scalar_one_or_none()

    if existing:
        existing.entity_id = workspace.entity_id
        existing.workspace_id = workspace.id
        existing.name = name
        existing.schedule_kind = schedule_kind
        existing.cron_expr = cron_expr
        existing.every_seconds = every_seconds
        existing.execution_type = execution_type
        existing.execution_target = {"workspace_id": workspace.id}
        existing.enabled = True
        existing.consecutive_errors = 0
        existing.job_type = "cron" if schedule_kind == "cron" else "interval"
        await db.flush()
        return existing

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name=name,
        job_type="cron" if schedule_kind == "cron" else "interval",
        schedule_kind=schedule_kind,
        cron_expr=cron_expr,
        every_seconds=every_seconds,
        execution_type=execution_type,
        execution_target={"workspace_id": workspace.id},
        enabled=True,
    )
    db.add(job)
    await db.flush()
    return job
