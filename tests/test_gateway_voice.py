"""Shared voice gateway routing, turn-taking, protocol and billing regressions."""

import asyncio
import base64
import io
import json
import math
import struct
import time
import wave
from contextlib import asynccontextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from packages.core.services.voice import realtime, whisper
from packages.core.services.voice.browser import browser_session_update
from packages.core.services.voice.gateway_call import GatewayVoiceSession, SpeechDetector, pcm_wav, speech_chunks
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from packages.core.services.voice.latency import VoiceTurnTiming
from packages.core.services.voice.realtime import RealtimeRoute, VoiceAgentOutcome, build_spoken_response_event
from packages.core.services.voice.profiles import speech_voice
from packages.core.services.voice.speech_request import begin_speech_provider_request
from packages.core.services.voice.work_types import VoiceWorkAction, VoiceWorkDecision
from tests.test_browser_voice import Socket, eventually


with wave.open(str(Path(__file__).parent / "fixtures/voice/zh-call.wav")) as _wav:
    _speech = _wav.readframes(_wav.getnframes())
_frames = [struct.unpack("<960h", _speech[p : p + 1920]) for p in range(0, len(_speech) - 1919, 1920)]
_voiced_frame = max(_frames, key=lambda values: sum(v * v for v in values))
_frame_rms = math.sqrt(sum(v * v for v in _voiced_frame) / len(_voiced_frame))


def pcm(amplitude=2500):
    # A speech spectrum, rather than the old 12kHz alternating-sample tone.
    return struct.pack("<960h", *(int(value * amplitude / _frame_rms) for value in _voiced_frame))


def gateway_session():
    ws = Socket()
    call = GatewayVoiceSession(
        ws,
        conversation_id="conversation",
        check_access=AsyncMock(),
        prepare=AsyncMock(),
        transcribe=AsyncMock(return_value="Hello"),
        speak=AsyncMock(return_value=b"mp3"),
        agent=AsyncMock(
            return_value=VoiceAgentOutcome(status="ok", spoken_reply="Reply.", conversation_id="conversation")
        ),
    )
    return call, ws


async def utterance(ws):
    for frame in [pcm()] * 8 + [pcm(0)] * 22:
        await ws.incoming.put({"type": "audio", "audio": base64.b64encode(frame).decode()})


def test_detector_ignores_noise_and_clicks_and_keeps_prefix():
    detector = SpeechDetector()
    for frame in [pcm(50)] * 100 + [pcm()] + [pcm(0)] * 20:
        assert detector.feed(frame) == []
    events = [event for frame in [pcm()] * 8 + [pcm(0)] * 22 for event in detector.feed(frame)]
    assert sum(started for started, _ in events) == 1
    audio = next(audio for _, audio in events if audio)
    with wave.open(io.BytesIO(pcm_wav(audio))) as wav:
        assert wav.getframerate() == 24000 and wav.getnchannels() == 1
        assert wav.getnframes() >= 8 * 960
    assert not detector.frames


async def test_gateway_call_continuously_listens_and_dispatches_each_turn_once():
    call, ws = gateway_session()
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    assert ws.events[0]["duplex_mode"] == "push_to_interrupt"
    assert ws.events[0]["transport_mode"] == "turn_based"
    assert ws.events[0]["voice"] == "warm" and ws.events[0]["voice_locked"] is False
    await utterance(ws)
    await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
    clip = next(e for e in ws.events if e["type"] == "audio_clip")
    await ws.incoming.put({"type": "clip_done", "item_id": clip["item_id"]})
    await utterance(ws)
    await eventually(lambda: call.agent.await_count == 2)
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)
    assert call.transcribe.await_count == 2
    assert call.closed and not call.detector.frames
    assert call.check_access.await_count >= 6


async def test_gateway_latency_logs_cover_each_turn_stage_without_conversation_text(caplog):
    caplog.set_level("INFO", logger="packages.core.services.voice.gateway_call")
    call, ws = gateway_session()
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))
    clip = next(event for event in ws.events if event["type"] == "audio_clip")
    await ws.incoming.put({"type": "clip_done", "item_id": clip["item_id"]})
    await eventually(lambda: any(event["type"] == "listening" for event in ws.events))
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)

    latency_logs = [record.getMessage() for record in caplog.records if "voice_latency " in record.getMessage()]
    stages = {message.split("stage=", 1)[1].split()[0] for message in latency_logs}
    assert {
        "access_preflight",
        "provider_prepare",
        "call_ready",
        "speech_capture",
        "stt_access_check",
        "transcription",
        "turn_admission",
        "agent_access_check",
        "agent_queue",
        "agent",
        "tts_access_check",
        "tts_chunk",
        "tts_first_audio",
        "turn_first_audio",
        "audio_delivery_complete",
        "playback_wait",
        "turn_complete",
    } <= stages
    ordered_stages = [
        message.split("stage=", 1)[1].split()[0]
        for message in latency_logs
    ]
    assert ordered_stages.index("playback_wait") < ordered_stages.index("turn_complete")
    assert all(f"call={call.call_id}" in message and "turn=" in message for message in latency_logs)
    assert "Hello" not in "\n".join(latency_logs)
    assert "Reply." not in "\n".join(latency_logs)


async def test_gateway_transcription_timeout_logs_failed_stage_and_cleans_timing(caplog):
    caplog.set_level("INFO", logger="packages.core.services.voice.gateway_call")
    call, _ = gateway_session()
    started_at = time.monotonic()
    call.input_timings[1] = VoiceTurnTiming(
        turn_id=1,
        speech_started_at=started_at,
        input_committed_at=started_at,
    )
    call.inputs.put_nowait((1, pcm()))
    call.transcribe.side_effect = TimeoutError("speech provider timed out")

    with pytest.raises(TimeoutError):
        await call.transcribe_inputs()

    latency_logs = [
        record.getMessage()
        for record in caplog.records
        if "voice_latency " in record.getMessage()
    ]
    assert any(
        "stage=transcription" in message and "outcome=timeout" in message
        for message in latency_logs
    )
    assert any(
        "stage=turn_complete" in message and "outcome=timeout" in message
        for message in latency_logs
    )
    assert not call.input_timings


async def test_gateway_voice_selection_applies_to_later_speech():
    call, ws = gateway_session()
    await ws.incoming.put({"type": "voice", "voice": "deep"})
    await ws.incoming.put({"type": "end"})
    await call.receive()
    assert call.voice == "deep"
    assert ws.events[-1] == {"type": "voice", "voice": "deep", "locked": False}


async def test_gateway_voice_change_during_reply_applies_to_the_next_turn():
    call, ws = gateway_session()
    call.agent.return_value = VoiceAgentOutcome(
        status="ok", spoken_reply="First sentence. Second sentence.", conversation_id="conversation"
    )
    first_speech_started = asyncio.Event()
    release_first_speech = asyncio.Event()
    spoken = []

    async def speak(text, voice):
        spoken.append((text, voice))
        if len(spoken) == 1:
            first_speech_started.set()
            await release_first_speech.wait()
        return b"audio"

    call.speak.side_effect = speak
    task = asyncio.create_task(call.run())
    try:
        await eventually(lambda: bool(ws.events))
        await utterance(ws)
        await asyncio.wait_for(first_speech_started.wait(), 2)
        await ws.incoming.put({"type": "voice", "voice": "deep"})
        await eventually(lambda: call.voice == "deep")
        release_first_speech.set()
        await eventually(lambda: len(spoken) == 2)
        assert [voice for _, voice in spoken] == ["warm", "warm"]

        first_clip = next(e for e in ws.events if e["type"] == "audio_clip")
        await ws.incoming.put({"type": "clip_done", "item_id": first_clip["item_id"]})
        await eventually(lambda: len([e for e in ws.events if e["type"] == "audio_clip"]) == 2)
        second_clip = next(e for e in reversed(ws.events) if e["type"] == "audio_clip")
        await ws.incoming.put({"type": "clip_done", "item_id": second_clip["item_id"]})
        await eventually(lambda: ws.events[-1]["type"] == "listening")

        await utterance(ws)
        await eventually(lambda: len(spoken) >= 3)
        assert spoken[2][1] == "deep"
    finally:
        release_first_speech.set()
        await ws.incoming.put({"type": "end"})
        await asyncio.wait_for(task, 2)


def test_voice_profiles_map_only_to_compatible_provider_voices():
    assert speech_voice("warm", "openai/tts-1-hd") == "nova"
    assert speech_voice("clear", "openai/tts-1") == "alloy"
    assert speech_voice("warm", "openai/gpt-4o-mini-tts") == "marin"
    assert speech_voice("clear", "google/gemini-2.5-flash-preview-tts") == "Kore"
    assert speech_voice("deep", "zyphra/zonos-v0.1-transformer") is None


def test_gateway_input_overload_keeps_the_newest_recording():
    call, _ = gateway_session()
    for input_id in range(1, 6):
        call.queue_input((input_id, bytes([input_id])))

    assert call.inputs.qsize() == 4
    assert [call.inputs.get_nowait()[0] for _ in range(4)] == [2, 3, 4, 5]


async def test_gateway_chat_backlog_does_not_block_or_drop_accepted_turns():
    call, _ = gateway_session()
    for generation in range(10, 14):
        call.turns.put_nowait((generation, f"queued-{generation}"))
    call.input_id = 2
    call.input_resolved.clear()
    call.transcribe.side_effect = ["older input", "current input"]
    call.inputs.put_nowait((1, pcm()))
    call.inputs.put_nowait((2, pcm()))

    worker = asyncio.create_task(call.transcribe_inputs())
    try:
        await eventually(lambda: call.transcribe.await_count == 2 and call.input_resolved.is_set())
        queued = [call.turns.get_nowait() for _ in range(call.turns.qsize())]
        assert [generation for generation, _ in queued] == [10, 11, 12, 13, 1, 2]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_gateway_manual_interrupt_stops_reply_and_returns_to_listening(caplog):
    caplog.set_level("INFO", logger="packages.core.services.voice.gateway_call")
    call, ws = gateway_session()
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
    clip = next(e for e in ws.events if e["type"] == "audio_clip")

    await ws.incoming.put({"type": "stop_reply", "item_id": clip["item_id"]})
    await eventually(
        lambda: any(e["type"] == "interrupt" and e["generation"] == 2 for e in ws.events)
    )
    assert any(e["type"] == "listening" and e["generation"] == 2 for e in ws.events)
    assert call.generation == 2

    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)
    assert any(
        "stage=turn_complete" in record.getMessage()
        and "outcome=interrupted" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        "stage=playback_wait" in record.getMessage()
        and "outcome=interrupted" in record.getMessage()
        for record in caplog.records
    )


async def test_gateway_hangup_during_final_playback_is_closed(caplog):
    caplog.set_level("INFO", logger="packages.core.services.voice.gateway_call")
    call, ws = gateway_session()
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))

    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)

    latency_logs = [
        record.getMessage()
        for record in caplog.records
        if "voice_latency " in record.getMessage()
    ]
    assert any(
        "stage=playback_wait" in message and "outcome=closed" in message
        for message in latency_logs
    )
    assert any(
        "stage=turn_complete" in message and "outcome=closed" in message
        for message in latency_logs
    )
    assert not any(
        "stage=turn_complete" in message and "outcome=interrupted" in message
        for message in latency_logs
    )


@pytest.mark.parametrize("text,first", [
    ("你好，我在。现在我们可以继续聊你的问题。", "你好，我在。"),
    ("Hello! Let us talk about version 3.1 and the next steps.", "Hello!"),
    ("你好" * 100, "你好" * 40),
])
def test_first_spoken_sentence_does_not_wait_for_a_full_batch(text, first):
    chunks = list(speech_chunks(text))
    assert chunks[0] == first
    assert "".join(chunks) == text
    assert all(chunk.strip() for chunk in chunks)


async def test_next_sentence_is_prepared_during_playback_but_not_sent_before_ack():
    call, ws = gateway_session()
    call.agent.return_value = VoiceAgentOutcome(
        status="ok", spoken_reply="你好，我在。现在我们可以继续聊你的问题。", conversation_id="conversation"
    )
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await eventually(lambda: call.speak.await_count == 2)
    clips = [e for e in ws.events if e["type"] == "audio_clip"]
    assert len(clips) == 1
    assert call.speak.await_args_list[0].args == ("你好，我在。", "warm")
    phases = [e["type"] for e in ws.events if e["type"] in {"transcribing", "thinking", "synthesizing"}]
    assert phases == ["transcribing", "thinking", "synthesizing", "synthesizing"]
    assert not any(e["type"] == "turn" for e in ws.events)
    captions = [e for e in ws.events if e["type"] == "caption"]
    assert [e["delta"] for e in captions] == ["你好，我在。"]
    await ws.incoming.put({"type": "clip_done", "item_id": clips[0]["item_id"]})
    await eventually(lambda: len([e for e in ws.events if e["type"] == "audio_clip"]) == 2)
    last_clip = next(e for e in reversed(ws.events) if e["type"] == "audio_clip")
    await eventually(lambda: len([e for e in ws.events if e["type"] == "caption"]) == 2)
    captions = [e for e in ws.events if e["type"] == "caption"]
    assert len({e["item_id"] for e in captions}) == 1
    assert "".join(e["delta"] for e in captions) == call.agent.return_value.spoken_reply
    assert not any(e["type"] == "turn" for e in ws.events)
    assert not any(e["type"] == "listening" for e in ws.events)
    await ws.incoming.put({"type": "clip_done", "item_id": last_clip["item_id"]})
    await eventually(lambda: any(e["type"] == "turn" for e in ws.events))
    turn = next(e for e in ws.events if e["type"] == "turn")
    assert turn["generation"] == 1 and turn["text"] == call.agent.return_value.spoken_reply
    await eventually(lambda: ws.events[-1]["type"] == "listening")
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)


async def test_hangup_during_next_sentence_preparation_settles_without_sending_it():
    call, ws = gateway_session()
    call.agent.return_value = VoiceAgentOutcome(
        status="ok", spoken_reply="First sentence. Second sentence.", conversation_id="conversation"
    )
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def speak(text, _voice):
        if text.startswith("First"):
            return b"first audio"
        begin_speech_provider_request()
        entered.set()
        await release.wait()
        settled.set()
        return b"second audio"

    call.speak.side_effect = speak
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await eventually(lambda: ws.closed)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 2)
    assert settled.is_set()
    assert len([e for e in ws.events if e["type"] == "audio_clip"]) == 1


async def test_hangup_settles_concurrent_recognition_and_synthesis_once():
    call, ws = gateway_session()
    tts_entered, stt_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    settled = []

    async def transcribe(_):
        if call.transcribe.await_count == 1:
            return "First turn"
        begin_speech_provider_request()
        stt_entered.set()
        await release.wait()
        settled.append("stt")
        return "Second turn"

    async def speak(_, _voice):
        begin_speech_provider_request()
        tts_entered.set()
        await release.wait()
        settled.append("tts")
        return b"audio"

    call.transcribe.side_effect = transcribe
    call.speak.side_effect = speak
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(tts_entered.wait(), 2)
    await utterance(ws)
    await asyncio.wait_for(stt_entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await eventually(lambda: ws.closed)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 2)
    assert sorted(settled) == ["stt", "tts"]
    call.agent.assert_awaited_once_with("First turn")
    assert not call.pending_audio
    assert not any(e["type"] == "audio_clip" for e in ws.events)


async def test_hangup_drains_every_transcript_already_exposed_to_the_caller():
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()

    async def agent(text):
        if text == "First turn":
            entered.set()
            await release.wait()
        return VoiceAgentOutcome(
            status="ok",
            spoken_reply=f"Reply to {text}",
            conversation_id="conversation",
        )

    call.transcribe.side_effect = ["First turn", "Second turn"]
    call.agent.side_effect = agent
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await utterance(ws)
    await eventually(
        lambda: any(
            event.get("type") == "transcript" and event.get("text") == "Second turn"
            for event in ws.events
        )
    )

    await ws.incoming.put({"type": "end"})
    await eventually(lambda: call.closed and ws.closed)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 2)

    assert [entry.args for entry in call.agent.await_args_list] == [
        ("First turn",),
        ("Second turn",),
    ]
    call.speak.assert_not_awaited()
    assert not any(event["type"] == "turn" for event in ws.events)


async def test_hangup_turn_settlement_is_bounded(monkeypatch, caplog):
    from packages.core.services.voice import gateway_call

    call, ws = gateway_session()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def agent(_text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    call.agent.side_effect = agent
    monkeypatch.setattr(gateway_call, "TURN_SETTLEMENT_TIMEOUT_SECONDS", 0.02)
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)

    assert stopped.is_set()
    assert "Voice turn settlement exceeded its deadline" in caplog.text
    call.speak.assert_not_awaited()


async def test_gateway_durable_work_returns_to_listening_without_timeout_reply():
    call, ws = gateway_session()
    receipt = VoiceWorkReceipt("work-1", "message-1", "Run the report")
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(work):
        assert work == receipt
        entered.set()
        await release.wait()
        return VoiceAgentOutcome(
            status="ok",
            spoken_reply="The report is ready.",
            conversation_id="conversation",
        )

    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock(side_effect=execute)

    worker = asyncio.create_task(call.run_turns())
    call.generation = 1
    call.queue_turn((1, receipt.text, receipt))

    await asyncio.wait_for(entered.wait(), 2)
    await eventually(
        lambda: {"type": "work", "status": "running"} in ws.events
    )
    assert {"type": "listening", "generation": 1} in ws.events
    assert not any(event.get("type") == "turn" for event in ws.events)
    call.speak.assert_not_awaited()
    assert not call.execute_work.await_args.args[0].recovered

    release.set()
    await eventually(
        lambda: {"type": "work", "status": "completed"} in ws.events
    )
    await eventually(lambda: len([event for event in ws.events if event.get("type") == "audio_clip"]) == 1)
    call.finish_clip("played")
    await eventually(
        lambda: any(
            event.get("type") == "turn" and event.get("text") == "The report is ready."
            for event in ws.events
        )
    )
    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_progress_question_stays_in_foreground_control_plane():
    call, ws = gateway_session()
    call.agent_active = True
    call.admit_work = AsyncMock()
    call.record_control_turn = AsyncMock()
    call.transcribe.return_value = "Any update?"
    call.input_id = 1
    call.input_timings[1] = VoiceTurnTiming(turn_id=1)
    worker = asyncio.create_task(call.transcribe_inputs())

    await call.inputs.put((1, pcm()))
    await eventually(lambda: call.record_control_turn.await_count == 1)

    call.admit_work.assert_not_awaited()
    assert call.turns.empty()
    assert "still working" in call.record_control_turn.await_args.args[1]
    await eventually(
        lambda: any(
            event.get("type") == "turn" and "still working" in event.get("text", "")
            for event in ws.events
        )
    )
    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_progress_race_reports_the_task_that_just_finished():
    call, ws = gateway_session()
    active = VoiceWorkReceipt("old", "message-old", "Run the report")
    call.agent_active = True
    call.agent_active_receipt = active
    call.admit_work = AsyncMock()
    call.record_control_turn = AsyncMock()
    call.transcribe.return_value = "Any update?"

    async def finish_during_route(_text, _receipt):
        call.agent_active = False
        call.agent_active_receipt = None
        call.last_work_completed_at = time.monotonic()
        call.last_work_status = "ok"
        return VoiceWorkDecision(
            VoiceWorkAction.STATUS,
            "I'm still working on it.",
        )

    call.route_followup = AsyncMock(side_effect=finish_during_route)
    call.input_id = 1
    call.input_timings[1] = VoiceTurnTiming(turn_id=1)
    worker = asyncio.create_task(call.transcribe_inputs())

    await call.inputs.put((1, pcm()))
    await eventually(lambda: call.record_control_turn.await_count == 1)

    call.admit_work.assert_not_awaited()
    assert call.turns.empty()
    assert "just finished" in call.record_control_turn.await_args.args[1]

    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_call_correction_stays_in_foreground_control_plane():
    call, ws = gateway_session()
    call.agent_active = True
    call.admit_work = AsyncMock()
    call.record_control_turn = AsyncMock()
    call.transcribe.return_value = "为什么说韩语?"
    call.input_id = 1
    call.input_timings[1] = VoiceTurnTiming(turn_id=1)
    worker = asyncio.create_task(call.transcribe_inputs())

    await call.inputs.put((1, pcm()))
    await eventually(lambda: call.record_control_turn.await_count == 1)

    call.admit_work.assert_not_awaited()
    assert call.turns.empty()
    assert "语言识别错了" in call.record_control_turn.await_args.args[1]
    await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))
    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_new_instruction_is_queued_with_one_foreground_reply():
    call, ws = gateway_session()
    active = VoiceWorkReceipt("active", "message-active", "Run the report")
    receipt = VoiceWorkReceipt("queued", "message-queued", "Email it to Alice")
    call.agent_active = True
    call.agent_active_receipt = active
    call.admit_work = AsyncMock(return_value=receipt)
    call.route_followup = AsyncMock(
        return_value=VoiceWorkDecision(
            VoiceWorkAction.QUEUE,
            "I'll finish “Run the report” first, then handle “Email it to Alice.”",
        )
    )
    call.transcribe.return_value = receipt.text
    call.input_id = 1
    call.input_timings[1] = VoiceTurnTiming(turn_id=1)
    worker = asyncio.create_task(call.transcribe_inputs())

    await call.inputs.put((1, pcm()))
    await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))

    assert call.turns.get_nowait() == (1, receipt.text, receipt)
    assert {"type": "work", "status": "queued"} in ws.events
    call.speak.assert_awaited_once_with(
        "I'll finish “Run the report” first, then handle “Email it to Alice.”",
        "warm",
    )
    assert receipt.id in call.acknowledged_work_ids

    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_explicit_replacement_cancels_and_queues_new_receipt():
    call, ws = gateway_session()
    active = VoiceWorkReceipt("old", "message-old", "Run the report")
    replacement = VoiceWorkReceipt(
        "new",
        "message-new",
        "Stop that and instead email Alice",
    )
    call.agent_active = True
    call.agent_active_receipt = active
    call.route_followup = AsyncMock(
        return_value=VoiceWorkDecision(
            VoiceWorkAction.REPLACE,
            "Okay, I'll switch to the new request.",
        )
    )
    call.cancel_work = AsyncMock(return_value=True)
    call.admit_work = AsyncMock(return_value=replacement)
    call.record_control_turn = AsyncMock()
    call.transcribe.return_value = replacement.text
    call.input_id = 1
    call.input_timings[1] = VoiceTurnTiming(turn_id=1)
    worker = asyncio.create_task(call.transcribe_inputs())

    await call.inputs.put((1, pcm()))
    await eventually(lambda: call.cancel_work.await_count == 1)
    await eventually(lambda: call.record_control_turn.await_count == 1)

    call.cancel_work.assert_awaited_once_with(active, replacement)
    assert active.id in call.suppressed_work_ids
    assert call.record_control_turn.await_args.args[0] == ""
    assert call.turns.get_nowait() == (1, replacement.text, replacement)
    assert {"type": "work", "status": "queued"} in ws.events

    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_skips_a_superseded_queued_replacement():
    call, _ = gateway_session()
    first = VoiceWorkReceipt("first", "message-first", "Email Alice")
    second = VoiceWorkReceipt("second", "message-second", "Email Bob")
    call.execute_work = AsyncMock(
        return_value=VoiceAgentOutcome(
            status="ok",
            spoken_reply="Done.",
            conversation_id="conversation",
        )
    )
    call.suppressed_work_ids.add(first.id)
    call.queue_turn((1, first.text, first))
    call.queue_turn((2, second.text, second))
    worker = asyncio.create_task(call.run_turns())

    await eventually(lambda: call.execute_work.await_count == 1)

    assert call.execute_work.await_args.args[0] == second
    assert first.id not in call.suppressed_work_ids

    worker.cancel()
    for task in tuple(call.control_tasks):
        task.cancel()
    await asyncio.gather(worker, *tuple(call.control_tasks), return_exceptions=True)


async def test_gateway_old_control_reply_is_not_relabelled_as_a_new_turn():
    call, ws = gateway_session()
    release_record = asyncio.Event()

    async def record_control_turn(_user_text, _reply):
        await release_record.wait()

    call.record_control_turn = record_control_turn
    task = asyncio.create_task(
        call._send_control_reply(0, "Any update?", "progress", record=True)
    )
    await asyncio.sleep(0)
    call.generation = 1
    release_record.set()
    await task

    assert not any(
        event["type"] in {"caption", "audio_clip", "turn"}
        for event in ws.events
    )
    call.speak.assert_not_awaited()


async def test_barge_in_during_action_does_not_cancel_replay_or_speak_old_reply():
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(text):
        entered.set()
        await release.wait()
        return VoiceAgentOutcome(status="ok", spoken_reply=f"Reply to {text}", conversation_id="conversation")

    call.agent.side_effect = action
    call.transcribe.side_effect = ["first", "second"]
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await entered.wait()
    # Recognition can confirm the new turn while the first action is still
    # running. Only confirmed text permanently supersedes its audio.
    await utterance(ws)
    await eventually(lambda: call.generation == 2)
    call.agent.assert_awaited_once_with("first")
    release.set()
    await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
    assert [entry.args for entry in call.agent.await_args_list] == [("first",), ("second",)]
    call.speak.assert_awaited_once_with("Reply to second", "warm")
    await ws.incoming.put({"type": "end"})
    await task


async def test_interruption_during_synthesis_settles_callback_without_playing_stale_audio():
    call, ws = gateway_session()
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def speak(text, _voice):
        entered.set()
        await release.wait()
        settled.set()
        return b"audio"

    call.speak.side_effect = speak
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await entered.wait()
    await utterance(ws)
    await eventually(lambda: call.generation == 2)
    release.set()
    await settled.wait()
    await ws.incoming.put({"type": "end"})
    await task
    assert not any(e["type"] == "audio_clip" and e["generation"] == 1 for e in ws.events)


@pytest.mark.parametrize("stage", ["transcribe", "speak"])
@pytest.mark.parametrize("ending", ["hangup", "cancel", "cancel_during_settlement"])
async def test_call_end_settles_accepted_audio_once_without_starting_more_work(stage, ending):
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()
    commits = []

    async def accepted_request(_, _voice=None):
        begin_speech_provider_request()
        entered.set()
        await release.wait()  # Upstream accepted the request but has not replied.
        commits.append(stage)  # The shared callback commits usage after reply.
        return "Hello" if stage == "transcribe" else b"audio"

    getattr(call, stage).side_effect = accepted_request
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    if ending == "cancel":
        task.cancel()
    else:
        await ws.incoming.put({"type": "end"})
    await eventually(lambda: call.closed and call.turns.empty() and ws.closed)
    if ending == "cancel_during_settlement":
        task.cancel()
    assert not task.done() and not commits
    release.set()
    if ending == "hangup":
        await asyncio.wait_for(task, 2)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert commits == [stage]
    assert call.transcribe.await_count == 1
    assert call.agent.await_count == (1 if stage == "speak" else 0)
    assert call.speak.await_count == (1 if stage == "speak" else 0)
    assert not call.pending_audio
    assert not any(e["type"] == "audio_clip" for e in ws.events)


@pytest.mark.parametrize("failure", ["provider_error", "deadline"])
async def test_failed_audio_settlement_is_bounded_and_leaves_no_background_task(monkeypatch, caplog, failure):
    from packages.core.services.voice import gateway_call

    call, ws = gateway_session()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def provider(_):
        begin_speech_provider_request()
        entered.set()
        try:
            await release.wait()
            raise RuntimeError("Provider failed")
        finally:
            finished.set()

    call.transcribe.side_effect = provider
    monkeypatch.setattr(gateway_call, "AUDIO_SETTLEMENT_TIMEOUT_SECONDS", 0.02)
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await ws.incoming.put({"type": "end"})
    await eventually(lambda: call.closed)
    if failure == "provider_error":
        release.set()
    await asyncio.wait_for(task, 2)
    assert finished.is_set() and not call.pending_audio
    assert "settlement failed" in caplog.text if failure == "provider_error" else "exceeded its deadline" in caplog.text
    call.agent.assert_not_awaited()


@pytest.mark.parametrize("blocked_event", ["caption", "audio_clip"])
async def test_interrupt_during_socket_send_never_labels_old_audio_as_current(blocked_event):
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()
    send_json = ws.send_json

    async def delayed_send(event):
        if event["type"] == blocked_event and event.get("generation") == 1:
            entered.set()
            await release.wait()
        await send_json(event)

    ws.send_json = delayed_send
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    await utterance(ws)
    await eventually(lambda: call.generation == 2)
    release.set()
    await eventually(lambda: any(e["type"] == "audio_clip" and e["generation"] == 2 for e in ws.events))
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)
    interrupt_index = next(i for i, e in enumerate(ws.events) if e["type"] == "interrupt" and e["generation"] == 2)
    late_old = [e for e in ws.events[interrupt_index + 1 :] if e.get("generation") == 1]
    assert [e["type"] for e in late_old] == [blocked_event]
    assert call.agent.await_count == 2


async def test_muted_audio_never_becomes_a_turn():
    call, ws = gateway_session()
    await ws.incoming.put({"type": "mute", "muted": True})
    await utterance(ws)
    await ws.incoming.put({"type": "end"})
    await call.run()
    call.transcribe.assert_not_awaited()
    call.agent.assert_not_awaited()


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session.update"},
        {"type": "audio", "audio": "!invalid!"},
        {"type": "audio", "audio": base64.b64encode(b"x" * 10000).decode()},
    ],
)
async def test_gateway_audio_rejects_injected_events_and_oversize(event):
    call, ws = gateway_session()
    await ws.incoming.put(event)
    with pytest.raises(ValueError):
        await call.receive()
    call.transcribe.assert_not_awaited()


async def test_revocation_before_turn_prevents_all_provider_work():
    call, ws = gateway_session()
    call.check_access.side_effect = HTTPException(403, "Revoked")
    await call.turns.put((0, pcm()))
    with pytest.raises(HTTPException):
        await call.run_turns()
    call.transcribe.assert_not_awaited()
    call.agent.assert_not_awaited()


@pytest.mark.parametrize("role", ["voice", "stt"])
async def test_non_openai_role_byok_wins_over_platform_realtime(monkeypatch, role):
    async def metadata(requested, **kwargs):
        assert kwargs == {"entity_id": "entity", "user_id": "owner"}
        return {"llm_api_key": "private"} if requested == role else None

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        metadata,
    )
    route = AsyncMock()
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        route,
    )
    assert (
        await realtime.resolve_realtime_route(
            "entity",
            user_id="owner",
            required=False,
            respect_audio_role_overrides=True,
        )
        is None
    )
    route.assert_not_awaited()


@pytest.mark.parametrize("role", ["voice", "stt"])
async def test_native_openai_audio_byok_does_not_expand_to_full_realtime(
    monkeypatch,
    role,
):
    async def metadata(requested, **kwargs):
        assert kwargs == {"entity_id": "entity", "user_id": "owner"}
        return {"llm_api_key": "sk-private", "llm_base_url": "https://api.openai.com/v1"} if requested == role else None

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        metadata,
    )
    resolver = AsyncMock()
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        resolver,
    )
    route = await realtime.resolve_realtime_route(
        "entity",
        user_id="owner",
        required=False,
        respect_audio_role_overrides=True,
    )
    assert route is None
    resolver.assert_not_awaited()


async def test_native_openai_primary_byok_uses_browser_realtime(monkeypatch):
    async def metadata(requested, **_kwargs):
        return {"llm_api_key": "sk-primary"} if requested == "primary" else None

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        metadata,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        AsyncMock(return_value="openai/gpt-5.6-terra"),
    )
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        AsyncMock(),
    )
    route = await realtime.resolve_realtime_route(
        "entity",
        user_id="owner",
        required=False,
        respect_audio_role_overrides=True,
    )
    assert route and route.byok and route.provider == "openai"
    assert route.model == "openai/gpt-realtime"


@pytest.mark.parametrize("provider", ["vercel", "openai"])
async def test_platform_voice_uses_shared_gateway_precedence(monkeypatch, provider):
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.delenv("OPENAI_REALTIME_MODEL", raising=False)
    monkeypatch.delenv("VERCEL_REALTIME_MODEL", raising=False)
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        AsyncMock(return_value="openai/gpt-5.6-terra"),
    )
    resolver = AsyncMock(
        return_value=SimpleNamespace(
            provider=provider,
            api_key="private",
            base_url=(
                "https://ai-gateway.vercel.sh/v1"
                if provider == "vercel"
                else "https://api.openai.com/v1"
            ),
            source_detail="db",
        )
    )
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        resolver,
    )
    route = await realtime.resolve_realtime_route(
        "entity",
        user_id="owner",
        required=False,
        respect_audio_role_overrides=True,
    )
    assert resolver.await_args.args[0] == "openai/gpt-realtime"
    assert resolver.await_args.kwargs["provider_chain"] == ("vercel", "openai")
    assert route is not None
    assert route.model == (
        "openai/gpt-realtime-mini"
        if provider == "vercel"
        else "openai/gpt-realtime"
    )


async def test_transcription_only_realtime_model_is_not_admitted(monkeypatch):
    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "openai/gpt-realtime-whisper")
    with pytest.raises(RuntimeError, match="supported native OpenAI"):
        await realtime.resolve_realtime_route(
            "entity",
            user_id="owner",
            required=False,
        )


def test_gateway_realtime_2_bills_text_audio_and_cached_audio_separately():
    from packages.core.services.model_pricing import estimate_token_cost_usd

    # 100 input tokens: 30 text, 50 fresh audio, 20 cached audio.
    # 50 output tokens: 10 text, 40 audio. Cached audio is $0.40/M.
    cost = estimate_token_cost_usd(
        100,
        50,
        "openai/gpt-realtime-2",
        pricing_source="official",
        cache_read_tokens=20,
        audio_input_tokens=70,
        audio_output_tokens=40,
        cached_audio_input_tokens=20,
    )
    assert cost == pytest.approx((30 * 4 + 50 * 32 + 20 * 0.4 + 10 * 24 + 40 * 64) / 1_000_000)


async def test_self_hosted_does_not_borrow_platform_realtime_credentials(monkeypatch):
    monkeypatch.setattr(realtime, "_resolve_realtime_byok", AsyncMock(return_value=None))
    resolver = AsyncMock()
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        resolver,
    )
    assert (
        await realtime.resolve_realtime_route(
            "entity",
            user_id="owner",
            required=False,
            allow_managed=False,
        )
        is None
    )
    resolver.assert_not_awaited()


async def test_vercel_protocol_keeps_live_tools_manual_responses_and_usage():
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock())
    adapter = realtime._VercelRealtimeConnection(
        RealtimeRoute(
            "private",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        )
    )
    adapter._socket = socket
    config = browser_session_update("gpt-realtime")
    await adapter.send(config)
    payload = json.loads(socket.send.await_args.args[0])
    options = payload["config"]["providerOptions"]
    assert options["audio"]["input"]["turn_detection"]["create_response"] is False
    assert options["audio"]["input"]["noise_reduction"] == {"type": "far_field"}
    assert options["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    assert options["tool_choice"] == "auto"
    socket.recv.return_value = json.dumps(
        {"type": "session-updated", "raw": {"type": "session.updated", "session": config["session"]}}
    )
    assert (await anext(adapter))["type"] == "session.updated"
    assert adapter._verified
    await adapter.send(build_spoken_response_event(spoken_reply="Approved response"))
    updates = [json.loads(c.args[0]) for c in socket.send.await_args_list]
    assert updates[-2]["config"]["providerOptions"]["tool_choice"] == "none"
    assert updates[-1]["options"]["modalities"] == ["audio"]
    await adapter.send({"type": "response.create"})
    updates = [json.loads(c.args[0]) for c in socket.send.await_args_list]
    assert updates[-2]["config"]["providerOptions"]["tool_choice"] == "auto"
    raw = {"type": "response.done", "response": {"id": "r", "usage": {"input_tokens": 50}}}
    socket.recv.return_value = json.dumps({"type": "response-done", "raw": raw})
    assert await anext(adapter) == raw


async def test_vercel_rejects_session_that_would_autonomously_answer():
    socket = SimpleNamespace(
        recv=AsyncMock(return_value=json.dumps({"raw": {"type": "session.updated", "session": {}}}))
    )
    adapter = realtime._VercelRealtimeConnection(
        RealtimeRoute(
            "private",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        )
    )
    adapter._socket = socket
    with pytest.raises(RuntimeError, match="response controls"):
        await anext(adapter)


async def test_vercel_rejects_session_with_changed_transcription_control():
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock())
    adapter = realtime._VercelRealtimeConnection(
        RealtimeRoute(
            "private",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        )
    )
    adapter._socket = socket
    config = browser_session_update("gpt-realtime")
    await adapter.send(config)
    accepted = json.loads(json.dumps(config["session"]))
    accepted["audio"]["input"]["transcription"] = {"model": "unexpected-model"}
    socket.recv.return_value = json.dumps(
        {
            "type": "session-updated",
            "raw": {"type": "session.updated", "session": accepted},
        }
    )

    with pytest.raises(RuntimeError, match="response controls"):
        await anext(adapter)


async def test_vercel_accepts_provider_added_transcription_defaults():
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock())
    adapter = realtime._VercelRealtimeConnection(
        RealtimeRoute(
            "private",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        )
    )
    adapter._socket = socket
    config = browser_session_update("gpt-realtime")
    await adapter.send(config)
    accepted = json.loads(json.dumps(config["session"]))
    accepted["audio"]["input"]["transcription"].update(
        language=None,
        prompt=None,
    )
    socket.recv.return_value = json.dumps(
        {
            "type": "session-updated",
            "raw": {"type": "session.updated", "session": accepted},
        }
    )

    assert (await anext(adapter))["type"] == "session.updated"
    assert adapter._verified


async def test_vercel_error_preserves_provider_code():
    socket = SimpleNamespace(
        recv=AsyncMock(
            return_value=json.dumps(
                {
                    "type": "error",
                    "code": "response_cancel_not_active",
                    "message": "No active response",
                }
            )
        )
    )
    adapter = realtime._VercelRealtimeConnection(
        RealtimeRoute(
            "private",
            "https://ai-gateway.vercel.sh/v1",
            "openai/gpt-realtime-mini",
            False,
            provider="vercel",
        )
    )
    adapter._socket = socket

    assert await anext(adapter) == {
        "type": "error",
        "error": {
            "code": "response_cancel_not_active",
            "message": "No active response",
        },
    }


async def test_vercel_mints_scoped_secret_and_never_puts_platform_key_in_websocket(monkeypatch):
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"token": "vcst_single_use", "expiresAt": 123})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        realtime.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler)),
    )

    @asynccontextmanager
    async def connect(url, **kwargs):
        captured.update(url=url, options=kwargs)
        yield SimpleNamespace()

    monkeypatch.setattr(realtime.websockets, "connect", connect)
    route = RealtimeRoute(
        "private-platform-key",
        "https://ai-gateway.vercel.sh/v1",
        "openai/gpt-realtime-mini",
        False,
        provider="vercel",
    )
    async with realtime.open_realtime_connection(route):
        pass
    request = captured["request"]
    assert str(request.url) == "https://ai-gateway.vercel.sh/v1/realtime/client-secrets"
    assert json.loads(request.content) == {
        "model": "openai/gpt-realtime-mini",
        "expiresIn": 60,
    }
    assert request.headers["ai-gateway-auth-method"] == "api-key"
    assert captured["url"].startswith("wss://ai-gateway.vercel.sh/v4/ai/realtime-model?ai-model-id=")
    assert captured["options"]["subprotocols"] == ["ai-gateway-realtime.v1", "ai-gateway-auth.vcst_single_use"]
    assert route.api_key not in captured["url"] + json.dumps(captured["options"])


@pytest.mark.parametrize(
    "byok,selected", [(False, "openai/whisper-1"), (True, "openai/whisper-1"), (False, "openai/gpt-4o-audio-preview")]
)
async def test_openrouter_stt_uses_documented_json_endpoint_and_reports_cost(monkeypatch, byok, selected):
    from packages.core.services import model_gateway

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    resolver = AsyncMock(
        return_value=SimpleNamespace(
            provider="openrouter", api_key="sk-or-private", base_url="https://openrouter.ai/api/v1", source_detail="db"
        )
    )
    monkeypatch.setattr(model_gateway, "resolve_official_model_route", resolver)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"text": "你好", "usage": {"seconds": 1.2, "cost": 0.00012}})

    original = httpx.AsyncClient
    monkeypatch.setattr(whisper.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    result = await whisper.transcribe_blob(
        pcm_wav(pcm()),
        mime="audio/wav",
        filename="voice.wav",
        resolved_model=selected,
        user_api_key="sk-or-private" if byok else None,
    )
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/audio/transcriptions"
    body = json.loads(requests[0].content)
    assert body["model"] == "openai/whisper-1" and body["input_audio"]["format"] == "wav"
    assert result.text == "你好" and result.cost_usd == 0.00012
    if byok:
        resolver.assert_not_awaited()


async def test_gateway_preflight_explains_missing_role_without_asking_for_openai(monkeypatch):
    from packages.core.services import model_resolver

    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(model_resolver, "resolve_llm_metadata_for_user", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as error:
        await chat_voice.prepare_gateway_call(ChatAudioScope("entity", "owner"))
    assert error.value.status_code == 503
    assert "Speech-to-text" in error.value.detail and "OpenRouter" in error.value.detail
    assert "native OpenAI Realtime" not in error.value.detail


@pytest.mark.parametrize("provider", ["openai", "vercel"])
async def test_browser_realtime_uses_shared_provider_transport(monkeypatch, provider):
    route = RealtimeRoute(
        "private",
        "https://api.openai.com/v1" if provider == "openai" else "https://ai-gateway.vercel.sh/v1",
        "openai/gpt-realtime",
        provider == "openai",
        provider=provider,
    )
    realtime = SimpleNamespace(ready=True, run=AsyncMock())
    constructor = Mock(return_value=realtime)
    monkeypatch.setattr(chat_voice, "resolve_realtime_route", AsyncMock(return_value=route))
    monkeypatch.setattr(chat_voice, "BrowserVoiceSession", constructor)
    scope = ChatAudioScope("entity", "owner", conversation_id="conversation")
    await chat_voice.run_voice_session(
        Socket(), object(), chat_voice.CallStart(token="test"), scope
    )
    assert constructor.call_args.kwargs["connection_factory"] is chat_voice.open_realtime_connection


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError, ConnectionError])
@pytest.mark.parametrize("ready", [False, True])
async def test_realtime_transport_failure_falls_back_only_before_ready(
    monkeypatch,
    caplog,
    failure,
    ready,
):
    caplog.set_level("WARNING", logger="apps.api.chat_voice")
    realtime = SimpleNamespace(ready=ready, run=AsyncMock(side_effect=failure("Transport failed")))
    gateway = SimpleNamespace(run=AsyncMock())
    monkeypatch.setattr(chat_voice, "resolve_realtime_route", AsyncMock(return_value=object()))
    monkeypatch.setattr(chat_voice, "BrowserVoiceSession", Mock(return_value=realtime))
    monkeypatch.setattr(chat_voice, "GatewayVoiceSession", Mock(return_value=gateway))
    scope = ChatAudioScope("entity", "owner", conversation_id="conversation")
    operation = chat_voice.run_voice_session(Socket(), object(), chat_voice.CallStart(token="test"), scope)
    if ready:
        with pytest.raises(failure):
            await operation
        gateway.run.assert_not_awaited()
    else:
        await operation
        gateway.run.assert_awaited_once()
        messages = [record.getMessage() for record in caplog.records]
        assert "Realtime setup failed; trying the shared speech gateway" in messages
        assert all("Vercel realtime setup failed" not in message for message in messages)
    realtime.run.assert_awaited_once()


@pytest.mark.parametrize(
    "failure", [HTTPException(403, "Revoked"), HTTPException(402, "Credits"), asyncio.CancelledError()]
)
async def test_realtime_authorization_and_cancellation_never_trigger_fallback(monkeypatch, failure):
    realtime = SimpleNamespace(ready=False, run=AsyncMock(side_effect=failure))
    gateway = Mock()
    monkeypatch.setattr(chat_voice, "resolve_realtime_route", AsyncMock(return_value=object()))
    monkeypatch.setattr(chat_voice, "BrowserVoiceSession", Mock(return_value=realtime))
    monkeypatch.setattr(chat_voice, "GatewayVoiceSession", gateway)
    with pytest.raises(type(failure)):
        await chat_voice.run_voice_session(
            Socket(), object(), chat_voice.CallStart(token="test"), ChatAudioScope("entity", "owner")
        )
    gateway.assert_not_called()
