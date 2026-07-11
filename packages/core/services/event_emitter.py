"""Domain event emission helpers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def emit_in_session(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    *,
    source: str | None = None,
    payload: dict[str, Any] | None = None,
    notify: bool = True,
) -> int:
    """Persist an event and run same-transaction notification fan-out.

    Use this from worker code that already owns a DB transaction. Webhooks are
    intentionally not delivered here; call ``deliver_webhook_event`` after the
    surrounding transaction commits.
    """
    from packages.core.services.event_service import log_event

    event_payload = dict(payload or {})
    await log_event(db, entity_id, event_type, source=source, payload=event_payload)
    if not notify:
        return 0

    try:
        from packages.core.services.task_event_notifications import notify_task_event
        return await notify_task_event(db, entity_id, event_type, event_payload)
    except Exception as e:
        logger.debug("Task event notification failed: %s", e)
        return 0


async def deliver_webhook_event(
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Deliver an already-committed domain event to webhook subscribers."""
    try:
        from packages.core.services.webhook_service import deliver_event
        await deliver_event(entity_id, event_type, payload or {})
    except Exception as e:
        logger.debug("Webhook delivery failed: %s", e)


async def deliver_task_external_event(
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Deliver committed task events to configured external channels."""
    try:
        from packages.core.services.task_external_notifications import (
            deliver_task_external_notifications,
        )
        await deliver_task_external_notifications(entity_id, event_type, payload or {})
    except Exception as e:
        logger.debug("Task external notification delivery failed: %s", e)


def emit(
    entity_id: str,
    event_type: str,
    source: str | None = None,
    payload: dict[str, Any] | None = None,
):
    """Fire-and-forget event log.

    Creates an asyncio task to log the event without blocking the caller.
    Safe to call from sync or async code.
    """
    async def _log():
        try:
            from packages.core.database import async_session
            async with async_session() as db:
                await emit_in_session(
                    db,
                    entity_id,
                    event_type,
                    source=source,
                    payload=payload,
                )
                await db.commit()
        except Exception as e:
            logger.debug("Event emission failed: %s", e)

        # Deliver webhooks only after the event transaction has committed.
        await deliver_webhook_event(entity_id, event_type, payload)
        await deliver_task_external_event(entity_id, event_type, payload)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_log())
    except RuntimeError:
        pass  # No event loop -- skip (e.g. during testing)
