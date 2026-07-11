"""Scheduler endpoints — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.scheduler_service import (
    create_scheduled_job, list_scheduled_jobs, get_scheduled_job,
    update_scheduled_job, delete_scheduled_job, toggle_scheduled_job,
    create_job_run, list_job_runs,
    create_agent_execution, list_agent_executions, update_agent_execution,
)
from apps.api.deps import get_current_user

jobs_router = APIRouter(prefix="/api/v1/jobs", tags=["scheduled-jobs"])
executions_router = APIRouter(prefix="/api/v1/executions", tags=["agent-executions"])

logger = logging.getLogger(__name__)


# ── Schemas: Scheduled Jobs ──

class ScheduledJobResponse(BaseModel):
    id: str
    job_id: str
    entity_id: str | None = None
    workspace_id: str | None = None
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
    consecutive_errors: int = 0
    created_at: str | None = None
    updated_at: str | None = None


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


class ToggleRequest(BaseModel):
    enabled: bool


class JobRunResponse(BaseModel):
    id: str
    job_id: str
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

def _job_response(j) -> ScheduledJobResponse:
    return ScheduledJobResponse(
        id=j.id, job_id=j.job_id, entity_id=j.entity_id,
        workspace_id=j.workspace_id, name=j.name, job_type=j.job_type,
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
        enabled=j.enabled, delete_after_run=j.delete_after_run,
        last_run_at=j.last_run_at.isoformat() if j.last_run_at else None,
        last_status=j.last_status,
        consecutive_errors=j.consecutive_errors or 0,
        created_at=j.created_at.isoformat() if j.created_at else None,
        updated_at=j.updated_at.isoformat() if j.updated_at else None,
    )


def _run_response(r) -> JobRunResponse:
    return JobRunResponse(
        id=r.id, job_id=r.job_id, status=r.status,
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


# ── Scheduled Jobs Endpoints ──

@jobs_router.get("", response_model=ScheduledJobListResponse)
async def list_jobs(
    enabled_only: bool = Query(False),
    workspace_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    jobs, total = await list_scheduled_jobs(
        db, user.entity_id, enabled_only=enabled_only,
        workspace_id=workspace_id,
        limit=limit, offset=offset,
    )
    return ScheduledJobListResponse(items=[_job_response(j) for j in jobs], total=total)


@jobs_router.post("", response_model=ScheduledJobResponse, status_code=201)
async def create_job(
    req: ScheduledJobCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    )

    # Auto-generate a frozen execution skill in the background
    if req.payload_message and req.agent_id:
        try:
            from packages.core.tasks.ai_tasks import generate_job_skill
            generate_job_skill.delay(job.id, req.payload_message, req.name or "")
        except Exception as e:
            logger.warning("Failed to dispatch skill generation for job %s: %s", job.job_id, e)

    return _job_response(job)


@jobs_router.get("/{job_id}", response_model=ScheduledJobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    return _job_response(job)


@jobs_router.put("/{job_id}", response_model=ScheduledJobResponse)
async def update_job(
    job_id: str,
    req: ScheduledJobUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Capture the task message BEFORE the update so we only regenerate the
    # linked skill (a slow LLM call) when the message actually changed —
    # otherwise editing just the schedule (e.g. the time) would block the save
    # on skill regeneration, leaving the UI stuck on "saving".
    from packages.core.services.scheduler_service import get_scheduled_job
    _existing = await get_scheduled_job(db, job_id, user.entity_id)
    _old_message = _existing.payload_message if _existing else None

    job = await update_scheduled_job(
        db, job_id, user.entity_id, **req.model_dump(exclude_none=True),
    )
    if not job:
        raise HTTPException(404, "Scheduled job not found")

    # Regenerate the procedure only when the task message actually changed.
    message_changed = bool(req.payload_message) and req.payload_message != _old_message
    if message_changed and job.agent_id:
        skill_id = (job.execution_target or {}).get("skill_id")
        if skill_id:
            # Update the existing skill via LLM patch
            try:
                from packages.core.services.skill_generator import update_skill
                await update_skill(
                    skill_id, f"Updated task: {req.payload_message}",
                    user.entity_id, db,
                )
                # Refresh execution_script from updated skill
                from packages.core.services.skill_service import get_skill
                updated_skill = await get_skill(db, skill_id)
                if updated_skill:
                    job.execution_script = updated_skill.system_prompt
                    await db.flush()
            except Exception as e:
                logger.warning("Failed to update skill for job %s: %s", job_id, e)
        else:
            # No skill yet — generate in background
            try:
                from packages.core.tasks.ai_tasks import generate_job_skill
                generate_job_skill.delay(job.id, req.payload_message, job.name or "")
            except Exception as e:
                logger.warning("Failed to dispatch skill generation for job %s: %s", job_id, e)

    return _job_response(job)


@jobs_router.delete("/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    job = await toggle_scheduled_job(db, job_id, user.entity_id, req.enabled)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    return _job_response(job)


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
                "type": l.log_type,
                "content": (l.content or "")[:1000],
                "ts": l.created_at.isoformat() if l.created_at else None,
            }
            for l in reversed(logs)  # logs come desc; reverse to chronological
            if l.log_type and (
                l.log_type.startswith("ai_") or l.log_type == "comment"
            )
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


@jobs_router.post("/{job_id}/run_now", response_model=RunNowResponse, status_code=202)
async def run_job_now(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually dispatch a scheduled job immediately, without waiting
    for the next periodic tick. The dispatcher creates the JobRun row
    on the worker side (same code path as the cron tick), so polling
    /runs after this call will surface it.
    """
    from datetime import datetime, timezone
    from packages.core.tasks.scheduler_tasks import _dispatch_job_task

    job = await get_scheduled_job(db, job_id, user.entity_id)
    if not job:
        raise HTTPException(404, "Scheduled job not found")
    if not job.enabled:
        raise HTTPException(409, "Job is disabled — enable it before running")

    now = datetime.now(timezone.utc)
    try:
        _dispatch_job_task.delay(job.id, now.isoformat(), manual=True)
    except Exception as exc:
        raise HTTPException(503, f"Worker queue unreachable: {exc}") from exc

    return RunNowResponse(job_id=job.job_id, queued_at=now.isoformat())


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
    execs, total = await list_agent_executions(
        db, user.entity_id, agent_id=agent_id, task_id=task_id,
        limit=limit, offset=offset,
    )
    return AgentExecutionListResponse(items=[_exec_response(e) for e in execs], total=total)


@executions_router.post("", response_model=AgentExecutionResponse, status_code=201)
async def create_execution(
    req: AgentExecutionCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    execution = await update_agent_execution(
        db, execution_id, **req.model_dump(exclude_none=True),
    )
    if not execution:
        raise HTTPException(404, "Agent execution not found")
    return _exec_response(execution)
