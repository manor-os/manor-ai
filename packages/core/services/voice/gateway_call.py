"""Continuous browser calls using the shared STT, Chat and TTS gateways."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import re
import struct
import time
import uuid
import wave
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import WebSocketDisconnect
import webrtcvad

from packages.core.services.voice.browser import MAX_CALL_SECONDS, SAMPLE_RATE
from packages.core.services.voice.latency import (
    VoiceTurnTiming,
    log_voice_latency,
    voice_latency_outcome,
    voice_latency_span,
)
from packages.core.services.voice.realtime import VoiceAgentOutcome
from packages.core.services.voice.profiles import (
    DEFAULT_VOICE_PROFILE,
    VoiceProfile,
    normalize_voice_profile,
)
from packages.core.services.voice.speech_request import SpeechRequestState, speech_request_scope

logger = logging.getLogger(__name__)
# Covers the shared speech provider's bounded retries and format conversion.
# The browser/microphone closes immediately; only accepted audio I/O drains.
AUDIO_SETTLEMENT_TIMEOUT_SECONDS = 12 * 60
# Accepted transcripts can outlive the microphone, but a stuck agent or tool
# must not hold the voice-call lease forever.
TURN_SETTLEMENT_TIMEOUT_SECONDS = 12 * 60
VAD_MIN_RMS = 0.008
VAD_START_MS = 180
VAD_END_SILENCE_MS = 600


class SpeechDetector:
    """Classify speech before admitting a turn or interrupting playback.

    Energy alone admits fans, handling noise and other non-speech as user
    commands. WebRTC VAD runs locally on 20ms frames; audio stays at 24kHz
    for transcription. Duplicating samples gives its supported 48kHz input.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.filter_input = [0.0, 0.0]
        self.filter_output = [0.0, 0.0]
        self.pending = bytearray()
        self._reset_turn()

    def _reset_turn(self):
        # WebRTC's adaptive classifier can keep classifying a sudden noise
        # floor as speech after a long utterance. Start each new turn with a
        # fresh classifier once the preceding silence has completed it.
        self.vad = webrtcvad.Vad(3)
        self.prefix = deque(maxlen=16)
        self.frames: list[bytes] = []
        self.speaking = False
        self.voiced_ms = self.silence_ms = self.total_ms = 0.0

    def feed(self, pcm: bytes) -> list[tuple[bool, bytes | None]]:
        self.pending.extend(pcm)
        events = []
        while len(self.pending) >= 960:
            frame = bytes(self.pending[:960])
            del self.pending[:960]
            event = self._feed_frame(frame)
            if event[0] or event[1] is not None:
                events.append(event)
        return events

    def _feed_frame(self, pcm: bytes) -> tuple[bool, bytes | None]:
        values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        # Remove low-frequency fan/handling rumble from classification only;
        # transcription receives the unmodified, full-band microphone audio.
        alpha = 1 / (1 + math.tau * 300 / SAMPLE_RATE)
        filtered = []
        for value in values:
            current = float(value)
            for stage in range(2):
                output = alpha * (self.filter_output[stage] + current - self.filter_input[stage])
                self.filter_input[stage], self.filter_output[stage] = current, output
                current = output
            filtered.append(max(-32768, min(32767, int(current))))
        rms = math.sqrt(sum(value * value for value in filtered) / len(filtered)) / 32768
        vad_pcm = struct.pack("<960h", *(sample for value in filtered for sample in (value, value)))
        # Still feed silence to the classifier so its state can settle.
        # Browser automatic gain control used to lift room audio until even a
        # distant TV or music bed crossed the old -52 dB floor.  Keep WebRTC's
        # speech-shape decision, but also require a near-field level.  The web
        # client disables AGC so distance remains a useful signal.
        voiced = self.vad.is_speech(vad_pcm, 48000) and rms >= VAD_MIN_RMS
        ms = 20
        started = False
        if not self.speaking:
            self.prefix.append(pcm)
            if not voiced:
                self.voiced_ms = 0
                return False, None
            self.voiced_ms += ms
            if self.voiced_ms < VAD_START_MS:
                return False, None
            self.speaking = started = True
            self.frames = list(self.prefix)
            self.total_ms = sum(map(len, self.frames)) / 48
        else:
            self.frames.append(pcm)
            self.total_ms += ms
        self.silence_ms = 0 if voiced else self.silence_ms + ms
        if self.silence_ms < VAD_END_SILENCE_MS and self.total_ms < 30000:
            return started, None
        audio = b"".join(self.frames)
        self._reset_turn()
        return started, audio


def pcm_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def speech_chunks(text: str):
    # Speak the first sentence promptly, even when the whole reply fits in
    # one batch. Group later sentences to avoid a provider call per sentence.
    pending = ""
    first = True
    for sentence in re.split(r"(?<=[!?。！？；\n])|(?<=[.;])(?=\s|$)", text):
        if pending and len(pending) + len(sentence) > 240:
            yield pending
            pending = ""
        pending += sentence
        limit = 80 if first else 350
        while len(pending) > limit:
            yield pending[:limit]
            pending = pending[limit:]
            first, limit = False, 350
        if first and pending.strip():
            yield pending
            pending, first = "", False
    if pending.strip():
        yield pending


class GatewayVoiceSession:
    def __init__(
        self,
        ws,
        *,
        conversation_id: str,
        check_access: Callable[[], Awaitable[None]],
        prepare: Callable[[], Awaitable[dict[str, str] | None]],
        transcribe: Callable[[bytes], Awaitable[str]],
        speak: Callable[[str, VoiceProfile], Awaitable[bytes]],
        agent: Callable[[str], Awaitable[VoiceAgentOutcome]],
        on_ready: Callable[[], None] | None = None,
        voice: VoiceProfile = DEFAULT_VOICE_PROFILE,
    ):
        self.ws, self.conversation_id = ws, conversation_id
        self.call_id = uuid.uuid4().hex[:12]
        self.check_access, self.prepare = check_access, prepare
        self.transcribe, self.speak, self.agent = transcribe, speak, agent
        self.on_ready = on_ready
        self.voice = normalize_voice_profile(voice)
        self.detector = SpeechDetector()
        self.inputs: asyncio.Queue = asyncio.Queue(maxsize=4)
        # Accepted transcripts are small and must not disappear after the UI has
        # shown them. The call duration bounds this queue; raw audio remains
        # bounded separately above to protect memory under capture overload.
        self.turns: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self.turn_available = asyncio.Event()
        self.input_id = 0
        self.input_resolved = asyncio.Event()
        self.input_resolved.set()
        self.generation = 0
        self.muted = self.closed = False
        self.started = time.monotonic()
        self.input_bytes = 0
        self.clip_id: str | None = None
        self.clip_done = asyncio.Event()
        self.clip_outcome: str | None = None
        self.pending_audio: dict[asyncio.Task, SpeechRequestState] = {}
        self.input_timings: dict[int, VoiceTurnTiming] = {}
        self.turn_timings: dict[int, VoiceTurnTiming] = {}
        self.stt_model = self.tts_model = "-"

    def log_latency(
        self,
        stage: str,
        started_at: float,
        *,
        generation: int | str | None = None,
        model: str = "-",
        outcome: str = "ok",
        segment: int = 0,
        ended_at: float | None = None,
    ) -> int:
        return log_voice_latency(
            logger,
            transport="shared_gateway",
            provider="gateway",
            model=model,
            call_id=self.call_id,
            turn=generation,
            stage=stage,
            started_at=started_at,
            outcome=outcome,
            segment=segment,
            ended_at=ended_at,
        )

    def finish_turn_timing(
        self,
        generation: int,
        *,
        outcome: str,
        ended_at: float | None = None,
    ) -> None:
        timing = self.turn_timings.pop(generation, None)
        if timing and timing.input_committed_at is not None:
            self.log_latency(
                "turn_complete",
                timing.input_committed_at,
                generation=timing.turn_id if timing.turn_id is not None else generation,
                outcome=outcome,
                ended_at=ended_at,
            )

    def finish_input_timing(
        self,
        input_id: int,
        *,
        outcome: str,
        ended_at: float | None = None,
    ) -> None:
        timing = self.input_timings.pop(input_id, None)
        if timing is None:
            return
        if timing.input_committed_at is not None:
            self.log_latency(
                "turn_complete",
                timing.input_committed_at,
                generation=timing.turn_id if timing.turn_id is not None else input_id,
                outcome=outcome,
                ended_at=ended_at,
            )
        elif timing.speech_started_at is not None:
            self.log_latency(
                "speech_capture",
                timing.speech_started_at,
                generation=timing.turn_id if timing.turn_id is not None else input_id,
                outcome=outcome,
                ended_at=ended_at,
            )

    def finish_clip(self, outcome: str) -> None:
        """Release one playback wait without overwriting an earlier browser ack."""

        if self.clip_done.is_set():
            return
        self.clip_outcome = outcome
        self.clip_done.set()

    async def wait_for_playback(
        self,
        *,
        generation: int | str,
        segment: int,
    ) -> str:
        started_at = time.monotonic()
        try:
            await asyncio.wait_for(self.clip_done.wait(), timeout=90)
        except BaseException as error:
            self.log_latency(
                "playback_wait",
                started_at,
                generation=generation,
                segment=segment,
                outcome=voice_latency_outcome(error),
            )
            raise
        outcome = self.clip_outcome or "error"
        self.log_latency(
            "playback_wait",
            started_at,
            generation=generation,
            segment=segment,
            outcome="ok" if outcome == "played" else outcome,
        )
        return outcome

    def queue_input(self, item: tuple[int, bytes | None]) -> None:
        """Keep capture live under load, preferring the caller's newest turn."""

        if self.inputs.full():
            try:
                dropped_id, _ = self.inputs.get_nowait()
                self.inputs.task_done()
                self.finish_input_timing(dropped_id, outcome="dropped")
                logger.warning(
                    "Voice transcription queue full call=%s dropped_input=%s",
                    self.call_id,
                    dropped_id,
                )
            except asyncio.QueueEmpty:
                pass
        self.inputs.put_nowait(item)

    def queue_turn(self, item: tuple[int, str]) -> None:
        """Preserve every accepted transcript until Chat handles it."""

        self.turns.put_nowait(item)
        self.turn_available.set()

    async def audio_request(self, operation):
        state = SpeechRequestState()
        with speech_request_scope(state):
            pending = asyncio.create_task(operation)
        self.pending_audio[pending] = state
        try:
            # Cancelling the call must not cancel the provider response and
            # the usage commit owned by the shared speech callback.
            return await asyncio.shield(pending)
        finally:
            if pending.done():
                self.pending_audio.pop(pending, None)

    async def settle_pending_audio(self):
        pending = list(self.pending_audio)
        if not pending:
            return
        settlement = asyncio.gather(*pending, return_exceptions=True)
        try:
            # Server-side cancellation must also stop browser capture now;
            # the accepted provider response may take longer to settle.
            async with asyncio.timeout(AUDIO_SETTLEMENT_TIMEOUT_SECONDS):
                try:
                    await self.ws.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
                results = await asyncio.shield(settlement)
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("Voice audio settlement failed (%s)", type(result).__name__)
        except TimeoutError:
            logger.error("Voice audio settlement exceeded its deadline")
        finally:
            for task in pending:
                task.cancel()
            await settlement
            self.pending_audio.clear()

    async def run(self):
        run_started_at = time.monotonic()
        with voice_latency_span(self.log_latency, "access_preflight"):
            await self.check_access()
        with voice_latency_span(self.log_latency, "provider_prepare"):
            models = await self.prepare()
        if isinstance(models, dict):
            self.stt_model = models.get("stt", "-")
            self.tts_model = models.get("voice", "-")
        try:
            await self.ws.send_json(
                {
                    "type": "ready",
                    "conversation_id": self.conversation_id,
                    "generation": self.generation,
                    "transport_mode": "turn_based",
                    # Shared STT/Chat/TTS cannot use the provider's native echo
                    # reference. Browser capture pauses during playback and the
                    # user can explicitly stop the reply before speaking.
                    "duplex_mode": "push_to_interrupt",
                    "voice": self.voice,
                    "voice_locked": False,
                }
            )
        except BaseException as error:
            self.log_latency(
                "call_ready",
                run_started_at,
                outcome=voice_latency_outcome(error),
            )
            raise
        self.log_latency("call_ready", run_started_at)
        if self.on_ready:
            self.on_ready()
        tasks = [asyncio.create_task(fn()) for fn in (self.receive, self.transcribe_inputs, self.run_turns, self.monitor)]
        try:
            async with asyncio.timeout(MAX_CALL_SECONDS):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            self.closed = True
            self.generation += 1
            self.detector.reset()
            self.input_resolved.set()
            self.finish_clip("closed")
            self.turn_available.set()
            try:
                await self.ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
            for pending, state in self.pending_audio.items():
                state.stopped = True
                if not state.submitted:
                    pending.cancel()
            turn_worker = tasks[2]
            for task in tasks:
                if task is not turn_worker:
                    task.cancel()
            cleanup = asyncio.create_task(self.finish(tasks))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A lease failure or server cancellation can arrive while a
                # browser hangup is already draining the accepted request.
                await asyncio.shield(cleanup)
                raise

    async def finish(self, tasks):
        settlement = asyncio.gather(*tasks, return_exceptions=True)
        timed_out = False
        try:
            async with asyncio.timeout(TURN_SETTLEMENT_TIMEOUT_SECONDS):
                await asyncio.shield(settlement)
        except TimeoutError:
            timed_out = True
            logger.error("Voice turn settlement exceeded its deadline")
            for task in tasks:
                task.cancel()
            await settlement
        while not self.turns.empty():
            self.turns.get_nowait()
        while not self.inputs.empty():
            self.inputs.get_nowait()
        await self.settle_pending_audio()
        final_outcome = "timeout" if timed_out else "closed"
        ended_at = time.monotonic()
        for input_id in list(self.input_timings):
            self.finish_input_timing(
                input_id,
                outcome=final_outcome,
                ended_at=ended_at,
            )
        for generation in list(self.turn_timings):
            self.finish_turn_timing(
                generation,
                outcome=final_outcome,
                ended_at=ended_at,
            )

    async def monitor(self):
        while True:
            await asyncio.sleep(20)
            await self.check_access()
            await self.ws.send_json({"type": "ping"})

    async def receive(self):
        while True:
            raw = await asyncio.wait_for(self.ws.receive_text(), timeout=35)
            if len(raw) > 16000:
                raise ValueError("Voice frame too large")
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "end":
                return
            if kind in {"pong", "played"}:
                continue
            if kind == "voice":
                self.voice = normalize_voice_profile(message.get("voice"))
                await self.ws.send_json(
                    {"type": "voice", "voice": self.voice, "locked": False}
                )
                continue
            if kind == "clip_done":
                if message.get("item_id") == self.clip_id:
                    logger.info("Voice clip acknowledged call=%s", self.call_id)
                    self.finish_clip("played")
                continue
            if kind == "stop_reply":
                if self.clip_id is not None and message.get("item_id") in {None, self.clip_id}:
                    self.generation += 1
                    self.finish_clip("interrupted")
                    self.input_resolved.set()
                    logger.info("Voice reply manually interrupted call=%s", self.call_id)
                    await self.ws.send_json({"type": "interrupt", "generation": self.generation})
                    await self.ws.send_json({"type": "listening", "generation": self.generation})
                continue
            if kind == "mute" and isinstance(message.get("muted"), bool):
                self.muted = message["muted"]
                # Muting discards a partial utterance, not a completed one
                # already being transcribed.
                if self.detector.speaking:
                    self.queue_input((self.input_id, None))
                self.detector.reset()
                continue
            if kind != "audio" or not isinstance(message.get("audio"), str):
                raise ValueError("Unsupported voice message")
            pcm = base64.b64decode(message["audio"], validate=True)
            if not pcm or len(pcm) % 2 or len(pcm) > 9600:
                raise ValueError("Invalid PCM audio frame")
            self.input_bytes += len(pcm)
            if self.input_bytes > (time.monotonic() - self.started + 3) * SAMPLE_RATE * 2:
                raise ValueError("Audio arrived faster than realtime")
            if self.muted:
                continue
            for started, utterance in self.detector.feed(pcm):
                if started:
                    self.input_id += 1
                    self.input_resolved.clear()
                    self.input_timings[self.input_id] = VoiceTurnTiming(
                        turn_id=self.input_id,
                        speech_started_at=time.monotonic()
                    )
                    logger.info("Voice input started call=%s input=%s", self.call_id, self.input_id)
                    await self.ws.send_json({"type": "input_started", "generation": self.generation})
                if utterance:
                    now = time.monotonic()
                    timing = self.input_timings.setdefault(
                        self.input_id,
                        VoiceTurnTiming(turn_id=self.input_id),
                    )
                    timing.input_committed_at = now
                    if timing.speech_started_at is not None:
                        self.log_latency(
                            "speech_capture",
                            timing.speech_started_at,
                            generation=self.input_id,
                            ended_at=now,
                        )
                    self.queue_input((self.input_id, utterance))
                    await self.ws.send_json({"type": "transcribing", "generation": self.generation})

    async def transcribe_inputs(self):
        # Recognition must continue while Chat/TTS is working or playback is
        # paused; otherwise an empty barge-in can never release that pause.
        while True:
            input_id, pcm = await self.inputs.get()
            timing = self.input_timings.setdefault(input_id, VoiceTurnTiming(turn_id=input_id))
            try:
                with voice_latency_span(
                    self.log_latency,
                    "stt_access_check",
                    generation=input_id,
                    model=self.stt_model,
                ):
                    await self.check_access()
            except BaseException as error:
                self.finish_input_timing(
                    input_id,
                    outcome=voice_latency_outcome(error),
                )
                raise
            transcription_started_at = time.monotonic()
            if timing.input_committed_at is None:
                timing.input_committed_at = transcription_started_at
            try:
                text = (
                    (await self.audio_request(self.transcribe(pcm_wav(pcm)))).strip()
                    if pcm
                    else ""
                )
            except BaseException as error:
                self.log_latency(
                    "transcription",
                    transcription_started_at,
                    generation=input_id,
                    model=self.stt_model,
                    outcome=voice_latency_outcome(error),
                )
                self.finish_input_timing(
                    input_id,
                    outcome=voice_latency_outcome(error),
                )
                raise
            timing.transcription_completed_at = time.monotonic()
            self.log_latency(
                "transcription",
                transcription_started_at,
                generation=input_id,
                model=self.stt_model,
                outcome="empty" if not text else "ok",
                ended_at=timing.transcription_completed_at,
            )
            logger.info("Voice input resolved call=%s input=%s empty=%s", self.call_id, input_id, not bool(text))
            if not text:
                self.finish_input_timing(input_id, outcome="empty")
                if input_id == self.input_id:
                    self.input_resolved.set()
                    await self.ws.send_json({"type": "input_empty", "generation": self.generation})
                continue
            try:
                with voice_latency_span(
                    self.log_latency,
                    "turn_admission",
                    generation=input_id,
                ):
                    await self.check_access()
            except BaseException as error:
                self.finish_input_timing(
                    input_id,
                    outcome=voice_latency_outcome(error),
                )
                raise
            self.generation += 1
            generation = self.generation
            timing.agent_queued_at = time.monotonic()
            self.turn_timings[generation] = timing
            self.input_timings.pop(input_id, None)
            self.finish_clip("interrupted")
            if input_id == self.input_id:
                self.input_resolved.set()
            await self.ws.send_json({"type": "interrupt", "generation": self.generation})
            # Admission happens before exposing the transcript to the UI. Once
            # visible, a hangup must drain this turn through Chat rather than
            # leave a message that exists only inside the call dialog.
            self.queue_turn((generation, text))
            await self.ws.send_json(
                {"type": "transcript", "role": "user", "text": text, "generation": generation}
            )
            await self.ws.send_json({"type": "thinking", "generation": generation})
            if not self.input_resolved.is_set():
                await self.ws.send_json({"type": "input_started", "generation": generation})

    async def run_turns(self):
        while True:
            if self.closed and self.turns.empty():
                return
            if self.turns.empty():
                await self.turn_available.wait()
                if self.closed and self.turns.empty():
                    return
            try:
                generation, text = self.turns.get_nowait()
            except asyncio.QueueEmpty:
                self.turn_available.clear()
                continue
            if self.turns.empty():
                self.turn_available.clear()
            timing = self.turn_timings.setdefault(
                generation,
                VoiceTurnTiming(turn_id=generation, agent_queued_at=time.monotonic()),
            )
            trace_turn = timing.turn_id if timing.turn_id is not None else generation
            try:
                with voice_latency_span(
                    self.log_latency,
                    "agent_access_check",
                    generation=trace_turn,
                ):
                    await self.check_access()
                # Serialize genuine turns. Barge-in suppresses stale speech,
                # but never cancels an action admitted by Chat. A hangup also
                # drains transcripts that were already exposed to the caller.
                agent_started_at = time.monotonic()
                if timing.agent_queued_at is not None:
                    self.log_latency(
                        "agent_queue",
                        timing.agent_queued_at,
                        generation=trace_turn,
                        ended_at=agent_started_at,
                    )
                try:
                    outcome = await self.agent(text)
                except BaseException as error:
                    self.log_latency(
                        "agent",
                        agent_started_at,
                        generation=trace_turn,
                        outcome=voice_latency_outcome(error),
                    )
                    raise
                timing.agent_completed_at = time.monotonic()
                self.log_latency(
                    "agent",
                    agent_started_at,
                    generation=trace_turn,
                    outcome=outcome.status,
                    ended_at=timing.agent_completed_at,
                )
            except BaseException as error:
                self.finish_turn_timing(
                    generation,
                    outcome=voice_latency_outcome(error),
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
                if not self.closed:
                    raise
                logger.exception("Accepted voice turn failed during hangup settlement")
                continue
            logger.info("Voice reply ready call=%s turn=%s current=%s", self.call_id, generation, self.generation)
            if outcome.conversation_id != self.conversation_id:
                self.finish_turn_timing(generation, outcome="error")
                raise RuntimeError("Voice conversation changed during call")
            if self.closed:
                self.finish_turn_timing(generation, outcome="closed")
                continue
            try:
                await self.ws.send_json(
                    {
                        "type": "turn",
                        "conversation_id": self.conversation_id,
                        "text": outcome.spoken_reply,
                        "status": outcome.status,
                        "generation": generation,
                    }
                )
                if self.closed:
                    self.finish_turn_timing(generation, outcome="closed")
                    continue
                # Keep one speaker for the entire assistant reply. A selection
                # during synthesis or playback applies to the next reply.
                turn_voice = self.voice
                chunks = list(speech_chunks(outcome.spoken_reply))
                first_audio_sent = False
                for chunk_index, chunk in enumerate(chunks, start=1):
                    await self.input_resolved.wait()
                    if generation != self.generation:
                        break
                    with voice_latency_span(
                        self.log_latency,
                        "tts_access_check",
                        generation=trace_turn,
                        model=self.tts_model,
                        segment=chunk_index,
                    ):
                        await self.check_access()
                    # The shared callback settles usage even when the user
                    # interrupts while the provider generates this chunk.
                    if generation != self.generation:
                        break
                    await self.ws.send_json({"type": "synthesizing", "generation": generation})
                    if generation != self.generation:
                        break
                    tts_started_at = time.monotonic()
                    try:
                        audio = await self.audio_request(self.speak(chunk, turn_voice))
                    except BaseException as error:
                        self.log_latency(
                            "tts_chunk",
                            tts_started_at,
                            generation=trace_turn,
                            model=self.tts_model,
                            outcome=voice_latency_outcome(error),
                            segment=chunk_index,
                        )
                        raise
                    tts_completed_at = time.monotonic()
                    self.log_latency(
                        "tts_chunk",
                        tts_started_at,
                        generation=trace_turn,
                        model=self.tts_model,
                        segment=chunk_index,
                        ended_at=tts_completed_at,
                    )
                    await self.input_resolved.wait()
                    if generation != self.generation:
                        break
                    if len(audio) > 4 * 1024 * 1024:
                        raise ValueError("Voice response too large")
                    # Prepare the next chunk while the previous one plays. Keep
                    # at most one clip in the browser and one prepared here.
                    if self.clip_id is not None:
                        await self.wait_for_playback(
                            generation=trace_turn,
                            segment=chunk_index - 1,
                        )
                        await self.input_resolved.wait()
                        if generation != self.generation:
                            break
                    item_id = str(uuid.uuid4())
                    self.clip_id = item_id
                    self.clip_outcome = None
                    self.clip_done.clear()
                    await self.ws.send_json(
                        {"type": "caption", "item_id": item_id, "delta": chunk, "generation": generation}
                    )
                    if generation != self.generation:
                        self.clip_id = None
                        break
                    await self.ws.send_json(
                        {
                            "type": "audio_clip",
                            "item_id": item_id,
                            "audio": base64.b64encode(audio).decode("ascii"),
                            "generation": generation,
                        }
                    )
                    audio_sent_at = time.monotonic()
                    if not first_audio_sent:
                        first_audio_sent = True
                        timing.first_audio_at = audio_sent_at
                        self.log_latency(
                            "tts_first_audio",
                            tts_started_at,
                            generation=trace_turn,
                            model=self.tts_model,
                            segment=chunk_index,
                            ended_at=audio_sent_at,
                        )
                        if timing.input_committed_at is not None:
                            self.log_latency(
                                "turn_first_audio",
                                timing.input_committed_at,
                                generation=trace_turn,
                                ended_at=audio_sent_at,
                            )
                    if chunk_index == len(chunks) and timing.input_committed_at is not None:
                        self.log_latency(
                            "audio_delivery_complete",
                            timing.input_committed_at,
                            generation=trace_turn,
                            segment=chunk_index,
                            ended_at=audio_sent_at,
                        )
                    logger.info("Voice clip sent call=%s turn=%s bytes=%s", self.call_id, generation, len(audio))
                if not chunks and generation == self.generation:
                    self.finish_turn_timing(
                        generation,
                        outcome=outcome.status,
                        ended_at=timing.agent_completed_at,
                    )
                # The final clip must finish before the next Chat turn starts.
                # Interruption releases this wait without replaying the action.
                if self.clip_id is not None and generation == self.generation:
                    await self.wait_for_playback(
                        generation=trace_turn,
                        segment=len(chunks),
                    )
                self.clip_id = None
                if self.closed:
                    self.finish_turn_timing(generation, outcome="closed")
                elif generation == self.generation:
                    self.finish_turn_timing(
                        generation,
                        outcome=outcome.status,
                    )
                    await self.ws.send_json({"type": "listening", "generation": generation})
                else:
                    self.finish_turn_timing(generation, outcome="interrupted")
            except BaseException as error:
                self.finish_turn_timing(
                    generation,
                    outcome=(
                        "closed"
                        if self.closed and not isinstance(error, asyncio.CancelledError)
                        else voice_latency_outcome(error)
                    ),
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
                if not self.closed:
                    raise
