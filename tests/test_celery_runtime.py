from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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


def test_celery_worker_allows_pricing_warmups_to_finish() -> None:
    from packages.core.celery_app import celery_app

    assert celery_app.conf.worker_proc_alive_timeout == 30.0


def test_sigterm_in_the_worker_main_process_reaches_its_pool_child(monkeypatch) -> None:
    """Celery handles SIGTERM in the main process; the task runs in a child."""
    import multiprocessing
    import os
    import signal
    import time

    from celery.signals import worker_shutting_down

    import packages.core.celery_app  # noqa: F401 - connects the shutdown receiver
    from packages.core import worker_drain

    monkeypatch.setattr(worker_drain, "_drain_flag", None)
    worker_drain.register_worker_main_process()
    context = multiprocessing.get_context("fork")
    started = context.Event()

    def pool_child() -> None:
        started.set()
        deadline = time.monotonic() + 10
        while not worker_drain.worker_drain_requested():
            if time.monotonic() > deadline:
                os._exit(1)
            time.sleep(0.01)
        os._exit(0)

    child = context.Process(target=pool_child)
    child.start()
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert started.wait(10)
        assert not worker_drain.worker_drain_requested()
        # Stand-in for Celery's warm-shutdown handler, which sends this signal.
        signal.signal(signal.SIGTERM, lambda *_: worker_shutting_down.send(
            sender="manor-worker-heavy@test", sig="SIGTERM", how="Warm", exitcode=0,
        ))
        os.kill(os.getpid(), signal.SIGTERM)
        child.join(10)
        assert child.exitcode == 0, "the pool child never saw the drain"
        assert worker_drain.worker_drain_requested()
    finally:
        signal.signal(signal.SIGTERM, previous)
        if child.is_alive():
            child.kill()


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


def _host_loop():
    """The parking host's own agent loop, as the harness enters it."""
    from packages.core.ai.runtime.execution_nesting import runtime_nested_execution_scope

    return runtime_nested_execution_scope()


@pytest.mark.asyncio
async def test_parking_host_dispatches_media_jobs_through_celery(monkeypatch) -> None:
    """A Task agent loop parks instead of waiting: a job on its loop would be
    drained by run_in_worker after the park, holding the heavy worker."""
    from packages.core.ai.runtime.media_job_scope import runtime_media_jobs_detached_scope
    from packages.core.tasks import media_tasks

    queued: list[str] = []

    async def must_not_run_inline(job_id: str) -> None:
        raise AssertionError("a parked Task's media job must not run on its loop")

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(media_tasks, "process_video_job", must_not_run_inline)
    monkeypatch.setattr(media_tasks.process_video_job_task, "delay", lambda job_id: queued.append(job_id))
    before = set(media_tasks._background_tasks)

    with runtime_media_jobs_detached_scope(task_id="task-1", run_id="run-1"), _host_loop():
        media_tasks.schedule_video_job("job-parked")

    assert queued == ["job-parked"]
    assert set(media_tasks._background_tasks) == before


@pytest.mark.asyncio
async def test_parking_host_leaves_a_job_pending_when_the_broker_is_down(monkeypatch, caplog) -> None:
    """Neither its own loop (drained while parked) nor a daemon thread: the job
    stays PENDING and the stale-job sweeper dispatches it again."""
    import threading

    from packages.core.ai.runtime.media_job_scope import runtime_media_jobs_detached_scope
    from packages.core.tasks import media_tasks

    def broker_down(_job_id):
        raise RuntimeError("broker unavailable")

    def no_thread(*_args, **_kwargs):
        raise AssertionError("a parked Task's job must not start a daemon thread")

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(media_tasks.process_video_job_task, "delay", broker_down)
    monkeypatch.setattr(threading, "Thread", no_thread)
    before = set(media_tasks._background_tasks)

    with runtime_media_jobs_detached_scope(task_id="task-1", run_id="run-1"), _host_loop():
        media_tasks.schedule_video_job("job-broker-down")

    assert set(media_tasks._background_tasks) == before
    assert "left pending for the stale-job sweeper" in caplog.text


@pytest.mark.asyncio
async def test_stale_job_sweeper_dispatches_a_pending_job_that_never_started(
    monkeypatch, db_session,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.database as database
    from packages.core.models.base import generate_ulid
    from packages.core.models.media_job import MediaJob
    from packages.core.tasks import media_tasks

    monkeypatch.setattr(
        database, "async_session",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
    )
    queued: list[str] = []
    monkeypatch.setattr(media_tasks.process_video_job_task, "delay", lambda job_id: queued.append(job_id))
    job = MediaJob(
        id=generate_ulid(), entity_id=generate_ulid(), kind="video", status="pending", prompt="x",
        model="bytedance/seedance-2.0",
        params={"task_id": generate_ulid(), "provider_submit_state": "not_started"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(job)
    await db_session.commit()

    await media_tasks.recover_stale_video_jobs(stale_after_seconds=60)

    assert job.id in queued


def test_media_job_lineage_names_only_the_scope_task_run() -> None:
    from packages.core.ai.runtime.media_job_scope import (
        runtime_media_job_lineage,
        runtime_media_jobs_detached,
        runtime_media_jobs_detached_scope,
    )

    kwargs = {"_runtime_tool_call_id_from_context": "call-7"}
    assert runtime_media_jobs_detached() is None
    assert runtime_media_job_lineage("task-1", kwargs) == {}
    with runtime_media_jobs_detached_scope(task_id="task-1", run_id="run-1"), _host_loop():
        assert runtime_media_jobs_detached() == {"task_id": "task-1", "run_id": "run-1"}
        assert runtime_media_job_lineage("task-1", kwargs) == {
            "task_coordinator_run_id": "run-1", "tool_call_id": "call-7",
        }
        assert runtime_media_job_lineage("task-2", {}) == {}
    assert runtime_media_jobs_detached() is None


_NESTED_LOOPS = ["prompt_skill", "delegated_agent", "workflow_agent_node", "generic_agent_loop", "workflow_run"]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", _NESTED_LOOPS)
async def test_nested_executions_inside_a_parking_host_keep_inline_media(monkeypatch, nested) -> None:
    """Only the parking host's own loop is detached. Any nested agent loop or
    workflow run waits for its media jobs in process; queueing them on
    heavy -c 1 would leave that wait behind its own worker."""
    from contextlib import asynccontextmanager

    from packages.core.ai import workflow_runner
    from packages.core.ai.runtime import harness, workspace_delegation_actions as delegation
    from packages.core.ai.runtime.execution_nesting import runtime_nested_execution_scope
    from packages.core.ai.runtime.media_job_scope import (
        runtime_media_jobs_detached,
        runtime_media_jobs_detached_scope,
    )
    from packages.core.tasks import media_tasks

    queued: list[str] = []
    ran: list[str] = []
    seen_scopes: list = []

    async def inline_job(job_id: str) -> None:
        ran.append(job_id)

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(media_tasks, "process_video_job", inline_job)
    monkeypatch.setattr(media_tasks.process_video_job_task, "delay", lambda job_id: queued.append(job_id))

    async def nested_work(*_args, **_kwargs):
        seen_scopes.append(runtime_media_jobs_detached())
        media_tasks.schedule_video_job(f"job-{nested}")
        return "ok"

    # The core loop body is faked; every real nested entry point is used.
    monkeypatch.setattr(harness, "_runtime_execute_agentic_loop", nested_work)
    entries = {
        "prompt_skill": harness.runtime_execute_skill_agent_loop,
        "workflow_agent_node": harness.runtime_execute_workflow_agent_loop,
        "generic_agent_loop": harness.runtime_execute_agentic_loop,
    }
    if nested == "delegated_agent":
        async def delegated_child(**_kwargs):
            return await harness.runtime_execute_agentic_loop()

        monkeypatch.setattr(delegation, "_runtime_workspace_delegate_service_action", delegated_child)
        run_nested = delegation.runtime_workspace_delegate_service_action
    elif nested == "workflow_run":
        class Claim:
            def __bool__(self):
                return True

            def raise_if_lost(self):
                return None

            def mark_terminal_committed(self):
                return None

        @asynccontextmanager
        async def claim(_run_id):
            yield Claim()

        async def run_claimed(self, *_args, **_kwargs):
            await nested_work()

        monkeypatch.setattr(workflow_runner, "workflow_run_execution_claim", claim)
        monkeypatch.setattr(workflow_runner.WorkflowRunner, "_run_claimed", run_claimed)

        async def run_nested():
            await workflow_runner.WorkflowRunner().run("run-1")
            return "ok"
    else:
        run_nested = entries[nested]

    with runtime_media_jobs_detached_scope(task_id="task-1", run_id="run-1"):
        # The parking host's own loop: its direct calls are detached.
        with runtime_nested_execution_scope():
            assert runtime_media_jobs_detached() == {"task_id": "task-1", "run_id": "run-1"}
            assert await run_nested() == "ok"
            assert runtime_media_jobs_detached() == {"task_id": "task-1", "run_id": "run-1"}
        # Outside any loop the scope does not apply either.
        assert runtime_media_jobs_detached() is None
    await asyncio.gather(*list(media_tasks._background_tasks))

    assert seen_scopes == [None]
    assert queued == [] and ran == [f"job-{nested}"]


@pytest.mark.asyncio
async def test_nesting_depth_is_restored_when_a_nested_loop_raises(monkeypatch) -> None:
    from packages.core.ai.runtime import harness
    from packages.core.ai.runtime.execution_nesting import runtime_execution_nesting_depth

    async def boom(**_kwargs):
        raise RuntimeError("nested loop failed")

    monkeypatch.setattr(harness, "_runtime_execute_agentic_loop", boom)
    before = runtime_execution_nesting_depth()
    with pytest.raises(RuntimeError, match="nested loop failed"):
        await harness.runtime_execute_agentic_loop()
    assert runtime_execution_nesting_depth() == before


@pytest.mark.parametrize("parent_fails", [False, True])
def test_worker_teardown_preserves_pending_inline_media(monkeypatch, parent_fails) -> None:
    """A pending tool receipt must not cancel its already-started child job."""
    import packages.core.database as dbmod
    from packages.core.tasks import media_tasks

    events = []

    class FakeEngine:
        async def dispose(self, *, close=True):
            if close:
                events.append("pool-closed")

    async def child(job_id):
        events.append("child-started")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        events.append("provider-state-and-output-saved")

    async def parent():
        media_tasks.schedule_video_job("already-submitted")
        events.append("pending-receipt-returned")
        if parent_fails:
            raise ValueError("later workflow node failed")
        return {"status": "pending"}

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "worker-heavy")
    monkeypatch.setattr(dbmod, "engine", FakeEngine())
    monkeypatch.setattr(media_tasks, "process_video_job", child)
    if parent_fails:
        with pytest.raises(ValueError, match="later workflow node failed"):
            run_in_worker(parent())
    else:
        assert run_in_worker(parent()) == {"status": "pending"}
    assert events == ["pending-receipt-returned", "child-started",
        "provider-state-and-output-saved", "pool-closed"]


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
    async def _no_timeouts(_db, **_kwargs):
        return []

    monkeypatch.setattr(runtime_tasks, "expire_runtime_human_input_requests", _no_timeouts)
    monkeypatch.setattr(
        runtime_tasks,
        "recover_stale_sandbox_allocations",
        lambda _db: _async_result(0),
        raising=False,
    )
    monkeypatch.setattr(runtime_tasks, "recover_waiting_sandbox_runs", lambda _db: _async_result(0))
    monkeypatch.setattr(runtime_tasks, "adopt_follower_reservations", lambda _db: _async_result(0))
    monkeypatch.setattr(runtime_tasks, "redeliver_exec_slot_resumes", lambda _db: _async_result(0))
    monkeypatch.setattr(runtime_tasks, "_wake_exec_slot_waiters", lambda _factory: _async_result(0))
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


def test_worker_process_start_warms_the_tool_registry(monkeypatch):
    """Lazy registration blocked the first chat turn of every child process."""
    from packages.core import celery_app as module
    from packages.core.ai.runtime import tool_registry

    calls = []
    monkeypatch.setattr(
        tool_registry, "runtime_ensure_tool_registry_initialized", lambda: calls.append(1),
    )
    module._warm_tool_registry()
    assert calls == [1]

    def broken():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(tool_registry, "runtime_ensure_tool_registry_initialized", broken)
    module._warm_tool_registry()  # never blocks worker boot
