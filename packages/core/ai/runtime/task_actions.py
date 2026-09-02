"""Runtime-owned facade for agent-callable task actions."""

from __future__ import annotations

import json
from typing import Any


_RUNTIME_TASK_PRIORITY_LABELS = {
    "minimal": 1,
    "min": 1,
    "low": 2,
    "medium": 3,
    "normal": 3,
    "high": 4,
    "critical": 5,
    "urgent": 5,
}

_RUNTIME_TASK_SEARCH_FILTER_KEYS = frozenset({
    "assignee_id",
    "assignee_ids",
    "category_id",
    "category_ids",
    "completed_after",
    "completed_before",
    "created_after",
    "created_before",
    "deadline_after",
    "deadline_before",
    "priorities",
    "priority",
    "priority_max",
    "priority_min",
    "query",
    "status",
    "statuses",
    "task_type",
    "task_types",
    "updated_after",
    "updated_before",
    "workspace_id",
    "workspace_ids",
})


def runtime_normalize_task_priority(value: Any, default: int = 3) -> int:
    """Normalize task priority labels/numbers to Manor's 1-5 scale."""

    if isinstance(value, str):
        mapped = _RUNTIME_TASK_PRIORITY_LABELS.get(value.strip().lower())
        if mapped is not None:
            return mapped
    try:
        return max(1, min(int(value), 5))
    except (TypeError, ValueError):
        return default


def runtime_task_summary_dict(task: Any) -> dict[str, Any]:
    """Serialize a Task-like object for agent-callable task tools."""

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "task_type": task.task_type,
        "assignee_id": task.assignee_id,
        "creator_id": task.creator_id,
        "workspace_id": task.workspace_id,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


async def runtime_search_tasks_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Search tasks through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services import task_service

    raw_params = dict(params or {})
    try:
        limit = max(1, min(int(raw_params.get("limit") or 20), 100))
        offset = max(0, int(raw_params.get("offset") or 0))
    except (TypeError, ValueError):
        return json.dumps({
            "error": "invalid_task_filters",
            "message": "limit and offset must be integers",
        })
    service_params = {
        key: value
        for key, value in raw_params.items()
        if key in _RUNTIME_TASK_SEARCH_FILTER_KEYS
        and value not in (None, "", [], ())
    }
    service_params.update({"limit": limit, "offset": offset})

    try:
        async with async_session() as db:
            tasks, total = await task_service.list_tasks(
                db,
                entity_id,
                **service_params,
            )
    except (TypeError, ValueError) as exc:
        return json.dumps({
            "error": "invalid_task_filters",
            "message": str(exc),
        })

    results = [runtime_task_summary_dict(task) for task in tasks]
    next_offset = offset + len(results)
    has_more = next_offset < total
    return json.dumps({
        "total": total,
        "count": len(results),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "tasks": results,
    })


async def runtime_create_task_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
    actor_agent_id: str | None = None,
) -> str:
    """Create a task through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services import task_service

    raw_params = dict(params or {})
    title = raw_params.get("title")
    if not title:
        return json.dumps({"error": "title is required"})
    if raw_params.get("task_type") == "interactive":
        return json.dumps({
            "error": "interactive_task_requires_workspace_host",
            "message": (
                "Create interactive Tasks with workspace_create_task so a "
                "Workspace and Host Agent subscription are bound explicitly."
            ),
        })

    async with async_session() as db:
        task = await task_service.create_task(
            db,
            entity_id,
            title=title,
            description=raw_params.get("description", ""),
            priority=runtime_normalize_task_priority(raw_params.get("priority", 3)),
            task_type=raw_params.get("task_type", "general"),
            assignee_id=raw_params.get("assignee_id"),
            deadline=raw_params.get("deadline"),
            creator_id=actor_user_id,
            creator_agent_id=actor_agent_id,
        )
        await db.commit()

    return json.dumps({"created": True, "task": runtime_task_summary_dict(task)})


async def runtime_update_task_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Update a task through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services import task_service

    raw_params = dict(params or {})
    task_id = raw_params.get("task_id")
    if not task_id:
        return json.dumps({"error": "task_id is required"})

    fields = {
        key: value
        for key, value in raw_params.items()
        if key != "task_id" and value is not None
    }
    if "priority" in fields:
        fields["priority"] = runtime_normalize_task_priority(fields["priority"])

    async with async_session() as db:
        task = await task_service.update_task(db, task_id, entity_id, **fields)
        if not task:
            return json.dumps({"error": f"Task {task_id} not found"})
        await db.commit()

    return json.dumps({"updated": True, "task": runtime_task_summary_dict(task)})


async def runtime_get_task_details_action(
    *,
    entity_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Get task details through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services import task_service

    raw_params = dict(params or {})
    task_id = raw_params.get("task_id")
    if not task_id:
        return json.dumps({"error": "task_id is required"})

    async with async_session() as db:
        task = await task_service.get_task(db, task_id, entity_id)
        if not task:
            return json.dumps({"error": f"Task {task_id} not found"})

        logs = await task_service.get_task_logs(db, task_id)
        log_dicts = [
            {
                "log_type": log.log_type,
                "content": log.content,
                "created_by": log.created_by,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]

    result = runtime_task_summary_dict(task)
    result["logs"] = log_dicts
    return json.dumps(result)
