"""Loopback E2E: real BrowserVoiceSession with a scripted, unbilled provider.

Run tests.voice_native_browser_server:app on 127.0.0.1:3299. No application
authentication, database, real microphone, or external model is used.
"""

import asyncio
import base64
import json
import math
import struct

from fastapi import FastAPI, WebSocket

from packages.core.services.voice.browser import BrowserVoiceSession
from packages.core.services.voice.live_agent import MAIN_AGENT_CAPABILITIES
from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from tests import voice_browser_server as fixture

app = FastAPI()
source = "请查询波音工厂明天的开放时间，并规划午餐和下午的步道。"
preamble = "我来查一下。"
answer = "我查完了：上午参观工厂，中午在海边吃饭，下午走附近的步道。"
main_reply = "完整分析：上午参观工厂，中午在海边吃饭，下午走附近的步道；完整行程已保存在聊天中。"
observed = {"order": [], "events": [], "admitted": [], "source_message_ids": [],
            "executed": [], "results": [], "recorded": [], "truncates": []}
provider = None


def mark(stage):
    observed["order"].append(stage)


class Provider:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.input_started = False
        self.responses = 0
        self.delegate_done = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def recv(self):
        event = await self.incoming.get()
        mark("provider:" + event["type"])
        return event

    async def emit(self, event):
        await self.incoming.put(event)

    async def send(self, event):
        kind = event["type"]
        if kind == "session.update":
            instructions = event["session"]["instructions"]
            observed["shared_capabilities"] = MAIN_AGENT_CAPABILITIES in instructions
            observed["inherited_context"] = "Fixture travel assistant" in instructions and "Mukilteo" in instructions
            await self.emit({"type": "session.updated"})
        elif kind == "input_audio_buffer.append" and not self.input_started:
            pcm = base64.b64decode(event["audio"])
            if not any(pcm):
                return
            self.input_started = True
            await self.emit({"type": "input_audio_buffer.committed", "item_id": "native-source"})
            await self.emit({"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": "native-source", "delta": "请查询波音工厂"})
        elif kind == "conversation.item.create" and event["item"]["type"] == "function_call_output":
            observed["results"].append(json.loads(event["item"]["output"]))
            mark("main_result_to_live")
        elif kind == "conversation.item.truncate":
            observed["truncates"].append(event)
        elif kind == "response.create":
            self.responses += 1
            response_id = f"native-response-{self.responses}"
            is_result = self.responses == 2
            if is_result:
                assert len(observed["results"]) == 1
                assert "instructions" not in event["response"]
                assert [tool["name"] for tool in event["response"]["tools"]] == ["wait_for_user"]
                mark("live_result_presentation")
            else:
                assert self.responses == 1
            text = answer if is_result else preamble
            item_id = "native-result" if is_result else "native-preamble"
            await self.emit({"type": "response.created", "response": {"id": response_id}})
            if is_result:
                await self.emit({"type": "response.output_audio.delta", "response_id": "native-response-1",
                                 "item_id": "native-preamble", "delta": base64.b64encode(b"\0" * 480).decode()})
            await self.emit({"type": "response.output_audio_transcript.delta",
                             "response_id": response_id, "item_id": item_id, "delta": text})
            # Audible PCM traverses the actual native browser playback path.
            pcm = b"".join(struct.pack("<h", int(3000 * math.sin(i * math.tau * 440 / 24000)))
                           for i in range(12000 if is_result else 48000))
            await self.emit({"type": "response.output_audio.delta", "response_id": response_id,
                             "item_id": item_id, "delta": base64.b64encode(pcm).decode()})
            await self.emit({"type": "response.output_audio.done", "response_id": response_id,
                             "item_id": item_id})
            output = [{"type": "message", "status": "completed",
                       "content": [{"type": "audio", "transcript": text}]}]
            if not is_result:
                output.append({"type": "function_call", "status": "completed",
                               "name": "delegate_to_main_agent", "call_id": "native-delegate",
                               "arguments": json.dumps({"utterance": "Provider paraphrase must not execute"})})
            done = {"type": "response.done", "event_id": response_id,
                    "response": {"id": response_id, "status": "completed", "output": output}}
            if is_result:
                await self.emit(done)
            else:
                self.delegate_done = done


class Socket(fixture.Socket):
    async def send_json(self, event):
        observed["events"].append({key: value for key, value in event.items() if key != "audio"})
        await super().send_json(event)


async def admit(text, source_message_ids):
    mark("admit")
    observed["admitted"].append(text)
    observed["source_message_ids"].append(list(source_message_ids))
    assert text == source
    assert source_message_ids == ("native-user",)
    users = [message for message in fixture.messages if message["author_kind"] == "user"]
    assert [(message["id"], message["body"]) for message in users] == [("native-user", source)]
    return VoiceWorkReceipt("native-work", "native-user", text)


async def execute(receipt):
    mark("execute")
    observed["executed"].append(receipt.id)
    fixture.messages.append({**fixture.messages[0], "id": "native-main-result", "body": main_reply})
    return VoiceAgentOutcome(status="ok", spoken_reply=main_reply, conversation_id="fixture-conversation")


async def unexpected_agent(_text):
    raise AssertionError("Durable receipt execution must own this request")


async def record(text, reply):
    mark("record")
    observed["recorded"].append([text, reply])
    assert text == source and reply == ""
    assert not any(message["id"] == "native-user" for message in fixture.messages)
    fixture.messages.append({**fixture.messages[0], "id": "native-user", "author_kind": "user", "body": text})
    return "native-user"


async def context(_text):
    return "Fixture travel assistant", [{"role": "user", "content": "We are visiting Mukilteo."}]


@app.websocket("/api/v1/audio/live")
async def live(ws: WebSocket):
    global provider
    for value in observed.values():
        if isinstance(value, list):
            value.clear()
    fixture.messages[1:] = []
    await ws.accept()
    start = await ws.receive_json()
    assert start["workspace_id"] == "proposal-approval-fixture"
    assert start["conversation_id"] == "fixture-conversation"
    provider = Provider()
    # BYOK plus omitted synthetic usage takes the existing no-billing path;
    # the injected connection cannot reach an external provider.
    await BrowserVoiceSession(
        Socket(ws), route=RealtimeRoute("fixture-only", "http://127.0.0.1", "gpt-realtime", True),
        entity_id="fixture-entity", user_id="fixture-user", workspace_id=start["workspace_id"],
        conversation_id=start["conversation_id"], agent_id="fixture-agent",
        check_access=fixture.noop, agent=unexpected_agent,
        admit_work=unexpected_agent, admit_input_work=admit, execute_work=execute,
        record_control_turn=record, live_context=context, connection_factory=lambda _: provider,
    ).run()


@app.get("/api/v1/voice-native-test-observations")
async def observations():
    return observed


@app.post("/api/v1/voice-native-test-finalize")
async def finalize():
    # The browser explicitly releases final ASR after asserting streamed text
    # and the non-interrupting late bridge, so ordering is deterministic, not a sleep.
    assert provider is not None and not observed["admitted"]
    await provider.emit({"type": "conversation.item.input_audio_transcription.completed",
                         "event_id": "native-asr-final", "item_id": "native-source", "transcript": source})
    return {"ok": True}


@app.post("/api/v1/voice-native-test-delegate")
async def delegate():
    assert provider is not None and provider.delegate_done is not None
    # Release only after the browser proves preamble output actually started.
    await provider.emit(provider.delegate_done)
    await provider.emit(provider.delegate_done)
    provider.delegate_done = None
    return {"ok": True}


app.get("/api/v1/{path:path}")(fixture.fixture_data)
