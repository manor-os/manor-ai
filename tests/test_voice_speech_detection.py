"""Real speech/noise input must gate both commands and playback interruption."""

import asyncio
import base64
import math
from pathlib import Path
import random
import struct
import wave

import pytest

from packages.core.services.voice.gateway_call import SpeechDetector
from packages.core.services.voice.realtime import VoiceAgentOutcome
from tests.test_browser_voice import eventually
from tests.test_gateway_voice import gateway_session, pcm, utterance


def noise(kind, seconds=2):
    rng = random.Random(25)
    values = []
    for sample in range(24000 * seconds):
        value = (
            rng.randint(-1200, 1200)
            if kind == "white"
            else (1000 if kind == "dc" else int(1500 * math.sin(sample * math.tau * 80 / 24000)))
        )
        values.append(value)
    return struct.pack(f"<{len(values)}h", *values)


@pytest.mark.parametrize("kind", ["white", "dc", "rumble"])
def test_non_speech_never_starts_a_command(kind):
    detector = SpeechDetector()
    audio = noise(kind, 8) + bytes(48000)
    assert not [event for pos in range(0, len(audio), 1920) for event in detector.feed(audio[pos : pos + 1920])]


@pytest.mark.parametrize("packet_bytes", [1920, 640, 9600])
def test_mandarin_with_natural_pause_is_one_complete_turn(packet_bytes):
    with wave.open(str(Path(__file__).parent / "fixtures/voice/zh-call.wav")) as wav:
        speech = wav.readframes(wav.getnframes())
    audio = bytes(48000) + speech + bytes(48000)
    detector = SpeechDetector()
    events = [
        event for pos in range(0, len(audio), packet_bytes) for event in detector.feed(audio[pos : pos + packet_bytes])
    ]
    assert sum(start for start, _ in events) == 1
    turns = [pcm for _, pcm in events if pcm]
    assert len(turns) == 1
    # Real speech at the start and end is retained, including the brief pause
    # between sentences that the old energy detector split into two requests.
    voiced_samples = [i for i, value in enumerate(struct.unpack(f"<{len(speech) // 2}h", speech)) if abs(value) > 300]
    assert speech[voiced_samples[0] * 2 : (voiced_samples[-1] + 1) * 2] in turns[0]


def test_distant_speech_shaped_background_does_not_start_a_turn():
    with wave.open(str(Path(__file__).parent / "fixtures/voice/zh-call.wav")) as wav:
        speech = wav.readframes(wav.getnframes())
    samples = struct.unpack(f"<{len(speech) // 2}h", speech)
    distant = struct.pack(f"<{len(samples)}h", *(int(sample * 0.2) for sample in samples))
    detector = SpeechDetector()
    audio = bytes(48000) + distant + bytes(48000)
    assert not [event for pos in range(0, len(audio), 1920) for event in detector.feed(audio[pos : pos + 1920])]


async def test_background_noise_during_thinking_does_not_discard_the_spoken_reply():
    call, ws = gateway_session()
    call.started -= 20
    entered, release = asyncio.Event(), asyncio.Event()

    async def agent(_):
        entered.set()
        await release.wait()
        return VoiceAgentOutcome(status="ok", spoken_reply="I can hear you.", conversation_id="conversation")

    call.agent.side_effect = agent
    task = asyncio.create_task(call.run())
    await eventually(lambda: bool(ws.events))
    await utterance(ws)
    await asyncio.wait_for(entered.wait(), 2)
    original_generation = call.generation
    audio = noise("white") + bytes(48000)
    for pos in range(0, len(audio), 1920):
        await ws.incoming.put({"type": "audio", "audio": base64.b64encode(audio[pos : pos + 1920]).decode()})
    await eventually(lambda: ws.incoming.empty())
    assert call.generation == original_generation
    release.set()
    await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))
    call.transcribe.assert_awaited_once()
    call.speak.assert_awaited_once_with("I can hear you.", "warm")
    await ws.incoming.put({"type": "end"})
    await asyncio.wait_for(task, 2)


async def test_empty_transcription_after_a_tentative_interrupt_keeps_the_pending_reply():
    call, ws = gateway_session()
    entered, release = asyncio.Event(), asyncio.Event()
    call.transcribe.side_effect = ["你好", ""]

    async def agent(_):
        entered.set()
        await release.wait()
        return VoiceAgentOutcome(status="ok", spoken_reply="你好，我在。", conversation_id="conversation")

    call.agent.side_effect = agent
    task = asyncio.create_task(call.run())
    try:
        await eventually(lambda: bool(ws.events))
        await utterance(ws)
        await asyncio.wait_for(entered.wait(), 2)
        # Speech detection is tentative: a breath or handling noise can pass
        # VAD even though the transcription service returns no words.
        await utterance(ws)
        release.set()
        await eventually(lambda: call.transcribe.await_count == 2)
        await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
        call.agent.assert_awaited_once_with("你好")
    finally:
        await ws.incoming.put({"type": "end"})
        await asyncio.wait_for(task, 2)


async def test_empty_transcription_resumes_a_paused_clip_without_regenerating_it():
    call, ws = gateway_session()
    call.transcribe.side_effect = ["你好", ""]
    task = asyncio.create_task(call.run())
    try:
        await eventually(lambda: bool(ws.events))
        await utterance(ws)
        await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
        clip = next(e for e in ws.events if e["type"] == "audio_clip")
        await utterance(ws)
        await eventually(lambda: any(e["type"] == "input_empty" for e in ws.events))
        assert call.generation == 1
        call.speak.assert_awaited_once()
        call.agent.assert_awaited_once()
        await ws.incoming.put({"type": "clip_done", "item_id": clip["item_id"]})
        await eventually(lambda: ws.events[-1]["type"] == "listening")
    finally:
        await ws.incoming.put({"type": "end"})
        await asyncio.wait_for(task, 2)


async def test_muting_a_partial_utterance_releases_paused_playback():
    call, ws = gateway_session()
    task = asyncio.create_task(call.run())
    try:
        await eventually(lambda: bool(ws.events))
        await utterance(ws)
        await eventually(lambda: any(e["type"] == "audio_clip" for e in ws.events))
        for _ in range(5):
            await ws.incoming.put({"type": "audio", "audio": base64.b64encode(pcm()).decode()})
        await eventually(lambda: call.input_id == 2)
        assert not call.input_resolved.is_set()
        await ws.incoming.put({"type": "mute", "muted": True})
        await eventually(lambda: any(e["type"] == "input_empty" for e in ws.events))
        assert call.input_resolved.is_set() and call.generation == 1
        call.transcribe.assert_awaited_once()
    finally:
        await ws.incoming.put({"type": "end"})
        await asyncio.wait_for(task, 2)


def test_noise_after_a_full_spoken_turn_does_not_inherit_speech_classification():
    with wave.open(str(Path(__file__).parent / "fixtures/voice/zh-call.wav")) as wav:
        speech = wav.readframes(wav.getnframes())
    detector = SpeechDetector()
    audio = speech + bytes(48000) + noise("white", 8) + bytes(48000)
    events = [event for pos in range(0, len(audio), 1920) for event in detector.feed(audio[pos : pos + 1920])]
    assert sum(start for start, _ in events) == 1
    assert len([pcm for _, pcm in events if pcm]) == 1
