"""The channel a reloaded page uses to follow a turn it is no longer streaming.

A personal conversation streams over the SSE body of the POST that started it.
Reload mid-reply and that connection dies while the turn keeps running — and
personal conversations get no other push (``_publish_workspace_chat_message_event``
returns early without a ``workspace_id``, and mid-stream DB checkpoints suppress
their broadcast on purpose). ``chat_stream_snapshot`` is that missing channel.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages.core.ai.agentic_loop import AgenticResult
from packages.core.ai.runtime import ChatSurface
from packages.core.services import chat_service
from packages.core.services.chat_service import (
    _tool_calls_for_snapshot,
    stream_chat_response,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def _context_factory(workspace_id: str | None):
    async def fake_context(*_args, **_kwargs):
        ctx = SimpleNamespace(
            workspace_id=workspace_id,
            task_id=None,
            runtime_envelope=None,
            tool_profile=None,
            allowed_tool_names=None,
            user=None,
            entity=None,
        )
        return "system", [], [], ctx

    return fake_context


async def _loop_with_one_tool(**kwargs):
    """Drive the callbacks a real agentic loop drives, in the same order."""
    on_tool_start = kwargs["on_tool_start"]
    on_tool_end = kwargs["on_tool_end"]
    stream_handler = kwargs["stream_handler"]

    on_tool_start("bash", {"command": "ls"})
    on_tool_end("bash", "file-a\nfile-b", 1200, {"command": "ls"})
    await stream_handler(
        "text_delta",
        {"content": "Here is the answer, written out at durable length."},
    )
    return AgenticResult(
        content="Here is the answer, written out at durable length.",
        messages=[],
        usage={},
        rounds=1,
        tool_calls_made=["bash"],
    )


async def _run_turn(
    *,
    workspace_id: str | None,
    surface: ChatSurface,
    user_id: str = "user_1",
    loop=_loop_with_one_tool,
):
    redis = _FakeRedis()

    async def fake_save(**_kwargs):
        return "msg_1"

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=_context_factory(workspace_id),
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=loop,
        ),
        patch(
            "packages.core.services.chat_service.save_or_update_assistant_stream_message",
            new=fake_save,
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_stream_runtime_events",
            new=AsyncMock(),
        ),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
        patch("packages.core.cache._get_redis", new=AsyncMock(return_value=redis)),
    ):
        async for _raw in stream_chat_response(
            "run it",
            "conv_1",
            entity_id="ent_1",
            user_id=user_id,
            agent_id=None,
            assistant_message_id="msg_1",
            persist_messages=True,
            runtime_surface=surface,
        ):
            pass
        # Snapshots are fire-and-forget tasks so a sync tool callback can push
        # one; drain them before asserting.
        pending = list(chat_service._SNAPSHOT_PUBLISH_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return [json.loads(payload) for _channel, payload in redis.published]


async def _run_turn_with_disconnect(*, loop, events_before_disconnect: int = 1):
    """Consume a few SSE events, then close the generator like a dead browser.

    This is the path the whole feature exists for: the StreamingResponse body
    dies with the page, GeneratorExit lands at the yield, and the finally block
    decides between detaching the loop task (a tool already started) and
    cancelling it (still idle).
    """
    redis = _FakeRedis()
    error_saves: list[dict] = []
    interrupted_saves: list[dict] = []

    async def fake_save(**_kwargs):
        return "msg_1"

    async def fake_error_save(**kwargs):
        error_saves.append(kwargs)
        return "msg_1"

    async def fake_interrupted_save(**kwargs):
        interrupted_saves.append(kwargs)
        return "msg_1"

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=_context_factory(None),
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=loop,
        ),
        patch(
            "packages.core.services.chat_service.save_or_update_assistant_stream_message",
            new=fake_save,
        ),
        patch(
            "packages.core.services.chat_service.save_assistant_stream_error_message",
            new=fake_error_save,
        ),
        patch(
            "packages.core.services.chat_service.save_assistant_stream_interrupted_message",
            new=fake_interrupted_save,
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_stream_runtime_events",
            new=AsyncMock(),
        ),
        patch("packages.core.services.chat_service.record_chat_llm_usage", new=AsyncMock()),
        patch("packages.core.cache._get_redis", new=AsyncMock(return_value=redis)),
    ):
        generator = stream_chat_response(
            "run it",
            "conv_1",
            entity_id="ent_1",
            user_id="user_1",
            agent_id=None,
            assistant_message_id="msg_1",
            persist_messages=True,
            runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        )
        consumed = 0
        async for _raw in generator:
            consumed += 1
            if consumed >= events_before_disconnect:
                break
        await generator.aclose()

        detached = list(chat_service._DETACHED_CHAT_TURNS)
        if detached:
            await asyncio.gather(*detached, return_exceptions=True)
        pending = list(chat_service._SNAPSHOT_PUBLISH_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return {
        "payloads": [json.loads(payload) for _channel, payload in redis.published],
        "error_saves": error_saves,
        "interrupted_saves": interrupted_saves,
    }


def _snapshots(payloads: list[dict]) -> list[dict]:
    return [p for p in payloads if p.get("event") == "chat_stream_snapshot"]


@pytest.mark.asyncio
async def test_personal_turn_pushes_snapshots_the_owner_can_resume_from():
    payloads = await _run_turn(workspace_id=None, surface=ChatSurface.GLOBAL_OWNER_CHAT)
    snapshots = _snapshots(payloads)

    assert snapshots, "a personal turn has no other channel once its SSE body is gone"
    for payload in snapshots:
        # Without target=user the relay calls broadcast_to_entity, which ignores
        # entity_id and sends to every socket in the process. These carry the
        # reply text.
        assert payload["target"] == "user"
        assert payload["user_id"] == "user_1"
        assert payload["data"]["conversation_id"] == "conv_1"

    seqs = [p["data"]["seq"] for p in snapshots]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    # A pending card can only come from the tool_start publish. Without it a
    # follower sees nothing until the tool returns, which for a long skill is
    # the whole complaint: a page that looks dead while work is happening.
    assert any(
        call.get("name") == "bash" and call.get("status") == "pending"
        for payload in snapshots
        for call in payload["data"]["tool_calls"]
    ), "a running tool must reach followers while it is still running"
    assert any(
        call.get("name") == "bash" and call.get("status") == "success"
        for payload in snapshots
        for call in payload["data"]["tool_calls"]
    )

    final = snapshots[-1]["data"]
    assert final["status"] == "done"
    assert final["content"] == "Here is the answer, written out at durable length."


@pytest.mark.asyncio
async def test_text_growth_publishes_without_waiting_for_a_tool_or_the_end(monkeypatch):
    """A pure-prose turn must still reach followers while it is being written.

    The tool hooks publish unconditionally and the terminal edge publishes once,
    so those two alone satisfy a "did anything get published" assertion. This
    covers the throttled publish in the text path, which is the only thing
    moving the page during a long answer with no tool calls.
    """
    monkeypatch.setattr(chat_service, "STREAM_SNAPSHOT_INTERVAL", 0.0)

    async def _text_only_loop(**kwargs):
        stream_handler = kwargs["stream_handler"]
        await stream_handler("text_delta", {"content": "First half of the answer. "})
        await stream_handler("text_delta", {"content": "Second half of the answer."})
        return AgenticResult(
            content="First half of the answer. Second half of the answer.",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=[],
        )

    payloads = await _run_turn(
        workspace_id=None,
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        loop=_text_only_loop,
    )
    snapshots = _snapshots(payloads)
    mid_run = [p["data"] for p in snapshots if p["data"]["status"] == "streaming"]
    assert mid_run, "a text-only turn published nothing until it finished"
    assert any(
        "First half of the answer." in data["content"] for data in mid_run
    )
    assert all(data["tool_calls"] == [] for data in mid_run)


@pytest.mark.asyncio
async def test_snapshots_never_carry_the_untruncated_tool_output():
    payloads = await _run_turn(workspace_id=None, surface=ChatSurface.GLOBAL_OWNER_CHAT)
    for payload in _snapshots(payloads):
        for call in payload["data"]["tool_calls"]:
            assert "raw_result" not in call


@pytest.mark.asyncio
async def test_workspace_turns_keep_their_own_realtime_path():
    payloads = await _run_turn(
        workspace_id="ws_1", surface=ChatSurface.WORKSPACE_CHAT
    )
    assert _snapshots(payloads) == [], (
        "workspace chat already refetches from workspace_chat_message; a second "
        "event would double-render it"
    )


@pytest.mark.asyncio
async def test_a_workspace_resolved_at_runtime_still_blocks_the_relay():
    # The surface is computed from the request, but the workspace a turn ends up
    # in comes from resolve_runtime_chat_context — the two can disagree, and it
    # is ctx.workspace_id that decides which chat UI renders the reply.
    payloads = await _run_turn(
        workspace_id="ws_1", surface=ChatSurface.GLOBAL_OWNER_CHAT
    )
    assert _snapshots(payloads) == []


@pytest.mark.asyncio
async def test_public_webchat_turns_are_not_relayed_to_a_manor_account():
    payloads = await _run_turn(
        workspace_id=None, surface=ChatSurface.PUBLIC_CUSTOMER_CHAT
    )
    assert _snapshots(payloads) == []


@pytest.mark.asyncio
async def test_a_turn_with_no_user_publishes_nothing():
    # Conversation.user_id is nullable — channel-originated turns have no tab to
    # push to, and send_to_user("") is a silent no-op rather than an error.
    payloads = await _run_turn(
        workspace_id=None, surface=ChatSurface.GLOBAL_OWNER_CHAT, user_id=""
    )
    assert _snapshots(payloads) == []


def test_tool_calls_for_snapshot_drops_raw_result_and_keeps_the_card():
    events = [
        {
            "name": "bash",
            "arguments": {"command": "ls"},
            "result": "file-a",
            "status": "success",
            "raw_result": "x" * 5000,
        }
    ]
    assert _tool_calls_for_snapshot(events) == [
        {
            "name": "bash",
            "arguments": {"command": "ls"},
            "result": "file-a",
            "status": "success",
        }
    ]
    # The source list is for the DB write and must not be mutated.
    assert "raw_result" in events[0]


def test_messages_api_tells_a_reloaded_page_a_turn_is_still_running():
    """Without this the reader cannot distinguish a finished reply from a live one.

    ``_to_chat_message_response`` whitelists meta keys, and the placeholder row
    a reload lands on looks identical either way — same role, same content
    column, no marker. The page would show a dead placeholder until the first
    snapshot arrived, and forever if none did.
    """
    from datetime import datetime

    from apps.api.routers.chat import _to_chat_message_response

    running = SimpleNamespace(
        id="msg_1",
        conversation_id="conv_1",
        role="assistant",
        content="The assistant started this response and is still working.",
        tool_calls=None,
        token_usage=None,
        attachments=None,
        message_kind="text",
        refs=None,
        meta={"stream_status": "streaming", "assistant_blocks": [], "workflow_run_id": "run-1"},
        pending_action=None,
        resolved_at=None,
        resolution=None,
        created_at=datetime(2026, 8, 7, 12, 0, 0),
    )
    response = _to_chat_message_response(running)
    assert response.meta["stream_status"] == "streaming"
    # The workflow keys still come through, and nothing else does: meta is a
    # whitelist because the raw column holds internal runtime state.
    assert response.meta["workflow_run_id"] == "run-1"
    assert "assistant_blocks" not in response.meta

    finished = SimpleNamespace(**{**running.__dict__, "meta": {}})
    assert "stream_status" not in _to_chat_message_response(finished).meta


@pytest.mark.asyncio
async def test_detached_turn_that_errors_persists_the_error_before_the_terminal_snapshot():
    """A follower's refetch must find the failure, not a healthy checkpoint.

    Once the client is gone, the generator code after the yield loop — where
    the normal error persistence lives — never runs. Without the in-task save,
    the row claims "streaming" until the next process restart while the
    terminal snapshot says "error", and the reader polls a contradiction.
    """
    started = asyncio.Event()

    async def _tool_then_crash(**kwargs):
        kwargs["on_tool_start"]("bash", {"command": "ls"})
        started.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("provider exploded")

    result = await _run_turn_with_disconnect(loop=_tool_then_crash, events_before_disconnect=2)

    assert result["error_saves"], "the detached task must persist its own failure"
    assert "provider exploded" in str(result["error_saves"][0]["error_message"])
    snapshots = _snapshots(result["payloads"])
    assert snapshots and snapshots[-1]["data"]["status"] == "error"


@pytest.mark.asyncio
async def test_cancelled_turn_reports_interrupted_not_done():
    """Disconnect before any tool starts cancels the turn — followers must not
    be told it finished cleanly with whatever partial text existed."""

    async def _idle_forever(**kwargs):
        # One visible event, so the disconnect lands at the event-loop yield the
        # way a real one does — closing at the pre-try stream_start yield would
        # skip the finally entirely and test nothing.
        await kwargs["stream_handler"]("process_note", {"content": "working"})
        await asyncio.sleep(30)
        raise AssertionError("the harness should have cancelled this loop")

    result = await _run_turn_with_disconnect(loop=_idle_forever, events_before_disconnect=2)

    assert result["interrupted_saves"], "an idle disconnect must persist the interruption"
    snapshots = _snapshots(result["payloads"])
    statuses = [payload["data"]["status"] for payload in snapshots]
    assert "done" not in statuses
    assert snapshots and snapshots[-1]["data"]["status"] == "interrupted"
