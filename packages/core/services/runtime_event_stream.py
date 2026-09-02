"""Replayable Redis Stream transport for durable Runtime SSE events."""
from __future__ import annotations

from typing import Any

from redis.exceptions import TimeoutError as RedisTimeoutError

from packages.core.config import get_settings


def runtime_event_stream_key(run_id: str) -> str:
    return f"manor:runtime:events:{run_id}"


def parse_sse_frame(frame: str) -> tuple[str, str]:
    event = "message"
    data_lines: list[str] = []
    for line in str(frame or "").splitlines():
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return event, "\n".join(data_lines)


def sse_frame_with_id(event: str, payload: str, event_id: str) -> str:
    data_lines = str(payload).splitlines() or [""]
    data = "\n".join(f"data: {line}" for line in data_lines)
    return f"id: {event_id}\nevent: {event}\n{data}\n\n"


async def append_runtime_sse_event(
    run_id: str,
    frame: str,
    *,
    redis_client: Any | None = None,
) -> str | None:
    event, payload = parse_sse_frame(frame)
    if not payload:
        return None
    if redis_client is None:
        from packages.core.cache import _get_redis

        redis_client = await _get_redis()
    if redis_client is None:
        return None
    settings = get_settings()
    key = runtime_event_stream_key(run_id)
    event_id = await redis_client.xadd(
        key,
        {"event": event, "payload": payload},
        maxlen=settings.RUNTIME_EVENT_STREAM_MAX_EVENTS,
        approximate=True,
    )
    await redis_client.expire(key, settings.RUNTIME_EVENT_STREAM_TTL_SECONDS)
    return str(event_id)


async def read_runtime_sse_events(
    run_id: str,
    *,
    after_id: str = "0-0",
    block_ms: int | None = 4000,
    count: int = 100,
    redis_client: Any | None = None,
) -> list[tuple[str, str]]:
    if redis_client is None:
        from packages.core.cache import _get_redis

        redis_client = await _get_redis()
    if redis_client is None:
        return []
    try:
        rows = await redis_client.xread(
            {runtime_event_stream_key(run_id): after_id},
            count=count,
            block=block_ms,
        )
    except RedisTimeoutError:
        return []
    frames: list[tuple[str, str]] = []
    for _, entries in rows or []:
        for event_id, fields in entries:
            event = fields.get("event") or fields.get(b"event") or "message"
            payload = fields.get("payload") or fields.get(b"payload") or "{}"
            if isinstance(event, bytes):
                event = event.decode("utf-8", errors="replace")
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            if isinstance(event_id, bytes):
                event_id = event_id.decode("ascii", errors="replace")
            frames.append((str(event_id), sse_frame_with_id(str(event), str(payload), str(event_id))))
    return frames


__all__ = [
    "append_runtime_sse_event",
    "parse_sse_frame",
    "read_runtime_sse_events",
    "runtime_event_stream_key",
    "sse_frame_with_id",
]
