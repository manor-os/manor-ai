"""Explicit per-step runtime deadline.

A plan step may legitimately run for a long time (video generation, large
batch work). Liveness is already proven by the in-process lease heartbeat
(``packages/core/workers/internal.py``), so the question a worker must answer
is not "has this run longer than the process timeout?" but "has this run
longer than the budget somebody actually configured for it?".

That budget is ``max_runtime_seconds``. Configuration is JSONB-backed — same
shape as ``retry_policy`` — so operators can tune it without a migration:

1. ``workspace.settings.execution_policy.max_runtime_seconds``
2. ``plan.plan_dag.metadata.max_runtime_seconds``
3. ``step.params.max_runtime_seconds``

Later sources override earlier ones (i.e. step beats plan beats workspace
beats the built-in default), matching ``retry_policy`` resolution exactly.

The built-in default is deliberately generous: **6 hours**. Steps are not
expected to hit it; it exists so a genuinely hung step eventually produces a
clean, diagnosable ``StepDeadlineExceeded`` failure that flows through the
normal retry policy — instead of the process-level SIGKILL that used to end
any step running past 30 minutes.

The Celery limits on ``execute_lease`` live here too, so the invariant
"celery limit > step deadline" is visible in one place and cannot silently
invert. Celery's limit is only a last-resort backstop for a wedged process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep


# Built-in default budget for one step attempt. Generous on purpose — the
# planner is NOT constrained to short steps.
DEFAULT_MAX_RUNTIME_SECONDS = 6 * 60 * 60  # 21600 (6h)

# Configured values are clamped into this range. The upper clamp is what keeps
# the Celery ceiling strictly above every possible deadline; if you raise it,
# raise the two Celery constants below by the same amount. Sub-second budgets
# are legal (tests use them) but useless in production.
MIN_MAX_RUNTIME_SECONDS = 0.001
MAX_MAX_RUNTIME_SECONDS = 6 * 60 * 60  # 21600 (6h)

# Celery limits for the ``execute_lease`` task. These are a BACKSTOP for a
# hung worker process, NOT the execution policy: the step deadline above is
# the policy, and it must always expire first so failures are structured
# (StepDeadlineExceeded → fail_lease → retry policy) rather than a
# SoftTimeLimitExceeded / SIGKILL that kills the heartbeat with the process.
CELERY_LEASE_SOFT_TIME_LIMIT_SECONDS = MAX_MAX_RUNTIME_SECONDS + 15 * 60  # 6h15m
CELERY_LEASE_HARD_TIME_LIMIT_SECONDS = MAX_MAX_RUNTIME_SECONDS + 30 * 60  # 6h30m

# Redis broker visibility timeout — the last link in the same chain.
#
# The Redis transport has no server-side ack: kombu re-delivers any message
# whose consumer has not acked it within ``visibility_timeout`` (default: ONE
# HOUR). With ``task_acks_late=True`` the ack happens when the task *finishes*,
# so before this constant existed every step still running at 60 minutes was
# handed to a second worker while the first was still executing it — duplicated
# external publishes, duplicated media generation, duplicated paid API calls,
# and a late ``complete_lease`` against an already-terminal lease.
#
# It must therefore sit ABOVE the hard time limit: a task that Celery itself
# would kill must be dead before the broker considers re-delivering it. The
# full invariant, all derived from ``MAX_MAX_RUNTIME_SECONDS``:
#
#     visibility timeout > celery hard limit > celery soft limit
#                        > MAX_MAX_RUNTIME_SECONDS >= any step deadline
#
# tests/test_orchestration_hardening.py guards the ordering.
CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS = MAX_MAX_RUNTIME_SECONDS + 60 * 60  # 7h

STEP_DEADLINE_ERROR_TYPE = "StepDeadlineExceeded"
STEP_DEADLINE_HINT = (
    "raise max_runtime_seconds for this step/plan/workspace, or split the work "
    "into multiple steps (a sleep step can poll an external job asynchronously)"
)


@dataclass(frozen=True)
class StepDeadline:
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    source: str = "default"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_seconds(value: Any) -> float | None:
    """Parse a configured budget; return None when unusable (fall through)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    if coerced <= 0:
        return None
    clamped = max(MIN_MAX_RUNTIME_SECONDS, min(MAX_MAX_RUNTIME_SECONDS, coerced))
    return int(clamped) if float(clamped).is_integer() else clamped


def _config_value(container: dict[str, Any]) -> Any:
    """Accept both the flat key and the ``execution_policy`` nesting."""
    cfg = _as_dict(container)
    if "max_runtime_seconds" in cfg:
        return cfg.get("max_runtime_seconds")
    execution_policy = _as_dict(cfg.get("execution_policy"))
    if "max_runtime_seconds" in execution_policy:
        return execution_policy.get("max_runtime_seconds")
    return None


def workspace_max_runtime_config(settings: dict[str, Any] | None) -> Any:
    settings_dict = _as_dict(settings)
    for container in (
        _as_dict(settings_dict.get("execution_policy")),
        _as_dict(settings_dict.get("task_execution")),
    ):
        value = container.get("max_runtime_seconds")
        if value is not None:
            return value
    return settings_dict.get("max_runtime_seconds")


def plan_max_runtime_config(plan: ExecutionPlan | None) -> Any:
    dag = _as_dict(getattr(plan, "plan_dag", None))
    return _config_value(_as_dict(dag.get("metadata")))


def step_max_runtime_config(step: ExecutionStep | None) -> Any:
    return _config_value(_as_dict(getattr(step, "params", None)))


def merge_max_runtime_configs(*configs: tuple[str, Any]) -> StepDeadline:
    """Later non-empty layers win — same precedence as ``retry_policy``."""
    seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    source = "default"
    for name, raw in configs:
        coerced = _coerce_seconds(raw)
        if coerced is not None:
            seconds = coerced
            source = name
    return StepDeadline(max_runtime_seconds=seconds, source=source)


async def resolve_step_deadline(
    db: AsyncSession,
    step: ExecutionStep,
    *,
    plan: ExecutionPlan | None = None,
) -> StepDeadline:
    """Resolve the effective runtime budget for one step attempt."""
    if plan is None and getattr(step, "plan_id", None):
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == step.plan_id)
        )).scalar_one_or_none()

    workspace_config: Any = None
    if getattr(step, "workspace_id", None):
        from packages.core.models.workspace import Workspace

        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == step.workspace_id)
        )).scalar_one_or_none()
        workspace_config = workspace_max_runtime_config(getattr(workspace, "settings", None))

    return merge_max_runtime_configs(
        ("workspace", workspace_config),
        ("plan", plan_max_runtime_config(plan)),
        ("step", step_max_runtime_config(step)),
    )


def step_deadline_error(
    *,
    max_runtime_seconds: float,
    elapsed_seconds: float,
    source: str = "default",
) -> dict[str, Any]:
    """The structured failure a blown deadline reports to ``fail_lease``."""
    budget: float | int = float(max_runtime_seconds)
    if float(budget).is_integer():
        budget = int(budget)
    return {
        "type": STEP_DEADLINE_ERROR_TYPE,
        "message": f"step exceeded its {budget}-second runtime budget",
        "max_runtime_seconds": budget,
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "max_runtime_source": source,
        "hint": STEP_DEADLINE_HINT,
    }
