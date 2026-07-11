"""Temporal-driven plan workflows — opt-in durable execution.

When ``TEMPORAL_ENABLED=true``, plans run as Temporal workflows
instead of the Celery-driven PlanExecutor cycle. Both paths share
the same data layer (execution_plans / execution_steps) and worker
layer (Dispatcher → InternalWorker / external workers); only the
plan-level orchestrator changes.

What Temporal buys us:

  * **Durable sleep** — ``await workflow.sleep(timedelta(days=7))``
    survives Manor process restarts, broker outages, and operator
    intervention. The Celery cycle pattern can't.
  * **Signal-based HITL** — ``await workflow.wait_condition(…)``
    blocks reliably for days waiting on a human; signals from
    Dispatcher (lease completed) wake the workflow.
  * **Replay debug** — every workflow execution is fully
    reconstructible from the event history. Temporal Web shows the
    timeline.
  * **Versioning** — workflow code can evolve without breaking
    in-flight runs.

What stays the same:

  * Dispatcher / Worker layer — workflows just signal them, then wait.
  * Chat events — Dispatcher posts on lease lifecycle as before.
  * MCP adapters — workers call the same code.

Layout:

  client.py         get_temporal_client() — feature-flagged singleton
  workflows.py      ExecutionPlanWorkflow — the per-plan workflow
  activities.py     load_plan_dag / mark_step_pending / finalize_plan
  signaling.py      Dispatcher → workflow signal helpers
  worker.py         TemporalWorker — process that runs workflows + activities
  service.py        start_plan_workflow(plan_id) — entry point
"""
from packages.core.temporal_app.client import (
    is_temporal_enabled,
    get_temporal_client,
    workflow_id_for_plan,
)
from packages.core.temporal_app.signaling import (
    signal_step_completed,
    signal_step_failed,
    signal_human_input,
    signal_cancel,
)
from packages.core.temporal_app.service import (
    start_plan_workflow,
    cancel_plan_workflow,
    PlanWorkflowResult,
)

__all__ = [
    "is_temporal_enabled",
    "get_temporal_client",
    "workflow_id_for_plan",
    "signal_step_completed",
    "signal_step_failed",
    "signal_human_input",
    "signal_cancel",
    "start_plan_workflow",
    "cancel_plan_workflow",
    "PlanWorkflowResult",
]
