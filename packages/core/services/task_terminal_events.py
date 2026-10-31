"""What a Task run publishes when it settles, and the config version it ran.

The terminal Task event drives notifications (``task.succeeded`` /
``task.failed``) and the realtime refresh of Task views.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def emit_task_event(task_event: Optional[dict]) -> None:
    """Publish a settled Task's event and refresh its live views; best-effort."""
    if not task_event:
        return
    from packages.core.ai.runtime.task_evidence import runtime_emit_plan_executor_task_event
    from packages.core.services.realtime import broadcast_task_runtime_update

    try:
        runtime_emit_plan_executor_task_event(task_event)
    except Exception:
        logger.debug("Task event emit failed", exc_info=True)
    payload = task_event.get("payload") or {}
    task_id = payload.get("task_id")
    if task_id:
        await broadcast_task_runtime_update(
            task_event.get("entity_id") or "",
            task_id=str(task_id),
            workspace_id=str(payload["workspace_id"]) if payload.get("workspace_id") else None,
            status=str(payload["task_status"]) if payload.get("task_status") else None,
            title=str(payload["title"]) if payload.get("title") else None,
            event=str(task_event.get("event_type") or "runtime_updated"),
        )


async def resolve_config_versions(
    db: AsyncSession, *, agent_id: str | None, skill_id: str | None = None,
) -> Optional[dict]:
    """``{"agent_revision": N, "skill_revision": N}`` of the config that ran.

    Raises on lookup failure; callers stamp best-effort.
    """
    versions: dict = {}
    if agent_id:
        from packages.core.models.workspace import Agent

        revision = (await db.execute(
            select(Agent.revision).where(Agent.id == agent_id)
        )).scalar_one_or_none()
        if revision is not None:
            versions["agent_revision"] = int(revision)
    if skill_id:
        from packages.core.models.skill import Skill

        revision = (await db.execute(
            select(Skill.revision).where(Skill.id == skill_id)
        )).scalar_one_or_none()
        if revision is not None:
            versions["skill_revision"] = int(revision)
    return versions or None


__all__ = ["emit_task_event", "resolve_config_versions"]
