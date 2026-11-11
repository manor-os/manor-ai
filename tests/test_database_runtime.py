"""Connection budgets are transaction-scoped, including slow/cancelled work."""
from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import Integer, String, event, func, literal, literal_column, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from apps.api.middleware_core import setup_middleware
from packages.core import database
from packages.core.database_runtime import (
    AdmissionPolicy, DatabaseAdmission, DatabaseBusyError, DatabaseEngineKind, DatabaseFailureKind,
    configure_database_runtime, database_request_id, release_read_transaction,
)


def _another_request(factory, value=3):
    """Read as an unrelated caller: a task outside the current permit holder.

    A Session opened by the holder itself (or a task it starts) is reentrant.
    """
    async def read():
        async with factory() as db:
            return await db.scalar(select(literal(value)))

    return asyncio.create_task(read(), context=contextvars.Context())


def runtime_settings(url, **overrides):
    return SimpleNamespace(**{
        "DATABASE_URL": url,
        "DATABASE_ECHO": False,
        "DATABASE_POOL_MODE": "sqlalchemy",
        "DATABASE_POOL_SIZE": 2,
        "DATABASE_MAX_OVERFLOW": 0,
        "DATABASE_POOL_TIMEOUT": 1,
        "DATABASE_POOL_RECYCLE": 1800,
        "DATABASE_ADMISSION_LIMIT": 1,
        "DATABASE_MAX_WAITERS": 3,
        "DATABASE_ADMISSION_TIMEOUT": 0.08,
        "DATABASE_HOLD_WARN_SECONDS": 5,
        **overrides,
    })


@pytest.mark.parametrize("role,kind", [
    ("api", DatabaseEngineKind.APPLICATION), ("chat", DatabaseEngineKind.APPLICATION),
    ("migration", DatabaseEngineKind.APPLICATION), ("worker", DatabaseEngineKind.WORKER),
    ("worker-room", DatabaseEngineKind.WORKER), ("worker-heavy", DatabaseEngineKind.WORKER),
    ("beat", DatabaseEngineKind.WORKER),
])
def test_service_role_selects_engine_policy(role, kind):
    assert DatabaseEngineKind.for_service(role) is kind


@pytest.mark.parametrize("name", ["DATABASE_ADMISSION_TIMEOUT", "DATABASE_HOLD_WARN_SECONDS"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_runtime_rejects_unbounded_or_invalid_deadlines(name, value):
    engine = create_async_engine("postgresql+asyncpg://invalid/unused")
    with pytest.raises(ValueError):
        configure_database_runtime(
            engine, runtime_settings("postgresql+asyncpg://invalid/unused", **{name: value}),
            DatabaseEngineKind.APPLICATION,
        )
    assert not hasattr(engine.sync_engine, "_manor_database_runtime")


def test_pgbouncer_application_pool_must_cover_the_admission_budget():
    """API/chat keep a client pool to PgBouncer; every admitted transaction
    holds one pooled connection, so the budget cannot exceed the pool."""
    with pytest.raises(ValueError):
        database.create_database_engine(runtime_settings(
            "postgresql+asyncpg://invalid/unused", DATABASE_POOL_MODE="pgbouncer",
            DATABASE_POOL_SIZE=2, DATABASE_MAX_OVERFLOW=0, DATABASE_ADMISSION_LIMIT=4,
        ), kind=DatabaseEngineKind.APPLICATION)

    engine = database.create_database_engine(runtime_settings(
        "postgresql+asyncpg://invalid/unused", DATABASE_POOL_MODE="pgbouncer",
        DATABASE_POOL_SIZE=5, DATABASE_MAX_OVERFLOW=0, DATABASE_ADMISSION_LIMIT=4,
    ), kind=DatabaseEngineKind.APPLICATION)

    runtime = engine.sync_engine._manor_database_runtime

    assert not isinstance(engine.pool, NullPool)
    assert runtime.policy.limit == 4
    assert runtime.pool_mode == "pooled"


@pytest.mark.parametrize(("mode", "kind", "expected_limit"), [
    ("pgbouncer", DatabaseEngineKind.APPLICATION, 4),
    ("pgbouncer", DatabaseEngineKind.WORKER, 1),
    ("sqlalchemy", DatabaseEngineKind.APPLICATION, 4),
    ("sqlalchemy", DatabaseEngineKind.WORKER, 6),
])
def test_null_pool_automatic_admission_preserves_direct_worker_budget(mode, kind, expected_limit):
    engine = create_async_engine("postgresql+asyncpg://invalid/unused", poolclass=NullPool)

    runtime = configure_database_runtime(
        engine,
        runtime_settings(
            "postgresql+asyncpg://invalid/unused", DATABASE_POOL_MODE=mode,
            DATABASE_ADMISSION_LIMIT=0, DATABASE_POOL_SIZE=5, DATABASE_MAX_OVERFLOW=2,
        ),
        kind,
    )

    assert runtime.policy.limit == expected_limit
    assert runtime.pool_mode == "nullpool"
    assert runtime.admission_enabled is True  # limit=0 still selects an automatic budget


@pytest.mark.parametrize("value,expected", [(None, True), ("true", True), ("false", False), ("0", False)])
def test_admission_enabled_setting_is_explicit(monkeypatch, value, expected):
    from packages.core.config import Settings

    if value is None:
        monkeypatch.delenv("DATABASE_ADMISSION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DATABASE_ADMISSION_ENABLED", value)
    assert Settings().DATABASE_ADMISSION_ENABLED is expected


@pytest.mark.parametrize("value", ["", "flase", "disabled"])
def test_admission_enabled_setting_rejects_typos(monkeypatch, value):
    from packages.core.config import Settings

    monkeypatch.setenv("DATABASE_ADMISSION_ENABLED", value)
    with pytest.raises(ValueError, match="DATABASE_ADMISSION_ENABLED"):
        Settings()


@pytest.mark.parametrize("mode", ["sqlalchemy", "pgbouncer"])
def test_worker_explicit_admission_is_independent_of_unused_pool_settings(mode):
    engine = database.create_database_engine(runtime_settings(
        "postgresql+asyncpg://invalid/unused", DATABASE_POOL_MODE=mode,
        DATABASE_POOL_SIZE=1, DATABASE_MAX_OVERFLOW=0, DATABASE_ADMISSION_LIMIT=4,
    ), kind=DatabaseEngineKind.WORKER)

    assert isinstance(engine.pool, NullPool)
    assert engine.sync_engine._manor_database_runtime.policy.limit == 4


@pytest.mark.asyncio
async def test_default_direct_worker_allows_nested_sessions(_test_database_guard, monkeypatch):
    from conftest import TEST_DATABASE_URL

    settings = runtime_settings(
        TEST_DATABASE_URL, DATABASE_ADMISSION_LIMIT=0,
        DATABASE_POOL_SIZE=5, DATABASE_MAX_OVERFLOW=2,
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    outer_factory = database.create_worker_session()
    inner_factory = database.create_worker_session()
    outer_engine, inner_engine = outer_factory.kw["bind"], inner_factory.kw["bind"]
    outer_runtime = outer_engine.sync_engine._manor_database_runtime
    inner_runtime = inner_engine.sync_engine._manor_database_runtime
    try:
        async with outer_factory() as outer:
            assert await outer.scalar(select(literal(1))) == 1
            async with inner_factory() as inner:
                assert await inner.scalar(select(literal(2))) == 2
                assert outer.in_transaction()
                # The caller counts once; its own nested Session is reentrant.
                for runtime in (outer_runtime, inner_runtime):
                    assert runtime.snapshot()["active_transactions"] == 1
                    assert runtime.snapshot()["active_nested_transactions"] == 1
            assert outer_runtime.snapshot()["active_transactions"] == 1
            assert outer_runtime.snapshot()["active_nested_transactions"] == 0
        snapshot = outer_runtime.snapshot()
        assert snapshot["active_transactions"] == snapshot["waiting"] == 0
        assert snapshot["wait_timeouts"] == 0
        assert outer_runtime.checked_out == inner_runtime.checked_out == 0
    finally:
        await outer_engine.dispose()
        await inner_engine.dispose()


@pytest.mark.parametrize("limit", [3, -1, 1.5, "4"])
def test_pooled_engine_rejects_admission_outside_its_capacity_or_invalid(limit):
    engine = create_async_engine("postgresql+asyncpg://invalid/unused", pool_size=2, max_overflow=0)

    with pytest.raises(ValueError):
        configure_database_runtime(
            engine,
            runtime_settings("postgresql+asyncpg://invalid/unused", DATABASE_ADMISSION_LIMIT=limit),
            DatabaseEngineKind.APPLICATION,
        )


@pytest.mark.parametrize("code,expected", [
    ("57P03", DatabaseFailureKind.UNAVAILABLE), ("08006", DatabaseFailureKind.UNAVAILABLE),
    ("53300", DatabaseFailureKind.UNAVAILABLE), ("23505", None), ("57014", None),
])
def test_database_failure_classification_uses_protocol_not_message(code, expected):
    class DriverError(Exception):
        sqlstate = code

    assert DatabaseFailureKind.from_exception(DriverError("secret")) == expected
    wrapped = DBAPIError(None, None, DriverError("secret"))
    assert DatabaseFailureKind.from_exception(wrapped) == expected


@pytest.mark.asyncio
async def test_database_recovery_error_does_not_attempt_database_error_capture(monkeypatch):
    from apps.api import middleware_core

    class RecoveryError(Exception):
        sqlstate = "57P03"

    record = AsyncMock(side_effect=AssertionError("must not reconnect to a failed database"))
    monkeypatch.setattr(middleware_core, "_record_server_error", record)
    app = FastAPI()
    setup_middleware(app)

    @app.get("/recovering")
    async def recovering():
        raise RecoveryError("private database password")

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.get("/recovering")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"
    assert "password" not in response.text
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_identity_database_busy_is_not_auth_rejection(monkeypatch):
    from contextlib import asynccontextmanager
    from apps.api.routers import ws

    @asynccontextmanager
    async def unavailable():
        raise DatabaseBusyError("wait timed out")
        yield

    monkeypatch.setattr(ws.db_module, "async_session", unavailable)
    with pytest.raises(DatabaseBusyError):
        await ws._resolve_active_websocket_identity({"token_version": 0}, "synthetic-user")


@pytest.mark.asyncio
async def test_admission_bounds_queue_timeout_and_cancel():
    gate = DatabaseAdmission(AdmissionPolicy(1, 1, 0.02))
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert gate.waiting == 1
    with pytest.raises(DatabaseBusyError, match="queue is full"):
        await gate.acquire()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.waiting == 0 and gate.active == 1
    with pytest.raises(DatabaseBusyError, match="timed out"):
        await gate.acquire()
    gate.release()
    await gate.acquire()
    gate.release()
    assert gate.active == 0 and gate.waiting == 0 and gate.rejected == 2
    assert gate.queue_full == 1 and gate.wait_timeouts == 1
    assert gate.peak_active == 1 and gate.peak_waiting == 1


@pytest.mark.asyncio
async def test_thousand_waiting_clients_do_not_pin_connections_and_burst_is_bounded(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    engine = database.create_database_engine(runtime_settings(
        TEST_DATABASE_URL, DATABASE_POOL_SIZE=7, DATABASE_ADMISSION_LIMIT=6,
        DATABASE_MAX_WAITERS=24, DATABASE_ADMISSION_TIMEOUT=2,
    ), kind=DatabaseEngineKind.APPLICATION)
    factory = database.create_session_factory(engine)
    runtime = engine.sync_engine._manor_database_runtime
    paused = asyncio.Event()
    ready = asyncio.Event()
    # Warm-up must not queue behind slow connection startup; overload is
    # exercised only after all clients reach the external-work barrier.
    initial_reads = asyncio.Semaphore(runtime.policy.limit)
    readers = peak_checkout = 0

    def observe_checkout(*_args):
        nonlocal peak_checkout
        peak_checkout = max(peak_checkout, runtime.checked_out)

    event.listen(engine.sync_engine, "checkout", observe_checkout)

    async def client():
        nonlocal readers
        async with factory() as db:
            # Pace connection startup, then keep all 1,000 clients waiting on
            # external work together. This is not 1,000 simultaneous SQL calls.
            async with initial_reads:
                assert await db.scalar(select(literal(1))) == 1
                assert await release_read_transaction(db)
                readers += 1
                if readers == 1000:
                    ready.set()
            await paused.wait()
            try:
                assert await db.scalar(select(literal(2))) == 2
                await db.commit()
                return "ok"
            except DatabaseBusyError:
                return "busy"

    tasks = [asyncio.create_task(client()) for _ in range(1000)]
    try:
        await asyncio.wait_for(ready.wait(), 30)
        assert engine.pool.checkedout() == 0
        assert runtime.snapshot()["active_transactions"] == 0
        async with factory(info={"database_probe": True}) as probe:
            assert await probe.scalar(select(literal(3))) == 3
        paused.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert set(outcomes) == {"ok", "busy"}
        snapshot = runtime.snapshot()
        assert peak_checkout <= 6 and snapshot["peak_active_transactions"] <= 6
        assert snapshot["peak_waiters"] <= 24
        assert snapshot["queue_full"] > 0
        assert snapshot["active_transactions"] == snapshot["waiting"] == 0
        assert engine.pool.checkedout() == 0
        async with factory() as recovered:
            assert await recovered.scalar(select(literal(4))) == 4
    finally:
        paused.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_busy_handler_never_records_error_in_database(monkeypatch):
    from apps.api import middleware_core

    async def forbidden(*args, **kwargs):
        raise AssertionError("overload reporting must not acquire another connection")

    monkeypatch.setattr(middleware_core, "_record_server_error", forbidden)
    app = FastAPI()
    setup_middleware(app)

    @app.get("/busy")
    async def busy():
        raise DatabaseBusyError("postgresql://secret@private-database")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/busy", headers={"X-Request-ID": "pool-test"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.json()["detail"]["code"] == "database_busy"
    assert response.json()["request_id"] == "pool-test"
    assert "secret" not in response.text


@pytest.mark.parametrize("accepted", [True, False])
def test_websocket_overload_returns_retryable_handshake_or_close(monkeypatch, accepted):
    from apps.api import middleware_core

    record_error = AsyncMock(side_effect=AssertionError("overload must not write another error"))
    monkeypatch.setattr(middleware_core, "_record_server_error", record_error)
    app = FastAPI()
    setup_middleware(app)

    @app.websocket("/busy")
    async def busy(socket: WebSocket):
        if accepted:
            await socket.accept()
        raise DatabaseBusyError("Database admission queue is full")

    with TestClient(app) as client:
        if accepted:
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect("/busy") as socket:
                    socket.receive_text()
            assert error.value.code == 1013
            assert error.value.reason == "Database is busy. Retry later."
        else:
            with pytest.raises(WebSocketDenialResponse) as error:
                with client.websocket_connect("/busy"):
                    pytest.fail("database overload must reject the handshake")
            assert error.value.status_code == 503
            assert error.value.headers["Retry-After"] == "2"
            assert error.value.json()["detail"]["code"] == "database_busy"
    record_error.assert_not_awaited()


class ProbeBase(DeclarativeBase):
    pass


class Probe(ProbeBase):
    __tablename__ = "database_runtime_probe"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String, default="original")


class RunProbe(ProbeBase):
    __tablename__ = "database_runtime_run_probe"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")


@pytest_asyncio.fixture
async def managed_db(_test_database_guard, request):
    from conftest import TEST_DATABASE_URL

    settings = runtime_settings(
        TEST_DATABASE_URL, DATABASE_MAX_WAITERS=32, DATABASE_ADMISSION_TIMEOUT=1,
        **getattr(request, "param", {}),
    )
    engine = database.create_database_engine(settings, kind=DatabaseEngineKind.APPLICATION)
    async with engine.begin() as connection:
        await connection.run_sync(ProbeBase.metadata.create_all)
    try:
        yield engine, database.create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(ProbeBase.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed_db", [{"DATABASE_ADMISSION_ENABLED": False}], indirect=True)
async def test_disabled_admission_preserves_safe_read_release_and_write_rollback(managed_db):
    engine, factory = managed_db
    runtime = engine.sync_engine._manor_database_runtime
    async with factory() as db:
        assert await db.scalar(select(literal(1))) == 1
        assert await release_read_transaction(db) is True
        assert runtime.checked_out == 0
        db.add(Probe(id=9001, value="uncommitted"))
        assert await release_read_transaction(db) is False
        await db.flush()
        assert await release_read_transaction(db) is False
        assert runtime.checked_out == 1
    async with factory() as db:
        assert await db.get(Probe, 9001) is None
    assert runtime.checked_out == 0


@pytest_asyncio.fixture
async def ungated_worker(_test_database_guard, monkeypatch):
    from conftest import TEST_DATABASE_URL

    settings = runtime_settings(
        TEST_DATABASE_URL, DATABASE_POOL_MODE="pgbouncer", DATABASE_ADMISSION_ENABLED=False,
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    engines = []

    def make():
        factory = database.create_worker_session()
        engines.append(factory.kw["bind"])
        return factory

    try:
        yield make
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_task", [False, True])
async def test_disabled_worker_allows_nested_sessions_across_factories_and_groups(ungated_worker, child_task):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    outer_factory, inner_factory = ungated_worker(), ungated_worker()
    outer_runtime = outer_factory.kw["bind"].sync_engine._manor_database_runtime
    inner_runtime = inner_factory.kw["bind"].sync_engine._manor_database_runtime
    group = DatabaseAdmissionGroup(limit=1, timeout=0.15)

    async def nested():
        async with inner_factory() as inner:
            assert await inner.scalar(select(literal(2))) == 2
            assert outer_runtime.checked_out == inner_runtime.checked_out == 1

    token = database_admission_group.set(group)
    try:
        async with outer_factory() as outer:
            assert await outer.scalar(select(literal(1))) == 1
            if child_task:
                await asyncio.wait_for(asyncio.create_task(nested()), 2)
            else:
                await nested()
            assert outer.in_transaction()
            snapshot = outer_runtime.snapshot()
            assert snapshot["admission_enabled"] is False
            assert snapshot["admission_limit"] is snapshot["active_transactions"] is snapshot["waiting"] is None
            assert snapshot["checked_out"] == 1
            assert snapshot["checkout_count"] == 1
        assert not group.holders and group.peak_active == 0
        assert outer_runtime.checked_out == inner_runtime.checked_out == 0
    finally:
        database_admission_group.reset(token)


@pytest.mark.asyncio
async def test_disabled_worker_parallel_tasks_and_cancellation_release_connections(ungated_worker):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = ungated_worker()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    group = DatabaseAdmissionGroup(limit=1, timeout=0.15)
    ready = [asyncio.Event() for _ in range(6)]
    release = asyncio.Event()

    async def member(index):
        async with factory() as db:
            assert await db.scalar(select(literal(index))) == index
            ready[index].set()
            await release.wait()

    token = database_admission_group.set(group)
    tasks = [asyncio.create_task(member(index)) for index in range(len(ready))]
    database_admission_group.reset(token)
    try:
        # All members hold a real transaction concurrently, beyond both old
        # admission limits. An independent heartbeat-like session still runs.
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 2)
        assert runtime.checked_out == 6
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert runtime.checked_out == 5
        async with factory() as heartbeat:
            assert await heartbeat.scalar(select(literal(1))) == 1
        release.set()
        await asyncio.gather(*tasks[1:])
        assert runtime.checked_out == 0
        assert not group.holders and group.peak_active == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_disabled_worker_does_not_consume_or_disable_application_admission(ungated_worker, tight_engine):
    worker_factory = ungated_worker()
    app_factory = tight_engine(DATABASE_POOL_MODE="pgbouncer", DATABASE_ADMISSION_TIMEOUT=0.08)
    runtime = app_factory.kw["bind"].sync_engine._manor_database_runtime
    async with worker_factory() as worker, app_factory() as app:
        assert await worker.scalar(select(literal(1))) == 1
        assert await app.scalar(select(literal(2))) == 2
        with pytest.raises(DatabaseBusyError):
            await _another_request(app_factory)
        assert runtime.snapshot()["active_transactions"] == 1
        assert runtime.snapshot()["wait_timeouts"] == 1
    assert runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_disabled_business_admission_preserves_separate_probe_limit(ungated_worker):
    factory = ungated_worker()
    async with factory(info={"database_probe": True}) as probe:
        assert await probe.scalar(select(literal(1))) == 1
        async with factory(info={"database_probe": True}) as rejected:
            with pytest.raises(DatabaseBusyError):
                await rejected.scalar(select(literal(1)))
        async with factory() as business:
            assert await business.scalar(select(literal(1))) == 1


@pytest.mark.asyncio
async def test_locale_and_tool_path_helpers_reuse_the_only_admission_slot(db_session, managed_db, monkeypatch):
    from uuid import uuid4
    from packages.core.ai.response_language import resolve_response_locale
    from packages.core.models.user import User
    from packages.core.models.tool_path_memory import ToolIntentPath
    from packages.core.services.tool_path_memory import _load_user_paths

    user_id = uuid4().hex[:26]
    db_session.add(User(id=user_id, entity_id="one-slot", email=f"{user_id}@test.invalid",
                        password_hash="unused", locale="fr"))
    db_session.add(ToolIntentPath(entity_id="one-slot", user_id=user_id, provider="test",
                                 tool_name="test_tool", intent_signature="test read",
                                 success_count=1, failure_count=0))
    await db_session.commit()
    engine, factory = managed_db
    runtime = engine.sync_engine._manor_database_runtime
    def unexpected_factory():
        raise AssertionError("helper opened another transaction")
    monkeypatch.setattr(database, "create_worker_session", unexpected_factory)
    monkeypatch.setattr(database, "async_session", unexpected_factory)
    async with factory() as db:
        await db.scalar(select(User.id).where(User.id == user_id))
        checkouts = runtime.checkout_count
        assert runtime.snapshot()["active_transactions"] == 1
        assert await resolve_response_locale(user_id, db=db) == "fr"
        assert (await _load_user_paths("one-slot", user_id, db=db))[0].tool_name == "test_tool"
        assert runtime.checkout_count == checkouts
        assert db.in_transaction()
        assert runtime.snapshot()["active_transactions"] == 1
    assert runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_cache_invalidation_runs_after_connection_and_admission_release(managed_db, monkeypatch):
    from packages.core import read_cache

    engine, factory = managed_db
    async def invalidate(*keys):
        snapshot = engine.sync_engine._manor_database_runtime.snapshot()
        assert snapshot["active_transactions"] == 0
        assert snapshot["checked_out"] == 0
    invalidate_mock = AsyncMock(side_effect=invalidate)
    monkeypatch.setattr(read_cache, "invalidate", invalidate_mock)
    async with factory() as db:
        await db.scalar(select(literal(1)))
        read_cache.invalidate_after_commit(db, "test-key")
        await db.commit()
    invalidate_mock.assert_awaited_once_with("test-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_write", [False, True])
async def test_context_does_not_lookup_keyword_paths_or_commit_pending_writes(managed_db, monkeypatch, pending_write):
    from packages.core.services import runtime_chat_context, tool_path_memory
    from test_tool_path_memory import _mock_prompt_pipeline

    engine, factory = managed_db
    _mock_prompt_pipeline(monkeypatch, {})
    async with factory() as db:
        await db.scalar(select(literal(1)))
        if pending_write:
            db.add(Probe(id=1001, value="uncommitted"))
            await db.flush()
        runtime = engine.sync_engine._manor_database_runtime
        checkouts = runtime.checkout_count
        lookup_mock = AsyncMock(side_effect=AssertionError("context must not inject keyword path hints"))
        monkeypatch.setattr(tool_path_memory, "lookup_paths", lookup_mock)
        await runtime_chat_context.resolve_runtime_chat_context(
            db, "find test files", entity_id="test", user_id="actor", conversation_id=None,
        )
        lookup_mock.assert_not_awaited()
        # Without optional hint I/O, the caller still owns its transaction;
        # context preparation must neither open a second slot nor commit it.
        assert runtime.checkout_count == checkouts
        assert runtime.snapshot()["active_transactions"] == 1
        assert db.in_transaction()
        await db.rollback()
    async with factory() as db:
        assert await db.get(Probe, 1001) is None


@pytest.mark.asyncio
async def test_twenty_slow_clients_release_pool_and_burst_has_bounded_backpressure(managed_db):
    engine, factory = managed_db
    paused = asyncio.Event()
    all_read = asyncio.Event()
    readers = 0

    async def slow_client():
        nonlocal readers
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
            assert await release_read_transaction(db)
            readers += 1
            if readers == 20:
                all_read.set()
            await paused.wait()  # slow Redis/LLM/sandbox/socket, with no DB permit
            try:
                assert await db.scalar(select(literal(2))) == 2
                await db.commit()
                return "ok"
            except DatabaseBusyError:
                # All twenty clients resume together. Exceeding the queue's
                # wait budget is controlled backpressure, not a DB failure.
                return "busy"

    tasks = [asyncio.create_task(slow_client()) for _ in range(20)]
    try:
        await asyncio.wait_for(all_read.wait(), 5)
        assert engine.pool.checkedout() == 0
        runtime = engine.sync_engine._manor_database_runtime
        assert runtime.snapshot()["active_transactions"] == 0
        async with factory() as other:
            assert await other.scalar(select(literal(3))) == 3
        paused.set()
        outcomes = await asyncio.gather(*tasks)
        assert len(outcomes) == 20 and "ok" in outcomes
        assert set(outcomes) <= {"ok", "busy"}
        assert engine.pool.checkedout() == 0
        assert runtime.snapshot()["active_transactions"] == 0
        async with factory() as recovered:
            assert await recovered.scalar(select(literal(4))) == 4
    finally:
        paused.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_pgbouncer_pool_admits_the_configured_budget_concurrently(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    engine = database.create_database_engine(runtime_settings(
        TEST_DATABASE_URL, DATABASE_POOL_MODE="pgbouncer", DATABASE_POOL_SIZE=5,
        DATABASE_MAX_OVERFLOW=0, DATABASE_ADMISSION_LIMIT=4,
        DATABASE_MAX_WAITERS=0, DATABASE_ADMISSION_TIMEOUT=1,
    ), kind=DatabaseEngineKind.APPLICATION)
    factory = database.create_session_factory(engine)
    all_admitted = asyncio.Event()
    release = asyncio.Event()
    admitted = 0

    async def managed_session():
        nonlocal admitted
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
            admitted += 1
            if admitted == 4:
                all_admitted.set()
            await release.wait()

    tasks = [asyncio.create_task(managed_session()) for _ in range(4)]
    try:
        await asyncio.wait_for(all_admitted.wait(), 5)
        assert not isinstance(engine.pool, NullPool)
        assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 4
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_slug_functions_release_the_only_admission_slot(managed_db):
    engine, factory = managed_db
    async with factory() as outer:
        slug = await outer.scalar(select(
            func.pg_catalog.replace(func.pg_catalog.lower(literal("My-Skill")), "-", "_"),
        ))
        assert slug == "my_skill"
        assert await release_read_transaction(outer)
        assert not outer.in_transaction()
        async with factory() as nested:
            assert await nested.scalar(select(literal(1))) == 1
    assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", [
    func.lower(literal("UNQUALIFIED")),
    func.pg_catalog.upper(literal("unknown")),
    func.pg_catalog.lower(func.upper(literal("nested"))),
    func.pg_catalog.lower(text("'raw sql'")),
    literal_column("pg_advisory_xact_lock(92817002)"),
    func.pg_catalog.lower(literal_column("pg_advisory_xact_lock(92817003)::text")),
])
async def test_other_functions_and_nested_raw_sql_keep_transaction_ownership(managed_db, expression):
    _, factory = managed_db
    async with factory() as db:
        await db.scalar(select(expression))
        # A later audited read must not clear an earlier unsafe statement.
        await db.scalar(select(func.pg_catalog.lower(literal("SAFE"))))
        assert not await release_read_transaction(db)
        assert db.in_transaction()
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["lock", "write_cte"])
async def test_catalog_function_cannot_hide_nested_write_or_lock(managed_db, operation):
    _, factory = managed_db
    async with factory() as db:
        db.add(Probe(id=1))
        await db.commit()
        if operation == "lock":
            value = select(Probe.value).where(Probe.id == 1).with_for_update().scalar_subquery()
        else:
            changed = update(Probe).where(Probe.id == 1).values(value="CHANGED").returning(Probe.value).cte()
            value = changed.c.value
        await db.scalar(select(func.pg_catalog.lower(value)))
        assert not await release_read_transaction(db)
        await db.rollback()
        assert (await db.get(Probe, 1)).value == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("read, unsafe", [
    (select(Probe.value).where(Probe.id == 1), select(Probe.value).where(Probe.id == 1).with_for_update()),
    (select(func.pg_catalog.lower(literal("a"))), select(func.lower(literal("a")))),
    (select(func.pg_catalog.lower(literal("a"))), select(func.pg_catalog.upper(literal("a")))),
])
async def test_a_known_read_shape_never_answers_for_a_locking_variant(managed_db, read, unsafe):
    """Statements are classified once per shape; a lock, an unknown function or
    different raw SQL is another shape."""
    _, factory = managed_db
    async with factory() as db:
        db.add(Probe(id=1))
        await db.commit()
        for _ in range(2):
            await db.execute(read)
            assert await release_read_transaction(db)
            await db.execute(unsafe)
            assert not await release_read_transaction(db)
            await db.rollback()


@pytest.mark.asyncio
async def test_pending_and_flushed_writes_core_sql_and_locks_not_auto_committed(managed_db):
    engine, factory = managed_db
    async with factory() as db:
        db.add(Probe(id=1))
        assert not await release_read_transaction(db)
        await db.flush()
        assert not await release_read_transaction(db)
        assert engine.pool.checkedout() == 1
        await db.rollback()
    async with factory() as db:
        db.add(Probe(id=1))
        await db.commit()
        await db.execute(update(Probe).values(value="changed"))
        assert not await release_read_transaction(db)
        await db.rollback()
        row = await db.scalar(select(Probe).with_for_update())
        assert row.value == "original"
        assert not await release_read_transaction(db)
        await db.rollback()
        await db.scalar(select(Probe).with_for_update(read=True))
        assert not await release_read_transaction(db)
        await db.rollback()
        await db.execute(text("SELECT pg_advisory_xact_lock(92817001)"))
        assert not await release_read_transaction(db)
        await db.rollback()
        await db.connection()
        assert not await release_read_transaction(db)
        await db.rollback()
        await db.scalar(select(literal(1)))
        async with db.begin_nested():
            assert not await release_read_transaction(db)
        assert await release_read_transaction(db)


@pytest.mark.asyncio
async def test_unmodified_dirty_assignment_releases_worker_heavy_admission_slot(managed_db):
    engine, factory = managed_db
    async with factory() as outer:
        outer.add(Probe(id=1, value="intake_release_identity"))
        await outer.commit()
        row = await outer.scalar(select(Probe).where(Probe.id == 1))
        await outer.refresh(row, attribute_names=["id"])
        row.value = "intake_release_identity"
        assert outer.sync_session.dirty
        assert not outer.sync_session.is_modified(row)
        with pytest.raises(DatabaseBusyError):
            await _another_request(factory, 1)
        assert await release_read_transaction(outer)
        assert await _another_request(factory, 2) == 2
        await outer.scalar(select(literal(3)))
        row.value = "other_step"
        assert outer.sync_session.is_modified(row)
        assert not await release_read_transaction(outer)
        await outer.rollback()


@pytest.mark.asyncio
async def test_cancellation_returns_connection_and_admission(managed_db, monkeypatch):
    engine, factory = managed_db
    monkeypatch.setattr(database, "async_session", factory)
    generator = database.get_db()
    db = await anext(generator)
    await db.scalar(select(literal(1)))
    with pytest.raises(asyncio.CancelledError):
        await generator.athrow(asyncio.CancelledError())
    assert engine.pool.checkedout() == 0
    assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_busy_queries_leave_probe_slot_and_recover(managed_db):
    engine, factory = managed_db
    async with factory() as holding:
        await holding.scalar(select(literal(1)))
        with pytest.raises(DatabaseBusyError):
            await _another_request(factory, 2)
        async with factory(info={"database_probe": True}) as probe:
            assert await probe.scalar(select(literal(3))) == 3
        assert engine.pool.checkedout() == 1
    async with factory() as recovered:
        assert await recovered.scalar(select(literal(4))) == 4
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_probe_admission_is_independent_when_policy_matches_business_gate(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    engine = database.create_database_engine(runtime_settings(
        TEST_DATABASE_URL, DATABASE_ADMISSION_LIMIT=1, DATABASE_MAX_WAITERS=0,
    ), kind=DatabaseEngineKind.APPLICATION)
    factory = database.create_session_factory(engine)
    try:
        assert engine.sync_engine._manor_database_runtime.policy == AdmissionPolicy(1, 0, 0.08)
        async with factory() as holding:
            assert await holding.scalar(select(literal(1))) == 1
            async with factory(info={"database_probe": True}) as probe:
                assert await probe.scalar(select(literal(2))) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_does_not_wait_for_an_inherited_business_admission_group(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine(DATABASE_MAX_WAITERS=0, DATABASE_ADMISSION_TIMEOUT=0.08)
    group = DatabaseAdmissionGroup(limit=1, timeout=0.1)

    async def check_probe():
        async with factory(info={"database_probe": True}) as probe:
            assert await probe.scalar(select(literal(2))) == 2
            assert "database_admission_group" not in probe.sync_session.info

    token = database_admission_group.set(group)
    try:
        async with factory() as business:
            assert await business.scalar(select(literal(1))) == 1
            await asyncio.create_task(check_probe())
    finally:
        database_admission_group.reset(token)
    assert not group.holders


@pytest.mark.asyncio
async def test_cancelled_queued_session_does_not_leak_admission(managed_db):
    engine, factory = managed_db
    runtime = engine.sync_engine._manor_database_runtime

    async def wait_for_connection():
        async with factory() as db:
            await db.scalar(select(literal(2)))

    async with factory() as holding:
        await holding.scalar(select(literal(1)))
        # Another request's task, not one started by the permit holder.
        waiting = asyncio.create_task(wait_for_connection(), context=contextvars.Context())
        try:
            async with asyncio.timeout(0.5):
                while runtime.snapshot()["waiting"] == 0:
                    await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert runtime.snapshot()["waiting"] == 0
            assert runtime.snapshot()["active_transactions"] == 1
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert runtime.snapshot()["active_transactions"] == 0
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_get_bind_peek_does_not_acquire_permit(managed_db):
    engine, factory = managed_db
    async with factory() as db:
        assert db.get_bind() is engine.sync_engine
        assert await db.run_sync(lambda sync: sync.get_bind()) is engine.sync_engine
        assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_long_hold_has_request_id_and_invalidated_connections_balance(managed_db, caplog):
    engine, factory = managed_db
    runtime = engine.sync_engine._manor_database_runtime
    runtime.warn_seconds = 0.01
    token = database_request_id.set("long-hold-test")
    try:
        async with factory() as db:
            await db.scalar(select(literal(1)))
            await asyncio.sleep(0.03)
            await db.invalidate()
        assert runtime.checked_out == 0
        assert runtime.long_holds == 1
        assert "request_id=long-hold-test" in caplog.text
    finally:
        database_request_id.reset(token)


@pytest.mark.asyncio
async def test_transaction_local_timeouts_reset_after_commit(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    settings = runtime_settings(
        TEST_DATABASE_URL, DATABASE_STATEMENT_TIMEOUT_MS=250,
        DATABASE_LOCK_TIMEOUT_MS=50, DATABASE_IDLE_TRANSACTION_TIMEOUT_MS=500,
    )
    engine = database.create_database_engine(settings, kind=DatabaseEngineKind.APPLICATION)
    try:
        async with database.create_session_factory(engine)() as db:
            values = (await db.execute(text(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), "
                "current_setting('idle_in_transaction_session_timeout')"
            ))).one()
            assert values == ("250ms", "50ms", "500ms")
            await db.commit()
        # Remove only the test's local-setting hook; a reused backend must not
        # retain the previous transaction's settings (PgBouncer-safe semantics).
        event.remove(engine.sync_engine, "begin", engine.sync_engine._manor_database_runtime.begin)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW statement_timeout")) == "0"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_factories_share_admission_within_loop(managed_db):
    from conftest import TEST_DATABASE_URL

    engine, factory = managed_db
    worker = database.create_database_engine(
        runtime_settings(TEST_DATABASE_URL, DATABASE_MAX_WAITERS=32, DATABASE_ADMISSION_TIMEOUT=1),
        kind="worker",
    )
    try:
        assert isinstance(worker.pool, NullPool)
        async with factory() as holding:
            await holding.scalar(select(literal(1)))
            with pytest.raises(DatabaseBusyError):
                await _another_request(database.create_session_factory(worker), 2)
            assert worker.sync_engine._manor_database_runtime.checkout_count == 0
        async with database.create_session_factory(worker)() as recovered:
            assert await recovered.scalar(select(literal(3))) == 3
        assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 0
    finally:
        await worker.dispose()


@pytest.mark.asyncio
async def test_statement_timeout_returns_connection_and_can_recover(_test_database_guard):
    from conftest import TEST_DATABASE_URL

    engine = database.create_database_engine(
        runtime_settings(TEST_DATABASE_URL, DATABASE_STATEMENT_TIMEOUT_MS=100),
        kind=DatabaseEngineKind.APPLICATION,
    )
    try:
        factory = database.create_session_factory(engine)
        async with factory() as db:
            with pytest.raises(DBAPIError, match="statement timeout"):
                await db.execute(text("SELECT pg_sleep(1)"))
            await db.rollback()
        assert engine.pool.checkedout() == 0
        assert engine.sync_engine._manor_database_runtime.snapshot()["active_transactions"] == 0
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authenticated_dependencies_release_reads_and_recheck_revocation(client, monkeypatch):
    from apps.api.deps import require_permission, require_role
    from packages.core.database_runtime import configure_database_runtime
    from packages.core.models.user import User
    from packages.core.permissions import Permission
    from conftest import TEST_DATABASE_URL

    runtime = configure_database_runtime(
        database.engine, runtime_settings(TEST_DATABASE_URL), DatabaseEngineKind.APPLICATION,
    )
    factory = database.create_session_factory(database.engine)
    monkeypatch.setattr(database, "async_session", factory)
    registered = await client.post("/api/v1/auth/register", json={
        "username": "pool_auth_owner", "email": "pool-auth-owner@example.com",
        "password": "securepass123", "entity_name": "Pool Auth Test",
    })
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    app = FastAPI()

    async def snapshot_check(db, user):
        assert not db.in_transaction()
        assert runtime.checked_out == 0
        assert runtime.snapshot()["active_transactions"] == 0
        await asyncio.sleep(0)  # route's external work starts after auth reads
        return {"id": user.id}

    @app.get("/role")
    async def role_check(user=Depends(require_role("owner")), db=Depends(database.get_db)):
        return await snapshot_check(db, user)

    @app.get("/permission")
    async def permission_check(
        user=Depends(require_permission(Permission.ADMIN_SETTINGS)), db=Depends(database.get_db),
    ):
        return await snapshot_check(db, user)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        for path in ("/role", "/permission"):
            response = await http.get(path, headers=headers)
            assert response.status_code == 200, response.text
        async with factory() as db:
            user = await db.get(User, registered.json()["user_id"])
            user.token_version += 1
            await db.commit()
        for path in ("/role", "/permission"):
            response = await http.get(path, headers=headers)
            assert response.status_code == 401, response.text
    assert runtime.checked_out == 0
    assert runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_workspace_entrypoint_commits_and_releases_before_sse(managed_db, monkeypatch):
    from apps.api.routers import chat, workspace_chat
    from packages.core.services import workspace_access, workspace_workflow_router

    engine, factory = managed_db

    async def read_boundary(db, *args, **kwargs):
        await db.scalar(select(literal(1)))
        return True

    async def entrypoint(*args, **kwargs):
        return SimpleNamespace(), SimpleNamespace(), SimpleNamespace()

    async def attachments(*args, **kwargs):
        return SimpleNamespace(cleaned_message="run flow", attachments=[])

    async def start(db, **kwargs):
        db.add(Probe(id=5, value="admitted"))
        await db.flush()
        return SimpleNamespace(
            conversation=SimpleNamespace(id="conversation"),
            user_message=SimpleNamespace(id="user-message", meta={}),
            activity_message=SimpleNamespace(id="activity", content="Workflow started."),
        )

    monkeypatch.setattr(workspace_chat, "_verify_workspace", read_boundary)
    monkeypatch.setattr(workspace_access, "user_can_write_workspace_artifacts", read_boundary)
    monkeypatch.setattr(workspace_workflow_router, "get_workspace_chat_entrypoint", entrypoint)
    monkeypatch.setattr(workspace_workflow_router, "start_workspace_chat_entrypoint", start)
    monkeypatch.setattr(chat, "_build_attachments", attachments)
    async with factory() as db:
        response = await workspace_chat.stream_chat_entrypoint(
            workspace_id="workspace", binding_id="binding", message="run flow",
            conversation_id=None, client_message_id=None, local_worker_id=None,
            thread_ref_kind=None, thread_ref_id=None, document_ids=None, files=[],
            _gate=None, user=SimpleNamespace(id="user", entity_id="entity", role="owner"), db=db,
        )
        assert engine.pool.checkedout() == 0
        assert not db.in_transaction()
        async with factory() as observer:
            assert (await observer.get(Probe, 5)).value == "admitted"
        frames = [frame async for frame in response.body_iterator]
        assert any("event: stream_end" in frame for frame in frames)
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_flow_start_releases_request_transaction_before_inline_run(managed_db, monkeypatch, stream):
    """The inline runner needs its own admission slot (claim, runner session).

    With the deployed budget of one slot per API loop, a request session still
    in a transaction would make every Flow start time out on its own claim.
    """
    from apps.api.routers import workflows

    engine, factory = managed_db
    ran = []

    async def allowed(*args, **kwargs):
        return None

    async def start_workflow(db, **kwargs):
        db.add(Probe(id=7, value="started"))
        await db.flush()
        return SimpleNamespace(id="run-1")

    async def prime(db, workflow_id, run_id):
        await db.scalar(select(literal(1)))
        return {}

    class Runner:
        def __init__(self, **kwargs):
            pass

        async def run(self, run_id, **kwargs):
            async with factory() as claim:
                assert (await claim.get(Probe, 7)).value == "started"
            ran.append(run_id)

    async def refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(workflows, "_require_workflow", allowed)
    monkeypatch.setattr(workflows.svc, "start_workflow", start_workflow)
    monkeypatch.setattr(workflows.svc, "get_run", AsyncMock(return_value=SimpleNamespace(id="run-1")))
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(workflows, "_prime_run_cache", prime)
    monkeypatch.setattr(workflows, "WorkflowRunner", Runner)
    monkeypatch.setattr(workflows, "_run_to_dict", lambda run: {"id": run.id})
    user = SimpleNamespace(id="user", entity_id="entity")
    async with factory() as db:
        monkeypatch.setattr(db, "refresh", refresh)
        if stream:
            response = await workflows.start_run_stream("flow", None, user=user, db=db)
            assert not db.in_transaction()
            frames = [frame async for frame in response.body_iterator if "event: node" not in frame]
            assert not any("event: error" in frame for frame in frames), frames
        else:
            await workflows.start_run("flow", None, user=user, db=db)
    assert ran == ["run-1"]
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_workflow_step_releases_run_read_before_tool_takes_its_own_slot(managed_db, monkeypatch):
    """A tool authorizes on its own session; the run's pause/cancel read must
    not keep the worker's only admission slot (staging Flow failed this way)."""
    from packages.core.ai.workflow_runner import WorkflowRunner

    engine, factory = managed_db
    async with factory() as db:
        db.add(Probe(id=8, value="run"))
        await db.commit()
    runner = WorkflowRunner()

    async def tool_step(step, run, db):
        async with factory() as tool_db:
            return {"status": "completed", "output": (await tool_db.get(Probe, 8)).value}

    monkeypatch.setattr(runner, "_execute_step", tool_step)
    run = SimpleNamespace(
        id="run-1", entity_id="entity", variables={}, step_results={}, trigger_data={},
        current_step_id=None, execution_trace=[],
    )
    async with factory() as db:
        await db.get(Probe, 8)  # the pause/cancel check leaves a read open
        result = await runner._execute_step_safe({"id": "tool", "type": "tool", "config": {}}, run, db)
    assert result["status"] == "completed", result
    assert result["output"] == "run"
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_frontier_step_pointer_does_not_hold_the_slot_through_the_step(managed_db, monkeypatch):
    """After an approval the next frontier runs as a batch; moving the run's
    progress pointer must not keep the worker's only slot for the step
    (the video step's credit check timed out on staging this way)."""
    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.workflow_runner import WorkflowRunner

    engine, factory = managed_db
    async with factory() as db:
        db.add(RunProbe(id=1, current_step_id="approve_social_copy"))
        await db.commit()
    runner = WorkflowRunner()

    async def video_step(step, run, db):
        async with factory() as credit_check:
            return {"status": "completed", "output": (await credit_check.get(RunProbe, 1)).current_step_id}

    monkeypatch.setattr(runner, "_execute_step", video_step)
    async with factory() as db:
        run = await db.get(RunProbe, 1)
        for name, value in {"entity_id": "entity", "variables": {}, "step_results": {}, "trigger_data": {}, "execution_trace": []}.items():
            setattr(run, name, value)
        run.current_step_id = "generate_social_image"
        result = await runner._execute_step_safe({"id": "generate_social_video", "type": "agent", "config": {}}, run, db)
        assert result.get("status") == "completed" and result.get("output") == "generate_social_video", result

        # Any other pending change is never committed on the step's behalf.
        run.status = "failed"
        run.current_step_id = "other"
        assert await runner_module._commit_step_pointer_only(db, run) is False
        await db.rollback()
    assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("before_claim", [True, False])
async def test_group_overload_defers_only_unclaimed_runs(monkeypatch, before_claim):
    from packages.core.services import room_agent_runtime

    run = SimpleNamespace(status="pending", checkpoint=None)
    calls = []
    commits = []

    class Session:
        async def __aenter__(self):
            calls.append(self)
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args):
            if before_claim:
                raise DatabaseBusyError("Database admission queue is full")
            return SimpleNamespace(scalar_one_or_none=lambda: run)

        async def commit(self):
            commits.append(run.status)

    loop = AsyncMock(side_effect=DatabaseBusyError("Database admission queue is full"))
    monkeypatch.setattr(room_agent_runtime, "async_session", Session)
    turn = room_agent_runtime.RoomAgentTurn(
        run_id="run", room_id="room", source_message_id="message", binding_id="binding",
        entity_id="entity", agent_id="agent", workspace_id=None, agent_name="Agent",
        agent_avatar_url=None, model=None, temperature=None, max_tokens=None,
        requester_label="User", requester_name="User", current_message="Hello", history=[],
    )
    monkeypatch.setattr(room_agent_runtime, "_admit_run", AsyncMock(
        return_value=(turn, None),
    ))
    monkeypatch.setattr(room_agent_runtime, "run_room_agent_completion", loop)
    monkeypatch.setattr(room_agent_runtime, "queue_chat_room_change_after_commit", lambda *a, **k: None)
    run.room_id = "room"
    await room_agent_runtime.execute_room_agent_run("run")
    if before_claim:
        assert len(calls) == 1 and commits == [] and run.status == "pending"
        loop.assert_not_awaited()
    else:
        assert len(calls) == 2 and commits == ["running", "failed"]
        loop.assert_awaited_once()
        assert run.error_code == "DatabaseBusyError"


# -- Admission groups: many concurrent runs of one request ---------------------


@pytest_asyncio.fixture
async def tight_engine(_test_database_guard):
    """The chat pod's shape: one admitted transaction, a short queue and wait."""
    from conftest import TEST_DATABASE_URL

    engines = []

    def make(**overrides):
        settings = runtime_settings(TEST_DATABASE_URL, **{
            "DATABASE_ADMISSION_LIMIT": 1, "DATABASE_MAX_WAITERS": 3,
            "DATABASE_ADMISSION_TIMEOUT": 0.3, **overrides,
        })
        engine = database.create_database_engine(settings, kind=DatabaseEngineKind.APPLICATION)
        engines.append(engine)
        return database.create_session_factory(engine)

    try:
        yield make
    finally:
        for engine in engines:
            await engine.dispose()


async def _member(factory, rounds=3):
    for _ in range(rounds):
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
            await asyncio.sleep(0.03)  # a short transaction held by real work
            await db.commit()
    return "ok"


@pytest.mark.asyncio
async def test_twenty_runs_of_one_request_queue_as_one_caller_instead_of_timing_out(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine()
    # Without a group, twenty concurrent runs overflow the queue and time out.
    alone = await asyncio.gather(*(_member(factory) for _ in range(20)), return_exceptions=True)
    assert any(isinstance(outcome, DatabaseBusyError) for outcome in alone)

    group = DatabaseAdmissionGroup(limit=1, timeout=30)
    token = database_admission_group.set(group)
    try:
        members = [asyncio.create_task(_member(factory)) for _ in range(20)]
    finally:
        database_admission_group.reset(token)
    await asyncio.sleep(0.05)
    # Another request on the same process still gets through in its own wait.
    assert await _member(factory, rounds=1) == "ok"
    assert await asyncio.gather(*members) == ["ok"] * 20
    assert group.peak_active == 1 and not group.holders


@pytest.mark.asyncio
async def test_a_member_opening_a_second_session_does_not_wait_on_its_own_permit(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine(DATABASE_ADMISSION_LIMIT=2, DATABASE_POOL_SIZE=3)
    group = DatabaseAdmissionGroup(limit=1, timeout=0.5)
    token = database_admission_group.set(group)
    try:
        async with factory() as outer:
            assert await outer.scalar(select(literal(1))) == 1
            async with factory() as inner:
                assert await inner.scalar(select(literal(2))) == 2
            await outer.commit()
    finally:
        database_admission_group.reset(token)
    assert not group.holders


# -- A permit holder's own independent Sessions --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting", ["same_task", "awaited_child_task", "worker_factory"])
async def test_a_permit_holder_never_waits_on_its_own_permit(tight_engine, nesting):
    """The workspace-draft shape: a turn holds the only permit (its draft row
    lock) while the credit preflight or a locale cache fill opens its own
    Session. That Session must not queue behind the caller's own permit, while
    unrelated callers stay bounded by the gate."""
    from conftest import TEST_DATABASE_URL

    factory = tight_engine()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    inner_factory, worker = factory, None
    if nesting == "worker_factory":
        # create_worker_session() on the chat loop: same policy, same gate.
        worker = database.create_database_engine(runtime_settings(TEST_DATABASE_URL, **{
            "DATABASE_ADMISSION_LIMIT": 1, "DATABASE_MAX_WAITERS": 3,
            "DATABASE_ADMISSION_TIMEOUT": 0.3,
        }), kind=DatabaseEngineKind.WORKER)
        inner_factory = database.create_session_factory(worker)

    async def independent_read():
        async with inner_factory() as db:
            return await db.scalar(select(literal(2)))

    holding = asyncio.Event()

    async def unrelated_request():
        await holding.wait()
        async with factory() as db:
            return await db.scalar(select(literal(3)))

    # Created before the permit is taken, so it is not part of the holder.
    other = asyncio.create_task(unrelated_request())
    try:
        async with factory() as outer:
            await outer.scalar(select(literal(1)))
            holding.set()
            if nesting == "awaited_child_task":
                assert await asyncio.create_task(independent_read()) == 2
            else:
                assert await independent_read() == 2
            assert outer.in_transaction()
            with pytest.raises(DatabaseBusyError):
                await other
            snapshot = runtime.snapshot()
            assert snapshot["active_transactions"] == 1
            assert snapshot["nested_admissions"] == 1
            assert snapshot["wait_timeouts"] == 1  # only the unrelated request
        snapshot = runtime.snapshot()
        assert snapshot["active_transactions"] == snapshot["active_nested_transactions"] == 0
        async with factory() as recovered:
            assert await recovered.scalar(select(literal(4))) == 4
    finally:
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)
        if worker is not None:
            await worker.dispose()


@pytest.mark.asyncio
async def test_credit_preflight_inside_the_permit_holders_transaction(db_session, tight_engine, monkeypatch, caplog):
    """Regression: staging chat (admission limit 1) failed every workspace
    draft with credit_check_unavailable after the 2s admission wait."""
    from decimal import Decimal

    from packages.core.ai import llm_client
    from packages.core.constants import plans
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Admission credit regression"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Admission credit regression",
                  monthly_budget_usd=Decimal("1"), monthly_spent_usd=Decimal("0")),
    ])
    await db_session.commit()
    factory = tight_engine()
    monkeypatch.setattr(plans, "ai_credit_limits_enabled", lambda: False)
    token = llm_client._billing_ctx_var.set(
        llm_client.LLMBillingContext(entity_id=entity_id, workspace_id=workspace_id),
    )
    try:
        async with factory() as turn:
            # The draft turn's row lock keeps the only permit for the whole turn.
            await turn.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
            await llm_client._preflight_credit_check(session_factory=factory)
            await turn.commit()

        # Genuine overload still fails closed, and is logged as such.
        holding, done = asyncio.Event(), asyncio.Event()

        async def other_request():
            async with factory() as db:
                await db.scalar(select(literal(1)))
                holding.set()
                await done.wait()

        other = asyncio.create_task(other_request())
        try:
            await holding.wait()
            with pytest.raises(llm_client.CreditCheckUnavailableError, match="credit_check_unavailable"):
                await llm_client._preflight_credit_check(session_factory=factory)
        finally:
            done.set()
            await other
        assert "credit_check_database_busy" in caplog.text
    finally:
        llm_client._billing_ctx_var.reset(token)


@pytest.mark.asyncio
async def test_session_permit_taken_in_a_child_task_still_belongs_to_its_caller(tight_engine):
    """A tool run by agentic_loop in a child task may commit and re-lock the
    caller's Session; the caller's next independent Session must still be
    admitted as its own, not wait on the permit now held by that Session."""
    factory = tight_engine()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    async with factory() as turn:
        await turn.scalar(select(literal(1)))

        async def tool_commits_and_relocks():
            await turn.commit()
            await turn.scalar(select(literal(2)))

        await asyncio.ensure_future(tool_commits_and_relocks())
        assert turn.in_transaction()
        async with factory() as preflight:
            assert await preflight.scalar(select(literal(3))) == 3
        assert runtime.snapshot()["wait_timeouts"] == 0
    assert runtime.snapshot()["active_transactions"] == 0


@pytest.mark.asyncio
async def test_workspace_draft_turn_runs_model_rounds_without_holding_admission(
    db_session, tight_engine, monkeypatch,
):
    """The staging draft turn: several model rounds, each preceded by the real
    credit preflight, with tools between them that run in agentic_loop child
    tasks. One tool (like ws_search_capabilities) locks the draft in two short
    draft_phase transactions. With one chat permit, no round may wait on the turn's own draft
    transaction, and the turn must persist its fields, messages and status."""
    from decimal import Decimal
    import json

    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.ai import agentic_loop, llm_client
    from packages.core.ai.tools import workspace_arch_tools
    from packages.core.constants import plans
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_architect
    from packages.core.services import workspace_draft_service as drafts

    entity_id, user_id, workspace_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Draft turn admission"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Budget probe",
                  monthly_budget_usd=Decimal("1"), monthly_spent_usd=Decimal("0")),
    ])
    await db_session.commit()
    factory = tight_engine()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    async with factory() as db:
        draft_id = (await drafts.create_draft_shell(
            db, entity_id=entity_id, user_id=user_id, initial_brief="A bakery workspace",
        )).id
        await db.commit()

    async def capability_matcher_like(db, *, entity_id, user_id, draft_id, draft_phase, **_):
        # ws_search_capabilities: read the draft, hold nothing during provider
        # work, then re-lock it in the same Session (inside the tool's child task).
        async with draft_phase():
            await db.get(WorkspaceDraft, draft_id)
        assert runtime.snapshot()["active_transactions"] == 0
        async with draft_phase():
            draft = await db.get(WorkspaceDraft, draft_id)
            draft.fields = {**draft.fields, "capability_plan": "matched"}
            flag_modified(draft, "fields")
        return json.dumps({"ok": True})

    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_search_capabilities", capability_matcher_like)
    held_during_rounds = []

    async def model_round():
        held_during_rounds.append(runtime.snapshot()["active_transactions"])
        await llm_client._preflight_credit_check()  # real, DB-backed (Workspace budget)

    async def loop(*, tool_executor, **_):
        async def not_cancelled():
            return False

        for name, args in [
            ("ws_commit_basics", {"name": "Bakery", "kind": "operations"}),
            ("ws_search_capabilities", {}),
            ("ws_commit_basics", {"primary_work": "Daily orders"}),
        ]:
            await model_round()
            result = await agentic_loop._await_cancellable(tool_executor(name, args), not_cancelled)
            assert json.loads(result).get("ok"), result
        await model_round()
        return SimpleNamespace(content="Your bakery workspace is drafted.", usage={}, rounds=4,
                               tool_calls_made=[{}, {}, {}])

    monkeypatch.setattr(workspace_architect, "runtime_execute_workspace_architect_loop", loop)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(plans, "ai_credit_limits_enabled", lambda: False)
    token = llm_client._billing_ctx_var.set(
        llm_client.LLMBillingContext(entity_id=entity_id, workspace_id=workspace_id),
    )
    try:
        async def runner():  # workspace_drafts._stream_turn.runner
            async with factory() as db:
                reply, _ = await drafts.process_draft_message(
                    db, draft_id=draft_id, entity_id=entity_id, user_id=user_id,
                    user_message="begin", dedupe_opening=True,
                )
                await db.commit()
                return reply

        assert await asyncio.create_task(runner()) == "Your bakery workspace is drafted."
    finally:
        llm_client._billing_ctx_var.reset(token)
    assert held_during_rounds == [0, 0, 0, 0]
    assert runtime.snapshot()["wait_timeouts"] == 0
    assert runtime.snapshot()["active_transactions"] == 0
    async with factory() as db:
        draft = await db.get(WorkspaceDraft, draft_id)
        assert draft.fields["name"] == "Bakery"
        assert draft.fields["primary_work"] == "Daily orders"
        assert draft.fields["capability_plan"] == "matched"
        assert [m["role"] for m in draft.messages] == ["assistant"]
        assert not any(key.startswith("_architect_turn") for key in draft.fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["person_edits", "another_turn_takes_over"])
async def test_the_app_designer_runs_while_the_draft_stays_free(db_session, tight_engine, monkeypatch, meanwhile):
    """ws_design_app_view runs its sub-agent for minutes. Meanwhile the draft
    row, the turn's connection and the chat permit must be free, so the
    person's edits land; a turn that lost its claim keeps no candidate."""
    import copy
    import json
    import time
    from pathlib import Path

    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.ai import agentic_loop
    from packages.core.ai.runtime.control import RuntimeTurnAborted
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_app_designer, workspace_architect
    from packages.core.services import workspace_draft_service as drafts

    recipe = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs/creative/creative-presentation-v1.json"
    entity_id, user_id = generate_ulid(), generate_ulid()
    factory = tight_engine()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    async with factory() as db:
        draft = await drafts.create_draft_shell(db, entity_id=entity_id, user_id=user_id, initial_brief="Decks")
        draft.fields = {**draft.fields, "workflows": json.loads(recipe.read_text())["recipe"]["workflows"]}
        draft_id = draft.id
        await db.commit()

    async def designer(context, **_):
        assert runtime.snapshot()["active_transactions"] == 0

        async def meanwhile_on_the_draft():
            async with factory() as db:
                row = (await db.execute(
                    select(WorkspaceDraft).where(WorkspaceDraft.id == draft_id).with_for_update(nowait=True)
                )).scalar_one()
                fields = dict(row.fields)
                if meanwhile == "person_edits":
                    fields["primary_work"] = "Edited while the App was designed"
                else:
                    fields[drafts.TURN_CLAIM_FIELD] = {"id": "another-turn", "expires_at": time.time() + 300}
                row.fields = fields
                flag_modified(row, "fields")
                await db.commit()

        # Another request: not a nested Session of the turn.
        await asyncio.create_task(meanwhile_on_the_draft(), context=contextvars.Context())
        config, errors, _ = context.check(copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"]))
        assert not errors
        return workspace_app_designer.AppDesignResult(
            app_view=config, recipes=["one flow, several kinds"], warnings=[], notes="Designed.", attempts=1,
        )

    async def loop(*, tool_executor, **_):
        async def not_cancelled():
            return False

        result = await agentic_loop._await_cancellable(
            tool_executor("ws_design_app_view", {"brief": "Make decks of different kinds."}), not_cancelled,
        )
        assert json.loads(result)["ok"], result
        return SimpleNamespace(content="The App is designed.", usage={}, rounds=2, tool_calls_made=[{}])

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", designer)
    monkeypatch.setattr(workspace_architect, "runtime_execute_workspace_architect_loop", loop)
    monkeypatch.setattr(database, "async_session", factory)

    async def turn():
        async with factory() as db:
            reply, _ = await drafts.process_draft_message(
                db, draft_id=draft_id, entity_id=entity_id, user_id=user_id, user_message="Design the App",
            )
            await db.commit()
            return reply

    if meanwhile == "person_edits":
        assert await asyncio.create_task(turn()) == "The App is designed."
    else:
        with pytest.raises(RuntimeTurnAborted, match="took over"):
            await asyncio.create_task(turn())
    assert runtime.snapshot()["wait_timeouts"] == 0
    async with factory() as db:
        fields = (await db.get(WorkspaceDraft, draft_id)).fields
    if meanwhile == "person_edits":
        assert fields["primary_work"] == "Edited while the App was designed"
        assert fields["_candidates"]["app"]["recipes"] == ["one flow, several kinds"]
        assert drafts.TURN_CLAIM_FIELD not in fields
    else:
        assert "_candidates" not in fields
        assert fields[drafts.TURN_CLAIM_FIELD]["id"] == "another-turn"


async def _draft_turn_with_slow_model(tight_engine, monkeypatch, *, meanwhile, calls, patch_slow):
    """Run one Architect turn making ``calls``. ``patch_slow(while_slow)`` makes
    one slow model call await ``while_slow``, which edits the draft from another
    request. Returns the tool results and the draft's final fields."""
    import json
    import time

    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.ai import agentic_loop
    from packages.core.ai.runtime.control import RuntimeTurnAborted
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_architect
    from packages.core.services import workspace_draft_service as drafts
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCandidate, AgentCapabilityCatalog, AgentCapabilityCatalogFactory, AgentCapabilityKind,
    )

    entity_id, user_id = generate_ulid(), generate_ulid()
    factory = tight_engine()
    runtime = factory.kw["bind"].sync_engine._manor_database_runtime
    async with factory() as db:
        draft = await drafts.create_draft_shell(db, entity_id=entity_id, user_id=user_id, initial_brief="Research")
        draft.fields = {**draft.fields, "services": [
            {"service_key": "research", "name": "Research", "description": "Find and verify sources."},
        ]}
        draft_id = draft.id
        await db.commit()

    held_while_slow = []

    async def while_slow():
        held_while_slow.append(runtime.snapshot()["active_transactions"])

        async def meanwhile_on_the_draft():
            async with factory() as db:
                row = (await db.execute(
                    select(WorkspaceDraft).where(WorkspaceDraft.id == draft_id).with_for_update(nowait=True)
                )).scalar_one()
                fields = dict(row.fields)
                if meanwhile == "person_edits":
                    fields["name"] = "Edited while the model worked"
                else:
                    fields[drafts.TURN_CLAIM_FIELD] = {"id": "another-turn", "expires_at": time.time() + 300}
                row.fields = fields
                flag_modified(row, "fields")
                await db.commit()

        # Another request: not a nested Session of the turn.
        await asyncio.create_task(meanwhile_on_the_draft(), context=contextvars.Context())

    async def catalog(*_, **__):
        return AgentCapabilityCatalog(candidates=(
            AgentCapabilityCandidate("tool:invoke_skill", AgentCapabilityKind.TOOL, "invoke_skill", "Skills"),
            AgentCapabilityCandidate("tool:web_search", AgentCapabilityKind.TOOL, "web_search", "Search"),
            AgentCapabilityCandidate("skill:research", AgentCapabilityKind.SKILL, "research", "Research",
                                     metadata={"declared_tools": ["web_search"]}),
        ))

    results = []

    async def loop(*, tool_executor, **_):
        async def not_cancelled():
            return False

        for name, args in calls:
            results.append(json.loads(await agentic_loop._await_cancellable(
                tool_executor(name, args), not_cancelled,
            )))
        return SimpleNamespace(content="Done.", usage={}, rounds=len(calls) + 1, tool_calls_made=[{}] * len(calls))

    patch_slow(while_slow)
    monkeypatch.setattr(AgentCapabilityCatalogFactory, "create", catalog)
    monkeypatch.setattr(workspace_architect, "runtime_execute_workspace_architect_loop", loop)
    monkeypatch.setattr(database, "async_session", factory)

    async def turn():
        async with factory() as db:
            await drafts.process_draft_message(
                db, draft_id=draft_id, entity_id=entity_id, user_id=user_id, user_message="Design the Agent",
            )
            await db.commit()

    aborted = None
    try:
        await asyncio.create_task(turn())
    except RuntimeTurnAborted as exc:
        aborted = exc
    assert held_while_slow == [0], results
    assert runtime.snapshot()["wait_timeouts"] == 0
    async with factory() as db:
        fields = (await db.get(WorkspaceDraft, draft_id)).fields
    if meanwhile == "person_edits":
        assert aborted is None, aborted
        assert fields["name"] == "Edited while the model worked"
        assert drafts.TURN_CLAIM_FIELD not in fields
    else:
        assert "took over" in str(aborted)
        assert fields[drafts.TURN_CLAIM_FIELD]["id"] == "another-turn"
    return results, fields


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["person_edits", "another_turn_takes_over"])
async def test_the_capability_matcher_runs_while_the_draft_stays_free(tight_engine, monkeypatch, meanwhile):
    """ws_search_capabilities waits on the semantic matcher. Meanwhile the draft
    row, the turn's connection and the chat permit must be free, so the
    person's edits land; a turn that lost its claim saves no plan."""
    from packages.core.ai.tools.workspace_arch_tools import _AGENT_CAPABILITY_PLANS_FIELD
    from packages.core.services import agent_generator

    def patch_slow(while_slow):
        async def match(*, capability_catalog, **_):
            await while_slow()
            return capability_catalog.resolve(["tool:web_search"])

        monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)

    results, fields = await _draft_turn_with_slow_model(
        tight_engine, monkeypatch, meanwhile=meanwhile, patch_slow=patch_slow,
        calls=[("ws_search_capabilities", {"service_key": "research"})],
    )
    if meanwhile == "person_edits":
        assert results[0]["ok"], results
        assert [plan["capability_ids"] for plan in fields[_AGENT_CAPABILITY_PLANS_FIELD]] == [["tool:web_search"]]
    else:
        assert not fields.get(_AGENT_CAPABILITY_PLANS_FIELD)




@pytest.mark.asyncio
async def test_a_member_that_times_out_at_the_process_gate_frees_the_group(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine(DATABASE_ADMISSION_TIMEOUT=0.1)
    group = DatabaseAdmissionGroup(limit=1, timeout=0.3)
    holding, done = asyncio.Event(), asyncio.Event()

    async def other_request():
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1  # holds the only slot
            holding.set()
            await done.wait()
            await db.commit()

    other = asyncio.create_task(other_request(), context=contextvars.Context())
    await holding.wait()
    token = database_admission_group.set(group)
    try:
        with pytest.raises(DatabaseBusyError):
            async with factory() as db:
                await db.scalar(select(literal(2)))
    finally:
        database_admission_group.reset(token)
        done.set()
        await other
    assert not group.holders
    token = database_admission_group.set(group)
    try:
        assert await _member(factory, rounds=1) == "ok"
    finally:
        database_admission_group.reset(token)


@pytest.mark.asyncio
async def test_the_groups_waiter_outlasts_another_requests_long_transaction(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine(DATABASE_ADMISSION_TIMEOUT=0.1)

    async def long_request():
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
            await asyncio.sleep(0.4)  # longer than the process wait budget
            await db.commit()

    holder = asyncio.create_task(long_request())
    await asyncio.sleep(0.05)
    group = DatabaseAdmissionGroup(limit=1, timeout=5)
    token = database_admission_group.set(group)
    try:
        assert await _member(factory, rounds=1) == "ok"
    finally:
        database_admission_group.reset(token)
    await holder
    assert not group.holders


@pytest.mark.asyncio
async def test_a_group_lets_requests_already_waiting_go_first(tight_engine):
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    factory = tight_engine(DATABASE_ADMISSION_TIMEOUT=2)
    order: list[str] = []

    async def caller(name, hold=0.0):
        async with factory() as db:
            assert await db.scalar(select(literal(1))) == 1
            order.append(name)
            await asyncio.sleep(hold)
            await db.commit()

    holder = asyncio.create_task(caller("holder", hold=0.2))
    await asyncio.sleep(0.05)
    first = asyncio.create_task(caller("first request", hold=0.1))
    await asyncio.sleep(0.02)  # the first request is waiting at the gate
    token = database_admission_group.set(DatabaseAdmissionGroup(limit=1, timeout=5))
    try:
        member = asyncio.create_task(caller("group member"))
    finally:
        database_admission_group.reset(token)
    await asyncio.sleep(0.05)
    # A request arriving after the member still goes first: the group never
    # queues while someone else is waiting.
    later = asyncio.create_task(caller("later request"))
    await asyncio.gather(holder, first, member, later)
    assert order == ["holder", "first request", "later request", "group member"]
