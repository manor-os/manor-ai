"""Tests for the system resource history pipeline: the sample writer
(``services.system_metrics.record_system_sample``, called by
``ops.collect_snapshot``), the ``metrics_query.system_metrics`` read
query with its server-side downsampling, and the 14-day retention prune
piggybacked on ``metrics_rollup.run_daily_rollup``.

All seeding uses ``flush()`` only (never commit) — the shared test DB
does not truncate committed rows between tests, so flush-only keeps each
test's now-anchored window free of leftovers from its siblings.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.system_metrics import SystemMetricsSample
from packages.core.services.metrics_query import system_metrics
from packages.core.services.metrics_rollup import run_daily_rollup
from packages.core.services.system_metrics import record_system_sample


def _snapshot(*, ts=None, cpu=42.5, mem=61.2, disk=73.4, load1=1.25) -> dict:
    """A realistic ``ops_service.collect_snapshot()`` dict (host shape
    copied from ``collect_host_metrics``)."""
    return {
        "ts": ts if ts is not None else int(time.time()),
        "host": {
            "cpu_pct": cpu,
            "cpu_count": 8,
            "mem": {"total_mb": 16000, "used_mb": 9800, "available_mb": 6200, "pct": mem},
            "disk": {"total_gb": 500.0, "used_gb": 367.0, "free_gb": 133.0, "pct": disk},
            "load_avg": [load1, 0.9, 0.7],
            "uptime_seconds": 12345,
            "net_io": {"bytes_sent": 100, "bytes_recv": 200},
        },
        "containers": [],
        "queues": {},
    }


def _sample(*, at: datetime, cpu=None, mem=None, disk=None, load=None) -> SystemMetricsSample:
    return SystemMetricsSample(
        id=generate_ulid(), sampled_at=at,
        cpu_pct=cpu, mem_pct=mem, disk_pct=disk, load_1m=load,
    )


# ── Writer ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_system_sample_persists_the_host_gauges(db_session):
    ts = int(time.time())
    row = await record_system_sample(db_session, _snapshot(ts=ts))
    assert row is not None

    fetched = (await db_session.execute(
        select(SystemMetricsSample).where(SystemMetricsSample.id == row.id)
    )).scalar_one()
    assert fetched.cpu_pct == 42.5
    assert fetched.mem_pct == 61.2
    assert fetched.disk_pct == 73.4
    assert fetched.load_1m == 1.25
    assert fetched.sampled_at == datetime.fromtimestamp(ts, tz=timezone.utc)


@pytest.mark.asyncio
async def test_record_system_sample_skips_a_gaugeless_snapshot(db_session):
    """psutil-unavailable snapshots (host == {"error": ...}) and outright
    garbage produce NO row — an all-None row would only add chart noise."""
    for snapshot in (
        {"ts": int(time.time()), "host": {"error": "psutil_unavailable"}},
        {"ts": int(time.time())},
        {},
        None,
        {"host": "not-a-dict"},
    ):
        assert await record_system_sample(db_session, snapshot) is None


@pytest.mark.asyncio
async def test_record_system_sample_tolerates_partial_or_malformed_hosts(db_session):
    """A partially-usable snapshot still yields a row, with None for the
    missing/malformed gauges — never an exception."""
    snap = _snapshot()
    snap["host"]["mem"] = "oops"          # malformed sub-dict
    del snap["host"]["disk"]              # missing sub-dict
    snap["host"]["load_avg"] = "high"     # malformed list

    row = await record_system_sample(db_session, snap)
    assert row is not None
    assert row.cpu_pct == 42.5
    assert row.mem_pct is None
    assert row.disk_pct is None
    assert row.load_1m is None

    # Numeric-looking strings are NOT numbers — coerced to None, and the
    # remaining real gauges still produce a row.
    snap2 = _snapshot()
    snap2["host"]["cpu_pct"] = "42.5"
    row2 = await record_system_sample(db_session, snap2)
    assert row2 is not None
    assert row2.cpu_pct is None
    assert row2.disk_pct == 73.4


@pytest.mark.asyncio
async def test_record_system_sample_falls_back_to_now_without_ts(db_session):
    snap = _snapshot()
    del snap["ts"]
    before = datetime.now(timezone.utc)
    row = await record_system_sample(db_session, snap)
    after = datetime.now(timezone.utc)
    assert row is not None
    assert before <= row.sampled_at <= after


# ── Read query ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_metrics_returns_ordered_raw_points_for_short_windows(db_session):
    now = datetime.now(timezone.utc)
    # Unique-ish marker: other tests in the process may have COMMITTED
    # now-anchored samples into the shared DB (e.g. the MCP mirror test),
    # so identify our rows by value instead of asserting a global count.
    marker = round(70.0 + (time.time() % 1) / 10, 6)
    seeded = []
    for i in range(10):
        at = now - timedelta(minutes=115 - i * 12)  # oldest first, all within 2h
        seeded.append(_sample(at=at, cpu=10.0 + i, mem=50.0 + i, disk=marker, load=0.5))
    # Add out of order to prove ordering comes from the query, not insert order.
    db_session.add_all(reversed(seeded))
    await db_session.flush()

    data = await system_metrics(db_session, hours=3)
    assert data["hours"] == 3
    points = data["points"]
    assert [p["ts"] for p in points] == sorted(p["ts"] for p in points)
    mine = [p for p in points if p["disk_pct"] == marker]
    assert len(mine) == 10
    assert [p["cpu_pct"] for p in mine] == [10.0 + i for i in range(10)]
    assert mine[0]["mem_pct"] == 50.0
    assert mine[0]["load_1m"] == 0.5
    assert data["current"] == points[-1]


@pytest.mark.asyncio
async def test_system_metrics_clamps_hours(db_session):
    assert (await system_metrics(db_session, hours=0))["hours"] == 1
    assert (await system_metrics(db_session, hours=99999))["hours"] == 336
    empty = await system_metrics(db_session, hours=3000)
    assert empty["current"] is None or isinstance(empty["current"], dict)


@pytest.mark.asyncio
async def test_system_metrics_downsamples_a_24h_window_into_5min_buckets(db_session):
    now = datetime.now(timezone.utc)

    # Bulk: 500 rows at 60s spacing, ending ~14h ago — far from the
    # hand-computed bucket below.
    bulk_start = now - timedelta(hours=23)
    db_session.add_all([
        _sample(at=bulk_start + timedelta(minutes=i), cpu=50.0, mem=60.0, disk=70.0, load=1.0)
        for i in range(500)
    ])

    # Hand-computable bucket: 3 rows inside one aligned 5-minute bucket
    # ~3h ago, cpu 10/20/30 → avg 20.
    bucket_start_epoch = (int((now - timedelta(hours=3)).timestamp()) // 300) * 300
    bucket_start = datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)
    db_session.add_all([
        _sample(at=bucket_start + timedelta(seconds=s), cpu=c, mem=40.0, disk=70.0, load=2.0)
        for s, c in ((30, 10.0), (90, 20.0), (150, 30.0))
    ])
    await db_session.flush()

    data = await system_metrics(db_session, hours=24)
    points = data["points"]
    # 503 raw rows collapse into <= ~105 five-minute buckets — well under
    # the ~300-point payload budget and far fewer than the raw row count.
    assert len(points) <= 300
    assert len(points) < 503

    target_ts = bucket_start.isoformat()
    matching = [p for p in points if p["ts"] == target_ts]
    assert len(matching) == 1, f"expected exactly one point at {target_ts}"
    assert matching[0]["cpu_pct"] == pytest.approx(20.0)
    assert matching[0]["mem_pct"] == pytest.approx(40.0)
    assert matching[0]["load_1m"] == pytest.approx(2.0)
    assert data["current"] == points[-1]


# ── Retention (piggybacked on the daily rollup) ──────────────────────


@pytest.mark.asyncio
async def test_daily_rollup_prunes_samples_older_than_14_days(db_session):
    now = datetime.now(timezone.utc)
    old_ids = [generate_ulid(), generate_ulid()]
    recent_id = generate_ulid()
    db_session.add_all([
        SystemMetricsSample(id=old_ids[0], sampled_at=now - timedelta(days=15), cpu_pct=1.0),
        SystemMetricsSample(id=old_ids[1], sampled_at=now - timedelta(days=14, hours=1), cpu_pct=2.0),
        SystemMetricsSample(id=recent_id, sampled_at=now - timedelta(days=13, hours=23), cpu_pct=3.0),
    ])
    await db_session.flush()

    summary = await run_daily_rollup(db_session)

    remaining = set((await db_session.execute(
        select(SystemMetricsSample.id).where(SystemMetricsSample.id.in_(old_ids + [recent_id]))
    )).scalars())
    assert remaining == {recent_id}
    # >= because stray committed rows from other sessions may also age out.
    assert summary["system_samples_pruned"] >= 2


@pytest.mark.asyncio
async def test_daily_rollup_survives_a_prune_failure(db_session, monkeypatch):
    """A prune error (e.g. samples table missing on a pre-migration DB
    mid-rolling-deploy) must not raise or poison that day's rollup
    upserts. Uses a REAL failing SQL statement (missing table) rather
    than a Python-raised stub so the SAVEPOINT isolation is exercised —
    on Postgres a failed statement aborts the enclosing transaction
    unless it ran inside a savepoint."""
    from datetime import date

    from sqlalchemy import text as sa_text

    from packages.core.models.metrics import MetricsDailyUsage
    from packages.core.models.usage import TokenUsageLog
    from packages.core.services import metrics_rollup as rollup_mod

    target = date(2026, 6, 10)  # day not used by any other rollup test
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=123, cost_usd=0.01, source="chat",
        created_at=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
    ))
    await db_session.flush()

    monkeypatch.setattr(
        rollup_mod, "delete",
        lambda *a, **k: sa_text("DELETE FROM system_metrics_samples_no_such_table"),
    )

    summary = await run_daily_rollup(db_session, target_day="2026-06-10")

    assert summary["system_samples_pruned"] is None  # "unknown", not 0
    assert summary["day"] == "2026-06-10"
    assert summary["usage_rows"] >= 1
    assert "tool_call_rows" in summary

    # The outer transaction survived the failed DELETE: the upsert row
    # is still queryable through the same session.
    row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).scalar_one()
    assert row.total_tokens >= 123
