"""Tests for the daily metrics rollup — aggregation correctness and
idempotent upsert. See docs/superpowers/specs/2026-07-26-platform-performance-metrics-design.md."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.metrics import MetricsDailyToolCalls, MetricsDailyUsage
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.services.metrics_rollup import run_daily_rollup


def _token_row(*, day: date, source: str, total_tokens: int, rounds=None, cost=0.01):
    hour = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    return TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=total_tokens, cost_usd=cost, source=source, rounds=rounds,
        created_at=hour,
    )


def _tool_row(*, day: date, tool_name: str, source: str, outcome: str, duration_ms: int = 100):
    hour = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    return ToolCallLog(
        id=generate_ulid(), entity_id=generate_ulid(), tool_name=tool_name,
        source=source, outcome=outcome, success=(outcome != "error"),
        duration_ms=duration_ms, created_at=hour,
    )


@pytest.mark.asyncio
async def test_rollup_aggregates_token_usage_by_source(db_session):
    target = date(2026, 7, 15)
    db_session.add_all([
        _token_row(day=target, source="chat", total_tokens=100, rounds=2),
        _token_row(day=target, source="chat", total_tokens=50, rounds=4),
        _token_row(day=target, source="chat", total_tokens=10, rounds=None),
        _token_row(day=target, source="workflow", total_tokens=200),
    ])
    await db_session.flush()

    result = await run_daily_rollup(db_session, target_day="2026-07-15")
    await db_session.commit()

    row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).scalar_one()
    assert row.call_count == 3
    assert row.total_tokens == 160
    assert row.rounds_sum == 6
    assert row.rounds_call_count == 2  # the rounds=None row doesn't count

    workflow_row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "workflow",
        )
    )).scalar_one()
    assert workflow_row.call_count == 1
    assert workflow_row.total_tokens == 200
    assert result["usage_rows"] == 2


@pytest.mark.asyncio
async def test_rollup_excludes_rows_outside_the_day_boundary(db_session):
    # Uses a day distinct from test_rollup_aggregates_token_usage_by_source:
    # db_session doesn't truncate between tests (committed rows persist for
    # the life of the test-session Postgres DB — see conftest.py), so two
    # tests sharing a day would double-count each other's raw log rows.
    target = date(2026, 7, 20)
    db_session.add_all([
        _token_row(day=target, source="chat", total_tokens=100),
        TokenUsageLog(
            id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
            total_tokens=999, cost_usd=0, source="chat",
            created_at=datetime(2026, 7, 19, 23, 59, tzinfo=timezone.utc),
        ),
        TokenUsageLog(
            id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
            total_tokens=999, cost_usd=0, source="chat",
            created_at=datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
        ),
    ])
    await db_session.flush()

    await run_daily_rollup(db_session, target_day="2026-07-20")
    await db_session.commit()

    row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).scalar_one()
    assert row.total_tokens == 100


@pytest.mark.asyncio
async def test_rollup_aggregates_tool_calls_by_tool_and_source(db_session):
    target = date(2026, 7, 16)
    db_session.add_all([
        _tool_row(day=target, tool_name="search_tools", source="chat", outcome="success"),
        _tool_row(day=target, tool_name="search_tools", source="chat", outcome="empty_result"),
        _tool_row(day=target, tool_name="search_tools", source="chat", outcome="error"),
        _tool_row(day=target, tool_name="send_email", source="chat", outcome="success"),
    ])
    await db_session.flush()

    result = await run_daily_rollup(db_session, target_day="2026-07-16")
    await db_session.commit()

    row = (await db_session.execute(
        select(MetricsDailyToolCalls).where(
            MetricsDailyToolCalls.day == target,
            MetricsDailyToolCalls.tool_name == "search_tools",
            MetricsDailyToolCalls.source == "chat",
        )
    )).scalar_one()
    assert row.call_count == 3
    assert row.error_count == 1
    assert row.empty_result_count == 1
    assert result["tool_call_rows"] == 2


@pytest.mark.asyncio
async def test_rollup_upsert_is_idempotent(db_session):
    target = date(2026, 7, 17)
    db_session.add(_token_row(day=target, source="chat", total_tokens=100))
    await db_session.flush()

    await run_daily_rollup(db_session, target_day="2026-07-17")
    await run_daily_rollup(db_session, target_day="2026-07-17")
    await db_session.commit()

    rows = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).all()
    assert len(rows) == 1
    assert rows[0][0].total_tokens == 100  # not doubled


@pytest.mark.asyncio
async def test_rollup_upsert_refreshes_values_on_rerun(db_session):
    """A second run for the same day after new rows landed must update the
    row in place, not skip it."""
    target = date(2026, 7, 18)
    db_session.add(_token_row(day=target, source="chat", total_tokens=100))
    await db_session.flush()
    await run_daily_rollup(db_session, target_day="2026-07-18")

    db_session.add(_token_row(day=target, source="chat", total_tokens=50))
    await db_session.flush()
    await run_daily_rollup(db_session, target_day="2026-07-18")
    await db_session.commit()

    row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).scalar_one()
    assert row.call_count == 2
    assert row.total_tokens == 150


@pytest.mark.asyncio
async def test_rollup_buckets_null_source_rows_as_unknown(db_session):
    """record_llm_usage's source param is genuinely optional at the call
    site, but metrics_daily_usage/metrics_daily_tool_calls.source is
    NOT NULL — a NULL-source raw row must land in an "unknown" bucket
    rather than fail the upsert.

    Uses 2026-07-25 rather than 2026-07-21: the latter's day-start
    (2026-07-21T00:00) is exactly the "next day" boundary sentinel row
    planted by test_rollup_excludes_rows_outside_the_day_boundary (day
    2026-07-20), so it isn't actually free of other tests' raw rows.
    """
    target = date(2026, 7, 25)
    hour = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    db_session.add_all([
        TokenUsageLog(
            id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
            total_tokens=100, cost_usd=0.01, source=None, created_at=hour,
        ),
        ToolCallLog(
            id=generate_ulid(), entity_id=generate_ulid(), tool_name="search_tools",
            source=None, outcome="success", success=True, duration_ms=100,
            created_at=hour,
        ),
    ])
    await db_session.flush()

    result = await run_daily_rollup(db_session, target_day="2026-07-25")
    await db_session.commit()

    usage_row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "unknown",
        )
    )).scalar_one()
    assert usage_row.call_count == 1
    assert usage_row.total_tokens == 100

    tool_row = (await db_session.execute(
        select(MetricsDailyToolCalls).where(
            MetricsDailyToolCalls.day == target,
            MetricsDailyToolCalls.tool_name == "search_tools",
            MetricsDailyToolCalls.source == "unknown",
        )
    )).scalar_one()
    assert tool_row.call_count == 1
    assert result["usage_rows"] == 1
    assert result["tool_call_rows"] == 1


def test_default_target_day_is_yesterday_utc(monkeypatch):
    from packages.core.services import metrics_rollup

    monkeypatch.setattr(
        metrics_rollup, "_utcnow",
        lambda: datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc),
    )
    assert metrics_rollup._resolve_target_day(None) == date(2026, 7, 29)
