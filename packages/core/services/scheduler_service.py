"""Scheduler service — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import DEFAULT_AGENT_MAX_TURNS
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution
from packages.core.models.workflow import WorkflowBinding


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
    search: str | None = None,
    status: str = "all",
    agent_id: str | None = None,
    include_workflows: bool = True,
    readable_workspace_ids: set[str] | None = None,
) -> tuple[list[ScheduledJob], int]:
    filters = _scheduled_job_filters(
        entity_id,
        workspace_id=workspace_id,
        search=search,
        agent_id=agent_id,
        include_workflows=include_workflows,
        readable_workspace_ids=readable_workspace_ids,
    )
    q = select(ScheduledJob).where(*filters)
    count_q = select(func.count()).select_from(ScheduledJob).where(*filters)

    if enabled_only:
        q = q.where(ScheduledJob.enabled == True)  # noqa: E712
        count_q = count_q.where(ScheduledJob.enabled == True)  # noqa: E712
    elif status == "enabled":
        q = q.where(ScheduledJob.enabled == True)  # noqa: E712
        count_q = count_q.where(ScheduledJob.enabled == True)  # noqa: E712
    elif status == "paused":
        q = q.where(ScheduledJob.enabled == False)  # noqa: E712
        count_q = count_q.where(ScheduledJob.enabled == False)  # noqa: E712
    elif status == "attention":
        q = q.where(ScheduledJob.consecutive_errors > 0)
        count_q = count_q.where(ScheduledJob.consecutive_errors > 0)

    q = q.order_by(
        ScheduledJob.created_at.desc(),
        ScheduledJob.id.desc(),
    ).limit(limit).offset(offset)

    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


async def summarize_scheduled_jobs(
    db: AsyncSession,
    entity_id: str,
    *,
    workspace_id: str | None = None,
    search: str | None = None,
    agent_id: str | None = None,
    include_workflows: bool = True,
    readable_workspace_ids: set[str] | None = None,
) -> dict[str, int]:
    """Return list-level totals without applying the selected status tab."""
    filters = _scheduled_job_filters(
        entity_id,
        workspace_id=workspace_id,
        search=search,
        agent_id=agent_id,
        include_workflows=include_workflows,
        readable_workspace_ids=readable_workspace_ids,
    )
    count_q = select(func.count()).select_from(ScheduledJob).where(*filters)
    enabled_q = count_q.where(ScheduledJob.enabled == True)  # noqa: E712
    attention_q = count_q.where(ScheduledJob.consecutive_errors > 0)
    total = (await db.execute(count_q)).scalar_one()
    enabled = (await db.execute(enabled_q)).scalar_one()
    attention = (await db.execute(attention_q)).scalar_one()
    return {
        "total": int(total or 0),
        "enabled": int(enabled or 0),
        "attention": int(attention or 0),
    }


def _scheduled_job_filters(
    entity_id: str,
    *,
    workspace_id: str | None = None,
    search: str | None = None,
    agent_id: str | None = None,
    include_workflows: bool = True,
    readable_workspace_ids: set[str] | None = None,
) -> list:
    filters = [ScheduledJob.entity_id == entity_id]
    if workspace_id:
        filters.append(ScheduledJob.workspace_id == workspace_id)
    if readable_workspace_ids is not None:
        filters.append(
            or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id.in_(readable_workspace_ids),
            )
        )
    if agent_id:
        filters.append(ScheduledJob.agent_id == agent_id)
    if not include_workflows:
        filters.append(
            or_(
                ScheduledJob.execution_type.is_(None),
                ScheduledJob.execution_type != "workflow",
            )
        )
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                ScheduledJob.id.ilike(pattern),
                ScheduledJob.name.ilike(pattern),
                ScheduledJob.job_id.ilike(pattern),
                ScheduledJob.job_type.ilike(pattern),
                ScheduledJob.payload_message.ilike(pattern),
            )
        )
    return filters


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


async def pause_workspace_automations(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
) -> dict[str, int]:
    """Pause every automation deployed into a workspace.

    Manual workflow bindings are workspace attachments, not automations, so
    they remain available. Scheduled jobs and inbound/event workflow bindings
    are the executable automation definitions shown in the Automations UI.
    """
    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
    )).scalars().all())
    bindings = list((await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.trigger_type != "manual",
        )
    )).scalars().all())

    now = datetime.now(timezone.utc)
    paused_jobs = 0
    for job in jobs:
        if job.enabled:
            job.enabled = False
            job.updated_at = now
            paused_jobs += 1

    paused_bindings = 0
    for binding in bindings:
        if binding.enabled or binding.status == "active":
            binding.enabled = False
            binding.status = "paused"
            binding.updated_at = now
            paused_bindings += 1

    await db.flush()
    if paused_jobs:
        from packages.core.services.realtime import broadcast_job_update

        await broadcast_job_update(entity_id, {
            "workspace_id": workspace_id,
            "enabled": False,
            "event": "workspace_paused",
        })
    return {
        "scheduled_jobs": paused_jobs,
        "workflow_bindings": paused_bindings,
    }


async def delete_workspace_automations(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
) -> dict[str, int]:
    """Delete every automation deployed into a workspace.

    Workflow definitions and manual workspace attachments are intentionally
    retained: deleting an automation must not destroy the reusable Flow.
    """
    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
    )).scalars().all())
    bindings = list((await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.trigger_type != "manual",
        )
    )).scalars().all())

    for job in jobs:
        await db.delete(job)
    for binding in bindings:
        await db.delete(binding)
    await db.flush()

    if jobs:
        from packages.core.services.realtime import broadcast_job_update

        await broadcast_job_update(entity_id, {
            "workspace_id": workspace_id,
            "event": "workspace_deleted",
        })
    return {
        "scheduled_jobs": len(jobs),
        "workflow_bindings": len(bindings),
    }


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
    idempotency_key: str | None = None,
) -> ScheduledJobRun:
    run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job_id,
        idempotency_key=idempotency_key,
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


async def claim_job_run(
    db: AsyncSession,
    job_id: str,
    status: str,
    *,
    idempotency_key: str,
    trigger_type: str | None = None,
    started_at: datetime | None = None,
) -> tuple[ScheduledJobRun, bool]:
    """Atomically claim one scheduler occurrence.

    Celery delivery is at-least-once.  The unique ``(job_id,
    idempotency_key)`` index therefore decides whether this worker owns the
    occurrence.  A different key always creates a different run, including
    multiple manual or scheduled runs on the same day.
    """

    clean_key = str(idempotency_key or "").strip()
    if not clean_key:
        raise ValueError("idempotency_key is required when claiming a job run")
    if len(clean_key) > 200:
        raise ValueError("idempotency_key must be at most 200 characters")

    run_id = generate_ulid()
    statement = (
        pg_insert(ScheduledJobRun)
        .values(
            id=run_id,
            job_id=job_id,
            idempotency_key=clean_key,
            status=status,
            trigger_type=trigger_type,
            started_at=started_at,
        )
        .on_conflict_do_nothing(
            index_elements=["job_id", "idempotency_key"],
        )
        .returning(ScheduledJobRun.id)
    )
    claimed_id = (await db.execute(statement)).scalar_one_or_none()
    if claimed_id:
        claimed = await db.get(ScheduledJobRun, claimed_id)
        if claimed is None:  # pragma: no cover - RETURNING row must be readable
            raise RuntimeError("Claimed scheduled job run could not be loaded")
        return claimed, True

    existing = (await db.execute(
        select(ScheduledJobRun).where(
            ScheduledJobRun.job_id == job_id,
            ScheduledJobRun.idempotency_key == clean_key,
        )
    )).scalar_one()
    return existing, False


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
    readable_workspace_ids: set[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentExecution], int]:
    q = select(AgentExecution).where(AgentExecution.entity_id == entity_id)
    count_q = select(func.count()).select_from(AgentExecution).where(AgentExecution.entity_id == entity_id)

    if readable_workspace_ids is not None:
        scope = or_(
            AgentExecution.workspace_id.is_(None),
            AgentExecution.workspace_id.in_(readable_workspace_ids),
        )
        q = q.where(scope)
        count_q = count_q.where(scope)

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
