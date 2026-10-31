"""Cancellation during context preparation must not become a chat failure."""
from __future__ import annotations

import asyncio
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai.runtime.context_compaction import CHECKPOINT_FIELDS
from tests.async_waits import LIVENESS_TIMEOUT_S, wait_for_signal

loop = importlib.import_module("packages.core.ai.agentic_loop")
pytestmark = pytest.mark.asyncio


@pytest.fixture
def compaction(monkeypatch):
    monkeypatch.setattr(loop, "MAX_CONTEXT_TOKENS", 12_000)
    # Durable chat resolves a real catalog model; cap its window the same way.
    monkeypatch.setenv("MANOR_CONTEXT_WINDOW_CAP_TOKENS", "12000")
    # Small-history cases exercise the opt-in message-count guard (lossless path).
    monkeypatch.setattr(loop, "MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    summary ={field: [] for field in CHECKPOINT_FIELDS}
    summary["objective"] = ["Remove empty spreadsheet rows without changing other cells."]
    model = AsyncMock(return_value=(json.dumps(summary), {"prompt": 100, "completion": 20, "total": 120}))
    normal = AsyncMock(return_value=("Done", {}))
    monkeypatch.setattr(loop, "runtime_execute_agentic_compaction_completion", model)
    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", normal)
    return model, normal


def history(*, large=True):
    return [message for index in range(26) for message in (
        {"role": "user", "content": f"Previous request {index}"},
        {"role": "assistant", "content": "previous answer " + ("x" * 6_000 if large else "")},
    )]


async def run(history_messages, **kwargs):
    return await loop.agentic_loop(
        system_prompt="Keep the original file safe.", user_message="Remove empty rows.",
        initial_messages=history_messages, tools=[], tool_executor=AsyncMock(),
        max_rounds=1, max_tokens=1_000, **kwargs,
    )


@pytest.mark.parametrize("checker", ["is_cancelled", "runtime_cancel_checker"])
async def test_cancel_interrupts_inflight_compaction(compaction, checker):
    model, normal = compaction
    started, stopped, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    checkpoint = AsyncMock()
    original = history()
    snapshot = deepcopy(original)

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.side_effect = blocked
    task = asyncio.create_task(run(original, on_context_checkpoint=checkpoint, **{checker: cancelled.is_set}))
    try:
        await wait_for_signal(started, task)
        cancelled.set()
        # The blocked model never returns: only cancellation can end the task.
        # Its checker is polled once a second, so this bound is a hang guard.
        result = await asyncio.wait_for(asyncio.shield(task), LIVENESS_TIMEOUT_S)
        assert result.stop_reason == "cancelled"
        assert result.error == "chat_turn_cancelled"
        assert result.control == {"kind": "cancelled", "reason": "user_cancelled"}
        assert result.tool_calls_made == []
        assert stopped.is_set(), "No detached provider request may survive cancellation"
        assert model.await_count == 1
        normal.assert_not_awaited()
        checkpoint.assert_not_awaited()
        assert original == snapshot
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_between_batches_preserves_completed_usage(compaction):
    model, normal = compaction
    cancelled = asyncio.Event()
    checkpoint = AsyncMock()
    response = model.return_value

    async def finish_one_batch(*_args, **_kwargs):
        cancelled.set()
        return response

    model.side_effect = finish_one_batch
    result = await run(history(), is_cancelled=cancelled.is_set, on_context_checkpoint=checkpoint)
    assert result.stop_reason == "cancelled"
    assert result.usage["total"] == 120
    assert model.await_count == 1
    normal.assert_not_awaited()
    checkpoint.assert_not_awaited()


async def test_response_arriving_during_cancel_query_keeps_usage(compaction):
    model, normal = compaction
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    response = model.return_value

    async def complete(*_args, **_kwargs):
        started.set()
        await release.wait()
        finished.set()
        return response

    async def is_cancelled():
        if not started.is_set():
            return False
        release.set()
        await finished.wait()
        return True

    model.side_effect = complete
    result = await asyncio.wait_for(run(history(), is_cancelled=is_cancelled), LIVENESS_TIMEOUT_S)
    assert result.stop_reason == "cancelled"
    assert result.usage["total"] == 120
    assert model.await_count == 1
    normal.assert_not_awaited()


@pytest.mark.parametrize("check_fails", [False, True])
async def test_no_confirmed_cancellation_keeps_ordinary_compaction(compaction, check_fails):
    model, normal = compaction

    async def is_cancelled():
        if check_fails:
            raise ConnectionError("Cancellation state temporarily unavailable")
        return False

    result = await run(history(), is_cancelled=is_cancelled)
    assert result.stop_reason == "completed"
    assert result.content == "Done"
    assert result.usage["total"] == 120 * model.await_count
    assert model.await_count > 1
    normal.assert_awaited_once()


@pytest.mark.parametrize("phase", ["archive", "prepare", "refresh", "checkpoint"])
async def test_cancel_during_context_hooks_does_not_publish_or_accept(compaction, phase):
    model, normal = compaction
    cancelled = asyncio.Event()
    events = []

    async def hook(name, value=None):
        events.append(name)
        if phase == name:
            cancelled.set()
            if name in {"archive", "checkpoint"}:
                raise RuntimeError("Runtime context writer no longer owns the active run.")
        return value

    async def archive(_messages):
        return await hook("archive", "archive-id")

    async def prepare(_messages, _archive_id):
        await hook("prepare")

    async def refresh(messages):
        return await hook("refresh", messages)

    async def checkpoint(_messages):
        await hook("checkpoint")

    memory = SimpleNamespace(prepare=prepare, refresh=refresh, accept=Mock())
    result = await run(
        history(large=False), is_cancelled=cancelled.is_set,
        on_context_compacted=archive, on_context_checkpoint=checkpoint, context_memory=memory,
    )
    assert result.stop_reason == "cancelled"
    phases = ["archive", "prepare", "refresh", "checkpoint"]
    assert events == phases[:phases.index(phase) + 1]
    memory.accept.assert_not_called()
    model.assert_not_awaited()
    normal.assert_not_awaited()


async def test_context_ownership_failure_without_cancel_is_not_hidden(compaction):
    from packages.core.ai.runtime.control import RuntimeContextWriterFenced

    checkpoint = AsyncMock(side_effect=RuntimeContextWriterFenced("Runtime context writer no longer owns the active run."))
    with pytest.raises(RuntimeContextWriterFenced):
        await run(history(large=False), is_cancelled=lambda: False, on_context_checkpoint=checkpoint)
    compaction[1].assert_not_awaited()


async def test_checkpoint_storage_failure_keeps_the_turn_running(compaction):
    _, normal = compaction
    checkpoint = AsyncMock(side_effect=RuntimeError("database unavailable"))
    memory = SimpleNamespace(prepare=AsyncMock(), refresh=AsyncMock(side_effect=lambda messages: messages), accept=Mock())
    result = await run(
        history(large=False), is_cancelled=lambda: False,
        on_context_checkpoint=checkpoint, context_memory=memory,
    )
    assert result.stop_reason == "completed"
    normal.assert_awaited()
    # Without a durable checkpoint the memory manifest must not advance.
    memory.accept.assert_not_called()


async def test_outer_task_cancel_drains_compaction_provider(compaction):
    model, normal = compaction
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.side_effect = blocked
    task = asyncio.create_task(run(history(), is_cancelled=lambda: False))
    try:
        await wait_for_signal(started, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        normal.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("prior_checkpoint", [False, True])
async def test_durable_chat_cancel_persists_interrupted_and_streams_cancelled(
    client, monkeypatch, compaction, prior_checkpoint,
):
    from sqlalchemy import select

    from packages.core import database
    from packages.core.ai import runtime
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.models.agent_delegation_session import AgentContextArchive
    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import Entity, User
    from packages.core.services import chat_service
    from packages.core.services.runtime_event_stream import parse_sse_frame
    from packages.core.services.runtime_run_service import create_runtime_run, request_runtime_run_cancel
    from packages.core.tasks import runtime_tasks

    model, normal = compaction
    started, stopped = asyncio.Event(), asyncio.Event()
    frames = []
    initial = history()
    if prior_checkpoint:
        initial.insert(0, {"role": "user", "content": CHECKPOINT_PREFIX + "\nHistorical data\n" + json.dumps({
            "records": [{"role": "user", "content": "Keep the original spreadsheet."}],
        })})

    async with database.async_session() as db:
        db.add(Entity(id="cancel-entity", name="Cancellation test"))
        db.add(User(id="cancel-user", entity_id="cancel-entity", email="cancel@test.invalid", password_hash="test", role="owner"))
        db.add(Conversation(id="cancel-conversation", entity_id="cancel-entity", user_id="cancel-user"))
        db.add(Message(id="cancel-assistant", conversation_id="cancel-conversation", role="assistant", content="", meta={"stream_status": "streaming"}))
        run_row = await create_runtime_run(
            db, conversation_id="cancel-conversation", entity_id="cancel-entity", user_id="cancel-user",
            assistant_message_id="cancel-assistant", execution_payload={"message": "Remove empty rows."},
        )
        run_id = run_row.id
        await db.commit()

    ctx = ChatContext(
        entity_id="cancel-entity", user_id="cancel-user", agent_id=None,
        conversation_id="cancel-conversation",
    )
    ctx.max_tokens = 1_000

    async def blocked(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def stream(message, conversation_id, *, surface, **kwargs):
        async for frame in chat_service.stream_chat_response(
            message, conversation_id, runtime_surface=runtime.ChatSurface.GLOBAL_OWNER_CHAT, **kwargs,
        ):
            yield frame

    async def append(_run_id, frame):
        event, payload = parse_sse_frame(frame)
        if event == "stream_end":
            async with database.async_session() as db:
                assert (await db.get(RuntimeRun, run_id)).status == "cancelled"
        frames.append((event, json.loads(payload)))

    model.side_effect = blocked
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", AsyncMock(return_value=("Keep files safe.", [], initial, ctx)))
    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", AsyncMock(return_value=("test-model", {}, None)))
    monkeypatch.setattr(database, "create_worker_session", lambda: database.async_session)
    monkeypatch.setattr(runtime, "runtime_stream_chat_turn", stream)
    monkeypatch.setattr(runtime_tasks, "append_runtime_sse_event", append)
    task = asyncio.create_task(runtime_tasks.execute_runtime_run_once(run_id=run_id, worker_id="cancel-worker"))
    try:
        await wait_for_signal(started, task)
        async with database.async_session() as db:
            cancelled = await request_runtime_run_cancel(db, run_id=run_id, entity_id="cancel-entity", user_id="cancel-user")
            assert cancelled.status == "cancel_requested"
            await db.commit()
        # A hang guard: the run sees the request at its next once-a-second
        # check, then persists the interrupted reply and terminal state.
        result = await asyncio.wait_for(asyncio.shield(task), LIVENESS_TIMEOUT_S)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS), return_exceptions=True)

    assert result["status"] == "cancelled"
    assert stopped.is_set()
    normal.assert_not_awaited()
    assert model.await_count == 1
    assert all(event != "error" for event, _ in frames)
    terminal = [payload for event, payload in frames if event == "stream_end"]
    assert len(terminal) == 1
    assert terminal[0]["stop_reason"] == "cancelled"
    assert terminal[0]["status"] == "cancelled"
    assert "error" not in terminal[0]
    assert terminal[0]["persisted"] is True
    async with database.async_session() as db:
        saved = await db.get(Message, "cancel-assistant")
        assert saved.meta["stream_status"] == "interrupted"
        assert saved.meta["stream_interrupted"] is True
        assert not saved.tool_calls
        assert "Please try again" not in saved.content
        finished = await db.get(RuntimeRun, run_id)
        assert finished.error is None
        assert finished.context_state is None
        assert finished.lease_owner is None
        archives = (await db.scalars(select(AgentContextArchive).where(AgentContextArchive.runtime_run_id == run_id))).all()
        assert len(archives) == 1
