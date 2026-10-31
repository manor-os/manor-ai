"""Scheduler service — scheduled jobs, job runs, agent executions."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    DEFAULT_AGENT_MAX_TURNS,
    ScheduledRunStatus,
)
from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
from packages.core.constants.task_actors import TaskActor
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.collaborator_scope import require_collaborator_references
from packages.core.services.reusable_resource_locks import (
    is_reusable_resource_local_id,
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_references,
    reusable_resource_ids_from_payload,
)
from packages.core.services.product_growth import persist_scheduled_job
from packages.core.services.workspace_autonomy import (
    scheduled_job_is_independent,
    workspace_allows_configured_work,
)
from packages.core.revisions import (
    SCHEDULED_JOB_CONTENT_REVISION_FIELDS,
    SCHEDULED_JOB_SCHEDULE_REVISION_FIELDS,
    assert_revision,
    bump_revision,
    content_patch_for,
)


# ── Scheduled Jobs ──

SCHEDULED_JOB_AUTO_PAUSE_ERROR_THRESHOLD = 3
MANAGED_AGENT_INITIATIVE_MUTATION_CODE = "MANAGED_AGENT_INITIATIVE"


@dataclass(frozen=True)
class RuntimeScheduledJobAgentRequest:
    """Trusted caller identity plus an optional model-selected Agent id."""

    requested_agent_id: str = ""
    caller_agent_id: str = ""
    caller_agent_subscription_id: str | None = None


@dataclass(frozen=True)
class RuntimeScheduledJobAgentSelection:
    """Exact Agent deployment persisted for one Runtime-created Job."""

    agent_id: str
    agent_subscription_id: str | None
    source: str


async def resolve_runtime_scheduled_job_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    request: RuntimeScheduledJobAgentRequest,
) -> RuntimeScheduledJobAgentSelection:
    """Resolve and lock one executable Agent deployment.

    Model input may select only an active entity Agent with one active
    deployment in the Job scope. Invalid input falls back to the trusted
    caller. Manor can only be selected when Manor is already the caller.
    """

    caller_agent_id = str(request.caller_agent_id or "").strip() or MANOR_AGENT_ID
    if is_master_agent(caller_agent_id):
        caller_agent_id = MANOR_AGENT_ID
    requested_agent_id = str(request.requested_agent_id or "").strip()
    caller_subscription_id = str(
        request.caller_agent_subscription_id or ""
    ).strip() or None

    custom_agent_ids = {
        agent_id
        for agent_id in (caller_agent_id, requested_agent_id)
        if agent_id and not is_master_agent(agent_id)
    }
    active_agent_ids: set[str] = set()
    scoped_subscriptions: list[AgentSubscription] = []
    if custom_agent_ids:
        active_agent_ids = set((await db.execute(
            select(Agent.id)
            .where(
                Agent.id.in_(sorted(custom_agent_ids)),
                Agent.entity_id == entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
            .order_by(Agent.id.asc())
            .with_for_update()
        )).scalars().all())

        subscription_filters = [
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.agent_id.in_(sorted(custom_agent_ids)),
            AgentSubscription.status == "active",
        ]
        if workspace_id or user_id:
            if workspace_id:
                subscription_filters.append(
                    AgentSubscription.workspace_id == workspace_id
                )
            else:
                subscription_filters.extend((
                    AgentSubscription.workspace_id.is_(None),
                    AgentSubscription.owner_user_id == user_id,
                ))
            scoped_subscriptions = list((await db.execute(
                select(AgentSubscription)
                .where(*subscription_filters)
                .order_by(
                    AgentSubscription.agent_id.asc(),
                    AgentSubscription.id.asc(),
                )
                .with_for_update()
            )).scalars().all())

    subscriptions_by_agent: dict[str, list[AgentSubscription]] = {}
    for subscription in scoped_subscriptions:
        subscriptions_by_agent.setdefault(subscription.agent_id, []).append(subscription)

    def custom_selection(
        agent_id: str,
        *,
        exact_subscription_id: str | None = None,
        source: str,
    ) -> RuntimeScheduledJobAgentSelection | None:
        if agent_id not in active_agent_ids:
            return None
        candidates = subscriptions_by_agent.get(agent_id, [])
        if exact_subscription_id:
            candidates = [
                row for row in candidates if row.id == exact_subscription_id
            ]
        if len(candidates) != 1:
            return None
        return RuntimeScheduledJobAgentSelection(
            agent_id=agent_id,
            agent_subscription_id=candidates[0].id,
            source=source,
        )

    def caller_selection(source: str) -> RuntimeScheduledJobAgentSelection | None:
        if is_master_agent(caller_agent_id):
            return RuntimeScheduledJobAgentSelection(
                agent_id=MANOR_AGENT_ID,
                agent_subscription_id=None,
                source=source,
            )
        return custom_selection(
            caller_agent_id,
            exact_subscription_id=caller_subscription_id,
            source=source,
        )

    if not requested_agent_id:
        selected = caller_selection("caller")
    elif is_master_agent(requested_agent_id):
        selected = (
            RuntimeScheduledJobAgentSelection(
                agent_id=MANOR_AGENT_ID,
                agent_subscription_id=None,
                source="requested",
            )
            if is_master_agent(caller_agent_id)
            else caller_selection("caller_fallback")
        )
    else:
        requested_subscription_id = (
            caller_subscription_id
            if requested_agent_id == caller_agent_id
            else None
        )
        selected = custom_selection(
            requested_agent_id,
            exact_subscription_id=requested_subscription_id,
            source="requested",
        ) or caller_selection("caller_fallback")

    if selected is None:
        raise ValueError("Calling Agent has no active deployment in this scope")
    return selected


class ManagedAgentInitiativeDriverError(ValueError):
    """A generic Scheduler mutation targeted an Initiative-owned driver."""

    code = MANAGED_AGENT_INITIATIVE_MUTATION_CODE


def is_agent_initiative_driver(job: object) -> bool:
    """Return whether a ScheduledJob is reserved as an Initiative driver."""

    target = getattr(job, "execution_target", None)
    target = target if isinstance(target, dict) else {}
    return bool(
        getattr(job, "agent_initiative_id", None)
        or str(getattr(job, "execution_type", "") or "") == "agent_initiative"
        or str(target.get("managed_by") or "") == "agent_initiative"
    )


def _assert_scheduled_job_mutation_authority(
    job: object,
    *,
    allow_agent_initiative_driver: bool,
) -> None:
    if is_agent_initiative_driver(job) and not allow_agent_initiative_driver:
        raise ManagedAgentInitiativeDriverError(
            "Agent Initiative drivers can only be changed through the "
            "Initiative lifecycle"
        )


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

    # A delete-after-run job remains enabled while its accepted scheduled
    # child is executing, then closes only after that occurrence is terminal.
    # A manual run-now is an independent occurrence and must not consume the
    # still-pending one-shot clock.
    disable_after_terminal_run = bool(
        job.delete_after_run
        and job.enabled
        and str(newest.trigger_type or "") != "manual"
    )

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
            allow_agent_initiative_driver=True,
        )
    return auto_paused


async def finalize_scheduled_workflow_run(
    db: AsyncSession,
    workflow_run: object,
) -> bool:
    """Settle the scheduler occurrence owned by a terminal Workflow run.

    A person's Run now / Assess now is a manual occurrence of the same job; when
    it pauses (for an approval) and finishes later, it must settle too, or the
    occurrence stays running and blocks the Workspace's next upgrade or run.
    """
    if str(getattr(workflow_run, "trigger_source", "") or "") not in {"schedule", "manual"}:
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
    # Only the run this occurrence dispatched may settle it.
    dispatched_child = (scheduled_run.result or {}).get("scheduled_child_id") if isinstance(scheduled_run.result, dict) else None
    if dispatched_child and dispatched_child != str(getattr(workflow_run, "id", "") or ""):
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


def scheduled_job_references(
    *,
    agent_id: str | None,
    execution_target: dict | None,
    conversation_id: str | None,
) -> dict[str, set]:
    """Every resource a Job names, for a collaborator's Workspace check.

    Any string counts, ULID-shaped or not: an unresolvable reference is
    outside the Workspace, so a non-id value cannot route around it.
    """
    from packages.core.services.collaborator_scope import payload_references

    target = execution_target or {}
    refs = payload_references(target)
    return {
        "agent": {*refs["agent"], agent_id},
        "skill": refs["skill"],
        "workflow": refs["workflow"],
        "agent_subscription": {target.get("agent_subscription_id"), target.get("subscription_id")},
        "workflow_binding": {target.get("binding_id")},
        "goal": {target.get("goal_id")},
        "stat": {target.get("stat_id")},
        "conversation": {conversation_id, target.get("conversation_id")},
    }


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
    execution_type: str | None = None,
    execution_target: dict | None = None,
    require_workspace: bool = False,
) -> bool:
    """Lock the Workspace parent before admitting an enabled Job mutation.

    Independent Flows and stat collectors have their own durable ``enabled``
    switch.  Workspace pause controls the Strategist/autonomous-agent runtime,
    while these configured automations remain available in a paused Workspace.
    """

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
    if await scheduled_execution_is_workspace_independent(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        execution_type=execution_type,
        execution_target=execution_target,
    ):
        return workspace_allows_configured_work(workspace)
    return workspace.status == "active"


async def scheduled_execution_is_workspace_independent(
    db: AsyncSession,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    execution_type: str | None,
    execution_target: dict | None,
) -> bool:
    """Classify an execution payload before a ScheduledJob row exists."""
    if not scheduled_job_is_independent(execution_type):
        return False
    if execution_type != "workspace_stat_collection":
        return True
    stat_id = str((execution_target or {}).get("stat_id") or "").strip()
    if not stat_id or not workspace_id or not entity_id:
        return True
    from packages.core.models.goal import Goal

    goal_id = await db.scalar(select(Goal.id).where(
        Goal.entity_id == entity_id,
        Goal.workspace_id == workspace_id,
        Goal.stat_id == stat_id,
    ).limit(1))
    return goal_id is None


async def scheduled_job_is_workspace_independent(
    db: AsyncSession,
    job: ScheduledJob,
) -> bool:
    """Whether this job keeps its own enabled switch while Workspace is paused.

    A Stats collector is normally independent, except when it backs a
    Workspace Goal.  Goal-owned collectors remain part of the Goal runtime
    so the pause/resume projection keeps its existing ownership contract.
    """
    return await scheduled_execution_is_workspace_independent(
        db,
        entity_id=job.entity_id,
        workspace_id=job.workspace_id,
        execution_type=job.execution_type,
        execution_target=job.execution_target,
    )


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
    require_actor_workspace_write: bool = False,
    publish_realtime: bool = True,
    runtime_agent_request: RuntimeScheduledJobAgentRequest | None = None,
    stop_after: datetime | str | None = None,
    owner_project_id: str | None = None,
    idempotency_key: str | None = None,
) -> ScheduledJob:
    scheduled_job_workspace_scope(workspace_id, execution_target)
    if isinstance(stop_after, str):
        stop_after = datetime.fromisoformat(stop_after)
    if stop_after is not None:
        if not isinstance(stop_after, datetime) or stop_after.tzinfo is None:
            raise ValueError("stop_after must be a timezone-aware timestamp")
        stop_after = stop_after.astimezone(timezone.utc)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not workspace_id or not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key requires a Workspace and 1 to 200 characters")
    # Workspace deletion and reusable-resource purge both acquire this fence
    # before their Workspace/resource rows. Keep Workspace-scoped creation in
    # the same global order so a local resource reference cannot invert the
    # locks. Entity-only jobs acquire the fence later only when they carry a
    # reusable-resource reference, avoiding needless tenant-wide contention.
    if workspace_id or runtime_agent_request is not None:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace_active = await _lock_workspace_scheduling_admission(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        execution_type=execution_type,
        execution_target=execution_target,
        require_workspace=require_workspace,
    )
    if workspace_id and require_actor_workspace_write:
        from packages.core.services.workspace_access import user_can_write_workspace_id

        if not user_id or not await user_can_write_workspace_id(
            db, workspace_id=workspace_id, entity_id=entity_id, user_id=user_id,
        ):
            raise PermissionError("Workspace write access is required to manage automations")
    if owner_project_id:
        from packages.core.models.workflow import WorkflowProject

        owner = await db.scalar(select(WorkflowProject).where(
            WorkflowProject.id == owner_project_id, WorkflowProject.entity_id == entity_id,
            WorkflowProject.workspace_id == workspace_id,
        ).with_for_update())
        if owner is None:
            raise ValueError("owner_project_id must belong to this Workspace")
    if idempotency_key:
        # The Workspace admission lock serializes competing creators. Check
        # replay only after lifecycle and actor authorization have succeeded.
        existing = await db.scalar(select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
            ScheduledJob.idempotency_key == idempotency_key,
        ).with_for_update())
        if existing is not None:
            equivalent = all((
                existing.owner_project_id == owner_project_id,
                existing.stop_after == stop_after,
                existing.execution_type == execution_type,
                (existing.execution_target or {}) == (execution_target or {}),
                existing.schedule_kind == schedule_kind,
                existing.cron_expr == cron_expr,
                existing.every_seconds == every_seconds,
                existing.run_at == run_at,
                existing.timezone == timezone_str,
                existing.user_id == user_id,
                existing.conversation_id == conversation_id,
                existing.agent_id == agent_id,
                existing.payload_message == payload_message,
                existing.execution_script == execution_script,
            ))
            if not equivalent:
                raise ValueError("idempotency_key already owns a different scheduled job")
            return existing
    runtime_agent_selection = None
    if runtime_agent_request is not None:
        runtime_agent_selection = await resolve_runtime_scheduled_job_agent(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            request=runtime_agent_request,
        )
        agent_id = runtime_agent_selection.agent_id
        execution_target = dict(execution_target or {})
        execution_target.pop("agent_subscription_id", None)
        if runtime_agent_selection.agent_subscription_id:
            execution_target["agent_subscription_id"] = (
                runtime_agent_selection.agent_subscription_id
            )
    # A collaborator schedules only inside a granted Workspace, naming its own resources.
    await require_collaborator_references(
        db,
        user_id=user_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        refs=scheduled_job_references(
            agent_id=agent_id, execution_target=execution_target, conversation_id=conversation_id,
        ),
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
        owner_project_id=owner_project_id,
        idempotency_key=idempotency_key,
        stop_after=stop_after,
        conversation_id=conversation_id,
        default_delivery_mode=default_delivery_mode,
        user_id=user_id,
        enabled=workspace_active,
    )
    if runtime_agent_selection is not None:
        # Transient receipt evidence for the Runtime caller; the durable exact
        # deployment lives in execution_target and is revalidated at dispatch.
        job._runtime_agent_selection = runtime_agent_selection
    if payload_message and agent_id and workspace_active:
        job.skill_generation_revision = int(job.revision or 1)
        job.skill_generation_next_attempt_at = datetime.now(timezone.utc)
    await persist_scheduled_job(db, job)

    if publish_realtime:
        # Real-time push so Automations page refreshes without a poll.
        # Derived runtime drivers opt out because their product owner exposes a
        # separate surface and generic job events would leak that abstraction.
        from packages.core.services.realtime import (
            broadcast_job_update, push_job_update,
        )
        summary = {
            "id": job.id, "job_id": job.job_id, "name": job.name,
            "schedule_kind": job.schedule_kind, "enabled": job.enabled,
            "workspace_id": job.workspace_id, "event": "created",
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
    workspace_ids: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    status: str = "all",
    statuses: Sequence[str] | None = None,
    agent_id: str | None = None,
    agent_ids: Sequence[str] | None = None,
    schedule_kinds: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    execution_types: Sequence[str] | None = None,
    last_statuses: Sequence[str] | None = None,
    created_after: str | datetime | None = None,
    created_before: str | datetime | None = None,
    updated_after: str | datetime | None = None,
    updated_before: str | datetime | None = None,
    last_run_after: str | datetime | None = None,
    last_run_before: str | datetime | None = None,
    next_run_after: str | datetime | None = None,
    next_run_before: str | datetime | None = None,
    consecutive_errors_min: int | None = None,
    consecutive_errors_max: int | None = None,
    include_workflows: bool = True,
    include_managed: bool = False,
    readable_workspace_ids: set[str] | None = None,
) -> tuple[list[ScheduledJob], int]:
    filters = _scheduled_job_filters(
        entity_id,
        workspace_id=workspace_id,
        workspace_ids=workspace_ids,
        search=search,
        agent_id=agent_id,
        agent_ids=agent_ids,
        schedule_kinds=schedule_kinds,
        job_types=job_types,
        execution_types=execution_types,
        last_statuses=last_statuses,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        last_run_after=last_run_after,
        last_run_before=last_run_before,
        next_run_after=next_run_after,
        next_run_before=next_run_before,
        consecutive_errors_min=consecutive_errors_min,
        consecutive_errors_max=consecutive_errors_max,
        include_workflows=include_workflows,
        include_managed=include_managed,
        readable_workspace_ids=readable_workspace_ids,
    )
    from packages.core.services.collaborator_steering import collaborators_acting_now

    if acting := collaborators_acting_now(entity_id):
        # While a collaborator acts (their request or run), only Jobs of their
        # live-granted Workspaces are listed; never entity-level Jobs.
        from packages.core.services.runtime_authorization.collaborator import runtime_collaborator_scope

        scope = await runtime_collaborator_scope(db, entity_id=entity_id, user_ids=acting) or frozenset()
        filters.append(ScheduledJob.workspace_id.in_(sorted(scope)))
    q = select(ScheduledJob).where(*filters)
    count_q = select(func.count()).select_from(ScheduledJob).where(*filters)

    effective_enabled, effective_paused = _scheduled_job_effective_status_conditions(
        entity_id,
    )

    if enabled_only:
        q = q.where(effective_enabled)
        count_q = count_q.where(effective_enabled)
    status_values = tuple(
        value
        for value in _scheduled_job_filter_values(
            status if status != "all" else None,
            statuses,
            lowercase=True,
        )
        if value != "all"
    )
    unknown_statuses = sorted(
        set(status_values) - {"enabled", "paused", "attention"}
    )
    if unknown_statuses:
        raise ValueError(
            "statuses must contain only enabled, paused, attention, or all"
        )
    if status_values:
        status_clauses = []
        if "enabled" in status_values:
            status_clauses.append(effective_enabled)
        if "paused" in status_values:
            status_clauses.append(effective_paused)
        if "attention" in status_values:
            status_clauses.append(ScheduledJob.consecutive_errors > 0)
        status_filter = or_(*status_clauses)
        q = q.where(status_filter)
        count_q = count_q.where(status_filter)

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
    include_managed: bool = False,
    readable_workspace_ids: set[str] | None = None,
) -> dict[str, int]:
    """Return list-level totals without applying the selected status tab."""
    filters = _scheduled_job_filters(
        entity_id,
        workspace_id=workspace_id,
        search=search,
        agent_id=agent_id,
        include_workflows=include_workflows,
        include_managed=include_managed,
        readable_workspace_ids=readable_workspace_ids,
    )
    count_q = select(func.count()).select_from(ScheduledJob).where(*filters)
    effective_enabled, _effective_paused = _scheduled_job_effective_status_conditions(
        entity_id,
    )
    enabled_q = count_q.where(
        effective_enabled,
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


def _scheduled_job_effective_status_conditions(entity_id: str):
    """Build the SQL predicates used by both list and summary projections.

    A paused Workspace blocks autonomous schedules, but a Workflow or
    standalone Stat collector keeps the lifecycle expressed by its own
    ``ScheduledJob.enabled``. A collector attached to a Workspace Goal remains
    autonomy-owned. The Goal lookup is correlated to the job's entity and
    Workspace as well as its configured stat id.
    """
    inactive_workspace = exists(
        select(Workspace.id).where(
            Workspace.id == ScheduledJob.workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.status != "active",
            Workspace.deleted_at.is_(None),
        )
    )
    from packages.core.models.goal import Goal

    goal_owned_stat_collector = exists(select(Goal.id).where(
        Goal.entity_id == ScheduledJob.entity_id,
        Goal.workspace_id == ScheduledJob.workspace_id,
        Goal.stat_id == ScheduledJob.execution_target["stat_id"].astext,
    ))
    independent_execution = or_(
        ScheduledJob.execution_type == "workflow",
        and_(
            ScheduledJob.execution_type == "workspace_stat_collection",
            ~goal_owned_stat_collector,
        ),
    )
    paused_independent_workspace = exists(
        select(Workspace.id).where(
            Workspace.id == ScheduledJob.workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.status == "paused",
            Workspace.deleted_at.is_(None),
            independent_execution,
        )
    )
    blocked_workspace = inactive_workspace & ~paused_independent_workspace
    effective_enabled = ScheduledJob.enabled.is_(True) & ~blocked_workspace
    effective_paused = ScheduledJob.enabled.is_(False) | blocked_workspace
    return effective_enabled, effective_paused


def _scheduled_job_filters(
    entity_id: str,
    *,
    workspace_id: str | None = None,
    workspace_ids: Sequence[str] | None = None,
    search: str | None = None,
    agent_id: str | None = None,
    agent_ids: Sequence[str] | None = None,
    schedule_kinds: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    execution_types: Sequence[str] | None = None,
    last_statuses: Sequence[str] | None = None,
    created_after: str | datetime | None = None,
    created_before: str | datetime | None = None,
    updated_after: str | datetime | None = None,
    updated_before: str | datetime | None = None,
    last_run_after: str | datetime | None = None,
    last_run_before: str | datetime | None = None,
    next_run_after: str | datetime | None = None,
    next_run_before: str | datetime | None = None,
    consecutive_errors_min: int | None = None,
    consecutive_errors_max: int | None = None,
    include_workflows: bool = True,
    include_managed: bool = False,
    readable_workspace_ids: set[str] | None = None,
) -> list:
    filters = [ScheduledJob.entity_id == entity_id]
    if not include_managed:
        # Agent Initiative owns these driver rows. They are deliberately absent
        # from generic Automation surfaces to preserve one mutation authority.
        filters.append(ScheduledJob.agent_initiative_id.is_(None))
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
    workspace_values = _scheduled_job_filter_values(workspace_id, workspace_ids)
    if workspace_values:
        filters.append(ScheduledJob.workspace_id.in_(workspace_values))
    if readable_workspace_ids is not None:
        filters.append(
            or_(
                ScheduledJob.workspace_id.is_(None),
                ScheduledJob.workspace_id.in_(readable_workspace_ids),
            )
        )
    agent_values = _scheduled_job_filter_values(agent_id, agent_ids)
    if agent_values:
        filters.append(ScheduledJob.agent_id.in_(agent_values))
    schedule_kind_values = _scheduled_job_filter_values(
        None,
        schedule_kinds,
        lowercase=True,
    )
    if schedule_kind_values:
        filters.append(ScheduledJob.schedule_kind.in_(schedule_kind_values))
    job_type_values = _scheduled_job_filter_values(None, job_types, lowercase=True)
    if job_type_values:
        filters.append(ScheduledJob.job_type.in_(job_type_values))
    execution_type_values = _scheduled_job_filter_values(
        None,
        execution_types,
        lowercase=True,
    )
    if execution_type_values:
        filters.append(ScheduledJob.execution_type.in_(execution_type_values))
    last_status_values = _scheduled_job_filter_values(
        None,
        last_statuses,
        lowercase=True,
    )
    if last_status_values:
        filters.append(ScheduledJob.last_status.in_(last_status_values))
    if (
        consecutive_errors_min is not None
        and consecutive_errors_max is not None
        and consecutive_errors_min > consecutive_errors_max
    ):
        raise ValueError(
            "consecutive_errors_min must be less than or equal to "
            "consecutive_errors_max"
        )
    if consecutive_errors_min is not None:
        filters.append(ScheduledJob.consecutive_errors >= consecutive_errors_min)
    if consecutive_errors_max is not None:
        filters.append(ScheduledJob.consecutive_errors <= consecutive_errors_max)
    for column, after, before, field_name in (
        (ScheduledJob.created_at, created_after, created_before, "created"),
        (ScheduledJob.updated_at, updated_after, updated_before, "updated"),
        (ScheduledJob.last_run_at, last_run_after, last_run_before, "last_run"),
        (ScheduledJob.next_run_at, next_run_after, next_run_before, "next_run"),
    ):
        after_dt, before_dt = _scheduled_job_datetime_range(
            after,
            before,
            field_name,
        )
        if after_dt is not None:
            filters.append(column.is_not(None))
            filters.append(column >= after_dt)
        if before_dt is not None:
            filters.append(column.is_not(None))
            filters.append(column <= before_dt)
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


def _scheduled_job_filter_values(
    single: Any,
    plural: Sequence[Any] | None,
    *,
    lowercase: bool = False,
) -> tuple[str, ...]:
    if plural is not None and (
        isinstance(plural, (str, bytes)) or not isinstance(plural, Sequence)
    ):
        raise TypeError("plural scheduled-job filters must be arrays")
    values: list[str] = []
    for value in (single, *(plural or ())):
        normalized = str(value or "").strip()
        if lowercase:
            normalized = normalized.lower()
        if normalized and normalized not in values:
            values.append(normalized)
    return tuple(values)


def _scheduled_job_datetime_bound(
    value: str | datetime | None,
    field_name: str,
) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{field_name} range endpoints must be ISO-8601 datetimes"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scheduled_job_datetime_range(
    after: str | datetime | None,
    before: str | datetime | None,
    field_name: str,
) -> tuple[datetime | None, datetime | None]:
    after_dt = _scheduled_job_datetime_bound(after, field_name)
    before_dt = _scheduled_job_datetime_bound(before, field_name)
    if after_dt is not None and before_dt is not None and after_dt > before_dt:
        raise ValueError(
            f"{field_name}_after must be less than or equal to {field_name}_before"
        )
    return after_dt, before_dt


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
        allow_agent_initiative_driver: bool = False,
    ) -> ScheduledJobMutationResult | None:
        _assert_scheduled_job_mutation_authority(
            job,
            allow_agent_initiative_driver=allow_agent_initiative_driver,
        )
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
        # Re-check the authoritative locked row: the caller's ORM object may
        # have been stale while waiting to acquire the Scheduler row lock.
        _assert_scheduled_job_mutation_authority(
            locked,
            allow_agent_initiative_driver=allow_agent_initiative_driver,
        )

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
        allow_agent_initiative_driver: bool = False,
    ) -> ScheduledJobMutationResult:
        """Mutate a Job row the caller already locked parent-first.

        Resource-topology changes must use :meth:`apply`, which acquires the
        entity lifecycle fence before the Job row.  Keeping that distinction
        explicit prevents callers such as Workspace pause and run projection
        from trying to acquire the broad lifecycle lock after a parent row.
        """

        _assert_scheduled_job_mutation_authority(
            job,
            allow_agent_initiative_driver=allow_agent_initiative_driver,
        )
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
    # An agent or system change acts for the Job's owner: a collaborator's Job
    # stays inside their Workspace whoever edits it on their behalf.
    acting_user_id = changed_by_id if changed_by_kind == TaskActor.USER.value else job.user_id
    if acting_user_id:
        await require_collaborator_references(
            db,
            user_id=acting_user_id,
            entity_id=entity_id,
            workspace_id=updates.get("workspace_id", job.workspace_id),
            refs=scheduled_job_references(
                agent_id=updates.get("agent_id"),
                execution_target=updates.get("execution_target"),
                conversation_id=updates.get("conversation_id"),
            ),
        )
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
    *,
    allow_agent_initiative_driver: bool = False,
) -> bool:
    job = await get_scheduled_job(db, job_pk, entity_id, for_update=True)
    if not job:
        return False
    # Generic callers may have inspected an ordinary job before an Initiative
    # lifecycle attached it. Authorize again only after locking the current
    # row, so that check/use race cannot delete the newly managed driver.
    _assert_scheduled_job_mutation_authority(
        job,
        allow_agent_initiative_driver=allow_agent_initiative_driver,
    )
    # Capture identity before delete so we can push afterwards
    summary = {
        "id": job.id, "job_id": job.job_id, "name": job.name,
        "workspace_id": job.workspace_id, "event": "deleted",
    }
    owner_id = job.user_id
    from packages.core.services.scheduled_run_lifecycle import (
        cancel_scheduled_runtime_hitl_tasks_for_job_deletion,
    )

    await cancel_scheduled_runtime_hitl_tasks_for_job_deletion(
        db,
        entity_id=entity_id,
        job_id=job.job_id,
        legacy_task_id=job.manor_task_id,
        reason="scheduled_job_deleted",
    )
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
    scope_workspace_id: str | None = None,
    scope_user_id: str | None = None,
) -> Optional[ScheduledJob]:
    job = await get_scheduled_job(db, job_pk, entity_id)
    if not job:
        return None
    if scope_workspace_id and job.workspace_id != scope_workspace_id:
        return None
    if enabled or scope_workspace_id:
        workspace_active = await _lock_workspace_scheduling_admission(
            db,
            entity_id=entity_id,
            workspace_id=job.workspace_id,
            execution_type=job.execution_type,
            execution_target=job.execution_target,
            require_workspace=require_workspace or bool(scope_workspace_id),
        )
        if enabled and not workspace_active:
            raise ValueError(
                "Workspace is not active - resume it before enabling automations"
            )
    if scope_workspace_id:
        # Workspace parent first, then Job row: keep deletion/pause lock order.
        job = await get_scheduled_job(db, job_pk, entity_id, for_update=True)
        if not job or job.workspace_id != scope_workspace_id:
            return None
        from packages.core.services.workspace_access import user_can_write_workspace_id

        if not scope_user_id or not await user_can_write_workspace_id(
            db,
            workspace_id=scope_workspace_id,
            entity_id=entity_id,
            user_id=scope_user_id,
        ):
            raise PermissionError("Workspace write access is required to manage automations")
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
        "enabled": job.enabled, "workspace_id": job.workspace_id, "event": "updated",
    }
    if job.user_id:
        await push_job_update(job.user_id, summary, entity_id=entity_id)
    await broadcast_job_update(entity_id, summary)
    return job


async def pause_workspace_autonomy_jobs(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
) -> dict[str, int]:
    """Pause Workspace-autonomy schedules while retaining configured automation.

    ``Workspace.status`` owns the Strategist and proactive Agent runtime.  A
    configured Flow or standalone Stat collector has an independent lifecycle
    and remains enabled while the Workspace is paused.  Bindings are retained:
    a binding is a reusable Flow attachment, not evidence that its timer is
    part of the Workspace-autonomy runtime.
    """
    return await _pause_workspace_automation_definitions(
        db,
        workspace_id,
        entity_id,
        include_configured=False,
    )


async def pause_workspace_automations(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
) -> dict[str, int]:
    """Disable every deployed automation definition in a Workspace.

    This is deliberately stronger than a normal Workspace pause. It is used by
    flows such as private delivery installation, where the recipient must
    explicitly activate any configured Flow or collector after setup.
    """
    return await _pause_workspace_automation_definitions(
        db,
        workspace_id,
        entity_id,
        include_configured=True,
    )


async def _pause_workspace_automation_definitions(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    include_configured: bool,
) -> dict[str, int]:
    """Pause autonomous schedules and, when requested, deployed automation."""
    jobs = list((await db.execute(
        select(ScheduledJob)
        .where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.workspace_id == workspace_id,
        )
        .order_by(ScheduledJob.id)
        .with_for_update()
    )).scalars().all())
    paused_jobs = 0
    for job in jobs:
        if (
            job.enabled
            and (
                include_configured
                or not await scheduled_job_is_workspace_independent(db, job)
            )
        ):
            result = await ScheduledJobMutationFactory.apply_locked(
                db,
                job,
                {"enabled": False},
                allow_agent_initiative_driver=True,
            )
            if result is not None and result.applied:
                paused_jobs += 1

    paused_bindings = 0
    if include_configured:
        bindings = list((await db.execute(
            select(WorkflowBinding).where(
                WorkflowBinding.entity_id == entity_id,
                WorkflowBinding.workspace_id == workspace_id,
                WorkflowBinding.trigger_type.not_in(("manual", "mcp")),
            )
        )).scalars().all())
        now = datetime.now(timezone.utc)
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
    initiative_ids = [
        job.agent_initiative_id
        for job in jobs
        if job.agent_initiative_id is not None
    ]
    if initiative_ids:
        # Workspace teardown is the one non-user path allowed to remove these
        # hidden clock drivers.  Move their product objects out of a runnable
        # state first so restore cannot expose an active Initiative with no
        # scheduler backing row.
        from packages.core.services.agent_initiative_service import (
            prepare_workspace_initiatives_for_driver_teardown,
        )

        await prepare_workspace_initiatives_for_driver_teardown(
            db,
            initiative_ids=initiative_ids,
            workspace_id=workspace_id,
            entity_id=entity_id,
        )
    if jobs:
        from packages.core.services.scheduled_run_lifecycle import (
            cancel_scheduled_runtime_hitl_tasks_for_job_deletion,
        )

        for job in jobs:
            await cancel_scheduled_runtime_hitl_tasks_for_job_deletion(
                db,
                entity_id=entity_id,
                job_id=job.job_id,
                legacy_task_id=job.manor_task_id,
                reason="workspace_automation_deleted",
            )
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
