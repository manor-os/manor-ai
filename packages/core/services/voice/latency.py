"""Small, privacy-safe timing primitives shared by browser voice transports."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class VoiceTurnTiming:
    turn_id: int | str | None = None
    speech_started_at: float | None = None
    input_committed_at: float | None = None
    transcription_completed_at: float | None = None
    agent_queued_at: float | None = None
    agent_completed_at: float | None = None
    tts_queued_at: float | None = None
    tts_started_at: float | None = None
    first_audio_at: float | None = None
    tts_attempt: int = 0


def voice_latency_outcome(error: BaseException) -> str:
    """Return a stable aggregation token for a failed timing span."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            return "cancelled"
        if isinstance(current, TimeoutError):
            return "timeout"
        current = current.__cause__ or current.__context__
    return "error"


@contextmanager
def voice_latency_span(
    emit: Callable[..., int],
    stage: str,
    **fields: object,
) -> Iterator[None]:
    """Log both successful and failed awaited work without swallowing errors."""

    started_at = time.monotonic()
    try:
        yield
    except BaseException as error:
        emit(
            stage,
            started_at,
            outcome=voice_latency_outcome(error),
            **fields,
        )
        raise
    else:
        emit(stage, started_at, **fields)


@asynccontextmanager
async def voice_latency_async_context(
    manager: Any,
    emit: Callable[..., int],
    stage: str,
    **fields: object,
) -> AsyncIterator[Any]:
    """Time entry into an async context while leaving its body unchanged."""

    started_at = time.monotonic()
    entered = False
    try:
        async with manager as value:
            entered = True
            emit(stage, started_at, **fields)
            yield value
    except BaseException as error:
        if not entered:
            emit(
                stage,
                started_at,
                outcome=voice_latency_outcome(error),
                **fields,
            )
        raise


def log_voice_latency(
    logger: logging.Logger,
    *,
    transport: str,
    provider: str,
    model: str,
    call_id: str,
    stage: str,
    started_at: float,
    turn: int | str | None = None,
    outcome: str = "ok",
    segment: int = 0,
    ended_at: float | None = None,
) -> int:
    """Emit one aggregation-friendly timing without conversation content or actor IDs."""

    def token(value: object) -> str:
        return re.sub(r"[^A-Za-z0-9._:/-]", "_", str(value))[:160] or "-"

    finished_at = time.monotonic() if ended_at is None else ended_at
    latency_ms = max(0, round((finished_at - started_at) * 1000))
    logger.info(
        "voice_latency transport=%s provider=%s model=%s call=%s turn=%s "
        "stage=%s segment=%s latency_ms=%s outcome=%s",
        token(transport),
        token(provider),
        token(model),
        token(call_id),
        token("-" if turn is None else turn),
        token(stage),
        segment,
        latency_ms,
        token(outcome),
    )
    return latency_ms
