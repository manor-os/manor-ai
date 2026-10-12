"""Authenticated current-email proof, separate from signup/MFA and login budgets."""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import _get_redis
from packages.core.models.user import User
from packages.core.services.email_verification_service import _generate_code, _password_fingerprint
from packages.core.services.user_email_proof import email_is_verified, mark_email_verified

TTL = 600
_BUDGET = """
local retry = 0
for i, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    local ttl = redis.call('TTL', key)
    if ttl < 0 then
        redis.call('EXPIRE', key, ARGV[1])
        ttl = tonumber(ARGV[1])
    end
    if count > tonumber(ARGV[i + 1]) then retry = math.max(retry, ttl) end
end
return retry
"""
_CONSUME = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class EmailProofError(Exception):
    def __init__(self, code: str, status: int = 400, retry_after: int = 0):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge_key(user_id: str) -> str:
    return f"manor:{{email-proof}}:challenge:{_digest(user_id)}"


async def _store_and_budget(email: str, ip: str, purpose: str):
    """Count both buckets before validation. Resend/success never resets attempts."""
    limits = (3, 20) if purpose == "send" else (5, 50)
    keys = [
        f"manor:{{email-proof}}:{purpose}:email:{_digest(email.lower())}",
        f"manor:{{email-proof}}:{purpose}:ip:{_digest(ip or 'unknown')}",
    ]
    try:
        store = await _get_redis()
        if store is None:
            raise RuntimeError()
        retry = int(await store.eval(_BUDGET, len(keys), *keys, TTL, *limits))
    except Exception:
        raise EmailProofError("email_verification_unavailable", 503) from None
    if retry > 0:
        raise EmailProofError("email_verification_rate_limited", 429, retry)
    return store


async def _current_user(db: AsyncSession, actor: User) -> User:
    # Capture admission before populate_existing refreshes a request's ORM object.
    email, version = actor.email, int(actor.token_version or 0)
    user = await db.scalar(
        select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.status != "active"
        or user.deleted_at is not None
        or user.email != email
        or int(user.token_version or 0) != version
    ):
        raise EmailProofError("email_verification_identity_changed", 401)
    return user


async def issue(db: AsyncSession, actor: User, ip: str) -> tuple[User, str | None]:
    store = await _store_and_budget(actor.email, ip, "send")
    user = await _current_user(db, actor)
    if email_is_verified(user):
        return user, None
    code = _generate_code()
    payload = json.dumps(
        {
            "email": user.email,
            "user_id": user.id,
            "token_version": int(user.token_version or 0),
            "password_fingerprint": _password_fingerprint(user.password_hash),
            "code_hash": _digest(code),
            "expires_at": time.time() + TTL,
        },
        sort_keys=True,
    )
    try:
        await store.setex(_challenge_key(user.id), TTL, payload)
    except Exception:
        raise EmailProofError("email_verification_unavailable", 503) from None
    return user, code


async def confirm(db: AsyncSession, actor: User, ip: str, code: str) -> User:
    store = await _store_and_budget(actor.email, ip, "confirm")
    user = await _current_user(db, actor)
    key = _challenge_key(user.id)
    try:
        raw = await store.get(key)
    except Exception:
        raise EmailProofError("email_verification_unavailable", 503) from None
    try:
        payload = json.loads(raw) if raw else {}
        valid = (
            not email_is_verified(user)
            and payload["email"] == user.email
            and payload["user_id"] == user.id
            and type(payload["token_version"]) is int
            and payload["token_version"] == int(user.token_version or 0)
            and payload["password_fingerprint"] == _password_fingerprint(user.password_hash)
            and type(payload["expires_at"]) in (int, float)
            and payload["expires_at"] > time.time()
            and isinstance(payload["code_hash"], str)
            and secrets.compare_digest(payload["code_hash"], _digest(code.strip().upper()))
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise EmailProofError("invalid_or_expired_verification_code")
    try:
        consumed = await store.eval(_CONSUME, 1, key, raw)
    except Exception:
        raise EmailProofError("email_verification_unavailable", 503) from None
    if consumed != 1 or payload["expires_at"] <= time.time():
        raise EmailProofError("invalid_or_expired_verification_code")
    # A failed DB commit requires a fresh code; consumed proof can never be replayed.
    mark_email_verified(user, user.email)
    await db.flush()
    return user
