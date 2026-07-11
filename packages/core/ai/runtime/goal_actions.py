"""Runtime-owned facade for agent-callable goal actions."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

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
        from packages.core.models.base import generate_ulid
        from packages.core.models.goal import Goal

        async with async_session() as db:
            goal = Goal(
                id=generate_ulid(),
                entity_id=entity_id,
                workspace_id=raw_params.get("workspace_id") or None,
                title=raw_params["title"],
                description=raw_params.get("description"),
                metric_key=raw_params["metric_key"],
                target_value=Decimal(str(target)),
                deadline=deadline,
                measurement_source=raw_params.get("measurement_source") or None,
                measurement_cadence=raw_params.get("measurement_cadence") or None,
                pace_status="unknown",
                status="active",
            )
            db.add(goal)
            await db.commit()
            await db.refresh(goal)

        return json.dumps({
            "goal_id": goal.id,
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
                stmt = stmt.where(Goal.status == "active")
            if raw_params.get("workspace_id"):
                stmt = stmt.where(Goal.workspace_id == raw_params["workspace_id"])

            rows = (await db.execute(stmt)).scalars().all()

        if not rows:
            return json.dumps({"goals": [], "hint": "No active goals."})

        return json.dumps({
            "goals": [
                {
                    "id": goal.id,
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
                }
                for goal in rows
            ],
        })
    except Exception as exc:
        logger.exception("get_goal_status failed")
        return json.dumps({"error": f"failed to read goals: {exc}"})


async def runtime_update_goal_value_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Record a manual goal measurement through the Runtime boundary."""

    raw_params = dict(params or {})
    goal_id = raw_params.get("goal_id")
    value = raw_params.get("value")
    if not goal_id or value is None:
        return json.dumps({"error": "goal_id and value are required"})

    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.goal import Goal, GoalMeasurement

        async with async_session() as db:
            stmt = select(Goal).where(Goal.id == goal_id, Goal.entity_id == entity_id)
            if raw_params.get("workspace_id"):
                stmt = stmt.where(Goal.workspace_id == raw_params["workspace_id"])
            goal = (await db.execute(stmt)).scalar_one_or_none()
            if not goal:
                return json.dumps({"error": "goal not found"})

            now = datetime.now(timezone.utc)
            recorded_workspace_id = goal.workspace_id
            db.add(GoalMeasurement(
                goal_id=goal.id,
                measured_at=now,
                value=Decimal(str(value)),
                source="manual",
                meta={"note": raw_params.get("note")} if raw_params.get("note") else None,
            ))
            if goal.baseline_value is None:
                goal.baseline_value = Decimal(str(value))
            goal.current_value = Decimal(str(value))
            goal.current_value_updated_at = now
            await db.commit()

        return json.dumps({
            "goal_id": goal_id,
            "workspace_id": recorded_workspace_id,
            "value": float(value),
            "recorded_at": now.isoformat(),
        })
    except Exception as exc:
        logger.exception("update_goal_value failed")
        return json.dumps({"error": f"failed to record measurement: {exc}"})
