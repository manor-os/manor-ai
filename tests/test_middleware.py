"""Tests for production API middleware."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from apps.api import middleware_core as mw


# ---------------------------------------------------------------------------
# Helpers — lightweight test app that doesn't need the DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def app_client():
    """Lightweight async client using the real app factory."""
    from httpx import ASGITransport, AsyncClient
    from apps.api.main import create_app

    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_request_id_generated(app_client):
    """Every response should include an X-Request-ID header."""
    resp = await app_client.get("/health")
    assert "x-request-id" in resp.headers
    rid = resp.headers["x-request-id"]
    assert len(rid) == 32  # uuid4 hex


@pytest.mark.anyio
async def test_request_id_passthrough(app_client):
    """When client sends X-Request-ID, the same value is echoed back."""
    custom_id = "my-trace-id-12345"
    resp = await app_client.get("/health", headers={"X-Request-ID": custom_id})
    assert resp.headers.get("x-request-id") == custom_id


@pytest.mark.anyio
async def test_404_structured_error(app_client):
    """Unknown endpoints return 404 with JSON detail."""
    resp = await app_client.get("/no-such-endpoint")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    # Request ID header should still be present
    assert resp.headers.get("x-request-id")


@pytest.mark.anyio
async def test_request_logging(app_client, caplog):
    """Request logging middleware emits method, path, and status."""
    with caplog.at_level(logging.INFO, logger="apps.api.middleware_core"):
        await app_client.get("/health")

    # Health checks are skipped in logging middleware
    assert not any("/health" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="apps.api.middleware_core"):
        await app_client.get("/no-such-route")

    log_messages = [r.message for r in caplog.records]
    assert any("GET" in m and "/no-such-route" in m and "404" in m for m in log_messages), (
        f"Expected structured log line, got: {log_messages}"
    )


@pytest.mark.anyio
async def test_rate_limit(app_client):
    """Exceeding the rate limit returns 429 with Retry-After header."""
    # Enable rate limiting and lower the limit for the test
    original_limit = mw.RATE_LIMIT_PER_MINUTE
    original_enabled = mw._RATE_LIMIT_ENABLED
    mw.RATE_LIMIT_PER_MINUTE = 5
    mw._RATE_LIMIT_ENABLED = True
    # Clear any existing bucket state
    mw._buckets.clear()

    try:
        statuses = []
        for _ in range(8):
            resp = await app_client.get("/api/v1/search?q=test")
            statuses.append(resp.status_code)

        assert 429 in statuses, f"Expected 429 among statuses: {statuses}"

        # Find the 429 response and check Retry-After
        for _ in range(3):
            resp = await app_client.get("/api/v1/search?q=test")
            if resp.status_code == 429:
                assert "retry-after" in resp.headers
                body = resp.json()
                assert body["error"] == "Too Many Requests"
                assert "request_id" in body
                break
    finally:
        mw.RATE_LIMIT_PER_MINUTE = original_limit
        mw._RATE_LIMIT_ENABLED = original_enabled
        mw._buckets.clear()


@pytest.mark.anyio
async def test_chat_rate_limit_returns_retry_after(monkeypatch):
    """Chat/API limiter returns 429 with Retry-After when a bucket is exhausted."""
    from httpx import ASGITransport, AsyncClient
    from apps.api.middleware.rate_limit import ChatRateLimitMiddleware, RateLimiter
    import apps.api.middleware.rate_limit as rate_limit

    app = FastAPI()
    app.add_middleware(ChatRateLimitMiddleware)

    @app.get("/api/v1/search")
    async def search():
        return {"ok": True}

    monkeypatch.setattr(rate_limit, "CHAT_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(rate_limit, "API_RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(rate_limit, "API_RATE_LIMIT_WINDOW", 30)
    monkeypatch.setattr(rate_limit, "_limiter", RateLimiter())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/search")).status_code == 200
        assert (await client.get("/api/v1/search")).status_code == 200
        resp = await client.get("/api/v1/search")

    assert resp.status_code == 429
    assert resp.headers["retry-after"].isdigit()
    assert resp.json()["error"] == "Too Many Requests"


@pytest.mark.anyio
async def test_redis_rate_limiter_enforces_shared_bucket():
    """Redis-backed limiter uses a fixed-window bucket and reports retry_after."""
    from apps.api.middleware.rate_limit import RateLimiter

    class FakeRedis:
        def __init__(self):
            self.values: dict[str, int] = {}
            self.expires: dict[str, int] = {}

        async def incr(self, key: str) -> int:
            self.values[key] = self.values.get(key, 0) + 1
            return self.values[key]

        async def expire(self, key: str, seconds: int) -> None:
            self.expires[key] = seconds

        async def ttl(self, key: str) -> int:
            return self.expires.get(key, -1)

    limiter = RateLimiter(redis_client=FakeRedis(), redis_enabled=True)

    first = await limiter.check("ip:127.0.0.1:api", 2, 60)
    second = await limiter.check("ip:127.0.0.1:api", 2, 60)
    third = await limiter.check("ip:127.0.0.1:api", 2, 60)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.retry_after == 60


def test_rate_limiter_keeps_sync_memory_api_for_non_middleware_callers():
    from apps.api.middleware.rate_limit import RateLimiter

    limiter = RateLimiter()

    assert limiter.check_sync("waitlist-ip:127.0.0.1", 1, 60).allowed is True
    result = limiter.check_sync("waitlist-ip:127.0.0.1", 1, 60)
    assert result.allowed is False
    assert result.retry_after > 0


@pytest.mark.anyio
async def test_redis_rate_limiter_fails_open(caplog):
    """Redis outages should warn and allow traffic instead of taking API down."""
    from apps.api.middleware.rate_limit import RateLimiter

    class BrokenRedis:
        async def incr(self, key: str) -> int:
            raise RuntimeError("redis down")

    limiter = RateLimiter(redis_client=BrokenRedis(), redis_enabled=True)

    with caplog.at_level(logging.WARNING, logger="apps.api.middleware.rate_limit"):
        result = await limiter.check("ip:127.0.0.1:api", 1, 60)

    assert result.allowed is True
    assert any("Redis rate limiter failed open" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_degraded_mode_blocks_high_cost_paths_and_spares_health(monkeypatch):
    """Degraded mode sheds high-cost routes while health/config remain available."""
    from httpx import ASGITransport, AsyncClient
    from apps.api.middleware.degraded import DegradedModeMiddleware

    app = FastAPI()
    app.add_middleware(DegradedModeMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/config")
    async def config():
        return {"ok": True}

    @app.post("/api/v1/chat/stream")
    async def chat_stream():
        return {"ok": True}

    @app.post("/api/v1/fs/upload")
    async def upload():
        return {"ok": True}

    monkeypatch.setenv("DEGRADED_MODE", "true")
    monkeypatch.setenv("DEGRADED_DISABLE_CHAT_STREAM", "true")
    monkeypatch.setenv("DEGRADED_DISABLE_LARGE_UPLOADS", "true")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health_resp = await client.get("/health")
        config_resp = await client.get("/config")
        chat_resp = await client.post("/api/v1/chat/stream")
        upload_resp = await client.post("/api/v1/fs/upload")

    assert health_resp.status_code == 200
    assert config_resp.status_code == 200
    assert chat_resp.status_code == 503
    assert chat_resp.json()["code"] == "degraded_mode"
    assert chat_resp.headers["retry-after"] == "60"
    assert upload_resp.status_code == 503
    assert upload_resp.json()["code"] == "degraded_mode"
