"""One pass over every watcher. Run by the ``attention.sweep`` beat task."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from packages.core.attention.delivery import deliver_attention
from packages.core.attention.reconcile import reconcile_watcher
from packages.core.attention.registry import registered_watchers

logger = logging.getLogger(__name__)


async def run_attention_sweep(*, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Reconcile each watcher in its own transaction, then tell what is due.

    One watcher failing (a bad row, a query error) never stops the others;
    it is logged and retried on the next sweep.
    """
    from packages.core.database import async_session

    now = now or datetime.now(timezone.utc)
    results: dict[str, dict[str, int]] = {}
    for watcher in registered_watchers():
        try:
            async with async_session() as db:
                results[watcher.key] = await reconcile_watcher(db, watcher, now=now)
                await db.commit()
        except Exception:
            logger.exception("attention sweep: watcher %s failed", watcher.key)
    try:
        results["delivery"] = await deliver_attention(now=now)
    except Exception:
        logger.exception("attention sweep: delivery failed")
    return results
