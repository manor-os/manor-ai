"""Live voice delegates its work to the ordinary chat entrypoints.

A member's voice call hands each turn to ``apps.api.routers.chat.chat_message``
(so it gets exactly the ordinary chat surface, tools and Skills), and a
public-token call hands it to ``public_chat.send_message``, which runs on the
public customer surface. Voice therefore needs no capability of its own.
"""
from __future__ import annotations

import ast
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.surfaces import ChatSurface


@pytest.fixture
def voice(monkeypatch):
    from apps.api import chat_voice
    from apps.api.chat_audio import ChatAudioScope

    calls: dict[str, dict] = {}

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def same_scope(_db, _request, _start, *, create):
        return scope

    async def no_budget(*_args, **_kwargs):
        return None

    async def credentials(_request):
        return None

    async def current_user(_request, *, credentials, db):
        return SimpleNamespace(id="user-1", entity_id="entity-1")

    async def chat_message(**kwargs):
        calls["chat_message"] = kwargs
        return SimpleNamespace(
            stop_reason="end_turn", error=None, hitl_requests=None,
            conversation_id=scope.conversation_id, content="done",
        )

    async def send_message(token, req, request, db):
        calls["send_message"] = {"token": token, "req": req}
        return {"status": "ok", "reply": "hi", "conversation_id": scope.conversation_id}

    scope = ChatAudioScope(
        entity_id="entity-1", user_id="user-1", conversation_id="conv-1", agent_id="agent-1",
    )
    monkeypatch.setattr(chat_voice, "async_session", session)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", same_scope)
    monkeypatch.setattr(chat_voice, "enforce_public_audio_budget", no_budget)
    monkeypatch.setattr(chat_voice, "security", credentials)
    monkeypatch.setattr(chat_voice, "get_current_user", current_user)
    monkeypatch.setattr("apps.api.routers.chat.chat_message", chat_message)
    monkeypatch.setattr("apps.api.routers.public_chat.send_message", send_message)
    return chat_voice, scope, calls


@pytest.mark.asyncio
async def test_member_voice_turn_runs_the_ordinary_chat_endpoint(voice) -> None:
    from apps.api.routers.chat import _surface_for_chat_request

    chat_voice, scope, calls = voice
    start = chat_voice.CallStart(token="jwt", conversation_id=scope.conversation_id)
    request = SimpleNamespace(state=SimpleNamespace())
    outcome = await chat_voice.call_agent(request, start, scope, "summarise my week")

    assert outcome.status == "ok"
    assert "send_message" not in calls
    sent = calls["chat_message"]
    assert sent["message"] == "summarise my week"
    assert sent["agent_id"] == scope.agent_id
    assert sent["editor_context"] is None
    surface = _surface_for_chat_request(
        agent_id=sent["agent_id"], workspace_id=sent["workspace_id"],
        editor_context=sent["editor_context"],
    )
    # The same surface a typed message in that chat would get.
    assert surface is ChatSurface.AGENT_DM


@pytest.mark.asyncio
async def test_public_voice_turn_runs_the_public_customer_chat(voice) -> None:
    from apps.api.routers import public_chat

    chat_voice, scope, calls = voice
    start = chat_voice.CallStart(public_token="tok", session_id="visitor-1")
    request = SimpleNamespace(state=SimpleNamespace())
    outcome = await chat_voice.call_agent(request, start, scope, "opening hours?")

    assert outcome.status == "ok"
    assert "chat_message" not in calls
    assert calls["send_message"]["token"] == "tok"
    assert calls["send_message"]["req"].text == "opening hours?"

    # send_message dispatches as a webchat inbound, which is the public
    # customer surface.
    # (Read the module source: the fixture replaced the function object.)
    module = ast.parse(inspect.getsource(public_chat))
    send = next(
        node for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_message"
    )
    channel_types = {
        keyword.value.value
        for node in ast.walk(send)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dispatch_inbound"
        for keyword in node.keywords
        if keyword.arg == "channel_type" and isinstance(keyword.value, ast.Constant)
    }
    assert channel_types == {"webchat"}
    from packages.core.ai.runtime.surfaces import surface_for_channel_context

    assert surface_for_channel_context(
        channel_type="webchat", is_verified=True, role="owner", user_id="user-1",
    ) is ChatSurface.PUBLIC_CUSTOMER_CHAT
