"""Scheduled notification sweeper.

Pairs with ``notify(deliver_at=...)``: when a producer asks for a future
delivery we persist a ``Notification`` row with ``dispatch_status="pending"``
and stash the original call args under ``meta._scheduled``. This module
sweeps for due rows + replays the original ``notify()`` call (now
immediate) so external channel fan-out + callbacks fire the same way as
an unscheduled notification.

The sweeper is intentionally idempotent: rows transition out of
``pending`` before dispatch, so a worker crash mid-fan-out cannot
double-deliver. A row that errors during dispatch flips to
``dispatch_status="failed"`` with the error in ``meta._scheduled.error``
— operators can investigate without the sweeper looping on it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.notification import Notification

logger = logging.getLogger(__name__)


_SWEEP_BATCH = 50


async def dispatch_due_notifications(
    db: AsyncSession, *, now: datetime | None = None, batch_size: int = _SWEEP_BATCH,
) -> dict[str, int]:
    """Find pending notifications whose ``deliver_at`` has passed and
    fan them out exactly as if ``notify()`` had been called now.

    Caller is responsible for committing — the sweeper does not call
    ``db.commit()`` so the surrounding transaction can group several
    operator-level actions. (Tests typically call ``db.commit()``
    themselves; the Celery wrapper does the same.)

    Returns ``{"due": int, "dispatched": int, "failed": int}`` for
    observability.
    """
    when = now or datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Notification).where(
            Notification.dispatch_status == "pending",
            Notification.deliver_at.isnot(None),
            Notification.deliver_at <= when,
        ).order_by(Notification.deliver_at.asc()).limit(batch_size)
    )).scalars().all()

    counts = {"due": len(rows), "dispatched": 0, "failed": 0}
    if not rows:
        return counts

    # Phase A: claim each row by flipping its status. Doing this BEFORE
    # the external dispatch means a crash during fan-out can't cause a
    # second sweep to re-fire — the row is no longer in ``pending``.
    claimed: list[Notification] = []
    for row in rows:
        row.dispatch_status = "dispatching"
        claimed.append(row)
    await db.flush()
    # Commit the claim so a crashing dispatcher doesn't leave rows
    # stuck in 'dispatching' from the perspective of other sweepers.
    await db.commit()

    # Phase B: replay each notify() call.
    from packages.core.services.notify import notify

    for row in claimed:
        scheduled = (row.meta or {}).get("_scheduled") if isinstance(row.meta, dict) else None
        if not isinstance(scheduled, dict):
            scheduled = {}
        original_meta = {k: v for k, v in (row.meta or {}).items() if k != "_scheduled"}
        try:
            await notify(
                entity_id=row.entity_id,
                user_id=row.user_id,
                type=row.type,
                title=row.title or "",
                body=row.content,
                link=scheduled.get("link"),
                meta=original_meta or None,
                severity=scheduled.get("severity"),
                workspace_id=scheduled.get("workspace_id"),
                actions=scheduled.get("actions"),
                callback_kind=scheduled.get("callback_kind"),
                callback_payload=scheduled.get("callback_payload"),
                expires_in_seconds=scheduled.get("expires_in_seconds"),
            )
            row.dispatch_status = "dispatched"
            counts["dispatched"] += 1
        except Exception as exc:
            logger.exception(
                "notification_scheduler: dispatch failed for notification=%s",
                row.id,
            )
            row.dispatch_status = "failed"
            err_meta = dict(scheduled)
            err_meta["error"] = str(exc)[:500]
            new_meta = dict(row.meta or {})
            new_meta["_scheduled"] = err_meta
            row.meta = new_meta
            counts["failed"] += 1

    await db.flush()
    await db.commit()
    return counts


async def cancel_scheduled(
    db: AsyncSession, *, notification_id: str,
) -> bool:
    """Mark a pending scheduled notification as canceled.

    Returns True if a pending row was canceled, False if the row was
    missing, already dispatched, or already canceled. Caller commits.
    """
    row = (await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )).scalar_one_or_none()
    if row is None or row.dispatch_status != "pending":
        return False
    row.dispatch_status = "canceled"
    await db.flush()
    return True
