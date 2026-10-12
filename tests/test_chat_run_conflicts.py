from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.exc import IntegrityError

from apps.api.routers import chat as chat_router
from packages.core.models.runtime_run import RuntimeRun
from packages.core.services.chat_manual_skills import ChatManualSkillTurn
from packages.core.services.runtime_file_context import RuntimeFileContextTurn


def _integrity_error(constraint: str | None, *, driver: str = "asyncpg") -> IntegrityError:
    original = Exception("Database integrity failure")
    if driver == "psycopg":
        original.diag = SimpleNamespace(constraint_name=constraint)
    elif driver == "asyncpg":
        cause = Exception("Database integrity failure")
        cause.constraint_name = constraint
        original.__cause__ = cause
    else:
        original.constraint_name = constraint
    return IntegrityError("INSERT", None, original)


@pytest.fixture
async def chat_request(monkeypatch):
    """Exercise the HTTP handler while replacing external admission/DB boundaries."""
    user = SimpleNamespace(id="user-1", entity_id="entity-1")
    conversation = SimpleNamespace(
        id="conversation-1", workspace_id=None, thread_ref_kind=None,
        thread_ref_id=None, agent_subscription_id=None, meta={},
    )
    db = SimpleNamespace(
        add=Mock(), commit=AsyncMock(), rollback=AsyncMock(), close=AsyncMock(),
    )
    lease = SimpleNamespace(release=AsyncMock())

    async def wrap(source):
        try:
            async for frame in source:
                yield frame
        finally:
            await lease.release()

    lease.wrap = wrap
    boundary_stubs = {
        "_get_accessible_conversation": conversation,
        "_require_chat_budget_unless_pending_approval": False,
        "_resolve_chat_workspace_scope": (None, None, None),
        "_resolve_task_session_request_agent": None,
        "_conversation_video_edit_route_state": chat_router._VideoEditRouteState.INACTIVE,
        "acquire_chat_stream_lease": lease,
        "_build_attachments": RuntimeFileContextTurn(cleaned_message="continue"),
        "_prepare_manual_skill_turn": ChatManualSkillTurn([], "continue", "continue"),
        "get_or_create_conversation": conversation,
        "resolve_chat_approval_turn": (None, None, True, None),
        "add_message": SimpleNamespace(id="user-message-1"),
        "create_assistant_stream_placeholder": SimpleNamespace(id="assistant-message-1"),
        "_response_surface_submission_agent": SimpleNamespace(
            agent_id=None, agent_subscription_id=None,
        ),
    }
    for name, result in boundary_stubs.items():
        monkeypatch.setattr(chat_router, name, AsyncMock(return_value=result))
    monkeypatch.setattr(
        chat_router, "_bind_response_surface_submission",
        AsyncMock(side_effect=lambda _db, **kwargs: kwargs["receipt"]),
    )
    find_replay = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_router, "_find_response_surface_submission_replay", find_replay)
    monkeypatch.setattr(
        chat_router, "get_settings",
        lambda: SimpleNamespace(
            MANOR_RUNTIME_EXECUTION_MODE="durable",
            SANDBOX_COORDINATION_MODE="external-runner",
        ),
    )

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[chat_router.get_current_user] = lambda: user
    app.dependency_overrides[chat_router.get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, db=db, lease=lease, find_replay=find_replay)


@pytest.mark.parametrize("driver", ["asyncpg", "psycopg", "direct"])
async def test_active_conversation_conflict_returns_coded_error(chat_request, driver):
    chat_request.db.commit.side_effect = _integrity_error(
        "uq_runtime_runs_active_conversation", driver=driver,
    )

    response = await chat_request.client.post(
        "/api/v1/chat/stream", data={"message": "continue", "disable_tools": "true"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": {
        "code": "lib.api.conversation_run_active",
        "message": (
            "The previous request is still running. "
            "Wait for it to finish before sending another message."
        ),
    }}
    assert any(isinstance(call.args[0], RuntimeRun) for call in chat_request.db.add.call_args_list)
    chat_request.db.rollback.assert_awaited_once()
    chat_request.lease.release.assert_awaited_once()
    chat_request.find_replay.assert_not_awaited()


@pytest.mark.parametrize("constraint", [
    "uq_runtime_outbox_events_dedupe_key",
    "uq_messages_conversation_response_surface_event",
    None,
])
async def test_other_integrity_errors_propagate_after_cleanup(chat_request, constraint):
    error = _integrity_error(constraint)
    chat_request.db.commit.side_effect = error

    with pytest.raises(IntegrityError) as exc_info:
        await chat_request.client.post(
            "/api/v1/chat/stream", data={"message": "continue", "disable_tools": "true"},
        )

    assert exc_info.value is error
    chat_request.db.rollback.assert_awaited_once()
    chat_request.lease.release.assert_awaited_once()
    chat_request.find_replay.assert_not_awaited()


def _surface_submission_form() -> dict[str, str]:
    return {
        "message": "continue",
        "conversation_id": "conversation-1",
        "response_surface_submission": json.dumps({
            "version": 1,
            "eventId": "event-1",
            "recordedAt": "2026-09-11T00:00:00Z",
            "sourceMessageId": "surface-message-1",
            "surfaceId": "surface-1",
            "title": "Choose an option",
            "action": "submit",
            "actionLabel": "Submit",
            "payload": {"choice": "continue"},
        }),
    }


async def test_response_surface_uniqueness_race_still_replays(chat_request):
    chat_request.db.commit.side_effect = [
        _integrity_error("uq_messages_conversation_response_surface_event"), None,
    ]
    assistant = SimpleNamespace(
        id="existing-assistant-1", role="assistant", content="Already completed.",
        meta={}, token_usage={}, tool_calls=[], attachments=None,
    )
    chat_request.find_replay.side_effect = [None, (assistant, None)]

    response = await chat_request.client.post(
        "/api/v1/chat/stream", data=_surface_submission_form(),
    )

    assert response.status_code == 200
    assert response.headers["X-Response-Surface-Event-ID"] == "event-1"
    assert '"submission_reused": true' in response.text
    assert "Already completed." in response.text
    assert "event: stream_end" in response.text
    assert chat_request.find_replay.await_count == 2
    chat_request.db.rollback.assert_awaited_once()
    chat_request.lease.release.assert_awaited_once()


@pytest.mark.parametrize("constraint,expected_lookups", [
    ("uq_messages_conversation_response_surface_event", 2),
    ("uq_runtime_outbox_events_dedupe_key", 1),
])
async def test_surface_submission_unreplayed_integrity_error_propagates(
    chat_request, constraint, expected_lookups,
):
    error = _integrity_error(constraint)
    chat_request.db.commit.side_effect = error

    with pytest.raises(IntegrityError) as exc_info:
        await chat_request.client.post(
            "/api/v1/chat/stream", data=_surface_submission_form(),
        )

    assert exc_info.value is error
    assert chat_request.find_replay.await_count == expected_lookups
    chat_request.db.rollback.assert_awaited_once()
    chat_request.lease.release.assert_awaited_once()
