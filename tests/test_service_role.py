from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import apps.api.middleware_core as middleware_core
import apps.api.streaming_concurrency as streaming_concurrency
from packages.core.service_role import (
    api_startup_side_effects_enabled,
    telegram_polling_startup_enabled,
    normalize_service_role,
    otel_service_name_for_role,
    validate_service_role_for_deployment,
)


def test_service_role_normalizes_supported_roles():
    assert normalize_service_role("chat") == "chat"
    assert normalize_service_role("worker-heavy") == "worker-heavy"
    assert normalize_service_role("sandbox") == "sandbox"
    assert normalize_service_role("unknown") == "api"


def test_cloud_deployment_rejects_unknown_explicit_service_role():
    assert validate_service_role_for_deployment(None, deployment_mode="cloud") is None
    assert validate_service_role_for_deployment("", deployment_mode="cloud") is None
    assert validate_service_role_for_deployment("chat", deployment_mode="cloud") is None
    assert validate_service_role_for_deployment("unknown", deployment_mode="oss") is None

    assert (
        validate_service_role_for_deployment("unknown", deployment_mode="cloud")
        == "Unsupported MANOR_SERVICE_ROLE=unknown for DEPLOYMENT_MODE=cloud"
    )


@pytest.mark.asyncio
async def test_cloud_lifespan_rejects_unknown_explicit_service_role(monkeypatch):
    """FastAPI startup should fail closed even if a process bypasses docker/entrypoint.sh."""

    from apps.api.main import lifespan
    from packages.core.config import get_settings

    class FakeSettings:
        DEPLOYMENT_MODE = "cloud"
        MANOR_SERVICE_ROLE = "typo-role"
        JWT_SECRET_KEY = "x" * 32

    get_settings.cache_clear()
    try:
        monkeypatch.setattr("packages.core.config.get_settings", lambda: FakeSettings())

        with pytest.raises(RuntimeError, match="Unsupported MANOR_SERVICE_ROLE=typo-role"):
            async with lifespan(FastAPI()):
                pass
    finally:
        get_settings.cache_clear()


def test_chat_role_uses_chat_tracing_and_skips_api_side_effects():
    assert otel_service_name_for_role("chat") == "manor-chat"
    assert otel_service_name_for_role("sandbox") == "manor-sandbox"
    assert api_startup_side_effects_enabled("chat") is False
    assert api_startup_side_effects_enabled("api", deployment_mode="oss") is True
    assert api_startup_side_effects_enabled("api", deployment_mode="cloud") is False
    assert api_startup_side_effects_enabled(
        "api",
        deployment_mode="cloud",
        explicit_enabled="true",
    ) is True


def test_telegram_polling_startup_requires_explicit_runner_or_single_machine_compat():
    assert telegram_polling_startup_enabled(role="api", deployment_mode="cloud") is False
    assert telegram_polling_startup_enabled(role="chat", deployment_mode="cloud") is False
    assert telegram_polling_startup_enabled(role="worker", deployment_mode="cloud") is False
    assert telegram_polling_startup_enabled(role="api", deployment_mode="oss") is True
    assert telegram_polling_startup_enabled(
        role="worker",
        deployment_mode="cloud",
        explicit_enabled="true",
    ) is True


@pytest.mark.asyncio
async def test_cloud_api_lifespan_starts_ws_redis_relay_per_api_pod(monkeypatch):
    """WS fan-out is per-process, so cloud API pods must run the relay."""

    from packages.core.config import get_settings

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeCache:
        async def close(self) -> None:
            pass

    class FakeSettings:
        DEPLOYMENT_MODE = "cloud"
        MANOR_SERVICE_ROLE = "api"
        JWT_SECRET_KEY = "x" * 32

    events: list[str] = []

    async def fake_load_plans(_db) -> int:
        return 0

    get_settings.cache_clear()
    import packages.core.cache as cache_module
    import packages.core.database as database_module

    try:
        monkeypatch.setattr("packages.core.config.get_settings", lambda: FakeSettings())
        monkeypatch.setattr(
            "packages.core.ai.runtime.runtime_ensure_tool_registry_initialized",
            lambda: object(),
        )
        monkeypatch.setattr("packages.core.observability.init_tracing", lambda *, service_name: None)
        monkeypatch.setattr("packages.core.observability.shutdown_tracing", lambda: None)
        monkeypatch.setattr(database_module, "async_session", lambda: FakeSession())
        monkeypatch.setattr("packages.core.constants.plans.load_plans_into_cache", fake_load_plans)
        monkeypatch.setattr("apps.api.routers.ws.start_redis_relay", lambda: events.append("start"))
        monkeypatch.setattr("apps.api.routers.ws.stop_redis_relay", lambda: events.append("stop"))
        monkeypatch.setattr(cache_module, "cache", FakeCache())

        from apps.api.main import lifespan

        async with lifespan(FastAPI()):
            pass
    finally:
        get_settings.cache_clear()

    assert events == ["start", "stop"]


@pytest.mark.asyncio
async def test_cloud_chat_lifespan_skips_external_pricing_warmup(monkeypatch):
    """manor-chat must not fan out non-chat startup warmups per replica."""

    from packages.core.config import get_settings

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeCache:
        async def close(self) -> None:
            pass

    pricing_warmups: list[float] = []

    async def fake_load_plans(_db) -> int:
        return 0

    async def fake_pricing_sync(*, timeout_s: float):
        pricing_warmups.append(timeout_s)
        return {"count": 0, "path": "/tmp/unused"}

    class FakeSettings:
        DEPLOYMENT_MODE = "cloud"
        MANOR_SERVICE_ROLE = "chat"
        JWT_SECRET_KEY = "x" * 32

    get_settings.cache_clear()
    import packages.core.cache as cache_module
    import packages.core.database as database_module

    try:
        monkeypatch.setattr("packages.core.config.get_settings", lambda: FakeSettings())
        monkeypatch.setattr(
            "packages.core.ai.runtime.runtime_ensure_tool_registry_initialized",
            lambda: object(),
        )
        monkeypatch.setattr("packages.core.observability.init_tracing", lambda *, service_name: None)
        monkeypatch.setattr("packages.core.observability.shutdown_tracing", lambda: None)
        monkeypatch.setattr(database_module, "async_session", lambda: FakeSession())
        monkeypatch.setattr("packages.core.constants.plans.load_plans_into_cache", fake_load_plans)
        monkeypatch.setattr(
            "packages.core.services.openrouter_pricing_sync.sync_openrouter_pricing_cache",
            fake_pricing_sync,
        )
        monkeypatch.setattr(cache_module, "cache", FakeCache())

        from apps.api.main import lifespan

        async with lifespan(FastAPI()):
            pass
    finally:
        get_settings.cache_clear()

    assert pricing_warmups == []


@pytest.mark.asyncio
async def test_internal_chat_capacity_is_role_gated_and_hidden_from_schema(monkeypatch):
    from apps.api.main import create_app
    from packages.core.config import Settings, get_settings

    app = create_app()

    assert "/internal/autoscaling/chat" not in app.openapi()["paths"]

    get_settings.cache_clear()
    try:
        monkeypatch.setattr(Settings, "MANOR_SERVICE_ROLE", "chat")
        monkeypatch.setattr(Settings, "MANOR_AUTOSCALING_METRICS_ENABLED", False, raising=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            disabled_response = await client.get("/internal/autoscaling/chat")

        assert disabled_response.status_code == 404

        monkeypatch.setattr(Settings, "MANOR_AUTOSCALING_METRICS_ENABLED", True, raising=False)
        get_settings.cache_clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat_response = await client.get("/internal/autoscaling/chat")

        assert chat_response.status_code == 200
        assert isinstance(chat_response.json()["active_streams"], int)

        monkeypatch.setattr(Settings, "MANOR_SERVICE_ROLE", "api")
        get_settings.cache_clear()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            api_response = await client.get("/internal/autoscaling/chat")

        assert api_response.status_code == 404
    finally:
        get_settings.cache_clear()


def test_chat_stream_paths_skip_base_http_middleware_wrapping():
    from apps.api.chat_stream_routes import CHAT_STREAM_ROUTE_EXAMPLES

    class DummyURL:
        def __init__(self, path: str) -> None:
            self.path = path

    class DummyRequest:
        def __init__(self, path: str) -> None:
            self.url = DummyURL(path)

    for path in CHAT_STREAM_ROUTE_EXAMPLES:
        assert middleware_core._is_streaming(DummyRequest(path)) is True

    assert middleware_core._is_streaming(DummyRequest("/api/v1/chat")) is False


def test_public_chat_non_stream_paths_do_not_trigger_chat_role_guard():
    non_stream_paths = [
        "/api/v1/public/chat/public-token",
        "/api/v1/public/chat/public-token/session",
        "/api/v1/public/chat/public-token/message",
        "/api/v1/public/chat/public-token/messages",
        "/api/v1/public/chat/public-token/qr",
        "/api/v1/public/chat/public-token/embed",
        "/api/v1/public/chat/public-token/embed.js",
    ]

    for path in non_stream_paths:
        assert middleware_core._is_chat_stream_role_guard_path(path) is False

    assert middleware_core._is_chat_stream_role_guard_path(
        "/api/v1/public/chat/public-token/message/stream"
    ) is True


def test_chat_stream_route_contract_is_shared_by_middleware_and_degraded_mode():
    from apps.api.chat_stream_routes import CHAT_STREAM_ROUTE_EXAMPLES, is_chat_stream_path
    from apps.api.middleware.degraded import degraded_reason

    for path in CHAT_STREAM_ROUTE_EXAMPLES:
        assert is_chat_stream_path(path) is True
        assert middleware_core._is_chat_stream_role_guard_path(path) is True
        assert degraded_reason(path, "POST") == "chat_stream"

    assert is_chat_stream_path("/api/v1/workspace-drafts/draft-123") is False
    assert is_chat_stream_path("/api/v1/public/chat/public-token/message") is False

@pytest.mark.asyncio
async def test_streaming_adapter_can_require_chat_service_role(monkeypatch):
    called = False

    async def fake_acquire(*, scope: str):
        nonlocal called
        called = True
        return object()

    monkeypatch.setenv("CHAT_STREAM_REQUIRE_CHAT_ROLE", "true")
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "api")
    monkeypatch.setattr(streaming_concurrency, "acquire_chat_stream_slot", fake_acquire)

    with pytest.raises(HTTPException) as exc_info:
        await streaming_concurrency.acquire_chat_stream_lease(scope="chat")

    assert exc_info.value.status_code == 503
    assert called is False

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "chat")
    lease = await streaming_concurrency.acquire_chat_stream_lease(scope="chat")

    assert lease is not None
    assert called is True


@pytest.mark.asyncio
async def test_streaming_role_middleware_rejects_non_chat_before_auth(monkeypatch):
    app = FastAPI()
    middleware_core.setup_stream_role_guard(app)

    called = False

    @app.post("/api/v1/chat/stream")
    async def stream_endpoint():
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setenv("CHAT_STREAM_REQUIRE_CHAT_ROLE", "true")
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "api")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/chat/stream", data={"message": "hello"})

    assert response.status_code == 503
    assert "must be served by manor-chat" in response.text
    assert called is False


@pytest.mark.asyncio
async def test_streaming_role_middleware_allows_chat_role(monkeypatch):
    app = FastAPI()
    middleware_core.setup_stream_role_guard(app)

    @app.post("/api/v1/chat/stream")
    async def stream_endpoint():
        return {"ok": True}

    monkeypatch.setenv("CHAT_STREAM_REQUIRE_CHAT_ROLE", "true")
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "chat")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/chat/stream", data={"message": "hello"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_streaming_role_middleware_marks_chat_smoke_response(monkeypatch):
    app = FastAPI()
    middleware_core.setup_stream_role_guard(app)

    @app.post("/api/v1/chat/stream")
    async def stream_endpoint():
        return {"ok": True}

    monkeypatch.setenv("CHAT_STREAM_REQUIRE_CHAT_ROLE", "true")
    monkeypatch.setenv("MANOR_SERVICE_ROLE", "chat")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat/stream",
            headers={"X-Manor-Smoke": "chat-route"},
            data={"message": "hello"},
        )

    assert response.status_code == 200
    assert response.headers["X-Manor-Service-Role"] == "chat"


@pytest.mark.asyncio
async def test_streaming_role_middleware_marks_api_smoke_response(monkeypatch):
    app = FastAPI()
    middleware_core.setup_stream_role_guard(app)

    @app.get("/health")
    async def health_endpoint():
        return {"status": "ok"}

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "api")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/health",
            headers={"X-Manor-Smoke": "api-route"},
        )

    assert response.status_code == 200
    assert response.headers["X-Manor-Service-Role"] == "api"


@pytest.mark.asyncio
async def test_streaming_role_middleware_does_not_intercept_websocket_scope(monkeypatch):
    called = False

    async def fake_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "websocket.accept"})

    middleware = middleware_core.ChatStreamRoleGuardMiddleware(fake_app)
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    monkeypatch.setenv("MANOR_SERVICE_ROLE", "api")

    await middleware(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"x-manor-smoke", b"ws-route")],
        },
        receive,
        send,
    )

    assert called is True
    assert sent == [{"type": "websocket.accept"}]
