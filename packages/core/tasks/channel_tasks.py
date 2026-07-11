"""Channel gateway Celery tasks.

Channel routers (Telegram / WeChat / WhatsApp / Slack / …) enqueue an
inbound message via ``dispatch_inbound_task.delay(…)`` so the webhook
can ack in <100 ms while the LLM run happens on a worker.

Durability beyond ``FastAPI.BackgroundTasks``:
  - broker is Redis-backed (CELERY_BROKER_URL)
  - ``task_acks_late=True`` + ``task_reject_on_worker_lost=True`` mean a
    crashed worker hands the task back to the queue
  - retries with exponential backoff (30 s / 60 s / 120 s) for transient
    errors like an LLM 429 or provider API hiccup
  - ``task_soft_time_limit=120 s`` / hard 180 s caps a stuck run so the
    queue drains even when one message pathologically loops
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from celery.exceptions import SoftTimeLimitExceeded

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)


@celery_app.task(
    name="channel.dispatch_inbound",
    bind=True,
    max_retries=3,
    soft_time_limit=120,
    time_limit=180,
)
def dispatch_inbound_task(
    self,
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    sender_id: str,
    sender_name: Optional[str] = None,
    chat_id: Optional[str] = None,
    content: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Celery wrapper around ``channel_gateway.dispatch_inbound``.

    The gateway function itself is queue-agnostic (no FastAPI / Celery
    types) — this wrapper just adapts it to Celery's sync worker model.
    """
    logger.info(
        "Channel dispatch: %s/%s sender=%s (attempt %d)",
        channel_type, channel_config_id, sender_id, self.request.retries + 1,
    )
    try:
        from packages.core.services.channel_gateway import dispatch_inbound
        result = _run_async(dispatch_inbound(
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            channel_type=channel_type,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            content=content,
            attachments=attachments,
        ))
    except SoftTimeLimitExceeded:
        # Don't retry — the agent run was already running too long.
        logger.error(
            "Channel dispatch exceeded soft time limit: %s/%s sender=%s",
            channel_type, channel_config_id, sender_id,
        )
        return {"status": "timeout"}
    except Exception as exc:
        # 30 s, 60 s, 120 s backoff
        countdown = 30 * (2 ** self.request.retries)
        logger.warning(
            "Channel dispatch %s/%s failed — retrying in %ds (attempt %d): %s",
            channel_type, channel_config_id, countdown,
            self.request.retries + 1, exc,
        )
        raise self.retry(exc=exc, countdown=countdown)

    logger.info(
        "Channel dispatch done: %s/%s status=%s",
        channel_type, channel_config_id, (result or {}).get("status"),
    )
    return result or {"status": "unknown"}


@celery_app.task(
    name="notification.dispatch_due",
    soft_time_limit=60,
    time_limit=120,
)
def dispatch_due_notifications_task() -> Dict[str, int]:
    """Periodic sweeper for scheduled notifications.

    Wakes up on the Celery beat cadence (configured in ``celery_app``)
    and runs ``notification_scheduler.dispatch_due_notifications``. Any
    notification whose ``deliver_at`` has passed and is still ``pending``
    gets dispatched as if ``notify()`` had been called at this moment.
    """
    from packages.core.database import async_session
    from packages.core.services.notification_scheduler import (
        dispatch_due_notifications,
    )

    async def _run() -> Dict[str, int]:
        async with async_session() as db:
            return await dispatch_due_notifications(db)

    return _run_async(_run())


@celery_app.task(
    name="integrations.health_check",
    bind=True, max_retries=1, soft_time_limit=30, time_limit=60,
)
def health_check_task(
    self,
    *,
    integration_id: str | None = None,
    oauth_account_id: str | None = None,
) -> Dict[str, Any]:
    """Background runner for a single provider health probe. Fired
    after a save (from the API) and from the daily sweep.
    """
    from packages.core.database import async_session
    from packages.core.services.integration_health import (
        run_and_persist_integration, run_and_persist_oauth,
    )

    async def _run() -> Dict[str, Any]:
        async with async_session() as db:
            if integration_id:
                result = await run_and_persist_integration(db, integration_id)
            elif oauth_account_id:
                result = await run_and_persist_oauth(db, oauth_account_id)
            else:
                return {"ok": False, "detail": "no id provided"}
            await db.commit()
            return result

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.warning("Health check task failed: %s", exc)
        return {"ok": False, "detail": str(exc)}


@celery_app.task(name="integrations.health_tick")
def health_tick() -> Dict[str, Any]:
    """Daily sweep — test every active Integration + every OAuthAccount.

    Small batches (~20 providers × N entities) so we just run serially;
    can shard later if needed.
    """
    from sqlalchemy import select
    from packages.core.database import async_session
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount
    from packages.core.services.integration_health import (
        run_and_persist_integration, run_and_persist_oauth,
    )

    async def _run() -> Dict[str, Any]:
        total = 0
        failed = 0
        async with async_session() as db:
            integrations = (await db.execute(
                select(Integration).where(Integration.status == "active")
            )).scalars().all()
            for row in integrations:
                total += 1
                result = await run_and_persist_integration(db, row.id)
                if not result.get("ok"):
                    failed += 1

            oauth_rows = (await db.execute(
                select(OAuthAccount)
            )).scalars().all()
            for row in oauth_rows:
                total += 1
                result = await run_and_persist_oauth(db, row.id)
                if not result.get("ok"):
                    failed += 1

            await db.commit()
        logger.info("Health tick: %d checked, %d failed", total, failed)
        return {"checked": total, "failed": failed}

    return _run_async(_run())
