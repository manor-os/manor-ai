from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.queues import CeleryQueue, queue_for_task
from packages.core.tasks._runtime import run_in_worker


def test_run_in_worker_detaches_stale_pool_before_task(monkeypatch):
    class FakeEngine:
        def __init__(self) -> None:
            self.dispose_calls: list[bool] = []

        async def dispose(self, *, close: bool = True) -> None:
            self.dispose_calls.append(close)

    import packages.core.database as dbmod

    fake_engine = FakeEngine()
    monkeypatch.setattr(dbmod, "engine", fake_engine)

    async def work() -> str:
        return "ok"

    assert run_in_worker(work()) == "ok"
    assert fake_engine.dispose_calls == [False, True]


def test_celery_worker_tracing_uses_specific_service_role(monkeypatch):
    import packages.core.celery_app as celery_mod

    service_names: list[str] = []
    pricing_syncs: list[float] = []

    def fake_init_tracing(*, service_name: str) -> None:
        service_names.append(service_name)

    async def fake_pricing_sync(*, timeout_s: float) -> None:
        pricing_syncs.append(timeout_s)

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr("packages.core.observability.init_tracing", fake_init_tracing)
    monkeypatch.setattr(
        "packages.core.services.openrouter_pricing_sync.sync_openrouter_pricing_cache",
        fake_pricing_sync,
    )

    celery_mod._init_otel()

    assert service_names == ["manor-worker-heavy"]
    assert pricing_syncs == [10.0]


def test_sandbox_allocation_task_uses_existing_interactive_queue() -> None:
    assert queue_for_task("runtime.allocate_sandbox") is CeleryQueue.INTERACTIVE


def test_channel_dispatch_uses_interactive_queue() -> None:
    """Inbound channel replies must not wait behind long-running plans."""
    assert queue_for_task("channel.dispatch_inbound") is CeleryQueue.INTERACTIVE


@pytest.mark.asyncio
async def test_heavy_worker_schedules_video_job_in_current_loop(monkeypatch) -> None:
    """A heavy Workflow must not enqueue media behind its own wait."""
    from packages.core.tasks import media_tasks

    started = asyncio.Event()
    queued: list[str] = []

    async def fake_process_video_job(job_id: str) -> None:
        assert job_id == "job-1"
        started.set()

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(media_tasks, "process_video_job", fake_process_video_job)
    monkeypatch.setattr(
        media_tasks.process_video_job_task,
        "delay",
        lambda job_id: queued.append(job_id),
    )

    media_tasks.schedule_video_job("job-1")
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.gather(*list(media_tasks._background_tasks))

    assert queued == []


def test_non_heavy_process_schedules_video_job_through_celery(monkeypatch) -> None:
    from packages.core.tasks import media_tasks

    queued: list[str] = []
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "api")
    monkeypatch.setattr(
        media_tasks.process_video_job_task,
        "delay",
        lambda job_id: queued.append(job_id),
    )

    media_tasks.schedule_video_job("job-2")

    assert queued == ["job-2"]


def test_document_upload_recovery_cleanup_has_a_daily_schedule() -> None:
    from packages.core.celery_app import celery_app

    entry = celery_app.conf.beat_schedule[
        "maintenance-cleanup-document-upload-recovery"
    ]
    assert entry["task"] == "maintenance.cleanup_document_upload_recovery"






def test_channel_dispatch_passes_receipt_id_once(monkeypatch) -> None:
    """Receipt-aware dispatch must reach the idempotency wrapper."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="discord",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        inbound_message_log_id="receipt-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_outlook_channel_dispatch_uses_receipt_idempotency_wrapper(monkeypatch) -> None:
    """Outlook worker redelivery must not run the agent twice."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="outlook",
        sender_id="sender@example.com",
        chat_id="graph-message-1",
        content="hello",
        inbound_message_log_id="receipt-outlook-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-outlook-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_ms_teams_channel_dispatch_uses_receipt_idempotency_wrapper(monkeypatch) -> None:
    """Teams worker redelivery must not run the agent twice."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="ms_teams",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        inbound_message_log_id="receipt-teams-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-teams-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_twilio_sms_dispatch_uses_receipt_idempotency_wrapper(monkeypatch) -> None:
    """Twilio SID redelivery must not run the agent twice."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="twilio_sms",
        sender_id="+14155550199",
        chat_id="+14155550199",
        content="hello",
        inbound_message_log_id="receipt-twilio-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-twilio-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_wechat_personal_dispatch_uses_receipt_idempotency_wrapper(monkeypatch) -> None:
    """WeChat Personal runner retries must not run the agent twice."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="wechat_personal",
        sender_id="peer-1",
        chat_id="peer-1",
        content="hello",
        inbound_message_log_id="receipt-wechat-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-wechat-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_channel_dispatch_soft_timeout_marks_durable_receipt_failed(monkeypatch) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from packages.core.tasks import channel_tasks

    failures: list[dict[str, str]] = []

    async def timeout_dispatch(**_kwargs):
        raise SoftTimeLimitExceeded()

    async def mark_failed(**kwargs):
        failures.append(kwargs)
        return True

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", timeout_dispatch)
    monkeypatch.setattr(channel_tasks, "_mark_inbound_dispatch_failed", mark_failed)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="outlook",
        sender_id="sender@example.com",
        chat_id="graph-message-1",
        content="hello",
        inbound_message_log_id="receipt-timeout-1",
        dispatch_claim_id="claim-timeout-1",
    )

    assert result == {"status": "timeout"}
    assert failures == [{
        "inbound_message_log_id": "receipt-timeout-1",
        "dispatch_claim_id": "claim-timeout-1",
        "error": "channel dispatch soft time limit exceeded",
    }]


def test_wechat_official_dispatch_uses_receipt_idempotency_wrapper(monkeypatch) -> None:
    """WeChat Official Account retries must not run the agent twice."""
    from packages.core.tasks import channel_tasks

    captured: dict[str, object] = {}

    async def fake_dispatch_once(*, inbound_message_log_id, dispatch_claim_id, **kwargs):
        captured["inbound_message_log_id"] = inbound_message_log_id
        captured["dispatch_claim_id"] = dispatch_claim_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", fake_dispatch_once)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    result = channel_tasks.dispatch_inbound_task.run(
        entity_id="entity-1",
        channel_config_id="channel-1",
        channel_type="wechat",
        sender_id="openid-1",
        chat_id="openid-1",
        content="hello",
        inbound_message_log_id="receipt-wechat-official-1",
    )

    assert result == {"status": "ok"}
    assert captured["inbound_message_log_id"] == "receipt-wechat-official-1"
    assert "inbound_message_log_id" not in captured["kwargs"]


def test_channel_dispatch_retries_approved_reply_timeout(monkeypatch) -> None:
    from packages.core.services.channel_outbound_delivery import (
        ApprovedExternalReplyRetryableTimeout,
    )
    from packages.core.tasks import channel_tasks

    async def timeout_dispatch(**_kwargs):
        raise ApprovedExternalReplyRetryableTimeout()

    async def unexpected_terminal_write(**_kwargs):
        raise AssertionError("A retryable approved reply must not terminalize the receipt")

    retry_calls: list[dict[str, object]] = []

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise RuntimeError("approved reply retry scheduled")

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", timeout_dispatch)
    monkeypatch.setattr(
        channel_tasks,
        "_mark_inbound_dispatch_failed",
        unexpected_terminal_write,
    )
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(channel_tasks.dispatch_inbound_task, "retry", retry)

    with pytest.raises(RuntimeError, match="approved reply retry scheduled"):
        channel_tasks.dispatch_inbound_task.run(
            entity_id="entity-1",
            channel_config_id="channel-1",
            channel_type="discord",
            sender_id="discord-user",
            chat_id="discord-channel",
            content="approve",
            inbound_message_log_id="receipt-approved-timeout-1",
            dispatch_claim_id="claim-approved-timeout-1",
        )

    assert len(retry_calls) == 1
    assert retry_calls[0]["countdown"] == 30
    assert isinstance(
        retry_calls[0]["exc"],
        ApprovedExternalReplyRetryableTimeout,
    )


def test_channel_dispatch_terminalizes_exhausted_approved_reply_timeout(
    monkeypatch,
) -> None:
    from packages.core.services.channel_outbound_delivery import (
        ApprovedExternalReplyRetryableTimeout,
    )
    from packages.core.tasks import channel_tasks

    async def timeout_dispatch(**_kwargs):
        raise ApprovedExternalReplyRetryableTimeout()

    failures: list[dict[str, str]] = []

    async def mark_failed(**kwargs):
        failures.append(kwargs)
        return True

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", timeout_dispatch)
    monkeypatch.setattr(channel_tasks, "_mark_inbound_dispatch_failed", mark_failed)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    channel_tasks.dispatch_inbound_task.push_request(
        retries=3,
        id="claim-approved-timeout-final",
    )
    try:
        result = channel_tasks.dispatch_inbound_task.run(
            entity_id="entity-1",
            channel_config_id="channel-1",
            channel_type="discord",
            sender_id="discord-user",
            chat_id="discord-channel",
            content="approve",
            inbound_message_log_id="receipt-approved-timeout-final",
            dispatch_claim_id="claim-approved-timeout-final",
        )
    finally:
        channel_tasks.dispatch_inbound_task.pop_request()

    assert result == {"status": "timeout"}
    assert failures == [{
        "inbound_message_log_id": "receipt-approved-timeout-final",
        "dispatch_claim_id": "claim-approved-timeout-final",
        "error": "channel dispatch soft time limit exceeded",
    }]


def test_channel_dispatch_exhaustion_marks_durable_receipt_failed_once(monkeypatch) -> None:
    from packages.core.tasks import channel_tasks

    failures: list[dict[str, str]] = []
    terminal_replies: list[dict[str, object]] = []

    async def failed_dispatch(**_kwargs):
        return {"status": "error", "reason": "reply_not_sent"}

    async def mark_failed(**kwargs):
        failures.append(kwargs)
        return True

    async def send_terminal(**kwargs):
        terminal_replies.append(kwargs)

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", failed_dispatch)
    monkeypatch.setattr(channel_tasks, "_mark_inbound_dispatch_failed", mark_failed)
    monkeypatch.setattr(channel_tasks, "_send_discord_terminal_reply", send_terminal)
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)

    channel_tasks.dispatch_inbound_task.push_request(retries=3, id="claim-final-1")
    try:
        result = channel_tasks.dispatch_inbound_task.run(
            entity_id="entity-1",
            channel_config_id="channel-1",
            channel_type="discord",
            sender_id="discord-user",
            chat_id="discord-channel",
            content="hello",
            inbound_message_log_id="receipt-final-1",
            dispatch_claim_id="claim-final-1",
            reply_context={"interaction_token": "token"},
        )
    finally:
        channel_tasks.dispatch_inbound_task.pop_request()

    assert result == {"status": "error", "reason": "reply_not_sent"}
    assert failures == [{
        "inbound_message_log_id": "receipt-final-1",
        "dispatch_claim_id": "claim-final-1",
        "error": "reply_not_sent",
    }]
    assert terminal_replies == [{
        "channel_config_id": "channel-1",
        "chat_id": "discord-channel",
        "reply_context": {"interaction_token": "token"},
        "status": "error",
    }]


@pytest.mark.parametrize("failure_mode", ["rejected", "exception"])
def test_channel_dispatch_does_not_ack_when_terminal_receipt_write_is_rejected(
    monkeypatch,
    failure_mode: str,
) -> None:
    from celery.exceptions import SoftTimeLimitExceeded

    from packages.core.tasks import channel_tasks

    async def timeout_dispatch(**_kwargs):
        raise SoftTimeLimitExceeded()

    async def reject_terminal_write(**_kwargs):
        if failure_mode == "exception":
            raise RuntimeError("database unavailable")
        return None

    retry_calls: list[dict[str, object]] = []

    def retry(**kwargs):
        retry_calls.append(kwargs)
        raise RuntimeError("terminal persistence retry scheduled")

    monkeypatch.setattr(channel_tasks, "_dispatch_slack_inbound_once", timeout_dispatch)
    monkeypatch.setattr(
        channel_tasks,
        "_mark_inbound_dispatch_failed",
        reject_terminal_write,
    )
    monkeypatch.setattr(channel_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(channel_tasks.dispatch_inbound_task, "retry", retry)

    with pytest.raises(RuntimeError, match="terminal persistence retry scheduled"):
        channel_tasks.dispatch_inbound_task.run(
            entity_id="entity-1",
            channel_config_id="channel-1",
            channel_type="outlook",
            sender_id="sender@example.com",
            chat_id="graph-message-1",
            content="hello",
            inbound_message_log_id="receipt-timeout-2",
            dispatch_claim_id="claim-timeout-2",
        )

    assert len(retry_calls) == 1
    assert retry_calls[0]["kwargs"]["terminal_failure_error"] == (
        "channel dispatch soft time limit exceeded"
    )
    assert retry_calls[0]["kwargs"]["terminal_failure_status"] == "timeout"
@pytest.mark.asyncio
async def test_sandbox_scheduler_only_queues_external_allocation(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    commits: list[bool] = []
    create_calls: list[str] = []
    runner = SimpleNamespace(
        id="runner-control",
        last_health_at=datetime.now(timezone.utc),
        status="healthy",
    )
    reservation = SimpleNamespace(id="reservation-control", version=2)
    health_calls: list[str] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            commits.append(True)

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    async def fake_create(**_kwargs):
        create_calls.append("called")
        return "sandbox-control"

    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: True)
    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: FakeSessionFactory(),
    )
    monkeypatch.setattr(runtime_tasks, "configured_sandbox_runners", lambda: [{}])
    monkeypatch.setattr(
        runtime_tasks,
        "reconcile_sandbox_runners",
        lambda _db, _configs: _async_result([runner]),
    )
    monkeypatch.setattr(
        runtime_tasks,
        "claim_due_sandbox_runner_health_checks",
        lambda _db, **_kwargs: _async_result([]),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_tasks,
        "_refresh_runner_health",
        lambda runner_id: health_calls.append(runner_id) or _async_result(None),
    )
    monkeypatch.setattr(runtime_tasks, "expire_sandbox_reservations", lambda _db: _async_result(0))
    monkeypatch.setattr(
        runtime_tasks,
        "recover_stale_sandbox_allocations",
        lambda _db: _async_result(0),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_tasks,
        "claim_next_sandbox_allocation",
        lambda _db: _async_result((reservation, runner)),
    )
    monkeypatch.setattr(runtime_tasks, "_create_reserved_sandbox", fake_create)

    result = await runtime_tasks.run_sandbox_scheduler_once()

    assert result["allocation_queued"] is True
    assert create_calls == []
    assert health_calls == []
    assert len(commits) == 2


async def _async_result(value):
    return value
