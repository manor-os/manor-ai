"""Task management tools — search, create, update, get details."""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.task_actions import (
    runtime_create_task_action,
    runtime_get_task_details_action,
    runtime_normalize_task_priority,
    runtime_search_tasks_action,
    runtime_task_summary_dict,
    runtime_update_task_action,
)
from packages.core.constants.task import TASK_STATUSES

logger = logging.getLogger(__name__)


def _normalize_priority(value: Any, default: int = 3) -> int:
    return runtime_normalize_task_priority(value, default=default)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SEARCH_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tasks",
        "description": (
            "Search tasks with all known filters in one call. Values within a plural "
            "filter are ORed; different filters and ranges are ANDed. Range endpoints "
            "are inclusive. Filtering happens before pagination, and total is the exact "
            "filtered count. Do not issue one call per status or priority."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search against task title and description.",
                },
                "status": {
                    "type": "string",
                    "enum": list(TASK_STATUSES),
                    "description": "Legacy single-status filter. Use statuses for multiple values.",
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TASK_STATUSES)},
                    "minItems": 1,
                    "maxItems": len(TASK_STATUSES),
                    "uniqueItems": True,
                    "description": "Match any listed status in one query (OR).",
                },
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Legacy exact priority filter. Use priorities for multiple values.",
                },
                "priorities": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 5},
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "description": "Match any listed priority in one query (OR).",
                },
                "priority_min": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Inclusive minimum priority.",
                },
                "priority_max": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Inclusive maximum priority.",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "Legacy single assignee/staff ID filter.",
                },
                "assignee_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "description": "Match any listed assignee/staff ID (OR).",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Legacy single Workspace ID filter.",
                },
                "workspace_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "description": "Match Tasks in any listed Workspace (OR).",
                },
                "category_id": {
                    "type": "string",
                    "description": "Legacy single Task category ID filter.",
                },
                "category_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "description": "Match any listed Task category ID (OR).",
                },
                "task_type": {
                    "type": "string",
                    "description": "Legacy single Task type slug filter.",
                },
                "task_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "uniqueItems": True,
                    "description": "Match any listed Task type slug (OR).",
                },
                "created_after": {
                    "type": "string",
                    "description": "Inclusive created-at lower bound as ISO-8601 datetime.",
                },
                "created_before": {
                    "type": "string",
                    "description": "Inclusive created-at upper bound as ISO-8601 datetime.",
                },
                "updated_after": {
                    "type": "string",
                    "description": "Inclusive updated-at lower bound as ISO-8601 datetime.",
                },
                "updated_before": {
                    "type": "string",
                    "description": "Inclusive updated-at upper bound as ISO-8601 datetime.",
                },
                "completed_after": {
                    "type": "string",
                    "description": "Inclusive completed-at lower bound as ISO-8601 datetime.",
                },
                "completed_before": {
                    "type": "string",
                    "description": "Inclusive completed-at upper bound as ISO-8601 datetime.",
                },
                "deadline_after": {
                    "type": "string",
                    "description": "Inclusive deadline lower bound as ISO-8601 datetime.",
                },
                "deadline_before": {
                    "type": "string",
                    "description": "Inclusive deadline upper bound as ISO-8601 datetime.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max results to return (default 20).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Filtered-result offset for pagination (default 0).",
                },
            },
            "required": [],
        },
    },
}

CREATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Create a new task. Returns the created task ID and summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title (required).",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed description of the task.",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority: 5=critical, 4=high, 3=medium (default), 2=low, 1=minimal.",
                },
                "task_type": {
                    "type": "string",
                    "description": "Task type/category slug (default 'general').",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "User ID to assign the task to.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Deadline as ISO-8601 datetime string.",
                },
            },
            "required": ["title"],
        },
    },
}

UPDATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_task",
        "description": "Update fields on an existing task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to update.",
                },
                "title": {"type": "string", "description": "New title."},
                "description": {"type": "string", "description": "New description."},
                "status": {
                    "type": "string",
                    "enum": [
                        "pending", "in_progress", "completed",
                        "failed", "cancelled", "blocked",
                    ],
                    "description": "New status.",
                },
                "priority": {
                    "type": "integer",
                    "description": "New priority (5=critical, 4=high, 3=medium, 2=low, 1=minimal).",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "New assignee user ID.",
                },
            },
            "required": ["task_id"],
        },
    },
}

GET_TASK_DETAILS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_task_details",
        "description": "Get full details for a single task by ID, including processing logs.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID.",
                },
            },
            "required": ["task_id"],
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_to_dict(task) -> dict:
    return runtime_task_summary_dict(task)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _search_tasks(entity_id: str, **kwargs: Any) -> str:
    return await runtime_search_tasks_action(entity_id=entity_id, params=kwargs)


async def _create_task(entity_id: str, **kwargs: Any) -> str:
    # The runtime always knows which agent is calling; passing it through is
    # what lets the creation log name that agent instead of a placeholder.
    context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_create_task_action(
        entity_id=entity_id,
        params=kwargs,
        actor_user_id=context.user_id,
        actor_agent_id=context.agent_id,
    )


async def _update_task(entity_id: str, **kwargs: Any) -> str:
    return await runtime_update_task_action(entity_id=entity_id, params=kwargs)


async def _get_task_details(entity_id: str, **kwargs: Any) -> str:
    return await runtime_get_task_details_action(entity_id=entity_id, params=kwargs)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (SEARCH_TASKS_SCHEMA, _search_tasks),
        (CREATE_TASK_SCHEMA, _create_task),
        (UPDATE_TASK_SCHEMA, _update_task),
        (GET_TASK_DETAILS_SCHEMA, _get_task_details),
    ]
