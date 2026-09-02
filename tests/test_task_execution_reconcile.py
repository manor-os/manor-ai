from datetime import datetime, timedelta, timezone

import pytest

from packages.core.constants.task import TaskLogType
from packages.core.contracts.task_output import task_output_envelope_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task, TaskLog
from packages.core.services.task_execution_reconcile import (
    reconcile_task_from_latest_completed_plan,
)


def _lead_schema() -> dict:
    return {
        "type": "object",
        "required": ["leads"],
        "additionalProperties": False,
        "properties": {
            "leads": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string"}},
                },
            }
        },
    }


def _valid_leads() -> dict:
    return {"leads": [{"name": "North"}]}


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


@pytest.mark.asyncio
async def test_reconcile_does_not_revive_a_supervisor_rejected_plan(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    rejected_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [{"key": "deliver", "kind": "subagent", "status": "done"}],
        "files": None,
        "supervisor_verdict": "needs_replan",
        "supervisor_evidence": "the output contract was not satisfied",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Rejected completed plan",
        status="failed",
        actual_output=rejected_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "failed"
    assert task.actual_output == rejected_output


@pytest.mark.asyncio
async def test_reconcile_honors_rejected_supervisor_log_when_output_is_stale(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    stale_accepted_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [],
        "files": None,
        "supervisor_verdict": "completed",
        "supervisor_evidence": "stale acceptance",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Rejected plan with stale output",
        status="failed",
        actual_output=stale_accepted_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="Supervisor rejected the result.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "needs_replan",
                    "evidence": "the deliverable is incomplete",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "failed"
    assert task.actual_output == stale_accepted_output


@pytest.mark.asyncio
async def test_reconcile_restores_exact_validated_task_result(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Recover structured output",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "recovered one lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="Supervisor accepted the result.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "completed",
                    "evidence": "deliver satisfied the contract",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is True
    assert task.status == "completed"
    assert task.actual_output["result"] == _valid_leads()
    assert task.actual_output["result_step_key"] == "deliver"
    assert task.actual_output["result_contract_source"] == "task.expected_output"
    assert task.actual_output["supervisor_verdict"] == "completed"
    assert task.actual_output["supervisor_evidence"] == "deliver satisfied the contract"
    assert task.actual_output["reconciled_from_plan"] is True


@pytest.mark.asyncio
async def test_reconcile_requires_supervisor_acceptance_for_structured_result(
    db_session,
):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Do not bypass the supervisor",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "one schema-valid lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
async def test_reconcile_does_not_complete_schema_task_without_valid_terminal_result(
    db_session,
):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Do not recover an invalid result",
        status="in_progress",
        expected_output=_lead_schema(),
        actual_output=None,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "claims success but violates the Task contract",
                    "outputs": {"data": {"leads": []}},
                },
            ),
            TaskLog(
                id=generate_ulid(),
                task_id=task_id,
                log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value,
                content="A stale review claimed acceptance.",
                meta={
                    "plan_id": plan_id,
                    "verdict": "completed",
                    "evidence": "stale acceptance",
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is False
    assert task.status == "in_progress"
    assert task.actual_output is None


@pytest.mark.asyncio
async def test_reconcile_upgrades_legacy_completed_output_with_exact_result(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    legacy_output = {
        "plan_id": plan_id,
        "plan_status": "completed",
        "steps": [
            {
                "key": "deliver",
                "kind": "subagent",
                "status": "done",
                "result_summary": "one validated lead",
            }
        ],
        "files": None,
        "supervisor_verdict": "completed",
        "supervisor_evidence": "deliver satisfied the contract",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Upgrade legacy structured output",
        status="completed",
        expected_output=_lead_schema(),
        actual_output=legacy_output,
    )
    db_session.add_all(
        [
            task,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="completed",
                plan_dag={},
            ),
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="deliver",
                kind="subagent",
                depends_on=[],
                step_status="done",
                expected_output_schema=task_output_envelope_schema(_lead_schema()),
                result={
                    "status": "succeeded",
                    "summary": "one validated lead",
                    "outputs": {"data": _valid_leads()},
                },
            ),
        ]
    )
    await db_session.commit()

    changed = await reconcile_task_from_latest_completed_plan(db_session, task)

    assert changed is True
    assert task.status == "completed"
    assert task.actual_output["result"] == _valid_leads()
    assert task.actual_output["result_step_key"] == "deliver"
    assert task.actual_output["result_contract_source"] == "task.expected_output"
    assert task.actual_output["supervisor_verdict"] == "completed"
