"""Stage a scheduled occurrence's terminal gateway delivery from its Task.

A scheduled agent Task can reach a terminal status outside the
``run_agent_task`` gateway (a user cancels a BLOCKED Task, the plan executor or
the Task coordinator finalizes it). Without a follow-up, the ScheduledJobRun
stays RUNNING forever and the scheduler sweep keeps probing it.

The transition itself cannot settle the run: callers write
``task.actual_output`` *after* ``apply_task_status_transition``, so the result
(``response``, error text) does not exist yet. Instead this stages, in the
caller's transaction, one runtime-outbox delivery of the exact gateway envelope
(``run_agent_task``). After commit the gateway sees a
terminal Task, builds the real ``_task_terminal_result`` and settles/projects
the occurrence through its normal path, which only touches a RUNNING run whose
admitted child is this Task.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from packages.core.constants.task import TaskStatus
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.services.task_runtime_approvals import TASK_RUNTIME_RESUME_EVENT

_TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.FAILED.value,
})


def scheduled_task_terminal_dedupe_key(run_id: str, task_id: str) -> str:
    return f"scheduled-task-terminal:{run_id}:{task_id}"


async def stage_scheduled_task_terminal_delivery(
    db, task: Any, new_status: Any,
) -> bool:
    """Insert one gateway-delivery outbox row; return True when one was staged."""

    new_status = getattr(new_status, "value", new_status)
    if str(new_status or "") not in _TERMINAL_STATUSES:
        return False
    details = task.details if isinstance(getattr(task, "details", None), dict) else {}
    run_id = str(details.get("scheduled_run_id") or "").strip()
    job_id = str(details.get("scheduled_job_id") or "").strip() or None
    if not run_id:
        return False

    from packages.core.models.base import generate_ulid

    statement = (
        pg_insert(RuntimeOutboxEvent)
        .values(
            id=generate_ulid(),
            event_type=TASK_RUNTIME_RESUME_EVENT,
            aggregate_id=task.id,
            dedupe_key=scheduled_task_terminal_dedupe_key(run_id, task.id),
            payload={
                "task_id": task.id,
                "agent_id": getattr(task, "agent_id", None),
                "scheduled_run_id": run_id,
                "scheduled_job_id": job_id,
                # A recovery-class delivery: if the Task's execution lease is
                # still held it is suppressed without spawning a recheck chain.
                "claim_recheck": True,
            },
        )
        .on_conflict_do_nothing(index_elements=[RuntimeOutboxEvent.dedupe_key])
        .returning(RuntimeOutboxEvent.id)
    )
    inserted = (await db.execute(statement)).scalar_one_or_none()
    return inserted is not None
