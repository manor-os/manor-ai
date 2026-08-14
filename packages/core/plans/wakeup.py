"""Post-commit wakeups for execution plans."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def wake_plan_cycle(plan_id: str | None) -> bool:
    """Enqueue one executor cycle after a worker lease transition."""
    if not plan_id:
        return False
    try:
        from packages.core.tasks.ai_tasks import run_plan

        run_plan.delay(plan_id)
        return True
    except Exception:
        # The lease state is already durable; the next worker tick or a manual
        # retry can recover if the broker is temporarily unavailable.
        logger.warning("failed to wake execution plan %s", plan_id, exc_info=True)
    return False
