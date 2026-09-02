"""Email verification — generate code, verify, resend. Uses Redis so codes survive API restarts."""
import hashlib
import json
import logging
import secrets
import time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User
from packages.core.config import get_settings

logger = logging.getLogger(__name__)

_CODE_TTL = 600  # 10 minutes
_MAX_ATTEMPTS = 5
_sync_redis = None


def _generate_code() -> str:
    # CASA test guidance expects a verifier containing both letters and
    # numbers. Exclude ambiguous glyphs while preserving at least one of each.
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    chars = [secrets.choice(letters), secrets.choice(digits)]
    chars.extend(secrets.choice(letters + digits) for _ in range(6))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _redis():
    """Get a sync Redis client for verification codes."""
    global _sync_redis
    if _sync_redis is None:
        try:
            import redis
            url = get_settings().REDIS_URL.replace("+asyncpg", "")
            _sync_redis = redis.from_url(url, decode_responses=True)
            _sync_redis.ping()
        except Exception as e:
            logger.warning("Redis not available for verification codes: %s", e)
            _sync_redis = False  # Mark as unavailable
    return _sync_redis if _sync_redis else None


def _key(email: str) -> str:
    return f"manor:verify:{email.strip().lower()}"


def _password_fingerprint(password_hash: str) -> str:
    """Fence a verifier to one stored password without copying the hash."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


def _delete_verifier(r, email: str) -> None:
    if r:
        r.delete(_key(email))
    else:
        _fallback.pop(email.strip().lower(), None)


async def create_verification(email: str, user_id: str, *, password_hash: str) -> str:
    """Create a short-lived verifier bound to the current password version."""
    code = _generate_code()
    r = _redis()
    now = time.time()
    data = {
        "code": code,
        "user_id": user_id,
        "attempts": 0,
        "password_fingerprint": _password_fingerprint(password_hash),
        "expires_at": now + _CODE_TTL,
    }
    if r:
        r.setex(_key(email), _CODE_TTL, json.dumps(data))
    else:
        for stored_email, previous in list(_fallback.items()):
            expires_at = previous.get("expires_at")
            if not isinstance(expires_at, (int, float)) or expires_at <= now:
                _fallback.pop(stored_email, None)
        _fallback[email.strip().lower()] = data

    logger.info("Email verification code issued", extra={"verification_user_id": user_id})
    return code


async def verify_email(db: AsyncSession, email: str, code: str) -> bool:
    """Activate only the pending account/password that issued this verifier."""
    r = _redis()
    if r:
        raw = r.get(_key(email))
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            _delete_verifier(r, email)
            return False
    else:
        data = _fallback.get(email.strip().lower())
        if not data:
            return False

    now = time.time()
    if not isinstance(data, dict):
        _delete_verifier(r, email)
        return False
    stored_code = data.get("code")
    user_id = data.get("user_id")
    fingerprint = data.get("password_fingerprint")
    expires_at = data.get("expires_at")
    attempts = data.get("attempts", 0)
    if (
        not isinstance(stored_code, str)
        or not isinstance(user_id, str)
        or not isinstance(fingerprint, str)
        or not isinstance(expires_at, (int, float))
        or not isinstance(attempts, int)
        or expires_at <= now
    ):
        _delete_verifier(r, email)
        return False

    data["attempts"] = attempts + 1
    if data["attempts"] > _MAX_ATTEMPTS:
        _delete_verifier(r, email)
        return False

    if not isinstance(code, str) or not secrets.compare_digest(stored_code, code):
        # Save updated attempt count
        if r:
            ttl = r.ttl(_key(email))
            if ttl > 0:
                r.setex(_key(email), ttl, json.dumps(data))
            else:
                r.delete(_key(email))
        return False

    # Serialize activation with pending-registration password updates and reload
    # identity-map snapshots that predate another request acquiring this lock.
    result = await db.execute(
        select(User).where(User.id == user_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if (
        user is None
        or user.email.strip().lower() != email.strip().lower()
        or user.deleted_at is not None
        or user.status != "pending"
        or expires_at <= time.time()
        or not secrets.compare_digest(
            fingerprint,
            _password_fingerprint(user.password_hash),
        )
    ):
        return False
    user.status = "active"
    await db.flush()

    # Keep the short-lived record until commit/expiry. The pending->active row
    # transition is the one-time fence, so a failed commit may safely retry but
    # a committed verifier can never activate the account again.
    return True


async def resend_verification(email: str, user_id: str, *, password_hash: str) -> str | None:
    """Resend verification code."""
    return await create_verification(email, user_id, password_hash=password_hash)


# In-memory fallback if Redis is down
_fallback: dict[str, dict] = {}
