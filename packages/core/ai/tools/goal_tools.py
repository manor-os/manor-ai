"""Goal management tools — create / inspect / update business goals.

A *Goal* in the goal-driven runtime is a persistent business target
(e.g. "Twitter followers ≥ 10000 by 2026-10-24"). It is **not** a unit
of work — Tasks are. The agent uses these tools when the user
expresses a long-running objective; the Strategist later reads active
goals to propose Tasks that move them.

The old GoalRun/GoalStep concept (single ad-hoc agent execution) is
gone — see packages/core/models/execution.py for the replacement
(ExecutionPlan/ExecutionStep) and packages/core/models/goal.py for
the persistent Goal model these tools now operate on.
"""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime.goal_actions import (
    runtime_create_goal_action,
    runtime_get_goal_status_action,
    runtime_update_goal_value_action,
)

logger = logging.getLogger(__name__)


CREATE_GOAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_goal",
        "description": (
            "Create a persistent business GOAL — a metric the user commits "
            "to moving over weeks or months (e.g. '10k Twitter followers by "
            "October'). Goals are periodically measured and inform the "
            "Strategist's weekly task proposals. Use only when the user "
            "expresses a measurable long-running objective. For one-off "
            "actions, create a Task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short label for the goal."},
                "description": {"type": "string", "description": "Optional long-form context."},
                "metric_key": {
                    "type": "string",
                    "description": "Canonical metric, e.g. 'follower_count', 'mrr', 'engagement_rate'.",
                },
                "target_value": {
                    "type": "number",
                    "description": "Target value of the metric.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Target completion date (YYYY-MM-DD). Optional.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace this goal belongs to. Omit for entity-level goals.",
                },
                "measurement_source": {
                    "type": "object",
                    "description": (
                        "How to measure: {provider, action, params}. Optional — "
                        "without it the goal must be updated manually."
                    ),
                },
                "measurement_cadence": {
                    "type": "string",
                    "description": "'hourly' | 'daily' | 'weekly' | cron expression.",
                },
            },
            "required": ["title", "metric_key", "target_value"],
        },
    },
}


GET_GOAL_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_goal_status",
        "description": "Return the current state and pace of one or all active goals.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "string",
                    "description": "Specific goal id; omit to list all active goals for the entity.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Filter by workspace; omit for all workspaces.",
                },
            },
        },
    },
}


UPDATE_GOAL_VALUE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_goal_value",
        "description": (
            "Manually record a measurement for a goal. Appends a row to "
            "goal_measurements and updates current_value. Used when "
            "automatic measurement isn't configured."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "value": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["goal_id", "value"],
        },
    },
}


async def _create_goal_handler(entity_id: str = "", **kwargs: Any) -> str:
    return await runtime_create_goal_action(entity_id=entity_id, params=kwargs)


async def _get_goal_status_handler(entity_id: str = "", **kwargs: Any) -> str:
    return await runtime_get_goal_status_action(entity_id=entity_id, params=kwargs)


async def _update_goal_value_handler(entity_id: str = "", **kwargs: Any) -> str:
    return await runtime_update_goal_value_action(entity_id=entity_id, params=kwargs)


def get_tools():
    return [
        (CREATE_GOAL_SCHEMA, _create_goal_handler),
        (GET_GOAL_STATUS_SCHEMA, _get_goal_status_handler),
        (UPDATE_GOAL_VALUE_SCHEMA, _update_goal_value_handler),
    ]
