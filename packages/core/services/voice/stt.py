"""Speech-to-text engines for Twilio Media Streams.

Twilio sends audio as μ-law 8 kHz, 20 ms frames, base64-encoded. The
engine receives raw μ-law bytes and emits a stream of transcript events:

    {type: "partial" | "final", text: str, confidence: float | None}

Reference implementation uses Deepgram's streaming WebSocket API (the
most common pairing for Twilio — configurable mulaw encoding, sub-second
latency). Swap by providing a different ``STTEngine`` subclass and
pointing ``get_stt_engine`` at it via the ``STT_PROVIDER`` env var.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: Optional[float] = None


class STTEngine(abc.ABC):
    """Streaming STT — open a session per call, feed μ-law audio chunks,
    pull transcripts out."""

    @abc.abstractmethod
    async def open(self) -> None:
        ...

    @abc.abstractmethod
    async def send_audio(self, mulaw_bytes: bytes) -> None:
        ...

    @abc.abstractmethod
    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield partial and final transcripts as they arrive."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...


# ── Deepgram reference implementation ──────────────────────────────────────

class DeepgramSTT(STTEngine):
    """Deepgram streaming STT over a websocket.

    Audio format fields match what Twilio sends — Deepgram accepts
    ``encoding=mulaw&sample_rate=8000`` natively so we don't have to
    resample.
    """

    _WS_URL = (
        "wss://api.deepgram.com/v1/listen"
        "?encoding=mulaw&sample_rate=8000&channels=1"
        "&model={model}&language={lang}"
        "&interim_results=true&punctuate=true&endpointing=400"
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "nova-2-phonecall",
        language: str = "en-US",
    ):
        self._api_key = api_key or os.getenv("DEEPGRAM_API_KEY", "")
        self._model = model
        self._language = language
        self._ws = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

    async def open(self) -> None:
        if not self._api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set — can't open STT stream")
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError("websockets package required — pip install websockets") from e

        url = self._WS_URL.format(model=self._model, lang=self._language)
        headers = [("Authorization", f"Token {self._api_key}")]
        self._ws = await websockets.connect(url, additional_headers=headers)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, mulaw_bytes: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(mulaw_bytes)
        except Exception:
            logger.debug("STT send_audio failed (connection gone)")

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if self._ws is not None:
            try:
                # Send Deepgram's close frame
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        await self._queue.put(None)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                alt = (msg.get("channel") or {}).get("alternatives") or []
                if not alt:
                    continue
                text = (alt[0].get("transcript") or "").strip()
                if not text:
                    continue
                await self._queue.put(Transcript(
                    text=text,
                    is_final=bool(msg.get("is_final")),
                    confidence=alt[0].get("confidence"),
                ))
        except Exception:
            logger.debug("STT read loop ended", exc_info=True)
        finally:
            await self._queue.put(None)


# ── Factory ─────────────────────────────────────────────────────────────────

_FACTORY: dict[str, Callable[[], STTEngine]] = {
    "deepgram": lambda: DeepgramSTT(),
}


def register_stt_engine(name: str, factory: Callable[[], STTEngine]) -> None:
    _FACTORY[name.lower()] = factory


def get_stt_engine(name: Optional[str] = None) -> STTEngine:
    provider = (name or os.getenv("STT_PROVIDER", "deepgram")).lower()
    factory = _FACTORY.get(provider)
    if not factory:
        raise RuntimeError(
            f"No STT engine registered under '{provider}'. "
            f"Available: {list(_FACTORY)}"
        )
    return factory()
