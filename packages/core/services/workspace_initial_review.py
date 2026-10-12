"""Transaction-owned first Strategist assessment for a new Workspace.

The existing scheduler occurrence is the durable handoff and retry owner.
This is an EVENT review, not unattended scheduled-work approval authority.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import ScheduledDispatchKind, ScheduledDispatchState
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)
INITIAL_REVIEW_OCCURRENCE = "workspace-initial-review:v1"


async def prepare_initial_workspace_review(db: AsyncSession, workspace: Workspace) -> None:
    """Stage once in the creation transaction. Caller commits; never publishes."""
    settings = dict(workspace.settings or {})
    if (
        workspace.status != "active"
        or workspace.deleted_at is not None
        or not workspace.heartbeat_enabled
        or settings.get("sandbox") is True
    ):
        return
    provisioning = dict(settings.get("provisioning") or {})
    if provisioning.get("initial_review_run_id"):
        return

    from packages.core.models.document import Integration
    from packages.core.models.goal import Goal
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup

    setup = await evaluate_workspace_blocking_setup(db, workspace)
    blocked = setup is not None and setup.blocks_work
    if blocked and not setup.details.get("allowed_setup_task_keys"):
        return
    startup = settings.get("blocking_setup")
    if not blocked and isinstance(startup, dict) and startup.get("on_ready_job_id"):
        # Explicit Blueprint startup retains its own occurrence and policy.
        return

    job = await db.scalar(select(ScheduledJob).where(
        ScheduledJob.job_id == f"sr:{workspace.id}",
        ScheduledJob.workspace_id == workspace.id,
        ScheduledJob.entity_id == workspace.entity_id,
        ScheduledJob.execution_type == "strategist_review",
        ScheduledJob.enabled.is_(True),
    ))
    if job is None:
        return

    # Installation itself is not a later readiness change. Only baseline the
    # Workspaces receiving this assessment; retain all other startup paths.
    if "_readiness_snapshot" not in settings:
        snapshot = {}
        for key, model, scope in (
            ("agents", AgentSubscription, AgentSubscription.workspace_id == workspace.id),
            ("goals", Goal, Goal.workspace_id == workspace.id),
            ("integrations", Integration, Integration.entity_id == workspace.entity_id),
        ):
            snapshot[key] = await db.scalar(select(func.count()).select_from(model).where(
                scope, model.status == "active",
            ))
        settings["_readiness_snapshot"] = snapshot

    from packages.core.services.scheduler_service import claim_job_run
    from packages.core.tasks.scheduler_tasks import (
        ScheduledDispatchLedgerContext,
        _prepared_scheduled_dispatch,
    )

    now = datetime.now(timezone.utc)
    run, claimed = await claim_job_run(
        db, job.job_id, "running",
        idempotency_key=INITIAL_REVIEW_OCCURRENCE,
        trigger_type="workspace_startup", started_at=now,
    )
    if claimed:
        run.result = {
            "dispatch": _prepared_scheduled_dispatch(
                ScheduledDispatchKind.STRATEGIST_REVIEW,
                args=[workspace.id],
                kwargs={
                    "run_id": run.id, "job_id_str": job.job_id,
                    "trigger_kind": "event", "trigger_detail": "workspace created",
                    "user_id": job.user_id or settings.get("created_by_user_id"),
                },
            ),
            "dispatch_status": ScheduledDispatchState.PREPARED.value,
            "dispatch_ledger": ScheduledDispatchLedgerContext(
                occurred_at=now.isoformat(), revision=job.revision, experiment_id=None,
            ),
        }
    provisioning["initial_review_run_id"] = run.id
    workspace.settings = {**settings, "provisioning": provisioning}
    await db.flush()


async def dispatch_initial_workspace_review(db: AsyncSession, workspace: Workspace) -> bool:
    """Publish a committed first assessment; the scheduler recovers failure."""
    if (
        workspace.status != "active"
        or workspace.deleted_at is not None
        or not workspace.heartbeat_enabled
        or (workspace.settings or {}).get("sandbox") is True
    ):
        return False
    run_id = ((workspace.settings or {}).get("provisioning") or {}).get("initial_review_run_id")
    if not run_id:
        return False
    run = await db.scalar(select(ScheduledJobRun).join(
        ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id,
    ).where(
        ScheduledJobRun.id == run_id,
        ScheduledJobRun.idempotency_key == INITIAL_REVIEW_OCCURRENCE,
        ScheduledJob.workspace_id == workspace.id,
        ScheduledJob.entity_id == workspace.entity_id,
        ScheduledJob.execution_type == "strategist_review",
        ScheduledJob.enabled.is_(True),
    ).execution_options(populate_existing=True))
    if run is None:
        return False
    result = run.result or {}
    if result.get("dispatch_status") == ScheduledDispatchState.PUBLISHED.value:
        return True

    from packages.core.tasks.scheduler_tasks import (
        _mark_scheduled_dispatch_published,
        _publish_scheduled_dispatch,
        _recover_prepared_scheduled_dispatch,
    )

    dispatch = _recover_prepared_scheduled_dispatch(run)
    if dispatch is None:
        return False
    try:
        _publish_scheduled_dispatch(dispatch)
        await _mark_scheduled_dispatch_published(run.id, dispatch)
        return True
    except Exception:
        logger.warning("Initial Workspace review remains recoverable: %s", run.id, exc_info=True)
        return False
