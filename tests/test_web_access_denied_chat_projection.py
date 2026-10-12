"""Stored denial evidence reaches normal, shared, and replayed chat receipts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import chat
from packages.core.ai.runtime.output_policy import (
    PUBLIC_TOOL_FAILURE_MESSAGE,
    PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE,
)


def stored_message():
    url = "https://blocked.example/source"
    calls = [{"name": "web_fetch", "status": "error", "arguments": {"url": url},
              "result": PUBLIC_TOOL_FAILURE_MESSAGE}]
    return SimpleNamespace(
        id="message-1", conversation_id="conversation-1", role="assistant",
        content="Answered using another source.", tool_calls=calls,
        meta={
            "runtime": {"metadata": {"runtime_events": [{
                "type": "tool_end", "tool_name": "web_fetch", "status": "failed",
                "error": f"Failed to fetch {url}: HTTP 403",
            }]}},
            "assistant_blocks": [{"type": "process", "id": "process-1",
                "lifecycle_status": "completed", "steps": [{
                    "id": "step-1", "kind": "tool", "name": "web_fetch", "status": "error",
                    "arguments_preview": json.dumps({"url": url}),
                    "result_preview": PUBLIC_TOOL_FAILURE_MESSAGE,
                }]}],
        },
        token_usage=None, attachments=None, message_kind=None, refs=None,
        pending_action=None, resolved_at=None, resolution=None, created_at=None,
    )


def assert_denial(payload):
    call = payload["tool_calls"][0]
    block = payload["assistant_blocks"][0]
    step = block["steps"][0]
    for row in (call, step):
        assert row["reason"] == "website_access_denied"
        assert row["status_code"] == 403
        assert row["status"] == "error"
    assert call["result"] == step["result_preview"] == PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE
    assert block["lifecycle_status"] == "completed"
    assert "runtime_events" not in json.dumps(payload, default=str)


def test_normal_history_projects_old_evidence_without_writing_message():
    message = stored_message()
    original = deepcopy(vars(message))
    response = chat._to_chat_message_response(message).model_dump()
    assert_denial(response)
    assert response["content"] == message.content
    assert vars(message) == original


@pytest.mark.asyncio
async def test_shared_history_uses_the_same_projection(monkeypatch):
    message = stored_message()
    original = deepcopy(vars(message))
    conversation = SimpleNamespace(
        id=message.conversation_id, entity_id="entity-1", user_id="user-1",
        agent_id=None, workspace_id=None, title="Research", summary=None,
        channel="web", status="active", created_at=None, updated_at=None,
    )
    monkeypatch.setattr(chat, "get_shared_conversation", AsyncMock(return_value=(None, conversation, [message])))
    response = await chat.view_shared_conversation("valid-test-token", db=object())
    assert_denial(response.messages[0].model_dump())
    assert vars(message) == original


@pytest.mark.asyncio
async def test_submission_replay_projects_evidence_without_starting_tools(monkeypatch):
    message = stored_message()
    monkeypatch.setattr(chat, "format_sse", lambda kind, payload: (kind, payload))
    frames = [frame async for frame in chat._response_surface_submission_replay_stream(message.conversation_id, message)]
    assert frames[-1][0] == "stream_end"
    assert_denial(frames[-1][1])
    assert frames[-1][1]["submission_reused"] is True
    assert frames[-1][1]["rounds"] == 0


def test_independent_turn_failure_is_not_erased_by_denial_projection():
    message = stored_message()
    message.meta.update(stream_error=True, stream_status="error", stop_reason="error")
    response = chat._to_chat_message_response(message).model_dump()
    assert response["assistant_blocks"][0]["lifecycle_status"] == "failed"
    assert response["tool_calls"][0]["reason"] == "website_access_denied"


def test_workspace_history_uses_same_evidence_without_changing_terminal_behavior():
    from apps.api.routers.workspace_chat import _to_message

    message = stored_message()
    message.created_at = datetime.now(timezone.utc)
    message.message_kind = "text"
    message.author_kind = "assistant"
    message.author_user_id = None
    message.author_subscription_id = None
    message.resolved_by_user_id = None
    original = deepcopy(vars(message))
    response = _to_message(message).model_dump()
    assert_denial(response)
    assert response["body"] == message.content
    assert vars(message) == original
    message.meta.update(stream_error=True, stream_status="error", stop_reason="error")
    failed = _to_message(message).model_dump()
    assert failed["assistant_blocks"] is None
    assert failed["tool_calls"][0]["reason"] == "website_access_denied"
