from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.services.task_execution_reconcile import (
    reconcile_task_from_latest_completed_plan,
)


@pytest.mark.asyncio
async def test_reconcile_does_not_complete_task_from_older_plan_while_new_plan_runs(
    db_session,
):
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    completed_plan_id = generate_ulid()
    running_plan_id = generate_ulid()

    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Retry in progress",
        status="in_progress",
        actual_output=None,
    )
    db_session.add(task)
    db_session.add_all(
        [
            ExecutionPlan(
                id=completed_plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
                created_at=now,
                completed_at=now,
            ),
            ExecutionPlan(
                id=running_plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                plan_dag={},
                created_at=now + timedelta(seconds=1),
                started_at=now + timedelta(seconds=1),
            ),
        ]
    )
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=completed_plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish",
            kind="subagent",
            step_status="done",
            result={"summary": "Older run finished."},
        )
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None
