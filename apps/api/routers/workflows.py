"""Workflow endpoints — definitions, runs, and step execution."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import workflow_service as svc
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


# ── Schemas ──

class WorkflowCreateRequest(BaseModel):
    name: str
    steps: list[dict]
    description: str | None = None
    trigger_type: str = "manual"
    trigger_config: dict = {}
    variables: dict = {}
    category: str | None = None
    tags: list[str] = []


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    steps: list[dict] | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    variables: dict | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_active: bool | None = None
    status: str | None = None


class WorkflowResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    description: str | None = None
    trigger_type: str
    trigger_config: dict = {}
    steps: list[dict] = []
    variables: dict = {}
    category: str | None = None
    tags: list[str] = []
    is_active: bool = True
    version: int = 1
    status: str = "active"


class RunStartRequest(BaseModel):
    variables: dict | None = None
    trigger_data: dict | None = None


class RunResponse(BaseModel):
    id: str
    workflow_id: str
    entity_id: str
    status: str
    current_step_id: str | None = None
    variables: dict = {}
    step_results: dict = {}
    trigger_data: dict = {}
    error: str | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ResumeRequest(BaseModel):
    variables: dict | None = None


# ── Helpers ──

def _wf_to_dict(wf) -> dict:
    return WorkflowResponse(
        id=wf.id,
        entity_id=wf.entity_id,
        name=wf.name,
        description=wf.description,
        trigger_type=wf.trigger_type,
        trigger_config=wf.trigger_config or {},
        steps=wf.steps or [],
        variables=wf.variables or {},
        category=wf.category,
        tags=wf.tags or [],
        is_active=wf.is_active,
        version=wf.version,
        status=wf.status,
    ).model_dump()


def _run_to_dict(run) -> dict:
    return RunResponse(
        id=run.id,
        workflow_id=run.workflow_id,
        entity_id=run.entity_id,
        status=run.status,
        current_step_id=run.current_step_id,
        variables=run.variables or {},
        step_results=run.step_results or {},
        trigger_data=run.trigger_data or {},
        error=run.error,
        started_by=run.started_by,
        started_at=run.started_at,
        completed_at=run.completed_at,
    ).model_dump(mode="json")


# ── Workflow Definition — collection endpoints ──

@router.get("", response_model=list[WorkflowResponse])
async def list_workflows(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await svc.list_workflows(db, user.entity_id)
    return [_wf_to_dict(w) for w in items]


@router.post("", status_code=201)
async def create_workflow(
    body: WorkflowCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wf = await svc.create_workflow(
        db,
        entity_id=user.entity_id,
        name=body.name,
        steps=body.steps,
        description=body.description,
        trigger_type=body.trigger_type,
        trigger_config=body.trigger_config,
        variables=body.variables,
        category=body.category,
        tags=body.tags,
    )
    await db.commit()
    return _wf_to_dict(wf)


# ── Run endpoints (must be before /{workflow_id} to avoid route shadowing) ──

@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return _run_to_dict(run)


@router.post("/runs/{run_id}/step")
async def execute_step(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await svc.execute_workflow_step(db, run_id, user.entity_id)
    if "error" in result and result.get("error") and "status" not in result:
        raise HTTPException(400, result["error"])
    await db.commit()
    return result


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status in ("completed", "cancelled"):
        raise HTTPException(400, f"Run already {run.status}")
    run.status = "cancelled"
    await db.flush()
    await db.commit()
    return _run_to_dict(run)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: ResumeRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await svc.get_run(db, run_id, user.entity_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status != "paused":
        raise HTTPException(400, "Run is not paused")
    # Merge in any new variables
    if body and body.variables:
        updated = dict(run.variables or {})
        updated.update(body.variables)
        run.variables = updated
    # Advance past the wait step
    wf = await svc.get_workflow(db, run.workflow_id, user.entity_id)
    if wf:
        steps = wf.steps or []
        current = next((s for s in steps if s["id"] == run.current_step_id), None)
        if current:
            next_steps = current.get("next", [])
            if next_steps:
                run.current_step_id = next_steps[0]
            else:
                run.current_step_id = None
    run.status = "running"
    await db.flush()
    await db.commit()
    return _run_to_dict(run)


# ── Workflow Definition — single-item endpoints ──

@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wf = await svc.get_workflow(db, workflow_id, user.entity_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    return _wf_to_dict(wf)


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wf = await svc.update_workflow(
        db, workflow_id, user.entity_id,
        **body.model_dump(exclude_none=True),
    )
    if not wf:
        raise HTTPException(404, "Workflow not found")
    await db.commit()
    return _wf_to_dict(wf)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await svc.delete_workflow(db, workflow_id, user.entity_id)
    if not deleted:
        raise HTTPException(404, "Workflow not found")
    await db.commit()


# ── Workflow-scoped run endpoints ──

@router.post("/{workflow_id}/run", status_code=201)
async def start_run(
    workflow_id: str,
    body: RunStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await svc.start_workflow(
            db,
            entity_id=user.entity_id,
            workflow_id=workflow_id,
            variables=body.variables if body else None,
            trigger_data=body.trigger_data if body else None,
            started_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    await db.commit()
    return _run_to_dict(run)


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: str,
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    runs = await svc.list_runs(db, user.entity_id, workflow_id=workflow_id, status=status, limit=limit)
    return [_run_to_dict(r) for r in runs]
