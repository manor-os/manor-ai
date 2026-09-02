from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.routers.tasks import _active_execution_task_ids
from packages.core.constants.execution import ExecutionPlanStatus, ExecutionStepStatus
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep


@pytest.mark.asyncio
async def test_task_execution_projection_excludes_manual_in_progress_tasks(db_session):
    entity_id = generate_ulid()
    manual_task_id = generate_ulid()
    running_task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add_all([
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            task_id=running_task_id,
            plan_dag={},
            status=ExecutionPlanStatus.RUNNING.value,
        ),
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="execute",
            kind="llm",
            step_status=ExecutionStepStatus.RUNNING.value,
        ),
    ])
    await db_session.flush()

    active = await _active_execution_task_ids(
        db_session,
        [SimpleNamespace(id=manual_task_id), SimpleNamespace(id=running_task_id)],
    )

    assert active == {running_task_id}
