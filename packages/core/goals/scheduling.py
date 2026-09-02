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
from typing import Annotated

from pydantic import BeforeValidator, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.goals import GoalStatus
from packages.core.cron import validate_cron_expression
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
    Five-field cron expressions use the same grammar as the runtime scheduler;
    everything else is matched against the ``every_seconds`` table above.
    """
    cadence = (cadence or "").strip().lower()
    if cadence in _CADENCE_TO_SECONDS:
        return "every", {"every_seconds": _CADENCE_TO_SECONDS[cadence]}
    if cadence in _CADENCE_TO_CRON:
        return "cron", {"cron_expr": _CADENCE_TO_CRON[cadence]}
    if len(cadence.split()) == 5:
        try:
            return "cron", {"cron_expr": validate_cron_expression(cadence)}
        except ValueError:
            pass
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


def validate_measurement_cadence(cadence: object) -> str:
    """Return a normalized cadence accepted by Goal scheduling."""
    value = str(cadence or "").strip()
    if len(value) > 64:
        raise ValueError("measurement_cadence must be at most 64 characters")
    if is_manual_measurement_cadence(value):
        return value
    _cadence_to_schedule(value)
    return value


GoalMeasurementCadenceInput = Annotated[
    str,
    StringConstraints(max_length=64),
    BeforeValidator(validate_measurement_cadence),
]


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


def measurement_source_requires_external_provider(source: object) -> bool:
    """Return true only for sources backed by a credentialed integration.

    ``workspace_internal`` is derived from Manor runtime evidence and manual
    sources are user-entered; neither has credentials to connect, so they
    must never count as declared external providers in readiness checks.
    """
    return (
        is_auto_measurement_source(source)
        and not is_workspace_internal_measurement_source(source)
    )


def should_install_measurement_schedule(goal: Goal) -> bool:
    return bool(
        getattr(goal, "status", None) == GoalStatus.ACTIVE.value
        and goal.measurement_cadence
        and not is_manual_measurement_cadence(goal.measurement_cadence)
        and is_auto_measurement_source(goal.measurement_source)
    )


def measurement_schedule_skip_reason(goal: Goal) -> str:
    status = str(getattr(goal, "status", "") or "").strip().lower()
    if status and status != GoalStatus.ACTIVE.value:
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
    from packages.core.goals.locking import lock_goal_for_mutation

    locked_goal = await lock_goal_for_mutation(
        db,
        goal.id,
        entity_id=goal.entity_id,
    )
    if locked_goal is None:
        raise ValueError(f"goal {goal.id} no longer exists")
    goal = locked_goal
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
        from packages.core.services.scheduler_service import (
            ScheduledJobMutationFactory,
        )

        updates = {
            "entity_id": goal.entity_id,
            "workspace_id": goal.workspace_id,
            "name": f"Measure goal: {goal.title}",
            "job_type": (
                "interval" if schedule_kind in {"every", "interval"} else "cron"
            ),
            "schedule_kind": schedule_kind,
            "cron_expr": schedule_fields.get("cron_expr"),
            "every_seconds": schedule_fields.get("every_seconds"),
            "execution_type": "goal_measurement",
            "execution_target": {"goal_id": goal.id},
            "goal_id": goal.id,
            "enabled": True,
            "consecutive_errors": 0,
        }
        result = await ScheduledJobMutationFactory.apply(db, existing, updates)
        if result is None:
            raise ValueError(f"scheduled job {job_id} no longer exists")
        return result.job

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
    from packages.core.services.product_growth import persist_scheduled_job

    await persist_scheduled_job(db, job)
    return job


async def remove_measurement_schedule(db: AsyncSession, goal: Goal) -> None:
    """Drop the ScheduledJob row for this goal, if any. Caller commits."""
    await db.execute(
        delete(ScheduledJob).where(ScheduledJob.job_id == _job_id_for(goal))
    )
    await db.flush()
