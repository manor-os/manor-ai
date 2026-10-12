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
previews = 0
reply = "完整分析：报告已经生成，共 42 行；APR 为 6.25%，最终以核保为准。"
presentation = "我查完了：APR 是 6.25%，最终以核保为准；完整报告已经放在聊天里。"
messages = [{
    "id": "welcome", "conversation_id": "fixture-conversation", "body": "Workspace ready.",
    "message_kind": "text", "author_kind": "agent", "created_at": "2026-08-31T12:00:00Z",
    "refs": [], "meta": {},
}]


async def noop():
    pass


async def transcribe(audio):
    assert audio.startswith(b"RIFF")
    return "你好" if inputs == 0 else "继续"


async def agent(text):
    messages.append({**messages[0], "id": "user", "author_kind": "user", "body": text})
    messages.append({**messages[0], "id": "reply", "body": reply})
    return VoiceAgentOutcome(status="ok", spoken_reply=reply, conversation_id="fixture-conversation")


async def present_result(_text, outcome):
    assert outcome.spoken_reply == reply
    return presentation


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
        global inputs, previews
        if event["type"] == "transcript" and event.get("role") == "user":
            inputs += 1
        elif event["type"] == "transcript_preview":
            previews += 1
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
        prepare=noop, transcribe=transcribe,
        agent=agent, render_result=present_result, speak=speak,
    ).run()


@app.get("/api/v1/voice-test-observations")
async def observations():
    return {
        "events": events,
        "received": received,
        "inputs": inputs,
        "previews": previews,
    }


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
