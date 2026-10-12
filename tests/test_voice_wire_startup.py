"""Real loopback WebSocket limits, without providers, audio devices, or a DB."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError

from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.services.voice.browser import BrowserVoiceSession, browser_session_update
from packages.core.services.voice.live_agent import live_voice_session_factory
from packages.core.services.voice.realtime import (
    RealtimeRoute,
    _VercelRealtimeConnection,
    _vercel_session_update_event,
    live_voice_instruction_token_budget,
)


# Keep the server's independent transport contract literal: increasing the
# production constant must not silently relax this integration boundary.
WIRE_LIMIT = 256_000


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _BrowserSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.events = []

    async def receive_text(self):
        return _json(await self.incoming.get())

    async def send_json(self, event):
        self.events.append(event)
        await self.outgoing.put(event)


async def _eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_loopback_receive_limit_accepts_exact_bytes_and_closes_oversize_with_1009():
    received = []

    async def handler(socket):
        try:
            async for payload in socket:
                received.append(len(payload.encode("utf-8")))
                await socket.send("accepted")
        except ConnectionClosedError:
            pass  # The server intentionally rejects the second message.

    async with websockets.serve(
        handler, "127.0.0.1", 0, max_size=WIRE_LIMIT, compression=None, close_timeout=1
    ) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}", compression=None, close_timeout=1) as socket:
            overhead = len(_json({"instructions": ""}).encode("utf-8"))
            exact = _json({"instructions": "x" * (WIRE_LIMIT - overhead)})
            assert len(exact.encode("utf-8")) == WIRE_LIMIT
            await socket.send(exact)
            assert await asyncio.wait_for(socket.recv(), 5) == "accepted"

            await socket.send(exact + " ")
            with pytest.raises(ConnectionClosedError) as closed:
                await asyncio.wait_for(socket.recv(), 5)
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1009

    assert received == [WIRE_LIMIT]


@pytest.mark.parametrize(
    "body",
    ["ordinary " * 1_000, '"\\' * 2_000],
    ids=["ascii", "json-escaping"],
)
async def test_long_history_stays_native_through_real_wire_startup_and_tool_switches(body):
    agent_instructions = "Fixture Agent identity: preserve authorized Workspace boundaries."
    summary = {"role": "system", "content": "Conversation summary: independent studio, not a shared room."}
    history = (
        [summary]
        + [{"role": "user", "content": f"old-{index}: {body}"} for index in range(30)]
        + [{"role": "user", "content": "最新要求：湾区独立 studio，月租严格低于 $1000。"}]
    )
    original_history = _json(history)
    model = "openai/gpt-realtime-2.1"
    full = live_voice_session_factory.instructions(agent_instructions, history)
    complete_event = browser_session_update(model, instructions=full, provider="vercel")
    complete_wire = _json(_vercel_session_update_event(complete_event["session"]))
    assert runtime_estimate_tokens_for_text(full) < live_voice_instruction_token_budget(model)
    assert len(complete_wire.encode("utf-8")) > WIRE_LIMIT

    received = []
    sizes = []
    adapters = []
    provider_sockets = []

    async def handler(socket):
        response_number = 0
        async for payload in socket:
            sizes.append(len(payload.encode("utf-8")))
            event = json.loads(payload)
            received.append(event)
            if event["type"] == "session-update":
                controls = event["config"]["providerOptions"]
                # Return provider-shaped controls, so the real adapter verifies
                # VAD suppression, tool choice, and transcription acceptance.
                await socket.send(
                    _json(
                        {
                            "type": "session-updated",
                            "raw": {"type": "session.updated", "session": controls},
                        }
                    )
                )
            elif event["type"] == "response-create":
                response_number += 1
                response = {"id": f"response-{response_number}", "status": "completed", "output": []}
                await socket.send(
                    _json({"type": "provider-event", "raw": {"type": "response.created", "response": response}})
                )
                # A completed audio response must actually deliver audio. The
                # browser-facing socket is in-memory; no audio device is used.
                await socket.send(
                    _json(
                        {
                            "type": "audio-delta",
                            "responseId": response["id"],
                            "itemId": f"audio-{response_number}",
                            "delta": base64.b64encode(b"\0" * 480).decode("ascii"),
                        }
                    )
                )
                await socket.send(
                    _json({"type": "provider-event", "raw": {"type": "response.done", "response": response}})
                )

    async with websockets.serve(
        handler, "127.0.0.1", 0, max_size=WIRE_LIMIT, compression=None, close_timeout=1
    ) as server:
        port = server.sockets[0].getsockname()[1]

        class LoopbackVercelConnection(_VercelRealtimeConnection):
            async def __aenter__(self):
                # Replace connection establishment only; send, recv, cached
                # session mapping, controls verification, and exit remain real.
                self._socket_manager = websockets.connect(f"ws://127.0.0.1:{port}", compression=None, close_timeout=1)
                self._socket = await self._socket_manager.__aenter__()
                provider_sockets.append(self._socket)
                adapters.append(self)
                return self

        browser = _BrowserSocket()
        check_access = AsyncMock()
        main_agent = AsyncMock()
        live_context = AsyncMock(return_value=(agent_instructions, history))
        call = BrowserVoiceSession(
            browser,
            route=RealtimeRoute(
                "unused-loopback-key", "https://ai-gateway.vercel.sh/v1", model, True, provider="vercel"
            ),
            entity_id="entity",
            user_id="user",
            conversation_id="conversation",
            workspace_id="workspace",
            agent_id="agent",
            check_access=check_access,
            agent=main_agent,
            live_context=live_context,
            connection_factory=LoopbackVercelConnection,
        )
        task = asyncio.create_task(call.run())
        try:
            ready = await asyncio.wait_for(browser.outgoing.get(), 5)
            assert ready["type"] == "ready"
            assert ready["transport_mode"] == "native_realtime"
            assert adapters[0]._verified is True
            assert call.ready is True

            # Drive the actual Browser response queue, including restoration of
            # foreground tools after a result's wait-only presentation tools.
            for index, event in enumerate(
                (
                    live_voice_session_factory.response_event(),
                    live_voice_session_factory.result_response_event(),
                    live_voice_session_factory.response_event(),
                ),
                start=1,
            ):
                await call.requests.put((call.generation, event))
                await _eventually(lambda: f"response-{index}" in call.engine.completed_response_ids)
                assert call.response_idle.is_set()
        finally:
            await browser.incoming.put({"type": "end"})
            await asyncio.wait_for(task, 5)

    assert provider_sockets[0].close_code == 1000
    assert call.engine.connection is None
    assert [event["type"] for event in received] == [
        "session-update",
        "session-update",
        "response-create",
        "session-update",
        "response-create",
        "session-update",
        "response-create",
    ]
    assert all(size <= WIRE_LIMIT for size in sizes)
    configs = [event["config"] for event in received if event["type"] == "session-update"]
    bridge_tools = {tool["name"] for tool in live_voice_session_factory.tools()}
    assert [{tool["name"] for tool in config["tools"]} for config in configs] == [
        bridge_tools,
        bridge_tools,
        {"wait_for_user"},
        bridge_tools,
    ]
    for config in configs:
        prompt = config["instructions"]
        assert prompt == call.live_instructions
        assert agent_instructions in prompt
        assert summary["content"] in prompt
        assert history[-1]["content"] in prompt
        assert "old-0:" not in prompt
        assert runtime_estimate_tokens_for_text(prompt) <= call.live_instruction_token_budget
    assert _json(history) == original_history
    assert [event["type"] for event in browser.events].count("ready") == 1
    assert not any(event["type"] == "error" for event in browser.events)
    live_context.assert_awaited_once_with("")
    main_agent.assert_not_awaited()
    assert check_access.await_count == 4
