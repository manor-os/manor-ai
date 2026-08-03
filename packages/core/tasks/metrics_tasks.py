"""Platform performance metrics Celery tasks."""
from __future__ import annotations

import logging

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
