"""OpenAI Realtime contracts for the Twilio Voice channel."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _response_done(
    *,
    event_id: str = "event-1",
    response_id: str = "response-1",
    response_status: str = "completed",
    item_status: str = "completed",
    name: str = "manor_agent_reply",
    call_id: str = "call-1",
    arguments: str = '{"utterance":"hello"}',
    usage: dict | None = None,
) -> dict:
    response = {
        "id": response_id,
        "status": response_status,
        "output": [
            {
                "type": "function_call",
                "id": "item-1",
                "status": item_status,
                "name": name,
                "call_id": call_id,
                "arguments": arguments,
            }
        ],
    }
    if usage is not None:
        response["usage"] = usage
    return {
        "type": "response.done",
        "event_id": event_id,
        "response": response,
    }


def test_realtime_session_update_is_pcmu_and_exposes_only_manor_bridge():
    from packages.core.services.voice.realtime import build_realtime_session_update

    event = build_realtime_session_update(model="gpt-realtime", voice="alloy")

    assert event["type"] == "session.update"
    session = event["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime"
    assert session["output_modalities"] == ["text"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "create_response": False,
        "interrupt_response": True,
    }
    assert session["tools"] == [
        {
            "type": "function",
            "name": "manor_agent_reply",
            "description": (
                "Forward the caller's current utterance to the bound Manor Agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {"utterance": {"type": "string"}},
                "required": ["utterance"],
                "additionalProperties": False,
            },
        }
    ]
    assert session["tool_choice"] == {
        "type": "function",
        "name": "manor_agent_reply",
    }
    serialized = json.dumps(event).lower()
    assert "workspace" not in serialized
    assert "agent_id" not in serialized


def test_realtime_transcription_session_uses_asr_without_bridge_tool():
    from packages.core.services.voice.realtime import build_realtime_session_update

    event = build_realtime_session_update(
        model="openai/gpt-realtime-mini",
        voice="alloy",
        input_transcription_model="gpt-4o-mini-transcribe",
    )

    session = event["session"]
    assert session["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-mini-transcribe"
    }
    assert "tools" not in session
    assert "tool_choice" not in session


def test_realtime_event_builders_keep_provider_output_constrained():
    from packages.core.services.voice.realtime import (
        build_function_output_event,
        build_input_audio_event,
        build_spoken_response_event,
    )

    assert build_input_audio_event("base64-pcmu") == {
        "type": "input_audio_buffer.append",
        "audio": "base64-pcmu",
    }
    output = build_function_output_event(
        call_id="call-1",
        status="ok",
        spoken_reply="The Manor answer.",
    )
    assert output["type"] == "conversation.item.create"
    assert output["item"]["type"] == "function_call_output"
    assert output["item"]["call_id"] == "call-1"
    assert json.loads(output["item"]["output"]) == {
        "status": "ok",
        "spoken_reply": "The Manor answer.",
    }

    response = build_spoken_response_event(spoken_reply="The Manor answer.")
    assert response == {
        "type": "response.create",
        "response": {
            "output_modalities": ["audio"],
            "tool_choice": "none",
            "instructions": (
                "Speak exactly this Manor response and nothing else: "
                "The Manor answer."
            ),
        },
    }


@pytest.mark.asyncio
async def test_resolve_realtime_route_uses_admin_vercel_before_managed_openai(
    monkeypatch,
):
    from packages.core.services.voice import realtime

    async def no_byok(_entity_id):
        return None

    async def managed_route(model_id, **kwargs):
        assert model_id == "openai/gpt-realtime"
        assert kwargs == {
            "reason": "channel.voice.realtime.official_provider_key",
            "provider_chain": ("vercel", "openai"),
        }
        return SimpleNamespace(
            api_key="managed-vercel-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            provider="vercel",
            source_detail="db",
        )

    monkeypatch.delenv("OPENAI_REALTIME_MODEL", raising=False)
    monkeypatch.delenv("VERCEL_REALTIME_MODEL", raising=False)
    monkeypatch.setattr(realtime, "_resolve_realtime_byok", no_byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        managed_route,
    )

    route = await realtime.resolve_realtime_route("entity-realtime")

    assert route == realtime.RealtimeRoute(
        api_key="managed-vercel-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
        auth_method="api-key",
    )


@pytest.mark.asyncio
async def test_resolve_realtime_route_accepts_managed_openai_fallback(monkeypatch):
    from packages.core.services.voice import realtime

    async def no_byok(_entity_id):
        return None

    async def managed_route(_model_id, **kwargs):
        assert kwargs["provider_chain"] == ("vercel", "openai")
        return SimpleNamespace(
            api_key="managed-openai-key",
            base_url="https://api.openai.com/v1",
            provider="openai",
            source_detail="db",
        )

    monkeypatch.setattr(realtime, "_resolve_realtime_byok", no_byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        managed_route,
    )

    route = await realtime.resolve_realtime_route("entity-realtime")

    assert route == realtime.RealtimeRoute(
        api_key="managed-openai-key",
        base_url="https://api.openai.com/v1",
        model="openai/gpt-realtime",
        byok=False,
        provider="openai",
        auth_method="api-key",
    )


@pytest.mark.asyncio
async def test_resolve_realtime_route_rejects_untrusted_vercel_gateway(monkeypatch):
    from packages.core.services.voice import realtime

    async def no_byok(_entity_id):
        return None

    async def untrusted_route(*_args, **_kwargs):
        return SimpleNamespace(
            api_key="managed-vercel-key",
            base_url="https://credential-thief.example/v1",
            provider="vercel",
            source_detail="db",
        )

    monkeypatch.setattr(realtime, "_resolve_realtime_byok", no_byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        untrusted_route,
    )

    with pytest.raises(RuntimeError, match="official Vercel Gateway"):
        await realtime.resolve_realtime_route("entity-realtime")


@pytest.mark.asyncio
async def test_resolve_realtime_route_prefers_native_openai_byok(monkeypatch):
    from packages.core.services.voice import realtime

    async def native_byok(_entity_id):
        return {
            "llm_api_key": "sk-user-openai",
            "llm_base_url": "https://api.openai.com/v1/",
        }

    async def managed_route(*_args, **_kwargs):
        raise AssertionError("managed route must not be queried for native BYOK")

    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "openai/gpt-realtime")
    monkeypatch.setattr(realtime, "_resolve_realtime_byok", native_byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        managed_route,
    )

    route = await realtime.resolve_realtime_route("entity-realtime")

    assert route == realtime.RealtimeRoute(
        api_key="sk-user-openai",
        base_url="https://api.openai.com/v1",
        model="openai/gpt-realtime",
        byok=True,
        provider="openai",
        auth_method="api-key",
    )


@pytest.mark.asyncio
async def test_resolve_realtime_route_uses_actor_for_primary_byok(monkeypatch):
    from packages.core.services.voice import realtime

    async def actor_byok(entity_id, user_id):
        assert (entity_id, user_id) == ("entity-realtime", "owner-user")
        return {
            "llm_api_key": "sk-owner-openai",
            "llm_base_url": "https://api.openai.com/v1",
        }

    monkeypatch.setattr(realtime, "_resolve_realtime_byok", actor_byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        AsyncMock(side_effect=AssertionError("managed route must not be used")),
    )

    route = await realtime.resolve_realtime_route(
        "entity-realtime",
        user_id="owner-user",
    )

    assert route.model == "openai/gpt-realtime"
    assert route.byok is True


@pytest.mark.asyncio
async def test_optional_realtime_route_returns_none_for_browser_fallback(monkeypatch):
    from packages.core.services.voice import realtime

    monkeypatch.setattr(realtime, "_resolve_realtime_byok", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        AsyncMock(return_value=None),
    )

    assert (
        await realtime.resolve_realtime_route(
            "entity-realtime",
            user_id="owner-user",
            required=False,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        (
            {
                "llm_api_key": "sk-user-openai",
                "llm_base_url": "https://compatible.example/v1",
            },
            "native OpenAI BYOK",
        ),
        ({"llm_api_key": ""}, "OpenRouter.*does not provide"),
    ],
)
async def test_resolve_realtime_route_rejects_unsupported_routes(
    monkeypatch,
    metadata,
    error,
):
    from packages.core.services.voice import realtime

    async def byok(_entity_id):
        return metadata

    async def missing_managed_route(*_args, **_kwargs):
        return None

    monkeypatch.setattr(realtime, "_resolve_realtime_byok", byok)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        missing_managed_route,
    )

    with pytest.raises(RuntimeError, match=error):
        await realtime.resolve_realtime_route("entity-realtime")


@pytest.mark.asyncio
async def test_vercel_realtime_connection_mints_token_and_uses_gateway_protocols(
    monkeypatch,
):
    from packages.core.services.voice import realtime

    http_calls: list[dict] = []
    websocket_calls: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "vcst_test", "expiresAt": 12345}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            http_calls.append({"url": url, "headers": headers, "json": json})
            return Response()

    class Socket:
        async def send(self, _payload):
            return None

        async def recv(self):
            return json.dumps({"type": "session-updated", "raw": {}})

    class SocketManager:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *_args):
            return False

    def connect(url, *, subprotocols, open_timeout, max_size):
        websocket_calls.append(
            {
                "url": url,
                "subprotocols": subprotocols,
                "open_timeout": open_timeout,
                "max_size": max_size,
            }
        )
        return SocketManager()

    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **_kwargs: HttpClient())
    monkeypatch.setattr(realtime.websockets, "connect", connect)
    route = realtime.RealtimeRoute(
        api_key="vercel-admin-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
        auth_method="api-key",
    )

    async with realtime.open_realtime_connection(route):
        pass

    assert http_calls == [
        {
            "url": "https://ai-gateway.vercel.sh/v1/realtime/client-secrets",
            "headers": {
                "Authorization": "Bearer vercel-admin-key",
                "ai-gateway-protocol-version": "0.0.1",
                "ai-gateway-auth-method": "api-key",
            },
            "json": {
                "model": "openai/gpt-realtime-mini",
                "expiresIn": 60,
            },
        }
    ]
    assert websocket_calls == [
        {
            "url": (
                "wss://ai-gateway.vercel.sh/v4/ai/realtime-model"
                "?ai-model-id=openai%2Fgpt-realtime-mini"
            ),
            "subprotocols": [
                "ai-gateway-realtime.v1",
                "ai-gateway-auth.vcst_test",
            ],
            "open_timeout": 15,
            "max_size": 2 * 1024 * 1024,
        }
    ]


@pytest.mark.asyncio
async def test_vercel_realtime_connection_rejects_non_client_secret(monkeypatch):
    from packages.core.services.voice import realtime

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "not-a-vercel-client-secret"}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **_kwargs: HttpClient())
    route = realtime.RealtimeRoute(
        api_key="vercel-admin-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
    )

    with pytest.raises(RuntimeError, match="client secret"):
        async with realtime.open_realtime_connection(route):
            pass


@pytest.mark.asyncio
async def test_vercel_realtime_connection_adapts_events_and_unwraps_raw(monkeypatch):
    from packages.core.services.voice import realtime

    sent: list[dict] = []
    raw_response = _response_done()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "vcst_test"}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    class Socket:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            return json.dumps(
                {
                    "type": "response-done",
                    "responseId": "response-1",
                    "status": "completed",
                    "raw": raw_response,
                }
            )

    class SocketManager:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **_kwargs: HttpClient())
    monkeypatch.setattr(
        realtime.websockets,
        "connect",
        lambda *_args, **_kwargs: SocketManager(),
    )
    route = realtime.RealtimeRoute(
        api_key="vercel-admin-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
    )

    async with realtime.open_realtime_connection(route) as connection:
        await connection.send(
            realtime.build_realtime_session_update(
                model=route.model,
                voice="alloy",
                input_transcription_model="gpt-4o-mini-transcribe",
            )
        )
        await connection.send(realtime.build_input_audio_event("base64-pcmu"))
        received = await connection.recv()

    assert sent[0]["type"] == "session-update"
    assert sent[0]["config"]["voice"] == "alloy"
    assert sent[0]["config"]["inputAudioFormat"] == {"type": "audio/pcmu"}
    assert sent[0]["config"]["outputAudioFormat"] == {"type": "audio/pcmu"}
    assert sent[0]["config"]["turnDetection"] == {"type": "server-vad"}
    assert sent[0]["config"]["inputAudioTranscription"] == {
        "model": "gpt-4o-mini-transcribe"
    }
    assert (
        sent[0]["config"]["providerOptions"]["audio"]["input"]
        ["turn_detection"]["create_response"]
        is False
    )
    assert "tools" not in sent[0]["config"]
    assert "tool_choice" not in sent[0]["config"]["providerOptions"]
    assert sent[1] == {"type": "input-audio-append", "audio": "base64-pcmu"}
    assert received == raw_response


@pytest.mark.asyncio
async def test_vercel_realtime_connection_normalizes_standard_audio_events():
    from packages.core.services.voice import realtime

    class Socket:
        def __init__(self):
            self.events = iter(
                [
                    {
                        "type": "audio-delta",
                        "responseId": "response-1",
                        "itemId": "item-1",
                        "delta": "base64-pcmu",
                        "raw": {"type": "response.audio.delta"},
                    },
                    {
                        "type": "audio-done",
                        "responseId": "response-1",
                        "itemId": "item-1",
                        "raw": {"type": "response.audio.done"},
                    },
                ]
            )

        async def recv(self):
            return json.dumps(next(self.events))

    route = realtime.RealtimeRoute(
        api_key="vercel-admin-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
    )
    connection = realtime._VercelRealtimeConnection(route)
    connection._socket = Socket()

    assert await connection.recv() == {
        "type": "response.output_audio.delta",
        "response_id": "response-1",
        "item_id": "item-1",
        "delta": "base64-pcmu",
    }
    assert await connection.recv() == {
        "type": "response.output_audio.done",
        "response_id": "response-1",
        "item_id": "item-1",
    }


@pytest.mark.asyncio
async def test_vercel_realtime_connection_supports_control_events_and_iteration():
    from packages.core.services.voice import realtime

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            return json.dumps(
                {
                    "type": "audio-done",
                    "responseId": "response-1",
                    "itemId": "item-1",
                }
            )

    socket = Socket()
    connection = realtime._VercelRealtimeConnection(
        realtime.RealtimeRoute(
            api_key="vercel-admin-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        )
    )
    connection._socket = socket

    await connection.send({"type": "input_audio_buffer.clear"})
    await connection.send({"type": "response.cancel"})
    await connection.send(
        {
            "type": "conversation.item.truncate",
            "item_id": "item-1",
            "content_index": 0,
            "audio_end_ms": 120,
        }
    )

    assert socket.sent == [
        {"type": "input-audio-clear"},
        {"type": "response-cancel"},
        {
            "type": "conversation-item-truncate",
            "itemId": "item-1",
            "contentIndex": 0,
            "audioEndMs": 120,
        },
    ]
    assert await anext(connection) == {
        "type": "response.output_audio.done",
        "response_id": "response-1",
        "item_id": "item-1",
    }


@pytest.mark.asyncio
async def test_vercel_realtime_connection_verifies_manor_response_controls():
    from packages.core.services.voice import realtime

    session_update = realtime.build_realtime_session_update(
        model="openai/gpt-realtime-mini"
    )

    class Socket:
        async def send(self, _payload):
            return None

        async def recv(self):
            session = session_update["session"] | {
                "tool_choice": "auto",
            }
            session["audio"] = {
                **session_update["session"]["audio"],
                "input": {
                    **session_update["session"]["audio"]["input"],
                    "turn_detection": {"create_response": True},
                },
            }
            return json.dumps(
                {
                    "type": "session-updated",
                    "raw": {"type": "session.updated", "session": session},
                }
            )

    connection = realtime._VercelRealtimeConnection(
        realtime.RealtimeRoute(
            api_key="vercel-admin-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        )
    )
    connection._socket = Socket()
    await connection.send(session_update)

    with pytest.raises(RuntimeError, match="response controls"):
        await connection.recv()


@pytest.mark.asyncio
async def test_vercel_realtime_connection_switches_between_bridge_and_speech_controls():
    from packages.core.services.voice import realtime

    class Socket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    socket = Socket()
    connection = realtime._VercelRealtimeConnection(
        realtime.RealtimeRoute(
            api_key="vercel-admin-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        )
    )
    connection._socket = socket
    await connection.send(
        realtime.build_realtime_session_update(model="openai/gpt-realtime-mini")
    )
    socket.sent.clear()

    await connection.send(
        realtime.build_spoken_response_event(spoken_reply="Manor answer")
    )
    await connection.send({"type": "response.create"})

    assert socket.sent[0]["type"] == "session-update"
    assert socket.sent[0]["config"]["providerOptions"]["tool_choice"] == "none"
    assert socket.sent[1]["type"] == "response-create"
    assert socket.sent[2]["type"] == "session-update"
    assert socket.sent[2]["config"]["providerOptions"]["tool_choice"] == {
        "type": "function",
        "name": "manor_agent_reply",
    }
    assert socket.sent[3] == {"type": "response-create"}


@pytest.mark.asyncio
async def test_vercel_spoken_response_does_not_depend_on_provider_tool_choice(
    monkeypatch,
):
    from packages.core.services.voice import realtime

    sent: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "vcst_test"}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    class Socket:
        async def send(self, payload):
            sent.append(json.loads(payload))

    class SocketManager:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **_kwargs: HttpClient())
    monkeypatch.setattr(
        realtime.websockets,
        "connect",
        lambda *_args, **_kwargs: SocketManager(),
    )
    route = realtime.RealtimeRoute(
        api_key="vercel-admin-key",
        base_url="https://ai-gateway.vercel.sh/v1",
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
    )

    async with realtime.open_realtime_connection(route) as connection:
        await connection.send(
            realtime.build_spoken_response_event(spoken_reply="Manor answer")
        )

    assert sent == [
        {
            "type": "session-update",
            "config": {"providerOptions": {"tool_choice": "none"}},
        },
        {
            "type": "response-create",
            "options": {
                "modalities": ["audio"],
                "instructions": (
                    "Speak exactly this Manor response and nothing else: Manor answer"
                ),
            },
        }
    ]


@pytest.mark.parametrize(
    "model",
    ["google/gemini-live", "openai/gpt-4o-realtime-preview", "gpt-audio"],
)
def test_realtime_model_rejects_non_ga_or_non_openai_models(monkeypatch, model):
    from packages.core.services.voice import realtime

    monkeypatch.setenv("OPENAI_REALTIME_MODEL", model)

    with pytest.raises(RuntimeError, match="gpt-realtime"):
        realtime.resolve_realtime_model()


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-realtime-1.5", "openai/gpt-audio-mini"],
)
def test_vercel_realtime_only_supports_realtime_mini(monkeypatch, model):
    from packages.core.services.voice import realtime

    monkeypatch.setenv("VERCEL_REALTIME_MODEL", model)

    with pytest.raises(RuntimeError, match="openai/gpt-realtime-mini"):
        realtime.resolve_vercel_realtime_model()


def test_bridge_call_executes_only_from_completed_response_done():
    from packages.core.services.voice.realtime import (
        BridgeCall,
        parse_completed_bridge_call,
    )

    seen_event_ids: set[str] = set()
    seen_call_ids: set[str] = set()
    arguments_done = {
        "type": "response.function_call_arguments.done",
        "event_id": "arguments-event",
        "response_id": "response-1",
        "item_id": "item-1",
        "output_index": 0,
        "call_id": "call-1",
        "arguments": '{"utterance":"must not execute yet"}',
    }

    assert parse_completed_bridge_call(
        arguments_done,
        seen_event_ids=seen_event_ids,
        seen_call_ids=seen_call_ids,
    ) is None
    assert seen_call_ids == set()

    call = parse_completed_bridge_call(
        _response_done(),
        seen_event_ids=seen_event_ids,
        seen_call_ids=seen_call_ids,
    )

    assert call == BridgeCall(
        call_id="call-1",
        utterance="hello",
        response_id="response-1",
    )
    assert seen_call_ids == {"call-1"}


def test_bridge_call_accepts_sdk_response_done_event():
    from openai.types.realtime import ResponseDoneEvent

    from packages.core.services.voice.realtime import parse_completed_bridge_call

    event = ResponseDoneEvent.model_validate(_response_done())

    call = parse_completed_bridge_call(
        event,
        seen_event_ids=set(),
        seen_call_ids=set(),
    )

    assert call is not None
    assert call.utterance == "hello"


@pytest.mark.parametrize(
    "event",
    [
        _response_done(response_status="cancelled"),
        _response_done(response_status="incomplete"),
        _response_done(item_status="incomplete"),
        _response_done(name="another_function"),
        _response_done(arguments="not-json"),
        _response_done(arguments="{}"),
        _response_done(arguments='{"utterance":"   "}'),
        _response_done(arguments='{"utterance":123}'),
        _response_done(arguments=json.dumps({"utterance": "x" * 4001})),
        _response_done(call_id=""),
    ],
)
def test_bridge_call_rejects_incomplete_or_invalid_provider_output(event):
    from packages.core.services.voice.realtime import parse_completed_bridge_call

    assert parse_completed_bridge_call(
        event,
        seen_event_ids=set(),
        seen_call_ids=set(),
    ) is None


def test_bridge_call_deduplicates_provider_event_and_call_ids():
    from packages.core.services.voice.realtime import parse_completed_bridge_call

    seen_event_ids: set[str] = set()
    seen_call_ids: set[str] = set()
    first = _response_done()
    assert parse_completed_bridge_call(
        first,
        seen_event_ids=seen_event_ids,
        seen_call_ids=seen_call_ids,
    ) is not None

    assert parse_completed_bridge_call(
        first,
        seen_event_ids=seen_event_ids,
        seen_call_ids=seen_call_ids,
    ) is None
    assert parse_completed_bridge_call(
        _response_done(event_id="event-2"),
        seen_event_ids=seen_event_ids,
        seen_call_ids=seen_call_ids,
    ) is None


@pytest.mark.asyncio
async def test_shared_realtime_engine_parses_turn_and_sends_constrained_outcome():
    from packages.core.services.voice.realtime import (
        RealtimeRoute,
        RealtimeVoiceEngine,
        VoiceAgentOutcome,
    )

    class Connection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    connection = Connection()
    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "sk-owner",
            "https://api.openai.com/v1",
            "openai/gpt-realtime",
            True,
        ),
        usage_scope={"entity_id": "entity", "user_id": "owner"},
        source="voice_test",
        response_operation_prefix="voice-test-response",
    )
    engine.connection = connection

    first = await engine.inspect_provider_event(_response_done())
    duplicate = await engine.inspect_provider_event(_response_done())

    assert first.bridge_call is not None
    assert first.bridge_call.utterance == "hello"
    assert duplicate.duplicate_response is True
    assert duplicate.bridge_call is None

    await engine.send_voice_outcome(
        first.bridge_call,
        VoiceAgentOutcome(status="ok", spoken_reply="Manor answer"),
    )

    assert connection.sent[0]["type"] == "conversation.item.create"
    assert json.loads(connection.sent[0]["item"]["output"]) == {
        "status": "ok",
        "spoken_reply": "Manor answer",
    }
    assert connection.sent[1]["type"] == "response.create"
    assert connection.sent[1]["response"]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_shared_realtime_engine_waits_for_verified_session_before_adapter_io():
    from packages.core.services.voice.realtime import (
        RealtimeRoute,
        RealtimeVoiceEngine,
        build_realtime_session_update,
    )

    connection = SimpleNamespace(
        send=AsyncMock(),
        recv=AsyncMock(
            side_effect=[
                {"type": "session.created"},
                {"type": "session.updated", "session": {"id": "session-1"}},
            ]
        ),
    )
    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "sk-owner",
            "https://api.openai.com/v1",
            "openai/gpt-realtime",
            True,
        ),
        usage_scope={"entity_id": "entity", "user_id": "owner"},
        source="voice_test",
        response_operation_prefix="voice-test-response",
    )
    engine.connection = connection
    update = build_realtime_session_update(model=engine.route.model)

    ready = await engine.initialize_session(update)

    connection.send.assert_awaited_once_with(update)
    assert connection.recv.await_count == 2
    assert ready["type"] == "session.updated"


@pytest.mark.asyncio
async def test_shared_realtime_engine_reports_session_control_timeout():
    from packages.core.services.voice.realtime import (
        RealtimeRoute,
        RealtimeVoiceEngine,
        build_realtime_session_update,
    )

    async def never_ready():
        await asyncio.Future()

    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "sk-owner",
            "https://api.openai.com/v1",
            "openai/gpt-realtime",
            True,
        ),
        usage_scope={"entity_id": "entity", "user_id": "owner"},
        source="voice_test",
        response_operation_prefix="voice-test-response",
    )
    engine.connection = SimpleNamespace(send=AsyncMock(), recv=never_ready)

    with pytest.raises(RuntimeError, match="accept session controls in time"):
        await engine.initialize_session(
            build_realtime_session_update(model=engine.route.model),
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_shared_realtime_engine_gates_each_managed_response(monkeypatch):
    from packages.core.services.voice.realtime import RealtimeRoute, RealtimeVoiceEngine

    connection = SimpleNamespace(send=AsyncMock())
    check_access = AsyncMock()
    assert_credit = AsyncMock()
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_current_billing_context",
        lambda: SimpleNamespace(entity_id="entity", source="voice_test", suppress=False),
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        assert_credit,
    )
    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "managed",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        ),
        usage_scope={"entity_id": "entity", "user_id": "owner"},
        source="voice_test",
        response_operation_prefix="voice-test-response",
        check_access=check_access,
    )
    engine.connection = connection

    await engine.create_response({"type": "response.create"})

    check_access.assert_awaited_once()
    assert_credit.assert_awaited_once_with("entity", source="voice_test")
    connection.send.assert_awaited_once_with({"type": "response.create"})


@pytest.mark.asyncio
async def test_shared_realtime_engine_reuses_active_call_reservation(monkeypatch):
    from packages.core.ai.runtime import runtime_llm_billing_context
    from packages.core.services.voice.billing import voice_call_reservation_scope
    from packages.core.services.voice.realtime import RealtimeRoute, RealtimeVoiceEngine

    connection = SimpleNamespace(send=AsyncMock())
    check_access = AsyncMock()
    assert_credit = AsyncMock()
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        assert_credit,
    )
    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "managed",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        ),
        usage_scope={"entity_id": "entity", "user_id": "owner"},
        source="voice_test",
        response_operation_prefix="voice-test-response",
        check_access=check_access,
    )
    engine.connection = connection

    async with runtime_llm_billing_context("entity", source="voice_test"):
        with voice_call_reservation_scope("reservation-1"):
            await engine.create_response({"type": "response.create"})

    check_access.assert_awaited_once()
    assert_credit.assert_not_awaited()
    connection.send.assert_awaited_once_with({"type": "response.create"})


@pytest.mark.asyncio
async def test_shared_realtime_engine_settles_vercel_usage_once(monkeypatch):
    from packages.core.services.voice.realtime import RealtimeRoute, RealtimeVoiceEngine

    db = AsyncMock()

    @asynccontextmanager
    async def database():
        yield db

    record = AsyncMock()
    monkeypatch.setattr("packages.core.database.async_session", database)
    monkeypatch.setattr("packages.core.services.usage_service.record_llm_usage", record)
    engine = RealtimeVoiceEngine(
        route=RealtimeRoute(
            "managed",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        ),
        usage_scope={
            "entity_id": "entity",
            "user_id": "owner",
            "workspace_id": "workspace",
            "agent_id": "agent",
        },
        source="voice_test",
        response_operation_prefix="voice-test-response",
    )
    event = _response_done(
        usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
    )

    await engine.settle_response(event, conversation_id="conversation")
    await engine.settle_response(event, conversation_id="conversation")

    record.assert_awaited_once()
    fields = record.await_args.kwargs
    assert fields["operation_id"] == "voice-test-response:response-1"
    assert fields["usage"]["provider"] == "vercel"
    assert fields["usage"]["pricing_source"] == "vercel"
    assert fields["conversation_id"] == "conversation"
    assert fields["strict"] is True


def test_completed_input_transcription_creates_one_agent_turn():
    from packages.core.services.voice.realtime import (
        BridgeCall,
        parse_completed_input_transcription,
    )

    seen_item_ids: set[str] = set()
    event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "event_id": "transcription-event-1",
        "item_id": "input-item-1",
        "content_index": 0,
        "transcript": " Hello, who are you? ",
        "usage": {
            "type": "tokens",
            "input_tokens": 8,
            "output_tokens": 5,
            "total_tokens": 13,
        },
    }

    assert parse_completed_input_transcription(
        event,
        seen_item_ids=seen_item_ids,
    ) == BridgeCall(
        call_id=None,
        utterance="Hello, who are you?",
        response_id="input-item-1",
    )
    assert (
        parse_completed_input_transcription(
            event,
            seen_item_ids=seen_item_ids,
        )
        is None
    )


@pytest.mark.parametrize("transcript", ["", "   ", "x" * 4001])
def test_completed_input_transcription_rejects_invalid_text(transcript):
    from packages.core.services.voice.realtime import (
        parse_completed_input_transcription,
    )

    assert (
        parse_completed_input_transcription(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "input-item-1",
                "transcript": transcript,
            },
            seen_item_ids=set(),
        )
        is None
    )


def test_extract_realtime_usage_preserves_audio_and_cache_details():
    from openai.types.realtime import ResponseDoneEvent

    from packages.core.services.voice.realtime import extract_realtime_usage

    event = ResponseDoneEvent.model_validate(
        _response_done(
            usage={
                "input_tokens": 120,
                "output_tokens": 45,
                "total_tokens": 165,
                "input_token_details": {
                    "text_tokens": 20,
                    "audio_tokens": 100,
                    "cached_tokens": 30,
                    "cached_tokens_details": {
                        "text_tokens": 5,
                        "audio_tokens": 25,
                    },
                },
                "output_token_details": {
                    "text_tokens": 5,
                    "audio_tokens": 40,
                },
            },
        )
    )

    usage = extract_realtime_usage(event, model="gpt-realtime", byok=False)

    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "total_tokens": 165,
        "cache_read_input_tokens": 30,
        "audio_input_tokens": 100,
        "audio_output_tokens": 40,
        "cached_audio_input_tokens": 25,
        "model": "openai/gpt-realtime",
        "provider": "openai",
        "byok": False,
        "billing_mode": "platform",
        "api_key_source": "platform",
        "pricing_source": "official",
    }


def test_extract_realtime_usage_clamps_invalid_counts_and_marks_byok():
    from packages.core.services.voice.realtime import extract_realtime_usage

    event = _response_done(
        usage={
            "input_tokens": -2,
            "output_tokens": 3,
            "total_tokens": 0,
            "input_token_details": {
                "audio_tokens": -1,
                "cached_tokens": 10,
                "cached_tokens_details": {"audio_tokens": 20},
            },
            "output_token_details": {"audio_tokens": 99},
        },
    )

    usage = extract_realtime_usage(event, model="openai/gpt-realtime", byok=True)

    assert usage is not None
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 3
    assert usage["cache_read_input_tokens"] == 0
    assert usage["audio_input_tokens"] == 0
    assert usage["audio_output_tokens"] == 3
    assert usage["cached_audio_input_tokens"] == 0
    assert usage["byok"] is True
    assert usage["billing_mode"] == "byok"
    assert usage["api_key_source"] == "byok"
    assert usage["pricing_source"] == "byok"


def test_extract_realtime_usage_attributes_managed_vercel_route():
    from packages.core.services.voice.realtime import extract_realtime_usage

    usage = extract_realtime_usage(
        _response_done(
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
        ),
        model="openai/gpt-realtime-mini",
        byok=False,
        provider="vercel",
    )

    assert usage is not None
    assert usage["model"] == "openai/gpt-realtime-mini"
    assert usage["provider"] == "vercel"
    assert usage["pricing_source"] == "vercel"


def test_extract_input_transcription_usage_attributes_managed_vercel_route():
    from packages.core.services.voice.realtime import (
        extract_input_transcription_usage,
    )

    usage = extract_input_transcription_usage(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "transcription-event-1",
            "item_id": "input-item-1",
            "transcript": "hello",
            "usage": {
                "type": "tokens",
                "input_tokens": 8,
                "output_tokens": 5,
                "total_tokens": 13,
                "input_token_details": {
                    "audio_tokens": 8,
                    "text_tokens": 0,
                },
            },
        },
        byok=False,
        provider="vercel",
    )

    assert usage == {
        "prompt_tokens": 8,
        "completion_tokens": 5,
        "total_tokens": 13,
        "audio_input_tokens": 8,
        "model": "openai/gpt-4o-mini-transcribe",
        "provider": "vercel",
        "byok": False,
        "billing_mode": "platform",
        "api_key_source": "platform",
        "pricing_source": "vercel",
    }


def test_extract_realtime_usage_ignores_events_without_usage():
    from packages.core.services.voice.realtime import extract_realtime_usage

    assert extract_realtime_usage(
        _response_done(),
        model="gpt-realtime",
        byok=False,
    ) is None


@pytest.mark.asyncio
async def test_voice_session_relays_pcmu_without_transcoding():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, payload):
            self.sent.append(json.loads(payload))

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    twilio = TwilioSocket()
    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        twilio,
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime
    await session._handle_twilio_message(
        {
            "event": "start",
            "start": {"streamSid": "stream-1", "callSid": "CA-1"},
        }
    )
    await session._handle_twilio_message(
        {
            "event": "media",
            "media": {"payload": "dHdpbGlvLXBjbXU="},
        }
    )

    assert realtime.sent[-1] == {
        "type": "input_audio_buffer.append",
        "audio": "dHdpbGlvLXBjbXU=",
    }

    await session._handle_realtime_event(
        {
            "type": "response.output_audio.delta",
            "event_id": "audio-event-1",
            "response_id": "spoken-response-1",
            "item_id": "assistant-item-1",
            "output_index": 0,
            "content_index": 0,
            "delta": "b3BlbmFpLXBjbXU=",
        }
    )

    assert twilio.sent[-1] == {
        "event": "media",
        "streamSid": "stream-1",
        "media": {"payload": "b3BlbmFpLXBjbXU="},
    }


@pytest.mark.asyncio
async def test_voice_session_explicitly_creates_response_after_server_vad_stop():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime

    await session._handle_realtime_event(
        {
            "type": "input_audio_buffer.speech_stopped",
            "event_id": "speech-stopped-1",
        }
    )

    assert realtime.sent == [{"type": "response.create"}]


@pytest.mark.asyncio
async def test_vercel_voice_session_waits_for_transcription_after_vad_stop():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime
    session._create_realtime_response = realtime.send

    await session._handle_realtime_event(
        {
            "type": "input_audio_buffer.speech_stopped",
            "event_id": "speech-stopped-1",
        }
    )

    assert realtime.sent == []


@pytest.mark.asyncio
async def test_vercel_voice_session_queues_completed_transcription():
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )
    from packages.core.services.voice.session import TwilioVoiceSession

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await session._handle_realtime_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "transcription-event-1",
            "item_id": "input-item-1",
            "content_index": 0,
            "transcript": "Hello, who are you?",
        }
    )

    assert session._bridge_calls.get_nowait() == BridgeCall(
        call_id=None,
        utterance="Hello, who are you?",
        response_id="input-item-1",
    )


@pytest.mark.asyncio
async def test_transcription_agent_outcome_sends_only_spoken_response():
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )
    from packages.core.services.voice.session import TwilioVoiceSession

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime

    await session._send_voice_outcome(
        BridgeCall(
            call_id=None,
            utterance="Hello",
            response_id="input-item-1",
        ),
        VoiceAgentOutcome(status="ok", spoken_reply="Hello from Manor."),
    )

    assert realtime.sent == [
        {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
                "tool_choice": "none",
                "instructions": (
                    "Speak exactly this Manor response and nothing else: Hello from Manor."
                ),
            },
        }
    ]


@pytest.mark.asyncio
async def test_voice_session_stop_logs_transport_counts_without_audio_content(caplog):
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        async def send_text(self, _payload):
            return None

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    caplog.set_level("INFO", logger="packages.core.services.voice.session")
    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime
    await session._handle_twilio_message(
        {
            "event": "start",
            "start": {"streamSid": "stream-1", "callSid": "CA-secret"},
        }
    )
    await session._handle_twilio_message(
        {
            "event": "media",
            "media": {"timestamp": "20", "payload": "c2VjcmV0LWF1ZGlv"},
        }
    )
    await session._handle_realtime_event(
        {"type": "input_audio_buffer.speech_started", "event_id": "vad-start"}
    )
    await session._handle_realtime_event(
        {"type": "input_audio_buffer.speech_stopped", "event_id": "vad-stop"}
    )
    await session._handle_realtime_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "transcription-event",
            "item_id": "input-item",
            "content_index": 0,
            "transcript": "private transcript",
        }
    )
    response_done = _response_done()
    response_done["response"]["output"] = []
    await session._handle_realtime_event(response_done)
    await session._handle_realtime_event(
        {
            "type": "response.output_audio.delta",
            "event_id": "audio-event",
            "response_id": "spoken-response",
            "item_id": "spoken-item",
            "content_index": 0,
            "delta": base64.b64encode(b"x" * 160).decode(),
        }
    )
    await session._handle_twilio_message({"event": "stop"})

    summary = next(
        record.getMessage()
        for record in caplog.records
        if "Twilio stream stopped" in record.getMessage()
    )
    assert "media_frames=1" in summary
    assert "media_payload_chars=16" in summary
    assert "vad_started=1" in summary
    assert "vad_stopped=1" in summary
    assert "transcriptions_completed=1" in summary
    assert "responses_done=1" in summary
    assert "output_audio_frames=1" in summary
    assert "output_audio_bytes=160" in summary
    assert "c2VjcmV0LWF1ZGlv" not in summary
    assert "private transcript" not in summary
    assert "CA-secret" not in summary


@pytest.mark.asyncio
async def test_voice_session_serializes_completed_bridge_calls_fifo():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        async def send_text(self, _payload):
            return None

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    active = 0
    max_active = 0
    calls: list[str] = []

    async def agent_callable(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        text = kwargs["text"]
        calls.append(text)
        if text == "first":
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()
        active -= 1
        return VoiceAgentOutcome(
            status="ok",
            spoken_reply=f"reply:{text}",
            conversation_id="conversation-1",
            agent_id="agent-1",
        )

    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
        billing_workspace_id="workspace-1",
        billing_agent_id="agent-1",
        channel_binding_id="binding-1",
        agent_subscription_id="subscription-1",
    )
    session._realtime = realtime
    session._state.stream_sid = "stream-1"
    session._state.call_sid = "CA-1"
    session._state.from_number = "+14155550199"
    worker = asyncio.create_task(session._agent_worker())

    await session._handle_realtime_event(
        _response_done(
            event_id="event-first",
            response_id="response-first",
            call_id="call-first",
            arguments='{"utterance":"first"}',
        )
    )
    await session._handle_realtime_event(
        _response_done(
            event_id="event-second",
            response_id="response-second",
            call_id="call-second",
            arguments='{"utterance":"second"}',
        )
    )

    await asyncio.wait_for(first_entered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert calls == ["first"]
    assert not second_entered.is_set()

    first_release.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    second_release.set()
    await session._bridge_calls.join()
    await session._bridge_calls.put(None)
    await worker

    assert calls == ["first", "second"]
    assert max_active == 1
    function_outputs = [
        event
        for event in realtime.sent
        if event.get("type") == "conversation.item.create"
    ]
    assert [event["item"]["call_id"] for event in function_outputs] == [
        "call-first",
        "call-second",
    ]
    assert [json.loads(event["item"]["output"]) for event in function_outputs] == [
        {"status": "ok", "spoken_reply": "reply:first"},
        {"status": "ok", "spoken_reply": "reply:second"},
    ]
    speech_responses = [
        event for event in realtime.sent if event.get("type") == "response.create"
    ]
    assert len(speech_responses) == 2
    assert all(
        event["response"]["tool_choice"] == "none"
        and event["response"]["output_modalities"] == ["audio"]
        for event in speech_responses
    )


@pytest.mark.asyncio
async def test_voice_session_barge_in_clears_and_truncates_bounded_audio():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, payload):
            self.sent.append(json.loads(payload))

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    twilio = TwilioSocket()
    realtime = RealtimeConnection()
    session = TwilioVoiceSession(
        twilio,
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime
    await session._handle_twilio_message(
        {"event": "start", "start": {"streamSid": "stream-1"}}
    )
    await session._handle_twilio_message(
        {
            "event": "media",
            "media": {"timestamp": "1000", "payload": "dXNlcg=="},
        }
    )
    assistant_pcmu = base64.b64encode(b"x" * 800).decode()
    audio_delta = {
        "type": "response.output_audio.delta",
        "event_id": "audio-1",
        "response_id": "response-audio-1",
        "item_id": "assistant-item-1",
        "output_index": 0,
        "content_index": 0,
        "delta": assistant_pcmu,
    }
    await session._handle_realtime_event(audio_delta)
    await session._handle_twilio_message(
        {
            "event": "media",
            "media": {"timestamp": "1060", "payload": "dXNlcjI="},
        }
    )
    await session._handle_realtime_event(
        {
            "type": "input_audio_buffer.speech_started",
            "event_id": "speech-1",
            "audio_start_ms": 900,
            "item_id": "user-item-1",
        }
    )

    assert {"event": "clear", "streamSid": "stream-1"} in twilio.sent
    truncate_events = [
        event
        for event in realtime.sent
        if event.get("type") == "conversation.item.truncate"
    ]
    assert truncate_events == [
        {
            "type": "conversation.item.truncate",
            "item_id": "assistant-item-1",
            "content_index": 0,
            "audio_end_ms": 60,
        }
    ]
    assert all(
        event.get("type") != "output_audio_buffer.clear"
        for event in realtime.sent
    )

    media_count = len([event for event in twilio.sent if event.get("event") == "media"])
    await session._handle_realtime_event(
        {**audio_delta, "event_id": "audio-late", "delta": "bGF0ZQ=="}
    )
    assert len(
        [event for event in twilio.sent if event.get("event") == "media"]
    ) == media_count


@pytest.mark.asyncio
async def test_voice_session_mark_acknowledges_completed_playback():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, payload):
            self.sent.append(json.loads(payload))

    class RealtimeConnection:
        async def send(self, _event):
            return None

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    twilio = TwilioSocket()
    session = TwilioVoiceSession(
        twilio,
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = RealtimeConnection()
    session._state.stream_sid = "stream-1"
    await session._handle_realtime_event(
        {
            "type": "response.output_audio.delta",
            "event_id": "audio-1",
            "response_id": "response-audio-1",
            "item_id": "assistant-item-1",
            "output_index": 0,
            "content_index": 0,
            "delta": base64.b64encode(b"x" * 160).decode(),
        }
    )
    await session._handle_realtime_event(
        {
            "type": "response.output_audio.done",
            "event_id": "audio-done-1",
            "response_id": "response-audio-1",
            "item_id": "assistant-item-1",
            "output_index": 0,
            "content_index": 0,
        }
    )
    mark = next(event for event in twilio.sent if event.get("event") == "mark")

    await session._handle_twilio_message(
        {"event": "mark", "mark": {"name": mark["mark"]["name"]}}
    )

    assert session._playback is None


@pytest.mark.asyncio
async def test_voice_session_long_turn_emits_one_hold_and_one_final(monkeypatch):
    from packages.core.services.voice import session as voice_session
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )

    class TwilioSocket:
        async def send_text(self, _payload):
            return None

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    release_agent = asyncio.Event()

    async def agent_callable(**_kwargs):
        await release_agent.wait()
        return VoiceAgentOutcome(status="ok", spoken_reply="final answer")

    monkeypatch.setattr(voice_session, "_HOLD_AFTER_SECONDS", 0.01)
    realtime = RealtimeConnection()
    session = voice_session.TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
        hold_message="still working",
    )
    session._realtime = realtime

    turn = asyncio.create_task(
        session._run_agent_turn(
            BridgeCall(
                call_id="call-1",
                utterance="slow request",
                response_id="response-1",
            )
        )
    )
    await asyncio.sleep(0.03)
    hold_responses = [
        event
        for event in realtime.sent
        if event.get("type") == "response.create"
        and event["response"].get("conversation") == "none"
    ]
    assert len(hold_responses) == 1
    assert "still working" in hold_responses[0]["response"]["instructions"]

    release_agent.set()
    await turn

    response_events = [
        event for event in realtime.sent if event.get("type") == "response.create"
    ]
    assert len(response_events) == 2
    assert sum(
        event["response"].get("conversation") == "none"
        for event in response_events
    ) == 1
    assert "final answer" in response_events[-1]["response"]["instructions"]
    assert len(
        [
            event
            for event in realtime.sent
            if event.get("type") == "conversation.item.create"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_voice_session_vercel_long_turn_keeps_tool_conversation_order(monkeypatch):
    from packages.core.ai.runtime import runtime_llm_billing_context
    from packages.core.services.voice import session as voice_session
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )

    class RealtimeConnection:
        def __init__(self):
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

    release_agent = asyncio.Event()

    async def agent_callable(**_kwargs):
        await release_agent.wait()
        return VoiceAgentOutcome(status="ok", spoken_reply="final answer")

    async def allow_credit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(voice_session, "_HOLD_AFTER_SECONDS", 0.01)
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        allow_credit,
    )
    realtime = RealtimeConnection()
    session = voice_session.TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = realtime
    async with runtime_llm_billing_context("entity-1", source="twilio_voice"):
        turn = asyncio.create_task(
            session._run_agent_turn(
                BridgeCall(
                    call_id="call-1",
                    utterance="slow request",
                    response_id="response-1",
                )
            )
        )

        await asyncio.sleep(0.03)
        assert realtime.sent == []

        release_agent.set()
        await turn

    assert [event["type"] for event in realtime.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    assert "final answer" in realtime.sent[-1]["response"]["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_result", "expected_status", "expected_reply"),
    [
        (
            {
                "status": "ok",
                "reply": "persisted Manor answer",
                "conversation_id": "conversation-1",
                "agent_id": "agent-1",
            },
            "ok",
            "persisted Manor answer",
        ),
        (
            {
                "status": "approval_required",
                "reply": "held secret that must never be spoken",
                "conversation_id": "conversation-1",
                "agent_id": "agent-1",
            },
            "approval_required",
            "This action requires approval in Manor before it can continue.",
        ),
        (
            {
                "status": "delivery_resolved",
                "ack_message": "Approved and queued.",
                "conversation_id": "conversation-1",
                "agent_id": "agent-1",
            },
            "action_handled",
            "Approved and queued.",
        ),
        (
            {
                "status": "no_reply",
                "conversation_id": "conversation-1",
                "agent_id": "agent-1",
            },
            "no_reply",
            "",
        ),
        (
            {"status": "unbound", "reason": "binding_scope_changed"},
            "error",
            "Sorry, I cannot complete that request right now.",
        ),
    ],
)
async def test_voice_agent_call_normalizes_gateway_outcome(
    monkeypatch,
    gateway_result,
    expected_status,
    expected_reply,
):
    from apps.api.routers.channels import voice_stream

    observed: dict = {}

    async def dispatch_inbound(**kwargs):
        observed.update(kwargs)
        return gateway_result

    monkeypatch.setattr(
        "packages.core.services.channel_gateway.dispatch_inbound",
        dispatch_inbound,
    )

    outcome = await voice_stream._voice_agent_call(
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_binding_id="binding-1",
        agent_subscription_id="subscription-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        call_sid="",
        from_number="+14155550199",
        text="hello",
    )

    assert outcome.status == expected_status
    assert outcome.spoken_reply == expected_reply
    assert "held secret" not in outcome.spoken_reply
    assert outcome.conversation_id == gateway_result.get("conversation_id")
    assert outcome.agent_id == gateway_result.get("agent_id")
    assert observed["deliver_reply"] is False
    assert observed["channel_binding_id"] == "binding-1"
    assert observed["channel_agent_subscription_id"] == "subscription-1"
    assert observed["channel_agent_id"] == "agent-1"
    assert observed["channel_workspace_id"] == "workspace-1"


@pytest.mark.asyncio
async def test_deferred_channel_action_ack_is_returned_without_text_delivery(
    monkeypatch,
):
    from packages.core.services import channel_gateway
    from packages.core.services.channel_inbound_actions import ChannelInboundAction

    async def unexpected_delivery(**_kwargs):
        raise AssertionError("deferred Voice ack must not use text delivery")

    monkeypatch.setattr(
        channel_gateway,
        "send_channel_text_reply",
        unexpected_delivery,
    )
    action = ChannelInboundAction(
        result={"status": "delivery_resolved"},
        ack_message="Approved and queued.",
    )

    ack = await channel_gateway._send_inbound_action_ack(
        action,
        channel_config_id="config-1",
        channel_binding_id="binding-1",
        channel_type="twilio_voice",
        chat_id="+14155550199",
        thread_ts=None,
        reply_context=None,
        log_label="pending-delivery",
        deliver_reply=False,
    )

    assert ack == "Approved and queued."


@pytest.mark.asyncio
async def test_voice_session_strictly_settles_every_realtime_response(monkeypatch):
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    recorded: list[dict] = []

    class DatabaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            recorded.append({"committed": True})

    async def record_llm_usage(_db, **kwargs):
        recorded.append(kwargs)

    class RealtimeConnection:
        async def send(self, _event):
            return None

    class TwilioSocket:
        async def send_text(self, _payload):
            return None

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: DatabaseSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.usage_service.record_llm_usage",
        record_llm_usage,
    )
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
        billing_user_id="user-1",
        billing_workspace_id="workspace-1",
        billing_agent_id="agent-1",
    )
    session._realtime = RealtimeConnection()
    session._conversation_id = "conversation-1"
    event = _response_done(
        event_id="usage-event-1",
        response_id="provider-response-1",
        usage={
            "input_tokens": 120,
            "output_tokens": 45,
            "total_tokens": 165,
            "input_token_details": {
                "audio_tokens": 100,
                "cached_tokens": 30,
                "cached_tokens_details": {"audio_tokens": 25},
            },
            "output_token_details": {"audio_tokens": 40},
        },
    )
    event["response"]["output"] = []

    await session._handle_realtime_event(event)
    await session._handle_realtime_event(event)

    assert recorded[0]["entity_id"] == "entity-1"
    assert recorded[0]["user_id"] == "user-1"
    assert recorded[0]["workspace_id"] == "workspace-1"
    assert recorded[0]["agent_id"] == "agent-1"
    assert recorded[0]["conversation_id"] == "conversation-1"
    assert recorded[0]["source"] == "twilio_voice"
    assert recorded[0]["operation_id"] == (
        "twilio-realtime:provider-response-1"
    )
    assert recorded[0]["strict"] is True
    assert recorded[0]["usage"]["audio_input_tokens"] == 100
    assert recorded[0]["usage"]["audio_output_tokens"] == 40
    assert recorded[0]["usage"]["cache_read_input_tokens"] == 30
    assert recorded[0]["usage"]["cached_audio_input_tokens"] == 25
    assert recorded[1] == {"committed": True}
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_voice_session_strictly_settles_input_transcription(monkeypatch):
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    recorded: list[dict] = []

    class DatabaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            recorded.append({"committed": True})

    async def record_llm_usage(_db, **kwargs):
        recorded.append(kwargs)

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: DatabaseSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.usage_service.record_llm_usage",
        record_llm_usage,
    )
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
        billing_user_id="user-1",
        billing_workspace_id="workspace-1",
        billing_agent_id="agent-1",
    )
    event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "event_id": "transcription-event-1",
        "item_id": "input-item-1",
        "content_index": 0,
        "transcript": "Hello",
        "usage": {
            "type": "tokens",
            "input_tokens": 8,
            "output_tokens": 5,
            "total_tokens": 13,
            "input_token_details": {"audio_tokens": 8},
        },
    }

    await session._handle_realtime_event(event)

    assert recorded[0]["entity_id"] == "entity-1"
    assert recorded[0]["user_id"] == "user-1"
    assert recorded[0]["workspace_id"] == "workspace-1"
    assert recorded[0]["agent_id"] == "agent-1"
    assert recorded[0]["conversation_id"] is None
    assert recorded[0]["source"] == "twilio_voice"
    assert recorded[0]["operation_id"] == ("twilio-transcription:transcription-event-1")
    assert recorded[0]["strict"] is True
    assert recorded[0]["usage"]["model"] == ("openai/gpt-4o-mini-transcribe")
    assert recorded[0]["usage"]["audio_input_tokens"] == 8
    assert recorded[1] == {"committed": True}


@pytest.mark.asyncio
async def test_voice_session_strict_settlement_failure_marks_session_failed(
    monkeypatch,
):
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class DatabaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fail_recording(_db, **_kwargs):
        raise RuntimeError("strict Realtime settlement failed")

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: DatabaseSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.usage_service.record_llm_usage",
        fail_recording,
    )
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    event = _response_done(
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )
    event["response"]["output"] = []

    with pytest.raises(RuntimeError, match="strict Realtime settlement failed"):
        await session._handle_realtime_event(event)

    assert session.error_message == "strict Realtime settlement failed"


@pytest.mark.asyncio
async def test_voice_session_retries_response_after_settlement_failure(monkeypatch):
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    attempts = 0
    commits = 0

    class DatabaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            nonlocal commits
            commits += 1

    async def record_llm_usage(_db, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary settlement failure")

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: DatabaseSession(),
    )
    monkeypatch.setattr(
        "packages.core.services.usage_service.record_llm_usage",
        record_llm_usage,
    )
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    event = _response_done(
        response_id="retry-response-1",
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    event["response"]["output"] = []

    with pytest.raises(RuntimeError, match="temporary settlement failure"):
        await session._handle_realtime_event(event)
    await session._handle_realtime_event(event)

    assert attempts == 2
    assert commits == 1
    assert session._settled_response_ids == {"retry-response-1"}


@pytest.mark.asyncio
async def test_voice_session_checks_credit_before_each_platform_response(monkeypatch):
    from packages.core.ai.runtime import runtime_llm_billing_context
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )
    from packages.core.services.voice.session import TwilioVoiceSession

    events: list[tuple[str, str]] = []

    async def allow_credit(entity_id, *, source):
        assert entity_id == "entity-1"
        assert source == "twilio_voice"
        events.append(("credit", source))

    class RealtimeConnection:
        async def send(self, event):
            events.append(("send", event["type"]))

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        allow_credit,
    )
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = RealtimeConnection()

    async with runtime_llm_billing_context("entity-1", source="twilio_voice"):
        await session._send_voice_outcome(
            BridgeCall(
                call_id="call-1",
                utterance="hello",
                response_id="response-1",
            ),
            VoiceAgentOutcome(status="ok", spoken_reply="answer"),
        )

    assert events == [
        ("send", "conversation.item.create"),
        ("credit", "twilio_voice"),
        ("send", "response.create"),
    ]


@pytest.mark.asyncio
async def test_voice_session_checks_credit_before_server_vad_response(monkeypatch):
    from packages.core.ai.runtime import runtime_llm_billing_context
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    events: list[tuple[str, str]] = []

    async def allow_credit(entity_id, *, source):
        assert entity_id == "entity-1"
        assert source == "twilio_voice"
        events.append(("credit", source))

    class RealtimeConnection:
        async def send(self, event):
            events.append(("send", event["type"]))

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        allow_credit,
    )
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    session._realtime = RealtimeConnection()

    async with runtime_llm_billing_context("entity-1", source="twilio_voice"):
        await session._handle_realtime_event(
            {
                "type": "input_audio_buffer.speech_stopped",
                "event_id": "speech-stopped-1",
            }
        )

    assert events == [
        ("credit", "twilio_voice"),
        ("send", "response.create"),
    ]


@pytest.mark.asyncio
async def test_voice_session_agent_worker_failure_terminates_call():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    class TwilioSocket:
        async def receive_text(self):
            await asyncio.Future()

    class RealtimeConnection:
        def __init__(self):
            self.ready = False

        async def send(self, _event):
            return None

        async def recv(self):
            if not self.ready:
                self.ready = True
                return {"type": "session.updated"}
            await asyncio.Future()

    class Manager:
        async def __aenter__(self):
            return RealtimeConnection()

        async def __aexit__(self, *_args):
            return False

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    async def fail_worker():
        raise RuntimeError("voice Agent worker failed")

    session._agent_worker = fail_worker

    await asyncio.wait_for(session.run(), timeout=1)

    assert session.error_message == "voice Agent worker failed"


@pytest.mark.asyncio
async def test_voice_session_cancellation_keeps_inflight_manor_turn_running():
    from packages.core.services.voice.realtime import (
        BridgeCall,
        RealtimeRoute,
        VoiceAgentOutcome,
    )
    from packages.core.services.voice.session import TwilioVoiceSession

    agent_entered = asyncio.Event()
    release_agent = asyncio.Event()
    agent_finished = asyncio.Event()
    agent_cancelled = asyncio.Event()

    async def agent_callable(**_kwargs):
        agent_entered.set()
        try:
            await release_agent.wait()
        except asyncio.CancelledError:
            agent_cancelled.set()
            raise
        agent_finished.set()
        return VoiceAgentOutcome(status="ok", spoken_reply="finished")

    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )
    turn = asyncio.create_task(
        session._run_agent_turn(
            BridgeCall(
                call_id="call-1",
                utterance="continue after disconnect",
                response_id="response-1",
            )
        )
    )
    await asyncio.wait_for(agent_entered.wait(), timeout=1)

    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)
    release_agent.set()
    await asyncio.wait_for(agent_finished.wait(), timeout=1)

    assert not agent_cancelled.is_set()


@pytest.mark.asyncio
async def test_voice_session_stop_detaches_inflight_manor_turn_without_reply():
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    agent_entered = asyncio.Event()
    release_agent = asyncio.Event()
    agent_finished = asyncio.Event()
    agent_cancelled = asyncio.Event()

    class TwilioSocket:
        def __init__(self):
            self.receive_count = 0

        async def receive_text(self):
            self.receive_count += 1
            if self.receive_count == 1:
                return json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "stream-1",
                            "callSid": "CA-1",
                            "customParameters": {
                                "from": "+14155550199",
                                "to": "+14155550100",
                            },
                        },
                    }
                )
            await agent_entered.wait()
            return json.dumps({"event": "stop"})

    class RealtimeConnection:
        def __init__(self):
            self.receive_count = 0
            self.sent: list[dict] = []

        async def send(self, event):
            self.sent.append(event)

        async def recv(self):
            self.receive_count += 1
            if self.receive_count == 1:
                return {"type": "session.updated"}
            if self.receive_count == 2:
                return _response_done(arguments='{"utterance":"finish this turn"}')
            await asyncio.Future()

    realtime = RealtimeConnection()

    class Manager:
        async def __aenter__(self):
            return realtime

        async def __aexit__(self, *_args):
            return False

    async def agent_callable(**_kwargs):
        agent_entered.set()
        try:
            await release_agent.wait()
        except asyncio.CancelledError:
            agent_cancelled.set()
            raise
        agent_finished.set()
        return VoiceAgentOutcome(status="ok", spoken_reply="finished")

    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await asyncio.wait_for(session.run(), timeout=1)

    sent_before_release = list(realtime.sent)
    assert len(session._detached_agent_tasks) == 1
    release_agent.set()
    await asyncio.wait_for(agent_finished.wait(), timeout=1)
    await asyncio.sleep(0)

    assert not agent_cancelled.is_set()
    assert session._detached_agent_tasks == set()
    assert realtime.sent == sent_before_release


def test_gpt_realtime_pricing_handles_cached_audio_without_double_charge():
    from packages.core.services.model_pricing import (
        OFFICIAL_TOKEN_PRICES,
        estimate_token_cost_usd,
    )

    price = OFFICIAL_TOKEN_PRICES["openai/gpt-realtime"]
    assert price.input_per_m == 4.0
    assert price.output_per_m == 16.0
    assert price.cache_read_multiplier == 0.1
    assert price.audio_input_per_m == 32.0
    assert price.audio_output_per_m == 64.0

    cost = estimate_token_cost_usd(
        120,
        45,
        "openai/gpt-realtime",
        cache_read_tokens=30,
        audio_input_tokens=100,
        audio_output_tokens=40,
        cached_audio_input_tokens=25,
    )

    assert cost == pytest.approx(0.005112)


@pytest.mark.asyncio
async def test_voice_session_reserves_before_platform_provider_and_settles(
    monkeypatch,
):
    from packages.core.services.voice import billing as voice_billing
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    events: list[tuple] = []

    async def reserve(**kwargs):
        events.append(("reserve", kwargs["source_id"]))

    async def settle(**kwargs):
        events.append(
            ("settle", kwargs["source_id"], kwargs["provider_started"])
        )

    class Connection:
        def __init__(self):
            self.ready = False

        async def send(self, event):
            events.append(("provider_send", event["type"]))

        async def recv(self):
            if not self.ready:
                self.ready = True
                return {"type": "session.updated"}
            await asyncio.Future()

    class Manager:
        async def __aenter__(self):
            events.append(("provider_enter",))
            return Connection()

        async def __aexit__(self, *_args):
            events.append(("provider_exit",))

    class TwilioSocket:
        async def receive_text(self):
            return json.dumps({"event": "stop"})

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(voice_billing, "reserve_voice_call_credits", reserve)
    monkeypatch.setattr(voice_billing, "settle_voice_call_credits", settle)
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await session.run()

    assert session.error_message is None
    assert events[0] == ("reserve", "call-session-1")
    assert events[1] == ("provider_enter",)
    assert ("provider_send", "session.update") in events
    assert events[-1] == ("settle", "call-session-1", True)


@pytest.mark.asyncio
async def test_vercel_voice_session_starts_with_realtime_transcription(monkeypatch):
    from packages.core.services.voice import billing as voice_billing
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    sent: list[dict] = []

    async def reserve(**_kwargs):
        return None

    async def settle(**_kwargs):
        return None

    class Connection:
        def __init__(self):
            self.ready = False

        async def send(self, event):
            sent.append(event)

        async def recv(self):
            if not self.ready:
                self.ready = True
                return {"type": "session.updated"}
            await asyncio.Future()

    class Manager:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class TwilioSocket:
        async def receive_text(self):
            return json.dumps({"event": "stop"})

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(voice_billing, "reserve_voice_call_credits", reserve)
    monkeypatch.setattr(voice_billing, "settle_voice_call_credits", settle)
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://ai-gateway.vercel.sh/v1",
            model="openai/gpt-realtime-mini",
            byok=False,
            provider="vercel",
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await session.run()

    assert session.error_message is None
    assert sent[0]["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-mini-transcribe"
    }
    assert "tools" not in sent[0]["session"]
    assert "tool_choice" not in sent[0]["session"]


@pytest.mark.asyncio
async def test_voice_session_releases_reservation_when_provider_never_starts(
    monkeypatch,
):
    from packages.core.services.voice import billing as voice_billing
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    settlements: list[bool] = []

    async def reserve(**_kwargs):
        return None

    async def settle(**kwargs):
        settlements.append(kwargs["provider_started"])

    class Manager:
        async def __aenter__(self):
            raise RuntimeError("provider unavailable")

        async def __aexit__(self, *_args):
            return None

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(voice_billing, "reserve_voice_call_credits", reserve)
    monkeypatch.setattr(voice_billing, "settle_voice_call_credits", settle)
    session = TwilioVoiceSession(
        SimpleNamespace(),
        realtime_route=RealtimeRoute(
            api_key="managed-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=False,
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await session.run()

    assert session.error_message == "provider unavailable"
    assert settlements == [False]


@pytest.mark.asyncio
async def test_voice_session_byok_skips_platform_reservation(monkeypatch):
    from packages.core.services.voice import billing as voice_billing
    from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
    from packages.core.services.voice.session import TwilioVoiceSession

    async def unexpected(**_kwargs):
        raise AssertionError("BYOK must not use platform Voice reservation")

    class Connection:
        def __init__(self):
            self.ready = False

        async def send(self, _event):
            return None

        async def recv(self):
            if not self.ready:
                self.ready = True
                return {"type": "session.updated"}
            await asyncio.Future()

    class Manager:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class TwilioSocket:
        async def receive_text(self):
            return json.dumps({"event": "stop"})

    async def agent_callable(**_kwargs):
        return VoiceAgentOutcome(status="no_reply", spoken_reply="")

    monkeypatch.setattr(voice_billing, "reserve_voice_call_credits", unexpected)
    monkeypatch.setattr(voice_billing, "settle_voice_call_credits", unexpected)
    session = TwilioVoiceSession(
        TwilioSocket(),
        realtime_route=RealtimeRoute(
            api_key="user-key",
            base_url="https://api.openai.com/v1",
            model="gpt-realtime",
            byok=True,
        ),
        realtime_connection_factory=lambda _route: Manager(),
        agent_callable=agent_callable,
        call_session_id="call-session-1",
        channel_config_id="config-1",
        entity_id="entity-1",
    )

    await session.run()

    assert session.error_message is None
