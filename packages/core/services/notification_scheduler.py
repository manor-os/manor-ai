"""Durable notification outbox dispatcher.

``Notification`` is the in-app source of truth. ``NotificationOutboxEvent``
owns external channel fan-out and is claimed with a PostgreSQL lease so several
dispatcher replicas can sweep concurrently. Delivery is at-least-once: a crash
after a provider accepts a message but before the outbox commit may retry it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.notification_types import (
    NotificationDispatchStatus,
    NotificationOutboxStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification, NotificationOutboxEvent

logger = logging.getLogger(__name__)


_SWEEP_BATCH = 50
_OUTBOX_LEASE = timedelta(minutes=5)
_MAX_ATTEMPTS = 5
_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)


def _eligible_outbox(when: datetime):
    return or_(
        and_(
            NotificationOutboxEvent.status == NotificationOutboxStatus.PENDING.value,
            NotificationOutboxEvent.available_at <= when,
        ),
        and_(
            NotificationOutboxEvent.status == NotificationOutboxStatus.PROCESSING.value,
            or_(
                NotificationOutboxEvent.locked_until.is_(None),
                NotificationOutboxEvent.locked_until <= when,
            ),
        ),
    )


def _retry_at(when: datetime, attempt_count: int) -> datetime:
    index = max(0, min(attempt_count - 1, len(_RETRY_DELAYS) - 1))
    return when + _RETRY_DELAYS[index]


async def _claim_outbox_events(
    db: AsyncSession,
    *,
    when: datetime,
    batch_size: int,
    notification_id: str | None = None,
) -> tuple[list[tuple[str, str]], int]:
    exhausted_query = (
        update(NotificationOutboxEvent)
        .where(
            _eligible_outbox(when),
            NotificationOutboxEvent.attempt_count >= _MAX_ATTEMPTS,
        )
        .values(
            status=NotificationOutboxStatus.FAILED.value,
            locked_until=None,
            claim_token=None,
            last_error="notification outbox retry limit exhausted after worker interruption",
        )
    )
    if notification_id:
        exhausted_query = exhausted_query.where(
            NotificationOutboxEvent.notification_id == notification_id,
        )
    exhausted = await db.execute(exhausted_query)
    query = (
        select(NotificationOutboxEvent)
        .where(
            _eligible_outbox(when),
            NotificationOutboxEvent.attempt_count < _MAX_ATTEMPTS,
        )
        .order_by(NotificationOutboxEvent.available_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    if notification_id:
        query = query.where(
            NotificationOutboxEvent.notification_id == notification_id,
        )
    rows = list((await db.execute(query)).scalars().all())
    claims: list[tuple[str, str]] = []
    for row in rows:
        claim_token = generate_ulid()
        row.status = NotificationOutboxStatus.PROCESSING.value
        row.attempt_count += 1
        row.locked_until = when + _OUTBOX_LEASE
        row.claim_token = claim_token
        row.last_error = None
        claims.append((row.id, claim_token))
    await db.commit()
    return claims, int(exhausted.rowcount or 0)


async def _cancel_inaccessible(
    db: AsyncSession,
    *,
    event_id: str,
    notification_id: str,
    claim_token: str,
) -> bool:
    notification = (
        await db.execute(
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if notification is None:
        await db.rollback()
        return False
    result = await db.execute(
        update(NotificationOutboxEvent)
        .where(
            NotificationOutboxEvent.id == event_id,
            NotificationOutboxEvent.status == NotificationOutboxStatus.PROCESSING.value,
            NotificationOutboxEvent.claim_token == claim_token,
        )
        .values(
            status=NotificationOutboxStatus.CANCELED.value,
            locked_until=None,
            claim_token=None,
        )
    )
    if result.rowcount:
        notification.dispatch_status = NotificationDispatchStatus.CANCELED.value
    await db.commit()
    return bool(result.rowcount)


async def _lock_authorized_workspace_for_delivery(
    db: AsyncSession,
    *,
    event_id: str,
    notification_id: str,
    claim_token: str,
    workspace_id: str | None,
    entity_id: str,
    user_id: str,
) -> bool:
    """Lock lifecycle, then revalidate recipient access for one external send."""
    from packages.core.services.notify import notification_recipient_is_authorized

    if not workspace_id:
        authorized = await notification_recipient_is_authorized(
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=None,
        )
        if authorized:
            return True
        await _cancel_inaccessible(
            db,
            event_id=event_id,
            notification_id=notification_id,
            claim_token=claim_token,
        )
        return False
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
        lock_workspace_recipient_authorization,
    )

    workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
    if workspace is None or workspace.deleted_at is not None or workspace.status != "active":
        await _cancel_inaccessible(
            db,
            event_id=event_id,
            notification_id=notification_id,
            claim_token=claim_token,
        )
        return False
    await lock_workspace_recipient_authorization(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        user_id=user_id,
    )
    if not await notification_recipient_is_authorized(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        db=db,
    ):
        await _cancel_inaccessible(
            db,
            event_id=event_id,
            notification_id=notification_id,
            claim_token=claim_token,
        )
        return False
    return True


async def _make_notification_visible(
    db: AsyncSession,
    *,
    event_id: str,
    claim_token: str,
    notification: Notification,
) -> bool:
    if notification.dispatch_status == NotificationDispatchStatus.DISPATCHED.value:
        owned = (
            await db.execute(
                select(NotificationOutboxEvent.id).where(
                    NotificationOutboxEvent.id == event_id,
                    NotificationOutboxEvent.status
                    == NotificationOutboxStatus.PROCESSING.value,
                    NotificationOutboxEvent.claim_token == claim_token,
                )
            )
        ).scalar_one_or_none()
        await db.commit()
        return owned is not None
    if notification.dispatch_status != NotificationDispatchStatus.PENDING.value:
        await db.rollback()
        return False

    owned_claim = (
        select(NotificationOutboxEvent.id)
        .where(
            NotificationOutboxEvent.id == event_id,
            NotificationOutboxEvent.status == NotificationOutboxStatus.PROCESSING.value,
            NotificationOutboxEvent.claim_token == claim_token,
        )
        .exists()
    )
    result = await db.execute(
        update(Notification)
        .where(
            Notification.id == notification.id,
            Notification.dispatch_status == NotificationDispatchStatus.PENDING.value,
            owned_claim,
        )
        .values(dispatch_status=NotificationDispatchStatus.DISPATCHED.value)
    )
    if not result.rowcount:
        await db.commit()
        return False
    from packages.core.services.realtime import queue_notification_after_commit

    meta = dict(notification.meta or {})
    queue_notification_after_commit(
        db,
        entity_id=notification.entity_id,
        user_id=notification.user_id,
        workspace_id=notification.workspace_id,
        notification={
            "id": notification.id,
            "type": notification.type,
            "title": notification.title,
            "content": notification.content,
            "link": meta.get("link"),
            "metadata": meta,
            "created_at": (
                notification.created_at.isoformat()
                if notification.created_at
                else None
            ),
        },
    )
    await db.commit()
    return True


async def _record_failure(
    db: AsyncSession,
    *,
    event_id: str,
    attempt_count: int,
    claim_token: str,
    when: datetime,
    exc: Exception,
) -> str:
    if attempt_count >= _MAX_ATTEMPTS:
        values = {
            "status": NotificationOutboxStatus.FAILED.value,
            "locked_until": None,
            "claim_token": None,
            "last_error": str(exc)[:2000],
        }
        outcome = "failed"
    else:
        values = {
            "status": NotificationOutboxStatus.PENDING.value,
            "available_at": _retry_at(when, attempt_count),
            "locked_until": None,
            "claim_token": None,
            "last_error": str(exc)[:2000],
        }
        outcome = "retried"
    result = await db.execute(
        update(NotificationOutboxEvent)
        .where(
            NotificationOutboxEvent.id == event_id,
            NotificationOutboxEvent.status == NotificationOutboxStatus.PROCESSING.value,
            NotificationOutboxEvent.claim_token == claim_token,
        )
        .values(**values)
    )
    await db.commit()
    return outcome if result.rowcount else "canceled"


async def _record_delivered_target(
    db: AsyncSession,
    *,
    event_id: str,
    claim_token: str,
    target_key: str,
) -> None:
    from packages.core.services.notify import _DELIVERED_TARGET_KEYS

    event = (
        await db.execute(
            select(NotificationOutboxEvent)
            .where(
                NotificationOutboxEvent.id == event_id,
                NotificationOutboxEvent.status
                == NotificationOutboxStatus.PROCESSING.value,
                NotificationOutboxEvent.claim_token == claim_token,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if event is None:
        await db.rollback()
        raise RuntimeError("notification outbox claim was lost during delivery")
    payload = dict(event.payload or {})
    delivered = list(payload.get(_DELIVERED_TARGET_KEYS) or [])
    if target_key not in delivered:
        delivered.append(target_key)
        payload[_DELIVERED_TARGET_KEYS] = delivered
        event.payload = payload
    await db.commit()


async def _process_claimed_event(
    db: AsyncSession,
    *,
    event_id: str,
    claim_token: str,
    when: datetime,
) -> str:
    row = (
        await db.execute(
            select(NotificationOutboxEvent, Notification)
            .join(Notification, Notification.id == NotificationOutboxEvent.notification_id)
            .where(
                NotificationOutboxEvent.id == event_id,
                NotificationOutboxEvent.status
                == NotificationOutboxStatus.PROCESSING.value,
                NotificationOutboxEvent.claim_token == claim_token,
            )
        )
    ).one_or_none()
    if row is None:
        return "canceled"
    event, notification = row
    # Provider callbacks commit after each delivered target and failure
    # handling rolls the session back. Snapshot every value needed after those
    # transaction boundaries so SQLAlchemy never attempts an implicit async
    # refresh from an expired ORM instance.
    attempt_count = event.attempt_count
    event_payload = dict(event.payload or {})
    notification_id = notification.id
    notification_entity_id = notification.entity_id
    notification_user_id = notification.user_id
    notification_workspace_id = notification.workspace_id
    notification_type = notification.type
    notification_title = notification.title or ""
    notification_body = notification.content
    notification_meta = dict(notification.meta or {})
    await db.commit()

    from packages.core.services.notify import (
        _DELIVERED_TARGET_KEYS,
        dispatch_persisted_notification,
    )

    try:
        authorized = await _lock_authorized_workspace_for_delivery(
            db,
            event_id=event_id,
            notification_id=notification_id,
            claim_token=claim_token,
            workspace_id=notification_workspace_id,
            entity_id=notification_entity_id,
            user_id=notification_user_id,
        )
    except Exception as exc:
        logger.warning(
            "notification recipient authorization failed notification=%s attempt=%s",
            notification_id,
            attempt_count,
            exc_info=True,
        )
        await db.rollback()
        return await _record_failure(
            db,
            event_id=event_id,
            attempt_count=attempt_count,
            claim_token=claim_token,
            when=when,
            exc=exc,
        )
    if not authorized:
        return "canceled"

    # In-app visibility is independent of external provider health. Once due
    # and authorized, the durable parent appears even if external fan-out must
    # retry later.
    visible = await _make_notification_visible(
        db,
        event_id=event_id,
        claim_token=claim_token,
        notification=notification,
    )
    if not visible:
        return "canceled"

    try:
        delivery_canceled = False

        async def authorize_external_target() -> bool:
            nonlocal delivery_canceled
            allowed = await _lock_authorized_workspace_for_delivery(
                db,
                event_id=event_id,
                notification_id=notification_id,
                claim_token=claim_token,
                workspace_id=notification_workspace_id,
                entity_id=notification_entity_id,
                user_id=notification_user_id,
            )
            if not allowed:
                delivery_canceled = True
            return allowed

        async def record_delivered_target(target_key: str) -> None:
            await _record_delivered_target(
                db,
                event_id=event_id,
                claim_token=claim_token,
                target_key=target_key,
            )

        await dispatch_persisted_notification(
            notification_id=notification_id,
            entity_id=notification_entity_id,
            user_id=notification_user_id,
            type=notification_type,
            title=notification_title,
            body=notification_body,
            meta=notification_meta,
            workspace_id=notification_workspace_id,
            payload=event_payload,
            delivered_target_keys=set(
                event_payload.get(_DELIVERED_TARGET_KEYS) or []
            ),
            before_external_target=authorize_external_target,
            on_target_delivered=record_delivered_target,
        )
        if delivery_canceled:
            return "canceled"
    except Exception as exc:
        logger.warning(
            "notification outbox dispatch failed notification=%s attempt=%s",
            notification_id,
            attempt_count,
            exc_info=True,
        )
        await db.rollback()
        return await _record_failure(
            db,
            event_id=event_id,
            attempt_count=attempt_count,
            claim_token=claim_token,
            when=when,
            exc=exc,
        )

    result = await db.execute(
        update(NotificationOutboxEvent)
        .where(
            NotificationOutboxEvent.id == event_id,
            NotificationOutboxEvent.status == NotificationOutboxStatus.PROCESSING.value,
            NotificationOutboxEvent.claim_token == claim_token,
        )
        .values(
            status=NotificationOutboxStatus.DELIVERED.value,
            delivered_at=when,
            locked_until=None,
            claim_token=None,
            last_error=None,
        )
    )
    await db.commit()
    return "dispatched" if result.rowcount else "canceled"


async def dispatch_notification_outbox(
    db: AsyncSession,
    *,
    notification_id: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """Claim and process one immediate notification outbox event."""
    when = now or datetime.now(timezone.utc)
    claimed, exhausted = await _claim_outbox_events(
        db,
        when=when,
        batch_size=1,
        notification_id=notification_id,
    )
    counts = {
        "due": len(claimed) + exhausted,
        "dispatched": 0,
        "retried": 0,
        "failed": exhausted,
        "canceled": 0,
    }
    for event_id, claim_token in claimed:
        outcome = await _process_claimed_event(
            db,
            event_id=event_id,
            claim_token=claim_token,
            when=when,
        )
        counts[outcome] += 1
    return counts


async def dispatch_due_notifications(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = _SWEEP_BATCH,
) -> dict[str, int]:
    """Claim due or stale outbox rows and fan them out with bounded retry."""
    when = now or datetime.now(timezone.utc)
    claimed, exhausted = await _claim_outbox_events(
        db,
        when=when,
        batch_size=batch_size,
    )
    counts = {
        "due": len(claimed) + exhausted,
        "dispatched": 0,
        "retried": 0,
        "failed": exhausted,
        "canceled": 0,
    }
    for event_id, claim_token in claimed:
        outcome = await _process_claimed_event(
            db,
            event_id=event_id,
            claim_token=claim_token,
            when=when,
        )
        counts[outcome] += 1
    return counts


async def cancel_scheduled(
    db: AsyncSession,
    *,
    notification_id: str,
) -> bool:
    """Cancel a future notification and its pending external outbox event."""
    notification = (
        await db.execute(
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        notification is None
        or notification.dispatch_status != NotificationDispatchStatus.PENDING.value
    ):
        return False
    notification.dispatch_status = NotificationDispatchStatus.CANCELED.value
    await db.execute(
        update(NotificationOutboxEvent)
        .where(
            NotificationOutboxEvent.notification_id == notification_id,
            NotificationOutboxEvent.status.in_((
                NotificationOutboxStatus.PENDING.value,
                NotificationOutboxStatus.PROCESSING.value,
            )),
        )
        .values(
            status=NotificationOutboxStatus.CANCELED.value,
            locked_until=None,
            claim_token=None,
        )
    )
    await db.flush()
    return True
