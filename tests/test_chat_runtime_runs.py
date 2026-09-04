from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import HTTPException
from redis.exceptions import TimeoutError as RedisTimeoutError

from apps.api.routers.chat import (
    _durable_runtime_event_stream,
    cancel_chat_runtime_run,
    get_chat_runtime_run_status,
    reconnect_chat_runtime_run_events,
)
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.base import generate_ulid
from packages.core.services.runtime_event_stream import (
    parse_sse_frame,
    read_runtime_sse_events,
    sse_frame_with_id,
)
from packages.core.services.runtime_run_service import (
    claim_runtime_run_execution,
    complete_runtime_run_execution,
)
from packages.core.tasks.runtime_tasks import _runtime_terminal_error_from_frame


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
            return run

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
    first = await cancel_chat_runtime_run(run.id, owner, db_session)
    second = await cancel_chat_runtime_run(run.id, owner, db_session)

    assert first["status"] == RuntimeRunStatus.CANCELLED.value
    assert second["status"] == RuntimeRunStatus.CANCELLED.value
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

    cancelled = await cancel_chat_runtime_run(run.id, owner, db_session)

    assert cancelled["id"] == run.id
    assert cancelled["status"] == RuntimeRunStatus.CANCEL_REQUESTED.value
    assert cancelled["completed_at"] is None


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
        response_surface_event_id="surface-event-1",
    )

    assert response.media_type == "text/event-stream"
    assert response.headers["X-Conversation-ID"] == "conversation-1"
    assert response.headers["X-Runtime-Run-ID"] == "runtime-run-1"
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
