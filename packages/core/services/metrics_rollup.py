"""Daily rollup for platform performance metrics.

Aggregates the prior UTC day's TokenUsageLog/ToolCallLog rows into
metrics_daily_usage / metrics_daily_tool_calls. Additive fields only —
never a percentile (see design spec Decision 2). Idempotent: an
INSERT ... ON CONFLICT DO UPDATE upsert keyed on the table's unique
constraint, so re-running for the same day (e.g. after a fix, or a
missed beat tick) just refreshes the row instead of duplicating it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.http_stats import HttpRequestHourly
from packages.core.models.metrics import MetricsDailyToolCalls, MetricsDailyUsage
from packages.core.models.system_metrics import SystemMetricsSample
from packages.core.models.usage import TokenUsageLog, ToolCallLog

# system_metrics_samples is a 30s-tick host-resource history (written by
# ops.collect_snapshot) — ~2,880 rows/day. Pruned here, piggybacking on
# the existing daily beat, rather than adding a beat entry + queue
# registration for a trivial one-statement DELETE.
SYSTEM_SAMPLE_RETENTION_DAYS = 14

# http_request_hourly is the flushed HTTP traffic history (written every
# 5 minutes by metrics.http_flush) — low hundreds of rows/hour. Same
# piggyback-on-the-daily-beat rationale as the samples prune above.
HTTP_HOURLY_RETENTION_DAYS = 90

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_target_day(target_day: Union[date, str, None]) -> date:
    if target_day is None:
        return (_utcnow() - timedelta(days=1)).date()
    if isinstance(target_day, str):
        return date.fromisoformat(target_day)
    return target_day


async def run_daily_rollup(
    db: AsyncSession, *, target_day: Union[date, str, None] = None,
) -> dict:
    """Roll up one UTC day. Returns a small summary dict for logging."""
    day = _resolve_target_day(target_day)
    day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    usage_rows = (await db.execute(
        select(
            TokenUsageLog.source,
            func.count(TokenUsageLog.id).label("call_count"),
            func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(TokenUsageLog.rounds), 0).label("rounds_sum"),
            func.count(TokenUsageLog.rounds).label("rounds_call_count"),
            func.coalesce(func.sum(TokenUsageLog.cache_read_tokens), 0).label("cache_read_tokens_sum"),
            func.coalesce(func.sum(TokenUsageLog.cache_creation_tokens), 0).label("cache_creation_tokens_sum"),
        )
        .where(TokenUsageLog.created_at >= day_start, TokenUsageLog.created_at < day_end)
        .group_by(TokenUsageLog.source)
        # Deterministic lock-acquisition order for the upserts below: two
        # concurrent rollup runs (e.g. an overlapping manual backfill and a
        # scheduled beat tick) must touch rows in the same order, or they
        # can deadlock on each other's row locks inside the same day.
        .order_by(TokenUsageLog.source)
    )).all()

    for row in usage_rows:
        # metrics_daily_usage.source is NOT NULL, unlike the raw log's
        # Optional[str] — record_llm_usage's source param is genuinely
        # optional at the call site, so bucket NULLs into "unknown" rather
        # than fail the upsert.
        source = row.source or "unknown"
        stmt = pg_insert(MetricsDailyUsage).values(
            id=generate_ulid(),
            day=day,
            source=source,
            call_count=int(row.call_count),
            total_tokens=int(row.total_tokens),
            total_cost_usd=float(row.total_cost_usd),
            rounds_sum=int(row.rounds_sum),
            rounds_call_count=int(row.rounds_call_count),
            cache_read_tokens_sum=int(row.cache_read_tokens_sum),
            cache_creation_tokens_sum=int(row.cache_creation_tokens_sum),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "source"],
            set_={
                "call_count": stmt.excluded.call_count,
                "total_tokens": stmt.excluded.total_tokens,
                "total_cost_usd": stmt.excluded.total_cost_usd,
                "rounds_sum": stmt.excluded.rounds_sum,
                "rounds_call_count": stmt.excluded.rounds_call_count,
                "cache_read_tokens_sum": stmt.excluded.cache_read_tokens_sum,
                "cache_creation_tokens_sum": stmt.excluded.cache_creation_tokens_sum,
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)

    tool_rows = (await db.execute(
        select(
            ToolCallLog.tool_name,
            ToolCallLog.source,
            func.count(ToolCallLog.id).label("call_count"),
            func.sum(case((ToolCallLog.outcome == "error", 1), else_=0)).label("error_count"),
            func.sum(case((ToolCallLog.outcome == "empty_result", 1), else_=0)).label("empty_result_count"),
            func.coalesce(func.sum(ToolCallLog.duration_ms), 0).label("duration_ms_sum"),
        )
        .where(ToolCallLog.created_at >= day_start, ToolCallLog.created_at < day_end)
        .group_by(ToolCallLog.tool_name, ToolCallLog.source)
        # Same deterministic-ordering rationale as the usage query above.
        .order_by(ToolCallLog.tool_name, ToolCallLog.source)
    )).all()

    for row in tool_rows:
        # metrics_daily_tool_calls.source is NOT NULL, unlike the raw log's
        # Optional[str] — see the matching comment in the usage loop above.
        source = row.source or "unknown"
        stmt = pg_insert(MetricsDailyToolCalls).values(
            id=generate_ulid(),
            day=day,
            tool_name=row.tool_name,
            source=source,
            call_count=int(row.call_count),
            error_count=int(row.error_count or 0),
            empty_result_count=int(row.empty_result_count or 0),
            duration_ms_sum=int(row.duration_ms_sum),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "tool_name", "source"],
            set_={
                "call_count": stmt.excluded.call_count,
                "error_count": stmt.excluded.error_count,
                "empty_result_count": stmt.excluded.empty_result_count,
                "duration_ms_sum": stmt.excluded.duration_ms_sum,
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)

    # Retention for the host-resource sample history (see the module
    # const above). Cutoff is relative to *now*, not target_day — a
    # backfill re-run for an old day must not wipe recent samples, and
    # pruning is idempotent either way.
    #
    # Isolated in a SAVEPOINT + try/except: a prune failure (e.g. the
    # samples table missing on a worker running new code against a
    # pre-migration DB mid-rolling-deploy) must never poison that day's
    # rollup upserts — the Celery task commits only after this function
    # returns, and with max_retries=0 a raised prune would silently drop
    # the whole day. A bare try/except is NOT enough on Postgres: a
    # failed statement aborts the transaction and turns the caller's
    # COMMIT into a rollback, so the DELETE runs inside begin_nested().
    # ``system_samples_pruned`` is None when the prune failed — "count
    # unknown", deliberately distinct from 0 ("nothing old enough").
    system_samples_pruned: Optional[int]
    try:
        prune_cutoff = _utcnow() - timedelta(days=SYSTEM_SAMPLE_RETENTION_DAYS)
        async with db.begin_nested():
            prune_result = await db.execute(
                delete(SystemMetricsSample).where(SystemMetricsSample.sampled_at < prune_cutoff)
            )
        system_samples_pruned = int(prune_result.rowcount or 0)
    except Exception:
        logger.warning(
            "system_metrics_samples retention prune failed; keeping the day's rollup",
            exc_info=True,
        )
        system_samples_pruned = None

    # Retention for the flushed HTTP traffic history — the exact same
    # SAVEPOINT-isolated, None-on-failure pattern as the samples prune
    # above (each prune in its OWN savepoint, so one failing never takes
    # the other prune or the day's rollup down with it).
    http_hourly_pruned: Optional[int]
    try:
        http_cutoff = _utcnow() - timedelta(days=HTTP_HOURLY_RETENTION_DAYS)
        async with db.begin_nested():
            http_prune_result = await db.execute(
                delete(HttpRequestHourly).where(HttpRequestHourly.hour < http_cutoff)
            )
        http_hourly_pruned = int(http_prune_result.rowcount or 0)
    except Exception:
        logger.warning(
            "http_request_hourly retention prune failed; keeping the day's rollup",
            exc_info=True,
        )
        http_hourly_pruned = None

    return {
        "day": day.isoformat(),
        "usage_rows": len(usage_rows),
        "tool_call_rows": len(tool_rows),
        "system_samples_pruned": system_samples_pruned,
        "http_hourly_pruned": http_hourly_pruned,
    }
