"""Wire Goals into the existing ScheduledJob system.

Only goals with an automatic ``measurement_source`` and a
``measurement_cadence`` get a ScheduledJob row tagged with
execution_type='goal_measurement'. Manual goals are measured by explicit
user/tool updates, not background polling.

We use a stable derived job_id (``gm:<goal_id>``) so re-installation is
idempotent and removal is by-id rather than by-content matching.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob

logger = logging.getLogger(__name__)


WORKSPACE_INTERNAL_MEASUREMENT_PROVIDER = "workspace_internal"
MANUAL_MEASUREMENT_PROVIDERS = {"manual", "manual_demo"}
MANUAL_MEASUREMENT_CADENCES = {"manual", "manual_demo"}
INTERNAL_MEASUREMENT_PROVIDERS = {WORKSPACE_INTERNAL_MEASUREMENT_PROVIDER}

_CADENCE_TO_SECONDS = {
    "minute": 60.0,
    "hourly": 3600.0,
    "daily": 86400.0,
    "weekly": 604800.0,
}

_CADENCE_TO_CRON = {
    # Monthly goals are common for budget/cost controls. A calendar cron
    # avoids pretending every month is exactly 30 days.
    "monthly": "0 9 1 * *",
    "quarterly": "0 9 1 */3 *",
    "yearly": "0 9 1 1 *",
}


def _cadence_to_schedule(cadence: str) -> tuple[str, dict]:
    """Translate a cadence string into ScheduledJob fields.

    Returns ``(schedule_kind, fields)`` where fields are columns to set.
    Cron expressions pass through verbatim (5 or 6 whitespace-delimited
    tokens); everything else is matched against the ``every_seconds``
    table above.
    """
    cadence = (cadence or "").strip().lower()
    if cadence in _CADENCE_TO_SECONDS:
        return "every", {"every_seconds": _CADENCE_TO_SECONDS[cadence]}
    if cadence in _CADENCE_TO_CRON:
        return "cron", {"cron_expr": _CADENCE_TO_CRON[cadence]}
    if " " in cadence:
        return "cron", {"cron_expr": cadence}
    raise ValueError(
        f"unsupported measurement_cadence={cadence!r} — expected one of "
        f"{sorted([*_CADENCE_TO_SECONDS, *_CADENCE_TO_CRON])} or a cron expression"
    )


def _job_id_for(goal: Goal) -> str:
    return f"gm:{goal.id}"


def _provider_key(source: object) -> str:
    if not isinstance(source, dict):
        return ""
    return str(source.get("provider") or "").strip().lower()


def is_manual_measurement_source(source: object) -> bool:
    provider = _provider_key(source)
    return bool(provider and (provider in MANUAL_MEASUREMENT_PROVIDERS or provider.startswith("manual_")))


def is_manual_measurement_cadence(cadence: object) -> bool:
    value = str(cadence or "").strip().lower()
    return bool(value and (value in MANUAL_MEASUREMENT_CADENCES or value.startswith("manual_")))


def is_workspace_internal_measurement_source(source: object) -> bool:
    return _provider_key(source) in INTERNAL_MEASUREMENT_PROVIDERS


def preserves_manual_workspace_measurement_source(source: object) -> bool:
    if not isinstance(source, dict):
        return False
    params = source.get("params") if isinstance(source.get("params"), dict) else {}
    return bool(
        source.get("preserve_workspace_manual")
        or source.get("manual_entry")
        or params.get("preserve_workspace_manual")
        or params.get("manual_entry")
        or str(params.get("mode") or "").strip().lower()
        in {"manual_entry", "external_dashboard", "analytics_dashboard"}
    )


def default_workspace_measurement_source(
    source: object,
    *,
    workspace_id: str | None,
) -> dict | None:
    """Default workspace goals to internal evidence instead of manual input.

    A workspace already has task links, execution outputs, artifacts, and
    Strategist-estimated impact. That evidence is enough to measure many
    deliverable goals without asking the user to type numbers.
    """
    if not workspace_id:
        return source if isinstance(source, dict) and source else None
    if not isinstance(source, dict) or not source:
        return {
            "provider": WORKSPACE_INTERNAL_MEASUREMENT_PROVIDER,
            "params": {"mode": "linked_task_impact"},
        }
    if is_manual_measurement_source(source):
        if preserves_manual_workspace_measurement_source(source):
            return source
        return {
            "provider": WORKSPACE_INTERNAL_MEASUREMENT_PROVIDER,
            "params": {"mode": "linked_task_impact"},
        }
    return source


def is_auto_measurement_source(source: object) -> bool:
    """Return true only for sources the scheduler can measure by itself."""
    provider = _provider_key(source)
    if not provider or is_manual_measurement_source(source):
        return False
    return True


def should_install_measurement_schedule(goal: Goal) -> bool:
    return bool(
        getattr(goal, "status", None) == "active"
        and goal.measurement_cadence
        and not is_manual_measurement_cadence(goal.measurement_cadence)
        and is_auto_measurement_source(goal.measurement_source)
    )


def measurement_schedule_skip_reason(goal: Goal) -> str:
    status = str(getattr(goal, "status", "") or "").strip().lower()
    if status and status != "active":
        return f"goal_{status}"
    if not getattr(goal, "measurement_cadence", None):
        return "measurement_cadence_missing"
    if is_manual_measurement_cadence(getattr(goal, "measurement_cadence", None)):
        return "manual_measurement_required"
    if not is_auto_measurement_source(getattr(goal, "measurement_source", None)):
        return "manual_measurement_required"
    return "measurement_schedule_not_applicable"


async def install_measurement_schedule(db: AsyncSession, goal: Goal) -> ScheduledJob:
    """Insert or refresh the ScheduledJob for this goal's measurement.

    Idempotent — derived job_id means re-running just updates the
    schedule. Caller commits.
    """
    if not should_install_measurement_schedule(goal):
        raise ValueError(
            f"goal {goal.id} does not have an automatic measurement source"
        )

    schedule_kind, schedule_fields = _cadence_to_schedule(goal.measurement_cadence)
    job_id = _job_id_for(goal)

    existing = (await db.execute(
        select(ScheduledJob).where(ScheduledJob.job_id == job_id)
    )).scalar_one_or_none()

    if existing:
        existing.entity_id = goal.entity_id
        existing.workspace_id = goal.workspace_id
        existing.schedule_kind = schedule_kind
        existing.cron_expr = schedule_fields.get("cron_expr")
        existing.every_seconds = schedule_fields.get("every_seconds")
        existing.execution_type = "goal_measurement"
        existing.execution_target = {"goal_id": goal.id}
        existing.enabled = True
        existing.consecutive_errors = 0
        await db.flush()
        return existing

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=goal.entity_id,
        workspace_id=goal.workspace_id,
        name=f"Measure goal: {goal.title}",
        job_type="interval" if schedule_kind in {"every", "interval"} else "cron",
        schedule_kind=schedule_kind,
        cron_expr=schedule_fields.get("cron_expr"),
        every_seconds=schedule_fields.get("every_seconds"),
        execution_type="goal_measurement",
        execution_target={"goal_id": goal.id},
        goal_id=goal.id,
        enabled=True,
    )
    db.add(job)
    await db.flush()
    return job


async def remove_measurement_schedule(db: AsyncSession, goal: Goal) -> None:
    """Drop the ScheduledJob row for this goal, if any. Caller commits."""
    await db.execute(
        delete(ScheduledJob).where(ScheduledJob.job_id == _job_id_for(goal))
    )
    await db.flush()
