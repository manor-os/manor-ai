"""Public chat authorization at the HTTP boundary, without providers or a live DB."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from apps.api import deps
from apps.api.routers import public_chat
from packages.core.ai import runtime
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import agent_subscription_service, channel_gateway, conversation_messages
from packages.core.services.auth_service import create_access_token


@pytest.fixture
async def public_chat_case(monkeypatch):
    db = AsyncMock()
    user = User(
        id="customer", entity_id="customer-entity", role="external",
        status="active", token_version=2, display_name="Customer",
        first_name="", last_name="", email="customer@example.test",
        password_hash="unused",
    )
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: user)
    monkeypatch.setattr(deps, "resolve_current_user_actor", AsyncMock(return_value=SimpleNamespace(
        entity_id=user.entity_id,
        display_role=user.role,
    )))
    cc = SimpleNamespace(
        id="channel", entity_id="business", workspace_id="workspace",
        name="Public chat", config={"login_required": False},
    )
    binding = SimpleNamespace(id="binding", user_id=None, config={})
    contact = SimpleNamespace(
        id="contact", user_id=None, profile={}, role="external",
        status="active", display_name="Visitor",
    )
    conversation = SimpleNamespace(id="conversation", meta={})
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(cc, binding)))
    monkeypatch.setattr(public_chat, "find_channel_session_contact", AsyncMock(return_value=contact))
    monkeypatch.setattr(public_chat, "find_claimed_webchat_contact_for_user", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=conversation))
    history = AsyncMock(return_value=[{"id": "reply", "content": "Customer history"}])
    monkeypatch.setattr(public_chat, "list_public_webchat_messages", history)
    monkeypatch.setattr(public_chat, "channel_workspace_is_routable", AsyncMock(return_value=True))
    monkeypatch.setattr(public_chat, "get_or_create_channel_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(agent_subscription_service, "resolve_subscription", AsyncMock(return_value=SimpleNamespace(
        id="subscription", agent_id="agent", workspace_id=cc.workspace_id, source="channel",
    )))
    monkeypatch.setattr(conversation_messages, "add_message", AsyncMock())
    monkeypatch.setattr(conversation_messages, "create_assistant_stream_placeholder", AsyncMock(return_value=SimpleNamespace(id="assistant")))
    runtime_calls = []

    async def fake_runtime(*args, **kwargs):
        runtime_calls.append((args, kwargs))
        yield 'event: stream_end\ndata: {}\n\n'

    monkeypatch.setattr(runtime, "runtime_stream_chat_turn", fake_runtime)
    dispatch = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(channel_gateway, "dispatch_inbound", dispatch)
    lease = SimpleNamespace(wrap=lambda stream: stream, release=AsyncMock())
    monkeypatch.setattr(public_chat, "acquire_chat_stream_lease", AsyncMock(return_value=lease))

    app = FastAPI()
    app.include_router(public_chat.router)

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, cc=cc, contact=contact, user=user, db=db,
            history=history, runtime_calls=runtime_calls, lease=lease, dispatch=dispatch,
        )


@pytest.mark.parametrize("claim", ["user_id", "verified_customer_user_id"])
async def test_anonymous_poll_cannot_read_claimed_session(public_chat_case, claim):
    case = public_chat_case
    if claim == "user_id":
        case.contact.user_id = case.user.id
    else:
        case.contact.profile = {claim: case.user.id}
    response = await case.client.get("/api/v1/public/chat/token/messages", params={"session_id": "session"})
    assert response.status_code == 403
    case.history.assert_not_awaited()


async def test_unclaimed_anonymous_session_can_poll(public_chat_case):
    response = await public_chat_case.client.get(
        "/api/v1/public/chat/token/messages", params={"session_id": "session"},
    )
    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == "Customer history"


@pytest.mark.parametrize("login_required", [False, True])
async def test_revoked_token_cannot_read_claimed_session(public_chat_case, login_required):
    case = public_chat_case
    case.cc.config["login_required"] = login_required
    case.contact.profile = {"verified_customer_user_id": case.user.id}
    token = create_access_token(case.user.id, case.user.entity_id, case.user.role, token_version=1)
    response = await case.client.get(
        "/api/v1/public/chat/token/messages", params={"session_id": "session"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code in {401, 403}
    case.history.assert_not_awaited()


async def test_current_token_can_read_its_claimed_session(public_chat_case):
    case = public_chat_case
    case.cc.config["login_required"] = True
    case.contact.profile = {"verified_customer_user_id": case.user.id}
    token = create_access_token(case.user.id, case.user.entity_id, case.user.role, token_version=2)
    response = await case.client.get(
        "/api/v1/public/chat/token/messages", params={"session_id": "session"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


async def test_revoked_token_leaves_no_authenticated_request_context(public_chat_case):
    from packages.core.services.auth_context import current_mfa_verified, set_current_mfa_verified

    case = public_chat_case
    token = create_access_token(
        case.user.id, case.user.entity_id, case.user.role,
        token_version=1, mfa_authenticated=True,
    )
    request = Request({
        "type": "http", "method": "GET", "path": "/api/v1/public/chat/token/messages",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })
    try:
        assert await public_chat._optional_current_user(request, case.db) is None
        assert not current_mfa_verified()
        assert not request.state.auth_claims
        assert request.state.impersonation is None
    finally:
        set_current_mfa_verified(False)


@pytest.mark.parametrize("endpoint", ["session", "message", "message/stream", "messages"])
@pytest.mark.parametrize("claim", [None, "user_id", "verified_customer_user_id"])
@pytest.mark.parametrize("revoked_token", [False, True])
async def test_blocked_contact_is_rejected_on_every_public_session_endpoint(public_chat_case, endpoint, claim, revoked_token):
    case = public_chat_case
    case.contact.status = "blocked"
    if claim == "user_id":
        case.contact.user_id = case.user.id
    elif claim:
        case.contact.profile = {claim: case.user.id}
    if revoked_token:
        token = create_access_token(case.user.id, case.user.entity_id, case.user.role, token_version=1)
        case.client.headers["Authorization"] = f"Bearer {token}"
    path = f"/api/v1/public/chat/token/{endpoint}"
    if endpoint == "messages":
        response = await case.client.get(path, params={"session_id": "session"})
    elif endpoint == "message/stream":
        response = await case.client.post(path, data={"session_id": "session", "message": "Hello"})
    else:
        response = await case.client.post(path, json={"session_id": "session", "text": "Hello"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "visitor_blocked"
    assert not case.runtime_calls
    case.dispatch.assert_not_awaited()
    case.history.assert_not_awaited()
    if endpoint == "message/stream":
        case.lease.release.assert_awaited_once()


@pytest.mark.parametrize("endpoint", ["session", "message", "message/stream", "messages", "audio/transcribe", "audio/speech"])
async def test_blocked_user_cannot_bypass_the_block_with_another_session(public_chat_case, monkeypatch, endpoint):
    case = public_chat_case
    blocked = SimpleNamespace(status="blocked")
    lookup = AsyncMock(return_value=blocked)
    monkeypatch.setattr(public_chat, "find_claimed_webchat_contact_for_user", lookup)
    token = create_access_token(case.user.id, case.user.entity_id, case.user.role, token_version=2)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/public/chat/token/{endpoint}"
    if endpoint == "messages":
        response = await case.client.get(path, params={"session_id": "new-session"}, headers=headers)
    elif endpoint == "message/stream":
        response = await case.client.post(path, data={"session_id": "new-session", "message": "Hello"}, headers=headers)
    elif endpoint == "audio/transcribe":
        response = await case.client.post(path, data={"session_id": "new-session"}, files={"file": ("voice.wav", b"test", "audio/wav")}, headers=headers)
    else:
        response = await case.client.post(path, json={"session_id": "new-session", "text": "Hello", "message_id": "reply"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "visitor_blocked"
    assert lookup.await_args.kwargs["status"] == "blocked"
    case.dispatch.assert_not_awaited()
    case.history.assert_not_awaited()
    assert not case.runtime_calls


async def test_anonymous_stream_explicitly_uses_public_attachment_policy(public_chat_case, monkeypatch):
    from packages.core.services.file_context import FileAttachments
    from packages.core.services.runtime_file_context import RuntimeFileContextTurn

    prepare = AsyncMock(return_value=RuntimeFileContextTurn(cleaned_message="Hello", attachments=FileAttachments()))
    monkeypatch.setattr(public_chat, "prepare_runtime_file_context_turn", prepare)
    response = await public_chat_case.client.post(
        "/api/v1/public/chat/token/message/stream",
        data={"session_id": "session", "message": "Hello"},
    )
    assert response.status_code == 200
    assert prepare.await_args.kwargs["surface"] == runtime.ChatSurface.PUBLIC_CUSTOMER_CHAT
    assert prepare.await_args.kwargs["user_id"] is None


async def test_public_message_does_not_inject_voice_context(public_chat_case):
    response = await public_chat_case.client.post(
        "/api/v1/public/chat/token/message",
        json={"session_id": "session", "text": "Hello"},
    )
    assert response.status_code == 200
    assert public_chat_case.dispatch.await_args.kwargs["runtime_metadata"] is None


async def test_internal_public_voice_turn_injects_live_voice_context(public_chat_case):
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/public/chat/token/message",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
        "scheme": "http",
        "state": {"voice_session_mode": "chat_gateway"},
    })
    await public_chat.send_message(
        "token",
        public_chat.MessageRequest(session_id="session", text="Hello"),
        request,
        public_chat_case.db,
    )
    assert public_chat_case.dispatch.await_args.kwargs["runtime_metadata"] == {
        "voice_session_mode": "chat_gateway",
    }


async def test_poll_refreshes_messages_under_the_same_session_scope(public_chat_case):
    case = public_chat_case
    case.history.side_effect = [[], [{"id": "assistant", "content": "Completed reply"}]]
    response = await case.client.get(
        "/api/v1/public/chat/token/messages",
        params={"session_id": "session", "after": "last-message", "refresh": "assistant"},
    )
    assert response.status_code == 200
    assert response.json() == {"messages": [], "updates": [{"id": "assistant", "content": "Completed reply"}]}
    assert case.history.await_args.kwargs == {"session_id": "session", "message_ids": ["assistant"]}
    assert case.history.await_args.args[1] == "conversation"


async def test_poll_bounds_the_number_of_refresh_ids(public_chat_case):
    response = await public_chat_case.client.get(
        "/api/v1/public/chat/token/messages",
        params=[("session_id", "session"), *(("refresh", str(i)) for i in range(51))],
    )
    assert response.status_code == 422
    public_chat_case.history.assert_not_awaited()


async def test_stream_persists_the_client_turn_before_sse_starts(public_chat_case, monkeypatch):
    from packages.core.services.file_context import FileAttachments
    from packages.core.services.runtime_file_context import RuntimeFileContextTurn

    prepare = AsyncMock(return_value=RuntimeFileContextTurn(cleaned_message="Hello", attachments=FileAttachments()))
    monkeypatch.setattr(public_chat, "prepare_runtime_file_context_turn", prepare)
    response = await public_chat_case.client.post(
        "/api/v1/public/chat/token/message/stream",
        data={"session_id": "session", "message": "Hello", "client_turn_id": "browser-turn-1"},
    )
    assert response.status_code == 200
    user_meta = conversation_messages.add_message.await_args.kwargs["meta"]
    reply_meta = conversation_messages.create_assistant_stream_placeholder.await_args.kwargs["meta"]
    assert user_meta["client_turn_id"] == reply_meta["client_turn_id"] == "browser-turn-1"
    public_chat_case.db.commit.assert_awaited_once()


async def test_stream_bounds_client_turn_ids(public_chat_case):
    response = await public_chat_case.client.post(
        "/api/v1/public/chat/token/message/stream",
        data={"session_id": "session", "message": "Hello", "client_turn_id": "x" * 65},
    )
    assert response.status_code == 422
    assert not public_chat_case.runtime_calls
