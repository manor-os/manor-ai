"""Goal-template endpoints — list bundled recipes + apply one to a workspace.

Distinct from ``apps.api.routers.templates`` (which is the legacy
*task*-template router for one-off recurring tasks). This is the M10
recipe surface: a template here mints a goal + seed tasks + measurement
schedule in one transaction.

Endpoints:

  GET  /api/v1/goal-templates                         list bundled recipes
  GET  /api/v1/goal-templates/{key}                   single recipe + schema
  POST /api/v1/workspaces/{workspace_id}/apply-template
                                                       apply a recipe
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.entity_service import get_workspace
from packages.core.templates import (
    REGISTRY,
    TemplateError,
    TemplateInput,
    apply_template,
    list_templates,
)

router = APIRouter(prefix="/api/v1/goal-templates", tags=["goal-templates"])
apply_router = APIRouter(prefix="/api/v1/workspaces", tags=["goal-templates"])


# ── Models ────────────────────────────────────────────────────────────

class GoalTemplateSummary(BaseModel):
    key: str
    title: str
    summary: str
    params_schema: dict[str, Any]


class ApplyTemplateRequest(BaseModel):
    template_key: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ApplyTemplateResponse(BaseModel):
    template_key: str
    goal_id: str | None
    task_ids: list[str]
    scheduled_job_ids: list[str]
    notes: list[str]


# ── List / detail ─────────────────────────────────────────────────────

@router.get("", response_model=list[GoalTemplateSummary])
async def list_goal_templates(
    user: User = Depends(get_current_user),
):
    """All bundled goal-template recipes. Auth-gated only so we don't
    leak the catalog to anonymous visitors — the recipes themselves
    are static + safe."""
    return [GoalTemplateSummary(**t) for t in list_templates()]


@router.get("/{key}", response_model=GoalTemplateSummary)
async def get_goal_template(
    key: str,
    user: User = Depends(get_current_user),
):
    if key not in REGISTRY:
        raise HTTPException(404, f"no goal template with key={key!r}")
    t = REGISTRY[key]
    return GoalTemplateSummary(
        key=t.key, title=t.title, summary=t.summary, params_schema=t.params_schema,
    )


# ── Apply ─────────────────────────────────────────────────────────────

@apply_router.post(
    "/{workspace_id}/apply-template",
    response_model=ApplyTemplateResponse,
    status_code=201,
)
async def apply_goal_template(
    workspace_id: str,
    req: ApplyTemplateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint goal + tasks + schedules from a recipe. Caller commits via FastAPI."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    inp = TemplateInput(
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        user_id=user.id,
        params=req.params,
    )
    try:
        result = await apply_template(db, req.template_key, inp)
    except TemplateError as exc:
        # Param-validation / unknown-key errors surface as 400 — these
        # are user-actionable, not server bugs.
        raise HTTPException(400, str(exc))

    await db.commit()
    return ApplyTemplateResponse(
        template_key=result.template_key,
        goal_id=result.goal_id,
        task_ids=result.task_ids,
        scheduled_job_ids=result.scheduled_job_ids,
        notes=result.notes,
    )
