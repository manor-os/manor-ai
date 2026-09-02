"""Tests for production API middleware."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

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

        async def eval(self, _script: str, key_count: int, key: str, seconds: int):
            assert key_count == 1
            self.values[key] = self.values.get(key, 0) + 1
            if self.expires.get(key, -1) < 0:
                self.expires[key] = int(seconds)
            return [self.values[key], self.expires[key]]

    limiter = RateLimiter(redis_client=FakeRedis(), redis_enabled=True)

    first = await limiter.check("ip:127.0.0.1:api", 2, 60)
    second = await limiter.check("ip:127.0.0.1:api", 2, 60)
    third = await limiter.check("ip:127.0.0.1:api", 2, 60)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.retry_after == 60


@pytest.mark.anyio
async def test_redis_rate_limiter_sets_ttl_before_a_lost_response():
    from apps.api.middleware.rate_limit import RateLimiter

    class LostReplyRedis:
        def __init__(self):
            self.values: dict[str, int] = {}
            self.expires: dict[str, int] = {}
            self.lose_reply = True

        async def eval(self, _script: str, key_count: int, key: str, seconds: int):
            assert key_count == 1
            self.values[key] = self.values.get(key, 0) + 1
            if self.expires.get(key, -1) < 0:
                self.expires[key] = int(seconds)
            if self.lose_reply:
                self.lose_reply = False
                raise RuntimeError("connection lost after script execution")
            return [self.values[key], self.expires[key]]

    redis = LostReplyRedis()
    limiter = RateLimiter(redis_client=redis, redis_enabled=True)

    assert (await limiter.check("ip:127.0.0.1:api", 2, 60)).allowed is True
    assert (await limiter.check("ip:127.0.0.1:api", 2, 60)).allowed is True
    assert list(redis.expires.values()) == [60]


def test_rate_limiter_keeps_sync_memory_api_for_non_middleware_callers():
    from apps.api.middleware.rate_limit import RateLimiter

    limiter = RateLimiter()

    assert limiter.check_sync("waitlist-ip:127.0.0.1", 1, 60).allowed is True
    result = limiter.check_sync("waitlist-ip:127.0.0.1", 1, 60)
    assert result.allowed is False
    assert result.retry_after > 0


def test_rate_limiter_bounds_and_expires_memory_buckets(monkeypatch):
    from apps.api.middleware import rate_limit
    from apps.api.middleware.rate_limit import RateLimiter

    now = [100.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])
    limiter = RateLimiter(max_memory_buckets=2, memory_cleanup_interval=1)

    assert limiter.check_sync("first", 1, 1).allowed is True
    assert limiter.check_sync("second", 1, 60).allowed is True
    assert limiter.check_sync("third", 1, 60).allowed is True
    assert list(limiter._windows) == ["second", "third"]

    expiring_limiter = RateLimiter(
        max_memory_buckets=3,
        memory_cleanup_interval=1,
    )
    assert expiring_limiter.check_sync("expiring", 1, 1).allowed is True
    assert expiring_limiter.check_sync("active", 1, 60).allowed is True
    now[0] = 102.0
    assert expiring_limiter.check_sync("next", 1, 60).allowed is True
    assert "expiring" not in expiring_limiter._windows
    assert "active" in expiring_limiter._windows


def test_client_ip_ignores_raw_forwarded_headers():
    from apps.api.middleware.rate_limit import client_ip

    direct_request = SimpleNamespace(
        headers={
            "x-forwarded-for": "198.51.100.10",
            "x-real-ip": "198.51.100.11",
        },
        client=SimpleNamespace(host="8.8.8.8"),
    )
    proxied_request = SimpleNamespace(
        headers={
            "x-forwarded-for": "198.51.100.12, 198.51.100.13",
            "x-real-ip": "198.51.100.99",
        },
        client=SimpleNamespace(host="198.51.100.14"),
    )

    assert client_ip(direct_request) == "8.8.8.8"
    assert client_ip(proxied_request) == "198.51.100.14"


def test_core_rate_limiter_uses_uvicorn_resolved_client_ip():
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.77, 203.0.113.9"},
        client=SimpleNamespace(host="203.0.113.9"),
    )

    assert mw._client_ip(request) == "203.0.113.9"


@pytest.mark.anyio
async def test_client_ip_rejects_spoofed_forwarded_ip_through_uvicorn():
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from apps.api.middleware.rate_limit import client_ip

    captured = {}

    async def app(scope, _receive, _send):
        request = Request(scope)
        captured["request_client"] = request.client.host
        captured["resolved_client"] = client_ip(request)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-forwarded-for", b"198.51.100.77, 203.0.113.9"),
            (b"x-real-ip", b"203.0.113.9"),
        ],
        "client": ("172.18.0.2", 43123),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = ProxyHeadersMiddleware(
        app,
        trusted_hosts="127.0.0.1,172.16.0.0/12",
    )
    await middleware(scope, receive, send)

    assert captured == {
        "request_client": "203.0.113.9",
        "resolved_client": "203.0.113.9",
    }


@pytest.mark.anyio
async def test_client_ip_keeps_private_client_outside_proxy_pod_cidr():
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from apps.api.middleware.rate_limit import client_ip

    captured = {}

    async def app(scope, _receive, _send):
        request = Request(scope)
        captured["resolved_client"] = client_ip(request)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-forwarded-for", b"198.51.100.77, 10.23.4.5"),
        ],
        "client": ("10.244.1.5", 43123),
        "server": ("127.0.0.1", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = ProxyHeadersMiddleware(
        app,
        trusted_hosts="127.0.0.1,10.244.0.0/16",
    )
    await middleware(scope, receive, send)

    assert captured == {"resolved_client": "10.23.4.5"}


@pytest.mark.anyio
async def test_compose_sidecar_is_not_a_trusted_proxy():
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from apps.api.middleware.rate_limit import client_ip

    captured = {}

    async def app(scope, _receive, _send):
        captured["resolved_client"] = client_ip(Request(scope))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-forwarded-for", b"198.51.100.77")],
        "client": ("172.30.0.50", 43123),
        "server": ("172.30.0.10", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = ProxyHeadersMiddleware(
        app,
        trusted_hosts="127.0.0.1,172.30.0.2,172.30.0.3",
    )
    await middleware(scope, receive, send)

    assert captured == {"resolved_client": "172.30.0.50"}


@pytest.mark.parametrize(
    "compose_path",
    [
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "docker-compose.cloud.yml",
    ],
)
def test_uvicorn_proxy_allowlist_never_trusts_every_source(compose_path):
    compose = (Path(__file__).parents[1] / compose_path).read_text()

    assert "--forwarded-allow-ips=*" not in compose
    assert "10.0.0.0/8" not in compose
    assert "172.16.0.0/12" not in compose
    assert compose.count("MANOR_DOCKER_NETWORK_PREFIX:-172.30.0") >= 2
    assert "MANOR_CADDY_PROXY_IP" not in compose
    assert "MANOR_WEB_PROXY_IP" not in compose
    assert "MANOR_DOCKER_NETWORK_CIDR" not in compose


def test_direct_api_ports_are_loopback_only():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()

    assert '"127.0.0.1:8010:8000"' in compose
    assert '"127.0.0.1:8011:8000"' in compose


def test_cloud_multiworker_rate_limits_use_redis():
    compose = (Path(__file__).parents[1] / "docker-compose.cloud.yml").read_text()

    assert "REDIS_RATE_LIMIT_ENABLED: ${REDIS_RATE_LIMIT_ENABLED:-true}" in compose


@pytest.mark.anyio
async def test_redis_rate_limiter_fails_open(caplog):
    """Redis outages should warn and allow traffic instead of taking API down."""
    from apps.api.middleware.rate_limit import RateLimiter

    class BrokenRedis:
        async def eval(self, _script: str, _key_count: int, _key: str, _seconds: int):
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


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/chat/stream",
        "/api/v1/public/chat/public-token/message/stream",
        "/api/v1/workspace-drafts/stream",
        "/api/v1/workspace-drafts/draft-123/messages/stream",
        "/api/v1/workspace-drafts/draft-123/finalize/stream",
        "/api/v1/agents/generate-stream",
        "/api/v1/agents/generate-draft-stream",
        "/api/v1/skills/generate-stream",
    ],
)
def test_degraded_mode_chat_stream_classification_matches_gateway_routes(monkeypatch, path):
    from apps.api.middleware.degraded import degraded_reason

    monkeypatch.setenv("DEGRADED_DISABLE_CHAT_STREAM", "true")
    monkeypatch.setenv("DEGRADED_DISABLE_MEDIA_GENERATION", "false")

    assert degraded_reason(path, "POST") == "chat_stream"
