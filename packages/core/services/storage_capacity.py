"""Room for media before it is made: the storage precheck.

Plan storage is enforced when a file becomes a Document. Paid or long media
production (a generated video, a Workflow stage that makes dozens of shots)
would otherwise spend credits and time and fail only when its output is
saved. Before such work starts, the caller estimates its output and calls
:func:`ensure_storage_capacity`; an Entity without room is refused up front
with the same :class:`StorageLimitExceeded` a save raises (HTTP 402, tool code
``knowledge_storage_limit``). Media jobs still in flight count against the
room too, since their files are about to be saved.

Estimates are deliberately plain: a per-second bitrate by resolution times
the planned duration and unit count. They decide only whether to start.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.document_service import StorageLimitExceeded

MB = 1024 * 1024

# Typical H.264 output of generated or rendered video, in bytes per second.
VIDEO_BYTES_PER_SECOND = {
    "480p": 150_000,      # ~1.2 Mbps
    "720p": 320_000,      # ~2.5 Mbps
    "1080p": 640_000,     # ~5 Mbps
    "1440p": 1_250_000,   # ~10 Mbps
    "2160p": 2_500_000,   # ~20 Mbps
}
_RESOLUTION_ALIASES = {"2k": "1440p", "4k": "2160p", "uhd": "2160p", "fhd": "1080p", "hd": "720p", "sd": "480p"}
DEFAULT_VIDEO_RESOLUTION = "720p"
AUDIO_BYTES_PER_SECOND = 24_000   # ~192 kbps
IMAGE_BYTES = 2 * MB
# When a unit's duration is not planned yet: a generated shot, a song.
DEFAULT_UNIT_SECONDS = {"video": 8.0, "audio": 180.0}
# A job pending longer than this has stalled; it no longer holds room.
IN_FLIGHT_WINDOW = timedelta(hours=6)


def _video_rate(resolution: Any) -> int:
    key = str(resolution or "").strip().lower()
    key = _RESOLUTION_ALIASES.get(key, key)
    if key not in VIDEO_BYTES_PER_SECOND and key.endswith("p") and key[:-1].isdigit():
        height = int(key[:-1])
        key = min(VIDEO_BYTES_PER_SECOND, key=lambda name: abs(int(name[:-1]) - height))
    return VIDEO_BYTES_PER_SECOND.get(key, VIDEO_BYTES_PER_SECOND[DEFAULT_VIDEO_RESOLUTION])


def estimate_media_bytes(
    kind: str,
    *,
    duration_seconds: float | None = None,
    resolution: str | None = None,
    units: int = 1,
) -> int:
    """Rough size of ``units`` outputs of ``kind`` (video, audio or image)."""
    units = max(0, int(units or 0))
    if kind == "image":
        return IMAGE_BYTES * units
    seconds = duration_seconds if duration_seconds and duration_seconds > 0 else DEFAULT_UNIT_SECONDS.get(kind, 0.0)
    rate = AUDIO_BYTES_PER_SECOND if kind == "audio" else _video_rate(resolution)
    return int(rate * seconds * units)


def estimate_units_bytes(kind: str, units: list[dict[str, Any]], *, resolution: str | None = None) -> int:
    """Size of planned units, each with an optional ``duration_s``."""
    return sum(
        estimate_media_bytes(kind, duration_seconds=unit.get("duration_s"), resolution=resolution)
        for unit in units
    )


async def in_flight_media_bytes(db: AsyncSession, entity_id: str) -> int:
    """Estimated size of the Entity's media jobs that have not finished yet."""
    from packages.core.models.media_job import MediaJob, MediaJobStatus

    jobs = (await db.execute(
        select(MediaJob.kind, MediaJob.duration_seconds, MediaJob.params).where(
            MediaJob.entity_id == entity_id,
            MediaJob.status.notin_(MediaJobStatus.terminal()),
            MediaJob.created_at >= datetime.now(UTC) - IN_FLIGHT_WINDOW,
        ).limit(500)
    )).all()
    return sum(
        estimate_media_bytes(
            kind, duration_seconds=duration, resolution=(params or {}).get("resolution"),
        )
        for kind, duration, params in jobs
    )


def _mb(value: float) -> str:
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


async def ensure_storage_capacity(
    db: AsyncSession,
    entity_id: str,
    incoming_bytes: int,
    *,
    what: str = "This",
) -> None:
    """Raise :class:`StorageLimitExceeded` unless the plan has room for ``incoming_bytes``.

    Media jobs already in flight are added to the request. OSS and unlimited
    plans (Enterprise) always have room.
    """
    from packages.core.constants.plans import is_cloud
    from packages.core.services.plan_gate import check

    if not is_cloud():
        return
    pending = await in_flight_media_bytes(db, entity_id)
    needed_mb = (max(0, incoming_bytes) + pending) / MB
    gate = await check(db, entity_id, "storage_mb", use_cache=False, amount=needed_mb)
    if gate.allowed:
        return
    limit = float(gate.limit or 0)
    current = float(gate.current or 0)
    free = max(0.0, limit - current)
    raise StorageLimitExceeded(
        f"{what} needs about {_mb(needed_mb)} of storage, but only {_mb(free)} of the "
        f"{gate.plan} plan's {_mb(limit)} is free. Free up space in Knowledge or upgrade "
        "your plan, then start again. Nothing was generated or charged.",
        plan=gate.plan, limit=gate.limit, current=gate.current,
    )


def storage_blocked_result(exc: StorageLimitExceeded, **extra: Any) -> str:
    """The tool result for a refused start: the person must free space or upgrade."""
    return json.dumps({
        **extra, "status": "blocked", "code": "knowledge_storage_limit", "error": str(exc),
        "plan": exc.plan, "limit": exc.limit, "current": exc.current,
    }, ensure_ascii=False)
