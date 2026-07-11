"""Ops monitoring Celery tasks.

Two periodic jobs:

  * ``ops.collect_snapshot`` (every 30s) — pulls host + container state
    via ``ops_service.collect_snapshot`` and writes JSON to Redis at
    ``ops:snapshot``. The admin /ops/snapshot endpoint reads from there
    to avoid blocking ~1.3s on Docker stats per request.

  * ``ops.alert_tick`` (every 60s) — re-collects (or reads cache) and
    runs the rule engine in ``ops_alerts``. Emits emails for any rule
    that fires + isn't in cooldown.

Both are intentionally cheap — the host collector is a few psutil calls;
the docker collector touches the local socket. Skip them on environments
without docker.sock or psutil; the tasks log + return rather than raise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)


_SNAPSHOT_KEY = "ops:snapshot"
_SNAPSHOT_TTL_S = 120  # 2 min — covers any beat skew between collect (30s) and alert (60s)


def _fresh_redis():
    """Open a fresh per-call Redis client (Celery tasks each run in
    their own asyncio event loop; the cached singleton in
    ``packages.core.cache`` raises ``Event loop is closed`` on the
    second tick)."""
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.debug("ops redis client init failed: %s", exc)
        return None


async def _write_snapshot_to_redis(snap: dict) -> None:
    r = _fresh_redis()
    if r is None:
        return
    try:
        await r.setex(_SNAPSHOT_KEY, _SNAPSHOT_TTL_S, json.dumps(snap))
    except Exception as exc:
        logger.debug("ops snapshot Redis write failed: %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


async def _read_snapshot_from_redis() -> dict | None:
    r = _fresh_redis()
    if r is None:
        return None
    try:
        cached = await r.get(_SNAPSHOT_KEY)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.debug("ops snapshot Redis read failed: %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:
            pass
    return None


@celery_app.task(name="ops.collect_snapshot")
def collect_snapshot_task() -> dict:
    """Collect a snapshot and store it in Redis. Returns a small summary
    so Flower / task-result inspectors can see what happened."""
    started = time.monotonic()
    try:
        from packages.core.services.ops_service import collect_snapshot
        snap = collect_snapshot()
    except Exception as exc:
        logger.warning("ops.collect_snapshot failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}

    try:
        asyncio.run(_write_snapshot_to_redis(snap))
    except RuntimeError:
        # Event loop already running (e.g., re-entry inside another task)
        # — fall back to fire-and-forget.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_write_snapshot_to_redis(snap))
        finally:
            loop.close()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "containers": len(snap.get("containers") or []),
        "host_cpu": (snap.get("host") or {}).get("cpu_pct"),
        "elapsed_ms": elapsed_ms,
    }


@celery_app.task(name="ops.log_scan")
def log_scan_task() -> dict:
    """Scan recent docker logs per running container, count error-level
    matches, update rolling 1h baseline, publish spike map for the rule
    engine. Runs every 60s alongside ``ops.alert_tick``.
    """
    try:
        from packages.core.services.ops_service import collect_log_error_counts
        from packages.core.services.ops_alerts import update_log_baselines_sync
        counts = collect_log_error_counts(since_seconds=60)
        spikes = update_log_baselines_sync(counts)
    except Exception as exc:
        logger.warning("ops.log_scan failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    if spikes:
        logger.info("ops.log_scan detected spikes: %s", list(spikes.keys()))
    return {"ok": True, "scanned": len(counts), "spikes": list(spikes.keys())}


@celery_app.task(name="ops.send_digest")
def send_digest_task() -> dict:
    """Daily digest of suppressed warning/info alerts."""
    try:
        from packages.core.services.ops_alerts import send_digest_sync
        return send_digest_sync()
    except Exception as exc:
        logger.warning("ops.send_digest failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


@celery_app.task(name="ops.alert_tick")
def alert_tick_task() -> dict:
    """Read the latest snapshot from Redis (fallback to live collect)
    and fire any matched alert rules."""
    # Prefer the cached snapshot — written 30s ago at most by collect_snapshot_task.
    try:
        snap = asyncio.run(_read_snapshot_from_redis())
    except RuntimeError:
        snap = None

    if not snap:
        try:
            from packages.core.services.ops_service import collect_snapshot
            snap = collect_snapshot()
        except Exception as exc:
            logger.warning("ops.alert_tick: snapshot collect failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    try:
        from packages.core.services.ops_alerts import evaluate_and_dispatch_sync
        summary = evaluate_and_dispatch_sync(snap)
    except Exception as exc:
        logger.warning("ops.alert_tick: dispatch failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}

    if summary.get("fired"):
        logger.info("ops.alert_tick fired: %s", summary["fired"])
    return {"ok": True, **summary}
