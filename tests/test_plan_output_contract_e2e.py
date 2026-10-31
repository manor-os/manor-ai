"""A Task output contract is enforced at the Dispatcher lease boundary.

External workers report a Step result through ``Dispatcher.complete_lease``.
The Step carries the Task's output contract; only a value that satisfies it
completes the Step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    task_output_envelope_schema,
)
from packages.core.dispatcher import Dispatcher, MISSING_RESULT
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker


def _lead_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["leads"],
        "additionalProperties": False,
        "properties": {
            "leads": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": ["name", "source_url"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "source_url": {"type": "string"},
                    },
                },
            }
        },
    }


def _valid_leads() -> dict[str, Any]:
    return {
        "leads": [
            {"name": "North", "source_url": "https://example.com/north"},
            {"name": "South", "source_url": "https://example.com/south"},
        ]
    }


async def _create_runtime(db, expected_output: dict[str, Any]):
    """One running Plan whose only Step carries the Task's output contract."""
    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Produce the contracted deliverable",
        description="Return exactly the structured value requested by the task.",
        status="in_progress",
        expected_output=expected_output,
    )
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Contract test worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        task_id=task.id,
        status="running",
        execution_mode="live",
        plan_dag={"steps": []},
    )
    contract = task_output_envelope_schema(expected_output)
    assert contract is not None
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=entity_id,
        step_key="deliver",
        kind="subagent",
        service_key="contract_test",
        params={"prompt": "Produce the final deliverable."},
        depends_on=[],
        step_status="pending",
        expected_output_schema=contract,
    )
    db.add_all([task, worker, plan, step])
    await db.flush()
    return task, plan, step, worker


async def _start_lease(db, plan, step, worker) -> WorkLease:
    lease = WorkLease(
        id=generate_ulid(),
        step_id=step.id,
        plan_id=plan.id,
        entity_id=plan.entity_id,
        workspace_id=plan.workspace_id,
        worker_id=worker.id,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        status="active",
    )
    db.add(lease)
    step.step_status = "running"
    step.attempt_count = (step.attempt_count or 0) + 1
    step.current_lease_id = lease.id
    await db.flush()
    return lease


@pytest.mark.asyncio
async def test_business_status_and_summary_survive_the_full_lease_boundary(
    db_session,
) -> None:
    expected = {
        "type": "object",
        "required": ["status", "summary"],
        "additionalProperties": False,
        "properties": {
            "status": {"const": "completed"},
            "summary": {"type": "string"},
        },
    }
    _task, plan, step, worker = await _create_runtime(db_session, expected)
    lease = await _start_lease(db_session, plan, step, worker)
    payload = {"status": "completed", "summary": "report ready"}

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=payload,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert lease.status == "completed"
    assert step.step_status == "done"
    assert step.result["outputs"]["data"] == payload


@pytest.mark.asyncio
async def test_canonical_shaped_business_value_survives_broad_task_schema(
    db_session,
) -> None:
    _task, plan, step, worker = await _create_runtime(
        db_session,
        {"type": "object"},
    )
    lease = await _start_lease(db_session, plan, step, worker)
    payload = {
        "status": "succeeded",
        "summary": "provider receipt",
        "outputs": {"data": {"provider_id": "job-42"}},
    }

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=payload,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert lease.status == "completed"
    assert step.step_status == "done"
    assert step.result["outputs"]["data"] == payload


@pytest.mark.asyncio
async def test_declared_malformed_envelope_is_rejected_under_broad_task_schema(
    db_session,
) -> None:
    _task, plan, step, worker = await _create_runtime(
        db_session,
        {"type": "object"},
    )
    lease = await _start_lease(db_session, plan, step, worker)

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result={
            "status": "failed",
            "summary": "blocked",
            "failure": {"reason": "permission denied"},
        },
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_FAILURE,
    )

    assert lease.status == "failed"
    assert step.step_status == "pending"
    assert step.error["type"] == "OutputSchemaError"
    assert any("retryable" in item["message"] for item in step.error["errors"])


@pytest.mark.asyncio
async def test_missing_v2_discriminator_fails_without_retrying_or_shape_guessing(
    db_session,
) -> None:
    _task, plan, step, worker = await _create_runtime(db_session, {"type": "object"})
    lease = await _start_lease(db_session, plan, step, worker)
    envelope_shaped_value = {
        "status": "succeeded",
        "summary": "looks canonical but is undeclared",
        "outputs": {"data": {"value": "ambiguous"}},
    }

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=envelope_shaped_value,
    )

    assert lease.status == "failed"
    assert step.step_status != "pending"
    assert step.error["type"] == "WorkerProtocolError"
    assert step.error["errors"][0]["path"] == "$.task_output_value_kind"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_payload, expected_error_fragment",
    [
        ({}, "required property"),
        ({"leads": [{"name": "Only", "source_url": "https://example.com"}]}, "too short"),
        (
            {
                "leads": [
                    {"name": "North", "source_url": 42},
                    {"name": "South", "source_url": "https://example.com/south"},
                ]
            },
            "not of type",
        ),
        ({**_valid_leads(), "unexpected": True}, "Additional properties"),
    ],
    ids=["missing-required", "wrong-count", "wrong-type", "extra-field"],
)
async def test_invalid_task_payload_never_completes_the_step(
    db_session,
    invalid_payload,
    expected_error_fragment,
) -> None:
    _task, plan, step, worker = await _create_runtime(db_session, _lead_schema())
    lease = await _start_lease(db_session, plan, step, worker)

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=invalid_payload,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert lease.status == "failed"
    assert step.step_status == "pending"
    assert step.result is None
    assert step.error["type"] == "OutputSchemaError"
    assert any(expected_error_fragment.lower() in item["message"].lower() for item in step.error["errors"])


@pytest.mark.asyncio
async def test_top_level_array_contract_survives_the_full_lease_boundary(db_session) -> None:
    array_schema = {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "items": _lead_schema()["properties"]["leads"]["items"],
    }
    records = _valid_leads()["leads"]
    _task, plan, step, worker = await _create_runtime(db_session, array_schema)
    lease = await _start_lease(db_session, plan, step, worker)

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=records,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert lease.status == "completed"
    assert step.step_status == "done"
    assert step.result["outputs"]["data"] == records


@pytest.mark.asyncio
async def test_omitted_result_fails_but_explicit_json_null_can_satisfy_null_schema(
    db_session,
) -> None:
    _task, plan, step, worker = await _create_runtime(db_session, {"type": "null"})

    omitted_lease = await _start_lease(db_session, plan, step, worker)
    await Dispatcher().complete_lease(
        db_session,
        omitted_lease.id,
        result=MISSING_RESULT,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )
    assert omitted_lease.status == "failed"
    assert step.step_status == "pending"
    assert step.error["errors"][0]["path"] == "$.result"

    null_lease = await _start_lease(db_session, plan, step, worker)
    await Dispatcher().complete_lease(
        db_session,
        null_lease.id,
        result=None,
        task_output_value_kind=TaskOutputValueKind.TASK_PAYLOAD,
    )

    assert null_lease.status == "completed"
    assert step.step_status == "done"
    assert "data" in step.result["outputs"]
    assert step.result["outputs"]["data"] is None
