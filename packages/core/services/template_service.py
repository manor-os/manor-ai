"""Task template service — CRUD, instantiation, recurring setup."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.task_template import TaskTemplate


# ── CRUD ──

async def list_templates(db: AsyncSession, entity_id: str) -> list[TaskTemplate]:
    result = await db.execute(
        select(TaskTemplate)
        .where(TaskTemplate.entity_id == entity_id, TaskTemplate.status == "active")
        .order_by(TaskTemplate.created_at.desc())
    )
    return list(result.scalars().all())


async def get_template(db: AsyncSession, template_id: str, entity_id: str) -> Optional[TaskTemplate]:
    result = await db.execute(
        select(TaskTemplate).where(
            TaskTemplate.id == template_id,
            TaskTemplate.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def create_template(
    db: AsyncSession, entity_id: str, *,
    name: str,
    title_template: str,
    description: str | None = None,
    description_template: str | None = None,
    priority: int = 3,
    task_type: str = "general",
    category_id: str | None = None,
    default_assignee_id: str | None = None,
    default_agent_id: str | None = None,
    agent_type: str | None = None,
    details_template: dict | None = None,
    tags: list[str] | None = None,
) -> TaskTemplate:
    tmpl = TaskTemplate(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        title_template=title_template,
        description=description,
        description_template=description_template,
        priority=priority,
        task_type=task_type,
        category_id=category_id,
        default_assignee_id=default_assignee_id,
        default_agent_id=default_agent_id,
        agent_type=agent_type,
        details_template=details_template or {},
        tags=tags or [],
    )
    db.add(tmpl)
    await db.flush()
    return tmpl


async def update_template(
    db: AsyncSession, template_id: str, entity_id: str, **kwargs
) -> Optional[TaskTemplate]:
    tmpl = await get_template(db, template_id, entity_id)
    if not tmpl:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(tmpl, key):
            setattr(tmpl, key, value)
    await db.flush()
    await db.refresh(tmpl)
    return tmpl


async def delete_template(db: AsyncSession, template_id: str, entity_id: str) -> bool:
    tmpl = await get_template(db, template_id, entity_id)
    if not tmpl:
        return False
    await db.delete(tmpl)
    await db.flush()
    return True


# ── Instantiation ──

async def instantiate_template(
    db: AsyncSession, entity_id: str, template_id: str,
    variables: dict | None = None,
    creator_id: str | None = None,
) -> Task:
    """Create a task from a template, filling {{var}} placeholders."""
    template = await get_template(db, template_id, entity_id)
    if not template:
        raise ValueError("Template not found")

    variables = variables or {}
    variables.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    variables.setdefault("datetime", datetime.now(timezone.utc).isoformat())

    title = _render_template(template.title_template, variables)
    description = _render_template(template.description_template or "", variables)
    details = _render_dict_template(template.details_template or {}, variables)

    from packages.core.services.task_service import create_task
    task = await create_task(
        db, entity_id,
        title=title,
        description=description,
        priority=template.priority,
        task_type=template.task_type,
        category_id=template.category_id,
        assignee_id=template.default_assignee_id,
        agent_id=template.default_agent_id,
        agent_type=template.agent_type,
        details=details,
        creator_id=creator_id,
    )
    return task


# ── Recurring ──

async def setup_recurring_task(
    db: AsyncSession, entity_id: str, template_id: str,
    cron_expr: str, user_id: str | None = None,
) -> "ScheduledJob":
    """Create a scheduled job that instantiates a task template on a cron schedule."""
    from packages.core.models.scheduler import ScheduledJob

    template = await get_template(db, template_id, entity_id)
    if not template:
        raise ValueError("Template not found")

    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"recurring-task-{template_id}-{generate_ulid()[:8]}",
        entity_id=entity_id,
        name=f"Recurring: {template.name}",
        job_type="cron",
        schedule_kind="cron",
        cron_expr=cron_expr,
        execution_type="task_template",
        execution_target={"template_id": template_id},
        user_id=user_id,
    )
    db.add(job)
    await db.flush()

    # Mark template as recurring
    template.is_recurring = True
    template.recurrence_rule = cron_expr
    await db.flush()

    return job


# ── Template rendering helpers ──

def _render_template(template: str, variables: dict) -> str:
    """Replace {{var}} placeholders with values."""
    def replacer(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", replacer, template)


def _render_dict_template(template: dict, variables: dict) -> dict:
    """Recursively render template placeholders in a dict."""
    result = {}
    for k, v in template.items():
        if isinstance(v, str):
            result[k] = _render_template(v, variables)
        elif isinstance(v, dict):
            result[k] = _render_dict_template(v, variables)
        else:
            result[k] = v
    return result
