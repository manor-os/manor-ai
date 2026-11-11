"""Platform performance metrics Celery tasks."""
from __future__ import annotations

import logging
import uuid

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=0, name="metrics.daily_rollup")
def metrics_daily_rollup(self, target_day: str | None = None):
    """Roll up the prior UTC day's usage/tool-call logs into the daily
    rollup tables. Beat-driven, once a day; idempotent, so a manual
    re-run (e.g. ``metrics_daily_rollup.delay(target_day="2026-07-15")``
    to backfill one day after a fix) is always safe."""
    try:
        from packages.core.database import create_worker_session
        from packages.core.services.metrics_rollup import run_daily_rollup

        async def _go():
            async with create_worker_session()() as db:
                result = await run_daily_rollup(db, target_day=target_day)
                await db.commit()
                return result

        result = _run_async(_go())
        logger.info("metrics_daily_rollup: %s", result)
    except Exception:
        logger.exception("metrics_daily_rollup failed")

    # Workspace health roll-up: its own session/transaction and its own
    # try/except, so neither roll-up failing ever skips the other.
    try:
        from packages.core.database import create_worker_session
        from packages.core.services.metrics_rollup import _resolve_target_day
        from packages.core.services.workspace_health_rollup import rollup_workspace_day

        _day = _resolve_target_day(target_day).isoformat()

        async def _workspace_day():
            async with create_worker_session()() as db:
                result = await rollup_workspace_day(db, day=_day)
                await db.commit()
                return result

        result = _run_async(_workspace_day())
        logger.info("metrics_daily_rollup workspace health: %s", result)
    except Exception:
        logger.exception("metrics_daily_rollup workspace health failed")



@celery_app.task(bind=True, max_retries=0, name="metrics.http_flush")
def metrics_http_flush(self):
    """Snapshot-sync the Redis HTTP traffic counters (written per-request
    by ``apps.api.middleware.http_stats``) into ``http_request_hourly``.
    Beat-driven every 5 minutes; idempotent — the upsert sets each row's
    count to the Redis running total (absolute), so overlapping or
    repeated runs converge instead of double-counting."""
    try:
        from packages.core.database import create_worker_session
        from packages.core.services.http_stats import flush_http_stats

        async def _go():
            async with create_worker_session()() as db:
                result = await flush_http_stats(db)
                await db.commit()
                return result

        result = _run_async(_go())
        logger.info("metrics_http_flush: %s", result)
    except Exception:
        logger.exception("metrics_http_flush failed")


_HEALTH_LOCK_KEY = "metrics:workspace_health:lock"
_HEALTH_LOCK_TTL_S = 50 * 60
"""Below the hourly interval: a killed worker must not skip the next tick."""
# Compare-and-delete so a run that outlived the TTL cannot release a lock the
# next run now holds.
_RELEASE_IF_OWNER = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


def _fresh_redis():
    """Per-call Redis client: each Celery task runs in its own event loop,
    so the cached singleton would raise ``Event loop is closed``."""
    try:
        import redis.asyncio as aioredis
        from packages.core.config import get_settings
        return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.debug("metrics redis client init failed: %s", exc)
        return None


async def _try_acquire_health_lock(token: str) -> bool:
    """Single-flight across workers. Redis down means "run"."""
    r = _fresh_redis()
    if r is None:
        return True
    try:
        return bool(await r.set(_HEALTH_LOCK_KEY, token, nx=True, ex=_HEALTH_LOCK_TTL_S))
    except Exception as exc:
        logger.debug("workspace health lock unavailable: %s", exc)
        return True
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


async def _release_health_lock(token: str) -> None:
    r = _fresh_redis()
    if r is None:
        return
    try:
        await r.eval(_RELEASE_IF_OWNER, 1, _HEALTH_LOCK_KEY, token)
    except Exception as exc:
        logger.debug("workspace health lock release failed: %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


@celery_app.task(bind=True, max_retries=0, name="metrics.workspace_health_refresh")
def metrics_workspace_health_refresh(self):
    """Re-roll today and refresh every Workspace's health snapshot. Single
    flight across workers; a tick that finds the lock held skips, and the
    next tick catches up because the roll-up replaces the day."""
    try:
        from packages.core.database import create_worker_session
        from packages.core.services.workspace_health_snapshot import refresh_workspace_health

        async def _go():
            token = uuid.uuid4().hex
            if not await _try_acquire_health_lock(token):
                return {"skipped": "lock held"}
            try:
                async with create_worker_session()() as db:
                    result = await refresh_workspace_health(db)
                    await db.commit()
                    return result
            finally:
                await _release_health_lock(token)

        result = _run_async(_go())
        logger.info("metrics_workspace_health_refresh: %s", result)
    except Exception:
        logger.exception("metrics_workspace_health_refresh failed")
