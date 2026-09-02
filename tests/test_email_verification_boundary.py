"""Verifier/password fencing and activation durability, using independent DB sessions."""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.deps import get_current_user
from apps.api.routers import auth
from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.services import email_service
from packages.core.services import email_verification_service as verification
from packages.core.services.auth_service import hash_password, register_user, verify_password
from packages.core.services.event_emitter import drain_external_event_deliveries


class CodeStore:
    def __init__(self):
        self.data = {}
        self.fail_writes = False

    def setex(self, key, _ttl, value):
        if self.fail_writes:
            raise ConnectionError("verification store unavailable")
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def ttl(self, key):
        return 600


@pytest.fixture(params=["redis", "fallback"])
async def code_store(request, monkeypatch, db_session):
    store = CodeStore() if request.param == "redis" else None
    monkeypatch.setattr(verification, "_redis", lambda: store)
    monkeypatch.setattr(verification, "_fallback", {})
    monkeypatch.setattr(auth, "CAPTCHA_ENABLED", False)
    monkeypatch.setattr(auth, "_CLOUD_FEATURES_ENABLED", True)
    monkeypatch.setattr(auth, "send_verification_email", AsyncMock(return_value=True))
    monkeypatch.setattr(email_service, "send_welcome_email", AsyncMock(return_value=True))
    yield store
    # Login activity may schedule event persistence after commit. Finish it
    # before pytest closes this test's loop and database connections.
    await drain_external_event_deliveries(
        persistence_timeout_seconds=5, delivery_timeout_seconds=5,
    )


async def pending_user(db):
    user, _ = await register_user(
        db, email=f"verify-{generate_ulid().lower()}@example.test",
        password="OriginalPassword123!",
    )
    user.status = "pending"
    await db.commit()
    code = await verification.create_verification(
        user.email, user.id, password_hash=user.password_hash,
    )
    return user, code


async def test_new_password_with_failed_code_storage_rejects_old_code(
    db_session, monkeypatch, code_store,
):
    user, original_code = await pending_user(db_session)
    email, user_id = user.email, user.id
    # Inject the failure at the code-store boundary, after the registration commit.
    async def fail_code_store(*_args, **_kwargs):
        raise ConnectionError("verification store unavailable")

    if code_store is not None:
        code_store.fail_writes = True
    else:
        monkeypatch.setattr(auth, "create_verification", fail_code_store)
    with pytest.raises(ConnectionError):
        await auth.register(
            auth.RegisterRequest(email=email, password="ReplacementPassword456!"),
            Request({"type": "http"}), db_session,
        )
    await db_session.rollback()  # request error cleanup cannot undo the earlier commit
    auth.send_verification_email.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        await auth.verify_email_endpoint(
            auth.VerifyEmailRequest(email=email, code=original_code), db_session,
        )
    assert error.value.status_code == 400
    await db_session.rollback()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with sessions() as observer:
        user = await observer.get(User, user_id)
        assert user.status == "pending"
        assert verify_password("ReplacementPassword456!", user.password_hash)

    # Recovery uses a freshly issued, bound code; the original can never be reused.
    if code_store is not None:
        code_store.fail_writes = False
    await auth.resend_verification_endpoint(auth.ResendVerificationRequest(email=email), db_session)
    new_code = auth.send_verification_email.await_args.args[1]
    assert (await auth.verify_email_endpoint(
        auth.VerifyEmailRequest(email=email, code=new_code), db_session,
    )).access_token


async def test_activation_is_committed_before_welcome_email_and_token(
    db_session, monkeypatch, code_store,
):
    user, code = await pending_user(db_session)
    email, user_id = user.email, user.id
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def welcome_after_commit(*_args):
        async with sessions() as observer:
            assert (await observer.get(User, user_id)).status == "active"

    monkeypatch.setattr(email_service, "send_welcome_email", welcome_after_commit)
    result = await auth.verify_email_endpoint(
        auth.VerifyEmailRequest(email=email, code=code), db_session,
    )
    async with sessions() as observer:
        active = await get_current_user(
            Request({"type": "http", "method": "GET", "path": "/api/v1/auth/me"}),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=result.access_token),
            observer,
        )
        assert active.id == user_id


async def test_failed_activation_commit_keeps_verifier_retryable(
    db_session, monkeypatch, code_store,
):
    user, code = await pending_user(db_session)
    request = auth.VerifyEmailRequest(email=user.email, code=code)
    commit = db_session.commit
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        await auth.verify_email_endpoint(request, db_session)
    email_service.send_welcome_email.assert_not_awaited()
    await db_session.rollback()
    monkeypatch.setattr(db_session, "commit", commit)
    result = await auth.verify_email_endpoint(request, db_session)
    assert result.access_token


async def test_successful_verifier_cannot_be_replayed(db_session, code_store):
    user, code = await pending_user(db_session)
    request = auth.VerifyEmailRequest(email=user.email, code=code)
    assert (await auth.verify_email_endpoint(request, db_session)).access_token
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await auth.verify_email_endpoint(request, db_session)
    assert error.value.status_code == 400
    email_service.send_welcome_email.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["legacy", "expired", "missing_user", "wrong_user", "active", "disabled", "deleted"])
async def test_verifier_fails_closed_for_stale_or_ineligible_accounts(db_session, code_store, invalid):
    user, code = await pending_user(db_session)
    if code_store is not None:
        payload = json.loads(code_store.get(verification._key(user.email)))
    else:
        payload = dict(verification._fallback[user.email])
    assert user.password_hash not in json.dumps(payload)
    if invalid == "legacy":
        payload.pop("password_fingerprint")
    elif invalid == "expired":
        payload["expires_at"] = 0
    elif invalid == "missing_user":
        payload["user_id"] = generate_ulid()
    elif invalid == "wrong_user":
        other, _ = await pending_user(db_session)
        other.password_hash = user.password_hash
        payload["user_id"] = other.id
    elif invalid == "deleted":
        user.deleted_at = datetime.now(timezone.utc)
    else:
        user.status = invalid
    await db_session.commit()
    if code_store is not None:
        code_store.setex(verification._key(user.email), 600, json.dumps(payload))
    else:
        verification._fallback[user.email] = payload
    assert not await verification.verify_email(db_session, user.email, code)
    email_service.send_welcome_email.assert_not_awaited()


async def test_fallback_expires_consumed_verifiers_without_accumulating(monkeypatch):
    monkeypatch.setattr(verification, "_redis", lambda: None)
    monkeypatch.setattr(verification, "_fallback", {})
    monkeypatch.setattr(verification.time, "time", lambda: 1000)
    await verification.create_verification("old@example.test", "old", password_hash="old-hash")
    monkeypatch.setattr(verification.time, "time", lambda: 1000 + verification._CODE_TTL + 1)
    await verification.create_verification("new@example.test", "new", password_hash="new-hash")
    assert list(verification._fallback) == ["new@example.test"]


async def _wait_for_db_lock(observer, backend_pid, task):
    # Synchronize on actual PostgreSQL lock state, not a guessed short delay.
    async with asyncio.timeout(10):
        while not task.done():
            if await observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": backend_pid}):
                return
            await asyncio.sleep(0.01)
    raise AssertionError("Concurrent verification operation did not wait on the user lock")


@pytest.mark.parametrize("winner", ["password_update", "activation"])
async def test_activation_and_password_update_serialize(db_session, code_store, winner):
    user, code = await pending_user(db_session)
    user_id, email = user.id, user.email
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with sessions() as contender, sessions() as observer:
        stale_user = await contender.get(User, user_id)
        assert stale_user.status == "pending"
        backend_pid = await contender.scalar(text("SELECT pg_backend_pid()"))
        if winner == "password_update":
            user.password_hash = hash_password("ReplacementPassword456!")
            await db_session.flush()
            task = asyncio.create_task(verification.verify_email(contender, email, code))
        else:
            assert await verification.verify_email(db_session, email, code)
            task = asyncio.create_task(auth.register(
                auth.RegisterRequest(email=email, password="ReplacementPassword456!"),
                Request({"type": "http"}), contender,
            ))
        try:
            await _wait_for_db_lock(observer, backend_pid, task)
            await db_session.commit()
            if winner == "password_update":
                assert not await asyncio.wait_for(task, timeout=10)
            else:
                with pytest.raises(HTTPException) as error:
                    await asyncio.wait_for(task, timeout=10)
                assert error.value.status_code == 400
            await contender.rollback()
            observed = await observer.get(User, user_id)
            assert observed.status == ("pending" if winner == "password_update" else "active")
            password = "ReplacementPassword456!" if winner == "password_update" else "OriginalPassword123!"
            assert verify_password(password, observed.password_hash)
            auth.send_verification_email.assert_not_awaited()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await db_session.rollback()
