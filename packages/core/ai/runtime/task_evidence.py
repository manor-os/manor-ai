from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.sources import (
    RUNTIME_PLAN_EXECUTOR_SOURCE,
    RUNTIME_TASK_RUNNER_SOURCE,
)


async def runtime_record_task_runner_execution_evidence(
    db: Any,
    **kwargs: Any,
) -> Any:
    """Record task-runner execution evidence with Runtime-owned source."""

    from packages.core.services.runtime_learning import record_task_execution_evidence

    return await record_task_execution_evidence(
        db,
        **kwargs,
        source=RUNTIME_TASK_RUNNER_SOURCE,
    )


async def runtime_record_plan_executor_task_evidence(
    db: Any,
    **kwargs: Any,
) -> Any:
    """Record plan-executor task evidence with Runtime-owned source."""

    from packages.core.services.runtime_learning import record_task_execution_evidence

    return await record_task_execution_evidence(
        db,
        **kwargs,
        source=RUNTIME_PLAN_EXECUTOR_SOURCE,
    )


async def runtime_persist_task_runner_runtime_events(
    envelope: RuntimeEnvelope | None,
) -> int:
    """Persist task-runner RuntimeEnvelope events with Runtime-owned source."""

    from packages.core.services.runtime_event_service import persist_runtime_events_best_effort

    return await persist_runtime_events_best_effort(
        envelope,
        source=RUNTIME_TASK_RUNNER_SOURCE,
    )


def runtime_emit_task_runner_status_event(
    entity_id: str,
    event_type: str,
    *,
    payload: dict[str, Any],
) -> None:
    """Emit task-runner status event with Runtime-owned source."""

    from packages.core.services import event_emitter

    event_emitter.emit(
        entity_id,
        event_type,
        source=RUNTIME_TASK_RUNNER_SOURCE,
        payload=payload,
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
