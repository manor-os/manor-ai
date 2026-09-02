"""Durable external EventLog delivery tasks."""
from __future__ import annotations

import logging

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)


@celery_app.task(
    name="events.dispatch_external_due",
    max_retries=0,
    soft_time_limit=45,
    time_limit=60,
)
def dispatch_external_events_task() -> dict[str, int | bool | str]:
    """Recover committed event deliveries independently of business tasks."""
    from packages.core.services.event_emitter import dispatch_due_external_events

    try:
        return {"ok": True, **run_in_worker(dispatch_due_external_events())}
    except Exception as exc:  # noqa: BLE001
        logger.exception("events.dispatch_external_due failed: %s", exc)
        return {"ok": False, "error": str(exc)}
