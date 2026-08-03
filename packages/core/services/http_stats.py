"""HTTP traffic counters — Redis key/field vocabulary + the 5-minute
flush that snapshot-syncs the counters into ``http_request_hourly``.

Hot path (``apps.api.middleware.http_stats``): one HINCRBY per request
into the current UTC hour's hash — no per-request DB writes. This module
owns the key/field format so the writer (middleware) and the reader
(flush task) can never drift apart:

    key:   http:stats:{YYYYMMDDHH}          (UTC hour bucket, 48h TTL)
    field: {METHOD}|{route_template}|{status_class}

``route_template`` is the matched route's template (``/items/{id}``,
never the raw path) to keep cardinality bounded; requests that matched
no route collapse into the single ``unmatched`` path. ``status_class``
is ``2xx``/``3xx``/``4xx``/``5xx``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.http_stats import HttpRequestHourly

logger = logging.getLogger(__name__)

STATS_KEY_PREFIX = "http:stats:"

# The TTL bounds Redis growth if the flush task stops running — it is
# NOT the recovery window. The flush only re-reads the last
# FLUSH_LOOKBACK_HOURS hour-buckets, so a flush outage longer than that
# permanently drops the tail of the oldest bucket even though its Redis
# key still exists until this TTL expires.
STATS_KEY_TTL_SECONDS = 48 * 3600

# How many completed hour-buckets each flush re-reads in addition to the
# current one. Re-flushing an already-synced bucket upserts identical
# absolute values (idempotent), so the extra look-back is free — it just
# buys a ~3h flush-outage recovery window for 3 extra HGETALLs.
FLUSH_LOOKBACK_HOURS = 3

UNMATCHED_PATH = "unmatched"


def hour_key(at: datetime | None = None) -> str:
    """Redis hash key for the UTC hour containing ``at`` (default now)."""
    at = at if at is not None else datetime.now(timezone.utc)
    return f"{STATS_KEY_PREFIX}{at.astimezone(timezone.utc):%Y%m%d%H}"


def status_class(status_code: int) -> str:
    return f"{int(status_code) // 100}xx"


def stats_field(method: str, route_template: str, status_code: int) -> str:
    return f"{method.upper()}|{route_template}|{status_class(status_code)}"


# ── Flush (Redis → Postgres) ─────────────────────────────────────────


def _flush_keys(now: datetime | None = None) -> list[str]:
    """Current hour + the previous FLUSH_LOOKBACK_HOURS hours, oldest
    first. The look-back both finalises a just-closed bucket's count and
    recovers cleanly from a flush outage of up to ~FLUSH_LOOKBACK_HOURS
    (each re-read is an idempotent absolute-value upsert)."""
    now = now if now is not None else datetime.now(timezone.utc)
    return [
        hour_key(now - timedelta(hours=offset))
        for offset in range(FLUSH_LOOKBACK_HOURS, -1, -1)
    ]


def _parse_hour(key: str) -> datetime:
    stamp = key[len(STATS_KEY_PREFIX):]
    return datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def _parse_field(field: str) -> tuple[str, str, str] | None:
    """``{METHOD}|{route_template}|{status_class}`` → (method, path,
    status_class), or None for garbage. Split from both ends so a ``|``
    inside a route template (never happens today, but cheap to be right
    about) stays part of the path. Clipped to the column widths — an
    over-long value must degrade to truncation, not fail the whole flush."""
    try:
        method, rest = field.split("|", 1)
        path, sclass = rest.rsplit("|", 1)
    except ValueError:
        return None
    if not method or not path or not sclass:
        return None
    return method[:10], path[:300], sclass[:3]


async def _read_hour_hashes(keys: list[str]) -> dict[str, dict[str, int]]:
    """HGETALL each key via the shared async Redis client. Returns only
    non-empty hashes; {} when Redis is unavailable (the flush is then a
    cheap no-op — the counters are still accumulating, or Redis is down
    and there is nothing to sync either way)."""
    from packages.core.cache import _get_redis

    r = await _get_redis()
    if r is None:
        return {}
    out: dict[str, dict[str, int]] = {}
    for key in keys:
        data = await r.hgetall(key)
        if not data:
            continue
        fields: dict[str, int] = {}
        for field, raw in data.items():
            try:
                fields[str(field)] = int(raw)
            except (TypeError, ValueError):
                logger.debug("http stats flush: non-integer count for %s %s", key, field)
        if fields:
            out[key] = fields
    return out


async def flush_http_stats(db: AsyncSession) -> dict:
    """Snapshot-sync the last few hours' Redis counters (see
    ``_flush_keys``) into ``http_request_hourly``. Does NOT commit — the
    Celery task owns the transaction, mirroring
    ``metrics_rollup.run_daily_rollup``.

    The upsert SETS count to the Redis value (absolute), never adds to
    it: Redis holds the running total for the hour, so each flush is a
    snapshot-sync and re-running is idempotent by construction."""
    hashes = await _read_hour_hashes(_flush_keys())

    entries: list[tuple[datetime, str, str, str, int]] = []
    for key, fields in hashes.items():
        hour = _parse_hour(key)
        for field, count in fields.items():
            parsed = _parse_field(field)
            if parsed is None:
                logger.debug("http stats flush: unparseable field %r in %s", field, key)
                continue
            method, path, sclass = parsed
            entries.append((hour, method, path, sclass, count))

    # Deterministic lock-acquisition order (same rationale as the daily
    # rollup's ORDER BY): two overlapping flush runs must touch rows in
    # the same order or they can deadlock on each other's row locks.
    entries.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    for hour, method, path, sclass, count in entries:
        stmt = pg_insert(HttpRequestHourly).values(
            id=generate_ulid(),
            hour=hour,
            method=method,
            path=path,
            status_class=sclass,
            count=count,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["hour", "method", "path", "status_class"],
            set_={"count": stmt.excluded.count},
        )
        await db.execute(stmt)

    return {"keys_read": len(hashes), "rows_upserted": len(entries)}
