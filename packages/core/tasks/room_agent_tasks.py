"""Bounded, recoverable delivery of group-chat Agent turns."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from packages.core.celery_app import celery_app
from packages.core.database import async_session
from packages.core.models.chat_room import ChatRoomAgentRun
from packages.core.queues import CeleryQueue
from packages.core.services.room_agent_runtime import execute_room_agent_run
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)
_REDRIVE_AFTER = timedelta(seconds=60)
_RUNNING_TIMEOUT = timedelta(minutes=12)  # Greater than the worker's 10-minute hard limit.
_SWEEP_BATCH = 100


def enqueue_room_agent_run(run_id: str) -> bool:
    """Best-effort broker handoff; the pending DB row remains the durable source."""
    try:
        execute_room_agent_task.apply_async(args=(run_id,), queue=CeleryQueue.ROOM_AGENTS.value)
        return True
    except Exception:
        logger.exception("Could not enqueue room Agent run %s; recovery will retry", run_id)
        return False


@celery_app.task(name="room_agent.execute", ignore_result=True)
def execute_room_agent_task(run_id: str) -> None:
    # The runtime claims the pending row under FOR UPDATE before any model or
    # tool work. Duplicate broker deliveries therefore cannot execute twice.
    run_in_worker(execute_room_agent_run(run_id))


async def recover_room_agent_runs(*, now: datetime | None = None) -> tuple[int, int]:
    """Redeliver lost pending work and terminate only definitely dead workers.

    Never replay a running run: it may already have performed a file mutation.
    """
    now = now or datetime.now(timezone.utc)
    pending_ids: list[str] = []
    interrupted = 0
    async with async_session() as db:
        pending = (await db.execute(
            select(ChatRoomAgentRun).where(
                ChatRoomAgentRun.status == "pending",
                ChatRoomAgentRun.created_at <= now - _REDRIVE_AFTER,
                or_(
                    ChatRoomAgentRun.last_enqueued_at.is_(None),
                    ChatRoomAgentRun.last_enqueued_at <= now - _REDRIVE_AFTER,
                ),
            ).order_by(ChatRoomAgentRun.created_at, ChatRoomAgentRun.id)
            .limit(_SWEEP_BATCH).with_for_update(skip_locked=True)
        )).scalars().all()
        for run in pending:
            run.last_enqueued_at = now
            pending_ids.append(run.id)
        # A hard-killed worker cannot finish its claimed run. Fail closed;
        # replay could duplicate a side effect already performed by a tool.
        stale = (await db.execute(
            select(ChatRoomAgentRun).where(
                ChatRoomAgentRun.status == "running",
                ChatRoomAgentRun.started_at <= now - _RUNNING_TIMEOUT,
            ).order_by(ChatRoomAgentRun.started_at, ChatRoomAgentRun.id)
            .limit(_SWEEP_BATCH).with_for_update(skip_locked=True)
        )).scalars().all()
        for run in stale:
            run.status = "interrupted"
            run.error_code = "worker_lost"
            run.completed_at = now
            interrupted += 1
        await db.commit()
    for run_id in pending_ids:
        enqueue_room_agent_run(run_id)
    return len(pending_ids), interrupted


@celery_app.task(name="room_agent.recover_pending", ignore_result=True)
def recover_pending_room_agent_tasks() -> None:
    run_in_worker(recover_room_agent_runs())
