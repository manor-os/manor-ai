from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.routers.tasks import _active_execution_task_ids
from packages.core.models.base import generate_ulid
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)


def _run(task_id: str, entity_id: str, phase: str) -> TaskCoordinatorRun:
    return TaskCoordinatorRun(
        id=generate_ulid(),
        task_id=task_id,
        entity_id=entity_id,
        workspace_id=generate_ulid(),
        root_subscription_id=generate_ulid(),
        root_agent_id=generate_ulid(),
        selection_fingerprint="execution-projection",
        lifecycle_generation="lifecycle-1",
        engine=TaskCoordinatorEngine.AGENT_LOOP.value,
        phase=phase,
        state_version=0,
    )


@pytest.mark.asyncio
async def test_task_execution_projection_excludes_manual_in_progress_tasks(db_session):
    entity_id = generate_ulid()
    manual_task_id = generate_ulid()
    running_task_id = generate_ulid()
    parked_task_id = generate_ulid()
    db_session.add_all([
        _run(running_task_id, entity_id, TaskCoordinatorPhase.RUNNING.value),
        _run(parked_task_id, entity_id, TaskCoordinatorPhase.WAITING_HUMAN.value),
    ])
    await db_session.flush()

    active = await _active_execution_task_ids(
        db_session,
        [
            SimpleNamespace(id=manual_task_id),
            SimpleNamespace(id=running_task_id),
            SimpleNamespace(id=parked_task_id),
        ],
    )

    assert active == {running_task_id}
