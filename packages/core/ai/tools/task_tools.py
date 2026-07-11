"""Task management tools — search, create, update, get details."""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime.task_actions import (
    runtime_create_task_action,
    runtime_get_task_details_action,
    runtime_normalize_task_priority,
    runtime_search_tasks_action,
    runtime_task_summary_dict,
    runtime_update_task_action,
)

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
            "Search for tasks by query text, status, or priority. "
            "Returns a list of matching tasks with their key fields."
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
                    "enum": [
                        "pending", "in_progress", "completed",
                        "failed", "cancelled", "blocked",
                    ],
                    "description": "Filter by task status.",
                },
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority (5=critical, 4=high, 3=medium, 2=low, 1=minimal).",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "Filter by assignee/staff ID.",
                },
                "completed_after": {
                    "type": "string",
                    "description": "Filter completed tasks at or after this ISO-8601 datetime.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20).",
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
    return await runtime_create_task_action(entity_id=entity_id, params=kwargs)


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
