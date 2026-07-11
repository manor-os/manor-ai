"""Budget — per-workspace + per-worker cost tracking + enforcement.

Two layers:

  Workspace budget    Hard cap per month per workspace. Enforced in
                      Dispatcher.checkout for worker leases and in LLM
                      preflight for workspace-scoped chat/planner/
                      strategist calls. When ``monthly_spent_usd``
                      crosses ``monthly_budget_usd``, new workspace
                      execution is refused. Operator can raise the cap
                      (or set ``auto_pause_on_budget=false`` to log
                      without blocking).

  Worker budget       Already on Worker model (M3 schema). Enforced
                      the same way: a worker that has spent its monthly
                      cap stops getting offered leases.

Cost flow:

  step.cost (set by worker handler at lease completion)
    → accumulate_step_cost(step, lease) called from Dispatcher.complete_lease
        → bumps plan.cost_tracking.usd
        → bumps workspace.monthly_spent_usd
        → bumps worker.monthly_spent_usd
        → fires chat alert at 80% / 100% thresholds (deduped via
          budget_alert_state machine)

  workspace-scoped LLM usage (chat, planner, strategist, worker LLMs)
    → record_llm_usage(...)
        → accumulate_workspace_ai_cost(...)
        → bumps workspace.monthly_spent_usd
        → fires the same budget alerts

Monthly reset is a periodic Celery task — first day of month, scan
workspaces + workers whose ``budget_reset_at`` is older than the start
of the current month, reset spent → 0, alert state → 'normal'.
"""
from packages.core.budget.aggregation import (
    accumulate_workspace_ai_cost,
    accumulate_step_cost,
    monthly_reset_due,
    monthly_reset_scan,
)
from packages.core.budget.enforcement import (
    BudgetExceeded,
    check_workspace_budget,
    check_worker_budget,
)
from packages.core.budget.service import (
    get_budget_status,
    get_workspace_spent_credits_per_kind,
    set_workspace_budget,
    BudgetStatus,
)

__all__ = [
    "accumulate_step_cost",
    "accumulate_workspace_ai_cost",
    "monthly_reset_due",
    "monthly_reset_scan",
    "BudgetExceeded",
    "check_workspace_budget",
    "check_worker_budget",
    "get_budget_status",
    "get_workspace_spent_credits_per_kind",
    "set_workspace_budget",
    "BudgetStatus",
]
