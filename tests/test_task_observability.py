from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

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


@pytest.mark.asyncio
async def test_retrying_step_failure_stays_in_plan_thread(monkeypatch):
    from packages.core.workspace_chat import notifiers

    calls: list[dict] = []

    async def fake_post(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(notifiers, "_safe_post", fake_post)

    await notifiers.notify_step_failed(
        entity_id="entity-1",
        workspace_id="workspace-1",
        plan_id="plan-1",
        step_id="step-1",
        step_key="create_verified_stickman_mp4",
        error={"message": "Missing intermediate narration artifacts."},
        will_retry=True,
        subscription_id="sub-1",
    )

    assert len(calls) == 1
    assert calls[0]["thread_ref_kind"] == "plan"
    assert calls[0]["thread_ref_id"] == "plan-1"
    assert "will retry" in calls[0]["body"]


@pytest.mark.asyncio
async def test_terminal_step_failure_reaches_main_workspace_chat(monkeypatch):
    from packages.core.workspace_chat import notifiers

    calls: list[dict] = []

    async def fake_post(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(notifiers, "_safe_post", fake_post)

    await notifiers.notify_step_failed(
        entity_id="entity-1",
        workspace_id="workspace-1",
        plan_id="plan-1",
        step_id="step-1",
        step_key="create_verified_stickman_mp4",
        error={"message": "Missing final MP4."},
        will_retry=False,
        subscription_id="sub-1",
    )

    assert len(calls) == 2
    assert calls[0]["thread_ref_kind"] == "plan"
    assert "thread_ref_kind" not in calls[1]
