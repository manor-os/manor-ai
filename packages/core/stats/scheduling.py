"""Install and remove deterministic Workspace Stat collection schedules."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace_stat import WorkspaceStat


_CADENCE_TO_SECONDS = {
    "minute": 60.0,
    "hourly": 3600.0,
    "daily": 86400.0,
    "weekly": 604800.0,
}
_CADENCE_TO_CRON = {
    "monthly": "0 9 1 * *",
    "quarterly": "0 9 1 */3 *",
    "yearly": "0 9 1 1 *",
}


def _schedule_fields(cadence: str) -> tuple[str, dict]:
    value = str(cadence or "").strip().lower()
    if value in _CADENCE_TO_SECONDS:
        return "every", {"every_seconds": _CADENCE_TO_SECONDS[value]}
    if value in _CADENCE_TO_CRON:
        return "cron", {"cron_expr": _CADENCE_TO_CRON[value]}
    if len(value.split()) in {5, 6}:
        return "cron", {"cron_expr": value}
    raise ValueError(f"unsupported stat collection cadence: {cadence}")


def _job_id(stat: WorkspaceStat) -> str:
    return f"wsstat:{stat.id}"


def should_schedule(stat: WorkspaceStat) -> bool:
    return bool(
        stat.status == "active"
        and stat.collector_type != "manual"
        and stat.collection_cadence
    )


async def sync_stat_collection_schedule(db: AsyncSession, stat: WorkspaceStat) -> None:
    existing = (await db.execute(select(ScheduledJob).where(
        ScheduledJob.job_id == _job_id(stat)
    ))).scalar_one_or_none()
    if not should_schedule(stat):
        if existing is not None:
            await db.delete(existing)
            await db.flush()
        return

    schedule_kind, fields = _schedule_fields(stat.collection_cadence or "")
    if existing is None:
        existing = ScheduledJob(
            id=generate_ulid(),
            job_id=_job_id(stat),
            entity_id=stat.entity_id,
            workspace_id=stat.workspace_id,
            name=f"Collect stat: {stat.name}",
            job_type="interval" if schedule_kind == "every" else "cron",
            execution_type="workspace_stat_collection",
            execution_target={"stat_id": stat.id},
            enabled=True,
        )
        db.add(existing)
    existing.name = f"Collect stat: {stat.name}"
    existing.schedule_kind = schedule_kind
    existing.every_seconds = fields.get("every_seconds")
    existing.cron_expr = fields.get("cron_expr")
    existing.execution_target = {"stat_id": stat.id}
    existing.enabled = True
    await db.flush()


async def remove_stat_collection_schedule(db: AsyncSession, stat: WorkspaceStat) -> None:
    await db.execute(delete(ScheduledJob).where(ScheduledJob.job_id == _job_id(stat)))
    await db.flush()
