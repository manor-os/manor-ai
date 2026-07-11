"""Password reset — generate token, verify, reset."""
import secrets
import time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User
from packages.core.services.auth_service import hash_password

# Simple in-memory token store (use Redis in production)
_reset_tokens: dict[str, dict] = {}  # token -> {user_id, email, expires_at}
_TOKEN_TTL = 3600  # 1 hour


async def request_password_reset(db: AsyncSession, email: str) -> str | None:
    """Generate a password reset token for the given email.
    Returns the token if user exists, None otherwise.
    """
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        return None

    token = secrets.token_urlsafe(32)
    _reset_tokens[token] = {
        "user_id": user.id,
        "email": email,
        "expires_at": time.time() + _TOKEN_TTL,
    }
    return token


async def verify_reset_token(token: str) -> dict | None:
    """Verify a reset token. Returns {user_id, email} or None if invalid/expired."""
    data = _reset_tokens.get(token)
    if not data:
        return None
    if time.time() > data["expires_at"]:
        del _reset_tokens[token]
        return None
    return {"user_id": data["user_id"], "email": data["email"]}


async def reset_password(db: AsyncSession, token: str, new_password: str) -> bool:
    """Reset password using a valid token. Returns True on success."""
    data = await verify_reset_token(token)
    if not data:
        return False

    result = await db.execute(select(User).where(User.id == data["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        return False

    user.password_hash = hash_password(new_password)
    await db.flush()
    del _reset_tokens[token]
    return True
