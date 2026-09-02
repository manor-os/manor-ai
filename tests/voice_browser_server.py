"""Loopback-only E2E fixture: real voice transport, synthetic providers/data.

Run with uvicorn on 127.0.0.1 and E2E_API pointing to it. This never authenticates
as a user, accesses application records, records a mic, or calls paid providers.
"""

import json
from pathlib import Path

from fastapi import FastAPI, WebSocket

from packages.core.services.voice.gateway_call import GatewayVoiceSession
from packages.core.services.voice.realtime import VoiceAgentOutcome

app = FastAPI()
events = []
received = []
inputs = 0
reply = "你好，我能听到你的声音，现在测试中文语音回复。"
messages = [{
    "id": "welcome", "conversation_id": "fixture-conversation", "body": "Workspace ready.",
    "message_kind": "text", "author_kind": "agent", "created_at": "2026-08-31T12:00:00Z",
    "refs": [], "meta": {},
}]


async def noop():
    pass


async def transcribe(audio):
    global inputs
    assert audio.startswith(b"RIFF")
    inputs += 1
    return "你好" if inputs == 1 else "继续"


async def agent(text):
    messages.append({**messages[0], "id": "user", "author_kind": "user", "body": text})
    messages.append({**messages[0], "id": "reply", "body": reply})
    return VoiceAgentOutcome(status="ok", spoken_reply=reply, conversation_id="fixture-conversation")


async def speak(_, _voice):
    return (Path(__file__).parent / "fixtures/voice/zh-reply.mp3").read_bytes()


class Socket:
    def __init__(self, ws):
        self.ws = ws

    async def receive_text(self):
        raw = await self.ws.receive_text()
        received.append(json.loads(raw)["type"])
        return raw

    async def send_json(self, event):
        events.append(event["type"])
        await self.ws.send_json(event)

    async def close(self):
        await self.ws.close()


@app.websocket("/api/v1/audio/live")
async def live(ws: WebSocket):
    await ws.accept()
    start = await ws.receive_json()
    assert start["workspace_id"] == "proposal-approval-fixture"
    assert start["conversation_id"] == "fixture-conversation"
    await GatewayVoiceSession(
        Socket(ws), conversation_id="fixture-conversation", check_access=noop,
        prepare=noop, transcribe=transcribe, agent=agent, speak=speak,
    ).run()


@app.get("/api/v1/voice-test-observations")
async def observations():
    return {"events": events, "received": received, "inputs": inputs}


@app.get("/api/v1/{path:path}")
async def fixture_data(path: str):
    if path.endswith("/chat/messages/page"):
        return {"items": messages, "has_more": False, "open_actions_complete": True, "open_action_count": 0}
    if path.endswith("/stats/quick-view"):
        return {"ordered_stat_ids": [], "hidden_stat_ids": [], "configured": False}
    if path.endswith("/connection-status"):
        return {"requirements": [], "required_issue_count": 0}
    if path.endswith(("/tasks", "/goals", "/stats")) or path in {"tasks", "goals"}:
        return {"items": [], "total": 0}
    return []
