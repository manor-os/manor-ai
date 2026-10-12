"""Offline Vercel send-boundary checks; no provider, database, or credentials."""

import copy
import json

import pytest

from packages.core.services.voice import realtime
from packages.core.services.voice.live_agent import live_voice_session_factory

pytestmark = pytest.mark.unit

TEXT_CASES = ["Plain ASCII", "中文转录", "🙂🚀", 'quotes " slash \\ newline\n tab\t']


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def connection():
    adapter = realtime._VercelRealtimeConnection(realtime.RealtimeRoute(
        "synthetic-api-key-must-not-appear", "https://ai-gateway.vercel.sh/v1",
        "openai/gpt-realtime-2.1", False, provider="vercel",
    ))
    adapter._socket = Socket()
    return adapter


def session_wire(instructions):
    return {"type": "session-update", "config": {"instructions": instructions}}


def instructions_for_wire_bytes(size, unit):
    overhead = len(compact(session_wire("")).encode("utf-8"))
    # JSON-escaped quotes/newlines count after serialization, not as source chars.
    unit_bytes = len(compact(unit).encode("utf-8")) - 2
    repeats, padding = divmod(size - overhead, unit_bytes)
    instructions = unit * repeats + "x" * padding
    assert len(compact(session_wire(instructions)).encode("utf-8")) == size
    return instructions


@pytest.mark.parametrize("text", TEXT_CASES)
@pytest.mark.parametrize("structured", [False, True])
def test_function_outputs_use_compact_unicode_json_and_roundtrip(text, structured):
    expected = {"status": "ok", "spoken_reply": text}
    if structured:
        expected = {"status": "ok", "response_text": text, "detail": [text]}
        event = realtime.build_function_result_event(call_id="call", result=expected)
    else:
        event = realtime.build_function_output_event(call_id="call", status="ok", spoken_reply=text)

    assert event["item"]["output"] == compact(expected)
    assert json.loads(event["item"]["output"]) == expected


@pytest.mark.parametrize("text", TEXT_CASES)
async def test_send_serializes_final_wire_as_compact_utf8_without_content_loss(text):
    adapter = connection()
    await adapter.send({"type": "session.update", "session": {"instructions": text}})

    assert adapter._socket.sent == [compact(session_wire(text))]
    assert json.loads(adapter._socket.sent[0])["config"]["instructions"] == text


@pytest.mark.parametrize("unit", ["a", "文", "🙂", '"\\\n'])
@pytest.mark.parametrize("excess", [0, 1])
async def test_message_limit_uses_final_utf8_bytes_with_inclusive_boundary(unit, excess):
    assert realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES == 256_000
    size = realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES + excess
    instructions = instructions_for_wire_bytes(size, unit)
    adapter = connection()
    event = {"type": "session.update", "session": {"instructions": instructions}}

    if excess:
        with pytest.raises(realtime.RealtimeMessageTooLarge):
            await adapter.send(event)
        assert adapter._socket.sent == []
        assert adapter._session == {}
    else:
        await adapter.send(event)
        assert len(adapter._socket.sent) == 1
        assert len(adapter._socket.sent[0].encode("utf-8")) == size
        assert json.loads(adapter._socket.sent[0])["config"]["instructions"] == instructions


async def test_oversized_session_does_not_poison_previously_accepted_context():
    adapter = connection()
    session = {"instructions": "Agent identity and conversation 中文🙂", "tools": [{"name": "delegate"}],
               "tool_choice": "auto"}
    await adapter.send({"type": "session.update", "session": session})
    saved = copy.deepcopy(adapter._session)
    adapter._socket.sent.clear()
    with pytest.raises(realtime.RealtimeMessageTooLarge):
        await adapter.send({"type": "session.update", "session": {
            "instructions": "文" * realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES,
            "tools": [], "tool_choice": "none",
        }})
    assert adapter._socket.sent == []
    assert adapter._session == saved

    await adapter.send({"type": "response.create"})
    config = json.loads(adapter._socket.sent[0])["config"]
    assert config["instructions"] == saved["instructions"]
    assert config["tools"] == saved["tools"]
    assert config["providerOptions"]["tool_choice"] == "auto"


async def test_oversized_response_preflights_entire_batch_before_sending_session_update():
    adapter = connection()
    await adapter.send({"type": "session.update", "session": {
        "instructions": "Shared conversation", "tool_choice": "auto",
    }})
    saved = copy.deepcopy(adapter._session)
    adapter._socket.sent.clear()
    with pytest.raises(realtime.RealtimeMessageTooLarge):
        await adapter.send({"type": "response.create", "response": {
            "instructions": "🙂" * realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES,
            "tools": [], "tool_choice": "none",
        }})

    # The mapped session-update fits, but the response-create does not. Neither
    # may reach the provider, or subsequent foreground tools/context diverge.
    assert adapter._socket.sent == []
    assert adapter._session == saved


@pytest.mark.parametrize("structured", [False, True])
async def test_oversized_nested_function_output_rejects_before_provider_send(structured):
    adapter = connection()
    body = '文🙂"\\\n' * 50_000
    if structured:
        event = realtime.build_function_result_event(call_id="call", result={"response_text": body})
    else:
        event = realtime.build_function_output_event(call_id="call", status="ok", spoken_reply=body)
    with pytest.raises(realtime.RealtimeMessageTooLarge):
        await adapter.send(event)
    assert adapter._socket.sent == []
    assert adapter._session == {}


@pytest.mark.parametrize("oversized", [False, True])
async def test_size_diagnostics_never_log_content_or_credentials(caplog, oversized):
    caplog.set_level("INFO")
    adapter = connection()
    marker = "synthetic-private-conversation-never-log"
    body = marker + "文" * (90_000 if oversized else 10)
    size = len(compact(session_wire(body)).encode("utf-8"))
    event = {"type": "session.update", "session": {"instructions": body}}
    error_text = ""
    if oversized:
        with pytest.raises(realtime.RealtimeMessageTooLarge) as error:
            await adapter.send(event)
        error_text = str(error.value)
    else:
        await adapter.send(event)

    assert str(size) in caplog.text
    assert "256000" in caplog.text
    diagnostics = caplog.text + error_text
    assert marker not in diagnostics
    assert adapter._route.api_key not in diagnostics


async def test_legal_result_and_foreground_switch_keep_context_and_function_payload_once():
    adapter = connection()
    context = live_voice_session_factory.instructions(
        "You are the travel Agent.", [{"role": "user", "content": "中文🙂 conversation"}],
    )
    session = realtime.build_realtime_session_update(model="openai/gpt-realtime-2.1", live_agent=True)
    session["session"]["instructions"] = context
    await adapter.send(session)
    saved = copy.deepcopy(adapter._session)
    adapter._socket.sent.clear()
    result = {"status": "ok", "request": "规划行程", "response_text": '结果🙂 "quoted"\nnext'}
    adapter._function_names["result-call"] = "delegate_to_main_agent"
    await adapter.send(realtime.build_function_result_event(call_id="result-call", result=result))
    await adapter.send(live_voice_session_factory.result_response_event())
    await adapter.send(live_voice_session_factory.response_event())

    sent = [json.loads(payload) for payload in adapter._socket.sent]
    assert [event["type"] for event in sent] == [
        "conversation-item-create", "session-update", "response-create", "session-update", "response-create",
    ]
    assert json.loads(sent[0]["item"]["output"]) == result
    assert sent[0]["item"]["name"] == "delegate_to_main_agent"
    assert sent[1]["config"]["instructions"] == sent[3]["config"]["instructions"] == context
    assert [tool["name"] for tool in sent[1]["config"]["tools"]] == ["wait_for_user"]
    assert sent[3]["config"]["tools"] == live_voice_session_factory.tools()
    assert "instructions" not in sent[2].get("options", {})
    assert "instructions" not in sent[4].get("options", {})
    assert adapter._session == saved
    assert all(len(payload.encode("utf-8")) <= realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES
               for payload in adapter._socket.sent)
