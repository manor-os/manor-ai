"""Notification service — CRUD and read/unread management."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import or_, select, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import NotificationDispatchStatus
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


def normalize_notification_workspace_scope(
    *,
    meta: dict | None,
    workspace_id: str | None,
) -> tuple[dict, str | None]:
    """Return one authoritative Workspace scope for storage and delivery."""
    normalized_meta = dict(meta or {})
    explicit = str(workspace_id or "").strip() or None
    metadata_scope = str(normalized_meta.get("workspace_id") or "").strip() or None
    if explicit and metadata_scope and explicit != metadata_scope:
        raise ValueError(
            "notification workspace scope conflicts with metadata.workspace_id"
        )
    resolved = explicit or metadata_scope
    if resolved:
        normalized_meta["workspace_id"] = resolved
    return normalized_meta, resolved


def _workspace_access_filter(
    *,
    restricted_entity_ids: Sequence[str] | None,
    readable_workspace_ids: Sequence[str] | None,
):
    """Hide workspace-scoped rows where the recipient lost read access."""
    restricted = [entity_id for entity_id in (restricted_entity_ids or []) if entity_id]
    if not restricted:
        return None
    readable = [workspace_id for workspace_id in (readable_workspace_ids or []) if workspace_id]
    workspace_id = func.coalesce(
        Notification.workspace_id,
        Notification.meta["workspace_id"].astext,
    )
    clauses = [
        workspace_id.is_(None),
        workspace_id == "",
        Notification.entity_id.notin_(restricted),
    ]
    if readable:
        clauses.append(workspace_id.in_(readable))
    return or_(*clauses)


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
    restricted_workspace_entity_ids: Sequence[str] | None = None,
    readable_workspace_ids: Sequence[str] | None = None,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    # ``dispatch_status='pending'`` rows are scheduled for the future
    # and shouldn't appear on the bell until the sweeper flips them to
    # 'dispatched'. 'canceled' and 'failed' likewise stay hidden — they
    # never made it out.
    visible = (
        Notification.dispatch_status == NotificationDispatchStatus.DISPATCHED.value
    )
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
    workspace_filter = _workspace_access_filter(
        restricted_entity_ids=restricted_workspace_entity_ids,
        readable_workspace_ids=readable_workspace_ids,
    )
    if workspace_filter is not None:
        q = q.where(workspace_filter)
        count_q = count_q.where(workspace_filter)

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
    workspace_id: str | None = None,
    idempotency_key: str | None = None,
    deliver_at: datetime | None = None,
    dispatch_status: str = NotificationDispatchStatus.DISPATCHED.value,
    push_realtime: bool = True,
) -> Notification:
    """Create a notification row.

    ``meta`` holds arbitrary structured payload the UI can use to render
    richer formats than a single ``content`` string — e.g. the daily
    briefing sends stat blocks + action items so the frontend can draw
    a proper report card instead of dumping the raw text blob.
    ``link`` is merged into ``meta`` under the ``link`` key.
    """
    notif, _created = await create_notification_once(
        db,
        entity_id,
        user_id,
        type,
        title,
        body=body,
        link=link,
        meta=meta,
        workspace_id=workspace_id,
        idempotency_key=idempotency_key,
        deliver_at=deliver_at,
        dispatch_status=dispatch_status,
        push_realtime=push_realtime,
    )
    return notif


async def create_notification_once(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    type: str,
    title: str,
    *,
    body: str | None = None,
    link: str | None = None,
    meta: dict | None = None,
    workspace_id: str | None = None,
    idempotency_key: str | None = None,
    deliver_at: datetime | None = None,
    dispatch_status: str = NotificationDispatchStatus.DISPATCHED.value,
    push_realtime: bool = True,
) -> tuple[Notification, bool]:
    """Create one logical notification and report whether this call won.

    A non-empty idempotency key is unique per entity and recipient. Concurrent
    producers therefore converge on one row without poisoning their owning
    transaction. Reusing a key for different content fails closed.
    """
    merged_meta, resolved_workspace_id = normalize_notification_workspace_scope(
        meta=meta,
        workspace_id=workspace_id,
    )
    if link and "link" not in merged_meta:
        merged_meta["link"] = link
    clean_key = str(idempotency_key or "").strip() or None
    if clean_key and len(clean_key) > 255:
        raise ValueError("notification idempotency_key must be at most 255 characters")

    notif_id = generate_ulid()
    values = {
        "id": notif_id,
        "entity_id": entity_id,
        "user_id": user_id,
        "workspace_id": resolved_workspace_id,
        "idempotency_key": clean_key,
        "type": type,
        "title": title,
        "content": body,
        "meta": merged_meta,
        "deliver_at": deliver_at,
        "dispatch_status": dispatch_status,
    }
    created = True
    if clean_key:
        statement = (
            pg_insert(Notification)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    Notification.entity_id,
                    Notification.user_id,
                    Notification.idempotency_key,
                ],
            )
            .returning(Notification.id)
        )
        inserted_id = (await db.execute(statement)).scalar_one_or_none()
        if inserted_id is None:
            created = False
            notif = (
                await db.execute(
                    select(Notification).where(
                        Notification.entity_id == entity_id,
                        Notification.user_id == user_id,
                        Notification.idempotency_key == clean_key,
                    )
                )
            ).scalar_one()
            same_payload = (
                notif.type == type
                and notif.title == title
                and notif.content == body
                and notif.workspace_id == resolved_workspace_id
                and notif.deliver_at == deliver_at
                and dict(notif.meta or {}) == merged_meta
            )
            if not same_payload:
                raise ValueError(
                    "notification idempotency_key already exists with different content"
                )
        else:
            notif = (
                await db.execute(select(Notification).where(Notification.id == inserted_id))
            ).scalar_one()
    else:
        notif = Notification(**values)
        db.add(notif)
        await db.flush()

    if (
        created
        and push_realtime
        and dispatch_status == NotificationDispatchStatus.DISPATCHED.value
    ):
        from packages.core.services.realtime import queue_notification_after_commit

        queue_notification_after_commit(
            db,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=resolved_workspace_id,
            notification={
                "id": notif.id,
                "type": type,
                "title": title,
                "content": body,
                "link": link,
                "metadata": merged_meta,
                "created_at": notif.created_at.isoformat() if notif.created_at else None,
            },
        )
    return notif, created


async def mark_read(
    db: AsyncSession,
    notification_id: str,
    user_id: str,
    *,
    entity_ids: Sequence[str],
    restricted_workspace_entity_ids: Sequence[str] | None = None,
    readable_workspace_ids: Sequence[str] | None = None,
) -> bool:
    scoped_entity_ids = [entity_id for entity_id in entity_ids if entity_id]
    if not scoped_entity_ids:
        return False
    query = select(Notification).where(
        Notification.id == notification_id,
        Notification.user_id == user_id,
        Notification.entity_id.in_(scoped_entity_ids),
    )
    workspace_filter = _workspace_access_filter(
        restricted_entity_ids=restricted_workspace_entity_ids,
        readable_workspace_ids=readable_workspace_ids,
    )
    if workspace_filter is not None:
        query = query.where(workspace_filter)
    result = await db.execute(query)
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
    restricted_workspace_entity_ids: Sequence[str] | None = None,
    readable_workspace_ids: Sequence[str] | None = None,
) -> int:
    scoped_entity_ids = [eid for eid in (entity_ids or []) if eid]
    entity_filter = (
        Notification.entity_id.in_(scoped_entity_ids)
        if scoped_entity_ids
        else Notification.entity_id == entity_id
    )
    query = update(Notification).where(
        entity_filter,
        Notification.user_id == user_id,
        Notification.read_at.is_(None),
    )
    workspace_filter = _workspace_access_filter(
        restricted_entity_ids=restricted_workspace_entity_ids,
        readable_workspace_ids=readable_workspace_ids,
    )
    if workspace_filter is not None:
        query = query.where(workspace_filter)
    result = await db.execute(query.values(read_at=datetime.now(timezone.utc)))
    await db.flush()
    return result.rowcount


async def delete_notification(
    db: AsyncSession,
    notification_id: str,
    user_id: str,
    *,
    entity_ids: Sequence[str],
    restricted_workspace_entity_ids: Sequence[str] | None = None,
    readable_workspace_ids: Sequence[str] | None = None,
) -> bool:
    scoped_entity_ids = [entity_id for entity_id in entity_ids if entity_id]
    if not scoped_entity_ids:
        return False
    query = select(Notification).where(
        Notification.id == notification_id,
        Notification.user_id == user_id,
        Notification.entity_id.in_(scoped_entity_ids),
    )
    workspace_filter = _workspace_access_filter(
        restricted_entity_ids=restricted_workspace_entity_ids,
        readable_workspace_ids=readable_workspace_ids,
    )
    if workspace_filter is not None:
        query = query.where(workspace_filter)
    result = await db.execute(query)
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
    restricted_workspace_entity_ids: Sequence[str] | None = None,
    readable_workspace_ids: Sequence[str] | None = None,
) -> int:
    scoped_entity_ids = [eid for eid in (entity_ids or []) if eid]
    entity_filter = (
        Notification.entity_id.in_(scoped_entity_ids)
        if scoped_entity_ids
        else Notification.entity_id == entity_id
    )
    try:
        query = select(func.count()).select_from(Notification).where(
            entity_filter,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.dispatch_status
            == NotificationDispatchStatus.DISPATCHED.value,
        )
        workspace_filter = _workspace_access_filter(
            restricted_entity_ids=restricted_workspace_entity_ids,
            readable_workspace_ids=readable_workspace_ids,
        )
        if workspace_filter is not None:
            query = query.where(workspace_filter)
        result = await db.execute(query)
    except ProgrammingError as exc:
        await db.rollback()
        _warn_once(exc)
        return 0
    return result.scalar_one()
