"""Personal access tokens: user-created bearer keys for one resource (the MCP endpoint).

For callers that cannot run an OAuth sign-in: a hosted API that only accepts a
preset ``Authorization`` header, a connector configured with an API key, CI. A
token is ``manor_pat_<32 random bytes, base64url>``; only its SHA-256 is stored.

A token acts as its user in the team it was created in, never beyond the scope
it was given, and is admitted through the same checks as an OAuth access token
(account, membership, suspension). It stops working when revoked, when it
expires, or when the user signs out everywhere (``token_version`` changes).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.oauth_provider import PersonalAccessToken
from packages.core.models.user import User
from packages.core.services.oauth_provider_service import MCP_SCOPE, MCP_READ_SCOPE, MCP_TOKEN_AUDIENCE

TOKEN_SCOPES = (MCP_SCOPE, MCP_READ_SCOPE)
TOKEN_PREFIX = "manor_pat_"
# The characters after TOKEN_PREFIX kept to recognise a token in a list.
_DISPLAY_PREFIX_LENGTH = 8

# Lifetimes a user may choose; None never expires.
EXPIRY_DAYS_CHOICES = (30, 90, 365, None)
DEFAULT_EXPIRY_DAYS = 90

# Writing last_used_at on every call would cost a write per MCP request.
LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)

AUTHORIZED_PARTY_PREFIX = "pat:"


def is_personal_access_token(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def create_personal_access_token(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    scope: str,
    expires_in_days: Optional[int],
) -> tuple[PersonalAccessToken, str]:
    """Create a token for the user's current team; the plaintext is returned only here.

    The caller commits.
    """
    if scope not in TOKEN_SCOPES:
        raise ValueError("invalid_scope")
    if expires_in_days not in EXPIRY_DAYS_CHOICES:
        raise ValueError("invalid_expiry")
    name = (name or "").strip()[:100]
    if not name:
        raise ValueError("invalid_name")
    secret = secrets.token_urlsafe(32)
    token = TOKEN_PREFIX + secret
    now = datetime.now(timezone.utc)
    row = PersonalAccessToken(
        user_id=user.id,
        entity_id=user.entity_id,
        name=name,
        audience=MCP_TOKEN_AUDIENCE,
        scope=scope,
        token_hash=_hash(token),
        token_prefix=secret[:_DISPLAY_PREFIX_LENGTH],
        token_version=int(user.token_version or 0),
        expires_at=None if expires_in_days is None else now + timedelta(days=expires_in_days),
        created_at=now,
    )
    db.add(row)
    await db.flush()
    return row, token


def _live(user: User, now: datetime):
    return and_(
        PersonalAccessToken.revoked_at.is_(None),
        or_(PersonalAccessToken.expires_at.is_(None), PersonalAccessToken.expires_at > now),
        PersonalAccessToken.token_version == int(user.token_version or 0),
    )


async def token_claims(db: AsyncSession, token: str, *, audience: str) -> Optional[dict]:
    """Access-token-shaped claims for a live token of this audience, else None.

    The claims go through the same admission as a decoded access token
    (``authenticate_token_claims``), which re-checks the account and team.
    """
    row = (await db.execute(
        select(PersonalAccessToken, User)
        .join(User, User.id == PersonalAccessToken.user_id)
        .where(PersonalAccessToken.token_hash == _hash(token))
    )).first()
    if row is None:
        return None
    pat, user = row
    now = datetime.now(timezone.utc)
    expires = _aware(pat.expires_at)
    if (pat.audience != audience or pat.scope not in TOKEN_SCOPES or pat.revoked_at is not None
            or (expires is not None and expires <= now)
            or int(pat.token_version) != int(user.token_version or 0)):
        return None
    return {
        "sub": pat.user_id,
        "entity_id": pat.entity_id,
        "role": "",
        "token_version": int(pat.token_version),
        "scope": pat.scope,
        "scp": [pat.scope],
        "aud": pat.audience,
        "azp": AUTHORIZED_PARTY_PREFIX + pat.id,
        "amr": [],
    }


async def record_use(db: AsyncSession, token_id: str, *, now: Optional[datetime] = None) -> bool:
    """Stamp last_used_at unless it was stamped within the interval. The caller commits."""
    now = now or datetime.now(timezone.utc)
    stamped = await db.execute(
        update(PersonalAccessToken)
        .where(
            PersonalAccessToken.id == token_id,
            or_(PersonalAccessToken.last_used_at.is_(None),
                PersonalAccessToken.last_used_at <= now - LAST_USED_WRITE_INTERVAL),
        )
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(stamped.rowcount)


async def list_personal_access_tokens(db: AsyncSession, user: User) -> list[dict]:
    """The user's tokens that still work, newest first."""
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(
        select(PersonalAccessToken)
        .where(PersonalAccessToken.user_id == user.id, _live(user, now))
        .order_by(PersonalAccessToken.created_at.desc())
    )).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "entity_id": row.entity_id,
            "scope": row.scope,
            "token_prefix": TOKEN_PREFIX + row.token_prefix,
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]


async def revoke_personal_access_token(db: AsyncSession, user: User, token_id: str) -> bool:
    """Revoke one of the user's tokens. False when it was not live. The caller commits."""
    revoked = await db.execute(
        update(PersonalAccessToken)
        .where(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user.id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return bool(revoked.rowcount)
