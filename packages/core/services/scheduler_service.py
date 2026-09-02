"""Scheduler service — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    DEFAULT_AGENT_MAX_TURNS,
    ScheduledRunStatus,
)
from packages.core.constants.agents import is_master_agent
from packages.core.constants.task_actors import TaskActor
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace import Workspace
from packages.core.services.reusable_resource_locks import (
    is_reusable_resource_local_id,
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_references,
    reusable_resource_ids_from_payload,
)
from packages.core.services.product_growth import persist_scheduled_job
from packages.core.revisions import (
    SCHEDULED_JOB_CONTENT_REVISION_FIELDS,
    SCHEDULED_JOB_SCHEDULE_REVISION_FIELDS,
    assert_revision,
    bump_revision,
    content_patch_for,
)


# ── Scheduled Jobs ──

SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD = 3


def scheduled_job_workspace_scope(
    workspace_id: str | None,
    execution_target: dict | None,
) -> str | None:
    """Return one canonical Workspace scope or reject a shadow override."""

    job_workspace_id = str(workspace_id or "").strip() or None
    target_workspace_id = str(
        (execution_target or {}).get("workspace_id") or ""
    ).strip() or None
    if target_workspace_id and target_workspace_id != job_workspace_id:
        raise ValueError(
            "execution_target.workspace_id must match the scheduled job "
            "workspace_id"
        )
    return job_workspace_id


def project_scheduled_job_outcome(job: ScheduledJob, status: str) -> bool:
    """Project one terminal run onto its parent job.

    Returns ``True`` only when this failure crosses the auto-pause threshold.
    Callers must first establish that they own the terminal transition and that
    the run is the newest run before using this helper.
    """
    normalized_status = str(status or "").strip().lower()
    job.last_status = normalized_status
    if normalized_status == "error":
        job.consecutive_errors = (job.consecutive_errors or 0) + 1
        if (
            job.consecutive_errors >= SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD
            and bool(job.enabled)
        ):
            return True
        return False

    # Any non-error terminal outcome breaks the consecutive-failure streak.
    job.consecutive_errors = 0
    return False


async def lock_scheduled_job_and_run(
    db: AsyncSession,
    *,
    job_id: str | None,
    run_id: str | None,
) -> tuple[ScheduledJob | None, ScheduledJobRun | None]:
    """Lock a scheduler parent and occurrence in the global parent-first order."""

    run_job_id = None
    if run_id:
        run_job_id = await db.scalar(
            select(ScheduledJobRun.job_id).where(ScheduledJobRun.id == run_id)
        )
        if job_id and run_job_id and job_id != run_job_id:
            return None, None
    resolved_job_id = job_id or run_job_id
    if not resolved_job_id:
        return None, None

    job = (await db.execute(
        select(ScheduledJob)
        .where(ScheduledJob.job_id == resolved_job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if job is None:
        return None, None

    run = None
    if run_id:
        run = (await db.execute(
            select(ScheduledJobRun)
            .where(
                ScheduledJobRun.id == run_id,
                ScheduledJobRun.job_id == resolved_job_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    return job, run


async def reconcile_scheduled_job_run_projection(
    db: AsyncSession,
    job: ScheduledJob,
    *,
    finalized_run_id: str | None,
) -> bool:
    """Project ordered occurrence facts onto a locked scheduler parent.

    The error streak is the newest contiguous prefix of failed occurrences,
    independent of worker completion order. An unresolved newest occurrence
    keeps the previously established streak until its terminal fact arrives.
    """

    runs = list((await db.execute(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job.job_id)
        .order_by(
            ScheduledJobRun.started_at.is_(None),
            ScheduledJobRun.started_at.desc(),
            ScheduledJobRun.created_at.desc(),
            ScheduledJobRun.id.desc(),
        )
        .limit(SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD + 1)
    )).scalars())
    if not runs:
        return False

    newest = runs[0]
    newest_status = str(newest.status or "").strip().lower()
    job.last_status = newest_status
    if newest_status == ScheduledRunStatus.RUNNING.value:
        return False

    # A delete-after-run job remains enabled while its accepted child is
    # executing, then closes only after the occurrence becomes terminal.
    disable_after_terminal_run = bool(job.delete_after_run and job.enabled)

    previous_errors = int(job.consecutive_errors or 0)
    consecutive_errors = 0
    for run in runs:
        if str(run.status or "").strip().lower() != "error":
            break
        consecutive_errors += 1

    # Some legacy/dispatch failures have no ScheduledJobRun. Preserve that
    # established prefix when the newly finalized newest occurrence fails.
    prefix_has_authoritative_boundary = consecutive_errors < len(runs)
    if (
        newest_status == "error"
        and finalized_run_id == newest.id
        and not prefix_has_authoritative_boundary
    ):
        consecutive_errors = max(consecutive_errors, previous_errors + 1)
    job.consecutive_errors = consecutive_errors
    if (
        consecutive_errors >= SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD
        and bool(job.enabled)
    ):
        auto_paused = True
    else:
        auto_paused = False
    if disable_after_terminal_run or auto_paused:
        await ScheduledJobMutationFactory.apply_locked(
            db,
            job,
            {"enabled": False},
            causation_id=finalized_run_id,
        )
    return auto_paused


async def finalize_scheduled_workflow_run(
    db: AsyncSession,
    workflow_run: object,
) -> bool:
    """Settle the scheduler occurrence owned by a terminal Workflow run."""
    if str(getattr(workflow_run, "trigger_source", "") or "") != "schedule":
        return False
    trigger_data = getattr(workflow_run, "trigger_data", None)
    if not isinstance(trigger_data, dict):
        return False
    scheduled_run_id = str(trigger_data.get("scheduled_run_id") or "").strip()
    scheduled_job_id = str(trigger_data.get("scheduled_job_id") or "").strip()
    if not scheduled_run_id or not scheduled_job_id:
        return False

    workflow_status = str(getattr(workflow_run, "status", "") or "").strip()
    scheduler_status = {
        "completed": ScheduledRunStatus.COMPLETED,
        "failed": ScheduledRunStatus.ERROR,
        "cancelled": ScheduledRunStatus.CANCELLED,
    }.get(workflow_status)
    if scheduler_status is None:
        return False

    job, scheduled_run = await lock_scheduled_job_and_run(
        db,
        job_id=scheduled_job_id,
        run_id=scheduled_run_id,
    )
    if (
        job is None
        or scheduled_run is None
        or scheduled_run.status != ScheduledRunStatus.RUNNING.value
    ):
        return False

    error = str(getattr(workflow_run, "error", "") or "").strip()
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledRunOutcome,
        apply_scheduled_run_outcome,
    )

    apply_scheduled_run_outcome(
        scheduled_run,
        ScheduledRunOutcome.from_execution(
            status=scheduler_status,
            error=error if scheduler_status is ScheduledRunStatus.ERROR else None,
            result={
                "workflow_run_id": str(getattr(workflow_run, "id", "") or ""),
                "status": workflow_status,
                **({"error": error} if error else {}),
            },
        ),
    )

    from packages.core.ledger.adapters import record_automation_run_finished

    await record_automation_run_finished(
        db,
        job,
        run_id=scheduled_run.id,
        status=scheduler_status.value,
    )
    auto_paused = await reconcile_scheduled_job_run_projection(
        db,
        job,
        finalized_run_id=scheduled_run.id,
    )
    if auto_paused:
        await notify_scheduled_job_auto_paused(
            db,
            job,
            failure_key=scheduled_run.id,
        )
    return True


async def notify_scheduled_job_auto_paused(
    db: AsyncSession,
    job: ScheduledJob,
    *,
    failure_key: str,
) -> None:
    """Notify the owning user once when repeated failures pause a job."""
    entity_id = str(getattr(job, "entity_id", None) or "").strip()
    user_id = str(getattr(job, "user_id", None) or "").strip()
    if not entity_id or not user_id:
        return

    from packages.core.services.notification_service import create_notification

    await create_notification(
        db,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=getattr(job, "workspace_id", None),
        type="automation_paused",
        title="Automation paused after repeated failures",
        body=(
            f'"{job.name or job.job_id}" was paused after '
            f"{job.consecutive_errors} consecutive failures. Review the latest "
            "run, fix the cause, then resume it."
        ),
        link=f"/jobs?job={job.job_id}",
        meta={
            "kind": "automation_auto_paused",
            "scheduled_job_id": job.job_id,
            "consecutive_errors": job.consecutive_errors,
        },
        idempotency_key=(
            f"scheduled-job:auto-paused:{job.id}:{failure_key}"
        )[:255],
    )


async def _lock_scheduled_job_resource_references(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str | None,
    execution_target: dict | None,
) -> None:
    """Protect local ULID refs; preserve legacy opaque scheduler labels."""
    agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(
        execution_target or {}
    )
    if (
        agent_id
        and is_reusable_resource_local_id(agent_id)
        and not is_master_agent(agent_id)
    ):
        agent_ids.add(str(agent_id))
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        workflow_ids=workflow_ids,
    )


async def _lock_workspace_scheduling_admission(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    require_workspace: bool = False,
) -> bool:
    """Lock the Workspace parent before admitting an enabled Job mutation."""

    if not workspace_id:
        return True
    workspace = (await db.execute(
        select(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if workspace is None:
        known_workspace_id = (await db.execute(
            select(Workspace.id)
            .where(Workspace.id == workspace_id)
            .limit(1)
        )).scalar_one_or_none()
        if require_workspace or known_workspace_id is not None:
            raise ValueError("Workspace not found")
        # Legacy API rows historically accepted opaque workspace labels. The
        # strict Agent Runtime callers opt out of this fallback. A real
        # foreign/deleted Workspace always fails closed.
        return True
    return workspace.status == "active"


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
    execution_script: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    default_delivery_mode: str | None = None,
    user_id: str | None = None,
    require_workspace: bool = True,
) -> ScheduledJob:
    scheduled_job_workspace_scope(workspace_id, execution_target)
    # Workspace deletion and reusable-resource purge both acquire this fence
    # before their Workspace/resource rows. Keep Workspace-scoped creation in
    # the same global order so a local resource reference cannot invert the
    # locks. Entity-only jobs acquire the fence later only when they carry a
    # reusable-resource reference, avoiding needless tenant-wide contention.
    if workspace_id:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace_active = await _lock_workspace_scheduling_admission(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        require_workspace=require_workspace,
    )
    await _lock_scheduled_job_resource_references(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
        execution_target=execution_target,
    )
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
        execution_script=execution_script,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        default_delivery_mode=default_delivery_mode,
        user_id=user_id,
        enabled=workspace_active,
    )
    if payload_message and agent_id and workspace_active:
        job.skill_generation_revision = int(job.revision or 1)
        job.skill_generation_next_attempt_at = datetime.now(timezone.utc)
    await persist_scheduled_job(db, job)

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
        await push_job_update(user_id, summary, entity_id=entity_id)
    await broadcast_job_update(entity_id, summary)

    return job


async def defer_scheduled_job_skill_generation(
    db: AsyncSession,
    *,
    job_id: str,
    revision: int,
    next_attempt_at: datetime,
) -> bool:
    """Move only the still-current durable Skill-generation intent."""

    result = await db.execute(
        update(ScheduledJob)
        .where(
            ScheduledJob.id == job_id,
            ScheduledJob.enabled.is_(True),
            ScheduledJob.skill_generation_revision == int(revision),
        )
        .values(skill_generation_next_attempt_at=next_attempt_at)
    )
    return bool(result.rowcount)


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

    inactive_workspace = exists(
        select(Workspace.id).where(
            Workspace.id == ScheduledJob.workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.status != "active",
            Workspace.deleted_at.is_(None),
        )
    )
    effective_enabled = ScheduledJob.enabled == True  # noqa: E712
    effective_enabled = effective_enabled & ~inactive_workspace

    if enabled_only:
        q = q.where(effective_enabled)
        count_q = count_q.where(effective_enabled)
    elif status == "enabled":
        q = q.where(effective_enabled)
        count_q = count_q.where(effective_enabled)
    elif status == "paused":
        effective_paused = or_(ScheduledJob.enabled == False, inactive_workspace)  # noqa: E712
        q = q.where(effective_paused)
        count_q = count_q.where(effective_paused)
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
    inactive_workspace = exists(
        select(Workspace.id).where(
            Workspace.id == ScheduledJob.workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.status != "active",
            Workspace.deleted_at.is_(None),
        )
    )
    enabled_q = count_q.where(
        ScheduledJob.enabled == True,  # noqa: E712
        ~inactive_workspace,
    )
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
    # A soft-deleted Workspace stops new automation immediately. Keep legacy
    # opaque labels (there is no matching Workspace row) and entity-level jobs
    # visible, but hide jobs tied to a real deleted Workspace.
    deleted_workspace = exists(
        select(Workspace.id).where(
            Workspace.id == ScheduledJob.workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_not(None),
        )
    )
    filters.append(~deleted_workspace)
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
    *,
    for_update: bool = False,
) -> Optional[ScheduledJob]:
    # Try by primary key first
    primary_key_query = (
        select(ScheduledJob).where(
            ScheduledJob.id == job_id_or_pk,
            ScheduledJob.entity_id == entity_id,
        )
    )
    if for_update:
        primary_key_query = primary_key_query.with_for_update()
    result = await db.execute(primary_key_query)
    job = result.scalar_one_or_none()
    if job:
        return job

    # Fall back to job_id
    job_id_query = (
        select(ScheduledJob).where(
            ScheduledJob.job_id == job_id_or_pk,
            ScheduledJob.entity_id == entity_id,
        )
    )
    if for_update:
        job_id_query = job_id_query.with_for_update()
    result = await db.execute(job_id_query)
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class ScheduledJobMutationResult:
    """One factory-applied ScheduledJob configuration mutation."""

    job: ScheduledJob
    applied: dict[str, Any]
    content_patch: dict[str, Any]


class ScheduledJobMutationFactory:
    """Own the lock, clock, revision, and audit contract for Job config."""

    _RESOURCE_TOPOLOGY_FIELDS = frozenset({
        "agent_id",
        "entity_id",
        "execution_target",
    })

    @classmethod
    async def apply(
        cls,
        db: AsyncSession,
        job: ScheduledJob,
        updates: dict[str, Any],
        *,
        expected_revision: int | None = None,
        changed_by_kind: str = TaskActor.SYSTEM.value,
        changed_by_id: str | None = None,
        causation_id: str | None = None,
    ) -> ScheduledJobMutationResult | None:
        current_entity_id = str(job.entity_id or "").strip()
        next_entity_id = str(updates.get("entity_id", job.entity_id) or "").strip()
        topology_change_requested = bool(
            cls._RESOURCE_TOPOLOGY_FIELDS.intersection(updates)
        )
        if topology_change_requested:
            # Purge takes the same entity fence before definition/Job rows.
            # A rare cross-entity reconciliation fences both sides in stable
            # order so two opposing moves cannot deadlock.
            for entity_id in sorted({current_entity_id, next_entity_id} - {""}):
                await lock_reusable_resource_lifecycle(db, entity_id=entity_id)

        locked = (await db.execute(
            select(ScheduledJob)
            .where(
                ScheduledJob.id == job.id,
                ScheduledJob.entity_id == job.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if locked is None:
            return None

        valid_updates = {
            key: value
            for key, value in updates.items()
            if hasattr(locked, key)
        }
        if topology_change_requested:
            next_execution_target = valid_updates.get(
                "execution_target",
                locked.execution_target,
            )
            agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(
                next_execution_target
            )
            next_agent_id = valid_updates.get("agent_id", locked.agent_id)
            if (
                next_agent_id
                and is_reusable_resource_local_id(next_agent_id)
                and not is_master_agent(next_agent_id)
            ):
                agent_ids.add(str(next_agent_id))
            await lock_reusable_resource_references(
                db,
                entity_id=next_entity_id or str(locked.entity_id or ""),
                agent_ids=agent_ids,
                skill_ids=skill_ids,
                workflow_ids=workflow_ids,
            )

        return await cls._apply_locked_mutation(
            db,
            locked,
            valid_updates,
            expected_revision=expected_revision,
            changed_by_kind=changed_by_kind,
            changed_by_id=changed_by_id,
            causation_id=causation_id,
        )

    @classmethod
    async def apply_locked(
        cls,
        db: AsyncSession,
        job: ScheduledJob,
        updates: dict[str, Any],
        *,
        expected_revision: int | None = None,
        changed_by_kind: str = TaskActor.SYSTEM.value,
        changed_by_id: str | None = None,
        causation_id: str | None = None,
    ) -> ScheduledJobMutationResult:
        """Mutate a Job row the caller already locked parent-first.

        Resource-topology changes must use :meth:`apply`, which acquires the
        entity lifecycle fence before the Job row.  Keeping that distinction
        explicit prevents callers such as Workspace pause and run projection
        from trying to acquire the broad lifecycle lock after a parent row.
        """

        topology_fields = cls._RESOURCE_TOPOLOGY_FIELDS.intersection(updates)
        if topology_fields:
            fields = ", ".join(sorted(topology_fields))
            raise ValueError(
                f"Locked ScheduledJob mutation cannot change topology: {fields}"
            )
        valid_updates = {
            key: value
            for key, value in updates.items()
            if hasattr(job, key)
        }
        return await cls._apply_locked_mutation(
            db,
            job,
            valid_updates,
            expected_revision=expected_revision,
            changed_by_kind=changed_by_kind,
            changed_by_id=changed_by_id,
            causation_id=causation_id,
        )

    @classmethod
    async def _apply_locked_mutation(
        cls,
        db: AsyncSession,
        locked: ScheduledJob,
        valid_updates: dict[str, Any],
        *,
        expected_revision: int | None,
        changed_by_kind: str,
        changed_by_id: str | None,
        causation_id: str | None,
    ) -> ScheduledJobMutationResult:
        await assert_revision(locked, expected_revision)

        applied = {
            key: value
            for key, value in valid_updates.items()
            if getattr(locked, key) != value
        }
        content_patch = content_patch_for(
            locked,
            applied,
            SCHEDULED_JOB_CONTENT_REVISION_FIELDS,
            include_none=True,
        )
        schedule_changed = bool(
            SCHEDULED_JOB_SCHEDULE_REVISION_FIELDS.intersection(content_patch)
        )
        release_dispatch_clock = schedule_changed
        if (
            content_patch
            and not schedule_changed
            and locked.last_run_at is not None
            and locked.last_status == "dispatched"
        ):
            active_run = (await db.execute(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.job_id == locked.job_id,
                    ScheduledJobRun.started_at == locked.last_run_at,
                )
                .order_by(ScheduledJobRun.id.desc())
                .limit(1)
                .with_for_update()
            )).scalar_one_or_none()
            if active_run is None:
                release_dispatch_clock = True
            else:
                from packages.core.constants.execution import (
                    ScheduledChildExecutionState,
                )
                from packages.core.services.scheduled_run_lifecycle import (
                    scheduled_child_execution_state,
                )

                release_dispatch_clock = bool(
                    active_run.status != ScheduledRunStatus.RUNNING.value
                    or scheduled_child_execution_state(active_run)
                    is not ScheduledChildExecutionState.ADMITTED
                )

        resuming = (
            "enabled" in applied
            and bool(applied["enabled"])
            and not bool(locked.enabled)
        )
        next_delete_after_run = bool(
            valid_updates.get("delete_after_run", locked.delete_after_run)
        )
        if resuming and next_delete_after_run and locked.last_run_at is not None:
            running_exists = await db.scalar(
                select(exists().where(
                    ScheduledJobRun.job_id == locked.job_id,
                    ScheduledJobRun.status == ScheduledRunStatus.RUNNING.value,
                ))
            )
            if not running_exists:
                release_dispatch_clock = True

        mutation_now = datetime.now(timezone.utc)
        for key, value in applied.items():
            setattr(locked, key, value)
        if resuming:
            locked.consecutive_errors = 0
        if release_dispatch_clock:
            locked.last_run_at = None
            locked.last_status = None
        if applied:
            locked.updated_at = mutation_now
        if content_patch:
            await bump_revision(
                db,
                locked,
                patch=content_patch,
                changed_by_kind=changed_by_kind,
                changed_by_id=changed_by_id,
                causation_id=causation_id,
            )
        generation_inputs_changed = bool(
            {"payload_message", "agent_id"}.intersection(content_patch)
        )
        generation_was_pending = (
            getattr(locked, "skill_generation_revision", None) is not None
        )
        generation_retry_requested = bool(
            resuming
            and getattr(locked, "skill_generation_last_error", None)
        )
        if (
            generation_inputs_changed
            or generation_was_pending
            or generation_retry_requested
        ):
            if locked.payload_message and locked.agent_id:
                if generation_inputs_changed or generation_retry_requested:
                    locked.skill_generation_attempts = 0
                    locked.skill_generation_last_error = None
                locked.skill_generation_revision = int(locked.revision or 1)
                locked.skill_generation_next_attempt_at = (
                    mutation_now if locked.enabled else None
                )
            else:
                locked.skill_generation_revision = None
                locked.skill_generation_next_attempt_at = None
                locked.skill_generation_attempts = 0
                locked.skill_generation_last_error = None
        if (
            schedule_changed
            or resuming
            or release_dispatch_clock
            or "enabled" in applied
        ):
            from packages.core.schedule_clock import (
                refresh_scheduled_job_next_run_at,
            )

            refresh_scheduled_job_next_run_at(
                locked,
                now=mutation_now,
                inclusive=True,
            )
        await db.flush()
        return ScheduledJobMutationResult(
            job=locked,
            applied=applied,
            content_patch=content_patch,
        )


async def mutate_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
    *,
    changed_by_kind: str = TaskActor.SYSTEM.value,
    changed_by_id: str | None = None,
    **kwargs,
) -> ScheduledJobMutationResult | None:
    """Apply one public Job update and retain its authoritative diff."""

    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return None
    # Public update semantics historically treat ``None`` as omitted. Internal
    # schedule reconcilers call the factory directly when clearing a field is
    # the intended mutation.
    updates = {key: value for key, value in kwargs.items() if value is not None}
    return await ScheduledJobMutationFactory.apply(
        db,
        job,
        updates,
        changed_by_kind=changed_by_kind,
        changed_by_id=changed_by_id,
    )


async def update_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
    *,
    changed_by_kind: str = TaskActor.SYSTEM.value,
    changed_by_id: str | None = None,
    **kwargs,
) -> Optional[ScheduledJob]:
    result = await mutate_scheduled_job(
        db,
        job_pk,
        entity_id,
        changed_by_kind=changed_by_kind,
        changed_by_id=changed_by_id,
        **kwargs,
    )
    if result is None:
        return None
    await db.refresh(result.job)
    return result.job


async def delete_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
) -> bool:
    job = await get_scheduled_job(db, job_pk, entity_id, for_update=True)
    if not job:
        return False
    # Capture identity before delete so we can push afterwards
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "event": "deleted",
    }
    owner_id = job.user_id
    await db.execute(
        delete(ScheduledJobRun).where(ScheduledJobRun.job_id == job.job_id)
    )
    await db.delete(job)
    await db.flush()

    from packages.core.services.realtime import (
        broadcast_job_update, push_job_update,
    )
    if owner_id:
        await push_job_update(owner_id, summary, entity_id=entity_id)
    await broadcast_job_update(entity_id, summary)
    return True


async def toggle_scheduled_job(
    db: AsyncSession,
    job_pk: str,
    entity_id: str,
    enabled: bool,
    *,
    changed_by_kind: str = TaskActor.SYSTEM.value,
    changed_by_id: str | None = None,
    require_workspace: bool = False,
) -> Optional[ScheduledJob]:
    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return None
    if enabled and not await _lock_workspace_scheduling_admission(
        db,
        entity_id=entity_id,
        workspace_id=job.workspace_id,
        require_workspace=require_workspace,
    ):
        raise ValueError(
            "Workspace is not active - resume it before enabling automations"
        )
    result = await ScheduledJobMutationFactory.apply(
        db,
        job,
        {"enabled": enabled},
        changed_by_kind=changed_by_kind,
        changed_by_id=changed_by_id,
    )
    if result is None:
        return None
    job = result.job
    await db.refresh(job)

    from packages.core.services.realtime import (
        broadcast_job_update, push_job_update,
    )
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "enabled": job.enabled, "event": "updated",
    }
    if job.user_id:
        await push_job_update(job.user_id, summary, entity_id=entity_id)
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
        select(ScheduledJob)
        .where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
        .order_by(ScheduledJob.id)
        .with_for_update()
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
            result = await ScheduledJobMutationFactory.apply_locked(
                db,
                job,
                {"enabled": False},
            )
            if result is not None and result.applied:
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
        select(ScheduledJob)
        .where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
        .order_by(ScheduledJob.id)
        .with_for_update()
    )).scalars().all())
    job_ids = [job.job_id for job in jobs]
    bindings = list((await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.trigger_type != "manual",
        )
    )).scalars().all())

    if job_ids:
        await db.execute(
            delete(ScheduledJobRun).where(ScheduledJobRun.job_id.in_(job_ids))
        )
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
    if is_reusable_resource_local_id(agent_id) and not is_master_agent(agent_id):
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            agent_ids=(agent_id,),
        )
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

    next_agent_id = kwargs.get("agent_id")
    if (
        next_agent_id
        and next_agent_id != execution.agent_id
        and execution.entity_id
        and is_reusable_resource_local_id(next_agent_id)
        and not is_master_agent(next_agent_id)
    ):
        await lock_reusable_resource_references(
            db,
            entity_id=execution.entity_id,
            agent_ids=(next_agent_id,),
        )

    for k, v in kwargs.items():
        if hasattr(execution, k) and v is not None:
            setattr(execution, k, v)

    await db.flush()
    await db.refresh(execution)
    return execution
