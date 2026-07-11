"""Helpers that bridge Dispatcher → Workflow.

Called from Dispatcher.complete_lease / fail_lease / lease_needs_human
when ``is_temporal_enabled()``. Each helper:

  * looks up the workflow handle by ``workflow_id_for_plan(plan_id)``
  * sends the right signal (step_completed / step_failed / human_input)
  * tolerates "workflow not found" silently — the plan may be running
    via the legacy Celery PlanExecutor instead.

Idempotent: signals are at-least-once on the Temporal side too, so the
workflow accumulators use last-write-wins semantics.
"""
from __future__ import annotations

import logging
from typing import Optional

from packages.core.temporal_app.client import (
    get_temporal_client,
    is_temporal_enabled,
    workflow_id_for_plan,
)

logger = logging.getLogger(__name__)


async def signal_step_completed(
    plan_id: str, step_key: str, result: Optional[dict] = None,
) -> None:
    await _signal(plan_id, "step_completed", step_key, result)


async def signal_step_failed(
    plan_id: str, step_key: str, error: dict,
) -> None:
    await _signal(plan_id, "step_failed", step_key, error)


async def signal_human_input(
    plan_id: str, step_key: str, response: dict,
) -> None:
    await _signal(plan_id, "human_input", step_key, response)


async def signal_cancel(plan_id: str) -> None:
    await _signal(plan_id, "cancel")


async def _signal(plan_id: str, name: str, *args) -> None:
    if not is_temporal_enabled():
        return
    client = await get_temporal_client()
    if client is None:
        return
    handle = client.get_workflow_handle(workflow_id_for_plan(plan_id))
    # temporalio's handle.signal needs ``args=[…]`` for multi-arg signals.
    try:
        await handle.signal(name, args=list(args))
    except Exception as exc:  # noqa: BLE001
        # Most common: workflow doesn't exist (plan ran via Celery
        # PlanExecutor instead, or workflow already terminated).
        # Either case is benign.
        logger.debug(
            "temporal signal %s on plan %s skipped: %s",
            name, plan_id, exc,
        )
