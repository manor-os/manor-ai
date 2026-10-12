"""Cancel the durable turn while context discovery is still waiting on I/O."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.ai import runtime
from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime.prompt_adapter import ChatContext
from packages.core.models.runtime_run import RuntimeRun
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity, User
from packages.core.services import chat_service
from packages.core.services.runtime_event_stream import parse_sse_frame
from packages.core.services.runtime_run_service import create_runtime_run, request_runtime_run_cancel
from packages.core.tasks import runtime_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["context", "cancelled_result", "recommendation_pending"])
async def test_cancel_drains_preparation_and_commits_terminal_before_publication(client, monkeypatch, phase):
    started, drained = asyncio.Event(), asyncio.Event()
    frames = []
    async with database.async_session() as db:
        db.add(Entity(id="prepare-entity", name="Cancellation test"))
        db.add(User(id="prepare-user", entity_id="prepare-entity", email="prepare@test.invalid", password_hash="test", role="owner"))
        db.add(Conversation(id="prepare-conversation", entity_id="prepare-entity", user_id="prepare-user"))
        db.add(Message(id="prepare-assistant", conversation_id="prepare-conversation", role="assistant", content="", meta={"stream_status": "streaming"}))
        run = await create_runtime_run(
            db, conversation_id="prepare-conversation", entity_id="prepare-entity", user_id="prepare-user",
            assistant_message_id="prepare-assistant", execution_payload={"message": "Cancellation regression"},
        )
        run_id = run.id
        await db.commit()

    async def context(db, *_args, **_kwargs):
        # Exercise real session cleanup too: cancellation must release its
        # transaction before the worker commits the terminal run and Message.
        await db.scalar(select(Conversation.id).where(Conversation.id == "prepare-conversation"))
        if phase == "context":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        return "system", [], [], ChatContext(entity_id="prepare-entity", user_id="prepare-user")

    async def cancelled_loop(**kwargs):
        if phase == "recommendation_pending":
            return AgenticResult(content="Finished", messages=[], usage={}, rounds=1, tool_calls_made=[])
        started.set()
        while not await kwargs["runtime_cancel_checker"]():
            await asyncio.sleep(0.01)
        return AgenticResult(content="", messages=[], usage={}, rounds=0, tool_calls_made=[], stop_reason="cancelled")

    async def recommendation(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

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

    loop = AsyncMock(side_effect=cancelled_loop)
    recommend = AsyncMock(side_effect=recommendation)
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", context)
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", loop)
    monkeypatch.setattr(chat_service._TurnWorkspaceRecommendation, "get", recommend)
    monkeypatch.setattr(database, "create_worker_session", lambda: database.async_session)
    monkeypatch.setattr(runtime, "runtime_stream_chat_turn", stream)
    monkeypatch.setattr(runtime_tasks, "append_runtime_sse_event", append)
    task = asyncio.create_task(runtime_tasks.execute_runtime_run_once(run_id=run_id, worker_id="prepare-worker"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with database.async_session() as db:
            await request_runtime_run_cancel(db, run_id=run_id, entity_id="prepare-entity", user_id="prepare-user")
            await db.commit()
        result = await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS), return_exceptions=True)

    assert result["status"] == "cancelled"
    if phase == "context":
        assert drained.is_set()
        loop.assert_not_awaited()
    if phase == "recommendation_pending":
        recommend.assert_awaited_once()
        assert drained.is_set()
    else:
        recommend.assert_not_awaited()
    terminal = [payload for event, payload in frames if event == "stream_end"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "cancelled"
    assert terminal[0]["stop_reason"] == "cancelled"
    assert terminal[0]["persisted"] is True
    assert all(event != "error" for event, _ in frames)
    async with database.async_session() as db:
        saved = await db.get(Message, "prepare-assistant")
        assert saved.meta["stream_status"] == "interrupted"
        assert saved.meta["assistant_blocks"][0]["lifecycle_status"] == "cancelled"
        finished = await db.get(RuntimeRun, run_id)
        assert finished.error is None
        assert finished.lease_owner is None
