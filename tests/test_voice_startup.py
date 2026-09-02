"""Bounded startup leaves room for fallback without timing out ready calls."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from packages.core.services.chat_concurrency import ChatConcurrencyConfig, ChatConcurrencyGate
from packages.core.services.voice.realtime import RealtimeRoute
from tests.test_browser_voice import Provider, Socket, eventually


async def test_public_call_rate_limit_runs_before_database_scope_resolution(monkeypatch):
    ws = Socket()
    ws.accept = AsyncMock()
    await ws.incoming.put({"type": "start", "public_token": "public", "session_id": "visitor"})
    order = []

    @asynccontextmanager
    async def database():
        yield object()

    async def enforce(*_args):
        order.append("limit")

    async def resolve(*_args, **_kwargs):
        order.append("scope")
        raise HTTPException(404, "Chat not found")

    monkeypatch.setattr(chat_voice, "async_session", database)
    monkeypatch.setattr(chat_voice, "call_request", lambda *_: object())
    monkeypatch.setattr(chat_voice, "enforce_public_audio_budget", enforce)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", resolve)

    await chat_voice.live_voice(ws)

    assert order == ["limit", "scope"]
    assert ws.closed
    assert ws.events == [{"type": "error", "message": "Chat not found", "status": 404}]


@pytest.fixture
async def endpoint(monkeypatch):
    ws = Socket()
    ws.accept = AsyncMock()
    await ws.incoming.put({"type": "start", "token": "test"})
    scope = ChatAudioScope("entity", "owner", conversation_id="conversation")
    gate = ChatConcurrencyGate(
        config=ChatConcurrencyConfig(
            enabled=False,
            global_limit=0,
            per_instance_limit=0,
            wait_timeout_seconds=0,
            slot_ttl_seconds=900,
        )
    )
    lease = await gate.acquire()

    @asynccontextmanager
    async def database():
        yield object()

    monkeypatch.setattr(chat_voice, "async_session", database)
    monkeypatch.setattr(chat_voice, "call_request", lambda *_: object())
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(chat_voice, "acquire_audio_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(chat_voice, "resolve_realtime_route", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_voice, "prepare_gateway_call", AsyncMock())
    # Startup timing is isolated from durable receipt recovery in this fixture.
    monkeypatch.setattr(chat_voice, "recover_call_work", AsyncMock(return_value=[]))
    cleanup = AsyncMock()
    monkeypatch.setattr(chat_voice, "remove_empty_created_call_conversation", cleanup)
    monkeypatch.setattr(chat_voice, "CALL_SETUP_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(chat_voice, "REALTIME_SETUP_TIMEOUT_SECONDS", 0.04)
    return SimpleNamespace(ws=ws, lease=lease, cleanup=cleanup)


@pytest.mark.parametrize("stall", ["connection", "session_ready"])
async def test_realtime_startup_deadline_covers_connection_and_session_then_falls_back(monkeypatch, endpoint, stall):
    provider = Provider()
    stopped = asyncio.Event()

    @asynccontextmanager
    async def connection(_):
        try:
            if stall == "connection":
                await asyncio.Event().wait()
            # Time already spent obtaining credentials is part of the same
            # deadline; an absent session.updated cannot start a new budget.
            await asyncio.sleep(0.025)
            yield provider
        finally:
            stopped.set()

    monkeypatch.setattr(
        chat_voice,
        "resolve_realtime_route",
        AsyncMock(
            return_value=RealtimeRoute(
                "test",
                "https://ai-gateway.vercel.sh/v1",
                "gpt-realtime",
                True,
                provider="vercel",
            )
        ),
    )
    monkeypatch.setattr(chat_voice, "open_realtime_connection", connection)
    task = asyncio.create_task(chat_voice.live_voice(endpoint.ws))
    await eventually(lambda: any(e["type"] == "ready" for e in endpoint.ws.events))
    assert stopped.is_set()
    chat_voice.prepare_gateway_call.assert_awaited_once()
    # A connected fallback must outlive both startup deadlines.
    await asyncio.sleep(0.17)
    assert not task.done()
    await endpoint.ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)
    assert endpoint.ws.closed and endpoint.lease._released
    assert not any(e["type"] == "error" for e in endpoint.ws.events)
    endpoint.cleanup.assert_not_awaited()


@pytest.mark.parametrize("ready", [False, True])
async def test_native_ready_clears_startup_deadlines_and_hangup_never_falls_back(monkeypatch, endpoint, ready):
    provider = Provider()
    monkeypatch.setattr(
        chat_voice,
        "resolve_realtime_route",
        AsyncMock(
            return_value=RealtimeRoute(
                "test",
                "https://ai-gateway.vercel.sh/v1",
                "gpt-realtime",
                True,
                provider="vercel",
            )
        ),
    )
    monkeypatch.setattr(chat_voice, "open_realtime_connection", lambda _: provider)
    task = asyncio.create_task(chat_voice.live_voice(endpoint.ws))
    await eventually(lambda: bool(provider.events))
    if ready:
        await provider.incoming.put({"type": "session.updated"})
        await eventually(lambda: any(e["type"] == "ready" for e in endpoint.ws.events))
        await asyncio.sleep(0.17)
        assert not task.done()
    await endpoint.ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)
    chat_voice.prepare_gateway_call.assert_not_awaited()
    assert endpoint.ws.closed and endpoint.lease._released
    assert not any(e["type"] == "error" for e in endpoint.ws.events)
    if ready:
        endpoint.cleanup.assert_not_awaited()
    else:
        endpoint.cleanup.assert_awaited_once()


@pytest.mark.parametrize("stage", ["authorization", "gateway"])
async def test_overall_setup_timeout_closes_socket_and_releases_acquired_slot(monkeypatch, endpoint, stage):
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        chat_voice, "resolve_call_scope" if stage == "authorization" else "prepare_gateway_call", blocked
    )
    await asyncio.wait_for(chat_voice.live_voice(endpoint.ws), 2)
    assert endpoint.ws.closed
    assert endpoint.ws.events == [
        {"type": "error", "message": "Voice connection timed out. Please try again.", "status": 504}
    ]
    if stage == "gateway":
        assert endpoint.lease._released
        endpoint.cleanup.assert_awaited_once()
    else:
        chat_voice.acquire_audio_lease.assert_not_awaited()
        endpoint.cleanup.assert_not_awaited()
