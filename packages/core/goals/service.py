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
from typing import Optional

from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.goals import GoalStatus
from packages.core.goals.commands import (
    GoalUpdateCommand,
    validate_goal_status_transition,
)
from packages.core.goals.factory import GoalIdentityFactory, GoalKeyConflictError
from packages.core.goals.locking import (
    lock_goal_for_mutation,
    lock_workspace_for_goal_mutation,
)
from packages.core.goals.numbers import goal_number_to_json, validate_goal_number
from packages.core.goals.pace import compute_pace
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    resolve_workspace_autonomy_state,
)
from packages.core.services.workspace_access import workspace_resource_not_soft_deleted

logger = logging.getLogger(__name__)


class GoalLifecycleError(ValueError):
    """Raised when a terminal Goal receives new runtime evidence."""


def _goal_contract_metric_key(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    metric_key = str(raw.get("metric_key") or "").strip()
    if metric_key:
        return metric_key
    return str(raw.get("goal_key") or raw.get("key") or "").strip()


def _goal_contract_goal_key(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("goal_key") or raw.get("key") or "").strip()


def _goal_contract_payload(goal: Goal, previous: object = None) -> dict:
    """Project portable Goal configuration without runtime measurements."""
    payload = dict(previous) if isinstance(previous, dict) else {}
    payload.update({
        "goal_key": goal.goal_key,
        "title": goal.title,
        "description": goal.description,
        "metric_key": goal.metric_key,
        "target_value": goal_number_to_json(goal.target_value),
        "baseline_value": goal_number_to_json(goal.baseline_value),
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
        "measurement_source": goal.measurement_source,
        "measurement_cadence": goal.measurement_cadence,
        "priority": goal.priority,
    })
    # Canonical fields above supersede legacy aliases. Runtime outcome fields
    # deliberately never enter the portable Workspace contract.
    payload.pop("target", None)
    payload.pop("cadence", None)
    payload.pop("status", None)
    payload.pop("current_value", None)
    payload.pop("pace_status", None)
    payload.pop("achieved_at", None)
    return payload


async def _sync_workspace_goal_contract(
    db: AsyncSession,
    goal: Goal,
    *,
    previous_metric_key: str | None = None,
    remove: bool = False,
) -> bool:
    """Keep Workspace portable Goal config aligned with an operator mutation."""
    if not goal.workspace_id:
        return False
    workspace = await lock_workspace_for_goal_mutation(
        db,
        workspace_id=goal.workspace_id,
        entity_id=goal.entity_id,
    )
    if workspace is None:
        return False

    operating_model = dict(workspace.operating_model or {})
    current_goals = [
        dict(row) for row in operating_model.get("goals") or []
        if isinstance(row, dict)
    ]
    # goal_key identifies one Goal. metric_key intentionally does not: many
    # Goals may measure the same metric with different targets or deadlines.
    matching_indexes = [
        index for index, row in enumerate(current_goals)
        if _goal_contract_goal_key(row) == goal.goal_key
    ]
    if not matching_indexes:
        # Legacy contracts could omit goal_key and only carry metric_key. Bind
        # at most one such row during migration; never collapse every Goal that
        # happens to share the metric.
        lookup_metrics = {
            key for key in (previous_metric_key, goal.metric_key) if key
        }
        matching_indexes = [
            index for index, row in enumerate(current_goals)
            if not _goal_contract_goal_key(row)
            and _goal_contract_metric_key(row) in lookup_metrics
        ][:1]

    next_goals = list(current_goals)
    if remove:
        next_goals = [
            row for index, row in enumerate(current_goals)
            if index not in matching_indexes
        ]
    elif matching_indexes:
        primary_index = matching_indexes[0]
        next_goals[primary_index] = _goal_contract_payload(
            goal,
            current_goals[primary_index],
        )
        next_goals = [
            row for index, row in enumerate(next_goals)
            if index == primary_index or index not in matching_indexes[1:]
        ]
    else:
        next_goals.append(_goal_contract_payload(goal))

    if next_goals == current_goals:
        return False
    operating_model["goals"] = next_goals
    workspace.operating_model = operating_model
    workspace.operation_revision = int(workspace.operation_revision or 0) + 1
    from packages.core.workspace_chat.context import invalidate

    invalidate(workspace.id)
    await db.flush()
    return True


async def _workspace_allows_goal_schedules(
    db: AsyncSession,
    *,
    workspace_id: str | None,
    entity_id: str,
) -> bool:
    """Return the current Workspace runtime switch under its mutation lock."""
    if not workspace_id:
        return True
    state = (await db.execute(
        select(
            Workspace.status,
            Workspace.heartbeat_enabled,
            Workspace.deleted_at,
        ).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
        )
    )).one_or_none()
    return bool(
        state is not None
        and resolve_workspace_autonomy_state(
            status=state.status,
            heartbeat_enabled=bool(state.heartbeat_enabled),
            deleted_at=state.deleted_at,
        ) is WorkspaceAutonomyState.RUNNING
    )


# ── CRUD ──────────────────────────────────────────────────────────────

async def create_goal(
    db: AsyncSession,
    *,
    entity_id: str,
    title: str,
    goal_key: str | None = None,
    metric_key: str | None,
    target_value: Decimal | float | int,
    workspace_id: Optional[str] = None,
    stat_id: Optional[str] = None,
    description: Optional[str] = None,
    baseline_value: Optional[Decimal | float | int] = None,
    deadline: Optional[date] = None,
    measurement_source: Optional[dict] = None,
    measurement_cadence: Optional[str] = None,
    priority: int = 3,
    install_schedule: bool = True,
    sync_contract: bool = True,
) -> Goal:
    """Create a Goal. If ``measurement_cadence`` and
    ``measurement_source`` are both set and ``install_schedule`` is
    true, a ScheduledJob is installed on the same DB transaction when
    the owning Workspace runtime is enabled (entity-level Goals are not
    runtime-gated).
    """
    workspace = await lock_workspace_for_goal_mutation(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    if workspace_id and workspace is None:
        raise ValueError(f"workspace {workspace_id} no longer exists")
    identity = await GoalIdentityFactory(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    ).create(
        title=title,
        goal_key=goal_key,
        metric_key=metric_key,
    )
    from packages.core.goals.scheduling import (
        default_workspace_measurement_source,
        is_workspace_internal_measurement_source,
        validate_measurement_cadence,
    )

    measurement_source = (
        None
        if stat_id
        else default_workspace_measurement_source(
            measurement_source,
            workspace_id=workspace_id,
        )
    )
    if measurement_source and is_workspace_internal_measurement_source(measurement_source):
        measurement_cadence = measurement_cadence or "daily"
        if baseline_value is None:
            baseline_value = 0
    if measurement_cadence is not None:
        measurement_cadence = validate_measurement_cadence(measurement_cadence)

    goal = Goal(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        stat_id=stat_id,
        title=title,
        description=description,
        goal_key=identity.goal_key,
        metric_key=identity.metric_key,
        target_value=validate_goal_number(target_value),
        baseline_value=(
            validate_goal_number(baseline_value)
            if baseline_value is not None else None
        ),
        deadline=deadline,
        measurement_source=measurement_source,
        measurement_cadence=measurement_cadence,
        priority=priority,
        pace_status="unknown",
        status=GoalStatus.ACTIVE.value,
    )
    savepoint = await db.begin_nested()
    try:
        db.add(goal)
        await db.flush()
    except IntegrityError as exc:
        await savepoint.rollback()
        raise GoalKeyConflictError(
            f"goal_key {identity.goal_key!r} already exists in this scope"
        ) from exc
    except Exception:
        await savepoint.rollback()
        raise
    else:
        await savepoint.commit()

    if install_schedule:
        # Local import to avoid circular dependency (scheduling needs
        # the Goal id and the entity it belongs to).
        from packages.core.goals.scheduling import (
            install_measurement_schedule,
            should_install_measurement_schedule,
        )
        if (
            should_install_measurement_schedule(goal)
            and await _workspace_allows_goal_schedules(
                db,
                workspace_id=workspace_id,
                entity_id=entity_id,
            )
        ):
            await install_measurement_schedule(db, goal)

    contract_changed = False
    if sync_contract:
        contract_changed = await _sync_workspace_goal_contract(db, goal)

    await sync_workspace_goal_mode(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        bump_operation_revision=sync_contract and not contract_changed,
    )

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
    readable_workspace_ids: set[str] | None = None,
) -> list[Goal]:
    stmt = select(Goal).where(Goal.entity_id == entity_id)
    stmt = stmt.where(
        workspace_resource_not_soft_deleted(
            Goal.workspace_id,
            entity_id=entity_id,
        )
    )
    if workspace_id:
        stmt = stmt.where(Goal.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Goal.status == status)
    if readable_workspace_ids is not None:
        # Restrict to readable workspaces + workspace-less goals; None (entity
        # admin) is unrestricted.
        stmt = stmt.where(or_(
            Goal.workspace_id.is_(None),
            Goal.workspace_id.in_(readable_workspace_ids),
        ))
    stmt = stmt.order_by(Goal.priority.desc(), Goal.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def update_goal(
    db: AsyncSession,
    goal_id: str,
    entity_id: str,
    *,
    sync_contract: bool = True,
    **fields,
) -> Optional[Goal]:
    """Update allowlisted Goal fields. ``measurement_source`` /
    ``measurement_cadence`` changes trigger a ScheduledJob refresh."""
    fields = GoalUpdateCommand.from_fields(fields).values
    goal = await lock_goal_for_mutation(db, goal_id, entity_id=entity_id)
    if not goal:
        return None
    validate_goal_status_transition(goal.status, fields.get("status"))

    previous_metric_key = goal.metric_key
    previous_stat_id = goal.stat_id
    previous_measurement_source = goal.measurement_source
    previous_measurement_cadence = goal.measurement_cadence
    # M11: operator-driven config fields tracked for revision bumps.
    # current_value / measurement writes intentionally excluded — those
    # are facts about the world, not config changes.
    previous_config = {
        "target_value": goal.target_value,
        "deadline": goal.deadline,
        "status": goal.status,
        "stat_id": goal.stat_id,
    }
    schedule_relevant_changed = (
        ("measurement_source" in fields and fields["measurement_source"] != goal.measurement_source)
        or ("measurement_cadence" in fields and fields["measurement_cadence"] != goal.measurement_cadence)
        or ("status" in fields and fields["status"] != goal.status)
    )

    from packages.core.goals.scheduling import validate_measurement_cadence

    for k, v in fields.items():
        if v is None and k not in {
            # explicit-clear-allowed fields
            "deadline", "description", "measurement_source",
            "measurement_cadence", "baseline_value", "stat_id",
        }:
            continue
        if k in {"target_value", "baseline_value", "current_value"} and v is not None:
            v = validate_goal_number(v)
        if k == "measurement_cadence" and v is not None:
            v = validate_measurement_cadence(v)
        if hasattr(goal, k):
            setattr(goal, k, v)

    if goal.stat_id != previous_stat_id:
        # A Stat binding owns the current observation contract. Retaining the
        # prior Stat's value would misrepresent the newly selected metric.
        goal.current_value = None
        goal.current_value_updated_at = None
        goal.pace_status = None
        goal.pace_computed_at = None
        goal.achieved_at = None

    config_changed = {
        key: getattr(goal, key)
        for key, old in previous_config.items()
        if getattr(goal, key) != old
    }
    if config_changed:
        from packages.core.revisions import bump_revision
        await bump_revision(db, goal, patch=config_changed)
    await db.flush()

    from packages.core.goals.scheduling import (
        default_workspace_measurement_source,
        is_workspace_internal_measurement_source,
    )
    goal.measurement_source = (
        None
        if goal.stat_id
        else default_workspace_measurement_source(
            goal.measurement_source,
            workspace_id=goal.workspace_id,
        )
    )
    if goal.stat_id:
        goal.measurement_cadence = None
    if goal.measurement_source and is_workspace_internal_measurement_source(goal.measurement_source):
        goal.measurement_cadence = goal.measurement_cadence or "daily"
        if goal.baseline_value is None:
            goal.baseline_value = Decimal("0")
    await db.flush()
    normalized_schedule_changed = (
        goal.measurement_source != previous_measurement_source
        or goal.measurement_cadence != previous_measurement_cadence
    )

    if schedule_relevant_changed or normalized_schedule_changed or "stat_id" in fields:
        from packages.core.goals.scheduling import (
            install_measurement_schedule,
            remove_measurement_schedule,
            should_install_measurement_schedule,
        )
        await remove_measurement_schedule(db, goal)
        if (
            should_install_measurement_schedule(goal)
            and await _workspace_allows_goal_schedules(
                db,
                workspace_id=goal.workspace_id,
                entity_id=goal.entity_id,
            )
        ):
            await install_measurement_schedule(db, goal)

    contract_changed = False
    if sync_contract:
        contract_changed = await _sync_workspace_goal_contract(
            db,
            goal,
            previous_metric_key=previous_metric_key,
            remove=goal.status == GoalStatus.ABANDONED.value,
        )

    await sync_workspace_goal_mode(
        db,
        workspace_id=goal.workspace_id,
        entity_id=goal.entity_id,
        bump_operation_revision=sync_contract and not contract_changed,
    )

    return goal


async def delete_goal(db: AsyncSession, goal_id: str, entity_id: str) -> bool:
    goal = await lock_goal_for_mutation(db, goal_id, entity_id=entity_id)
    if not goal:
        return False
    workspace_id = goal.workspace_id
    goal_entity_id = goal.entity_id
    from packages.core.goals.scheduling import remove_measurement_schedule
    await remove_measurement_schedule(db, goal)
    if goal.status != GoalStatus.ABANDONED.value:
        goal.status = GoalStatus.ABANDONED.value
        from packages.core.revisions import bump_revision

        await bump_revision(
            db,
            goal,
            patch={"status": GoalStatus.ABANDONED.value},
        )
    contract_changed = await _sync_workspace_goal_contract(db, goal, remove=True)
    await db.flush()
    await sync_workspace_goal_mode(
        db,
        workspace_id=workspace_id,
        entity_id=goal_entity_id,
        bump_operation_revision=not contract_changed,
    )
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
    baseline_value_if_missing: Optional[Decimal | float | int] = None,
) -> GoalMeasurement:
    """Append a measurement, update goal.current_value, recompute pace.

    Caller owns the transaction — this only flushes, never commits.
    Returns the inserted GoalMeasurement row.
    """
    locked_goal = await lock_goal_for_mutation(
        db,
        goal.id,
        entity_id=goal.entity_id,
    )
    if locked_goal is None:
        raise ValueError(f"goal {goal.id} no longer exists")
    goal = locked_goal
    if goal.status in {
        GoalStatus.ACHIEVED.value,
        GoalStatus.ABANDONED.value,
    }:
        raise GoalLifecycleError(f"goal {goal.id} is {goal.status}")
    measured_at = measured_at or datetime.now(timezone.utc)
    value_dec = validate_goal_number(value)

    # Baseline lock: first ever measurement sets baseline.
    if goal.baseline_value is None:
        goal.baseline_value = (
            validate_goal_number(baseline_value_if_missing)
            if baseline_value_if_missing is not None
            else value_dec
        )

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

    old_pace = goal.pace_status
    achieved_now = False
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
        if goal.pace_status == "achieved" and goal.status == GoalStatus.ACTIVE.value:
            goal.status = GoalStatus.ACHIEVED.value
            goal.achieved_at = measured_at
            achieved_now = True
            from packages.core.goals.scheduling import remove_measurement_schedule
            await remove_measurement_schedule(db, goal)

    await db.flush()
    # Ledger (M1): goal_measured (+ pace change / achievement), same transaction.
    from packages.core.ledger.adapters import record_goal_measurement_events
    await record_goal_measurement_events(
        db, goal, value=value_dec, source=source, measured_at=measured_at,
        old_pace=old_pace, pace_recomputed=recompute_pace_now, achieved_now=achieved_now,
    )
    if achieved_now:
        await sync_workspace_goal_mode(
            db,
            workspace_id=goal.workspace_id,
            entity_id=goal.entity_id,
        )
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
    actual_impact: Optional[Decimal | float | int] = None,
) -> GoalTaskLink:
    """Idempotent: re-linking the same (goal, task) updates the row."""
    goal = await lock_goal_for_mutation(db, goal_id)
    if goal is None:
        raise ValueError(f"goal {goal_id} no longer exists")
    if goal.status == GoalStatus.ABANDONED.value:
        raise GoalLifecycleError(f"goal {goal_id} is abandoned")
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
        if actual_impact is not None:
            existing.actual_impact = Decimal(str(actual_impact))
        await db.flush()
        return existing

    link = GoalTaskLink(
        goal_id=goal_id,
        task_id=task_id,
        contribution=contribution,
        estimated_impact=(
            Decimal(str(estimated_impact)) if estimated_impact is not None else None
        ),
        actual_impact=(
            Decimal(str(actual_impact)) if actual_impact is not None else None
        ),
    )
    db.add(link)
    await db.flush()
    return link


async def sync_workspace_goal_mode(
    db: AsyncSession,
    *,
    workspace_id: str | None,
    entity_id: str,
    bump_operation_revision: bool = True,
) -> bool | None:
    """Persist whether a Workspace currently has any active Goals."""
    workspace = await lock_workspace_for_goal_mutation(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    if workspace is None or workspace.deleted_at is not None:
        return None

    has_active_goals = (await db.execute(
        select(Goal.id).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == GoalStatus.ACTIVE.value,
        ).limit(1)
    )).scalar_one_or_none() is not None
    operating_model = dict(workspace.operating_model or {})
    strategist = dict(operating_model.get("strategist") or {})
    if strategist.get("use_goals") != has_active_goals:
        strategist["use_goals"] = has_active_goals
        operating_model["strategist"] = strategist
        workspace.operating_model = operating_model
        if bump_operation_revision:
            workspace.operation_revision = int(workspace.operation_revision or 0) + 1
        from packages.core.workspace_chat.context import invalidate

        invalidate(workspace.id)
        await db.flush()
    return has_active_goals


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
