"""Security boundaries for short-lived, one-time email verifiers."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.services import email_verification_service as verification
from packages.core.services.auth_service import hash_password, register_user


class CodeStore:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttls[key] = ttl

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    def ttl(self, key):
        return self.ttls.get(key, -2)


@pytest.fixture(params=["redis", "fallback"])
def verifier_store(request, monkeypatch):
    store = CodeStore() if request.param == "redis" else None
    monkeypatch.setattr(verification, "_redis", lambda: store)
    monkeypatch.setattr(verification, "_fallback", {})
    return store


def _payload(store, email):
    if store is None:
        return verification._fallback[email]
    return json.loads(store.get(verification._key(email)))


def _save(store, email, payload):
    if store is None:
        verification._fallback[email] = payload
    else:
        store.setex(verification._key(email), verification._CODE_TTL, json.dumps(payload))


def _mock_db(user):
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: user)
    return db


async def test_verifier_expires_on_every_storage_path(monkeypatch, verifier_store):
    clock = [1_000.0]
    monkeypatch.setattr(verification.time, "time", lambda: clock[0])
    email = "Expiry@Example.Test"
    code = await verification.create_verification(
        email, "synthetic-user", password_hash="synthetic-password-hash",
    )
    clock[0] += verification._CODE_TTL + 1
    assert not await verification.verify_email(_mock_db(None), email.lower(), code)
    if verifier_store is None:
        assert not verification._fallback
    else:
        assert verifier_store.get(verification._key(email)) is None


@pytest.mark.parametrize("bad_record", ["not-json", "[]", '{"code": 123}'])
async def test_malformed_verifier_records_fail_closed(monkeypatch, bad_record):
    store = CodeStore()
    monkeypatch.setattr(verification, "_redis", lambda: store)
    email = "malformed@example.test"
    store.setex(verification._key(email), 600, bad_record)
    assert not await verification.verify_email(_mock_db(None), email, "ABC23456")
    assert store.get(verification._key(email)) is None


async def test_verifier_is_bound_to_current_password(verifier_store):
    email = "password-fence@example.test"
    code = await verification.create_verification(
        email, "synthetic-user", password_hash="old-password-hash",
    )
    user = SimpleNamespace(
        id="synthetic-user", email=email, password_hash="new-password-hash",
        status="pending", deleted_at=None,
    )
    assert not await verification.verify_email(_mock_db(user), email, code)
    assert user.status == "pending"


async def test_committed_activation_is_one_time(verifier_store):
    email = "one-time@example.test"
    password_hash = "synthetic-password-hash"
    code = await verification.create_verification(
        email, "synthetic-user", password_hash=password_hash,
    )
    user = SimpleNamespace(
        id="synthetic-user", email=email, password_hash=password_hash,
        status="pending", deleted_at=None,
    )
    db = _mock_db(user)
    assert await verification.verify_email(db, email, code)
    assert user.status == "active"
    assert not await verification.verify_email(db, email, code)


async def test_attempt_budget_deletes_verifier(verifier_store):
    email = "budget@example.test"
    code = await verification.create_verification(
        email, "synthetic-user", password_hash="synthetic-password-hash",
    )
    assert code != "WRONG234"
    for _ in range(verification._MAX_ATTEMPTS):
        assert not await verification.verify_email(_mock_db(None), email, "WRONG234")
    assert not await verification.verify_email(_mock_db(None), email, "WRONG234")
    if verifier_store is None:
        assert email not in verification._fallback
    else:
        assert verifier_store.get(verification._key(email)) is None


async def test_fallback_cleanup_is_bounded(monkeypatch):
    monkeypatch.setattr(verification, "_redis", lambda: None)
    monkeypatch.setattr(verification, "_fallback", {})
    clock = [1_000.0]
    monkeypatch.setattr(verification.time, "time", lambda: clock[0])
    await verification.create_verification(
        "old@example.test", "old", password_hash="old-hash",
    )
    clock[0] += verification._CODE_TTL + 1
    await verification.create_verification(
        "new@example.test", "new", password_hash="new-hash",
    )
    assert list(verification._fallback) == ["new@example.test"]


async def test_concurrent_redemption_has_one_winner(db_session, monkeypatch):
    store = CodeStore()
    monkeypatch.setattr(verification, "_redis", lambda: store)
    user, _ = await register_user(
        db_session,
        email=f"concurrent-{generate_ulid().lower()}@example.test",
        password="CASA Concurrent Password 2026!",
    )
    user.status = "pending"
    await db_session.commit()
    code = await verification.create_verification(
        user.email, user.id, password_hash=user.password_hash,
    )
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def redeem():
        async with sessions() as db:
            result = await verification.verify_email(db, user.email, code)
            await db.commit()
            return result

    results = await asyncio.wait_for(asyncio.gather(redeem(), redeem()), timeout=10)
    assert sorted(results) == [False, True]
    async with sessions() as observer:
        assert (await observer.get(User, user.id)).status == "active"


async def test_password_update_invalidates_existing_verifier(
    db_session, monkeypatch,
):
    store = CodeStore()
    monkeypatch.setattr(verification, "_redis", lambda: store)
    user, _ = await register_user(
        db_session,
        email=f"fence-{generate_ulid().lower()}@example.test",
        password="CASA Original Password 2026!",
    )
    user.status = "pending"
    await db_session.commit()
    code = await verification.create_verification(
        user.email, user.id, password_hash=user.password_hash,
    )
    user.password_hash = hash_password("CASA Replacement Password 2026!")
    await db_session.commit()
    assert not await verification.verify_email(db_session, user.email, code)
    assert user.status == "pending"
