from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from packages.core.services.retry_policy import (
    error_with_retry_policy,
    merge_retry_policy_configs,
    retry_delay_seconds,
    retry_not_before,
    step_retry_ready,
    workspace_retry_policy_config,
)


def test_retry_policy_merges_workspace_plan_and_step_overrides():
    policy = merge_retry_policy_configs(
        ("workspace", {"max_attempts": 4, "strategy": "fixed", "base_delay_seconds": 10}),
        ("plan", {"strategy": "exponential"}),
        ("step", {"max_attempts": 2, "auto_human_on_exhausted": True}),
    )

    assert policy.max_attempts == 2
    assert policy.strategy == "exponential"
    assert policy.base_delay_seconds == 10
    assert policy.auto_human_on_exhausted is True
    assert policy.source == "step"


def test_retry_delay_supports_immediate_fixed_and_exponential():
    immediate = merge_retry_policy_configs(("step", {"strategy": "immediate", "base_delay_seconds": 30}))
    fixed = merge_retry_policy_configs(("step", {"strategy": "fixed", "base_delay_seconds": 30}))
    exponential = merge_retry_policy_configs(
        ("step", {"strategy": "exponential", "base_delay_seconds": 30, "max_delay_seconds": 100})
    )

    assert retry_delay_seconds(immediate, 3) == 0
    assert retry_delay_seconds(fixed, 3) == 30
    assert retry_delay_seconds(exponential, 1) == 30
    assert retry_delay_seconds(exponential, 3) == 100


def test_error_with_retry_policy_sets_next_retry_at_when_delayed():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    policy = merge_retry_policy_configs(("workspace", {"strategy": "fixed", "base_delay_seconds": 45}))

    error, next_retry_at = error_with_retry_policy(
        {"type": "ProviderError", "message": "temporary"},
        policy=policy,
        now=now,
        attempt_count=1,
    )

    assert next_retry_at == now + timedelta(seconds=45)
    assert retry_not_before(error) == next_retry_at
    assert not step_retry_ready(SimpleNamespace(error=error), now=now)
    assert step_retry_ready(SimpleNamespace(error=error), now=next_retry_at)


def test_workspace_retry_policy_supports_nested_and_legacy_keys():
    assert workspace_retry_policy_config(
        {
            "execution_policy": {"retry_policy": {"max_attempts": 5}},
        }
    ) == {"max_attempts": 5}
    assert workspace_retry_policy_config(
        {
            "task_execution": {"retry_policy": {"max_attempts": 4}},
        }
    ) == {"max_attempts": 4}
    assert workspace_retry_policy_config(
        {
            "retry_policy": {"max_attempts": 2},
        }
    ) == {"max_attempts": 2}
