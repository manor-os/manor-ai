from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.sources import (
    RUNTIME_PLAN_AND_RUN_TASK_SOURCE,
    RUNTIME_PLAN_EXECUTOR_SOURCE,
)


async def runtime_record_task_agent_loop_evidence(
    db: Any,
    **kwargs: Any,
) -> Any:
    """Record a plan-free Task agent loop run's evidence.

    The same task_run evidence as a Plan, without a Plan or Steps: nothing
    is faked, so plan_id/plan_status are absent and steps are empty.
    """

    from packages.core.services.runtime_learning import record_task_execution_evidence

    return await record_task_execution_evidence(
        db,
        **kwargs,
        plan_id=None,
        plan_status=None,
        steps=None,
        source=RUNTIME_PLAN_AND_RUN_TASK_SOURCE,
    )


def runtime_emit_plan_executor_task_event(
    task_event: dict[str, Any] | None,
) -> None:
    """Emit a plan-executor task event with Runtime-owned source."""

    if not task_event:
        return

    from packages.core.services import event_emitter

    event_emitter.emit(
        task_event["entity_id"],
        task_event["event_type"],
        source=RUNTIME_PLAN_EXECUTOR_SOURCE,
        payload=task_event.get("payload") or {},
    )
