from datetime import datetime, timezone
from types import SimpleNamespace

from packages.core.dispatcher.service import _step_log_meta


def test_step_log_meta_includes_failure_correlation_fields():
    step = SimpleNamespace(
        id="step-1",
        plan_id="plan-1",
        current_lease_id=None,
        step_key="fetch_data",
        kind="action",
        provider="platform",
        action_key="generate_file",
        attempt_count=2,
        max_attempts=3,
        error={"type": "ProviderError", "message": "bad gateway"},
    )
    lease = SimpleNamespace(
        id="lease-1",
        worker_id="worker-1",
        lease_until=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )

    meta = _step_log_meta(
        step,
        lease,
        will_retry=True,
        next_retry_at="2026-05-01T12:00:01+00:00",
    )

    assert meta["plan_id"] == "plan-1"
    assert meta["step_id"] == "step-1"
    assert meta["lease_id"] == "lease-1"
    assert meta["worker_id"] == "worker-1"
    assert meta["action_key"] == "generate_file"
    assert meta["capability_id"] == "file.write"
    assert meta["error_type"] == "ProviderError"
    assert meta["retry_count"] == 2
    assert meta["next_retry_at"] == "2026-05-01T12:00:01+00:00"
