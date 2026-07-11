"""High-level start / cancel entry points.

Call sites (Strategist approve, plans router, plan_and_run_task celery
task) check ``is_temporal_enabled()`` and pick:

  if is_temporal_enabled():
      await start_plan_workflow(plan_id)
  else:
      run_plan.delay(plan_id)   # legacy Celery PlanExecutor path
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from packages.core.config import get_settings
from packages.core.temporal_app.client import (
    get_temporal_client,
    is_temporal_enabled,
    workflow_id_for_plan,
)

logger = logging.getLogger(__name__)


@dataclass
class PlanWorkflowResult:
    workflow_id: str
    run_id: str
    status: str = "started"


async def start_plan_workflow(plan_id: str) -> Optional[PlanWorkflowResult]:
    """Start (or signal-on-existing) the workflow for a plan.

    Returns None when Temporal is disabled — caller falls back to
    Celery. Otherwise returns the started workflow's id + run_id."""
    if not is_temporal_enabled():
        return None

    client = await get_temporal_client()
    if client is None:
        return None

    from temporalio.common import WorkflowIDReusePolicy

    wf_id = workflow_id_for_plan(plan_id)
    settings = get_settings()
    handle = await client.start_workflow(
        "ExecutionPlan",
        plan_id,
        id=wf_id,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        # Reusing the same id on retry is fine — completed-or-failed
        # plan workflows can be re-run (manual replay), but we never
        # want two concurrent executions of the same plan.
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )
    logger.info(
        "Temporal workflow started: plan=%s wf_id=%s run_id=%s",
        plan_id, wf_id, handle.first_execution_run_id,
    )
    return PlanWorkflowResult(
        workflow_id=wf_id,
        run_id=handle.first_execution_run_id,
        status="started",
    )


async def cancel_plan_workflow(plan_id: str) -> bool:
    """Send the ``cancel`` signal to the workflow. Returns True if
    delivered, False if Temporal disabled or workflow not found."""
    if not is_temporal_enabled():
        return False
    client = await get_temporal_client()
    if client is None:
        return False
    handle = client.get_workflow_handle(workflow_id_for_plan(plan_id))
    try:
        await handle.signal("cancel")
        return True
    except Exception:
        return False
