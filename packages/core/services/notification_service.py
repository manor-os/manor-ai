"""Notification service — CRUD and read/unread management."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select, func, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification

logger = logging.getLogger(__name__)


# The notifications endpoint is polled on a setInterval by every logged-in
# tab. If the local DB is behind on migrations (very common in dev when
# someone pulls and forgets ``alembic upgrade head``), a missing column
# like ``dispatch_status`` turns every poll into a 500 — flooding the
# console and masking other backend errors. We catch
# ``ProgrammingError`` (asyncpg's UndefinedColumn maps to this in
# SQLAlchemy) and degrade to "you have no notifications" with a server-
# side warning so the rest of the UI keeps working. Production DBs are
# expected to be on the latest schema; the catch is a dev safety net.
_SCHEMA_WARNING_LOGGED = False


def _warn_once(exc: Exception) -> None:
    """Log the schema-mismatch warning at most once per process so we
    don't spam logs on every poll."""
    global _SCHEMA_WARNING_LOGGED
    if _SCHEMA_WARNING_LOGGED:
        return
    _SCHEMA_WARNING_LOGGED = True
    logger.warning(
        "notifications query hit a schema mismatch (%s) — degrading to "
        "empty result. Run `alembic upgrade head` to apply pending "
        "migrations.",
        type(exc).__name__,
    )


async def list_notifications(
    db: AsyncSession, entity_id: str, user_id: str, *,
    entity_ids: Sequence[str] | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    # ``dispatch_status='pending'`` rows are scheduled for the future
    # and shouldn't appear on the bell until the sweeper flips them to
    # 'dispatched'. 'canceled' and 'failed' likewise stay hidden — they
    # never made it out.
    visible = Notification.dispatch_status.in_(("dispatched", "dispatching"))
    scoped_entity_ids = [eid for eid in (entity_ids or []) if eid]
    entity_filter = (
        Notification.entity_id.in_(scoped_entity_ids)
        if scoped_entity_ids
        else Notification.entity_id == entity_id
    )
    q = select(Notification).where(
        entity_filter,
        Notification.user_id == user_id,
        visible,
    )
    count_q = select(func.count()).select_from(Notification).where(
        entity_filter,
        Notification.user_id == user_id,
        visible,
    )

    if unread_only:
        q = q.where(Notification.read_at.is_(None))
        count_q = count_q.where(Notification.read_at.is_(None))

    q = q.order_by(Notification.created_at.desc()).limit(limit).offset(offset)

    try:
        result = await db.execute(q)
        count_result = await db.execute(count_q)
    except ProgrammingError as exc:
        # Likely a missing column from an unapplied migration. Roll back
        # the failed transaction so subsequent operations on the session
        # don't poison-pill, log once, and return an empty page.
        await db.rollback()
        _warn_once(exc)
        return [], 0
    return list(result.scalars().all()), count_result.scalar_one()


async def create_notification(
    db: AsyncSession, entity_id: str, user_id: str,
    type: str, title: str, *,
    body: str | None = None,
    link: str | None = None,
    meta: dict | None = None,
) -> Notification:
    """Create a notification row.

    ``meta`` holds arbitrary structured payload the UI can use to render
    richer formats than a single ``content`` string — e.g. the daily
    briefing sends stat blocks + action items so the frontend can draw
    a proper report card instead of dumping the raw text blob.
    ``link`` is merged into ``meta`` under the ``link`` key.
    """
    merged_meta: dict = dict(meta or {})
    if link and "link" not in merged_meta:
        merged_meta["link"] = link

    notif = Notification(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        type=type,
        title=title,
        content=body,
        meta=merged_meta,
    )
    db.add(notif)
    await db.flush()

    # Push to connected WebSocket client in real-time
    from packages.core.services.realtime import push_notification
    await push_notification(user_id, {
        "id": notif.id,
        "type": type,
        "title": title,
        "content": body,
        "link": link,
        "metadata": merged_meta,
        "created_at": notif.created_at.isoformat() if notif.created_at else None,
    })

    return notif


async def mark_read(db: AsyncSession, notification_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        return False
    notif.read_at = datetime.now(timezone.utc)
    await db.flush()
    return True


async def mark_all_read(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    *,
    entity_ids: Sequence[str] | None = None,
) -> int:
    scoped_entity_ids = [eid for eid in (entity_ids or []) if eid]
    entity_filter = (
        Notification.entity_id.in_(scoped_entity_ids)
        if scoped_entity_ids
        else Notification.entity_id == entity_id
    )
    result = await db.execute(
        update(Notification)
        .where(
            entity_filter,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.flush()
    return result.rowcount


async def delete_notification(db: AsyncSession, notification_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        return False
    await db.delete(notif)
    await db.flush()
    return True


async def count_unread(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    *,
    entity_ids: Sequence[str] | None = None,
) -> int:
    scoped_entity_ids = [eid for eid in (entity_ids or []) if eid]
    entity_filter = (
        Notification.entity_id.in_(scoped_entity_ids)
        if scoped_entity_ids
        else Notification.entity_id == entity_id
    )
    try:
        result = await db.execute(
            select(func.count()).select_from(Notification).where(
                entity_filter,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.dispatch_status.in_(("dispatched", "dispatching")),
            )
        )
    except ProgrammingError as exc:
        await db.rollback()
        _warn_once(exc)
        return 0
    return result.scalar_one()
