from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core import database
from packages.core.models import Entity
from packages.core.models.task import Conversation, Message
from packages.core.services import conversation_history as history


@pytest.mark.asyncio
async def test_history_backfills_rows_outside_recent_window_once(client, monkeypatch):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database.async_session() as db:
        db.add(Entity(id="summary-window-entity", name="Summary window test"))
        db.add(Conversation(id="summary-window", entity_id="summary-window-entity"))
        db.add_all([
            Message(
                id=f"summary-window-{index:04}", conversation_id="summary-window",
                role="user", content=f"durable fact {index:04}", created_at=timestamp,
            )
            for index in range(205)
        ])
        await db.commit()

    scheduled = []
    monkeypatch.setattr(history, "_schedule_conversation_summary", lambda conv, **kw: scheduled.append((conv, kw)))
    completion = AsyncMock(return_value=SimpleNamespace(content="Remember durable fact 0000."))
    monkeypatch.setattr(history, "runtime_execute_conversation_summary_completion", completion)

    async with database.async_session() as db:
        recent = await history.load_conversation_history(db, "summary-window")
    assert len(recent) == 200
    assert recent[0]["content"] == "durable fact 0005"
    assert len(scheduled) == 1
    conv_id, cutoff = scheduled[0]
    await history.update_conversation_summary(conv_id, **cutoff)
    await history.update_conversation_summary(conv_id, **cutoff)

    completion.assert_awaited_once()
    supplied = completion.call_args.kwargs["text_block"]
    assert "durable fact 0000" in supplied and "durable fact 0004" in supplied
    assert "durable fact 0005" not in supplied
    async with database.async_session() as db:
        conversation = await db.get(Conversation, conv_id)
        assert conversation.summary_covered_message_id == "summary-window-0004"
        assert conversation.summary_version == 1

    # A larger history budget/window must not reintroduce covered raw rows.
    monkeypatch.setattr(history, "MAX_HISTORY_ROWS", 250)
    async with database.async_session() as db:
        restored = await history.load_conversation_history(db, conv_id)
    assert restored[0]["content"].startswith("[Earlier conversation summary]")
    assert restored[1:] == recent


@pytest.mark.asyncio
async def test_streaming_reply_does_not_hide_a_full_history_window(client, monkeypatch):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database.async_session() as db:
        db.add(Entity(id="stream-window-entity", name="Streaming window"))
        db.add(Conversation(id="stream-window", entity_id="stream-window-entity"))
        db.add_all([
            Message(
                id=f"stream-window-{index:04}", conversation_id="stream-window",
                role="assistant" if index == 204 else "user",
                content=f"fact {index:04}", created_at=timestamp,
                meta={"stream_status": "running"} if index == 204 else {},
            )
            for index in range(205)
        ])
        await db.commit()

    completion = AsyncMock(return_value=SimpleNamespace(content="Remember fact 0000."))
    monkeypatch.setattr(history, "runtime_execute_conversation_summary_completion", completion)
    async with database.async_session() as db:
        recent = await history.load_conversation_history(db, "stream-window")
    assert len(recent) == 199
    assert recent[0]["content"] == "fact 0005"
    pending = history._summary_tasks.get("stream-window")
    assert pending is not None, "The running reply must not hide the older page"
    await pending

    completion.assert_awaited_once()
    supplied = completion.call_args.kwargs["text_block"]
    assert "fact 0000" in supplied and "fact 0004" in supplied
    assert "fact 0005" not in supplied and "fact 0204" not in supplied
    async with database.async_session() as db:
        conversation = await db.get(Conversation, "stream-window")
        assert conversation.summary_covered_message_id == "stream-window-0004"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_local_summary_task_is_single_flight_and_released(monkeypatch, cancelled):
    ready = asyncio.Event()
    release = asyncio.Event()

    async def update(*_args, **_kwargs):
        ready.set()
        await release.wait()

    update_mock = AsyncMock(side_effect=update)
    monkeypatch.setattr(history, "update_conversation_summary", update_mock)
    cutoff = {"cutoff_at": datetime.now(timezone.utc), "cutoff_message_id": "last"}
    history._schedule_conversation_summary("single-flight", **cutoff)
    task = history._summary_tasks["single-flight"]
    await ready.wait()
    history._schedule_conversation_summary("single-flight", **cutoff)
    assert history._summary_tasks["single-flight"] is task
    update_mock.assert_awaited_once()
    if cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    await asyncio.sleep(0)
    assert "single-flight" not in history._summary_tasks
