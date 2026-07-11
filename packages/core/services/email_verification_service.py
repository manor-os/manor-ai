"""Email verification — generate code, verify, resend. Uses Redis so codes survive API restarts."""
import json
import logging
import secrets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User
from packages.core.config import get_settings

logger = logging.getLogger(__name__)

_CODE_TTL = 600  # 10 minutes
_MAX_ATTEMPTS = 5
_sync_redis = None


def _generate_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


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
    return f"manor:verify:{email}"


async def create_verification(email: str, user_id: str) -> str:
    """Create a verification code for an email. Stored in Redis."""
    code = _generate_code()
    r = _redis()
    data = json.dumps({"code": code, "user_id": user_id, "attempts": 0})
    if r:
        r.setex(_key(email), _CODE_TTL, data)
    else:
        _fallback[email] = {"code": code, "user_id": user_id, "attempts": 0}

    logger.warning("DEV — Verification code for %s: %s", email, code)
    return code


async def verify_email(db: AsyncSession, email: str, code: str) -> bool:
    """Verify the email code. Activates user on success."""
    r = _redis()
    if r:
        raw = r.get(_key(email))
        if not raw:
            return False
        data = json.loads(raw)
    else:
        data = _fallback.get(email)
        if not data:
            return False

    data["attempts"] = data.get("attempts", 0) + 1
    if data["attempts"] > _MAX_ATTEMPTS:
        if r:
            r.delete(_key(email))
        else:
            _fallback.pop(email, None)
        return False

    if data["code"] != code:
        # Save updated attempt count
        if r:
            ttl = r.ttl(_key(email))
            r.setex(_key(email), max(ttl, 60), json.dumps(data))
        return False

    # Code matches — activate user
    result = await db.execute(select(User).where(User.id == data["user_id"]))
    user = result.scalar_one_or_none()
    if user:
        user.status = "active"
        await db.flush()

    if r:
        r.delete(_key(email))
    else:
        _fallback.pop(email, None)
    return True


async def resend_verification(email: str, user_id: str) -> str | None:
    """Resend verification code."""
    return await create_verification(email, user_id)


# In-memory fallback if Redis is down
_fallback: dict[str, dict] = {}
