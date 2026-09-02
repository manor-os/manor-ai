"""Durable Twilio Voice call sessions and short-lived stream credentials."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.channel import TwilioVoiceCallSession


VOICE_SESSION_TTL = timedelta(minutes=15)
_TERMINAL_STATUSES = {"completed", "failed", "busy", "no_answer", "canceled", "expired"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_call_session_token(token: str) -> str:
    """Return the one-way digest stored for a Media Stream token."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


async def create_call_session(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_config_id: str,
    owner_user_id: str | None = None,
    workspace_id: str | None = None,
    direction: str,
    call_sid: str | None,
    from_number: str | None,
    to_number: str | None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    expires_at: datetime | None = None,
    metadata: dict | None = None,
) -> tuple[TwilioVoiceCallSession, str]:
    """Create a pending call and return the raw token exactly once."""
    token = _new_token()
    session = TwilioVoiceCallSession(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        channel_config_id=channel_config_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        direction=direction,
        status="pending",
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        session_token_hash=hash_call_session_token(token),
        expires_at=expires_at or (_now() + VOICE_SESSION_TTL),
        metadata_json=dict(metadata or {}),
    )
    db.add(session)
    await db.flush()
    return session, token


async def rotate_pending_call_session_token(
    db: AsyncSession,
    session: TwilioVoiceCallSession,
) -> str:
    """Rotate a not-yet-consumed token when Twilio retries a webhook."""
    if session.token_used_at is not None:
        raise ValueError("Voice session token has already been consumed")
    if session.status in _TERMINAL_STATUSES or session.expires_at <= _now():
        raise ValueError("Voice session is no longer usable")
    token = _new_token()
    session.session_token_hash = hash_call_session_token(token)
    await db.flush()
    return token


async def get_call_session_by_sid(
    db: AsyncSession,
    *,
    channel_config_id: str,
    call_sid: str,
) -> TwilioVoiceCallSession | None:
    if not call_sid:
        return None
    result = await db.execute(
        select(TwilioVoiceCallSession)
        .where(
            TwilioVoiceCallSession.channel_config_id == channel_config_id,
            TwilioVoiceCallSession.call_sid == call_sid,
        )
        .with_for_update()
        .limit(1)
    )
    return result.scalar_one_or_none()


async def consume_call_session_token(
    db: AsyncSession,
    raw_token: str,
) -> TwilioVoiceCallSession | None:
    """Atomically claim a stream token and move the call to connecting."""
    if not raw_token or len(raw_token) > 200:
        return None
    result = await db.execute(
        select(TwilioVoiceCallSession)
        .where(TwilioVoiceCallSession.session_token_hash == hash_call_session_token(raw_token))
        .with_for_update()
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    now = _now()
    if (
        session.token_used_at is not None
        or session.expires_at <= now
        or session.status in _TERMINAL_STATUSES
    ):
        if session.expires_at <= now and session.status not in _TERMINAL_STATUSES:
            session.status = "expired"
            await db.flush()
        return None
    session.token_used_at = now
    session.status = "connecting"
    await db.flush()
    return session


async def get_call_session_by_token(
    db: AsyncSession,
    raw_token: str,
) -> TwilioVoiceCallSession | None:
    """Read a session for the outbound TwiML callback without consuming it."""
    if not raw_token or len(raw_token) > 200:
        return None
    result = await db.execute(
        select(TwilioVoiceCallSession)
        .where(TwilioVoiceCallSession.session_token_hash == hash_call_session_token(raw_token))
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session is None or session.expires_at <= _now() or session.status in _TERMINAL_STATUSES:
        return None
    return session


async def mark_outbound_call_connecting(
    db: AsyncSession,
    raw_token: str,
) -> TwilioVoiceCallSession | None:
    """Lock and revalidate an outbound TwiML session before changing its state."""
    if not raw_token or len(raw_token) > 200:
        return None
    result = await db.execute(
        select(TwilioVoiceCallSession)
        .where(
            TwilioVoiceCallSession.session_token_hash
            == hash_call_session_token(raw_token)
        )
        .execution_options(populate_existing=True)
        .with_for_update()
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if (
        session is None
        or session.direction != "outbound"
        or session.expires_at <= _now()
        or session.status in _TERMINAL_STATUSES
    ):
        return None
    session.status = "connecting"
    await db.flush()
    return session


async def get_call_session_for_update(
    db: AsyncSession,
    session_id: str,
) -> TwilioVoiceCallSession | None:
    result = await db.execute(
        select(TwilioVoiceCallSession)
        .where(TwilioVoiceCallSession.id == session_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def mark_call_connected(
    db: AsyncSession,
    session: TwilioVoiceCallSession,
    *,
    stream_sid: str | None = None,
) -> None:
    session = await get_call_session_for_update(db, session.id)
    if session is None:
        raise ValueError("Voice session no longer exists")
    if session.status in _TERMINAL_STATUSES:
        raise ValueError("Voice session is already terminal")
    session.status = "in_progress"
    session.stream_sid = stream_sid or session.stream_sid
    session.connected_at = session.connected_at or _now()
    await db.flush()


async def finish_call_session(
    db: AsyncSession,
    session: TwilioVoiceCallSession,
    *,
    status: str,
    duration_seconds: int | None = None,
    error_message: str | None = None,
) -> None:
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal Voice session status: {status}")
    session = await get_call_session_for_update(db, session.id)
    if session is None:
        return
    # Provider callbacks can be retried or arrive out of order. Once a call
    # is terminal, never let a later callback rewrite a failure/cancellation
    # as a successful completion (or vice versa).
    if session.status in _TERMINAL_STATUSES and session.status != status:
        return
    session.status = status
    session.ended_at = session.ended_at or _now()
    if duration_seconds is not None:
        session.duration_seconds = max(0, int(duration_seconds))
    if error_message:
        session.error_message = str(error_message)[:500]
    await db.flush()


async def cancel_pending_call_sessions(
    db: AsyncSession,
    *,
    channel_config_ids: list[str],
    reason: str,
) -> int:
    """Invalidate not-yet-connected sessions before their channel disappears."""
    config_ids = [str(config_id) for config_id in channel_config_ids if config_id]
    if not config_ids:
        return 0
    result = await db.execute(
        update(TwilioVoiceCallSession)
        .where(
            TwilioVoiceCallSession.channel_config_id.in_(config_ids),
            TwilioVoiceCallSession.status.in_(("pending", "connecting")),
        )
        .values(
            status="canceled",
            ended_at=_now(),
            error_message=str(reason)[:500],
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


async def cancel_unconnected_call_sessions_for_binding(
    db: AsyncSession,
    *,
    channel_config_id: str,
    channel_binding_id: str,
    reason: str,
) -> int:
    """Cancel only unconnected calls frozen to one changed Voice binding."""

    if not channel_config_id or not channel_binding_id:
        return 0
    result = await db.execute(
        update(TwilioVoiceCallSession)
        .where(
            TwilioVoiceCallSession.channel_config_id == channel_config_id,
            TwilioVoiceCallSession.status.in_(("pending", "connecting")),
            TwilioVoiceCallSession.metadata_json["channel_binding_id"].astext
            == channel_binding_id,
        )
        .values(
            status="canceled",
            ended_at=_now(),
            error_message=str(reason)[:500],
        )
    )
    await db.flush()
    return int(result.rowcount or 0)
