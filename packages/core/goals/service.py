"""Goal CRUD service.

Single source of truth for Goal mutations. The HTTP router, the
Strategist, and the agent's create_goal tool all funnel through here
so things like "schedule a measurement when measurement_cadence is
set" or "emit goal.created event" only happen in one place.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.goals.pace import compute_pace
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink

logger = logging.getLogger(__name__)


# ── CRUD ──────────────────────────────────────────────────────────────

async def create_goal(
    db: AsyncSession,
    *,
    entity_id: str,
    title: str,
    metric_key: str,
    target_value: Decimal | float | int,
    workspace_id: Optional[str] = None,
    description: Optional[str] = None,
    baseline_value: Optional[Decimal | float | int] = None,
    deadline: Optional[date] = None,
    measurement_source: Optional[dict] = None,
    measurement_cadence: Optional[str] = None,
    priority: int = 3,
    install_schedule: bool = True,
) -> Goal:
    """Create a Goal. If ``measurement_cadence`` and
    ``measurement_source`` are both set and ``install_schedule`` is
    true, a ScheduledJob is installed on the same DB transaction so
    the measurement service starts polling on the next scheduler tick.
    """
    from packages.core.goals.scheduling import (
        default_workspace_measurement_source,
        is_workspace_internal_measurement_source,
    )

    measurement_source = default_workspace_measurement_source(
        measurement_source,
        workspace_id=workspace_id,
    )
    if measurement_source and is_workspace_internal_measurement_source(measurement_source):
        measurement_cadence = measurement_cadence or "daily"
        if baseline_value is None:
            baseline_value = 0

    goal = Goal(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title=title,
        description=description,
        metric_key=metric_key,
        target_value=Decimal(str(target_value)),
        baseline_value=(
            Decimal(str(baseline_value)) if baseline_value is not None else None
        ),
        deadline=deadline,
        measurement_source=measurement_source,
        measurement_cadence=measurement_cadence,
        priority=priority,
        pace_status="unknown",
        status="active",
    )
    db.add(goal)
    await db.flush()

    if install_schedule:
        # Local import to avoid circular dependency (scheduling needs
        # the Goal id and the entity it belongs to).
        from packages.core.goals.scheduling import (
            install_measurement_schedule,
            should_install_measurement_schedule,
        )
        if should_install_measurement_schedule(goal):
            await install_measurement_schedule(db, goal)

    return goal


async def get_goal(db: AsyncSession, goal_id: str, entity_id: str) -> Optional[Goal]:
    return (await db.execute(
        select(Goal).where(Goal.id == goal_id, Goal.entity_id == entity_id)
    )).scalar_one_or_none()


async def list_goals(
    db: AsyncSession, entity_id: str,
    *,
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[Goal]:
    stmt = select(Goal).where(Goal.entity_id == entity_id)
    if workspace_id:
        stmt = stmt.where(Goal.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Goal.status == status)
    stmt = stmt.order_by(Goal.priority.desc(), Goal.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def update_goal(
    db: AsyncSession, goal_id: str, entity_id: str, **fields,
) -> Optional[Goal]:
    """Update arbitrary Goal fields. ``measurement_source`` /
    ``measurement_cadence`` changes trigger a ScheduledJob refresh."""
    goal = await get_goal(db, goal_id, entity_id)
    if not goal:
        return None

    previous_measurement_source = goal.measurement_source
    previous_measurement_cadence = goal.measurement_cadence
    schedule_relevant_changed = (
        ("measurement_source" in fields and fields["measurement_source"] != goal.measurement_source)
        or ("measurement_cadence" in fields and fields["measurement_cadence"] != goal.measurement_cadence)
        or ("status" in fields and fields["status"] != goal.status)
    )

    for k, v in fields.items():
        if v is None and k not in {
            # explicit-clear-allowed fields
            "deadline", "description", "measurement_source",
            "measurement_cadence", "baseline_value",
        }:
            continue
        if k in {"target_value", "baseline_value", "current_value"} and v is not None:
            v = Decimal(str(v))
        if hasattr(goal, k):
            setattr(goal, k, v)
    await db.flush()

    from packages.core.goals.scheduling import (
        default_workspace_measurement_source,
        is_workspace_internal_measurement_source,
    )
    goal.measurement_source = default_workspace_measurement_source(
        goal.measurement_source,
        workspace_id=goal.workspace_id,
    )
    if goal.measurement_source and is_workspace_internal_measurement_source(goal.measurement_source):
        goal.measurement_cadence = goal.measurement_cadence or "daily"
        if goal.baseline_value is None:
            goal.baseline_value = Decimal("0")
    await db.flush()
    normalized_schedule_changed = (
        goal.measurement_source != previous_measurement_source
        or goal.measurement_cadence != previous_measurement_cadence
    )

    if schedule_relevant_changed or normalized_schedule_changed:
        from packages.core.goals.scheduling import (
            install_measurement_schedule,
            remove_measurement_schedule,
            should_install_measurement_schedule,
        )
        await remove_measurement_schedule(db, goal)
        if should_install_measurement_schedule(goal):
            await install_measurement_schedule(db, goal)

    return goal


async def delete_goal(db: AsyncSession, goal_id: str, entity_id: str) -> bool:
    goal = await get_goal(db, goal_id, entity_id)
    if not goal:
        return False
    from packages.core.goals.scheduling import remove_measurement_schedule
    await remove_measurement_schedule(db, goal)
    await db.delete(goal)
    await db.flush()
    return True


# ── Measurements ──────────────────────────────────────────────────────

async def record_measurement(
    db: AsyncSession,
    goal: Goal,
    *,
    value: Decimal | float | int,
    source: str = "manual",
    meta: Optional[dict] = None,
    measured_at: Optional[datetime] = None,
    recompute_pace_now: bool = True,
) -> GoalMeasurement:
    """Append a measurement, update goal.current_value, recompute pace.

    Caller owns the transaction — this only flushes, never commits.
    Returns the inserted GoalMeasurement row.
    """
    measured_at = measured_at or datetime.now(timezone.utc)
    value_dec = Decimal(str(value))

    # Baseline lock: first ever measurement sets baseline.
    if goal.baseline_value is None:
        goal.baseline_value = value_dec

    measurement = GoalMeasurement(
        goal_id=goal.id,
        measured_at=measured_at,
        value=value_dec,
        source=source,
        meta=meta,
    )
    db.add(measurement)

    goal.current_value = value_dec
    goal.current_value_updated_at = measured_at

    if recompute_pace_now:
        goal.pace_status = compute_pace(
            current_value=goal.current_value,
            baseline_value=goal.baseline_value,
            target_value=goal.target_value,
            created_at=goal.created_at,
            deadline=goal.deadline,
            today=measured_at.date(),
        )
        goal.pace_computed_at = measured_at

        # Achievement transition — recorded once.
        if goal.pace_status == "achieved" and goal.status == "active":
            goal.status = "achieved"
            goal.achieved_at = measured_at
            from packages.core.goals.scheduling import remove_measurement_schedule
            await remove_measurement_schedule(db, goal)

    await db.flush()
    return measurement


async def list_measurements(
    db: AsyncSession, goal_id: str, *, limit: int = 100,
) -> list[GoalMeasurement]:
    return list((await db.execute(
        select(GoalMeasurement)
        .where(GoalMeasurement.goal_id == goal_id)
        .order_by(desc(GoalMeasurement.measured_at))
        .limit(limit)
    )).scalars().all())


# ── Goal ↔ Task linkage ───────────────────────────────────────────────

async def link_task_to_goal(
    db: AsyncSession,
    *,
    goal_id: str,
    task_id: str,
    contribution: str = "direct",
    estimated_impact: Optional[Decimal | float | int] = None,
) -> GoalTaskLink:
    """Idempotent: re-linking the same (goal, task) updates the row."""
    existing = (await db.execute(
        select(GoalTaskLink).where(
            GoalTaskLink.goal_id == goal_id,
            GoalTaskLink.task_id == task_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.contribution = contribution
        if estimated_impact is not None:
            existing.estimated_impact = Decimal(str(estimated_impact))
        await db.flush()
        return existing

    link = GoalTaskLink(
        goal_id=goal_id,
        task_id=task_id,
        contribution=contribution,
        estimated_impact=(
            Decimal(str(estimated_impact)) if estimated_impact is not None else None
        ),
    )
    db.add(link)
    await db.flush()
    return link


async def list_links_for_goal(
    db: AsyncSession, goal_id: str,
) -> list[GoalTaskLink]:
    return list((await db.execute(
        select(GoalTaskLink).where(GoalTaskLink.goal_id == goal_id)
    )).scalars().all())


async def list_goals_for_task(
    db: AsyncSession, task_id: str,
) -> list[GoalTaskLink]:
    return list((await db.execute(
        select(GoalTaskLink).where(GoalTaskLink.task_id == task_id)
    )).scalars().all())
