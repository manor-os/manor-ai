"""Temporal worker process.

Run as a separate Python process (or container in production):

    python -m packages.core.temporal_app.worker

Subscribes to ``TEMPORAL_TASK_QUEUE``, registers the
``ExecutionPlanWorkflow`` and all activities, and processes events
until interrupted. One Temporal worker process per Manor cluster is
fine for low/medium volume; horizontally scale by running more.

Distinct from Manor's own ``Worker`` concept (the M3 lease executor).
This is the *Temporal* worker — it runs the workflow + activity code,
and is a Temporal SDK requirement when using Temporal at all.
"""
from __future__ import annotations

import asyncio
import logging
import signal as _signal

from packages.core.config import get_settings
from packages.core.temporal_app import activities as activities_module
from packages.core.temporal_app.client import get_temporal_client
from packages.core.temporal_app.workflows import ExecutionPlanWorkflow

logger = logging.getLogger(__name__)


# All @activity.defn-decorated functions in activities.py.
_ACTIVITIES = [
    activities_module.load_plan_dag,
    activities_module.announce_plan_started,
    activities_module.mark_step_pending_for_dispatcher,
    activities_module.mark_step_done_inline,
    activities_module.mark_step_waiting_human,
    activities_module.mark_step_skipped,
    activities_module.finalize_plan,
    activities_module.resolve_step_refs,
]


async def run_temporal_worker() -> None:
    """Connect to Temporal, register workflow + activities, run forever."""
    from temporalio.worker import Worker

    client = await get_temporal_client()
    if client is None:
        raise RuntimeError(
            "TEMPORAL_ENABLED is false or temporalio not installed — "
            "cannot run a Temporal worker"
        )

    settings = get_settings()
    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[ExecutionPlanWorkflow],
        activities=_ACTIVITIES,
    )

    logger.info(
        "Temporal worker starting on queue=%s namespace=%s",
        settings.TEMPORAL_TASK_QUEUE, settings.TEMPORAL_NAMESPACE,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    async with worker:
        await stop.wait()
    logger.info("Temporal worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_temporal_worker())


if __name__ == "__main__":
    main()
