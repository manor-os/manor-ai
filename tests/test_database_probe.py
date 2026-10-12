"""Overlapping health checks share bounded work, not cached health."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from apps.api.routers import health


@pytest.mark.asyncio
async def test_overlapping_probes_share_one_read_and_cancel_independently(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = closes = 0

    @asynccontextmanager
    async def factory(**kwargs):
        nonlocal calls, closes
        assert kwargs == {"info": {"database_probe": True}}
        calls += 1

        async def execute(_):
            entered.set()
            await release.wait()

        try:
            yield SimpleNamespace(execute=execute)
        finally:
            closes += 1

    monkeypatch.setattr(health.db_module, "async_session", factory)
    tasks = [asyncio.create_task(health._probe_database()) for _ in range(20)]
    await entered.wait()
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    release.set()
    assert all(result.ready for result in await asyncio.gather(*tasks[1:]))
    assert calls == closes == 1
    assert (await health._probe_database()).ready
    assert calls == closes == 2  # A later outage cannot be masked by a cache.


@pytest.mark.asyncio
async def test_failed_probe_logs_only_safe_diagnostics_and_rechecks(monkeypatch, caplog):
    class RecoveryError(Exception):
        sqlstate = "57P03"

    @asynccontextmanager
    async def factory(**kwargs):
        raise RecoveryError("postgresql://password@private-host/customer-data")
        yield

    monkeypatch.setattr(health.db_module, "async_session", factory)
    result = await health._probe_database()
    assert not result.ready and result.failure == "database_unavailable"
    assert "exception_type=RecoveryError" in caplog.text
    assert "password" not in caplog.text and "private-host" not in caplog.text
    response = await health.readiness()
    assert response.status_code == 503 and response.headers["Retry-After"] == "2"


@pytest.mark.asyncio
async def test_probe_timeout_closes_session_and_releases_shared_task(monkeypatch):
    original_timeout = asyncio.timeout
    closed = False

    @asynccontextmanager
    async def factory(**kwargs):
        nonlocal closed

        async def execute(_):
            await asyncio.Event().wait()

        try:
            yield SimpleNamespace(execute=execute)
        finally:
            closed = True

    # Keep the real cancellation/cleanup semantics with a short test deadline.
    monkeypatch.setattr(health.db_module, "async_session", factory)
    monkeypatch.setattr(health.asyncio, "timeout", lambda _: original_timeout(0.02))
    result = await health._probe_database()
    assert result.failure == "database_probe_timeout" and closed
    assert getattr(asyncio.get_running_loop(), "_manor_database_probe").done()
