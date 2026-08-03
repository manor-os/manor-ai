"""Daily rollup tables for platform performance metrics.

Additive-only fields (counts/sums) so a daily row can be safely summed
across a date range for a multi-day trend view — this table never stores
a pre-aggregated percentile (p90/p99/median are recomputed live over the
raw tables every time; see the design spec's Decision 2 for why summing
daily percentiles would be mathematically wrong). Populated once a day by
``packages.core.services.metrics_rollup.run_daily_rollup``, scheduled via
the ``metrics.daily_rollup`` Celery beat task
(``packages/core/tasks/metrics_tasks.py``). Platform-wide (no
``entity_id``) — this is an admin rollup, not a per-tenant one.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, Integer, Numeric, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class MetricsDailyUsage(Base):
    """One row per (day, source): LLM token/cost/rounds totals."""
    __tablename__ = "metrics_daily_usage"
    __table_args__ = (
        UniqueConstraint("day", "source", name="uq_metrics_daily_usage_day_source"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    total_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    rounds_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    rounds_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Denominator for avg rounds/request — only rows with a non-null
    ``TokenUsageLog.rounds`` contribute (chat-only until Part 1's "out
    of scope" note is revisited for other sources)."""
    cache_read_tokens_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_creation_tokens_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class MetricsDailyToolCalls(Base):
    """One row per (day, tool_name, source): tool-call outcome totals."""
    __tablename__ = "metrics_daily_tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "day", "tool_name", "source", name="uq_metrics_daily_tool_calls_day_tool_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    empty_result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    """Combinable for an average (``duration_ms_sum / call_count``) — never
    a percentile; p90/p99 latency stays a live query (Part 3)."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
