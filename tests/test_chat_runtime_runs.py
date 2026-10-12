from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select

from apps.api.routers.chat import (
    _durable_runtime_event_stream,
    _terminal_runtime_approval_outcome,
    cancel_chat_runtime_run,
    get_chat_runtime_run_status,
    reconnect_chat_runtime_run_events,
)
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.services.runtime_event_stream import (
    parse_sse_frame,
    read_runtime_sse_events,
    sse_frame_with_id,
)
from packages.core.services.runtime_run_service import (
    claim_runtime_run_execution,
    complete_runtime_run_execution,
    recover_expired_runtime_run_leases,
)
from packages.core.tasks.runtime_tasks import _runtime_terminal_error_from_frame


@pytest.mark.unit
def test_host_resolved_runtime_approval_has_typed_terminal_routing() -> None:
    from packages.core.ai.runtime.approval_service import RuntimeApprovalResolution
    from packages.core.services.chat_approvals import (
        _chat_runtime_resolution_metadata,
    )

    handled = _chat_runtime_resolution_metadata(
        RuntimeApprovalResolution("Already processed.")
    )
    assert handled == {
        "approval_kind": "runtime_terminal",
        "approval_outcome": "handled",
    }
    assert _terminal_runtime_approval_outcome(handled) == "handled"
    assert _terminal_runtime_approval_outcome({
        "approval_kind": "runtime_terminal",
        "approval_outcome": "rejected",
    }) == "rejected"
    assert _terminal_runtime_approval_outcome({
        "approval_kind": "task_runtime",
        "task_runtime_resume": {"choice": "rejected"},
    }) == "rejected"
    assert _terminal_runtime_approval_outcome({
        "approval_kind": "task_runtime",
        "task_runtime_resume": {"choice": "approved"},
    }) is None


def _run(*, run_id: str, user_id: str = "user_chat_run") -> RuntimeRun:
    return RuntimeRun(
        id=run_id,
        root_run_id=run_id,
        conversation_id=generate_ulid(),
        assistant_message_id=generate_ulid(),
        entity_id="entity_chat_run",
        user_id=user_id,
        status=RuntimeRunStatus.QUEUED.value,
        execution_payload={"message": "hello"},
    )


@pytest.mark.unit
def test_runtime_terminal_error_frame_marks_durable_run_failed() -> None:
    assert _runtime_terminal_error_from_frame(
        'event: stream_end\ndata: {"stop_reason":"chrome_cli_worker_not_paired",'
        '"error":"Chrome requires a paired local worker."}\n\n'
    ) == {
        "code": "chrome_cli_worker_not_paired",
        "message": "Chrome requires a paired local worker.",
    }
    assert _runtime_terminal_error_from_frame(
        'event: stream_end\ndata: {"stop_reason":"completed"}\n\n'
    ) is None


@pytest.mark.unit
async def test_execute_runtime_run_persists_terminal_stream_failure(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    run = SimpleNamespace(
        id="run-terminal-failure",
        conversation_id="conversation-terminal-failure",
        assistant_message_id="message-terminal-failure",
        entity_id="entity-terminal-failure",
        user_id="user-terminal-failure",
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "检查 LinkedIn"},
        checkpoint=None,
        status=RuntimeRunStatus.RUNNING.value,
    )
    completion_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

        async def get(self, _model, _run_id):
            return run if _run_id == run.id else None

    async def fake_claim(_db, **_kwargs):
        return run

    async def fake_complete(db, **kwargs):
        completion_calls.append({"db": db, **kwargs})
        return SimpleNamespace(status=RuntimeRunStatus.FAILED.value)

    async def fake_stream(*_args, **_kwargs):
        yield (
            'event: stream_end\ndata: {"stop_reason":"chrome_cli_worker_not_paired",'
            '"error":"Chrome requires a paired local worker."}\n\n'
        )

    async def fake_heartbeat(*_args, **_kwargs):
        await asyncio.Future()

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.claim_runtime_run_execution",
        fake_claim,
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.complete_runtime_run_execution",
        fake_complete,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_stream_chat_turn",
        fake_stream,
    )
    monkeypatch.setattr(runtime_tasks, "append_runtime_sse_event", AsyncMock())
    monkeypatch.setattr(runtime_tasks, "_runtime_run_lease_heartbeat", fake_heartbeat)

    result = await runtime_tasks.execute_runtime_run_once(
        run_id=run.id,
        worker_id="worker-terminal-failure",
    )

    assert result["status"] == RuntimeRunStatus.FAILED.value
    assert completion_calls == [
        {
            "db": ANY,
            "run_id": run.id,
            "worker_id": "worker-terminal-failure",
            "result": {"message_id": run.assistant_message_id},
            "error": {
                "code": "chrome_cli_worker_not_paired",
                "message": "Chrome requires a paired local worker.",
            },
        }
    ]


@pytest.mark.unit
async def test_execute_runtime_run_stops_when_lease_heartbeat_ends(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    run = SimpleNamespace(
        id="run-stolen-lease",
        conversation_id="conversation-stolen-lease",
        assistant_message_id="message-stolen-lease",
        entity_id="entity-stolen-lease",
        user_id="user-stolen-lease",
        agent_id=None,
        workspace_id=None,
        execution_payload={"message": "read the fixture"},
        checkpoint=None,
    )
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()
    lease_lost = asyncio.Event()
    complete = AsyncMock()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    async def fake_claim(_db, **_kwargs):
        return run

    async def fake_stream(*_args, **_kwargs):
        stream_started.set()
        try:
            await asyncio.Future()
            yield "event: impossible\\ndata: {}\\n\\n"
        finally:
            stream_closed.set()

    async def fake_heartbeat(*_args, **_kwargs):
        await stream_started.wait()
        lease_lost.set()

    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.claim_runtime_run_execution",
        fake_claim,
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.complete_runtime_run_execution",
        complete,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_stream_chat_turn",
        fake_stream,
    )
    monkeypatch.setattr(runtime_tasks, "_runtime_run_lease_heartbeat", fake_heartbeat)

    execution = asyncio.create_task(runtime_tasks.execute_runtime_run_once(
        run_id=run.id, worker_id="worker-old",
    ))
    try:
        await asyncio.wait_for(lease_lost.wait(), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=2)
        assert stream_closed.is_set()
        complete.assert_not_awaited()
    finally:
        if not execution.done():
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution


@pytest.mark.unit
async def test_outbox_dispatch_commits_recovery_before_publishing(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    event = SimpleNamespace(
        event_type="runtime.execute_run",
        payload={"run_id": "recovered-run"},
        delivered_at=None,
        last_error=None,
        attempt_count=0,
    )
    commits = 0
    published: list[tuple[str, dict, str]] = []

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [event]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

        async def commit(self):
            nonlocal commits
            commits += 1

    async def fake_recover(*_args, **_kwargs):
        return 1, 0

    def fake_send_task(name, *, kwargs, queue):
        assert commits == 1
        published.append((name, kwargs, queue))

    monkeypatch.setattr(runtime_tasks, "_durable_external_sandbox_enabled", lambda: True)
    monkeypatch.setattr(runtime_tasks, "expire_runtime_human_input_requests", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        runtime_tasks,
        "_recover_task_runtime_resume_outbox",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.recover_expired_runtime_run_leases",
        fake_recover,
    )
    monkeypatch.setattr(runtime_tasks.celery_app, "send_task", fake_send_task)

    delivered = await runtime_tasks.dispatch_runtime_outbox_once(batch_size=1)

    assert delivered == 1
    assert commits == 2
    assert published == [
        ("runtime.execute_run", {"run_id": "recovered-run"}, "interactive")
    ]
    assert event.delivered_at is not None


@pytest.mark.unit
async def test_outbox_dispatch_runs_when_external_sandbox_is_disabled(monkeypatch) -> None:
    """Generic continuation events must publish in ordinary deployments."""

    from packages.core.tasks import runtime_tasks

    event = SimpleNamespace(
        event_type="packages.core.tasks.ai_tasks.run_agent_task",
        payload={"task_id": "task-1"},
        delivered_at=None,
        last_error=None,
        attempt_count=0,
    )
    commits = 0
    published: list[tuple[str, dict, str]] = []

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [event]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

        async def commit(self):
            nonlocal commits
            commits += 1

    monkeypatch.setattr(
        runtime_tasks,
        "_durable_external_sandbox_enabled",
        lambda: False,
    )
    recovery = AsyncMock(return_value=(0, 0))
    expiry = AsyncMock(return_value=[])
    monkeypatch.setattr("packages.core.services.runtime_run_service.recover_expired_runtime_run_leases", recovery)
    monkeypatch.setattr(runtime_tasks, "expire_runtime_human_input_requests", expiry)
    monkeypatch.setattr(
        runtime_tasks,
        "_recover_task_runtime_resume_outbox",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        runtime_tasks.celery_app,
        "send_task",
        lambda name, *, kwargs, queue: published.append((name, kwargs, queue)),
    )

    delivered = await runtime_tasks.dispatch_runtime_outbox_once(batch_size=1)

    assert delivered == 1
    assert commits == 2
    assert published == [
        (
            "packages.core.tasks.ai_tasks.run_agent_task",
            {"task_id": "task-1"},
            "heavy",
        )
    ]
    assert event.delivered_at is not None

    # Both run recovery and question expiry are required in local/inline mode.
    recovery.assert_awaited_once()
    expiry.assert_awaited_once()


@pytest.mark.unit
async def test_outbox_dispatch_backs_off_failed_oldest_event(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    event = SimpleNamespace(
        event_type="runtime.execute_run",
        payload={"run_id": "retry-run"},
        delivered_at=None,
        available_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        last_error=None,
        attempt_count=0,
    )
    commits = 0

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [event]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

        async def commit(self):
            nonlocal commits
            commits += 1

    monkeypatch.setattr(
        runtime_tasks,
        "_durable_external_sandbox_enabled",
        lambda: False,
    )
    monkeypatch.setattr("packages.core.services.runtime_run_service.recover_expired_runtime_run_leases", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(runtime_tasks, "expire_runtime_human_input_requests", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        runtime_tasks,
        "_recover_task_runtime_resume_outbox",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "packages.core.database.create_worker_session",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        runtime_tasks.celery_app,
        "send_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("broker unavailable")
        ),
    )
    before = datetime.now(timezone.utc)

    delivered = await runtime_tasks.dispatch_runtime_outbox_once(batch_size=1)

    assert delivered == 0
    assert commits == 2
    assert event.attempt_count == 1
    assert event.last_error == "broker unavailable"
    assert event.available_at >= before + timedelta(
        seconds=runtime_tasks._RUNTIME_OUTBOX_RETRY_BASE_SECONDS
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attempt_count", "delay_seconds"),
    [(1, 5), (2, 10), (3, 20), (20, 300)],
)
def test_runtime_outbox_retry_backoff_is_exponential_and_bounded(
    attempt_count,
    delay_seconds,
) -> None:
    from packages.core.tasks import runtime_tasks

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert runtime_tasks._runtime_outbox_retry_available_at(
        now,
        attempt_count=attempt_count,
    ) == now + timedelta(seconds=delay_seconds)


@pytest.mark.integration
async def test_invalid_resume_recovery_rows_do_not_starve_oldest_batch(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import task_runtime_approvals
    from packages.core.tasks import runtime_tasks

    now = datetime.now(timezone.utc)
    older = RuntimeOutboxEvent(
        event_type=task_runtime_approvals.TASK_RUNTIME_RESUME_EVENT,
        aggregate_id="invalid-resume-older",
        dedupe_key=f"invalid-resume:{generate_ulid()}",
        payload={"task_id": "task-older"},
        delivered_at=now - timedelta(minutes=10),
        available_at=now - timedelta(minutes=10),
    )
    newer = RuntimeOutboxEvent(
        event_type=task_runtime_approvals.TASK_RUNTIME_RESUME_EVENT,
        aggregate_id="invalid-resume-newer",
        dedupe_key=f"invalid-resume:{generate_ulid()}",
        payload={"task_id": "task-newer", "runtime_hitl_id": "hitl-newer"},
        delivered_at=now - timedelta(minutes=9),
        available_at=now - timedelta(minutes=9),
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    async def invalid_delivery(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        task_runtime_approvals,
        "validate_task_runtime_resume_delivery",
        invalid_delivery,
    )

    assert await runtime_tasks._recover_task_runtime_resume_outbox(
        db_session,
        now=now,
        batch_size=1,
    ) == 0
    await db_session.flush()
    assert older.last_error == runtime_tasks._TASK_RUNTIME_RESUME_INVALID_SENTINEL
    assert newer.last_error is None

    assert await runtime_tasks._recover_task_runtime_resume_outbox(
        db_session,
        now=now,
        batch_size=1,
    ) == 0
    await db_session.flush()
    assert newer.last_error == runtime_tasks._TASK_RUNTIME_RESUME_INVALID_SENTINEL


@pytest.mark.integration
async def test_runtime_execution_claim_is_exclusive_and_terminal_write_is_idempotent(db_session) -> None:
    now = datetime.now(timezone.utc)
    run = _run(run_id=generate_ulid())
    db_session.add(run)
    await db_session.flush()

    claimed = await claim_runtime_run_execution(
        db_session,
        run_id=run.id,
        worker_id="worker-a",
        now=now,
        lease_seconds=60,
    )
    denied = await claim_runtime_run_execution(
        db_session,
        run_id=run.id,
        worker_id="worker-b",
        now=now + timedelta(seconds=1),
        lease_seconds=60,
    )

    assert claimed is run
    assert run.status == RuntimeRunStatus.RUNNING.value
    assert denied is None

    completed = await complete_runtime_run_execution(
        db_session,
        run_id=run.id,
        worker_id="worker-a",
        result={"message_id": run.assistant_message_id},
        now=now + timedelta(seconds=2),
    )
    repeated = await complete_runtime_run_execution(
        db_session,
        run_id=run.id,
        worker_id="worker-a",
        result={"message_id": "late-retry"},
        now=now + timedelta(seconds=3),
    )

    assert completed.status == RuntimeRunStatus.COMPLETED.value
    assert repeated.result == {"message_id": run.assistant_message_id}


@pytest.mark.integration
async def test_expired_runtime_lease_is_requeued_once_with_versioned_outbox(db_session) -> None:
    now = datetime.now(timezone.utc)
    run = _run(run_id=generate_ulid())
    run.status = RuntimeRunStatus.RUNNING.value
    run.version = 2
    run.lease_owner = "worker-from-terminated-pod"
    run.lease_expires_at = now - timedelta(seconds=1)
    db_session.add(run)
    await db_session.flush()

    recovered, failed = await recover_expired_runtime_run_leases(
        db_session,
        now=now,
    )
    repeated = await recover_expired_runtime_run_leases(
        db_session,
        now=now,
    )

    outbox = list(
        (
            await db_session.execute(
                select(RuntimeOutboxEvent).where(
                    RuntimeOutboxEvent.aggregate_id == run.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert (recovered, failed) == (1, 0)
    assert repeated == (0, 0)
    assert run.status == RuntimeRunStatus.QUEUED.value
    assert run.lease_owner is None
    assert run.lease_expires_at is None
    assert run.version == 3
    assert len(outbox) == 1
    assert outbox[0].dedupe_key == f"runtime.execute_run:{run.id}:recovery:3"
    assert outbox[0].payload == {"run_id": run.id}


@pytest.mark.integration
async def test_runtime_lease_recovery_is_bounded_and_finalizes_expired_cancel(db_session) -> None:
    now = datetime.now(timezone.utc)
    exhausted = _run(run_id=generate_ulid())
    exhausted.status = RuntimeRunStatus.RUNNING.value
    exhausted.lease_owner = "lost-worker"
    exhausted.lease_expires_at = now - timedelta(seconds=2)
    cancelled = _run(run_id=generate_ulid())
    cancelled.status = RuntimeRunStatus.CANCEL_REQUESTED.value
    cancelled.lease_owner = "lost-cancel-worker"
    cancelled.lease_expires_at = now - timedelta(seconds=1)
    db_session.add_all([exhausted, cancelled])
    for attempt in range(3):
        db_session.add(
            RuntimeOutboxEvent(
                event_type="runtime.execute_run",
                aggregate_id=exhausted.id,
                dedupe_key=(
                    f"runtime.execute_run:{exhausted.id}:recovery:{attempt + 2}"
                ),
                payload={"run_id": exhausted.id},
                available_at=now,
                created_at=now,
            )
        )
    await db_session.flush()

    recovered, failed = await recover_expired_runtime_run_leases(
        db_session,
        now=now,
    )

    assert (recovered, failed) == (0, 1)
    assert exhausted.status == RuntimeRunStatus.FAILED.value
    assert exhausted.status_reason == "runtime_recovery_exhausted"
    assert exhausted.error["code"] == "runtime_recovery_exhausted"
    assert exhausted.completed_at == now
    assert cancelled.status == RuntimeRunStatus.CANCELLED.value
    assert cancelled.status_reason == "user_cancelled"
    assert cancelled.completed_at == now


@pytest.mark.integration
async def test_status_and_cancel_endpoints_enforce_owner_and_cancel_once(db_session, monkeypatch) -> None:
    run = _run(run_id=generate_ulid())
    db_session.add(run)
    await db_session.flush()
    owner = SimpleNamespace(id=run.user_id, entity_id=run.entity_id, role="member")
    outsider = SimpleNamespace(id="user_outsider", entity_id=run.entity_id, role="member")

    status = await get_chat_runtime_run_status(run.id, owner, db_session)
    assert status["id"] == run.id
    assert status["status"] == RuntimeRunStatus.QUEUED.value
    with pytest.raises(HTTPException) as denied:
        await get_chat_runtime_run_status(run.id, outsider, db_session)
    assert denied.value.status_code == 404

    cancelled_resources: list[str] = []

    async def fake_cancel_resources(cancelled_run):
        cancelled_resources.append(cancelled_run.id)

    monkeypatch.setattr(
        "apps.api.routers.chat.cancel_runtime_run_resources",
        fake_cancel_resources,
    )
    first_cleanup = BackgroundTasks()
    second_cleanup = BackgroundTasks()
    first = await cancel_chat_runtime_run(run.id, first_cleanup, owner, db_session)
    second = await cancel_chat_runtime_run(run.id, second_cleanup, owner, db_session)

    assert first["status"] == RuntimeRunStatus.CANCELLED.value
    assert second["status"] == RuntimeRunStatus.CANCELLED.value
    assert cancelled_resources == []
    await first_cleanup()
    await second_cleanup()
    assert cancelled_resources == [run.id, run.id]


@pytest.mark.integration
async def test_cancel_endpoint_keeps_running_run_nonterminal_until_worker_stops(
    db_session,
    monkeypatch,
) -> None:
    run = _run(run_id=generate_ulid())
    run.status = RuntimeRunStatus.RUNNING.value
    run.lease_owner = "worker-a"
    run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    db_session.add(run)
    await db_session.flush()
    owner = SimpleNamespace(id=run.user_id, entity_id=run.entity_id, role="member")

    monkeypatch.setattr(
        "apps.api.routers.chat.cancel_runtime_run_resources",
        AsyncMock(),
    )

    cancelled = await cancel_chat_runtime_run(run.id, BackgroundTasks(), owner, db_session)

    assert cancelled["id"] == run.id
    assert cancelled["status"] == RuntimeRunStatus.CANCEL_REQUESTED.value
    assert cancelled["completed_at"] is None


@pytest.mark.unit
async def test_cancel_response_does_not_wait_for_sandbox_cleanup(monkeypatch) -> None:
    """A slow runner must not turn a committed stop into a browser timeout."""
    run = _run(run_id="run-slow-cleanup")
    run.status = RuntimeRunStatus.CANCEL_REQUESTED.value
    db = SimpleNamespace(transaction_open=True)

    async def commit():
        db.transaction_open = False

    async def project_status(*_args, **_kwargs):
        # Queue and human-input projections may query the database.
        db.transaction_open = True
        return {"id": run.id, "status": run.status}

    db.commit = AsyncMock(side_effect=commit)
    owner = SimpleNamespace(id=run.user_id, entity_id=run.entity_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup(_run):
        db.commit.assert_awaited_once()
        assert not db.transaction_open
        cleanup_started.set()
        await release_cleanup.wait()

    monkeypatch.setattr("apps.api.routers.chat._get_owned_runtime_run", AsyncMock(return_value=run))
    monkeypatch.setattr("apps.api.routers.chat.request_runtime_run_cancel", AsyncMock(return_value=run))
    monkeypatch.setattr("apps.api.routers.chat.cancel_runtime_run_resources", cleanup)
    monkeypatch.setattr(
        "apps.api.routers.chat.project_runtime_run_status",
        project_status,
    )
    background = BackgroundTasks()
    response = await asyncio.wait_for(
        cancel_chat_runtime_run(run.id, background, owner, db), timeout=0.2,
    )
    assert response == {"id": run.id, "status": "cancel_requested"}
    db.commit.assert_awaited_once()
    assert not db.transaction_open, "Slow background cleanup must not retain a DB transaction"
    assert not cleanup_started.is_set()
    release_cleanup.set()
    await background()
    assert cleanup_started.is_set()


@pytest.mark.unit
async def test_terminal_runtime_stream_drains_events_appended_after_status_read(monkeypatch) -> None:
    run = _run(run_id="01RUNTIMETERMINALDRAIN0001")
    first_frame = 'id: 1-0\nevent: text_delta\ndata: {"text":"hello"}\n\n'
    middle_frame = 'id: 2-0\nevent: text_delta\ndata: {"text":" world"}\n\n'
    final_frame = 'id: 3-0\nevent: stream_end\ndata: {"run_id":"run"}\n\n'
    reads: list[tuple[str, int | None]] = []

    async def fake_read(run_id: str, *, after_id: str, block_ms: int | None = 4000):
        reads.append((after_id, block_ms))
        if len(reads) == 1:
            return [("1-0", first_frame)]
        if len(reads) == 2:
            return [("2-0", middle_frame)]
        if len(reads) == 3:
            return [("3-0", final_frame)]
        return []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _run_id):
            return SimpleNamespace(
                status=RuntimeRunStatus.COMPLETED.value,
                status_reason=None,
            )

    monkeypatch.setattr(
        "packages.core.services.runtime_event_stream.read_runtime_sse_events",
        fake_read,
    )
    monkeypatch.setattr("packages.core.database.async_session", FakeSession)

    frames = [frame async for frame in _durable_runtime_event_stream(run)]

    assert frames[-3:] == [first_frame, middle_frame, final_frame]
    assert reads == [
        ("0-0", 4000),
        ("1-0", None),
        ("2-0", None),
        ("3-0", None),
    ]


@pytest.mark.unit
async def test_runtime_stream_releases_after_stream_end_before_status_commit(monkeypatch) -> None:
    run = _run(run_id="01RUNTIMESTREAMENDRELEASE01")
    text_frame = 'id: 1-0\nevent: text_delta\ndata: {"text":"hello"}\n\n'
    end_frame = 'id: 2-0\nevent: stream_end\ndata: {"run_id":"run"}\n\n'
    reads: list[tuple[str, int | None]] = []

    async def fake_read(run_id: str, *, after_id: str, block_ms: int | None = 4000):
        reads.append((after_id, block_ms))
        if len(reads) == 1:
            return [("1-0", text_frame), ("2-0", end_frame)]
        assert block_ms is None
        return []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _run_id):
            return SimpleNamespace(
                status=RuntimeRunStatus.RUNNING.value,
                status_reason=None,
            )

    monkeypatch.setattr(
        "packages.core.services.runtime_event_stream.read_runtime_sse_events",
        fake_read,
    )
    monkeypatch.setattr("packages.core.database.async_session", FakeSession)

    frames = [frame async for frame in _durable_runtime_event_stream(run)]

    assert frames[-2:] == [text_frame, end_frame]
    assert reads == [("0-0", 4000), ("2-0", None)]


@pytest.mark.integration
async def test_reconnect_runtime_stream_uses_chat_concurrency_lease(db_session, monkeypatch) -> None:
    run = _run(run_id=generate_ulid())
    db_session.add(run)
    await db_session.flush()
    owner = SimpleNamespace(id=run.user_id, entity_id=run.entity_id, role="member")
    scopes: list[str] = []

    class FakeLease:
        def wrap(self, stream):
            return stream

    async def fake_acquire(*, scope: str):
        scopes.append(scope)
        return FakeLease()

    monkeypatch.setattr("apps.api.routers.chat.acquire_chat_stream_lease", fake_acquire)

    response = await reconnect_chat_runtime_run_events(
        run.id,
        SimpleNamespace(headers={}),
        None,
        owner,
        db_session,
    )

    assert response.media_type == "text/event-stream"
    assert scopes == ["chat-reconnect"]


@pytest.mark.unit
async def test_chat_streaming_response_releases_request_db_before_wrapping() -> None:
    from apps.api.routers import chat

    assert hasattr(chat, "_chat_streaming_response")
    lifecycle: list[str] = []

    class FakeDB:
        async def commit(self):
            lifecycle.append("commit")

        async def close(self):
            lifecycle.append("close")

    class FakeLease:
        def wrap(self, source):
            assert lifecycle == ["commit", "close"]
            lifecycle.append("wrap")
            return source

    async def source():
        yield "event: stream_end\ndata: {}\n\n"

    response = await chat._chat_streaming_response(
        FakeDB(),
        FakeLease(),
        source(),
        conversation_id="conversation-1",
        runtime_run_id="runtime-run-1",
        user_message_id="user-message-1",
        client_message_id="client-message-1",
        response_surface_event_id="surface-event-1",
    )

    assert response.media_type == "text/event-stream"
    assert response.headers["X-Conversation-ID"] == "conversation-1"
    assert response.headers["X-Runtime-Run-ID"] == "runtime-run-1"
    assert response.headers["X-User-Message-ID"] == "user-message-1"
    assert response.headers["X-Client-Message-ID"] == "client-message-1"
    assert response.headers["X-Response-Surface-Event-ID"] == "surface-event-1"
    assert lifecycle == ["commit", "close", "wrap"]


@pytest.mark.unit
def test_runtime_event_frames_preserve_sse_type_payload_and_replay_id() -> None:
    event, payload = parse_sse_frame(
        'event: runtime_waiting\ndata: {"run_id":"run-1","position":2}\n\n'
    )

    assert event == "runtime_waiting"
    assert payload == '{"run_id":"run-1","position":2}'
    assert sse_frame_with_id(event, payload, "42-0") == (
        'id: 42-0\nevent: runtime_waiting\ndata: {"run_id":"run-1","position":2}\n\n'
    )


@pytest.mark.unit
async def test_runtime_event_stream_timeout_is_treated_as_an_idle_read() -> None:
    class TimeoutRedis:
        block_ms: int | None = None

        async def xread(self, streams, *, count, block):
            self.block_ms = block
            raise RedisTimeoutError("socket read timeout")

    redis = TimeoutRedis()

    assert await read_runtime_sse_events("run-timeout", redis_client=redis) == []
    assert redis.block_ms == 4000


@pytest.mark.unit
async def test_lease_heartbeat_retries_a_failed_renewal(monkeypatch) -> None:
    """The turn shares the loop's one admission permit; a busy renewal used to
    end the heartbeat silently and let the 90s lease lapse mid-turn."""
    from packages.core.tasks import runtime_tasks

    sleeps: list[float] = []
    outcomes = [RuntimeError("Database admission wait timed out"), True, False]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    async def fake_renew(_db, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.renew_runtime_run_lease", fake_renew,
    )
    monkeypatch.setattr(runtime_tasks.asyncio, "sleep", fake_sleep)

    await runtime_tasks._runtime_run_lease_heartbeat("run-1", "worker-1")

    assert sleeps == [30.0, 5.0, 30.0]
    assert outcomes == []


async def test_publish_runtime_run_outbox_now_publishes_once(monkeypatch) -> None:
    from packages.core.database import async_session
    from packages.core.tasks import runtime_tasks

    run_id = generate_ulid()
    published: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        runtime_tasks.celery_app,
        "send_task",
        lambda name, *, kwargs, queue: published.append((name, kwargs, queue)),
    )
    async with async_session() as db:
        db.add(RuntimeOutboxEvent(
            event_type="runtime.execute_run",
            aggregate_id=run_id,
            dedupe_key=f"runtime.execute_run:{run_id}:1",
            payload={"run_id": run_id},
            available_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    try:
        assert await runtime_tasks.publish_runtime_run_outbox_now(run_id) is True
        # Already delivered: neither this path nor the sweep publishes it again.
        assert await runtime_tasks.publish_runtime_run_outbox_now(run_id) is False
        assert published == [("runtime.execute_run", {"run_id": run_id}, "interactive")]
        async with async_session() as db:
            event = (await db.execute(
                select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == run_id)
            )).scalar_one()
            assert event.delivered_at is not None
            assert event.attempt_count == 1
    finally:
        async with async_session() as db:
            for event in (await db.execute(
                select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == run_id)
            )).scalars():
                await db.delete(event)
            await db.commit()


async def test_heartbeat_failure_does_not_fail_a_finished_run(monkeypatch) -> None:
    from packages.core.tasks import runtime_tasks

    run = SimpleNamespace(
        id="run-heartbeat-error", conversation_id="conversation-heartbeat-error",
        assistant_message_id="message-heartbeat-error", entity_id="entity-1",
        user_id="user-1", agent_id=None, workspace_id=None,
        execution_payload={"message": "hi"}, checkpoint=None,
        status=RuntimeRunStatus.RUNNING.value,
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

        async def get(self, _model, _run_id):
            return run if _run_id == run.id else None

    async def fake_stream(*_args, **_kwargs):
        await asyncio.sleep(0)  # let the heartbeat task run (and fail) first
        yield (
            'event: stream_end\ndata: {"stop_reason":"chrome_cli_worker_not_paired",'
            '"error":"Chrome requires a paired local worker."}\n\n'
        )

    async def failing_heartbeat(*_args, **_kwargs):
        raise RuntimeError("Database admission wait timed out")

    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: lambda: FakeSession())
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.claim_runtime_run_execution",
        AsyncMock(return_value=run),
    )
    monkeypatch.setattr(
        "packages.core.services.runtime_run_service.complete_runtime_run_execution",
        AsyncMock(return_value=SimpleNamespace(status=RuntimeRunStatus.FAILED.value)),
    )
    monkeypatch.setattr("packages.core.ai.runtime.runtime_stream_chat_turn", fake_stream)
    monkeypatch.setattr(runtime_tasks, "append_runtime_sse_event", AsyncMock())
    monkeypatch.setattr(runtime_tasks, "_runtime_run_lease_heartbeat", failing_heartbeat)

    # The turn reached its terminal state; the heartbeat error is logged, not
    # raised (raising made Celery retry and re-run a finished turn).
    result = await runtime_tasks.execute_runtime_run_once(run_id=run.id, worker_id="worker-1")

    assert result["status"] == RuntimeRunStatus.FAILED.value
