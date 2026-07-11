"""TOTP two-factor authentication service.

Uses HMAC-based One-Time Password (HOTP/TOTP) per RFC 6238.
Implements without pyotp dependency — pure Python TOTP.
"""
import hashlib
import hmac
import secrets
import struct
import time
import base64
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User
from packages.core.services.auth_service import hash_password, verify_password


def generate_totp_secret() -> str:
    """Generate a random 20-byte base32-encoded secret."""
    return base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")


def get_totp_uri(secret: str, username: str, issuer: str = "Manor AI") -> str:
    """Generate otpauth:// URI for QR code scanning."""
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def generate_totp_code(secret: str, time_step: int = None) -> str:
    """Generate a 6-digit TOTP code for the current (or given) time step."""
    if time_step is None:
        time_step = int(time.time()) // 30

    # Decode base32 secret (pad if needed)
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)

    # HMAC-SHA1
    msg = struct.pack(">Q", time_step)
    h = hmac.new(key, msg, hashlib.sha1).digest()

    # Dynamic truncation
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)


def verify_totp_code(secret: str, code: str, window: int = 1) -> bool:
    """Verify a TOTP code with a time window (default +-1 step = +-30s)."""
    current_step = int(time.time()) // 30
    for offset in range(-window, window + 1):
        if generate_totp_code(secret, current_step + offset) == code:
            return True
    return False


def generate_backup_codes(count: int = 8) -> tuple[list[str], list[str]]:
    """Generate backup codes. Returns (plain_codes, hashed_codes)."""
    plain = [secrets.token_hex(4).upper() for _ in range(count)]
    hashed = [hash_password(c) for c in plain]
    return plain, hashed


# -- DB operations --

async def setup_2fa(db: AsyncSession, user_id: str) -> dict:
    """Begin 2FA setup: generate secret, return URI for QR code.
    Does NOT enable 2FA yet -- user must verify first.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"error": "User not found"}

    secret = generate_totp_secret()
    user.totp_secret = secret  # store (will enable after verification)
    await db.flush()

    username = user.display_name or user.email.split("@")[0]
    uri = get_totp_uri(secret, username)
    return {"secret": secret, "uri": uri, "username": username}


async def verify_and_enable_2fa(db: AsyncSession, user_id: str, code: str) -> dict:
    """Verify a TOTP code and enable 2FA. Returns backup codes on success."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.totp_secret:
        return {"error": "2FA setup not started"}

    if not verify_totp_code(user.totp_secret, code):
        return {"error": "Invalid code"}

    # Generate backup codes
    plain_codes, hashed_codes = generate_backup_codes()
    user.totp_enabled = True
    user.backup_codes = hashed_codes
    await db.flush()

    return {"enabled": True, "backup_codes": plain_codes}


async def disable_2fa(db: AsyncSession, user_id: str, code: str) -> bool:
    """Disable 2FA after verifying current code."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.totp_enabled:
        return False

    if not verify_totp_code(user.totp_secret, code):
        return False

    user.totp_enabled = False
    user.totp_secret = None
    user.backup_codes = None
    await db.flush()
    return True


async def verify_2fa_login(db: AsyncSession, user_id: str, code: str) -> bool:
    """Verify 2FA code during login. Also accepts backup codes."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.totp_enabled:
        return True  # 2FA not enabled = pass

    # Try TOTP code first
    if verify_totp_code(user.totp_secret, code):
        return True

    # Try backup codes
    if user.backup_codes:
        for i, hashed in enumerate(user.backup_codes):
            if verify_password(code, hashed):
                # Consume the backup code
                user.backup_codes = [c for j, c in enumerate(user.backup_codes) if j != i]
                await db.flush()
                return True

    return False
