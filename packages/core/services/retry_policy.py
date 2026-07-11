"""Execution retry policy helpers.

Configuration is intentionally JSONB-backed so operators can tune
runtime behavior without a schema migration. Resolution order:

1. workspace.settings.execution_policy.retry_policy
2. plan.plan_dag.metadata.retry_policy
3. step.params.retry_policy

Later sources override earlier ones. Defaults are safe and match the
existing behavior: 3 attempts, immediate retry, no automatic HITL handoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan, ExecutionStep


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_STRATEGY = "immediate"
DEFAULT_BASE_DELAY_SECONDS = 0
DEFAULT_MAX_DELAY_SECONDS = 900
VALID_STRATEGIES = {"immediate", "fixed", "exponential"}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    strategy: str = DEFAULT_STRATEGY
    base_delay_seconds: int = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: int = DEFAULT_MAX_DELAY_SECONDS
    auto_human_on_exhausted: bool = False
    human_prompt: str | None = None
    source: str = "default"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_between(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, coerced))


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def normalize_retry_policy(config: dict[str, Any] | None, *, source: str = "default") -> RetryPolicy:
    cfg = _as_dict(config)
    strategy = str(cfg.get("strategy") or DEFAULT_STRATEGY).strip().lower()
    if strategy not in VALID_STRATEGIES:
        strategy = DEFAULT_STRATEGY

    return RetryPolicy(
        max_attempts=_int_between(
            cfg.get("max_attempts"),
            default=DEFAULT_MAX_ATTEMPTS,
            min_value=1,
            max_value=10,
        ),
        strategy=strategy,
        base_delay_seconds=_int_between(
            cfg.get("base_delay_seconds"),
            default=DEFAULT_BASE_DELAY_SECONDS,
            min_value=0,
            max_value=3600,
        ),
        max_delay_seconds=_int_between(
            cfg.get("max_delay_seconds"),
            default=DEFAULT_MAX_DELAY_SECONDS,
            min_value=0,
            max_value=86400,
        ),
        auto_human_on_exhausted=_bool(cfg.get("auto_human_on_exhausted"), default=False),
        human_prompt=(str(cfg.get("human_prompt")).strip() if cfg.get("human_prompt") else None),
        source=source,
    )


def merge_retry_policy_configs(*configs: tuple[str, dict[str, Any] | None]) -> RetryPolicy:
    merged: dict[str, Any] = {}
    source = "default"
    for name, cfg in configs:
        cfg_dict = _as_dict(cfg)
        if cfg_dict:
            merged.update(cfg_dict)
            source = name
    return normalize_retry_policy(merged, source=source)


def workspace_retry_policy_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings_dict = _as_dict(settings)
    execution_policy = _as_dict(settings_dict.get("execution_policy"))
    task_execution = _as_dict(settings_dict.get("task_execution"))
    return (
        _as_dict(execution_policy.get("retry_policy"))
        or _as_dict(task_execution.get("retry_policy"))
        or _as_dict(settings_dict.get("retry_policy"))
    )


def plan_retry_policy_config(plan: ExecutionPlan | None) -> dict[str, Any]:
    dag = _as_dict(getattr(plan, "plan_dag", None))
    metadata = _as_dict(dag.get("metadata"))
    execution_policy = _as_dict(metadata.get("execution_policy"))
    return _as_dict(metadata.get("retry_policy")) or _as_dict(execution_policy.get("retry_policy"))


def step_retry_policy_config(step: ExecutionStep | None) -> dict[str, Any]:
    params = _as_dict(getattr(step, "params", None))
    execution_policy = _as_dict(params.get("execution_policy"))
    return _as_dict(params.get("retry_policy")) or _as_dict(execution_policy.get("retry_policy"))


async def resolve_retry_policy_for_step(
    db: AsyncSession,
    step: ExecutionStep,
    *,
    plan: ExecutionPlan | None = None,
) -> RetryPolicy:
    if plan is None:
        plan = (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == step.plan_id)
        )).scalar_one_or_none()

    workspace_config: dict[str, Any] = {}
    if step.workspace_id:
        from packages.core.models.workspace import Workspace
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == step.workspace_id)
        )).scalar_one_or_none()
        workspace_config = workspace_retry_policy_config(getattr(workspace, "settings", None))

    return merge_retry_policy_configs(
        ("workspace", workspace_config),
        ("plan", plan_retry_policy_config(plan)),
        ("step", step_retry_policy_config(step)),
    )


def apply_retry_policy_to_step(step: ExecutionStep, policy: RetryPolicy) -> None:
    """Mirror the effective max_attempts onto the step for UI/diagnostics."""
    if step.max_attempts != policy.max_attempts:
        step.max_attempts = policy.max_attempts


def retry_delay_seconds(policy: RetryPolicy, attempt_count: int) -> int:
    if policy.strategy == "immediate":
        return 0
    base = max(0, policy.base_delay_seconds)
    if policy.strategy == "fixed":
        return min(policy.max_delay_seconds, base)
    exponent = max(0, int(attempt_count) - 1)
    return min(policy.max_delay_seconds, base * (2 ** exponent))


def retry_not_before(error: dict[str, Any] | None) -> datetime | None:
    err = _as_dict(error)
    value = err.get("next_retry_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def step_retry_ready(step: ExecutionStep, *, now: datetime | None = None) -> bool:
    retry_after = retry_not_before(getattr(step, "error", None))
    if retry_after is None:
        return True
    return retry_after <= (now or datetime.now(timezone.utc))


def error_with_retry_policy(
    error: dict[str, Any] | None,
    *,
    policy: RetryPolicy,
    now: datetime,
    attempt_count: int,
) -> tuple[dict[str, Any], datetime | None]:
    enriched = dict(error or {})
    delay = retry_delay_seconds(policy, attempt_count)
    next_retry_at = now + timedelta(seconds=delay) if delay > 0 else None
    enriched["retry_policy"] = {
        "source": policy.source,
        "strategy": policy.strategy,
        "max_attempts": policy.max_attempts,
        "base_delay_seconds": policy.base_delay_seconds,
        "max_delay_seconds": policy.max_delay_seconds,
    }
    if next_retry_at is not None:
        enriched["next_retry_at"] = next_retry_at.isoformat()
    else:
        enriched.pop("next_retry_at", None)
    return enriched, next_retry_at


def human_prompt_for_exhausted_step(step: ExecutionStep, error: dict[str, Any], policy: RetryPolicy) -> str:
    if policy.human_prompt:
        return policy.human_prompt
    err_type = error.get("type") or "error"
    err_msg = error.get("message") or "The worker could not complete this step."
    return (
        f"Step '{step.step_key}' failed after {step.attempt_count}/{policy.max_attempts} attempt(s). "
        f"Please review and provide guidance. Last error: {err_type}: {err_msg}"
    )
