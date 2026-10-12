"""Worker auth frees DB slots, bounds CPU work and rechecks authorization."""
from __future__ import annotations

import asyncio
from copy import copy
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from packages.core.workers import authentication as auth


def worker(**changes):
    return SimpleNamespace(**{
        "id": "worker-1", "entity_id": "entity-1", "secret_hash": "hash-1",
        "status": "active", "expires_at": None, "kind": "custom_http",
        "created_by_user_id": "user-1", "allowed_ips": [],
        "capabilities": {"protocol_version": 2}, **changes,
    })


@pytest.mark.asyncio
async def test_verification_is_off_loop_and_cancelled_requests_keep_the_cpu_slot(monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    def blocking_check(*_args):
        started.set()
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(3)
        return True

    monkeypatch.setattr(auth, "_verify_secret_hash", blocking_check)
    verifier = auth.WorkerSecretVerifier(concurrency=1, max_waiters=1, timeout=0.03)
    first = asyncio.create_task(verifier.verify("hash", "secret"))
    try:
        # This coroutine can run while the bcrypt thread is blocked.
        await asyncio.wait_for(entered.wait(), 1)
        assert started.is_set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(verifier.verify("hash", "secret"))
        await asyncio.sleep(0)
        with pytest.raises(auth.WorkerAuthenticationBusy, match="queue is full"):
            await verifier.verify("hash", "secret")
        with pytest.raises(auth.WorkerAuthenticationBusy, match="timed out"):
            await second
        assert verifier.waiting == 0
        assert verifier._slots.locked()
    finally:
        finish.set()
    assert await asyncio.wait_for(verifier.verify("hash", "secret"), 1)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_queue_capacity(monkeypatch):
    verifier = auth.WorkerSecretVerifier(concurrency=1, max_waiters=1, timeout=0.1)
    await verifier._slots.acquire()
    pending = asyncio.create_task(verifier.verify("hash", "secret"))
    await asyncio.sleep(0)
    assert verifier.waiting == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert verifier.waiting == 0
    verifier._slots.release()


@pytest.mark.asyncio
async def test_real_bcrypt_valid_invalid_and_malformed_hash():
    verifier = auth.WorkerSecretVerifier()
    secret_hash = bcrypt.hashpw(b"secret", bcrypt.gensalt(rounds=4)).decode()
    assert await verifier.verify(secret_hash, "secret")
    assert not await verifier.verify(secret_hash, "wrong")
    assert not await verifier.verify("not-a-bcrypt-hash", "secret")


@pytest.mark.asyncio
async def test_successful_verification_is_cached_per_stored_hash_and_secret(monkeypatch):
    calls = []

    def check(secret_hash, presented):
        calls.append((secret_hash, presented))
        return presented == "secret"

    monkeypatch.setattr(auth, "_verify_secret_hash", check)
    verifier = auth.WorkerSecretVerifier()
    assert await verifier.verify("hash-1", "secret")
    assert await verifier.verify("hash-1", "secret")
    assert len(calls) == 1
    # Failures are never cached, a rotated stored hash is a miss, and a
    # different presented secret never rides on a cached success.
    assert not await verifier.verify("hash-1", "wrong")
    assert not await verifier.verify("hash-1", "wrong")
    assert await verifier.verify("hash-2", "secret")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_cached_verification_expires_and_is_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "_verify_secret_hash", lambda h, s: calls.append(h) or True)
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    verifier = auth.WorkerSecretVerifier(cache_ttl=10, cache_size=2)
    for secret_hash in ("a", "b", "c"):
        assert await verifier.verify(secret_hash, "secret")
    assert len(verifier._verified) == 2
    assert await verifier.verify("a", "secret")  # evicted: verified again
    assert calls == ["a", "b", "c", "a"]
    clock[0] += 11
    assert await verifier.verify("a", "secret")  # expired: verified again
    assert calls[-1] == "a" and len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"status": "revoked"}, {"secret_hash": "rotated"}, {"entity_id": "other-tenant"},
    {"expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}, None,
])
async def test_authorization_changes_during_bcrypt_are_rejected(monkeypatch, change):
    initial = worker()
    refreshed = worker(**change) if change is not None else None
    lookup = AsyncMock(side_effect=[initial, refreshed])
    release = AsyncMock()

    async def verify(*_args):
        assert release.await_count == 1, "bcrypt must not hold the read transaction"
        return True

    monkeypatch.setattr(auth, "get_worker", lookup)
    monkeypatch.setattr(auth, "release_read_transaction", release)
    monkeypatch.setattr(auth, "_verifier", lambda: SimpleNamespace(verify=verify))
    assert await auth.authenticate_worker_secret(object(), initial.id, "secret") is None
    assert lookup.await_args_list[1].kwargs == {"fresh": True}
    assert release.await_count == 2


@pytest.mark.asyncio
async def test_returns_current_policy_instead_of_cached_orm_row(monkeypatch):
    current = worker(status="paused", allowed_ips=["192.0.2.1"], capabilities={"protocol_version": 1})
    monkeypatch.setattr(auth, "get_worker", AsyncMock(side_effect=[worker(), current]))
    monkeypatch.setattr(auth, "release_read_transaction", AsyncMock())
    monkeypatch.setattr(auth, "_verifier", lambda: SimpleNamespace(verify=AsyncMock(return_value=True)))
    assert await auth.authenticate_worker_secret(object(), current.id, "secret") is current


@pytest.mark.asyncio
async def test_http_auth_overload_does_not_reject_credentials(monkeypatch):
    from apps.api.deps import get_current_worker

    monkeypatch.setattr(auth, "authenticate_worker_secret", AsyncMock(side_effect=auth.WorkerAuthenticationBusy()))
    request = Request({"type": "http", "headers": [(b"manor-worker-id", b"worker-1")]})
    with pytest.raises(HTTPException) as rejected:
        await get_current_worker(request, HTTPAuthorizationCredentials(scheme="Bearer", credentials="secret"), object())
    assert rejected.value.status_code == 503
    assert rejected.value.detail["code"] == "worker_auth_busy"
    assert rejected.value.headers == {"Retry-After": "2"}




@pytest.mark.asyncio
async def test_auth_backpressure_does_not_acquire_database_to_capture_an_error(monkeypatch):
    from apps.api import middleware_core

    record = AsyncMock()
    monkeypatch.setattr(middleware_core, "_record_server_error", record)
    request = Request({"type": "http", "headers": []})
    request.state.request_id = "auth-overload-request"
    response = await middleware_core.http_error_handler(request, HTTPException(
        503, {"code": "worker_auth_busy", "message": "Retry shortly"}, headers={"Retry-After": "2"},
    ))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.headers["x-request-id"] == "auth-overload-request"
    record.assert_not_awaited()
    await middleware_core.http_error_handler(request, HTTPException(500, "Unexpected failure"))
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_refreshed_protocol_and_ip_policy_are_enforced_by_http_auth(monkeypatch):
    from apps.api.deps import get_current_worker

    request = Request({"type": "http", "headers": [
        (b"manor-worker-id", b"worker-1"), (b"manor-protocol-version", b"2"),
    ], "client": ("192.0.2.2", 1234)})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="secret")
    for current, expected in [
        (worker(capabilities={"protocol_version": 1}), 426),
        (worker(allowed_ips=["192.0.2.1"]), 403),
    ]:
        monkeypatch.setattr(auth, "authenticate_worker_secret", AsyncMock(return_value=current))
        with pytest.raises(HTTPException) as rejected:
            await get_current_worker(request, credentials, object())
        assert rejected.value.status_code == expected


@pytest.mark.parametrize("active,expected", [(False, 10), (True, 5)])
def test_heartbeat_cadence_uses_jitter_without_ignoring_operator_preferences(monkeypatch, active, expected):
    from apps.api.routers import workers

    monkeypatch.setattr(workers.random, "uniform", lambda low, high: 1)
    assert workers._next_heartbeat_interval(worker(preferences={}), has_active_leases=active) == expected
    assert workers._next_heartbeat_interval(worker(preferences={"heartbeat_interval_seconds": 2}), has_active_leases=active) == 2
    assert workers._next_heartbeat_interval(worker(preferences={"heartbeat_interval_seconds": "invalid"}), has_active_leases=active) == expected
    monkeypatch.setattr(workers.random, "uniform", lambda low, high: high)
    assert workers._next_heartbeat_interval(worker(preferences={"heartbeat_interval_seconds": 0}), has_active_leases=active) >= 1
    assert workers._next_heartbeat_interval(worker(preferences={"heartbeat_interval_seconds": 1000}), has_active_leases=active) <= 60


@pytest.mark.asyncio
async def test_managed_auth_releases_real_database_slot_and_observes_concurrent_revocation(db_session, monkeypatch):
    from packages.core.config import get_settings
    from packages.core.database import create_database_engine, create_session_factory
    from packages.core.database_runtime import DatabaseEngineKind
    from packages.core.models.worker import Worker
    from packages.core.models.base import generate_ulid
    from sqlalchemy import select, update

    worker_id = generate_ulid()
    db_session.add(Worker(
        id=worker_id, entity_id=generate_ulid(), kind="custom_http",
        display_name="Authentication regression", secret_hash="hash", status="active",
        capabilities={"protocol_version": 2},
    ))
    await db_session.commit()
    settings = copy(get_settings())
    for name, value in {
        "DATABASE_URL": db_session.bind.url.render_as_string(hide_password=False),
        "DATABASE_POOL_MODE": "sqlalchemy",
        "DATABASE_POOL_SIZE": 2, "DATABASE_MAX_OVERFLOW": 0,
        "DATABASE_ADMISSION_LIMIT": 1, "DATABASE_MAX_WAITERS": 1,
        "DATABASE_ADMISSION_TIMEOUT": 0.1,
    }.items():
        setattr(settings, name, value)
    engine = create_database_engine(settings, kind=DatabaseEngineKind.APPLICATION, is_test_database=True)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            # Preload the identity map; the second auth read must overwrite it.
            cached = await session.scalar(select(Worker).where(Worker.id == worker_id))
            await session.commit()

            async def verify(*_args):
                assert not session.in_transaction()
                # The single DB permit is available to a concurrent request
                # throughout bcrypt, allowing the revocation to commit.
                async with factory() as other:
                    await other.execute(update(Worker).where(Worker.id == worker_id).values(status="revoked"))
                    await other.commit()
                assert cached.status == "active", "prove the identity map was stale"
                return True

            monkeypatch.setattr(auth, "_verifier", lambda: SimpleNamespace(verify=verify))
            assert await auth.authenticate_worker_secret(session, worker_id, "secret") is None
            assert cached.status == "revoked"
            assert not session.in_transaction()
    finally:
        await engine.dispose()
