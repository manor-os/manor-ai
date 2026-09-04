"""Tests for the four platform performance metrics query functions.
See docs/superpowers/specs/2026-07-26-platform-performance-metrics-design.md."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.usage import TokenUsageLog
from packages.core.services import metrics_query


def _usage_row(*, source: str, total_tokens: int, duration_ms: int, rounds=None, age_days=1, cost=0.01,
               prompt_tokens=0, cache_read_tokens=0):
    return TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=total_tokens, duration_ms=duration_ms, cost_usd=cost,
        source=source, rounds=rounds,
        prompt_tokens=prompt_tokens, cache_read_tokens=cache_read_tokens,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_efficiency_metrics_computes_live_percentiles_and_averages(db_session):
    for ms in (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
        db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=ms, rounds=2, age_days=1))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == "chat")
    assert bucket["call_count"] == 10
    assert bucket["p90_duration_ms"] is not None
    assert bucket["p99_duration_ms"] is not None
    assert bucket["avg_tokens_per_request"] == 100
    assert bucket["avg_rounds_per_request"] == 2


@pytest.mark.asyncio
async def test_efficiency_metrics_reports_no_data_for_sources_with_no_rounds(db_session):
    db_session.add(_usage_row(source="workflow", total_tokens=500, duration_ms=1000, rounds=None, age_days=1))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == "workflow")
    assert bucket["avg_rounds_per_request"] is None
    assert bucket["avg_tokens_per_request"] == 500


@pytest.mark.asyncio
async def test_efficiency_metrics_uses_rollup_beyond_the_live_window(db_session, monkeypatch):
    """Beyond 7 days, avg rounds/tokens come from the rollup table, not the
    raw log — seed ONLY a rollup row (no TokenUsageLog rows at all) and
    confirm the average still appears.

    Uses a randomly-generated ``source`` (rather than a literal like
    "chat") so this test's rollup row can't collide with committed rows
    from other test files (e.g. test_metrics_rollup.py, test_metrics_tasks.py)
    seeded on the same day when the whole suite runs in one pytest process
    — db_session doesn't truncate committed state between tests/files."""
    from packages.core.models.metrics import MetricsDailyUsage
    from datetime import date

    source = f"test-source-{generate_ulid()}"
    db_session.add(MetricsDailyUsage(
        id=generate_ulid(), day=date.today() - timedelta(days=10), source=source,
        call_count=4, total_tokens=400, total_cost_usd=0.04,
        rounds_sum=8, rounds_call_count=4,
    ))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=30)

    bucket = next((b for b in result["buckets"] if b["source"] == source), None)
    assert bucket is not None
    assert bucket["call_count"] == 4
    assert bucket["avg_tokens_per_request"] == 100
    assert bucket["avg_rounds_per_request"] == 2


@pytest.mark.asyncio
async def test_efficiency_metrics_filters_by_source(db_session):
    db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=100, age_days=1))
    db_session.add(_usage_row(source="workflow", total_tokens=200, duration_ms=200, age_days=1))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=7, sources=["chat"])

    assert {b["source"] for b in result["buckets"]} == {"chat"}


@pytest.mark.asyncio
async def test_efficiency_metrics_caps_days_at_90(db_session):
    result = await metrics_query.efficiency_metrics(db_session, days=365)
    assert result["days"] == 90


@pytest.mark.asyncio
async def test_efficiency_metrics_computes_cache_hit_rate(db_session):
    db_session.add(_usage_row(
        source="chat", total_tokens=100, duration_ms=100,
        prompt_tokens=600, cache_read_tokens=400, age_days=1,
    ))
    db_session.add(_usage_row(
        source="chat", total_tokens=100, duration_ms=100,
        prompt_tokens=400, cache_read_tokens=200, age_days=1,
    ))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == "chat")
    # 600 cache_read / 1000 prompt = 0.6, matching the /usage endpoint's
    # established cache_hit_rate definition.
    assert bucket["cache_hit_rate"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_efficiency_metrics_cache_hit_rate_is_none_without_prompt_tokens(db_session):
    db_session.add(_usage_row(
        source="workflow", total_tokens=100, duration_ms=100,
        prompt_tokens=0, cache_read_tokens=0, age_days=1,
    ))
    await db_session.flush()

    result = await metrics_query.efficiency_metrics(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == "workflow")
    # No prompt tokens recorded means "no data", never a fake 0% hit rate.
    assert bucket["cache_hit_rate"] is None


def _tool_call_row(*, tool_name: str, source: str, outcome: str, age_days=1):
    from packages.core.models.usage import ToolCallLog
    return ToolCallLog(
        id=generate_ulid(), entity_id=generate_ulid(), tool_name=tool_name,
        source=source, outcome=outcome, success=(outcome != "error"),
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_discovery_health_computes_search_hit_rate_and_dead_end_rate(db_session):
    db_session.add_all([
        _tool_call_row(tool_name="search_tools", source="chat", outcome="success"),
        _tool_call_row(tool_name="search_tools", source="chat", outcome="empty_result"),
        _tool_call_row(tool_name="search_tools", source="chat", outcome="error"),
        _tool_call_row(tool_name="send_email", source="chat", outcome="success"),
    ])
    await db_session.flush()

    result = await metrics_query.discovery_health(db_session, days=7)

    assert result["search_call_count"] == 3
    assert result["search_hit_rate"] == pytest.approx(1 - 1 / 3)
    assert result["total_tool_calls"] == 4
    assert result["tool_dead_end_rate"] == pytest.approx(1 / 4)


@pytest.mark.asyncio
async def test_discovery_health_reports_intent_path_suppression_rate(db_session):
    from packages.core.models.tool_path_memory import ToolIntentPath

    db_session.add_all([
        ToolIntentPath(
            id=generate_ulid(), entity_id=generate_ulid(), user_id=generate_ulid(),
            intent_signature="send email", provider="manor", tool_name="send_email",
            success_count=5, failure_count=0,
        ),
        ToolIntentPath(
            id=generate_ulid(), entity_id=generate_ulid(), user_id=generate_ulid(),
            intent_signature="fly to mars", provider="manor", tool_name="search_flights",
            success_count=1, failure_count=2,
        ),
    ])
    await db_session.flush()

    result = await metrics_query.discovery_health(db_session, days=7)

    assert result["intent_path_total"] == 2
    assert result["intent_path_suppression_rate"] == pytest.approx(0.5)
    assert result["intent_path_success_rate"] == pytest.approx(6 / 8)


@pytest.mark.asyncio
async def test_discovery_health_handles_zero_search_calls(db_session):
    result = await metrics_query.discovery_health(db_session, days=7)
    assert result["search_hit_rate"] is None
    assert result["tool_dead_end_rate"] is None


@pytest.mark.asyncio
async def test_reliability_metrics_computes_tool_error_rate(db_session):
    db_session.add_all([
        _tool_call_row(tool_name="send_email", source="chat", outcome="success"),
        _tool_call_row(tool_name="send_email", source="chat", outcome="error"),
    ])
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=7)

    bucket = next(b for b in result["tool_error_buckets"] if b["tool_name"] == "send_email")
    assert bucket["call_count"] == 2
    assert bucket["error_count"] == 1
    assert bucket["error_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reliability_metrics_computes_approval_denial_rate(db_session):
    from packages.core.models.hitl_request import HitlRequest

    now = datetime.now(timezone.utc)
    db_session.add_all([
        HitlRequest(
            id=generate_ulid(), entity_id=generate_ulid(), action_key="a.publish",
            resource_kind="platform", origin_kind="tool_call", status="denied",
            dedup_key=generate_ulid(), decided_at=now, created_at=now - timedelta(minutes=5),
        ),
        HitlRequest(
            id=generate_ulid(), entity_id=generate_ulid(), action_key="a.publish",
            resource_kind="platform", origin_kind="tool_call", status="granted",
            dedup_key=generate_ulid(), decided_at=now, created_at=now - timedelta(minutes=1),
        ),
    ])
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=7)

    bucket = next(b for b in result["approval_buckets"] if b["action_key"] == "a.publish")
    assert bucket["total"] == 2
    assert bucket["denied"] == 1
    assert bucket["denial_rate"] == pytest.approx(0.5)
    assert bucket["p90_pending_ms"] is not None


@pytest.mark.asyncio
async def test_reliability_metrics_computes_task_terminal_status_failure_rate(db_session):
    from packages.core.models.task import Task

    db_session.add_all([
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t1", status="completed"),
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t2", status="failed"),
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t3", status="completed"),
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t4", status="pending"),
    ])
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=7)

    assert result["task_total"] == 3  # "pending" isn't a terminal status
    assert result["task_failure_rate"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_reliability_metrics_excludes_waiting_on_customer_from_terminal_set(db_session):
    """waiting_on_customer is an active/in-flight status (a task can resume
    from it into in_progress/completed/failed) — it must not count as
    terminal in either the denominator or the numerator."""
    from packages.core.models.task import Task

    db_session.add_all([
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t1", status="completed"),
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t2", status="failed"),
        Task(id=generate_ulid(), entity_id=generate_ulid(), title="t3", status="waiting_on_customer"),
    ])
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=7)

    assert result["task_total"] == 2
    assert result["task_failure_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reliability_metrics_uses_rollup_for_tool_error_buckets_beyond_live_window(db_session):
    """Beyond 7 days, tool_error_buckets come from the MetricsDailyToolCalls
    rollup, not the raw ToolCallLog — seed ONLY a rollup row and confirm
    the bucket still appears with the right counts.

    Uses a randomly-generated ``tool_name`` (rather than a literal like
    "send_email") so this test's rollup row can't collide with committed
    rows from other test files seeded on the same day when the whole
    suite runs in one pytest process — db_session doesn't truncate
    committed state between tests/files."""
    from datetime import date

    from packages.core.models.metrics import MetricsDailyToolCalls

    tool_name = f"test-tool-{generate_ulid()}"
    db_session.add(MetricsDailyToolCalls(
        id=generate_ulid(), day=date.today() - timedelta(days=10),
        tool_name=tool_name, source="chat",
        call_count=8, error_count=2, empty_result_count=0,
    ))
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=30)

    bucket = next(b for b in result["tool_error_buckets"] if b["tool_name"] == tool_name)
    assert bucket["call_count"] == 8
    assert bucket["error_count"] == 2
    assert bucket["error_rate"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_reliability_metrics_flags_stuck_loop_requests(db_session):
    """A request with far more rounds than its source's own 30-day median
    (3x threshold, per the v1 heuristic) counts as stuck."""
    for _ in range(5):
        db_session.add(_usage_row(source="chat", total_tokens=10, duration_ms=100, rounds=2, age_days=10))
    db_session.add(_usage_row(source="chat", total_tokens=10, duration_ms=100, rounds=10, age_days=1))
    await db_session.flush()

    result = await metrics_query.reliability_metrics(db_session, days=7)

    assert result["stuck_loop_count"] == 1


@pytest.mark.asyncio
async def test_reliability_metrics_reports_no_data_when_no_terminal_signal_exists(db_session):
    """No Task rows at all -> task_failure_rate is None, never a
    fabricated 100% or 0% (Decision 1)."""
    result = await metrics_query.reliability_metrics(db_session, days=7)
    assert result["task_failure_rate"] is None
    assert result["task_total"] == 0


@pytest.mark.asyncio
async def test_economics_report_computes_cost_per_request(db_session):
    db_session.add_all([
        _usage_row(source="chat", total_tokens=100, duration_ms=100, cost=0.02, age_days=1),
        _usage_row(source="chat", total_tokens=100, duration_ms=100, cost=0.04, age_days=1),
    ])
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == "chat")
    assert bucket["call_count"] == 2
    assert bucket["total_cost_usd"] == pytest.approx(0.06)
    assert bucket["cost_per_request"] == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_economics_report_computes_cost_per_successful_task(db_session):
    from packages.core.models.task import Task

    conversation_id = generate_ulid()
    db_session.add(Task(
        id=generate_ulid(), entity_id=generate_ulid(), title="t1",
        status="completed", conversation_id=conversation_id,
    ))
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=100, cost_usd=0.10, source="workflow",
        conversation_id=conversation_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7)

    assert result["completed_task_count"] == 1
    assert result["cost_per_successful_task"] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_economics_report_is_none_when_no_completed_tasks_exist(db_session):
    result = await metrics_query.economics_report(db_session, days=7)
    assert result["cost_per_successful_task"] is None
    assert result["completed_task_count"] == 0


@pytest.mark.asyncio
async def test_economics_report_does_not_double_count_cost_across_multiple_tasks_per_conversation(db_session):
    from packages.core.models.task import Task

    conv_a = generate_ulid()
    conv_b = generate_ulid()
    db_session.add(Task(id=generate_ulid(), entity_id=generate_ulid(), title="a1", status="completed", conversation_id=conv_a))
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=100, cost_usd=10.0, source="workflow", conversation_id=conv_a,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    for _ in range(3):
        db_session.add(Task(id=generate_ulid(), entity_id=generate_ulid(), title="b", status="completed", conversation_id=conv_b))
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=100, cost_usd=10.0, source="workflow", conversation_id=conv_b,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7)

    assert result["completed_task_count"] == 4
    assert result["cost_per_successful_task"] == pytest.approx(5.0)  # $20 total / 4 tasks, not $10


@pytest.mark.asyncio
async def test_economics_report_reports_no_data_when_all_costs_are_null(db_session):
    """BYOK calls store cost_usd=NULL (cost estimation is skipped for the
    user's own key) and real estimation always produces a positive number
    — so a 0 cost sum alongside recorded calls means "no cost data", not
    "these calls were free". The bucket must say None, never $0."""
    source = f"byok-source-{generate_ulid()}"
    db_session.add_all([
        _usage_row(source=source, total_tokens=100, duration_ms=100, cost=None, age_days=1),
        _usage_row(source=source, total_tokens=100, duration_ms=100, cost=None, age_days=1),
    ])
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7)

    bucket = next(b for b in result["buckets"] if b["source"] == source)
    assert bucket["call_count"] == 2
    assert bucket["total_cost_usd"] is None
    assert bucket["cost_per_request"] is None


@pytest.mark.asyncio
async def test_economics_report_reports_token_usage_even_when_costs_are_null(db_session):
    """Product decision: Economics shows token usage for EVERY call — BYOK
    only means the price isn't counted. A bucket whose calls are all
    NULL-cost (BYOK) still reports real total_tokens/tokens_per_request
    while the cost fields keep their None ("no cost data") semantics."""
    source = f"byok-source-{generate_ulid()}"
    db_session.add_all([
        _usage_row(source=source, total_tokens=1200, duration_ms=100, cost=None, age_days=1),
        _usage_row(source=source, total_tokens=800, duration_ms=100, cost=None, age_days=1),
    ])
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7, sources=[source])

    bucket = next(b for b in result["buckets"] if b["source"] == source)
    assert bucket["call_count"] == 2
    assert bucket["total_tokens"] == 2000
    assert bucket["tokens_per_request"] == pytest.approx(1000)
    assert bucket["total_cost_usd"] is None
    assert bucket["cost_per_request"] is None
    # Top-level total across (the filtered) buckets.
    assert result["total_tokens"] == 2000


@pytest.mark.asyncio
async def test_economics_report_sums_tokens_from_rollup_beyond_live_window(db_session):
    """Beyond 7 days the token sum comes from the MetricsDailyUsage rollup
    (same hybrid rule as the cost fields). Random source so this test's
    rollup row can't collide with committed rows from other test files."""
    from datetime import date

    from packages.core.models.metrics import MetricsDailyUsage

    source = f"test-source-{generate_ulid()}"
    db_session.add(MetricsDailyUsage(
        id=generate_ulid(), day=date.today() - timedelta(days=10), source=source,
        call_count=4, total_tokens=400, total_cost_usd=0.04,
        rounds_sum=8, rounds_call_count=4,
    ))
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=30, sources=[source])

    bucket = next(b for b in result["buckets"] if b["source"] == source)
    assert bucket["total_tokens"] == 400
    assert bucket["tokens_per_request"] == pytest.approx(100)
    assert result["total_tokens"] == 400


@pytest.mark.asyncio
async def test_economics_report_cost_per_successful_task_is_none_when_costs_are_null(db_session):
    """Same BYOK principle for the task-join metric: a completed task whose
    conversation only has NULL-cost rows reports None, not $0/task."""
    from packages.core.models.task import Task

    conversation_id = generate_ulid()
    db_session.add(Task(
        id=generate_ulid(), entity_id=generate_ulid(), title="t1",
        status="completed", conversation_id=conversation_id,
    ))
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=100, cost_usd=None, source="workflow",
        conversation_id=conversation_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    result = await metrics_query.economics_report(db_session, days=7)

    assert result["completed_task_count"] == 1
    assert result["cost_per_successful_task"] is None


@pytest.mark.asyncio
async def test_efficiency_trend_returns_one_point_per_day(db_session):
    for age in (0, 1, 2):
        db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=500, age_days=age))
    await db_session.flush()

    points = await metrics_query.efficiency_trend(db_session, days=7)

    assert len(points) >= 1
    assert all("day" in p and "value" in p for p in points)


@pytest.mark.asyncio
async def test_discovery_trend_computes_daily_dead_end_rate(db_session):
    db_session.add_all([
        _tool_call_row(tool_name="send_email", source="chat", outcome="success", age_days=0),
        _tool_call_row(tool_name="send_email", source="chat", outcome="error", age_days=0),
    ])
    await db_session.flush()

    points = await metrics_query.discovery_trend(db_session, days=7)

    today_point = next((p for p in points if p["value"] is not None), None)
    assert today_point is not None
    assert today_point["value"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reliability_trend_computes_daily_tool_error_rate(db_session):
    db_session.add_all([
        _tool_call_row(tool_name="send_email", source="chat", outcome="success", age_days=0),
        _tool_call_row(tool_name="send_email", source="chat", outcome="error", age_days=0),
    ])
    await db_session.flush()

    points = await metrics_query.reliability_trend(db_session, days=7)

    today_point = next((p for p in points if p["value"] is not None), None)
    assert today_point is not None
    assert today_point["value"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_economics_trend_computes_daily_cost_per_request(db_session):
    db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=100, cost=0.05, age_days=0))
    await db_session.flush()

    points = await metrics_query.economics_trend(db_session, days=7)

    today_point = next((p for p in points if p["value"] is not None), None)
    assert today_point is not None
    assert today_point["value"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_economics_trend_reports_none_for_days_with_null_costs(db_session):
    """A day whose calls are all BYOK (NULL cost) must yield value=None so
    the frontend can distinguish "no cost data" from a genuine $0 day."""
    db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=100, cost=None, age_days=0))
    await db_session.flush()

    points = await metrics_query.economics_trend(db_session, days=7)

    today = datetime.now(timezone.utc).date().isoformat()
    today_point = next(p for p in points if p["day"] == today)
    assert today_point["value"] is None


@pytest.mark.asyncio
async def test_economics_trend_carries_daily_tokens_alongside_null_cost(db_session):
    """Each trend point also reports that day's total tokens — real for
    every call including BYOK — while `value` (cost/request) keeps its
    None-when-no-cost-data semantics untouched."""
    db_session.add(_usage_row(source="chat", total_tokens=700, duration_ms=100, cost=None, age_days=0))
    await db_session.flush()

    points = await metrics_query.economics_trend(db_session, days=7)

    today = datetime.now(timezone.utc).date().isoformat()
    today_point = next(p for p in points if p["day"] == today)
    # >= not ==: the shared test DB may hold committed null-cost rows from
    # other suite files on the same day (db_session is flush-only).
    assert today_point["tokens"] >= 700
    assert isinstance(today_point["tokens"], int)
    assert today_point["value"] is None


@pytest.mark.asyncio
async def test_trend_functions_cap_at_the_live_window(db_session):
    for age in (0, 2, 4, 6, 8, 10, 12):
        db_session.add(_usage_row(source="chat", total_tokens=100, duration_ms=500, age_days=age))
    await db_session.flush()

    points = await metrics_query.efficiency_trend(db_session, days=90)

    assert len(points) <= metrics_query.LIVE_WINDOW_DAYS + 1
    earliest_included = (datetime.now(timezone.utc) - timedelta(days=metrics_query.LIVE_WINDOW_DAYS)).date()
    assert all(datetime.fromisoformat(p["day"]).date() >= earliest_included for p in points)
