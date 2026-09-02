"""Scheduler endpoints — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import CONVERSATION_LOG_TYPES
from packages.core.constants.task_actors import TaskActor
from packages.core.constants.execution import (
    DEFAULT_AGENT_MAX_TURNS,
    ScheduledJobSkillGenerationStatus,
)
from packages.core.constants.execution import (
    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS,
)
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.scheduler_service import (
    create_scheduled_job, list_scheduled_jobs, get_scheduled_job,
    mutate_scheduled_job, delete_scheduled_job, toggle_scheduled_job,
    summarize_scheduled_jobs,
    list_job_runs,
    create_agent_execution, list_agent_executions, update_agent_execution,
    defer_scheduled_job_skill_generation,
    scheduled_job_workspace_scope,
)
from apps.api.deps import get_current_user, require_workspace_readable, require_workspace_writable
from packages.core.services.workspace_access import (
    readable_workspace_ids_for_user,
    user_writable_workspace_ids,
)
from packages.core.workspaces import is_sandbox_workspace

jobs_router = APIRouter(prefix="/api/v1/jobs", tags=["scheduled-jobs"])
executions_router = APIRouter(prefix="/api/v1/executions", tags=["agent-executions"])

logger = logging.getLogger(__name__)


async def _legacy_compatible_workspace_gate(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
    *,
    writable: bool,
) -> bool:
    """Gate real Workspace ids while preserving old opaque job labels."""
    normalized = str(workspace_id or "").strip()
    if not normalized:
        return False
    known = (await db.execute(
        select(Workspace.id).where(
            Workspace.id == normalized,
        ).limit(1)
    )).scalar_one_or_none()
    if known:
        if writable:
            await require_workspace_writable(db, user, normalized)
        else:
            await require_workspace_readable(db, user, normalized)
        return True
    return False


async def _can_manage_workspace_resource(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> bool:
    """Project write capability for a Workspace resource detail response."""
    normalized = str(workspace_id or "").strip()
    if not normalized:
        return True
    known = (await db.execute(
        select(Workspace.id).where(
            Workspace.id == normalized,
        ).limit(1)
    )).scalar_one_or_none()
    if not known:
        return True
    return normalized in await user_writable_workspace_ids(
        db,
        entity_id=user.entity_id,
        workspace_ids={normalized},
        user_id=user.id,
        role=user.role,
    )


async def _workspace_status_for_job(
    db: AsyncSession,
    user: User,
    workspace_id: str | None,
) -> str | None:
    """Return the live Workspace status for a real job binding."""
    normalized = str(workspace_id or "").strip()
    if not normalized:
        return None
    return (await db.execute(
        select(Workspace.status).where(
            Workspace.id == normalized,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()


# ── Schemas: Scheduled Jobs ──

class ScheduledJobResponse(BaseModel):
    id: str
    job_id: str
    entity_id: str | None = None
    workspace_id: str | None = None
    workspace_status: str | None = None
    name: str | None = None
    job_type: str = "cron"
    schedule_kind: str | None = None
    cron_expr: str | None = None
    every_seconds: float | None = None
    run_at: str | None = None
    timezone: str = "UTC"
    payload_message: str | None = None
    agent_id: str | None = None
    execution_type: str | None = None
    execution_target: dict = {}
    execution_script: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    default_delivery_mode: str | None = None
    goal_id: str | None = None
    goal_step_id: str | None = None
    manor_task_id: str | None = None
    enabled: bool = True
    delete_after_run: bool | None = False
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    skill_generation_status: ScheduledJobSkillGenerationStatus = (
        ScheduledJobSkillGenerationStatus.IDLE
    )
    skill_generation_attempts: int = 0
    skill_generation_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    can_manage: bool = True


class ScheduledJobCreateRequest(BaseModel):
    job_id: str
    name: str
    job_type: str = "cron"
    schedule_kind: str | None = None
    cron_expr: str | None = None
    every_seconds: float | None = None
    run_at: str | None = None
    timezone: str = "UTC"
    payload_message: str | None = None
    agent_id: str | None = None
    execution_type: str | None = None
    execution_target: dict | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    default_delivery_mode: str | None = None


class ScheduledJobUpdateRequest(BaseModel):
    name: str | None = None
    job_type: str | None = None
    schedule_kind: str | None = None
    cron_expr: str | None = None
    every_seconds: float | None = None
    run_at: str | None = None
    timezone: str | None = None
    payload_message: str | None = None
    agent_id: str | None = None
    execution_type: str | None = None
    execution_target: dict | None = None
    execution_script: str | None = None
    default_delivery_mode: str | None = None


class ScheduledJobListResponse(BaseModel):
    items: list[ScheduledJobResponse]
    total: int
    summary_total: int
    enabled_total: int
    attention_total: int


class ToggleRequest(BaseModel):
    enabled: bool


class JobRunResponse(BaseModel):
    id: str
    job_id: str
    idempotency_key: str | None = None
    status: str
    trigger_type: str | None = None
    result: dict | None = None
    error: str | None = None
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None


# ── Schemas: Agent Executions ──

class AgentExecutionResponse(BaseModel):
    id: str
    entity_id: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    status: str = "running"
    turns_used: int = 0
    max_turns: int = DEFAULT_AGENT_MAX_TURNS
    supervisor_verdict: str | None = None
    input_message: str | None = None
    output_message: str | None = None
    tools_used: list = []
    token_usage: dict = {}
    error: str | None = None
    duration_ms: float | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None


class AgentExecutionCreateRequest(BaseModel):
    agent_id: str
    task_id: str | None = None
    conversation_id: str | None = None
    workspace_id: str | None = None
    input_message: str | None = None
    max_turns: int = DEFAULT_AGENT_MAX_TURNS


class AgentExecutionUpdateRequest(BaseModel):
    status: str | None = None
    turns_used: int | None = None
    supervisor_verdict: str | None = None
    output_message: str | None = None
    tools_used: list | None = None
    token_usage: dict | None = None
    error: str | None = None
    duration_ms: float | None = None
    completed_at: str | None = None


class AgentExecutionListResponse(BaseModel):
    items: list[AgentExecutionResponse]
    total: int


# ── Helpers ──

def _job_response(
    j,
    *,
    last_error: str | None = None,
    can_manage: bool = True,
    workspace_status: str | None = None,
) -> ScheduledJobResponse:
    generation_revision = getattr(j, "skill_generation_revision", None)
    generation_error = getattr(j, "skill_generation_last_error", None)
    if generation_revision is not None:
        generation_status = (
            ScheduledJobSkillGenerationStatus.PENDING
            if bool(j.enabled)
            else ScheduledJobSkillGenerationStatus.PAUSED
        )
    elif generation_error:
        generation_status = ScheduledJobSkillGenerationStatus.FAILED
    else:
        generation_status = ScheduledJobSkillGenerationStatus.IDLE
    return ScheduledJobResponse(
        id=j.id, job_id=j.job_id, entity_id=j.entity_id,
        workspace_id=j.workspace_id, name=j.name, job_type=j.job_type,
        workspace_status=workspace_status,
        schedule_kind=j.schedule_kind, cron_expr=j.cron_expr,
        every_seconds=j.every_seconds, run_at=j.run_at, timezone=j.timezone,
        payload_message=j.payload_message, agent_id=j.agent_id,
        execution_type=j.execution_type,
        execution_target=j.execution_target or {},
        execution_script=j.execution_script,
        conversation_id=j.conversation_id, user_id=j.user_id,
        default_delivery_mode=j.default_delivery_mode,
        goal_id=j.goal_id, goal_step_id=j.goal_step_id,
        manor_task_id=j.manor_task_id,
        enabled=bool(j.enabled and (workspace_status is None or workspace_status == "active")), delete_after_run=j.delete_after_run,
        last_run_at=j.last_run_at.isoformat() if j.last_run_at else None,
        last_status=j.last_status,
        last_error=last_error,
        consecutive_errors=j.consecutive_errors or 0,
        skill_generation_status=generation_status,
        skill_generation_attempts=(
            getattr(j, "skill_generation_attempts", 0) or 0
        ),
        skill_generation_error=generation_error,
        created_at=j.created_at.isoformat() if j.created_at else None,
        updated_at=j.updated_at.isoformat() if j.updated_at else None,
        can_manage=can_manage,
    )


def _run_response(r) -> JobRunResponse:
    return JobRunResponse(
        id=r.id, job_id=r.job_id, status=r.status,
        idempotency_key=r.idempotency_key,
        trigger_type=r.trigger_type, result=r.result, error=r.error,
        duration_ms=r.duration_ms,
        prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
        started_at=r.started_at.isoformat() if r.started_at else None,
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        created_at=r.created_at.isoformat() if r.created_at else None,
    )


def _exec_response(e) -> AgentExecutionResponse:
    return AgentExecutionResponse(
        id=e.id, entity_id=e.entity_id, workspace_id=e.workspace_id,
        agent_id=e.agent_id, task_id=e.task_id,
        conversation_id=e.conversation_id, status=e.status,
        turns_used=e.turns_used or 0, max_turns=e.max_turns or 5,
        supervisor_verdict=e.supervisor_verdict,
        input_message=e.input_message, output_message=e.output_message,
        tools_used=e.tools_used or [], token_usage=e.token_usage or {},
        error=e.error, duration_ms=e.duration_ms,
        started_at=e.started_at.isoformat() if e.started_at else None,
        completed_at=e.completed_at.isoformat() if e.completed_at else None,
        created_at=e.created_at.isoformat() if e.created_at else None,
    )


async def _latest_job_errors(
    db: AsyncSession,
    job_ids: list[str],
) -> dict[str, str]:
    if not job_ids:
        return {}

    from packages.core.models.scheduler import ScheduledJobRun

    ranked = (
        select(
            ScheduledJobRun.job_id.label("job_id"),
            ScheduledJobRun.error.label("error"),
            func.row_number().over(
                partition_by=ScheduledJobRun.job_id,
                order_by=ScheduledJobRun.created_at.desc(),
            ).label("position"),
        )
        .where(ScheduledJobRun.job_id.in_(job_ids))
        .subquery()
    )
    rows = (
        await db.execute(
            select(ranked.c.job_id, ranked.c.error).where(
                ranked.c.position == 1,
            )
        )
    ).all()
    return {
        str(job_id): str(error)
        for job_id, error in rows
        if error
    }


# ── Scheduled Jobs Endpoints ──

@jobs_router.get("", response_model=ScheduledJobListResponse)
async def list_jobs(
    enabled_only: bool = Query(False),
    workspace_id: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    status: Literal["all", "enabled", "paused", "attention"] = Query("all"),
    agent_id: str | None = Query(None),
    include_workflows: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Older entity-level automation rows may carry an opaque workspace label
    # that has no Workspace row. Preserve that legacy filter behavior while
    # enforcing membership for real, persisted Workspaces.
    if workspace_id:
        workspace_exists = (await db.execute(
            select(Workspace.id).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == user.entity_id,
            ).limit(1)
        )).scalar_one_or_none()
        if workspace_exists:
            await require_workspace_readable(db, user, workspace_id)
    readable_ws = await readable_workspace_ids_for_user(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    jobs, total = await list_scheduled_jobs(
        db, user.entity_id, enabled_only=enabled_only,
        workspace_id=workspace_id,
        search=search, status=status, agent_id=agent_id,
        include_workflows=include_workflows,
        readable_workspace_ids=readable_ws,
        limit=limit, offset=offset,
    )
    summary = await summarize_scheduled_jobs(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        search=search,
        agent_id=agent_id,
        include_workflows=include_workflows,
        readable_workspace_ids=readable_ws,
    )
    latest_errors = await _latest_job_errors(
        db,
        [job.job_id for job in jobs if (job.consecutive_errors or 0) > 0],
    )
    job_workspace_ids = {
        str(job.workspace_id)
        for job in jobs
        if job.workspace_id
    }
    real_job_workspace_ids = set((await db.execute(
        select(Workspace.id).where(
            Workspace.deleted_at.is_(None),
            Workspace.id.in_(job_workspace_ids),
        )
    )).scalars()) if job_workspace_ids else set()
    workspace_statuses = {
        str(workspace_id): status
        for workspace_id, status in (await db.execute(
            select(Workspace.id, Workspace.status).where(
                Workspace.deleted_at.is_(None),
                Workspace.id.in_(real_job_workspace_ids),
            )
        )).all()
    } if real_job_workspace_ids else {}
    writable_ws = await user_writable_workspace_ids(
        db,
        entity_id=user.entity_id,
        workspace_ids={str(value) for value in real_job_workspace_ids},
        user_id=user.id,
        role=user.role,
    )
    return ScheduledJobListResponse(
        items=[
            _job_response(
                job,
                last_error=latest_errors.get(job.job_id),
                can_manage=(
                    not job.workspace_id
                    or str(job.workspace_id) not in real_job_workspace_ids
                    or str(job.workspace_id) in writable_ws
                ),
                workspace_status=workspace_statuses.get(str(job.workspace_id)) if job.workspace_id else None,
            )
            for job in jobs
        ],
        total=total,
        summary_total=summary["total"],
        enabled_total=summary["enabled"],
        attention_total=summary["attention"],
    )


@jobs_router.post("", response_model=ScheduledJobResponse, status_code=201)
async def create_job(
    req: ScheduledJobCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        scheduled_job_workspace_scope(req.workspace_id, req.execution_target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    workspace_known = await _legacy_compatible_workspace_gate(
        db,
        user,
        req.workspace_id,
        writable=True,
    )
    if req.workspace_id and not workspace_known:
        raise HTTPException(404, "Workspace not found")
    try:
        job = await create_scheduled_job(
            db, user.entity_id, req.job_id, req.name,
            job_type=req.job_type, schedule_kind=req.schedule_kind,
            cron_expr=req.cron_expr, every_seconds=req.every_seconds,
            run_at=req.run_at, timezone_str=req.timezone,
            payload_message=req.payload_message, agent_id=req.agent_id,
            execution_type=req.execution_type, execution_target=req.execution_target,
            workspace_id=req.workspace_id,
            conversation_id=req.conversation_id,
            default_delivery_mode=req.default_delivery_mode,
            user_id=user.id,
            require_workspace=bool(req.workspace_id),
        )
    except ValueError as exc:
        if str(exc) == "Workspace not found":
            raise HTTPException(404, "Workspace not found") from exc
        raise
    if req.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == req.workspace_id,
                Workspace.entity_id == user.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is not None and workspace.status != "active":
            job.enabled = False
            job.next_run_at = None
            job.skill_generation_next_attempt_at = None
            await db.flush()

    # Commit the producer before publishing its background consumer. This also
    # releases every lifecycle/resource lock before billable provider work.
    response = _job_response(job)
    if req.payload_message and req.agent_id and job.enabled:
        generation_args = (
            job.id,
            req.payload_message,
            req.name or "",
            int(job.revision or 1),
        )
        await db.commit()
        try:
            from packages.core.tasks.ai_tasks import generate_job_skill
            generate_job_skill.delay(*generation_args)
            await defer_scheduled_job_skill_generation(
                db,
                job_id=generation_args[0],
                revision=generation_args[3],
                next_attempt_at=datetime.now(timezone.utc)
                + timedelta(
                    seconds=SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
                ),
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("Failed to dispatch skill generation for job %s: %s", req.job_id, e)

    return response


@jobs_router.get("/{job_id}", response_model=ScheduledJobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, job.workspace_id, writable=False)
    return _job_response(
        job,
        can_manage=await _can_manage_workspace_resource(db, user, job.workspace_id),
        workspace_status=await _workspace_status_for_job(db, user, job.workspace_id),
    )


@jobs_router.put("/{job_id}", response_model=ScheduledJobResponse)
async def update_job(
    job_id: str,
    req: ScheduledJobUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await get_scheduled_job(db, job_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(
        db,
        user,
        existing.workspace_id,
        writable=True,
    )
    if req.execution_target is not None:
        try:
            scheduled_job_workspace_scope(
                existing.workspace_id,
                req.execution_target,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    mutation = await mutate_scheduled_job(
        db,
        job_id,
        user.entity_id,
        changed_by_kind=TaskActor.USER.value,
        changed_by_id=user.id,
        **req.model_dump(exclude_none=True),
    )
    if mutation is None:
        raise HTTPException(404, "Scheduled job not found")
    job = mutation.job
    # The factory uses a nested transaction while holding the row lock. SQLAlchemy
    # may expire server-managed columns when that savepoint closes, so reload the
    # locked row before constructing the response or generation payload.
    await db.refresh(job)

    # The locked factory diff is authoritative under concurrent PUTs. Two
    # same-payload requests serialize on the Job row and only the one that
    # actually changed the revision publishes generation work.
    generation_inputs_changed = (
        bool(job.enabled)
        and bool(job.payload_message)
        and bool(job.agent_id)
        and (
            "payload_message" in mutation.content_patch
            or "agent_id" in mutation.content_patch
        )
    )
    response = _job_response(
        job,
        workspace_status=await _workspace_status_for_job(
            db,
            user,
            job.workspace_id,
        ),
    )
    if generation_inputs_changed:
        generation_args = (
            job.id,
            job.payload_message,
            job.name or "",
            int(job.revision or 1),
        )
        await db.commit()
        try:
            from packages.core.tasks.ai_tasks import generate_job_skill

            generate_job_skill.delay(*generation_args)
            await defer_scheduled_job_skill_generation(
                db,
                job_id=generation_args[0],
                revision=generation_args[3],
                next_attempt_at=datetime.now(timezone.utc)
                + timedelta(
                    seconds=SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS
                ),
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning(
                "Failed to dispatch skill generation for job %s: %s",
                job_id,
                e,
            )

    return response


@jobs_router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await get_scheduled_job(db, job_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, existing.workspace_id, writable=True)
    deleted = await delete_scheduled_job(db, job_id, user.entity_id)
    if not deleted:
        raise HTTPException(404, "Scheduled job not found")


@jobs_router.post("/{job_id}/toggle", response_model=ScheduledJobResponse)
async def toggle_job(
    job_id: str,
    req: ToggleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await get_scheduled_job(db, job_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, existing.workspace_id, writable=True)
    if req.enabled and existing.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == existing.workspace_id,
                Workspace.entity_id == user.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is not None and workspace.status != "active":
            raise HTTPException(409, "Workspace is not active - resume it before enabling automations")
    job = await toggle_scheduled_job(
        db,
        job_id,
        user.entity_id,
        req.enabled,
        changed_by_kind=TaskActor.USER.value,
        changed_by_id=user.id,
    )
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    return _job_response(
        job,
        workspace_status=await _workspace_status_for_job(db, user, job.workspace_id),
    )


@jobs_router.get("/{job_id}/runs", response_model=list[JobRunResponse])
async def get_job_runs(
    job_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify job belongs to entity
    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, job.workspace_id, writable=False)
    runs = await list_job_runs(db, job.job_id, limit=limit)
    return [_run_response(r) for r in runs]


class JobRunDetailResponse(BaseModel):
    """Run row + linked Task / AgentExecution payload so the UI can show
    the prompt that was sent and what the agent produced."""
    run: JobRunResponse
    task: dict | None = None
    """{'id', 'title', 'status', 'description', 'response',
    'turns_used', 'supervisor_verdict'} for the Task that was
    created/dispatched for this run, or None if the exec_type didn't
    create a task (e.g. workflow / goal_measurement). ``response``
    carries the canned-failure-text + provider error detail when the
    agent hit an LLM error."""
    agent_execution: AgentExecutionResponse | None = None


@jobs_router.get("/{job_id}/runs/{run_id}", response_model=JobRunDetailResponse)
async def get_job_run_detail(
    job_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Detail view: the run + the prompt + the agent's output + tools +
    tokens. Joins through Task.details.scheduled_run_id to find the
    AgentExecution row."""
    from sqlalchemy import select
    from packages.core.models.scheduler import (
        ScheduledJobRun, AgentExecution,
    )
    from packages.core.models.task import Task

    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, job.workspace_id, writable=False)

    run = (await db.execute(
        select(ScheduledJobRun).where(
            ScheduledJobRun.id == run_id,
            ScheduledJobRun.job_id == job.job_id,
        )
    )).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Job run not found")

    # Find the Task this run created (linked via details.scheduled_run_id).
    task_row = (await db.execute(
        select(Task).where(
            Task.entity_id == user.entity_id,
            Task.details["scheduled_run_id"].astext == run_id,
        ).limit(1)
    )).scalar_one_or_none()
    task_dict = None
    if task_row:
        actual = task_row.actual_output or {}
        # Pull execution timeline from task_logs so the user can see
        # which tools the agent actually called (vs. just claimed in its
        # final JSON). TaskRunner writes ai_agent_turn / ai_supervisor_verdict
        # / ai_execution_* entries that map 1:1 to LLM rounds.
        from packages.core.services.task_service import get_task_logs
        logs = await get_task_logs(db, task_row.id)
        timeline = [
            {
                "type": log_entry.log_type,
                "content": (log_entry.content or "")[:1000],
                "ts": log_entry.created_at.isoformat() if log_entry.created_at else None,
            }
            for log_entry in reversed(logs)  # logs come desc; reverse to chronological
            if log_entry.log_type in CONVERSATION_LOG_TYPES
        ]
        task_dict = {
            "id": task_row.id,
            "title": task_row.title,
            "status": task_row.status,
            "description": task_row.description,
            # The agent's last-turn response. On failure, this carries the
            # canned "Sorry, the request failed" prefix + the actual provider
            # error detail (see llm_client._failure_message).
            "response": actual.get("response"),
            "turns_used": actual.get("turns_used"),
            "supervisor_verdict": (task_row.details or {}).get("ai_result", {}).get("supervisor_verdict"),
            "timeline": timeline,
        }

    # Find the AgentExecution for that task, if any.
    exec_row = None
    if task_row:
        exec_row = (await db.execute(
            select(AgentExecution).where(
                AgentExecution.task_id == task_row.id,
            ).order_by(AgentExecution.created_at.desc()).limit(1)
        )).scalar_one_or_none()

    return JobRunDetailResponse(
        run=_run_response(run),
        task=task_dict,
        agent_execution=_exec_response(exec_row) if exec_row else None,
    )


class RunNowResponse(BaseModel):
    job_id: str
    queued_at: str
    idempotency_key: str


@jobs_router.post("/{job_id}/run_now", response_model=RunNowResponse, status_code=202)
async def run_job_now(
    job_id: str,
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually dispatch a scheduled job immediately, without waiting
    for the next periodic tick. The dispatcher creates the JobRun row
    on the worker side (same code path as the cron tick), so polling
    /runs after this call will surface it.
    """
    from datetime import datetime, timezone
    from packages.core.models.base import generate_ulid
    from packages.core.tasks.scheduler_tasks import _dispatch_job_task

    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    await _legacy_compatible_workspace_gate(db, user, job.workspace_id, writable=True)
    if not job.enabled:
        raise HTTPException(409, "Job is disabled — enable it before running")
    if job.workspace_id:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == job.workspace_id,
                Workspace.entity_id == user.entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is not None and workspace.status != "active":
            raise HTTPException(409, "Workspace is not active - resume it before running automations")
        if workspace is not None and is_sandbox_workspace(workspace):
            raise HTTPException(
                409,
                "Workspace simulation cannot run ordinary automations",
            )

    now = datetime.now(timezone.utc)
    request_key = idempotency_key or generate_ulid()
    occurrence_key = f"manual:{request_key}"
    try:
        _dispatch_job_task.delay(
            job.id,
            now.isoformat(),
            manual=True,
            occurrence_key=occurrence_key,
        )
    except Exception as exc:
        raise HTTPException(503, f"Worker queue unreachable: {exc}") from exc

    return RunNowResponse(
        job_id=job.job_id,
        queued_at=now.isoformat(),
        idempotency_key=request_key,
    )


# ── Agent Execution Endpoints ──

@executions_router.get("", response_model=AgentExecutionListResponse)
async def list_executions(
    agent_id: str | None = Query(None),
    task_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    readable_ws = await readable_workspace_ids_for_user(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    execs, total = await list_agent_executions(
        db, user.entity_id, agent_id=agent_id, task_id=task_id,
        readable_workspace_ids=readable_ws, limit=limit, offset=offset,
    )
    return AgentExecutionListResponse(items=[_exec_response(e) for e in execs], total=total)


@executions_router.post("", response_model=AgentExecutionResponse, status_code=201)
async def create_execution(
    req: AgentExecutionCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _legacy_compatible_workspace_gate(db, user, req.workspace_id, writable=True)
    execution = await create_agent_execution(
        db, user.entity_id, req.agent_id,
        task_id=req.task_id, conversation_id=req.conversation_id,
        workspace_id=req.workspace_id, input_message=req.input_message,
        max_turns=req.max_turns,
    )
    return _exec_response(execution)


@executions_router.put("/{execution_id}", response_model=AgentExecutionResponse)
async def update_execution(
    execution_id: str,
    req: AgentExecutionUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.models.scheduler import AgentExecution
    execution_row = (await db.execute(
        select(AgentExecution).where(
            AgentExecution.id == execution_id,
            AgentExecution.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if not execution_row:
        raise HTTPException(404, "Agent execution not found")
    await _legacy_compatible_workspace_gate(db, user, execution_row.workspace_id, writable=True)
    execution = await update_agent_execution(
        db, execution_id, **req.model_dump(exclude_none=True),
    )
    if not execution:
        raise HTTPException(404, "Agent execution not found")
    return _exec_response(execution)
