"""Shared password-reset links, fenced by the committed password version."""
import asyncio
import hashlib
import json
import math
import secrets
import time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cache import _get_redis
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password, validate_password_strength

_TOKEN_TTL = 3600  # 1 hour
_STORE_TIMEOUT = 2.0


class PasswordResetUnavailableError(RuntimeError):
    """Shared storage is unavailable; never fall back to a process-local token."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_key(token: str) -> str:
    return f"manor:password-reset:{_digest(token)}"


async def request_password_reset(db: AsyncSession, email: str) -> str | None:
    """Generate a password reset token for the given email.
    Returns the token if user exists, None otherwise.
    """
    result = await db.execute(select(User).where(
        User.email == email.strip().lower(), User.deleted_at.is_(None),
    ))
    user = result.scalar_one_or_none()

    token = secrets.token_urlsafe(32)
    payload = {
        "user_id": user.id,
        "email": user.email,
        "token_version": int(user.token_version or 0),
        "password_fingerprint": _digest(user.password_hash),
        "expires_at": time.time() + _TOKEN_TTL,
    } if user else {}
    try:
        async with asyncio.timeout(_STORE_TIMEOUT):
            store = await _get_redis()
            if store is None:
                raise PasswordResetUnavailableError()
            # Unknown addresses perform the same storage write, so even a
            # write-only outage cannot expose account existence. Their empty
            # record cannot authorize a reset and expires without being mailed.
            await store.setex(_token_key(token), _TOKEN_TTL, json.dumps(payload))
    except Exception:
        raise PasswordResetUnavailableError() from None
    return token if user else None


async def verify_reset_token(token: str) -> dict | None:
    """Read a shared verifier; reset_password also checks the locked identity."""
    try:
        async with asyncio.timeout(_STORE_TIMEOUT):
            store = await _get_redis()
            if store is None:
                raise PasswordResetUnavailableError()
            raw = await store.get(_token_key(token))
    except Exception:
        raise PasswordResetUnavailableError() from None
    try:
        data = json.loads(raw) if raw else {}
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("user_id"), str)
            and isinstance(data.get("email"), str)
            and type(data.get("token_version")) is int
            and isinstance(data.get("password_fingerprint"), str)
            and data["password_fingerprint"].isascii()
            and len(data["password_fingerprint"]) == 64
            and type(data.get("expires_at")) in (int, float)
            and math.isfinite(data["expires_at"])
            and data["expires_at"] > time.time()
        )
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        return None
    return data


async def reset_password(db: AsyncSession, token: str, new_password: str) -> bool:
    """Reset password using a valid token. Returns True on success."""
    validate_password_strength(new_password)
    data = await verify_reset_token(token)
    if not data:
        return False

    result = await db.execute(
        select(User).where(User.id == data["user_id"])
        .with_for_update().execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if (
        user is None
        or user.deleted_at is not None
        or user.email != data["email"]
        or int(user.token_version or 0) != data["token_version"]
        or not secrets.compare_digest(_digest(user.password_hash), data["password_fingerprint"])
        or data["expires_at"] <= time.time()
    ):
        return False

    user.password_hash = hash_password(new_password)
    user.token_version = int(user.token_version or 0) + 1
    await db.flush()
    # Keep the Redis record until TTL expiry. The locked password/version change
    # consumes every link for the old identity only when the caller commits.
    # A DB rollback leaves the link retryable; concurrent callers reload the
    # committed version and cannot reuse it, even through another API process.
    return True
