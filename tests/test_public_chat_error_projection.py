"""Visitor output hides operator diagnostics without rewriting runtime evidence."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.ai.agentic_loop import AgenticResult, _llm_call_failed_result
from packages.core.ai.runtime import ChatSurface
from packages.core.ai.runtime.output_policy import PUBLIC_REQUEST_FAILURE_MESSAGE
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services import chat_service
from packages.core.services.channel_conversations import list_public_webchat_messages


DETAIL = (
    "No usable LLM API key found. Self-hosted mode requires your own native "
    "provider API key for the selected model; Manor official routing is Cloud-only."
)


def _failure(*, missing_configuration=False):
    return _llm_call_failed_result(
        DETAIL, messages=[], usage={}, rounds=1, tool_calls_made=[],
        **({"error_code": "llm_configuration_missing"} if missing_configuration else {}),
    )


def _assert_no_operator_diagnostics(payload):
    serialized = json.dumps(payload)
    for diagnostic in ("No usable", "API key", "Cloud-only", DETAIL):
        assert diagnostic not in serialized


@pytest.mark.parametrize("surface, outcome, partial, missing_configuration", [
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, "error", False, False),
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, "error", True, False),
    (ChatSurface.GLOBAL_OWNER_CHAT, "error", False, False),
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, "completed", False, False),
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, "credit_exhausted", False, False),
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, "error", False, True),
    (ChatSurface.GLOBAL_OWNER_CHAT, "error", False, True),
])
async def test_stream_projects_public_failure_but_keeps_admin_evidence(
    monkeypatch, surface, outcome, partial, missing_configuration,
):
    result = _failure(missing_configuration=missing_configuration) if outcome == "error" else AgenticResult(
        content="Your credits are exhausted." if outcome == "credit_exhausted" else "Reception opens at 09:00.",
        messages=[], usage={}, rounds=1, tool_calls_made=[], stop_reason=outcome,
    )
    original_content = result.content
    context = SimpleNamespace(
        workspace_id=None, task_id=None, runtime_envelope=None,
        tool_profile=None, allowed_tool_names=None, user=None, entity=None,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def commit(self):
            pass

    async def loop(**kwargs):
        if partial:
            await kwargs["stream_handler"]("text_delta", {
                # Cross the existing 120-character flush threshold after the
                # summary sentinel buffer, so the reset exercises visible text.
                "content": (
                    "I will check the reception hours in the published FAQ before answering your question "
                    "about this sample residence. I will look for the current reception schedule and any "
                    "weekend exceptions in the available information."
                ),
            })
        return result

    save = AsyncMock(return_value="reply-1")
    snapshots = AsyncMock()
    trace = MagicMock(trace_id="public-error-test")
    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(chat_service, "ChatTrace", lambda **_kwargs: trace)
    monkeypatch.setattr(chat_service, "get_chat_turn_cancel_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(chat_service, "chat_turn_cancellation_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", AsyncMock(return_value=("system", [], [], context)))
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", loop)
    monkeypatch.setattr(chat_service, "save_or_update_assistant_stream_message", save)
    monkeypatch.setattr(chat_service, "runtime_persist_chat_stream_runtime_events", AsyncMock())
    monkeypatch.setattr(chat_service, "record_chat_runtime_learning", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_service, "schedule_learning_candidate_applies", AsyncMock())
    monkeypatch.setattr(chat_service, "_publish_chat_stream_snapshot", snapshots)

    events = []
    async for frame in chat_service.stream_chat_response(
        "What are the reception hours?", "conversation-1", entity_id="entity-1",
        user_id="user-1", assistant_message_id="reply-1", runtime_surface=surface,
    ):
        event, data = frame.split("\ndata: ", 1)
        events.append((event.removeprefix("event: "), json.loads(data)))
    if chat_service._SNAPSHOT_PUBLISH_TASKS:
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS))

    visible = ""
    for event, data in events:
        if event == "text_reset":
            visible = ""
        elif event == "text_delta":
            visible += data.get("content", "")
    configuration_message = (
        "AI is not configured for this chat. Ask your administrator to configure "
        "an AI provider, then try again."
    )
    public_failure = configuration_message if missing_configuration else PUBLIC_REQUEST_FAILURE_MESSAGE
    expected = public_failure if outcome == "error" else original_content
    assert visible == expected
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT and outcome == "error":
        _assert_no_operator_diagnostics(events)
        if partial:
            assert any(event == "text_delta" and "I will check" in data.get("content", "") for event, data in events)
            assert any(event == "text_reset" for event, _data in events)
    terminal = next(data for event, data in events if event == "stream_end")
    assert terminal["stop_reason"] == outcome
    assert terminal["persisted"] is True
    assert terminal["message_id"] == "reply-1"
    if outcome == "error":
        assert terminal["error"] == public_failure
        assert [block["text"] for block in terminal["assistant_blocks"] if block["type"] == "text"] == [expected]
        assert all(block["type"] in {"process", "text"} for block in terminal["assistant_blocks"])
        assert terminal["status"] == "error"
        assert terminal["error_code"] == result.error
    assert save.await_args.kwargs["content"] == original_content
    assert result.content == original_content
    trace.log_complete.assert_called_once_with(result=result)
    if outcome == "error":
        assert save.await_args.kwargs["meta"]["error"] == result.error
        assert save.await_args.kwargs["meta"]["stop_reason"] == "error"
        assert save.await_args.kwargs["meta"]["stream_status"] == "error"
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
        # Public visitors recover via their scoped history, never owner WebSockets.
        snapshots.assert_not_awaited()
    else:
        assert snapshots.await_args.args[2]["content"] == expected
        if outcome == "error":
            assert snapshots.await_args.args[2]["status"] == "error"

    if not partial:
        # The same public surface's non-streaming response cannot bypass projection.
        response = await chat_service.run_chat_message(
            "What are the reception hours?", "conversation-1", entity_id="entity-1",
            user_id="user-1", runtime_surface=surface,
        )
        assert response["content"] == expected
        assert response["stop_reason"] == outcome
        assert result.content == original_content
        if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT and outcome == "error":
            _assert_no_operator_diagnostics(response)


async def test_public_history_and_refresh_project_errors_without_changing_stored_rows(db_session):
    conversation = Conversation(id=generate_ulid(), entity_id=generate_ulid(), channel="webchat")
    db_session.add(conversation)
    await db_session.flush()
    failure = _failure()
    rows = [
        Message(
            id=generate_ulid(), conversation_id=conversation.id, role=role, content=content,
            meta={"channel_type": "webchat", "chat_id": "visitor", **meta},
        )
        for role, content, meta in [
            ("assistant", failure.content, {"stop_reason": "error", "error": "llm_call_failed", "error_message": DETAIL}),
            ("assistant", "Reception opens at 09:00.", {"stream_status": "completed"}),
            ("user", "What are the reception hours?", {}),
        ]
    ]
    db_session.add_all(rows)
    await db_session.flush()
    error_id = rows[0].id
    for query in ({}, {"message_ids": [row.id for row in rows]}):
        messages = await list_public_webchat_messages(db_session, conversation.id, session_id="visitor", **query)
        by_id = {message["id"]: message for message in messages}
        assert by_id[error_id]["content"] == PUBLIC_REQUEST_FAILURE_MESSAGE
        assert by_id[error_id]["stream_status"] == "error"
        assert by_id[rows[1].id]["content"] == rows[1].content
        assert by_id[rows[2].id]["content"] == rows[2].content
        _assert_no_operator_diagnostics(messages)
    await db_session.refresh(rows[0])
    assert rows[0].content == failure.content
    assert rows[0].meta["error"] == "llm_call_failed"
    assert rows[0].meta["error_message"] == DETAIL

    # A visitor quoting an error is still their own message, not a runtime failure.
    rows[2].content = failure.content
    await db_session.flush()
    quoted = await list_public_webchat_messages(
        db_session, conversation.id, session_id="visitor", message_ids=[rows[2].id],
    )
    assert quoted[0]["content"] == failure.content
