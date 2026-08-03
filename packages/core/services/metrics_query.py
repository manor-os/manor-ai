"""Platform performance metrics — the four read queries shared by the
admin HTTP endpoints and the admin-MCP mirror tools (one implementation,
not duplicated the way /usage and get_usage_report duplicate theirs).

Hybrid live/rollup rule (design spec Decision 2): windows of
LIVE_WINDOW_DAYS or less aggregate live over the raw tables; longer
windows sum the daily rollup tables. Percentiles are ALWAYS computed
live via percentile_cont, regardless of window length, capped at
MAX_WINDOW_DAYS — never derived from the rollup (a p90 over 30 days is
not the average of 30 daily p90s).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.task import TaskStatus
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.http_stats import HttpRequestHourly
from packages.core.models.metrics import MetricsDailyToolCalls, MetricsDailyUsage
from packages.core.models.system_metrics import SystemMetricsSample
from packages.core.models.task import Task
from packages.core.models.tool_path_memory import ToolIntentPath
from packages.core.models.usage import TokenUsageLog, ToolCallLog

LIVE_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90


def _clamp_days(days: int) -> int:
    return max(1, min(int(days), MAX_WINDOW_DAYS))


def _use_live_aggregates(days: int) -> bool:
    return days <= LIVE_WINDOW_DAYS


async def efficiency_metrics(
    db: AsyncSession, *, days: int = 30, sources: Optional[list[str]] = None,
) -> dict:
    """Area A: p90/p99 latency, avg rounds/request, avg tokens/request,
    grouped by request-type ``source``."""
    days = _clamp_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    percentile_where = [TokenUsageLog.created_at >= cutoff, TokenUsageLog.duration_ms.isnot(None)]
    if sources:
        percentile_where.append(TokenUsageLog.source.in_(sources))
    percentile_rows = (await db.execute(
        select(
            TokenUsageLog.source.label("source"),
            func.percentile_cont(0.90).within_group(TokenUsageLog.duration_ms.asc()).label("p90_duration_ms"),
            func.percentile_cont(0.99).within_group(TokenUsageLog.duration_ms.asc()).label("p99_duration_ms"),
        )
        .where(*percentile_where)
        .group_by(TokenUsageLog.source)
    )).all()
    percentiles_by_source = {
        (r.source or "unknown"): {
            "p90_duration_ms": float(r.p90_duration_ms) if r.p90_duration_ms is not None else None,
            "p99_duration_ms": float(r.p99_duration_ms) if r.p99_duration_ms is not None else None,
        }
        for r in percentile_rows
    }

    # Prompt-cache hit rate: sum(cache_read_tokens) / sum(prompt_tokens),
    # the same definition the older /usage endpoint uses. ALWAYS computed
    # live over TokenUsageLog regardless of window (like the percentiles
    # above): the rollup table stores cache_read_tokens_sum but NOT
    # prompt_tokens_sum, so the exact ratio can't be derived from rollup
    # without a migration — and raw rows are retained indefinitely, so a
    # live ratio query is the same cost-shape as the percentile query.
    cache_where = [TokenUsageLog.created_at >= cutoff]
    if sources:
        cache_where.append(TokenUsageLog.source.in_(sources))
    cache_rows = (await db.execute(
        select(
            TokenUsageLog.source.label("source"),
            func.coalesce(func.sum(TokenUsageLog.cache_read_tokens), 0).label("cache_read"),
            func.coalesce(func.sum(TokenUsageLog.prompt_tokens), 0).label("prompt"),
        )
        .where(*cache_where)
        .group_by(TokenUsageLog.source)
    )).all()
    cache_by_source = {
        (r.source or "unknown"): (
            (int(r.cache_read) / int(r.prompt)) if int(r.prompt) else None
        )
        for r in cache_rows
    }

    if _use_live_aggregates(days):
        agg_where = [TokenUsageLog.created_at >= cutoff]
        if sources:
            agg_where.append(TokenUsageLog.source.in_(sources))
        agg_rows = (await db.execute(
            select(
                TokenUsageLog.source.label("source"),
                func.count(TokenUsageLog.id).label("call_count"),
                func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(TokenUsageLog.rounds), 0).label("rounds_sum"),
                func.count(TokenUsageLog.rounds).label("rounds_call_count"),
            )
            .where(*agg_where)
            .group_by(TokenUsageLog.source)
        )).all()
    else:
        cutoff_day = cutoff.date()
        rollup_where = [MetricsDailyUsage.day >= cutoff_day]
        if sources:
            rollup_where.append(MetricsDailyUsage.source.in_(sources))
        agg_rows = (await db.execute(
            select(
                MetricsDailyUsage.source.label("source"),
                func.sum(MetricsDailyUsage.call_count).label("call_count"),
                func.sum(MetricsDailyUsage.total_tokens).label("total_tokens"),
                func.sum(MetricsDailyUsage.rounds_sum).label("rounds_sum"),
                func.sum(MetricsDailyUsage.rounds_call_count).label("rounds_call_count"),
            )
            .where(*rollup_where)
            .group_by(MetricsDailyUsage.source)
        )).all()

    agg_by_source = {(r.source or "unknown"): r for r in agg_rows}
    all_sources = set(percentiles_by_source) | set(agg_by_source) | set(cache_by_source)

    buckets = []
    for source in sorted(all_sources):
        agg = agg_by_source.get(source)
        call_count = int(agg.call_count) if agg else 0
        total_tokens = int(agg.total_tokens) if agg else 0
        rounds_sum = int(agg.rounds_sum) if agg else 0
        rounds_call_count = int(agg.rounds_call_count) if agg else 0
        pct = percentiles_by_source.get(source, {})
        buckets.append({
            "source": source,
            "call_count": call_count,
            "p90_duration_ms": pct.get("p90_duration_ms"),
            "p99_duration_ms": pct.get("p99_duration_ms"),
            "avg_tokens_per_request": (total_tokens / call_count) if call_count else None,
            "avg_rounds_per_request": (rounds_sum / rounds_call_count) if rounds_call_count else None,
            "cache_hit_rate": cache_by_source.get(source),
        })

    return {"days": days, "buckets": buckets}


async def discovery_health(db: AsyncSession, *, days: int = 30) -> dict:
    """Area B: search_tools hit rate, cross-tool dead-end rate, and
    Tool Discovery v2 intent-path cache health. Platform-wide (not
    per-source — search and intent-path lookups aren't naturally scoped
    to a request type).

    Note: the intent_path_* fields are always all-time cumulative
    (ToolIntentPath has no per-day rows), not windowed to ``days`` like
    the other fields in this dict."""
    days = _clamp_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if _use_live_aggregates(days):
        total_row = (await db.execute(
            select(
                func.count(ToolCallLog.id).label("call_count"),
                func.sum(case((ToolCallLog.outcome == "error", 1), else_=0)).label("error_count"),
            )
            .where(ToolCallLog.created_at >= cutoff)
        )).one()
        search_row = (await db.execute(
            select(
                func.count(ToolCallLog.id).label("call_count"),
                func.sum(case((ToolCallLog.outcome == "empty_result", 1), else_=0)).label("empty_result_count"),
            )
            .where(ToolCallLog.created_at >= cutoff, ToolCallLog.tool_name == "search_tools")
        )).one()
    else:
        cutoff_day = cutoff.date()
        total_row = (await db.execute(
            select(
                func.coalesce(func.sum(MetricsDailyToolCalls.call_count), 0).label("call_count"),
                func.coalesce(func.sum(MetricsDailyToolCalls.error_count), 0).label("error_count"),
            )
            .where(MetricsDailyToolCalls.day >= cutoff_day)
        )).one()
        search_row = (await db.execute(
            select(
                func.coalesce(func.sum(MetricsDailyToolCalls.call_count), 0).label("call_count"),
                func.coalesce(func.sum(MetricsDailyToolCalls.empty_result_count), 0).label("empty_result_count"),
            )
            .where(MetricsDailyToolCalls.day >= cutoff_day, MetricsDailyToolCalls.tool_name == "search_tools")
        )).one()

    total_calls = int(total_row.call_count or 0)
    total_errors = int(total_row.error_count or 0)
    search_calls = int(search_row.call_count or 0)
    search_empty = int(search_row.empty_result_count or 0)

    path_row = (await db.execute(
        select(
            func.count(ToolIntentPath.id).label("total_paths"),
            func.sum(case((ToolIntentPath.failure_count >= 2, 1), else_=0)).label("suppressed_paths"),
            func.coalesce(func.sum(ToolIntentPath.success_count), 0).label("success_sum"),
            func.coalesce(func.sum(ToolIntentPath.failure_count), 0).label("failure_sum"),
        )
    )).one()
    total_paths = int(path_row.total_paths or 0)
    suppressed_paths = int(path_row.suppressed_paths or 0)
    success_sum = int(path_row.success_sum or 0)
    failure_sum = int(path_row.failure_sum or 0)
    total_outcomes = success_sum + failure_sum

    return {
        "days": days,
        "total_tool_calls": total_calls,
        "tool_dead_end_rate": (total_errors / total_calls) if total_calls else None,
        "search_call_count": search_calls,
        "search_hit_rate": (1 - search_empty / search_calls) if search_calls else None,
        "intent_path_total": total_paths,
        "intent_path_suppression_rate": (suppressed_paths / total_paths) if total_paths else None,
        "intent_path_success_rate": (success_sum / total_outcomes) if total_outcomes else None,
    }


async def reliability_metrics(
    db: AsyncSession, *, days: int = 30, sources: Optional[list[str]] = None,
) -> dict:
    """Area C: tool error rate, approval denial rate + pending-duration
    percentiles, task terminal-status failure rate, and a v1 stuck-loop
    heuristic. Per Decision 1: request types with no explicit terminal
    signal report ``None`` (never a fabricated success rate).

    ``sources`` scopes ``tool_error_buckets`` and the stuck-loop check
    only — HitlRequest and Task have no request-type source concept,
    so ``approval_buckets``, ``task_total``, and ``task_failure_rate``
    are always platform-wide."""
    days = _clamp_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Tool error rate, by tool_name + source.
    if _use_live_aggregates(days):
        tool_where = [ToolCallLog.created_at >= cutoff]
        if sources:
            tool_where.append(ToolCallLog.source.in_(sources))
        tool_rows = (await db.execute(
            select(
                ToolCallLog.tool_name.label("tool_name"),
                ToolCallLog.source.label("source"),
                func.count(ToolCallLog.id).label("call_count"),
                func.sum(case((ToolCallLog.outcome == "error", 1), else_=0)).label("error_count"),
            )
            .where(*tool_where)
            .group_by(ToolCallLog.tool_name, ToolCallLog.source)
            .order_by(func.count(ToolCallLog.id).desc())
            .limit(200)
        )).all()
    else:
        cutoff_day = cutoff.date()
        tool_where = [MetricsDailyToolCalls.day >= cutoff_day]
        if sources:
            tool_where.append(MetricsDailyToolCalls.source.in_(sources))
        tool_rows = (await db.execute(
            select(
                MetricsDailyToolCalls.tool_name.label("tool_name"),
                MetricsDailyToolCalls.source.label("source"),
                func.sum(MetricsDailyToolCalls.call_count).label("call_count"),
                func.sum(MetricsDailyToolCalls.error_count).label("error_count"),
            )
            .where(*tool_where)
            .group_by(MetricsDailyToolCalls.tool_name, MetricsDailyToolCalls.source)
            .order_by(func.sum(MetricsDailyToolCalls.call_count).desc())
            .limit(200)
        )).all()

    tool_error_buckets = [
        {
            "tool_name": r.tool_name,
            "source": r.source or "unknown",
            "call_count": int(r.call_count),
            "error_count": int(r.error_count or 0),
            "error_rate": (int(r.error_count or 0) / int(r.call_count)) if r.call_count else None,
        }
        for r in tool_rows
    ]

    # Approval denial rate + pending-duration percentiles (always live —
    # HitlRequest is not rolled up; volume is much lower than tool
    # calls, and no rollup was specced for it).
    pending_ms = (
        func.extract("epoch", HitlRequest.decided_at)
        - func.extract("epoch", HitlRequest.created_at)
    ) * 1000
    approval_rows = (await db.execute(
        select(
            HitlRequest.action_key.label("action_key"),
            HitlRequest.resource_kind.label("resource_kind"),
            func.count(HitlRequest.id).label("total"),
            func.sum(case((HitlRequest.status == ApprovalStatus.DENIED, 1), else_=0)).label("denied"),
            func.percentile_cont(0.90).within_group(pending_ms.asc()).label("p90_pending_ms"),
        )
        .where(HitlRequest.created_at >= cutoff, HitlRequest.decided_at.isnot(None))
        .group_by(HitlRequest.action_key, HitlRequest.resource_kind)
        .limit(200)
    )).all()
    approval_buckets = [
        {
            "action_key": r.action_key,
            "resource_kind": r.resource_kind,
            "total": int(r.total),
            "denied": int(r.denied or 0),
            "denial_rate": (int(r.denied or 0) / int(r.total)) if r.total else None,
            "p90_pending_ms": float(r.p90_pending_ms) if r.p90_pending_ms is not None else None,
        }
        for r in approval_rows
    ]

    # Task terminal-status failure rate — the only "success rate" this
    # area reports, and only for request types with this real signal.
    # "waiting_on_customer" is NOT terminal (per constants/task.py it's
    # grouped with the active/in-flight statuses — a task can resume
    # from it back into in_progress/completed/failed), so it's excluded
    # from both the denominator and numerator here.
    task_rows = (await db.execute(
        select(Task.status.label("status"), func.count(Task.id).label("count"))
        .where(
            Task.created_at >= cutoff,
            Task.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED)),
        )
        .group_by(Task.status)
    )).all()
    task_counts = {r.status: int(r.count) for r in task_rows}
    task_total = sum(task_counts.values())
    task_failure_rate = (task_counts.get("failed", 0) / task_total) if task_total else None

    # Stuck-loop heuristic (v1, intentionally simple): flag requests whose
    # rounds exceed 3x their source's own 30-day median. The median is a
    # percentile and is ALWAYS live (never rollup-derived, per Decision 2).
    # Sampled to the most recent 20k in-window rows — a v1 heuristic, not
    # exhaustive; revisit if this proves too coarse at real volume.
    median_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    median_rows = (await db.execute(
        select(
            TokenUsageLog.source.label("source"),
            func.percentile_cont(0.5).within_group(TokenUsageLog.rounds.asc()).label("median_rounds"),
        )
        .where(TokenUsageLog.created_at >= median_cutoff, TokenUsageLog.rounds.isnot(None))
        .group_by(TokenUsageLog.source)
    )).all()
    thresholds = {
        (r.source or "unknown"): float(r.median_rounds) * 3
        for r in median_rows if r.median_rounds is not None
    }
    stuck_where = [TokenUsageLog.created_at >= cutoff, TokenUsageLog.rounds.isnot(None)]
    if sources:
        stuck_where.append(TokenUsageLog.source.in_(sources))
    candidate_rows = (await db.execute(
        select(TokenUsageLog.source, TokenUsageLog.rounds)
        .where(*stuck_where)
        .order_by(TokenUsageLog.created_at.desc())
        .limit(20000)
    )).all()
    stuck_loop_count = sum(
        1 for r in candidate_rows
        if (r.source or "unknown") in thresholds
        and r.rounds > thresholds[r.source or "unknown"]
    )

    return {
        "days": days,
        "tool_error_buckets": tool_error_buckets,
        "approval_buckets": approval_buckets,
        "task_total": task_total,
        "task_failure_rate": task_failure_rate,
        "stuck_loop_count": stuck_loop_count,
        "stuck_loop_thresholds": thresholds,
    }


async def economics_report(
    db: AsyncSession, *, days: int = 30, sources: Optional[list[str]] = None,
) -> dict:
    """Area D: token usage (all calls — BYOK included; BYOK just means
    the price isn't counted), cost per request (all types), cost per
    successful task (only for types with a Task.status terminal signal —
    see the module's note on the conversation_id bridge). `sources`
    scopes only the per-request buckets (and the top-level total_tokens
    derived from them), not cost_per_successful_task (which joins
    against Task, which has no source column)."""
    days = _clamp_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if _use_live_aggregates(days):
        where = [TokenUsageLog.created_at >= cutoff]
        if sources:
            where.append(TokenUsageLog.source.in_(sources))
        rows = (await db.execute(
            select(
                TokenUsageLog.source.label("source"),
                func.count(TokenUsageLog.id).label("call_count"),
                func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("total_cost_usd"),
                func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
            )
            .where(*where)
            .group_by(TokenUsageLog.source)
        )).all()
    else:
        cutoff_day = cutoff.date()
        where = [MetricsDailyUsage.day >= cutoff_day]
        if sources:
            where.append(MetricsDailyUsage.source.in_(sources))
        rows = (await db.execute(
            select(
                MetricsDailyUsage.source.label("source"),
                func.sum(MetricsDailyUsage.call_count).label("call_count"),
                func.sum(MetricsDailyUsage.total_cost_usd).label("total_cost_usd"),
                func.sum(MetricsDailyUsage.total_tokens).label("total_tokens"),
            )
            .where(*where)
            .group_by(MetricsDailyUsage.source)
        )).all()

    # Cost estimation for metered providers always produces a positive
    # number, and BYOK calls store NULL cost_usd (estimation is
    # deliberately skipped for the user's own key). So a 0 cost sum
    # alongside recorded calls means "no cost data", not "free" — report
    # None so the frontend never shows a fake $0. Caveat: local models
    # (e.g. Ollama, priced $0/$0 in model_pricing.py) write genuine
    # cost_usd = 0.0 rows and will also read as "no cost data" — an
    # acceptable tradeoff for an admin page that is cloud-focused.
    # Token usage, by contrast, is real for EVERY call — BYOK only means
    # the price isn't counted, not that the tokens weren't consumed. So
    # total_tokens/tokens_per_request are always genuine sums (no
    # None-when-zero rule): a 0 with recorded calls would just mean the
    # provider reported no usage, which we surface as-is.
    buckets = []
    for r in rows:
        call_count = int(r.call_count)
        total_cost = float(r.total_cost_usd or 0)
        total_tokens = int(r.total_tokens or 0)
        has_cost_data = total_cost > 0
        buckets.append({
            "source": r.source or "unknown",
            "call_count": call_count,
            "total_tokens": total_tokens,
            "tokens_per_request": (total_tokens / call_count) if call_count else None,
            "total_cost_usd": total_cost if (has_cost_data or call_count == 0) else None,
            "cost_per_request": (total_cost / call_count) if (call_count and has_cost_data) else None,
        })

    # Pre-aggregate cost per conversation, windowed to `days` (the plan's
    # original join had no date filter on the TokenUsageLog side at all —
    # also fixed here). Each conversation's cost must be counted exactly
    # once even if it produced multiple completed tasks — a naive
    # Task-JOIN-TokenUsageLog fans out and multiplies a shared
    # conversation's cost once per matching task row.
    cost_by_conversation = (
        select(
            TokenUsageLog.conversation_id.label("conversation_id"),
            func.sum(TokenUsageLog.cost_usd).label("cost_usd"),
        )
        .where(TokenUsageLog.created_at >= cutoff, TokenUsageLog.conversation_id.isnot(None))
        .group_by(TokenUsageLog.conversation_id)
        .subquery()
    )

    completed_task_count = (await db.execute(
        select(func.count(Task.id))
        .where(
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
            Task.conversation_id.isnot(None),
        )
    )).scalar_one()
    completed_task_count = int(completed_task_count or 0)

    distinct_completed_conversations = (
        select(Task.conversation_id)
        .where(
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
            Task.conversation_id.isnot(None),
        )
        .distinct()
        .subquery()
    )
    total_cost_row = (await db.execute(
        select(func.coalesce(func.sum(cost_by_conversation.c.cost_usd), 0))
        .select_from(distinct_completed_conversations)
        .join(
            cost_by_conversation,
            cost_by_conversation.c.conversation_id == distinct_completed_conversations.c.conversation_id,
        )
    )).scalar_one()
    total_cost = float(total_cost_row or 0)

    # Same no-cost-data principle as the buckets above: completed tasks
    # whose conversations only have NULL-cost (BYOK) rows sum to 0, which
    # means "no cost data", not a genuine $0/task.
    cost_per_successful_task = (
        (total_cost / completed_task_count) if (completed_task_count and total_cost > 0) else None
    )

    return {
        "days": days,
        "buckets": buckets,
        "total_tokens": sum(b["total_tokens"] for b in buckets),
        "completed_task_count": completed_task_count,
        "cost_per_successful_task": cost_per_successful_task,
    }


# ── Daily trend series (sparklines) ──────────────────────────────────
#
# The four snapshot-aggregate queries above answer "what does the last
# N days look like as one number." The admin overview cards additionally
# need a day-by-day series for a 7-day sparkline — added in Part 4 of the
# rollout once the frontend spec called for it.


def _clamp_trend_days(days: int) -> int:
    """Sparklines are a recent-week glance, not a long-range trend — the
    detail page's own 30/90d chart (built on the Part 2 rollup) covers
    that. Capped at LIVE_WINDOW_DAYS so this always queries live, single
    grouped query, no N+1 per-day round trips."""
    return max(1, min(int(days), LIVE_WINDOW_DAYS))


async def efficiency_trend(db: AsyncSession, *, days: int = 7) -> list[dict]:
    """Daily p99 latency across all sources — Area A sparkline."""
    days = _clamp_trend_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_expr = func.date_trunc("day", func.timezone("UTC", TokenUsageLog.created_at))
    rows = (await db.execute(
        select(
            day_expr.label("day"),
            func.percentile_cont(0.99).within_group(TokenUsageLog.duration_ms.asc()).label("p99_duration_ms"),
        )
        .where(TokenUsageLog.created_at >= cutoff, TokenUsageLog.duration_ms.isnot(None))
        .group_by(day_expr)
        .order_by(day_expr)
    )).all()
    return [
        {"day": r.day.date().isoformat(), "value": float(r.p99_duration_ms) if r.p99_duration_ms is not None else None}
        for r in rows
    ]


async def discovery_trend(db: AsyncSession, *, days: int = 7) -> list[dict]:
    """Daily cross-tool dead-end rate — Area B sparkline."""
    days = _clamp_trend_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_expr = func.date_trunc("day", func.timezone("UTC", ToolCallLog.created_at))
    rows = (await db.execute(
        select(
            day_expr.label("day"),
            func.count(ToolCallLog.id).label("call_count"),
            func.sum(case((ToolCallLog.outcome == "error", 1), else_=0)).label("error_count"),
        )
        .where(ToolCallLog.created_at >= cutoff)
        .group_by(day_expr)
        .order_by(day_expr)
    )).all()
    return [
        {
            "day": r.day.date().isoformat(),
            "value": (int(r.error_count or 0) / int(r.call_count)) if r.call_count else None,
        }
        for r in rows
    ]


async def reliability_trend(db: AsyncSession, *, days: int = 7) -> list[dict]:
    """Daily tool error rate — Area C sparkline (same shape as the
    dead-end rate; kept as a separate function since the two areas may
    diverge later, e.g. if reliability's headline changes to task
    failure rate once task volume is high enough to be non-noisy)."""
    days = _clamp_trend_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_expr = func.date_trunc("day", func.timezone("UTC", ToolCallLog.created_at))
    rows = (await db.execute(
        select(
            day_expr.label("day"),
            func.count(ToolCallLog.id).label("call_count"),
            func.sum(case((ToolCallLog.outcome == "error", 1), else_=0)).label("error_count"),
        )
        .where(ToolCallLog.created_at >= cutoff)
        .group_by(day_expr)
        .order_by(day_expr)
    )).all()
    return [
        {
            "day": r.day.date().isoformat(),
            "value": (int(r.error_count or 0) / int(r.call_count)) if r.call_count else None,
        }
        for r in rows
    ]


async def economics_trend(db: AsyncSession, *, days: int = 7) -> list[dict]:
    """Daily cost per request plus daily token total, all sources combined
    — Area D sparkline. ``tokens`` is real for every call (BYOK included);
    ``value`` keeps its cost-per-request meaning and None semantics."""
    days = _clamp_trend_days(days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_expr = func.date_trunc("day", func.timezone("UTC", TokenUsageLog.created_at))
    rows = (await db.execute(
        select(
            day_expr.label("day"),
            func.count(TokenUsageLog.id).label("call_count"),
            func.coalesce(func.sum(TokenUsageLog.cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
        )
        .where(TokenUsageLog.created_at >= cutoff)
        .group_by(day_expr)
        .order_by(day_expr)
    )).all()
    # Same no-cost-data principle as economics_report: a day whose calls
    # all have NULL cost_usd (BYOK) sums to 0, which means "no cost data",
    # not a genuine $0 day — report None so the sparkline can skip it.
    # Tokens carry no such rule — a day only produces a row if calls
    # existed, so the sum is a genuine total.
    return [
        {
            "day": r.day.date().isoformat(),
            "value": (
                (float(r.total_cost_usd) / int(r.call_count))
                if (r.call_count and float(r.total_cost_usd or 0) > 0)
                else None
            ),
            "tokens": int(r.total_tokens or 0),
        }
        for r in rows
    ]


# ── System resource history ──────────────────────────────────────────
#
# Host CPU/mem/disk/load samples written every 30s by ops.collect_snapshot
# into system_metrics_samples (retained 14 days by the daily rollup).
# Unlike the four areas above there is no live/rollup split — the sample
# table IS the history; only the bucket width varies with the window.

SYSTEM_METRICS_MAX_HOURS = 336  # 14 days — matches the retention prune


def _system_point(row) -> dict:
    return {
        "ts": row.ts.isoformat(),
        "cpu_pct": float(row.cpu_pct) if row.cpu_pct is not None else None,
        "mem_pct": float(row.mem_pct) if row.mem_pct is not None else None,
        "disk_pct": float(row.disk_pct) if row.disk_pct is not None else None,
        "load_1m": float(row.load_1m) if row.load_1m is not None else None,
    }


async def system_metrics(db: AsyncSession, *, hours: int = 24) -> dict:
    """Host CPU/memory/disk/load history for the admin Performance page.

    Downsampled server-side (SQL avg-per-bucket, never Python thinning)
    to keep the payload chart-sized: ≤3h → raw 30s rows (≤360 points);
    ≤24h → 5-minute avg buckets (≤288); longer → hourly avg buckets
    (≤336). ``current`` echoes the last point (or None when the
    collector hasn't written any samples in the window).
    """
    hours = max(1, min(int(hours), SYSTEM_METRICS_MAX_HOURS))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if hours <= 3:
        rows = (await db.execute(
            select(
                SystemMetricsSample.sampled_at.label("ts"),
                SystemMetricsSample.cpu_pct.label("cpu_pct"),
                SystemMetricsSample.mem_pct.label("mem_pct"),
                SystemMetricsSample.disk_pct.label("disk_pct"),
                SystemMetricsSample.load_1m.label("load_1m"),
            )
            .where(SystemMetricsSample.sampled_at >= cutoff)
            .order_by(SystemMetricsSample.sampled_at)
        )).all()
    else:
        # Postgres date_trunc has no 5-minute unit — floor the epoch to
        # the bucket width instead (works uniformly for 300s and 3600s).
        bucket_s = 300 if hours <= 24 else 3600
        bucket_expr = func.to_timestamp(
            func.floor(func.extract("epoch", SystemMetricsSample.sampled_at) / bucket_s)
            * bucket_s
        )
        rows = (await db.execute(
            select(
                bucket_expr.label("ts"),
                func.avg(SystemMetricsSample.cpu_pct).label("cpu_pct"),
                func.avg(SystemMetricsSample.mem_pct).label("mem_pct"),
                func.avg(SystemMetricsSample.disk_pct).label("disk_pct"),
                func.avg(SystemMetricsSample.load_1m).label("load_1m"),
            )
            .where(SystemMetricsSample.sampled_at >= cutoff)
            .group_by(bucket_expr)
            .order_by(bucket_expr)
        )).all()

    points = [_system_point(r) for r in rows]
    return {
        "hours": hours,
        "points": points,
        "current": (points[-1] if points else None),
    }


# ── HTTP traffic history ─────────────────────────────────────────────
#
# Hourly request counts per (method, route template, status class),
# flushed from the middleware's Redis counters every 5 minutes into
# http_request_hourly (retained 90 days). Rows ARE the hour buckets, so
# no downsampling is needed — the cap alone bounds the payload.

TRAFFIC_MAX_HOURS = 720  # 30 days of hourly points; table retention is 90d


async def traffic_report(db: AsyncSession, *, hours: int = 24) -> dict:
    """HTTP traffic for the admin Performance page: an hourly
    count/count_5xx series (summed across paths), the window's request
    total + 5xx error rate, and the top 20 endpoints by volume.

    Rate fields are ``None`` (never a fabricated 0.0) when the
    denominator is 0. NOTE: the freshest bucket lags the middleware by
    up to one flush tick (≤5 min) — the counters live in Redis until
    ``metrics.http_flush`` syncs them.
    """
    hours = max(1, min(int(hours), TRAFFIC_MAX_HOURS))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    five_xx = case(
        (HttpRequestHourly.status_class == "5xx", HttpRequestHourly.count), else_=0,
    )

    point_rows = (await db.execute(
        select(
            HttpRequestHourly.hour.label("hour"),
            func.sum(HttpRequestHourly.count).label("count"),
            func.sum(five_xx).label("count_5xx"),
        )
        .where(HttpRequestHourly.hour >= cutoff)
        .group_by(HttpRequestHourly.hour)
        .order_by(HttpRequestHourly.hour)
    )).all()
    points = [
        {
            "hour": r.hour.isoformat(),
            "count": int(r.count or 0),
            "count_5xx": int(r.count_5xx or 0),
        }
        for r in point_rows
    ]
    total_requests = sum(p["count"] for p in points)
    total_5xx = sum(p["count_5xx"] for p in points)

    endpoint_rows = (await db.execute(
        select(
            HttpRequestHourly.method.label("method"),
            HttpRequestHourly.path.label("path"),
            func.sum(HttpRequestHourly.count).label("count"),
            func.sum(five_xx).label("count_5xx"),
        )
        .where(HttpRequestHourly.hour >= cutoff)
        .group_by(HttpRequestHourly.method, HttpRequestHourly.path)
        .order_by(func.sum(HttpRequestHourly.count).desc())
        .limit(20)
    )).all()
    top_endpoints = [
        {
            "method": r.method,
            "path": r.path,
            "count": int(r.count or 0),
            "rate_5xx": (int(r.count_5xx or 0) / int(r.count)) if r.count else None,
        }
        for r in endpoint_rows
    ]

    return {
        "hours": hours,
        "total_requests": total_requests,
        "error_5xx_rate": (total_5xx / total_requests) if total_requests else None,
        "points": points,
        "top_endpoints": top_endpoints,
    }
