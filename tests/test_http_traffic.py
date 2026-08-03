"""Tests for the HTTP traffic pipeline: the counting middleware
(``apps.api.middleware.http_stats`` — Redis hot path), the 5-minute
flush into ``http_request_hourly`` (``services.http_stats``), the
``metrics_query.traffic_report`` read query, and the 90-day retention
prune piggybacked on ``metrics_rollup.run_daily_rollup``.

All DB seeding uses ``flush()`` only (never commit) — the shared test DB
does not truncate committed rows between tests, so flush-only plus
unique marker paths keeps each test's window free of sibling leftovers.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.middleware import http_stats as http_stats_mw
from packages.core.models.base import generate_ulid
from packages.core.models.http_stats import HttpRequestHourly
from packages.core.services import http_stats as http_stats_svc


# ── Middleware (hot path) ─────────────────────────────────────────────


def _tiny_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(http_stats_mw.HttpStatsMiddleware)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str):
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


@pytest.fixture
def captured_increments(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        http_stats_mw, "_schedule_increment",
        lambda key, field: calls.append((key, field)),
    )
    return calls


@pytest.mark.asyncio
async def test_middleware_records_the_route_template_not_the_raw_path(captured_increments):
    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        resp = await c.get("/items/123")
    assert resp.status_code == 200

    assert len(captured_increments) == 1
    key, field = captured_increments[0]
    assert re.fullmatch(r"http:stats:\d{10}", key)
    assert key == http_stats_svc.hour_key()  # current UTC hour bucket
    assert field == "GET|/items/{item_id}|2xx"


@pytest.mark.asyncio
async def test_middleware_collapses_unrouted_requests_into_unmatched(captured_increments):
    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        for path in ("/no-such-route", "/scan/wp-admin.php", "/scan/etc/passwd"):
            resp = await c.get(path)
            assert resp.status_code == 404

    fields = [f for _k, f in captured_increments]
    # One field for all three scan paths — cardinality stays bounded.
    assert fields == ["GET|unmatched|4xx"] * 3


@pytest.mark.asyncio
async def test_middleware_skips_health_probes(captured_increments):
    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert captured_increments == []


@pytest.mark.asyncio
async def test_middleware_counts_crash_500s_and_reraises(captured_increments):
    """The catch-all 500 handler lives on Starlette's ServerErrorMiddleware
    OUTSIDE the user middleware stack, so an uncaught route exception
    passes through dispatch with no response at all. It must still be
    counted as a 5xx under its matched template — crash-500s are exactly
    what admins most want on the 5xx chart — and re-raised unchanged."""
    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        with pytest.raises(RuntimeError, match="kaboom"):
            await c.get("/boom")

    assert [f for _k, f in captured_increments] == ["GET|/boom|5xx"]


@pytest.mark.asyncio
async def test_middleware_is_fail_open_when_redis_is_down(monkeypatch):
    """A broken Redis (client factory raising) must not fail the request —
    goes through the REAL _schedule_increment/_increment pair so the
    fire-and-forget path is exercised end to end."""
    import packages.core.cache as cache_mod

    monkeypatch.setattr(http_stats_mw, "_backoff_until", 0.0)  # isolate from siblings

    async def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(cache_mod, "_get_redis", _boom)

    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        resp = await c.get("/items/ok-without-redis")
    assert resp.status_code == 200
    assert resp.json() == {"id": "ok-without-redis"}

    # Drain the fire-and-forget tasks: none may leak an exception.
    await asyncio.gather(*list(http_stats_mw._pending))


@pytest.mark.asyncio
async def test_middleware_backs_off_after_a_redis_failure(monkeypatch):
    """After one failed increment the middleware must not even SCHEDULE
    Redis work for the backoff window — cache._get_redis logs a warning
    and re-connects on every call when Redis is down, so per-request
    probing would turn an outage into warning spam proportional to
    traffic."""
    import time as time_mod

    import packages.core.cache as cache_mod

    monkeypatch.setattr(http_stats_mw, "_backoff_until", 0.0)

    async def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(cache_mod, "_get_redis", _boom)

    async with AsyncClient(transport=ASGITransport(app=_tiny_app()), base_url="http://t") as c:
        # Request 1: schedules the probe increment, which fails and arms
        # the backoff.
        resp = await c.get("/items/first")
        assert resp.status_code == 200
        await asyncio.gather(*list(http_stats_mw._pending))
        assert http_stats_mw._backoff_until > time_mod.monotonic()

        # Request 2 (inside the window): _schedule_increment must return
        # before creating any task — spy on _increment to prove it never
        # even started.
        scheduled: list[tuple[str, str]] = []

        async def _spy(key: str, field: str) -> None:
            scheduled.append((key, field))

        monkeypatch.setattr(http_stats_mw, "_increment", _spy)
        resp = await c.get("/items/second")
        assert resp.status_code == 200
        await asyncio.gather(*list(http_stats_mw._pending))
        assert scheduled == []


# ── Flush (Redis → Postgres) ─────────────────────────────────────────


def _fake_reader(monkeypatch, hashes: dict[str, dict[str, int]]) -> None:
    async def _read(keys):
        # The real flush only ever looks at the current hour + the
        # FLUSH_LOOKBACK_HOURS before it.
        assert keys == http_stats_svc._flush_keys()
        assert len(keys) == http_stats_svc.FLUSH_LOOKBACK_HOURS + 1
        return hashes

    monkeypatch.setattr(http_stats_svc, "_read_hour_hashes", _read)


@pytest.mark.asyncio
async def test_flush_upserts_absolute_counts_not_increments(db_session, monkeypatch):
    from sqlalchemy import select

    marker = f"/traffic-test/{generate_ulid()}/{{id}}"
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = http_stats_svc.hour_key(hour)
    hashes = {key: {f"GET|{marker}|2xx": 5, f"POST|{marker}|5xx": 2}}
    _fake_reader(monkeypatch, hashes)

    summary = await http_stats_svc.flush_http_stats(db_session)
    assert summary == {"keys_read": 1, "rows_upserted": 2}

    rows = (await db_session.execute(
        select(HttpRequestHourly).where(HttpRequestHourly.path == marker)
    )).scalars().all()
    assert {(r.method, r.status_class, r.count) for r in rows} == {
        ("GET", "2xx", 5), ("POST", "5xx", 2),
    }
    assert all(r.hour == hour for r in rows)

    # Redis total grew from 5 to 9; a re-flush SETS 9 (snapshot-sync),
    # never 5+9 — the running total lives in Redis, not the table.
    hashes[key][f"GET|{marker}|2xx"] = 9
    summary = await http_stats_svc.flush_http_stats(db_session)
    assert summary == {"keys_read": 1, "rows_upserted": 2}

    count = (await db_session.execute(
        select(HttpRequestHourly.count).where(
            HttpRequestHourly.path == marker,
            HttpRequestHourly.method == "GET",
            HttpRequestHourly.status_class == "2xx",
        )
    )).scalar_one()
    assert count == 9


@pytest.mark.asyncio
async def test_flush_skips_garbage_fields_and_empty_redis(db_session, monkeypatch):
    from sqlalchemy import select

    marker = f"/traffic-garbage/{generate_ulid()}"
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hashes = {http_stats_svc.hour_key(hour): {
        f"GET|{marker}|2xx": 3,
        "no-pipes-at-all": 7,   # unparseable → skipped, not fatal
        "||": 1,                # empty parts → skipped
    }}
    _fake_reader(monkeypatch, hashes)

    summary = await http_stats_svc.flush_http_stats(db_session)
    assert summary["rows_upserted"] == 1
    rows = (await db_session.execute(
        select(HttpRequestHourly).where(HttpRequestHourly.path == marker)
    )).scalars().all()
    assert len(rows) == 1 and rows[0].count == 3

    # Redis unavailable / nothing buffered → clean no-op.
    _fake_reader(monkeypatch, {})
    assert await http_stats_svc.flush_http_stats(db_session) == {
        "keys_read": 0, "rows_upserted": 0,
    }


# ── traffic_report read query ────────────────────────────────────────


@pytest.mark.asyncio
async def test_traffic_report_totals_points_and_top_endpoints(db_session):
    from packages.core.services.metrics_query import traffic_report

    marker = f"/traffic-report/{generate_ulid()}"
    now = datetime.now(timezone.utc)
    h0 = now.replace(minute=0, second=0, microsecond=0)
    h1 = h0 - timedelta(hours=1)
    # Huge counts so our buckets are guaranteed to lead top_endpoints even
    # with stray committed rows from sibling sessions in the shared DB.
    db_session.add_all([
        HttpRequestHourly(id=generate_ulid(), hour=h1, method="GET",
                          path=marker, status_class="2xx", count=3_000_000),
        HttpRequestHourly(id=generate_ulid(), hour=h1, method="GET",
                          path=marker, status_class="5xx", count=1_000_000),
        HttpRequestHourly(id=generate_ulid(), hour=h0, method="POST",
                          path=f"{marker}/b", status_class="2xx", count=2_000_000),
    ])
    await db_session.flush()

    data = await traffic_report(db_session, hours=24)

    assert data["hours"] == 24
    assert data["total_requests"] >= 6_000_000
    assert data["error_5xx_rate"] is not None and data["error_5xx_rate"] > 0

    # One point per hour bucket, ascending, summed across paths.
    hours_seq = [p["hour"] for p in data["points"]]
    assert hours_seq == sorted(hours_seq)
    assert len(hours_seq) == len(set(hours_seq))
    by_hour = {p["hour"]: p for p in data["points"]}
    p1 = by_hour[h1.isoformat()]
    assert p1["count"] >= 4_000_000
    assert p1["count_5xx"] >= 1_000_000

    # Exact values are safe per-endpoint: (method, path) is unique to us.
    top = data["top_endpoints"]
    assert len(top) <= 20
    assert top[0] == {
        "method": "GET", "path": marker, "count": 4_000_000,
        "rate_5xx": pytest.approx(0.25),
    }
    assert top[1] == {
        "method": "POST", "path": f"{marker}/b", "count": 2_000_000,
        "rate_5xx": 0.0,  # requests happened, none 5xx — a real 0, not None
    }


@pytest.mark.asyncio
async def test_traffic_report_clamps_hours(db_session):
    from packages.core.services.metrics_query import traffic_report

    assert (await traffic_report(db_session, hours=0))["hours"] == 1
    assert (await traffic_report(db_session, hours=-5))["hours"] == 1
    assert (await traffic_report(db_session, hours=99999))["hours"] == 720


# ── Retention (piggybacked on the daily rollup) ──────────────────────


def _hourly_row(*, at: datetime, path: str, count: int = 1) -> HttpRequestHourly:
    return HttpRequestHourly(
        id=generate_ulid(), hour=at, method="GET", path=path,
        status_class="2xx", count=count,
    )


@pytest.mark.asyncio
async def test_daily_rollup_prunes_http_hourly_older_than_90_days(db_session):
    from sqlalchemy import select

    from packages.core.services.metrics_rollup import run_daily_rollup

    marker = f"/traffic-retention/{generate_ulid()}"
    now = datetime.now(timezone.utc)
    old_ids = [generate_ulid(), generate_ulid()]
    recent_id = generate_ulid()
    db_session.add_all([
        HttpRequestHourly(id=old_ids[0], hour=now - timedelta(days=91), method="GET",
                          path=marker, status_class="2xx", count=1),
        HttpRequestHourly(id=old_ids[1], hour=now - timedelta(days=90, hours=1), method="POST",
                          path=marker, status_class="5xx", count=2),
        HttpRequestHourly(id=recent_id, hour=now - timedelta(days=89, hours=23), method="GET",
                          path=marker, status_class="4xx", count=3),
    ])
    await db_session.flush()

    summary = await run_daily_rollup(db_session)

    remaining = set((await db_session.execute(
        select(HttpRequestHourly.id).where(HttpRequestHourly.id.in_(old_ids + [recent_id]))
    )).scalars())
    assert remaining == {recent_id}
    # >= because stray committed rows from other sessions may also age out.
    assert summary["http_hourly_pruned"] >= 2


@pytest.mark.asyncio
async def test_daily_rollup_survives_an_http_prune_failure(db_session, monkeypatch):
    """An http_request_hourly prune error (e.g. table missing on a
    pre-migration DB mid-rolling-deploy) must not raise, must not poison
    that day's rollup upserts, and must not take the OTHER prune down —
    each prune runs in its own SAVEPOINT. Uses a REAL failing SQL
    statement so the savepoint isolation is exercised (a failed statement
    aborts the enclosing Postgres transaction unless nested)."""
    from datetime import date

    from sqlalchemy import select
    from sqlalchemy import text as sa_text

    from packages.core.models.metrics import MetricsDailyUsage
    from packages.core.models.usage import TokenUsageLog
    from packages.core.services import metrics_rollup as rollup_mod

    target = date(2026, 6, 11)  # day not used by any other rollup test
    db_session.add(TokenUsageLog(
        id=generate_ulid(), entity_id=generate_ulid(), model="gpt-5.5",
        total_tokens=321, cost_usd=0.01, source="chat",
        created_at=datetime(2026, 6, 11, 12, tzinfo=timezone.utc),
    ))
    await db_session.flush()

    real_delete = rollup_mod.delete

    def fake_delete(target_model):
        if target_model is HttpRequestHourly:
            return sa_text("DELETE FROM http_request_hourly_no_such_table")
        return real_delete(target_model)

    monkeypatch.setattr(rollup_mod, "delete", fake_delete)

    summary = await rollup_mod.run_daily_rollup(db_session, target_day="2026-06-11")

    assert summary["http_hourly_pruned"] is None  # "unknown", not 0
    # The sibling samples prune ran in its OWN savepoint and survived.
    assert summary["system_samples_pruned"] is not None
    assert summary["day"] == "2026-06-11"
    assert summary["usage_rows"] >= 1

    # The outer transaction survived the failed DELETE: the upsert row
    # is still queryable through the same session.
    row = (await db_session.execute(
        select(MetricsDailyUsage).where(
            MetricsDailyUsage.day == target, MetricsDailyUsage.source == "chat",
        )
    )).scalar_one()
    assert row.total_tokens >= 321
