"""Explicit per-step runtime deadline of a worker job.

A worker job (a local CLI action, a remote coding run) may legitimately run
for a long time. The Dispatcher hands its budget to the worker with the lease
(``max_runtime_seconds``), so a genuinely hung job ends with a diagnosable
``StepDeadlineExceeded`` failure instead of an unbounded wait.

Configuration is JSONB-backed — same shape as ``retry_policy`` — so operators
can tune it without a migration:

1. ``workspace.settings.execution_policy.max_runtime_seconds``
2. ``plan.plan_dag.metadata.max_runtime_seconds``
3. ``step.params.max_runtime_seconds``

Later sources override earlier ones, matching ``retry_policy`` resolution.
The built-in default is deliberately generous: **6 hours**.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep


# Built-in default budget for one step attempt.
DEFAULT_MAX_RUNTIME_SECONDS = 6 * 60 * 60  # 21600 (6h)

# Configured values are clamped into this range. Sub-second budgets are legal
# (tests use them) but useless in production.
MIN_MAX_RUNTIME_SECONDS = 0.001
MAX_MAX_RUNTIME_SECONDS = 6 * 60 * 60  # 21600 (6h)

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

