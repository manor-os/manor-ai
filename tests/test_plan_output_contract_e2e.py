"""End-to-end coverage for the Task -> Plan -> Step output contract chain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select

from packages.core.constants.execution import ExecutionPlanStatus
from packages.core.constants.supervisor import SupervisorVerdict
from packages.core.contracts.task_output import (
    TaskOutputValueKind,
    task_output_envelope_schema,
    task_output_payload_schema,
)
from packages.core.dispatcher import Dispatcher, MISSING_RESULT
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.executor import (
    PlanExecutor,
    _validated_terminal_task_output,
)
from packages.core.plans.schema import Plan
from packages.core.plans.service import create_plan_from_dag
from packages.core.workers.submit_result import (
    build_submit_result_tool,
    step_result_from_submit,
)


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


async def _create_runtime(
    db,
    expected_output: dict[str, Any],
    *,
    entity_id: str | None = None,
    worker: Worker | None = None,
):
    entity_id = entity_id or generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Produce the contracted deliverable",
        description="Return exactly the structured value requested by the task.",
        status="in_progress",
        expected_output=expected_output,
    )
    if worker is None:
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
        db.add(worker)
    db.add(task)
    await db.flush()

    # Deliberately give the Planner the wrong terminal shape. The Task is the
    # authority, so persistence must replace this before any worker sees it.
    planner_output = Plan.model_validate(
        {
            "steps": [
                {
                    "key": "deliver",
                    "kind": "subagent",
                    "service_key": "contract_test",
                    "output_shape": "TextResult",
                    "params": {"prompt": "Produce the final deliverable."},
                }
            ]
        }
    )
    plan = await create_plan_from_dag(
        db,
        entity_id=entity_id,
        workspace_id=None,
        task_id=task.id,
        agent_subscription_id=None,
        plan=planner_output,
        enforce_contract=True,
    )
    plan.status = "running"
    step = (await db.execute(select(ExecutionStep).where(ExecutionStep.plan_id == plan.id))).scalar_one()
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


def _install_completed_supervisor(monkeypatch, captured: list[dict[str, Any]]) -> None:
    import packages.core.plans.executor as executor_module

    async def _completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "verdict": "completed",
                    "evidence": "deliver passed its declared output contract",
                }
            )
        )

    monkeypatch.setattr(
        executor_module,
        "runtime_execute_plan_supervisor_completion",
        _completion,
    )


@pytest.mark.asyncio
async def test_worker_v2_http_completion_reaches_reviewer_and_task_output(
    client,
    monkeypatch,
) -> None:
    """Prove the public Worker boundary and formal Task result as one chain."""
    from packages.core.database import async_session

    registered_user = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "worker_v2_contract_e2e",
            "email": "worker-v2-contract-e2e@example.com",
            "password": "pass123",
        },
    )
    assert registered_user.status_code == 200, registered_user.text
    user_payload = registered_user.json()
    user_headers = {"Authorization": f"Bearer {user_payload['access_token']}"}

    registered_worker = await client.post(
        "/api/v1/workers/register",
        headers=user_headers,
        json={
            "kind": "custom_http",
            "display_name": "Worker v2 contract E2E",
            "capabilities": {
                "supported_kinds": ["subagent"],
                "max_concurrent_leases": 1,
                "max_risk_level": "high",
                "uses_manor_credentials": False,
                "deployment": "remote",
                "protocol_version": 2,
            },
        },
    )
    assert registered_worker.status_code == 201, registered_worker.text
    worker_payload = registered_worker.json()
    assert worker_payload["protocol_version"] == 2

    async with async_session() as db:
        worker = await db.get(Worker, worker_payload["worker_id"])
        assert worker is not None
        task, plan, step, worker = await _create_runtime(
            db,
            _lead_schema(),
            entity_id=user_payload["entity_id"],
            worker=worker,
        )
        lease = await _start_lease(db, plan, step, worker)
        task_id, plan_id, step_id, lease_id = task.id, plan.id, step.id, lease.id
        await db.commit()

    worker_headers = {
        "Authorization": f"Bearer {worker_payload['worker_secret']}",
        "Manor-Worker-Id": worker_payload["worker_id"],
        "Manor-Protocol-Version": "2",
    }
    wakeups: list[str] = []
    monkeypatch.setattr(
        "packages.core.plans.wakeup.wake_plan_cycle",
        wakeups.append,
    )
    completed = await client.post(
        f"/api/v1/workers/leases/{lease_id}/complete",
        headers=worker_headers,
        json={
            "result": _valid_leads(),
            "task_output_value_kind": TaskOutputValueKind.TASK_PAYLOAD.value,
        },
    )
    assert completed.status_code == 204, completed.text
    assert wakeups == [plan_id]

    captured: list[dict[str, Any]] = []
    _install_completed_supervisor(monkeypatch, captured)
    async with async_session() as db:
        task = await db.get(Task, task_id)
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, step_id)
        assert task is not None and plan is not None and step is not None
        assert step.result == {
            "status": "succeeded",
            "summary": "structured task output submitted",
            "outputs": {"data": _valid_leads()},
        }

        event = await PlanExecutor._finalize(
            db,
            plan,
            ExecutionPlanStatus.COMPLETED,
        )
        await db.commit()

        assert len(captured) == 1
        assert captured[0]["steps"][0]["contract_check"] == "passed"
        assert task.status == "completed"
        assert task.actual_output["result"] == _valid_leads()
        assert task.actual_output["result_step_key"] == "deliver"
        assert task.actual_output["supervisor_verdict"] == "completed"
        assert event["event_type"] == "task.succeeded"


@pytest.mark.asyncio
async def test_task_contract_reaches_worker_dispatcher_reviewer_and_actual_output(
    db_session,
    monkeypatch,
) -> None:
    task, plan, step, worker = await _create_runtime(db_session, _lead_schema())
    persisted_schema = plan.plan_dag["steps"][0]["expected_output_schema"]

    assert plan.plan_dag["steps"][0]["output_shape"] is None
    assert persisted_schema == step.expected_output_schema
    assert task_output_payload_schema(persisted_schema) == _lead_schema()

    submit_tool = build_submit_result_tool(step.expected_output_schema)
    assert submit_tool["function"]["parameters"]["properties"]["result"] == _lead_schema()
    submitted = step_result_from_submit(
        {"summary": "qualified two leads", "result": _valid_leads()},
        step.expected_output_schema,
    )
    lease = await _start_lease(db_session, plan, step, worker)
    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=submitted,
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
    )
    assert step.step_status == "done"
    assert step.result == {
        "status": "succeeded",
        "summary": "qualified two leads",
        "outputs": {"data": _valid_leads()},
    }

    captured: list[dict[str, Any]] = []
    _install_completed_supervisor(monkeypatch, captured)
    event = await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()

    assert len(captured) == 1
    assert captured[0]["task_output_contract"] == _lead_schema()
    assert captured[0]["steps"][0]["output_contract"] == _lead_schema()
    assert captured[0]["steps"][0]["contract_check"] == "passed"
    assert task.status == "completed"
    assert task.actual_output["result"] == _valid_leads()
    assert task.actual_output["result_step_key"] == "deliver"
    assert task.actual_output["result_contract_source"] == "task.expected_output"
    assert task.actual_output["supervisor_verdict"] == "completed"
    assert event["event_type"] == "task.succeeded"


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
    submitted = step_result_from_submit(
        {"summary": "claimed success", "result": invalid_payload},
        step.expected_output_schema,
    )

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=submitted,
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
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
    tool = build_submit_result_tool(step.expected_output_schema)
    assert tool["function"]["parameters"]["properties"]["result"] == array_schema
    lease = await _start_lease(db_session, plan, step, worker)

    await Dispatcher().complete_lease(
        db_session,
        lease.id,
        result=step_result_from_submit(
            {"summary": "two records", "result": records},
            step.expected_output_schema,
        ),
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
    )

    assert lease.status == "completed"
    assert step.step_status == "done"
    assert step.result["outputs"]["data"] == records


@pytest.mark.asyncio
async def test_omitted_result_fails_but_explicit_json_null_can_satisfy_null_schema(
    db_session,
    monkeypatch,
) -> None:
    task, plan, step, worker = await _create_runtime(db_session, {"type": "null"})
    tool = build_submit_result_tool(step.expected_output_schema)
    parameters = tool["function"]["parameters"]
    assert parameters["required"] == ["summary"]
    tool_validator = Draft202012Validator(parameters)
    with pytest.raises(ValidationError):
        tool_validator.validate({"summary": "missing contracted value"})
    tool_validator.validate({"summary": "explicit null", "result": None})

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
    submitted_null = step_result_from_submit(
        {"summary": "the contracted value is null", "result": None},
        step.expected_output_schema,
    )
    await Dispatcher().complete_lease(
        db_session,
        null_lease.id,
        result=submitted_null,
        task_output_value_kind=TaskOutputValueKind.STEP_RESULT_ENVELOPE,
    )

    assert null_lease.status == "completed"
    assert step.step_status == "done"
    assert "data" in step.result["outputs"]
    assert step.result["outputs"]["data"] is None

    captured: list[dict[str, Any]] = []
    _install_completed_supervisor(monkeypatch, captured)
    await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()
    assert task.status == "completed"
    assert "result" in task.actual_output
    assert task.actual_output["result"] is None


@pytest.mark.asyncio
async def test_reviewer_gate_rejects_plan_step_contract_drift_without_calling_model(
    db_session,
    monkeypatch,
) -> None:
    task, plan, step, _worker = await _create_runtime(db_session, _lead_schema())
    step.expected_output_schema = task_output_envelope_schema({"type": "string"})
    step.step_status = "done"
    step.result = {
        "status": "succeeded",
        "summary": "wrong contract",
        "outputs": {"data": "wrong shape"},
    }
    await db_session.flush()

    async def _must_not_call_model(**_kwargs):
        raise AssertionError("deterministic contract drift must gate before model review")

    import packages.core.plans.executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "runtime_execute_plan_supervisor_completion",
        _must_not_call_model,
    )
    decision = await PlanExecutor._supervise_outcome(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )

    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    assert "differs from the Plan contract" in decision.evidence

    async def _cannot_replan(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        PlanExecutor,
        "_maybe_replan",
        staticmethod(_cannot_replan),
    )
    await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()

    assert task.status == "failed"
    assert task.actual_output["supervisor_verdict"] == "needs_replan"
    assert "result" not in task.actual_output
    assert "data" not in task.actual_output["steps"][0]


@pytest.mark.asyncio
async def test_reviewer_gate_revalidates_a_done_result_before_acceptance(
    db_session,
    monkeypatch,
) -> None:
    task, plan, step, _worker = await _create_runtime(db_session, _lead_schema())
    step.step_status = "done"
    # Simulates a legacy/manual write that bypassed Dispatcher validation.
    step.result = {
        "status": "succeeded",
        "summary": "claims success",
        "outputs": {"data": {"leads": []}},
    }
    await db_session.flush()

    async def _must_not_call_model(**_kwargs):
        raise AssertionError("invalid persisted output must gate before model review")

    import packages.core.plans.executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "runtime_execute_plan_supervisor_completion",
        _must_not_call_model,
    )
    decision = await PlanExecutor._supervise_outcome(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )

    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    assert "does not satisfy its output contract" in decision.evidence

    async def _cannot_replan(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        PlanExecutor,
        "_maybe_replan",
        staticmethod(_cannot_replan),
    )
    event = await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()

    assert task.status == "failed"
    assert task.actual_output["supervisor_verdict"] == "needs_replan"
    assert "result" not in task.actual_output
    assert event["event_type"] == "task.failed"


@pytest.mark.asyncio
async def test_schema_valid_failed_envelope_is_not_a_formal_task_result(
    db_session,
) -> None:
    task, plan, step, _worker = await _create_runtime(db_session, _lead_schema())
    step.step_status = "failed"
    step.attempt_count = step.max_attempts
    step.result = {
        "status": "failed",
        "summary": "upstream source was unavailable",
        "outputs": {"data": _valid_leads()},
        "failure": {"reason": "upstream unavailable", "retryable": False},
    }
    step.error = {
        "type": "StepResultFailed",
        "message": "upstream source was unavailable",
    }
    await db_session.flush()

    event = await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.FAILED,
    )
    await db_session.flush()

    assert task.status == "failed"
    assert task.actual_output["supervisor_verdict"] == "failed"
    assert "result" not in task.actual_output
    assert "data" not in task.actual_output["steps"][0]
    assert event["event_type"] == "task.failed"


@pytest.mark.asyncio
async def test_schema_valid_output_rejected_for_missing_artifact_is_not_formal(
    db_session,
    monkeypatch,
) -> None:
    expected_output = {**_lead_schema(), "artifact_required": True}
    task, plan, step, _worker = await _create_runtime(db_session, expected_output)
    step.step_status = "done"
    step.result = {
        "status": "succeeded",
        "summary": "produced data but no durable artifact",
        "outputs": {"data": _valid_leads()},
    }
    await db_session.flush()

    async def _cannot_replan(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        PlanExecutor,
        "_maybe_replan",
        staticmethod(_cannot_replan),
    )
    event = await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()

    assert task.status == "failed"
    assert task.actual_output["supervisor_verdict"] == "needs_replan"
    assert "result" not in task.actual_output
    assert "data" not in task.actual_output["steps"][0]
    assert event["event_type"] == "task.failed"


def test_validated_task_output_tracks_the_terminal_producer() -> None:
    task = SimpleNamespace(expected_output=_lead_schema())
    upstream = SimpleNamespace(
        step_key="upstream",
        kind="subagent",
        depends_on=[],
        step_status="done",
        expected_output_schema=task_output_envelope_schema({"type": "string"}),
        result={
            "status": "succeeded",
            "summary": "intermediate value",
            "outputs": {"data": "not the Task deliverable"},
        },
    )
    terminal = SimpleNamespace(
        step_key="deliver",
        kind="subagent",
        depends_on=["upstream"],
        step_status="done",
        expected_output_schema=task_output_envelope_schema(_lead_schema()),
        result={
            "status": "succeeded",
            "summary": "final Task deliverable",
            "outputs": {"data": _valid_leads()},
        },
    )

    assert _validated_terminal_task_output(task, [upstream, terminal]) == (
        "deliver",
        _valid_leads(),
    )


@pytest.mark.asyncio
async def test_refinalize_requires_prior_supervisor_acceptance_for_formal_result(
    db_session,
) -> None:
    task, plan, step, _worker = await _create_runtime(db_session, _lead_schema())
    step.step_status = "done"
    step.result = {
        "status": "succeeded",
        "summary": "schema-valid worker candidate",
        "outputs": {"data": _valid_leads()},
    }
    task.status = "completed"
    task.actual_output = {
        "plan_id": plan.id,
        "plan_status": "completed",
        "steps": [],
        "files": None,
    }
    await db_session.flush()

    await PlanExecutor._finalize(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )
    await db_session.flush()

    assert task.status == "completed"
    assert "result" not in task.actual_output
    assert "data" not in task.actual_output["steps"][0]


@pytest.mark.asyncio
async def test_dynamic_provider_schema_is_additive_not_plan_drift(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Create provider resource",
        status="in_progress",
    )
    db_session.add(task)
    await db_session.flush()
    plan = await create_plan_from_dag(
        db_session,
        entity_id=entity_id,
        workspace_id=None,
        task_id=task.id,
        agent_subscription_id=None,
        plan=Plan.model_validate(
            {
                "steps": [
                    {
                        "key": "create_resource",
                        "kind": "action",
                        "service_key": "provider_test",
                        "provider": "test_provider",
                        "action_key": "create",
                        "params": {"name": "Resource"},
                    }
                ]
            }
        ),
    )
    step = (await db_session.execute(select(ExecutionStep).where(ExecutionStep.plan_id == plan.id))).scalar_one()
    provider_schema = {
        "type": "object",
        "required": ["resource_id"],
        "additionalProperties": False,
        "properties": {"resource_id": {"type": "string"}},
    }
    step.expected_output_schema = provider_schema
    step.step_status = "done"
    step.result = {"resource_id": "resource-1"}
    plan.status = "completed"
    await db_session.flush()

    captured: list[dict[str, Any]] = []
    _install_completed_supervisor(monkeypatch, captured)
    decision = await PlanExecutor._supervise_outcome(
        db_session,
        plan,
        ExecutionPlanStatus.COMPLETED,
    )

    assert decision.verdict is SupervisorVerdict.COMPLETED
    assert captured[0]["steps"][0]["output_contract"] == provider_schema
    assert captured[0]["steps"][0]["contract_check"] == "passed"
