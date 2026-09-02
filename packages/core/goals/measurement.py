"""Measurement service.

Single entry point: ``measure_goal(goal_id)`` — resolves the
Integration, calls the registered measurer, records the value, lets
record_measurement recompute pace, and emits events on pace changes /
achievements.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.goals import GoalStatus
from packages.core.database import async_session
from packages.core.goals import measurers as measurer_registry
from packages.core.goals.numbers import goal_number_to_json
from packages.core.goals.service import record_measurement
from packages.core.models.document import Integration
from packages.core.models.goal import Goal
from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    workspace_autonomy_skip_reason,
    workspace_autonomy_state,
)

logger = logging.getLogger(__name__)


class MeasurementError(Exception):
    """Raised when a measurement attempt fails for a known reason
    (no integration, unsupported metric, adapter error). Distinct from
    unexpected exceptions so the scheduler retry policy can treat them
    differently — measurement errors don't auto-retry forever."""


def _workspace_measurement_skip_reason(
    workspace,
    *,
    require_autonomous_runtime: bool,
) -> str | None:
    autonomy_state = workspace_autonomy_state(workspace)
    if autonomy_state is WorkspaceAutonomyState.RUNNING:
        return None
    if (
        autonomy_state is WorkspaceAutonomyState.DISABLED
        and not require_autonomous_runtime
    ):
        return None
    return workspace_autonomy_skip_reason(workspace)


async def measure_goal(
    goal_id: str,
    db: Optional[AsyncSession] = None,
    *,
    require_autonomous_runtime: bool = True,
) -> dict:
    """Run one measurement cycle for a goal.

    Caller may pass a DB session to participate in an outer transaction;
    otherwise opens its own. Returns a small status dict for logging.
    """
    if db is None:
        async with async_session() as owned_db:
            return await _measure(
                owned_db,
                goal_id,
                commit=True,
                require_autonomous_runtime=require_autonomous_runtime,
            )
    return await _measure(
        db,
        goal_id,
        commit=False,
        require_autonomous_runtime=require_autonomous_runtime,
    )


async def _measure(
    db: AsyncSession,
    goal_id: str,
    *,
    commit: bool,
    require_autonomous_runtime: bool,
) -> dict:
    prev_pace_status: Optional[str] = None
    new_pace_status: Optional[str] = None
    achieved_now = False

    try:
        goal = (await db.execute(
            select(Goal).where(Goal.id == goal_id)
        )).scalar_one_or_none()

        if goal is None:
            raise MeasurementError(f"goal {goal_id} not found")
        if goal.status != GoalStatus.ACTIVE.value:
            from packages.core.goals.scheduling import remove_measurement_schedule
            await remove_measurement_schedule(db, goal)
            if commit:
                await db.commit()
            return {"goal_id": goal_id, "skipped": True, "reason": f"status={goal.status}"}
        if goal.workspace_id:
            from packages.core.models.workspace import Workspace
            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == goal.workspace_id,
                    Workspace.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if workspace is None:
                return {"goal_id": goal_id, "skipped": True, "reason": "workspace_not_found"}
            runtime_skip_reason = _workspace_measurement_skip_reason(
                workspace,
                require_autonomous_runtime=require_autonomous_runtime,
            )
            if runtime_skip_reason:
                return {
                    "goal_id": goal_id,
                    "skipped": True,
                    "reason": runtime_skip_reason,
                }

        source = deepcopy(goal.measurement_source or {})
        measured_metric_key = goal.metric_key
        measured_stat_id = goal.stat_id
        measured_goal_state = (
            goal.baseline_value,
            goal.current_value,
            goal.target_value,
            goal.deadline,
            goal.created_at,
        )
        value_depends_on_goal_state = False
        from packages.core.goals.scheduling import (
            is_auto_measurement_source,
            is_workspace_internal_measurement_source,
        )

        # Sandbox workspaces never call real integrations — generate
        # a plausible value from the goal's pace curve so the demo
        # shows realistic-looking measurements + pace transitions.
        sandbox_value = await _maybe_simulate(db, goal)
        if sandbox_value is not None:
            value_depends_on_goal_state = True
            value = sandbox_value
            measurement_source_label = "simulated"
            params = {}
            provider = "_sandbox"
        else:
            if not is_auto_measurement_source(source):
                return {
                    "goal_id": goal_id,
                    "skipped": True,
                    "reason": "manual_measurement_required",
                }
            provider = source.get("provider")
            if not provider:
                raise MeasurementError(
                    f"goal {goal_id} has no measurement_source.provider"
                )

            if is_workspace_internal_measurement_source(source):
                value_depends_on_goal_state = True
                internal = await _measure_workspace_internal(db, goal)
                if internal is None:
                    return {
                        "goal_id": goal_id,
                        "skipped": True,
                        "reason": "no_workspace_internal_evidence",
                    }
                value = internal["value"]
                params = internal["meta"]
                measurement_source_label = "workspace_internal"
            else:
                measurer = measurer_registry.get(provider)
                if measurer is None:
                    raise MeasurementError(
                        f"no measurer registered for provider={provider!r}; "
                        f"available: {measurer_registry.supported_providers()}"
                    )

                integration = await _resolve_integration(db, goal.entity_id, provider)
                if integration is None:
                    raise MeasurementError(
                        f"no active integration for entity={goal.entity_id} provider={provider}"
                    )

                params = source.get("params") or {}
                value = await measurer(integration, params, goal.metric_key)
                measurement_source_label = f"integration:{provider}"

        if goal.workspace_id:
            # Provider calls can take long enough for the operator to stop the
            # Workspace after the first check. Fence the final write behind
            # the same Workspace -> Goal lock order used by lifecycle changes.
            from packages.core.goals.locking import lock_workspace_for_goal_mutation

            workspace = await lock_workspace_for_goal_mutation(
                db,
                workspace_id=goal.workspace_id,
                entity_id=goal.entity_id,
            )
            if workspace is None:
                return {
                    "goal_id": goal_id,
                    "skipped": True,
                    "reason": "workspace_not_found",
                }
            await db.refresh(workspace)
            runtime_skip_reason = _workspace_measurement_skip_reason(
                workspace,
                require_autonomous_runtime=require_autonomous_runtime,
            )
            if runtime_skip_reason:
                return {
                    "goal_id": goal_id,
                    "skipped": True,
                    "reason": runtime_skip_reason,
                }

        from packages.core.goals.locking import lock_goal_for_mutation

        locked_goal = await lock_goal_for_mutation(
            db,
            goal_id,
            entity_id=goal.entity_id,
        )
        if locked_goal is None:
            return {
                "goal_id": goal_id,
                "skipped": True,
                "reason": "goal_not_found",
            }
        if locked_goal.status != GoalStatus.ACTIVE.value:
            return {
                "goal_id": goal_id,
                "skipped": True,
                "reason": f"status={locked_goal.status}",
            }
        if (
            (locked_goal.measurement_source or {}) != source
            or locked_goal.metric_key != measured_metric_key
            or locked_goal.stat_id != measured_stat_id
            or (
                value_depends_on_goal_state
                and (
                    locked_goal.baseline_value,
                    locked_goal.current_value,
                    locked_goal.target_value,
                    locked_goal.deadline,
                    locked_goal.created_at,
                ) != measured_goal_state
            )
        ):
            return {
                "goal_id": goal_id,
                "skipped": True,
                "reason": "goal_measurement_config_changed",
            }
        goal = locked_goal

        prev_pace_status = goal.pace_status
        measurement = await record_measurement(
            db,
            goal,
            value=value,
            source=measurement_source_label,
            meta={
                "measurement_source": source,
                "provider": provider,
                "measurement": params,
            },
            baseline_value_if_missing=(
                Decimal("0")
                if is_workspace_internal_measurement_source(source)
                else None
            ),
        )
        new_pace_status = goal.pace_status
        achieved_now = goal.status == GoalStatus.ACHIEVED.value and goal.achieved_at is not None and (
            (datetime.now(timezone.utc) - goal.achieved_at).total_seconds() < 60
        )

        if commit:
            await db.commit()

        # Event emission is best-effort and outside the DB transaction —
        # subscribers (workspace_chat, notifications) shouldn't block
        # the measurement write.
        await _maybe_emit_events(
            goal_id=goal_id,
            workspace_id=goal.workspace_id,
            metric_key=goal.metric_key,
            value=measurement.value,
            prev_pace=prev_pace_status,
            new_pace=new_pace_status,
            achieved_now=achieved_now,
        )

        return {
            "goal_id": goal_id,
            "value": goal_number_to_json(measurement.value),
            "pace": new_pace_status,
            "achieved": achieved_now,
        }

    except MeasurementError:
        if commit:
            await db.rollback()
        raise
    except Exception as exc:
        logger.exception("measure_goal %s failed unexpectedly", goal_id)
        if commit:
            await db.rollback()
        raise MeasurementError(str(exc)) from exc


async def _maybe_simulate(db: AsyncSession, goal: Goal) -> Optional["Decimal"]:
    """If the goal's workspace is a sandbox, return a fake measurement
    value. Otherwise return None — caller falls through to the real
    integration path."""
    from decimal import Decimal  # noqa: F401 — typing hint only

    if not goal.workspace_id:
        return None

    from packages.core.models.workspace import Workspace
    from packages.core.workspaces import is_sandbox_workspace, simulate_goal_value

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == goal.workspace_id)
    )).scalar_one_or_none()
    if workspace is None or not is_sandbox_workspace(workspace):
        return None

    return simulate_goal_value(goal)


async def _resolve_integration(
    db: AsyncSession, entity_id: str, provider: str,
) -> Optional[Integration]:
    """Pick the most recently active integration for (entity, provider).
    A workspace might have multiple Twitter accounts later — Demo A v0
    just takes the most recent active row."""
    return (await db.execute(
        select(Integration)
        .where(
            Integration.entity_id == entity_id,
            Integration.provider == provider,
            Integration.status == "active",
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def _measure_workspace_internal(db: AsyncSession, goal: Goal) -> Optional[dict]:
    """Measure deliverable-style goals from Manor's own runtime evidence.

    This is the default for workspace goals that don't have a connected
    external metric provider. It uses GoalTaskLink attribution first, because
    Strategist-created tasks already carry metric deltas such as "3 drafts" or
    "5 competitor signals".
    """
    from packages.core.models.goal import GoalTaskLink
    from packages.core.models.task import Task

    rows = list((await db.execute(
        select(GoalTaskLink, Task)
        .outerjoin(Task, Task.id == GoalTaskLink.task_id)
        .where(GoalTaskLink.goal_id == goal.id)
        .order_by(GoalTaskLink.created_at.asc(), GoalTaskLink.task_id.asc())
    )).all())

    total_delta = Decimal("0")
    completed = 0
    linked = 0
    evidence: list[dict] = []

    for link, task in rows:
        if task is not None and task.entity_id != goal.entity_id:
            continue
        linked += 1
        status = str(getattr(task, "status", "") or "").lower() if task is not None else "missing"
        if status != "completed":
            evidence.append({
                "task_id": link.task_id,
                "status": status,
                "delta": 0,
                "source": "not_completed",
            })
            continue

        completed += 1
        if link.actual_impact is not None:
            delta = Decimal(str(link.actual_impact))
            source = "actual_impact"
        elif link.estimated_impact is not None:
            delta = Decimal(str(link.estimated_impact))
            source = "estimated_impact_proxy"
        else:
            delta = Decimal("1")
            source = "completed_task"
        total_delta += delta
        evidence.append({
            "task_id": link.task_id,
            "status": status,
            "delta": float(delta),
            "source": source,
        })

    baseline = Decimal(str(goal.baseline_value or 0))
    value = baseline + total_delta
    return {
        "value": value,
        "meta": {
            "mode": "linked_task_impact",
            "linked_task_count": linked,
            "completed_task_count": completed,
            "delta": float(total_delta),
            "evidence": evidence[:20],
        },
    }


async def _maybe_emit_events(
    *,
    goal_id: str,
    workspace_id: Optional[str],
    metric_key: str,
    value: Decimal,
    prev_pace: Optional[str],
    new_pace: Optional[str],
    achieved_now: bool,
) -> None:
    """Surface goal events into workspace_chat (the user-facing
    surface). Best-effort — failures here must not break the measurement
    write. We need the entity_id for chat notifications; load lazily."""
    try:
        from sqlalchemy import select
        from packages.core.database import async_session
        from packages.core.models.goal import Goal
        from packages.core.workspace_chat import notifiers as chat_notify
    except Exception:
        return

    try:
        async with async_session() as db:
            goal = (await db.execute(
                select(Goal).where(Goal.id == goal_id)
            )).scalar_one_or_none()
        if goal is None:
            return
        entity_id = goal.entity_id

        if achieved_now:
            await chat_notify.notify_goal_achieved(
                entity_id=entity_id, workspace_id=workspace_id,
                goal_id=goal_id, metric_key=metric_key, value=value,
            )
            return

        if prev_pace != new_pace and new_pace in ("at_risk", "behind", "ahead"):
            await chat_notify.notify_goal_pace_changed(
                entity_id=entity_id, workspace_id=workspace_id,
                goal_id=goal_id, metric_key=metric_key, value=value,
                prev_pace=prev_pace, new_pace=new_pace,
            )
        else:
            # Quiet tick — notifier filters to only post when pace is
            # interesting, so most measurements stay invisible in chat.
            await chat_notify.notify_goal_measured(
                entity_id=entity_id, workspace_id=workspace_id,
                goal_id=goal_id, metric_key=metric_key, value=value,
                pace=new_pace,
            )
    except Exception:
        logger.debug("goal event chat emission failed", exc_info=True)
