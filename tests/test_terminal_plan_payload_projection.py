"""Accepted Plan contracts survive Task projection without field-name aliases."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.constants.supervisor import (
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.constants.task import TaskLogType
from packages.core.contracts.shapes import get_shape
from packages.core.contracts.task_output import (
    PLAN_OUTPUT_CONTRACT_SOURCE,
    PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE,
    TASK_OUTPUT_CONTRACT_SOURCE,
    plan_output_contract_schema,
    task_output_envelope_schema,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task, TaskLog
from packages.core.plans.executor import PlanExecutor, _accepted_terminal_output
from packages.core.services.task_execution_reconcile import (
    _actual_output_from_steps,
    reconcile_task_from_latest_completed_plan,
)


def _custom_schema():
    return {
        "type": "object",
        "required": ["files", "summary_zh"],
        "additionalProperties": False,
        "properties": {
            "files": {"type": "array"},
            "summary_zh": {"type": "string"},
        },
    }


def _runtime(schema=None, payload=None, *, source=PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE):
    schema = _custom_schema() if schema is None else schema
    entity_id, task_id, plan_id = (generate_ulid() for _ in range(3))
    task = Task(id=task_id, entity_id=entity_id, title="Deliver exact payload", status="in_progress")
    declaration = {
        "key": "deliver", "kind": "subagent", "service_key": "writer",
        "params": {"prompt": "Deliver the exact contracted result."},
        "expected_output_schema": schema,
    }
    if source == PLAN_OUTPUT_CONTRACT_SOURCE:
        declaration.pop("expected_output_schema")
        declaration["output_shape"] = "TextResult"
    plan = ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id, status="completed",
        plan_dag={"steps": [declaration]},
    )
    step = ExecutionStep(
        id=generate_ulid(), entity_id=entity_id, plan_id=plan_id,
        step_key="deliver", kind="subagent", depends_on=[], step_status="done",
        expected_output_schema=plan_output_contract_schema(schema, source=source),
        result=payload, params={},
    )
    return task, plan, step


@pytest.mark.parametrize("schema,payload", [
    (_custom_schema(), {"files": [], "summary_zh": "完整的交付摘要"}),
    ({"type": "array", "items": {"type": "integer"}}, [1, 2]),
    ({"type": "string"}, ""),
    ({"type": "boolean"}, False),
    ({"type": "integer"}, 0),
    ({"type": "null"}, None),
    ({"type": "object", "properties": {"status": {"const": "failed"}}}, {"status": "failed"}),
])
def test_exact_custom_payload_is_preserved_including_falsey_values(schema, payload):
    task, plan, step = _runtime(schema, payload)
    candidate = _accepted_terminal_output(task, plan, [step], supervisor_accepted=True)
    assert candidate == ("deliver", payload, PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE)
    output = _actual_output_from_steps(plan, [step], task=task, supervisor_review={"verdict": "completed"})
    assert "result" in output
    assert output["result"] == payload
    assert output["steps"][0]["data"] == payload
    assert output["result_contract_source"] == PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE


def test_canonical_plan_payload_keeps_its_own_source():
    schema = get_shape("TextResult").json_schema()
    payload = {"text": "Accepted canonical delivery"}
    task, plan, step = _runtime(schema, payload, source=PLAN_OUTPUT_CONTRACT_SOURCE)
    assert _accepted_terminal_output(task, plan, [step], supervisor_accepted=True) == (
        "deliver", payload, PLAN_OUTPUT_CONTRACT_SOURCE,
    )


def test_nested_worker_receipt_is_not_another_terminal_delivery():
    from packages.core.constants.execution import (
        COORDINATOR_CHILD_PARAM_KEY,
        COORDINATOR_PARENT_STEP_PARAM_KEY,
    )

    payload = {"files": [], "summary_zh": "Only the logical terminal is delivered"}
    task, plan, step = _runtime(payload=payload)
    _, _, child = _runtime(payload={"not": "a valid terminal result"})
    child.step_key = "nested_child"
    child.params = {
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_PARENT_STEP_PARAM_KEY: "deliver",
    }
    assert _accepted_terminal_output(task, plan, [step, child], supervisor_accepted=True) == (
        "deliver", payload, PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE,
    )


@pytest.mark.parametrize("case", [
    "unaccepted", "failed", "pending", "invalid", "drift", "multiple",
    "duplicate_key", "unmarked", "no_schema", "legacy_envelope", "plan_failed", "plan_running",
    "invalid_task_contract", "task_contract_mismatch",
])
def test_plan_fallback_does_not_promote_unproven_results(case):
    task, plan, step = _runtime(payload={"files": [], "summary_zh": "candidate"})
    steps = [step]
    if case in {"failed", "pending"}:
        step.step_status = case
    elif case in {"plan_failed", "plan_running"}:
        plan.status = case.removeprefix("plan_")
    elif case == "invalid":
        step.result = {"files": []}
    elif case == "drift":
        step.expected_output_schema = plan_output_contract_schema({"type": "object"}, source=PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE)
    elif case in {"multiple", "duplicate_key"}:
        other = SimpleNamespace(**{key: getattr(step, key) for key in (
            "step_key", "kind", "depends_on", "params", "expected_output_schema", "step_status", "result",
        )})
        other.step_key = "other" if case == "multiple" else "deliver"
        steps.append(other)
    elif case == "unmarked":
        step.expected_output_schema = _custom_schema()
    elif case == "no_schema":
        step.expected_output_schema = None
    elif case == "legacy_envelope":
        from packages.core.contracts.envelope import step_result_envelope_schema
        step.expected_output_schema = step_result_envelope_schema()
        step.result = {"status": "succeeded", "outputs": {"text": "legacy"}}
    elif case == "invalid_task_contract":
        task.expected_output = {"type": "not-a-json-type"}
    elif case == "task_contract_mismatch":
        task.expected_output = {"type": "string"}
    assert _accepted_terminal_output(task, plan, steps, supervisor_accepted=case != "unaccepted") is None


def test_task_authored_envelope_remains_authoritative():
    task, plan, step = _runtime(payload=None)
    task.expected_output = {"type": "string"}
    step.expected_output_schema = task_output_envelope_schema(task.expected_output)
    step.result = {"status": "succeeded", "summary": "Done", "outputs": {"data": "Task payload"}}
    assert _accepted_terminal_output(task, plan, [step], supervisor_accepted=True) == (
        "deliver", "Task payload", TASK_OUTPUT_CONTRACT_SOURCE,
    )
    step.result = {"status": "failed", "summary": "No result", "failure": {"reason": "blocked", "retryable": False}}
    assert _accepted_terminal_output(task, plan, [step], supervisor_accepted=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("schema,payload", [
    (_custom_schema(), {"files": [], "summary_zh": "自定义字段必须完整保留"}),
    ({"type": "array", "items": {"type": "integer"}}, []),
    ({"type": "boolean"}, False),
    ({"type": "integer"}, 0),
    ({"type": "null"}, None),
])
async def test_final_writer_and_completed_read_repair_keep_exact_custom_payload(db_session, schema, payload):
    task, plan, step = _runtime(schema, payload)
    plan.status = "running"
    db_session.add_all([task, plan, step])
    await db_session.flush()
    await PlanExecutor._finalize(
        db_session, plan, "completed",
        server_decision=SupervisorDecision(
            verdict=SupervisorVerdict.COMPLETED,
            evidence="The delivered payload satisfies the request.",
            source=SupervisorDecisionSource.GATE,
        ),
    )
    await db_session.flush()
    assert task.status == "completed"
    assert task.actual_output["result"] == payload
    assert task.actual_output["steps"][0]["data"] == payload
    assert task.actual_output["result_contract_source"] == PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE
    original = deepcopy(step.result)
    # Simulate the pre-fix projection, including the completed fast path.
    task.actual_output = {
        "plan_id": plan.id, "plan_status": "completed", "files": None,
        "steps": [{"key": "deliver", "status": "done", "result_summary": ""}],
        "supervisor_verdict": "completed",
    }
    await db_session.flush()
    assert await reconcile_task_from_latest_completed_plan(db_session, task) is True
    assert task.actual_output["result"] == payload
    assert task.actual_output["steps"][0]["data"] == payload
    assert task.actual_output["result_contract_source"] == PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE
    assert step.result == original
    assert await reconcile_task_from_latest_completed_plan(db_session, task) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unaccepted", "rejected", "wrong_plan", "failed", "invalid", "multiple", "drift", "invalid_task_contract"])
async def test_read_repair_cannot_complete_unaccepted_or_invalid_plan_delivery(db_session, case):
    task, plan, step = _runtime(payload={"files": [], "summary_zh": "candidate"})
    if case == "failed":
        step.step_status = "failed"
    elif case == "invalid":
        step.result = {"files": []}
    elif case == "drift":
        step.expected_output_schema = plan_output_contract_schema({"type": "object"}, source=PLAN_SCHEMA_OUTPUT_CONTRACT_SOURCE)
    elif case == "invalid_task_contract":
        task.expected_output = {"type": "not-a-json-type"}
    if case == "multiple":
        _, _, other = _runtime(payload={"files": [], "summary_zh": "second"})
        other.plan_id, other.entity_id, other.step_key = plan.id, plan.entity_id, "second"
        db_session.add(other)
    db_session.add_all([task, plan, step])
    if case != "unaccepted":
        db_session.add(TaskLog(
            id=generate_ulid(), task_id=task.id,
            log_type=TaskLogType.AI_SUPERVISOR_VERDICT.value, content="Persisted review",
            meta={"plan_id": generate_ulid() if case == "wrong_plan" else plan.id,
                  "verdict": "failed" if case == "rejected" else "completed"},
        ))
    await db_session.flush()
    assert await reconcile_task_from_latest_completed_plan(db_session, task) is False
    assert task.status == "in_progress"
    assert task.actual_output is None
