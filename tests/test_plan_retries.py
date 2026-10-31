"""Waiting worker-job Step decisions lock Task -> Plan -> Step, like every Task-bound mutation."""

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid


async def _auth(client: AsyncClient, username: str = "planretry") -> tuple[dict, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Plan Retry Corp",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, me.json()["entity_id"]


async def _create_plan(
    entity_id: str,
    *,
    status: str = "failed",
    with_task: bool = False,
) -> tuple[str, str, str, str | None]:
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task

    plan_id = generate_ulid()
    failed_step_id = generate_ulid()
    done_step_id = generate_ulid()
    task_id = generate_ulid() if with_task else None
    async with dbmod.async_session() as db:
        if task_id:
            db.add(
                Task(
                    id=task_id,
                    entity_id=entity_id,
                    title="Plan retry task",
                    status="failed",
                    priority=3,
                    task_type="general",
                    details={"existing": True, "manual_retry_count": 4},
                    actual_output={"stale": True},
                )
            )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status=status,
                execution_mode="live",
                approval_required=False,
                plan_dag={
                    "steps": [
                        {
                            "key": "failed",
                            "kind": "llm",
                            "service_key": "content",
                            "params": {"prompt": "Retry the failed step."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "key": "done",
                            "kind": "llm",
                            "service_key": "content",
                            "params": {"prompt": "Keep the completed result."},
                            "depends_on": [],
                            "expected_output_schema": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    ]
                },
                last_error={"type": "boom"},
            )
        )
        db.add(
            ExecutionStep(
                id=failed_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="failed",
                kind="llm",
                params={},
                depends_on=[],
                step_status="failed",
                attempt_count=2,
                max_attempts=3,
                error={"type": "ProviderError"},
                result={"stale": True},
            )
        )
        db.add(
            ExecutionStep(
                id=done_step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="done",
                kind="llm",
                params={},
                depends_on=[],
                step_status="done",
                attempt_count=1,
                max_attempts=3,
                result={"ok": True},
            )
        )
        await db.commit()
    return plan_id, failed_step_id, done_step_id, task_id


@pytest.mark.asyncio
async def test_waiting_step_lock_order_matches_task_bound_plan_mutations(
    client: AsyncClient,
):
    import packages.core.database as dbmod
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.task import Task
    from packages.core.services.step_resume import lock_waiting_step_for_decision

    _headers, entity_id = await _auth(client, "waiting_step_lock_order")
    plan_id, step_id, _done_step_id, task_id = await _create_plan(
        entity_id,
        status="paused",
        with_task=True,
    )
    assert task_id is not None

    async with dbmod.async_session() as db:
        task = await db.get(Task, task_id)
        step = await db.get(ExecutionStep, step_id)
        assert task is not None and step is not None
        task.status = "waiting_on_customer"
        step.step_status = "waiting_human"
        step.error = None
        await db.commit()

    async with dbmod.async_session() as db:
        lock_order: list[str] = []

        class RecordingSession:
            async def execute(self, statement, *args, **kwargs):
                sql = str(statement)
                if "FOR UPDATE" in sql:
                    if "FROM tasks" in sql:
                        lock_order.append("task")
                    elif "FROM execution_plans" in sql:
                        lock_order.append("plan")
                    elif "FROM execution_steps" in sql:
                        lock_order.append("step")
                return await db.execute(statement, *args, **kwargs)

        locked = await lock_waiting_step_for_decision(
            RecordingSession(),  # type: ignore[arg-type]
            entity_id=entity_id,
            task_id=task_id,
            plan_id=plan_id,
            step_id=step_id,
        )

        assert locked is not None and locked.id == step_id
        assert lock_order == ["task", "plan", "step"]
