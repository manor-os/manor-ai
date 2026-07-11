"""TwilioVoiceSession — one instance per connected call.

Protocol reference: https://www.twilio.com/docs/voice/media-streams/websocket-messages

Receive events from Twilio:
  - connected: initial handshake, no payload needed
  - start:     {streamSid, callSid, customParameters, tracks, mediaFormat}
  - media:     {sequenceNumber, timestamp, chunk, payload (base64 μ-law)}
  - mark:      echo of a mark we sent — useful for knowing when the user
               has actually heard our last utterance
  - stop:      call ended

Send events to Twilio:
  - media:     {event, streamSid, media:{payload:base64 μ-law}}
  - mark:      {event, streamSid, mark:{name}}
  - clear:     flush our buffered outbound (use when interrupting)

Turn-taking logic (what makes this class >50 lines):

  1. Accumulate μ-law from ``media`` events and feed STT.
  2. When STT emits ``is_final`` transcript, kick off an agent run and
     start a 10-second timer.
  3. If the timer fires before the agent finishes, synthesize and stream
     a "Still working on it, one moment" hold message, then keep
     waiting.
  4. When the agent returns, synthesize + stream the real reply.
  5. Reset state and listen for the next user turn.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import WebSocket

from packages.core.services.voice.stt import STTEngine, get_stt_engine, Transcript
from packages.core.services.voice.tts import TTSEngine, get_tts_engine

logger = logging.getLogger(__name__)


_HOLD_AFTER_SECONDS = 10.0
_HOLD_MESSAGE_DEFAULT = "I'm working on it, give me a moment."
_FRAME_MS = 20  # Twilio streams 20 ms frames

# 8 kHz μ-law: 8000 samples/sec × 1 byte/sample × 0.020 sec = 160 bytes
_FRAME_BYTES = 160


@dataclass
class _CallState:
    stream_sid: str = ""
    call_sid: str = ""
    from_number: str = ""
    to_number: str = ""
    channel_config_id: str = ""
    entity_id: str = ""
    custom: Dict[str, str] = field(default_factory=dict)


class TwilioVoiceSession:
    """Runs one Twilio Media Streams websocket. Create, call ``run()``,
    let it block until Twilio sends ``stop`` or the socket closes."""

    def __init__(
        self,
        ws: WebSocket,
        *,
        agent_callable,
        channel_config_id: str,
        entity_id: str,
        stt: Optional[STTEngine] = None,
        tts: Optional[TTSEngine] = None,
        hold_message: str = _HOLD_MESSAGE_DEFAULT,
    ):
        self._ws = ws
        self._agent = agent_callable
        self._stt: STTEngine = stt or get_stt_engine()
        self._tts: TTSEngine = tts or get_tts_engine()
        self._hold_message = hold_message
        self._state = _CallState(
            channel_config_id=channel_config_id,
            entity_id=entity_id,
        )
        self._closed = False
        self._hold_audio_cache: Optional[bytes] = None

    # ── Public entry point ──────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop. Returns when Twilio closes the stream."""
        await self._stt.open()
        stt_task = asyncio.create_task(self._consume_transcripts())
        try:
            await self._receive_loop()
        except Exception:
            logger.exception("Voice session receive loop crashed")
        finally:
            self._closed = True
            await self._stt.close()
            stt_task.cancel()

    # ── Inbound websocket events ────────────────────────────────────────

    async def _receive_loop(self) -> None:
        while not self._closed:
            raw = await self._ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")
            if event == "start":
                self._on_start(msg.get("start", {}))
            elif event == "media":
                payload_b64 = msg.get("media", {}).get("payload")
                if payload_b64:
                    await self._stt.send_audio(base64.b64decode(payload_b64))
            elif event == "mark":
                # mark echo — useful for gate-keeping long utterances
                logger.debug("mark received: %s", msg.get("mark"))
            elif event == "stop":
                logger.info("Twilio stream stopped (streamSid=%s)", self._state.stream_sid)
                self._closed = True
                break
            # connected/dtmf/other events are ignored for now

    def _on_start(self, start: Dict[str, Any]) -> None:
        self._state.stream_sid = start.get("streamSid", "")
        self._state.call_sid = start.get("callSid", "")
        custom = start.get("customParameters") or {}
        self._state.custom = custom
        self._state.from_number = custom.get("from", "")
        self._state.to_number = custom.get("to", "")

    # ── Transcript loop ─────────────────────────────────────────────────

    async def _consume_transcripts(self) -> None:
        """Pull finalised transcripts out of the STT engine and run the
        turn-taking orchestration for each one."""
        async for t in self._stt.receive_transcripts():
            if self._closed:
                return
            if not t.is_final or not t.text:
                # Interim results could drive barge-in; not implemented yet
                continue
            logger.info("Final transcript: %r", t.text)
            try:
                await self._handle_turn(t)
            except Exception:
                logger.exception("Turn handling failed")

    # ── Turn-taking with 10-second hold ─────────────────────────────────

    async def _handle_turn(self, transcript: Transcript) -> None:
        """Run the agent; if it doesn't finish inside _HOLD_AFTER_SECONDS,
        play the hold message and keep waiting."""
        agent_task = asyncio.create_task(self._agent(
            entity_id=self._state.entity_id,
            channel_config_id=self._state.channel_config_id,
            call_sid=self._state.call_sid,
            from_number=self._state.from_number,
            text=transcript.text,
        ))
        try:
            reply = await asyncio.wait_for(
                asyncio.shield(agent_task),
                timeout=_HOLD_AFTER_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.info("Agent > %ss — streaming hold message", _HOLD_AFTER_SECONDS)
            await self._speak(self._hold_message, cache=True)
            try:
                reply = await agent_task
            except Exception:
                logger.exception("Agent crashed after hold")
                await self._speak("Sorry, something went wrong. Try again in a moment.")
                return
        except Exception:
            logger.exception("Agent call failed")
            await self._speak("Sorry, something went wrong. Try again in a moment.")
            return

        if reply:
            await self._speak(reply)

    # ── Outbound audio ──────────────────────────────────────────────────

    async def _speak(self, text: str, *, cache: bool = False) -> None:
        """TTS ``text`` and stream it back to Twilio in 20ms frames."""
        if not self._state.stream_sid:
            logger.warning("Cannot speak — stream not started yet")
            return

        if cache and text == self._hold_message and self._hold_audio_cache:
            mulaw = self._hold_audio_cache
        else:
            try:
                mulaw = await self._tts.synthesize(text)
            except Exception:
                logger.exception("TTS synth failed")
                return
            if cache and text == self._hold_message:
                self._hold_audio_cache = mulaw

        await self._stream_mulaw(mulaw)
        await self._send_mark(f"end-of-utterance-{uuid.uuid4().hex[:8]}")

    async def _stream_mulaw(self, mulaw_bytes: bytes) -> None:
        """Slice into 20ms frames and send as media events."""
        view = memoryview(mulaw_bytes)
        for offset in range(0, len(view), _FRAME_BYTES):
            if self._closed:
                return
            frame = bytes(view[offset:offset + _FRAME_BYTES])
            # Pad the tail to a full frame so Twilio plays cleanly
            if len(frame) < _FRAME_BYTES:
                frame = frame + b"\xff" * (_FRAME_BYTES - len(frame))  # μ-law silence
            await self._ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self._state.stream_sid,
                "media": {"payload": base64.b64encode(frame).decode()},
            }))

    async def _send_mark(self, name: str) -> None:
        await self._ws.send_text(json.dumps({
            "event": "mark",
            "streamSid": self._state.stream_sid,
            "mark": {"name": name},
        }))

    async def clear(self) -> None:
        """Flush queued outbound — use when the user interrupts."""
        if not self._state.stream_sid:
            return
        await self._ws.send_text(json.dumps({
            "event": "clear",
            "streamSid": self._state.stream_sid,
        }))
