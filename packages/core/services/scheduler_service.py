"""Scheduler service — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution


# ── Scheduled Jobs ──

async def create_scheduled_job(
    db: AsyncSession,
    entity_id: str,
    job_id: str,
    name: str,
    *,
    job_type: str = "cron",
    schedule_kind: str | None = None,
    cron_expr: str | None = None,
    every_seconds: float | None = None,
    run_at: str | None = None,
    timezone_str: str = "UTC",
    payload_message: str | None = None,
    agent_id: str | None = None,
    execution_type: str | None = None,
    execution_target: dict | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    default_delivery_mode: str | None = None,
    user_id: str | None = None,
) -> ScheduledJob:
    job = ScheduledJob(
        id=generate_ulid(),
        entity_id=entity_id,
        job_id=job_id,
        name=name,
        job_type=job_type,
        schedule_kind=schedule_kind,
        cron_expr=cron_expr,
        every_seconds=every_seconds,
        run_at=run_at,
        timezone=timezone_str,
        payload_message=payload_message,
        agent_id=agent_id,
        execution_type=execution_type,
        execution_target=execution_target or {},
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        default_delivery_mode=default_delivery_mode,
        user_id=user_id,
    )
    db.add(job)
    await db.flush()

    # Real-time push so Automations page refreshes without a poll.
    # Broadcast so every admin sees agent-initiated jobs too.
    from packages.core.services.realtime import (
        broadcast_job_update, push_job_update,
    )
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "schedule_kind": job.schedule_kind, "enabled": job.enabled,
        "event": "created",
    }
    if user_id:
        await push_job_update(user_id, summary)
    await broadcast_job_update(entity_id, summary)

    return job


async def list_scheduled_jobs(
    db: AsyncSession,
    entity_id: str,
    enabled_only: bool = False,
    workspace_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ScheduledJob], int]:
    q = select(ScheduledJob).where(ScheduledJob.entity_id == entity_id)
    count_q = select(func.count()).select_from(ScheduledJob).where(ScheduledJob.entity_id == entity_id)

    if enabled_only:
        q = q.where(ScheduledJob.enabled == True)  # noqa: E712
        count_q = count_q.where(ScheduledJob.enabled == True)  # noqa: E712
    if workspace_id:
        q = q.where(ScheduledJob.workspace_id == workspace_id)
        count_q = count_q.where(ScheduledJob.workspace_id == workspace_id)

    q = q.order_by(ScheduledJob.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


async def get_scheduled_job(
    db: AsyncSession,
    job_id_or_pk: str,
    entity_id: str,
) -> Optional[ScheduledJob]:
    # Try by primary key first
    result = await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.id == job_id_or_pk,
            ScheduledJob.entity_id == entity_id,
        )
    )
    job = result.scalar_one_or_none()
    if job:
        return job

    # Fall back to job_id
    result = await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.job_id == job_id_or_pk,
            ScheduledJob.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def update_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
    **kwargs,
) -> Optional[ScheduledJob]:
    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return None

    old_run_at = job.run_at

    for k, v in kwargs.items():
        if hasattr(job, k) and v is not None:
            setattr(job, k, v)

    # If run_at changed on a one-shot job, reset last_run_at so it re-triggers
    if job.schedule_kind == "at" and job.run_at != old_run_at:
        job.last_run_at = None
        job.last_status = None

    job.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(job)
    return job


async def delete_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
) -> bool:
    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return False
    # Capture identity before delete so we can push afterwards
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "event": "deleted",
    }
    owner_id = job.user_id
    await db.delete(job)
    await db.flush()

    from packages.core.services.realtime import (
        broadcast_job_update, push_job_update,
    )
    if owner_id:
        await push_job_update(owner_id, summary)
    await broadcast_job_update(entity_id, summary)
    return True


async def toggle_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
    enabled: bool,
) -> Optional[ScheduledJob]:
    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return None

    job.enabled = enabled
    job.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(job)

    from packages.core.services.realtime import (
        broadcast_job_update, push_job_update,
    )
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "enabled": job.enabled, "event": "updated",
    }
    if job.user_id:
        await push_job_update(job.user_id, summary)
    await broadcast_job_update(entity_id, summary)
    return job


# ── Job Runs ──

async def create_job_run(
    db: AsyncSession,
    job_id: str,
    status: str,
    *,
    trigger_type: str | None = None,
    result: dict | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> ScheduledJobRun:
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job_id,
        status=status,
        trigger_type=trigger_type,
        result=result,
        error=error,
        duration_ms=duration_ms,
        started_at=started_at,
        completed_at=completed_at,
    )
    db.add(run)
    await db.flush()
    return run


async def list_job_runs(
    db: AsyncSession,
    job_id: str,
    limit: int = 50,
) -> list[ScheduledJobRun]:
    result = await db.execute(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id)
        .order_by(ScheduledJobRun.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ── Agent Executions ──

async def create_agent_execution(
    db: AsyncSession,
    entity_id: str,
    agent_id: str,
    *,
    task_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    input_message: str | None = None,
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
) -> AgentExecution:
    execution = AgentExecution(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent_id,
        task_id=task_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        input_message=input_message,
        max_turns=max_turns,
        started_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    await db.flush()
    return execution


async def list_agent_executions(
    db: AsyncSession,
    entity_id: str,
    agent_id: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentExecution], int]:
    q = select(AgentExecution).where(AgentExecution.entity_id == entity_id)
    count_q = select(func.count()).select_from(AgentExecution).where(AgentExecution.entity_id == entity_id)

    if agent_id:
        q = q.where(AgentExecution.agent_id == agent_id)
        count_q = count_q.where(AgentExecution.agent_id == agent_id)
    if task_id:
        q = q.where(AgentExecution.task_id == task_id)
        count_q = count_q.where(AgentExecution.task_id == task_id)

    q = q.order_by(AgentExecution.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


async def update_agent_execution(
    db: AsyncSession,
    execution_id: str,
    **kwargs,
) -> Optional[AgentExecution]:
    result = await db.execute(
        select(AgentExecution).where(AgentExecution.id == execution_id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        return None

    for k, v in kwargs.items():
        if hasattr(execution, k) and v is not None:
            setattr(execution, k, v)

    await db.flush()
    await db.refresh(execution)
    return execution
