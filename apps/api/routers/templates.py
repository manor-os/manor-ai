"""Task template endpoints — CRUD, instantiation, recurring setup."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.template_service import (
    list_templates, get_template, create_template, update_template,
    delete_template, instantiate_template, setup_recurring_task,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/tasks/templates", tags=["tasks"])


# ── Schemas ──

class TemplateResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    description: str | None = None
    title_template: str
    description_template: str | None = None
    priority: int = 3
    task_type: str = "general"
    category_id: str | None = None
    default_assignee_id: str | None = None
    default_agent_id: str | None = None
    agent_type: str | None = None
    details_template: dict = {}
    tags: list[str] = []
    is_recurring: bool = False
    recurrence_rule: str | None = None
    status: str = "active"
    created_at: str | None = None


class TemplateCreateRequest(BaseModel):
    name: str
    title_template: str
    description: str | None = None
    description_template: str | None = None
    priority: int = 3
    task_type: str = "general"
    category_id: str | None = None
    default_assignee_id: str | None = None
    default_agent_id: str | None = None
    agent_type: str | None = None
    details_template: dict = {}
    tags: list[str] = []


class TemplateUpdateRequest(BaseModel):
    name: str | None = None
    title_template: str | None = None
    description: str | None = None
    description_template: str | None = None
    priority: int | None = None
    task_type: str | None = None
    category_id: str | None = None
    default_assignee_id: str | None = None
    default_agent_id: str | None = None
    agent_type: str | None = None
    details_template: dict | None = None
    tags: list[str] | None = None


class InstantiateRequest(BaseModel):
    variables: dict = {}


class RecurringRequest(BaseModel):
    cron_expr: str


class TaskResponse(BaseModel):
    id: str
    entity_id: str
    title: str
    description: str | None = None
    status: str
    priority: int
    task_type: str
    details: dict = {}
    created_at: str | None = None


class ScheduledJobResponse(BaseModel):
    id: str
    job_id: str
    name: str | None = None
    cron_expr: str | None = None
    execution_type: str | None = None
    execution_target: dict = {}


# ── Helpers ──

def _to_response(t) -> TemplateResponse:
    return TemplateResponse(
        id=t.id, entity_id=t.entity_id, name=t.name,
        description=t.description, title_template=t.title_template,
        description_template=t.description_template, priority=t.priority,
        task_type=t.task_type, category_id=t.category_id,
        default_assignee_id=t.default_assignee_id,
        default_agent_id=t.default_agent_id, agent_type=t.agent_type,
        details_template=t.details_template or {},
        tags=t.tags or [], is_recurring=t.is_recurring,
        recurrence_rule=t.recurrence_rule, status=t.status,
        created_at=t.created_at.isoformat() if t.created_at else None,
    )


# ── Endpoints ──

@router.get("", response_model=list[TemplateResponse])
async def list_task_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    templates = await list_templates(db, user.entity_id)
    return [_to_response(t) for t in templates]


@router.post("", response_model=TemplateResponse, status_code=201)
async def create_task_template(
    req: TemplateCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tmpl = await create_template(
        db, user.entity_id,
        name=req.name, title_template=req.title_template,
        description=req.description,
        description_template=req.description_template,
        priority=req.priority, task_type=req.task_type,
        category_id=req.category_id,
        default_assignee_id=req.default_assignee_id,
        default_agent_id=req.default_agent_id,
        agent_type=req.agent_type,
        details_template=req.details_template,
        tags=req.tags,
    )
    return _to_response(tmpl)


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_task_template(
    template_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tmpl = await get_template(db, template_id, user.entity_id)
    if not tmpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return _to_response(tmpl)


@router.put("/{template_id}", response_model=TemplateResponse)
async def update_task_template(
    template_id: str,
    req: TemplateUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    updates = req.model_dump(exclude_none=True)
    tmpl = await update_template(db, template_id, user.entity_id, **updates)
    if not tmpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return _to_response(tmpl)


@router.delete("/{template_id}", status_code=204)
async def delete_task_template(
    template_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_template(db, template_id, user.entity_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")


@router.post("/{template_id}/instantiate", response_model=TaskResponse, status_code=201)
async def instantiate_task_template(
    template_id: str,
    req: InstantiateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        task = await instantiate_template(
            db, user.entity_id, template_id,
            variables=req.variables, creator_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return TaskResponse(
        id=task.id, entity_id=task.entity_id, title=task.title,
        description=task.description, status=task.status,
        priority=task.priority, task_type=task.task_type,
        details=task.details or {},
        created_at=task.created_at.isoformat() if task.created_at else None,
    )


@router.post("/{template_id}/recurring", response_model=ScheduledJobResponse, status_code=201)
async def setup_recurring(
    template_id: str,
    req: RecurringRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        job = await setup_recurring_task(
            db, user.entity_id, template_id,
            cron_expr=req.cron_expr, user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return ScheduledJobResponse(
        id=job.id, job_id=job.job_id, name=job.name,
        cron_expr=job.cron_expr, execution_type=job.execution_type,
        execution_target=job.execution_target,
    )
