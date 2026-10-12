"""A BYOK provider failure names the user's own provider and says what to check.

2026-09-18 prod: the entity's BYOK gateway (apitokengate.com) answered 429/503 for
four minutes. Every layer collapsed the failure to "Sorry, the request failed",
so the owner could not tell that the outage was on *their* provider rather than
on Manor. Owner surfaces now get a typed, structured message; visitors still get
the generic sentence because they cannot act on the owner's configuration.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from packages.core.ai import llm_client
from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime import ChatSurface
from packages.core.ai.runtime.output_policy import (
    BYOK_PROVIDER_ERROR_CODE,
    PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE,
    PUBLIC_REQUEST_FAILURE_MESSAGE,
    runtime_assistant_result_meta,
    runtime_assistant_stream_error_content,
    runtime_public_assistant_message_content,
    runtime_visitor_assistant_message_content,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services import chat_service
from packages.core.services.channel_conversations import list_public_webchat_messages


GATEWAY = "https://apitokengate.com/v1"
MODEL = "openai/gpt-6-astra"
RAW_BODY = "Service temporarily unavailable"
PUBLIC_DETAIL = "HTTP 503 from apitokengate.com (model openai/gpt-6-astra)"
OWNER_MESSAGE = f"{PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE}\n\nError detail: {PUBLIC_DETAIL}"


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{GATEWAY}/chat/completions")
    response = httpx.Response(status, text=RAW_BODY, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _route(source: str) -> SimpleNamespace:
    return SimpleNamespace(
        api_key="sk-" + "k" * 40, base_url=GATEWAY, provider="openai", source=source,
    )


def _byok_usage() -> dict:
    return {
        "error": f"HTTP 503: {RAW_BODY}",
        "error_code": BYOK_PROVIDER_ERROR_CODE,
        "error_public_detail": PUBLIC_DETAIL,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("source", ["byok", "official"])
@pytest.mark.parametrize("failure, expected_detail", [
    (_http_error(503), PUBLIC_DETAIL),
    (_http_error(401), "HTTP 401 from apitokengate.com (model openai/gpt-6-astra)"),
    (httpx.ConnectError("dns"), "no response from apitokengate.com (model openai/gpt-6-astra)"),
])
async def test_provider_failure_is_typed_only_on_byok_routes(
    monkeypatch, with_tools, source, failure, expected_detail,
):
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=_route(source)))
    monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_client, "_validate_llm_key_model_compatibility", lambda *_args: None)
    monkeypatch.setattr(llm_client, "_post_with_retry", AsyncMock(side_effect=failure))
    messages = [{"role": "user", "content": "现在有哪些每日任务"}]
    if with_tools:
        result = await llm_client.chat_completion_with_tools(messages, [], model=MODEL)
    else:
        result = await llm_client.chat_completion(messages, model=MODEL)
    usage = result[-1]
    assert usage["error"], usage
    if source == "byok":
        assert usage["error_code"] == BYOK_PROVIDER_ERROR_CODE
        assert usage["error_public_detail"] == expected_detail
        assert RAW_BODY not in usage["error_public_detail"]
    else:
        assert usage.get("error_code") != BYOK_PROVIDER_ERROR_CODE
        assert "error_public_detail" not in usage


@pytest.mark.asyncio
async def test_byok_failure_tells_the_owner_to_check_their_provider(monkeypatch):
    completion = AsyncMock(return_value=("", _byok_usage()))
    execute = AsyncMock(side_effect=AssertionError("No tool execution without a model response"))
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", completion,
    )
    result = await agentic_loop(
        system_prompt="system", user_message="现在有哪些每日任务", tools=[], tool_executor=execute,
    )
    assert result.stop_reason == "error"
    assert result.error == BYOK_PROVIDER_ERROR_CODE
    assert result.content == OWNER_MESSAGE
    assert result.error_detail == {"code": BYOK_PROVIDER_ERROR_CODE, "message": OWNER_MESSAGE}
    assert RAW_BODY not in result.content
    execute.assert_not_awaited()

    meta = runtime_assistant_result_meta(result)
    assert meta["stream_status"] == "error"
    assert meta["error"] == BYOK_PROVIDER_ERROR_CODE
    # Owner history / export keep the structured detail...
    assert runtime_public_assistant_message_content(result.content, meta) == OWNER_MESSAGE
    # ...visitors of a public webchat cannot fix the owner's provider.
    assert runtime_visitor_assistant_message_content(result.content, meta) == PUBLIC_REQUEST_FAILURE_MESSAGE


def test_stream_error_content_keeps_only_the_structured_byok_detail():
    assert runtime_assistant_stream_error_content(OWNER_MESSAGE) == OWNER_MESSAGE
    assert runtime_assistant_stream_error_content(BYOK_PROVIDER_ERROR_CODE) == (
        PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE
    )
    assert runtime_assistant_stream_error_content(PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE) == (
        PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE
    )
    for tampered in (
        "HTTP 503 from apitokengate.com Authorization: Bearer sk-live-1234567890abcdef",
        f"HTTP 503: {RAW_BODY}",
        "(sqlalchemy) current transaction is aborted [SQL: SELECT 1]",
        f"{PUBLIC_DETAIL}\nrequest: messages=201 tools=15",
        "HTTP 503 from apitokengate.com/v1?api_key=secret (model openai/gpt-6-astra)",
    ):
        projected = runtime_assistant_stream_error_content(
            f"{PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE}\n\nError detail: {tampered}"
        )
        assert projected == PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE, tampered


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def commit(self):
        pass


def _byok_loop_result() -> "chat_service.AgenticResult":
    from packages.core.ai.agentic_loop import AgenticResult

    return AgenticResult(
        content=OWNER_MESSAGE, messages=[], usage=_byok_usage(), rounds=1, tool_calls_made=[],
        stop_reason="error", error=BYOK_PROVIDER_ERROR_CODE,
        error_detail={"code": BYOK_PROVIDER_ERROR_CODE, "message": OWNER_MESSAGE},
    )


def _patch_chat_service(monkeypatch, result):
    context = SimpleNamespace(
        workspace_id=None, task_id=None, runtime_envelope=None,
        tool_profile=None, allowed_tool_names=None, user=None, entity=None,
    )
    save = AsyncMock(return_value="reply-1")
    trace = MagicMock(trace_id="byok-error-test")
    monkeypatch.setattr("packages.core.database.async_session", _Session)
    monkeypatch.setattr(chat_service, "ChatTrace", lambda **_kwargs: trace)
    monkeypatch.setattr(chat_service, "get_chat_turn_cancel_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(chat_service, "chat_turn_cancellation_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", AsyncMock(return_value=("system", [], [], context)))
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", AsyncMock(return_value=result))
    monkeypatch.setattr(chat_service, "save_or_update_assistant_stream_message", save)
    monkeypatch.setattr(chat_service, "runtime_persist_chat_stream_runtime_events", AsyncMock())
    monkeypatch.setattr(chat_service, "record_chat_runtime_learning", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_service, "schedule_learning_candidate_applies", AsyncMock())
    monkeypatch.setattr(chat_service, "_publish_chat_stream_snapshot", AsyncMock())
    return save


@pytest.mark.parametrize("surface, expected", [
    (ChatSurface.GLOBAL_OWNER_CHAT, OWNER_MESSAGE),
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, PUBLIC_REQUEST_FAILURE_MESSAGE),
])
async def test_stream_and_response_carry_the_byok_message_to_owners_only(monkeypatch, surface, expected):
    result = _byok_loop_result()
    save = _patch_chat_service(monkeypatch, result)

    events = []
    async for frame in chat_service.stream_chat_response(
        "现在有哪些每日任务", "conversation-1", entity_id="entity-1",
        user_id="user-1", assistant_message_id="reply-1", runtime_surface=surface,
    ):
        event, data = frame.split("\ndata: ", 1)
        events.append((event.removeprefix("event: "), json.loads(data)))
    if chat_service._SNAPSHOT_PUBLISH_TASKS:
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS))

    terminal = next(data for event, data in events if event == "stream_end")
    assert terminal["status"] == "error"
    assert terminal["error"] == expected
    assert terminal["error_code"] == BYOK_PROVIDER_ERROR_CODE
    assert terminal["limit_detail"] == {"code": BYOK_PROVIDER_ERROR_CODE, "message": expected}
    visible = ""
    for event, data in events:
        if event == "text_reset":
            visible = ""
        elif event == "text_delta":
            visible += data.get("content", "")
    assert visible == expected
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
        # Neither the provider host nor the raw provider body reach a visitor,
        # in any event field (usage carried both before this projection).
        assert "apitokengate" not in json.dumps(events)
        assert RAW_BODY not in json.dumps(events)
    else:
        assert terminal["usage"]["error_public_detail"] == PUBLIC_DETAIL
    # The stored row keeps the owner-facing evidence regardless of surface.
    assert save.await_args.kwargs["content"] == OWNER_MESSAGE
    assert save.await_args.kwargs["meta"]["error"] == BYOK_PROVIDER_ERROR_CODE

    response = await chat_service.run_chat_message(
        "现在有哪些每日任务", "conversation-1", entity_id="entity-1",
        user_id="user-1", runtime_surface=surface,
    )
    assert response["content"] == expected
    assert response["error"] == expected
    assert response["limit_detail"] == {"code": BYOK_PROVIDER_ERROR_CODE, "message": expected}
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
        assert "apitokengate" not in json.dumps(response)
        assert RAW_BODY not in json.dumps(response)


async def test_public_webchat_history_hides_the_owner_provider_from_visitors(db_session):
    conversation = Conversation(id=generate_ulid(), entity_id=generate_ulid(), channel="webchat")
    db_session.add(conversation)
    await db_session.flush()
    row = Message(
        id=generate_ulid(), conversation_id=conversation.id, role="assistant", content=OWNER_MESSAGE,
        meta={
            "channel_type": "webchat", "chat_id": "visitor", "stop_reason": "error",
            "stream_status": "error", "error": BYOK_PROVIDER_ERROR_CODE,
        },
    )
    db_session.add(row)
    await db_session.flush()
    messages = await list_public_webchat_messages(db_session, conversation.id, session_id="visitor")
    assert messages[0]["content"] == PUBLIC_REQUEST_FAILURE_MESSAGE
    assert messages[0]["stream_status"] == "error"
    assert "apitokengate" not in json.dumps(messages)
    await db_session.refresh(row)
    assert row.content == OWNER_MESSAGE


def test_byok_outage_stays_a_transient_signature_for_plan_steps():
    """Plan steps wrap the sentence as ``subagent stopped with error: …``;
    a BYOK 503 must keep the silent-retry classification the generic outage
    already has, while a BYOK 401 stays unmatched (never retried silently)."""
    from packages.core.governance.error_signatures import classify_execution_error

    outage = classify_execution_error({
        "type": "StepResultFailed", "message": f"subagent stopped with error: {OWNER_MESSAGE}",
    })
    assert outage is not None
    assert outage.is_transient is True
    assert "BYOK" in outage.what_happened
    auth = classify_execution_error({
        "type": "StepResultFailed",
        "message": (
            f"subagent stopped with error: {PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE}\n\n"
            "Error detail: HTTP 401 from apitokengate.com (model openai/gpt-6-astra)"
        ),
    })
    assert auth is None
