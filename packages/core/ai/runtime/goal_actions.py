"""Runtime-owned facade for agent-callable goal actions."""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from packages.core.constants.goals import GoalStatus

logger = logging.getLogger(__name__)


async def runtime_create_goal_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Create a persistent business goal through the Runtime boundary."""

    raw_params = dict(params or {})
    if not raw_params.get("title") or not raw_params.get("metric_key"):
        return json.dumps({"error": "title and metric_key are required"})

    target = raw_params.get("target_value")
    if target is None:
        return json.dumps({"error": "target_value is required"})

    deadline = raw_params.get("deadline")
    if deadline:
        try:
            deadline = date.fromisoformat(deadline)
        except ValueError:
            return json.dumps({"error": f"deadline {deadline!r} is not YYYY-MM-DD"})

    try:
        from packages.core.database import async_session
        from packages.core.goals.service import create_goal

        async with async_session() as db:
            goal = await create_goal(
                db,
                entity_id=entity_id,
                workspace_id=raw_params.get("workspace_id") or None,
                title=raw_params["title"],
                goal_key=raw_params.get("goal_key") or None,
                description=raw_params.get("description"),
                metric_key=raw_params["metric_key"],
                target_value=Decimal(str(target)),
                deadline=deadline,
                measurement_source=raw_params.get("measurement_source") or None,
                measurement_cadence=raw_params.get("measurement_cadence") or None,
            )
            await db.commit()
            await db.refresh(goal)

        return json.dumps({
            "goal_id": goal.id,
            "goal_key": goal.goal_key,
            "title": goal.title,
            "metric_key": goal.metric_key,
            "target_value": float(goal.target_value),
            "status": goal.status,
            "deadline": goal.deadline.isoformat() if goal.deadline else None,
        })
    except Exception as exc:
        logger.exception("create_goal failed")
        return json.dumps({"error": f"failed to create goal: {exc}"})


async def runtime_get_goal_status_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Read goal status through the Runtime boundary."""

    raw_params = dict(params or {})
    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.goal import Goal

        async with async_session() as db:
            stmt = select(Goal).where(Goal.entity_id == entity_id)
            if raw_params.get("goal_id"):
                stmt = stmt.where(Goal.id == raw_params["goal_id"])
            else:
                stmt = stmt.where(Goal.status == GoalStatus.ACTIVE.value)
            if raw_params.get("workspace_id"):
                stmt = stmt.where(Goal.workspace_id == raw_params["workspace_id"])

            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return json.dumps({"goals": [], "hint": "No active goals."})

        return json.dumps({
            "goals": [
                {
                    "id": goal.id,
                    "goal_key": goal.goal_key,
                    "title": goal.title,
                    "metric_key": goal.metric_key,
                    "target_value": float(goal.target_value),
                    "current_value": (
                        float(goal.current_value)
                        if goal.current_value is not None
                        else None
                    ),
                    "baseline_value": (
                        float(goal.baseline_value)
                        if goal.baseline_value is not None
                        else None
                    ),
                    "current_value_updated_at": (
                        goal.current_value_updated_at.isoformat()
                        if getattr(goal, "current_value_updated_at", None)
                        else None
                    ),
                    "measurement_source": getattr(goal, "measurement_source", None),
                    "measurement_cadence": getattr(goal, "measurement_cadence", None),
                    "pace_status": goal.pace_status,
                    "deadline": goal.deadline.isoformat() if goal.deadline else None,
                    "status": goal.status,
                    "workspace_id": goal.workspace_id,
                    "stat_id": goal.stat_id,
                    "measurement_write_action": "record_stat_assessment" if goal.stat_id else "update_goal_value",
                }
                for goal in rows
            ],
        })
    except Exception as exc:
        logger.exception("get_goal_status failed")
        return json.dumps({"error": f"failed to read goals: {exc}"})


def _goal_measurement_lookup(raw_params: dict[str, Any]) -> str:
    """Accept goal_id or goal_key from the same update_goal_value call."""

    for key in ("goal_id", "goal_key"):
        value = str(raw_params.get(key) or "").strip()
        if value:
            return value
    return ""


def _goal_measurement_value(raw_params: dict[str, Any]) -> Any:
    """Accept value, or a score/current_value alias from the same call."""

    for key in ("value", "score", "current_value"):
        if raw_params.get(key) is not None:
            return raw_params.get(key)
    return None


def _goal_value_scale_mismatch_error(goal: Any, value: Any) -> str | None:
    """Reject 0-1 fractions when the Goal itself uses a larger numeric scale."""

    target = getattr(goal, "target_value", None)
    if target is None:
        return None
    try:
        target_num = Decimal(str(target))
        numeric = Decimal(str(value))
    except Exception:
        return None
    if target_num > 1 and numeric > 0 and numeric < 1:
        return json.dumps({
            "error": (
                "value is on the wrong scale for this Goal. "
                "Persist on the Goal's own scale "
                f"(target_value={float(target_num)}); "
                "do not write a 0-1 fraction."
            ),
            "target_value": float(target_num),
            "received_value": float(numeric),
        })
    return None


async def runtime_update_goal_value_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Record a manual goal measurement through the Runtime boundary."""

    raw_params = dict(params or {})
    goal_id = _goal_measurement_lookup(raw_params)
    value = _goal_measurement_value(raw_params)
    if not goal_id or value is None:
        received = sorted(
            str(key)
            for key in raw_params.keys()
            if str(key) not in {"workspace_id", "note"}
        )
        return json.dumps({
            "error": "goal_id (or goal_key) and value are required in the same call",
            "received_keys": received,
            "example": {"goal_id": "<goal ulid or goal_key>", "value": 0},
        })

    try:
        from sqlalchemy import or_, select
        from sqlalchemy.exc import MultipleResultsFound

        from packages.core.database import async_session
        from packages.core.goals.service import record_measurement
        from packages.core.models.goal import Goal

        async with async_session() as db:
            stmt = select(Goal).where(Goal.entity_id == entity_id)
            if raw_params.get("workspace_id"):
                stmt = stmt.where(
                    Goal.workspace_id == raw_params["workspace_id"],
                    or_(Goal.id == goal_id, Goal.goal_key == goal_id),
                )
            else:
                stmt = stmt.where(Goal.id == goal_id)
            try:
                goal = (await db.execute(stmt)).scalar_one_or_none()
            except MultipleResultsFound:
                return json.dumps({"error": "goal not found"})
            if not goal:
                return json.dumps({"error": "goal not found"})

            scale_error = _goal_value_scale_mismatch_error(goal, value)
            if scale_error:
                return scale_error

            recorded_goal_id = goal.id
            recorded_workspace_id = goal.workspace_id
            measurement = await record_measurement(
                db,
                goal,
                value=Decimal(str(value)),
                source="manual",
                meta={"note": raw_params.get("note")} if raw_params.get("note") else None,
            )
            now = measurement.measured_at
            await db.commit()

        return json.dumps({
            "goal_id": recorded_goal_id,
            "workspace_id": recorded_workspace_id,
            "value": float(value),
            "recorded_at": now.isoformat(),
            "measurement": {"goal_id": recorded_goal_id, "measured_at": now.isoformat()},
            "observation_id": (measurement.meta or {}).get("observation_id"),
            "status": "persisted",
        })
    except Exception as exc:
        logger.exception("update_goal_value failed")
        from packages.core.ai.runtime.tool_error_contract import tool_error_payload
        return json.dumps(tool_error_payload(
            tool_name="update_goal_value", code="goal_measurement_failed",
            message=str(exc), details={"goal_id": goal_id, "next_read": "get_stat_status"},
        ))
