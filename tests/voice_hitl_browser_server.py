"""Loopback Chromium fixture: real Voice transport and DB approval runtime.

This covers the Main approval resolver/Runtime executor, NOT the Main HTTP
endpoint. ASR, Live decisions, TTS, account discovery and external provider I/O
are scripted. Only the explicitly named disposable database is admitted.
Run with one uvicorn worker on 127.0.0.1:3300; browser cases must be serial.

After creating the dedicated local database (existing databases are preserved):
    TEST_DATABASE_URL=postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_voice_hitl_browser_20260909_test \\
      PYTHONPATH=. .venv/bin/python -m uvicorn tests.voice_hitl_browser_server:app --host 127.0.0.1 --port 3300
In another terminal:
    VOICE_HITL_E2E=1 E2E_API=http://127.0.0.1:3300 E2E_PORT=5190 \\
      npm --prefix apps/web run test:e2e -- voice-hitl-live.spec.ts --workers=1

No real microphone, account authentication, deployment, or external I/O occurs.
Each reset creates a new test scope; prior scenarios remain in the isolated DB.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import io
import json
import math
import os
import struct
import wave

from sqlalchemy.engine import make_url

_DATABASE = "manor_voice_hitl_browser_20260909_test"
_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
_url = make_url(_DATABASE_URL) if _DATABASE_URL else None
if (
    _url is None or _url.database != _DATABASE
    or _url.host not in {"localhost", "127.0.0.1"} or _url.port != 5434
    or _url.drivername != "postgresql+asyncpg"
):
    raise RuntimeError(f"This fixture requires the isolated loopback {_DATABASE} database")
os.environ["DATABASE_URL"] = _DATABASE_URL
os.environ["DATABASE_URL_SYNC"] = _DATABASE_URL.replace("+asyncpg", "")

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from apps.api.routers.workspace_chat import _to_message  # noqa: E402
import packages.core.models  # noqa: E402,F401
from packages.core.models.base import Base  # noqa: E402
from packages.core.models.hitl_request import HitlRequest  # noqa: E402
from packages.core.models.task import Message  # noqa: E402
from packages.core.services.mcp_seed import seed_mcp_catalog  # noqa: E402
from packages.core.services.voice.approvals import (  # noqa: E402
    VoiceApprovalReplyError, load_voice_pending_actions, prepare_voice_approval_reply,
)
from packages.core.services.voice.browser import BrowserVoiceSession  # noqa: E402
from packages.core.services.voice.gateway_call import GatewayVoiceSession  # noqa: E402
from packages.core.services.voice.live_agent import (  # noqa: E402
    LiveVoiceTextDecision, LiveVoiceTurnAction,
)
from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome  # noqa: E402
from packages.core.services.voice.work_queue import (  # noqa: E402
    admit_voice_work, claim_voice_work, finish_voice_work,
)
from tests.test_voice_hitl_integration import (  # noqa: E402
    _effects, _execute, _pending, _resolve, _setup,
)


engine = create_async_engine(_DATABASE_URL)
session_factory = async_sessionmaker(engine, expire_on_commit=False)
lock = asyncio.Lock()
db = None
patches = None
ids = None
mode = "gateway"
scenario = "single"
active_connections = 0
utterances = deque()
observed = {}


@asynccontextmanager
async def lifespan(_app):
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)
    await seed_mcp_catalog(engine)
    yield
    if patches is not None:
        patches.undo()
    if db is not None:
        await db.close()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


async def noop():
    pass


def public_message(row):
    return _to_message(row).model_dump(mode="json")


async def current_actions():
    return await load_voice_pending_actions(
        db, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
    )


def presentation(outcome):
    if outcome.pending_actions:
        listing = "；".join(
            f"第{index}项：{action.title}，{action.description}"
            for index, action in enumerate(outcome.pending_actions, 1)
        )
        instruction = (
            "请在聊天界面完成。" if any(item.requires_ui for item in outcome.pending_actions)
            else "请明确选择批准或拒绝。"
        )
        return f"{outcome.spoken_reply} {listing}。{instruction}"
    return outcome.spoken_reply


async def admit(text_value, source_message_ids=(), **context):
    async with lock:
        observed["captured"].append({
            "text": text_value,
            "target_id": context.get("pending_action_id"),
            "presented_actions": [item.to_record() for item in context.get("presented_actions", ())],
        })
        return await admit_voice_work(
            db, conversation_id=ids["conversation_id"], text=text_value,
            user_id=ids["user_id"], public_channel=False,
            source_message_ids=tuple(source_message_ids), **context,
        )


async def execute(receipt):
    async with lock:
        await claim_voice_work(
            db, receipt, conversation_id=ids["conversation_id"],
            expected_author_user_id=ids["user_id"],
        )
        action = None
        error = None
        status = "ok"
        try:
            action = await prepare_voice_approval_reply(
                db, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
                user_id=ids["user_id"], text=receipt.text,
                target_id=receipt.pending_action_id, presented_actions=receipt.presented_actions,
            )
            if action:
                result = await _resolve(db, ids, action)
                forced = (result[3] or {}).get("forced_tool_calls", [])
                for call in forced:
                    executed = await _execute(db, ids, call)
                    assert executed.get("ok") is True, executed
                decision = json.loads(action)
                observed["decisions"].append({**decision, "executions": len(forced)})
                reply = "已批准，操作执行一次。" if forced else "已拒绝，未执行操作。"
            elif receipt.text == "请处理请求":
                reply = "这项操作需要你的确认，尚未执行。"
            else:
                reply = "这是普通询问，没有批准任何操作。"
        except VoiceApprovalReplyError as exc:
            error = exc.code
            status = "approval_required"
            observed["errors"].append({"code": exc.code, "text": receipt.text})
            reply = f"没有执行操作：{exc.message}"
        except Exception as exc:
            observed["server_errors"].append(f"{type(exc).__name__}: {exc}")
            await db.rollback()
            raise
        pending = await current_actions()
        if pending:
            status = "approval_required"
        db.add(Message(
            conversation_id=ids["conversation_id"], role="assistant", content=reply, author_kind="agent",
        ))
        await db.commit()
        await finish_voice_work(
            db, receipt, conversation_id=ids["conversation_id"],
            state="failed" if error else "completed", error=error,
        )
        outcome = VoiceAgentOutcome(
            status=status, spoken_reply=reply, conversation_id=ids["conversation_id"],
            pending_actions=pending,
        )
        observed["turns"].append({
            "input": receipt.text, "result": reply, "status": status, "error": error,
            "structured_action": json.loads(action) if action else None,
            "presented_actions": [item.to_payload(item_number=index) for index, item in enumerate(pending, 1)],
        })
        return outcome


async def unexpected_agent(_text):
    raise AssertionError("Voice work must execute its durable receipt")


async def record(user_text, assistant_text):
    async with lock:
        source = None
        if user_text:
            source = Message(
                conversation_id=ids["conversation_id"], role="user", content=user_text,
                meta={"voice_control": True, "author_user_id": ids["user_id"]},
            )
            db.add(source)
        if assistant_text:
            db.add(Message(
                conversation_id=ids["conversation_id"], role="assistant", content=assistant_text, author_kind="agent",
            ))
        await db.commit()
        return source.id if source is not None else None


async def transcribe(audio):
    assert audio.startswith(b"RIFF")
    observed["asr_requests"] += 1
    return utterances[0] if utterances else ""


async def foreground(_text, _receipt=None, **_kwargs):
    # The provider supplies no trusted text/target; actual captured ASR and
    # the transport-owned presentation snapshot cross the production helper.
    return LiveVoiceTextDecision(action=LiveVoiceTurnAction.DELEGATE)


async def render(_text, outcome):
    return presentation(outcome)


async def speak(_text, _voice):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(_audio_pcm())
    return output.getvalue()


def _audio_pcm():
    return b"".join(
        struct.pack("<h", int(3000 * math.sin(i * math.tau * 440 / 24000)))
        for i in range(12000)
    )


class Socket:
    def __init__(self, ws):
        self.ws = ws

    async def receive_text(self):
        raw = await self.ws.receive_text()
        event = json.loads(raw)
        if event["type"] == "audio":
            observed["audio_in"] += 1
        return raw

    async def send_json(self, event):
        observed["events"].append({key: value for key, value in event.items() if key != "audio"})
        if event["type"] in {"audio", "audio_clip"}:
            observed["audio_out"] += 1
        if event["type"] == "transcript" and event.get("role") == "user":
            value = event.get("text", "")
            observed["inputs"].append(value)
            if utterances and value == utterances[0]:
                utterances.popleft()
        await self.ws.send_json(event)

    async def close(self):
        await self.ws.close()


class Provider:
    """Native provider protocol; a configured utterance still requires PCM."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sequence = 0
        self.response_sequence = 0
        self.current_text = ""
        self.result = None
        self.input_active = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def recv(self):
        return await self.incoming.get()

    async def emit(self, event):
        await self.incoming.put(event)

    async def send(self, event):
        kind = event["type"]
        if kind == "session.update":
            await self.emit({"type": "session.updated"})
        elif kind == "input_audio_buffer.append" and utterances and not self.input_active:
            if not any(base64.b64decode(event["audio"])):
                return
            self.input_active = True
            self.sequence += 1
            self.current_text = utterances[0]
            item_id = f"hitl-input-{self.sequence}"
            await self.emit({"type": "input_audio_buffer.committed", "item_id": item_id})
            await self.emit({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": item_id, "transcript": self.current_text})
        elif kind == "conversation.item.create" and event["item"].get("type") == "function_call_output":
            self.result = json.loads(event["item"]["output"])
        elif kind == "response.create":
            self.response_sequence += 1
            response_id = f"hitl-response-{self.response_sequence}"
            await self.emit({"type": "response.created", "response": {"id": response_id}})
            if self.result is None:
                output = [{"type": "function_call", "status": "completed",
                           "name": "delegate_to_main_agent", "call_id": f"hitl-call-{self.sequence}",
                           "arguments": json.dumps({"utterance": "Untrusted provider paraphrase"})}]
            else:
                result = self.result
                self.result = None
                text_value = result.get("response_text") or "未执行操作。"
                cards = result.get("pending_actions") or []
                if cards:
                    text_value += " " + "；".join(
                        f"第{card['item_number']}项：{card['title']}，{card['description']}" for card in cards
                    )
                    if any(card.get("requires_ui") for card in cards):
                        text_value += "。请在聊天界面完成。"
                item_id = f"hitl-result-{self.response_sequence}"
                await self.emit({"type": "response.output_audio_transcript.delta",
                                 "response_id": response_id, "item_id": item_id, "delta": text_value})
                await self.emit({"type": "response.output_audio.delta", "response_id": response_id,
                                 "item_id": item_id, "delta": base64.b64encode(_audio_pcm()).decode()})
                await self.emit({"type": "response.output_audio.done", "response_id": response_id,
                                 "item_id": item_id})
                output = [{"type": "message", "status": "completed",
                           "content": [{"type": "audio", "transcript": text_value}]}]
                self.input_active = False
            await self.emit({"type": "response.done", "event_id": response_id,
                             "response": {"id": response_id, "status": "completed", "output": output}})


@app.post("/api/v1/voice-hitl-test-reset")
async def reset(payload: dict):
    global db, patches, ids, mode, scenario
    if active_connections:
        raise HTTPException(409, "End the previous browser call before resetting")
    if payload.get("mode") not in {"gateway", "native"} or payload.get("scenario") not in {"single", "multi", "ui"}:
        raise HTTPException(400, "Invalid fixture mode/scenario")
    async with lock:
        if patches is not None:
            patches.undo()
        if db is not None:
            await db.close()
        db = session_factory()
        patches = pytest.MonkeyPatch()
        ids = await _setup(db, patches)
        mode, scenario = payload["mode"], payload["scenario"]
        message, _actions = await _pending(
            db, ids, *(('First post', 'Exact second post') if scenario == "multi" else ('First post',)),
        )
        message.author_kind = "agent"
        if scenario == "ui":
            cards = [{**card, "type": "login", "prompt": "Sign in required",
                      "content": "Use the original Chat controls."} for card in message.meta["hitl_requests"]]
            message.meta = {**message.meta, "hitl_requests": cards}
            message.pending_action = cards[0]
        await db.commit()
        utterances.clear()
        observed.clear()
        observed.update({"inputs": [], "turns": [], "captured": [], "decisions": [], "errors": [],
                         "server_errors": [], "events": [], "audio_in": 0, "audio_out": 0, "asr_requests": 0})
        return {"workspace_id": "proposal-approval-fixture", "conversation_id": ids["conversation_id"],
                "mode": mode, "scenario": scenario}


@app.post("/api/v1/voice-hitl-test-utterance")
async def utterance(payload: dict):
    value = payload.get("text")
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise HTTPException(400, "Provide a bounded scripted ASR transcript")
    if utterances:
        raise HTTPException(409, "The previous transcript still needs browser PCM")
    utterances.append(value)
    return {"queued": True}


@app.websocket("/api/v1/audio/live")
async def live(ws: WebSocket):
    global active_connections
    await ws.accept()
    start = await ws.receive_json()
    if ids is None or start.get("workspace_id") != "proposal-approval-fixture" or start.get("conversation_id") not in {ids["conversation_id"], "fixture-conversation", None}:
        await ws.close(code=1008)
        return
    active_connections += 1
    try:
        common = dict(conversation_id=ids["conversation_id"], check_access=noop,
                      agent=unexpected_agent, admit_work=admit, execute_work=execute, record_control_turn=record)
        if mode == "gateway":
            await GatewayVoiceSession(
                Socket(ws), **common, prepare=noop, transcribe=transcribe,
                speak=speak, foreground_agent=foreground, render_result=render,
            ).run()
        else:
            provider = Provider()
            await BrowserVoiceSession(
                Socket(ws), **common, entity_id=ids["entity_id"], user_id=ids["user_id"],
                workspace_id=ids["workspace_id"], agent_id="fixture-agent",
                route=RealtimeRoute("fixture-only", "http://127.0.0.1", "gpt-realtime", True),
                admit_input_work=admit, connection_factory=lambda _: provider,
            ).run()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        observed["server_errors"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        active_connections -= 1


@app.get("/api/v1/voice-hitl-test-observations")
async def observations():
    async with lock:
        if ids is None:
            return {"ready": True, "database": _DATABASE}
        requests = (await db.scalars(select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"])
                                    .order_by(HitlRequest.id))).all()
        messages = (await db.scalars(select(Message).where(Message.conversation_id == ids["conversation_id"])
                                    .order_by(Message.created_at, Message.id))).all()
        return {**observed, "mode": mode, "scenario": scenario, "active_connections": active_connections,
                "effects": [{"id": row.id, "text": row.content} for row in await _effects(db, ids)],
                "pending": [item.to_payload(item_number=index)
                            for index, item in enumerate(await current_actions(), 1)],
                "requests": [{"id": row.id, "status": row.status} for row in requests],
                "messages": [public_message(row) for row in messages if row.role != "tool"]}


@app.get("/api/v1/{path:path}")
async def fixture_data(path: str):
    if path.startswith("agent-initiatives/conversation/"):
        return {
            "items": [],
            "conversation": {
                "id": ids["conversation_id"] if ids else None,
                "title": "Voice to Main",
                "scope": "workspace_main",
            },
        }
    if path.endswith("/chat/messages/page"):
        data = await observations()
        return {"items": data.get("messages", []), "has_more": False, "open_actions_complete": True,
                "open_action_count": len(data.get("pending", [])),
                "conversation_id": ids["conversation_id"] if ids else None}
    if path.endswith("/stats/quick-view"):
        return {"ordered_stat_ids": [], "hidden_stat_ids": [], "configured": False}
    if path.endswith("/connection-status"):
        return {"requirements": [], "required_issue_count": 0}
    if path.endswith(("/tasks", "/goals", "/stats")) or path in {"tasks", "goals"}:
        return {"items": [], "total": 0}
    return []
