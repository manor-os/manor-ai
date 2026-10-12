"""Planner accepts only a contract-valid candidate and can repair its own output."""
from copy import deepcopy
import json
from types import SimpleNamespace

from jsonschema import Draft202012Validator, ValidationError
import pytest

from packages.core.ai.runtime import RuntimePlannerChatTurnResult
from packages.core.ai.runtime.planning import runtime_planner_task_prompt, runtime_planner_tool_schemas
from packages.core.contracts.task_output import task_output_payload_schema
from packages.core.plans import planner
from packages.core.plans.service import plan_contract_gaps


TASK_SCHEMA = {
    "type": "object", "required": ["heading"], "additionalProperties": False,
    "properties": {"heading": {"type": "string"}},
}


def _task(expected_output=None):
    return SimpleNamespace(
        id="task-test", entity_id="entity-test", workspace_id=None,
        title="Write LANG-P1B.md", description="Save the requested Markdown document.",
        details={}, input_contract=None, expected_output=expected_output,
        owner_service_key="writer", delegate_service_keys=[],
    )


def _context():
    return planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="writer", id="sub", agent_id="writer-agent")],
        agents_by_id={"writer-agent": SimpleNamespace(id="writer-agent", name="Writer", system_prompt="")},
        allowed_service_keys={"writer"}, provider_actions={},
    )


def _candidate(**contract):
    return {"steps": [{
        "key": "write_document", "kind": "subagent", "service_key": "writer",
        "params": {"prompt": "Save LANG-P1B.md with one heading and three points."},
        "execution_contract": {"delivery": "artifact", "effects": "artifact_write"},
        **contract,
    }]}


def _response(candidate, mode="tool"):
    if mode == "direct":
        return RuntimePlannerChatTurnResult(json.dumps(candidate), [], {})
    argument = json.dumps(candidate) if mode == "legacy_tool" else candidate
    return RuntimePlannerChatTurnResult("", [{
        "id": "submit-1", "name": "submit_plan",
        "arguments": json.dumps({"plan_json": argument}),
    }], {})


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,rule", [('{"plan_json":', "json"), ([], "type"), (False, "type"), (None, "type")])
async def test_planner_argument_errors_use_shared_contract_without_empty_object_repair(monkeypatch, arguments, rule):
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        if len(calls) == 1:
            return RuntimePlannerChatTurnResult("", [{
                "id": "bad-args", "name": "submit_plan", "arguments": arguments,
            }], {})
        feedback = json.loads(kwargs["messages"][-1]["content"])
        assert feedback["contract"] == "tool_error"
        assert feedback["details"]["schema_rule"] == rule
        assert feedback["details"]["path"] == "$"
        return _response(_candidate(output_shape="DocumentResult"))

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    plan = await planner._generate_plan(_task(), _context())
    assert len(calls) == 2
    assert plan.steps[0].output_shape == "DocumentResult"


def test_planner_validation_feedback_does_not_expose_pydantic_input_values():
    from pydantic import BaseModel, ValidationError as ModelValidationError
    from packages.core.ai.runtime.planning import runtime_planner_validation_feedback

    class Input(BaseModel):
        count: int

    with pytest.raises(ModelValidationError) as error:
        Input(count="private-input-must-not-appear")
    feedback = runtime_planner_validation_feedback(error.value, {})
    assert "private-input-must-not-appear" not in json.dumps(feedback)
    assert feedback["details"]["issues"][0]["field_paths"] == ["plan_json.count"]


def test_submit_plan_advertises_the_real_nested_plan_schema():
    schema = runtime_planner_tool_schemas()[-1]["function"]["parameters"]
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    # Resolve all canonical refs through the tool parameter document root.
    validator.validate({"plan_json": _candidate(output_shape="DocumentResult")})
    with pytest.raises(ValidationError):
        validator.validate({"plan_json": {"steps": [{"key": "missing_kind"}]}})
    with pytest.raises(ValidationError):
        validator.validate({"plan_json": "opaque JSON string"})
    reserved = _candidate(output_shape="TextResult")
    reserved["steps"][0]["kind"] = "code"
    with pytest.raises(ValidationError):
        validator.validate({"plan_json": reserved})
    step_properties = schema["$defs"]["PlanStep"]["properties"]
    assert {"output_shape", "expected_output_schema"} <= step_properties.keys()
    assert "plan_json.steps[i].output_shape" in step_properties["output_shape"]["description"]
    assert "summary alongside files" in step_properties["output_shape"]["description"]
    assert "not inside params" in step_properties["expected_output_schema"]["description"]


@pytest.mark.parametrize("shape", [
    "InventedResult", "StepResult", "ArtifactResult.files[]",
    "ArtifactResult.files[] plus a concise completion record containing fetched URL, saved filename, and real openable file link",
])
def test_planner_tool_schema_rejects_unknown_or_prose_output_shapes(shape):
    schema = runtime_planner_tool_schemas()[-1]["function"]["parameters"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"plan_json": _candidate(output_shape=shape)})


def test_planner_shape_enum_uses_the_registry_and_preserves_task_schema_exception():
    from packages.core.contracts.shapes import shape_names

    schema = runtime_planner_tool_schemas()[-1]["function"]["parameters"]
    shape_property = schema["$defs"]["PlanStep"]["properties"]["output_shape"]
    assert shape_property["anyOf"][0]["enum"] == [name for name in shape_names() if name != "StepResult"]
    validator = Draft202012Validator(schema)
    for shape in shape_property["anyOf"][0]["enum"]:
        validator.validate({"plan_json": _candidate(output_shape=shape)})
    # Task binding, not the provider schema, decides whether this omission is
    # valid. Do not break the unique terminal Task-schema inheritance path.
    validator.validate({"plan_json": _candidate()})
    validator.validate({"plan_json": _candidate(output_shape=None, expected_output_schema=None)})


def test_provider_tool_adapters_preserve_planner_refs_and_shape_enum():
    from packages.core.ai.llm_client import _normalize_tools_for_llm, _openai_tools_to_anthropic_tools

    tools = runtime_planner_tool_schemas()
    normalized = _normalize_tools_for_llm(deepcopy(tools))
    expected = tools[-1]["function"]["parameters"]
    assert normalized[-1]["function"]["parameters"] == expected
    anthropic = _openai_tools_to_anthropic_tools(normalized)
    assert anthropic[-1]["input_schema"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tool", "legacy_tool", "direct"])
async def test_missing_step_contract_is_repaired_in_same_model_conversation(monkeypatch, mode):
    calls = []
    invalid = _candidate()
    corrected = _candidate(output_shape="DocumentResult")

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        return _response(invalid if len(calls) == 1 else corrected, mode)

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    plan = await planner._generate_plan(_task(), _context())

    assert len(calls) == 2
    assert plan.steps[0].output_shape == "DocumentResult"
    assert not plan_contract_gaps(plan.topo_order(), require_explicit_agent_outputs=True)
    history = calls[1]["messages"]
    assert history[-1]["role"] == ("user" if mode == "direct" else "tool")
    assert "write_document" in history[-1]["content"]
    assert "expected_output_schema" in history[-1]["content"]
    assert "Runtime must not guess" in history[-1]["content"]
    assert "Plan accepted" not in history[-1]["content"]
    if mode != "direct":
        assert history[-1]["tool_call_id"] == "submit-1"
        assert history[-2]["tool_calls"]
    feedback = json.loads(history[-1]["content"].split("\nPlease correct the plan")[0])
    issue = next(item for item in feedback["details"]["issues"] if item["code"] == "missing_explicit_output_contract")
    assert issue["step_key"] == "write_document"
    assert issue["field_paths"] == [
        "plan_json.steps[0].output_shape", "plan_json.steps[0].expected_output_schema",
    ]
    assert "ArtifactResult" in issue["allowed_output_shapes"]
    assert "StepResult" not in issue["allowed_output_shapes"]
    assert feedback["contract"] == "tool_error"
    assert feedback["error"] == "plan_validation_failed"
    assert "repair" not in feedback and "repair" not in feedback["details"]
    assert "output_shape" not in invalid["steps"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("contract,diagnostic", [
    ({"output_shape": "InventedResult"}, "unknown canonical output_shape"),
    ({"expected_output_schema": {}}, "empty/open schema"),
    ({"output_shape": "StepResult"}, "legacy generic envelope"),
    ({"output_shape": "TextResult", "expected_output_schema": TASK_SCHEMA}, "exactly one"),
    ({"expected_output_schema": {"$ref": "#/$defs/Missing"}}, "invalid expected_output_schema"),
])
async def test_contract_specific_error_reaches_model_before_a_corrected_custom_schema(monkeypatch, contract, diagnostic):
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        return _response(_candidate(**contract) if len(calls) == 1 else _candidate(expected_output_schema=TASK_SCHEMA))

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    plan = await planner._generate_plan(_task(), _context())
    assert diagnostic in calls[1]["messages"][-1]["content"]
    feedback = json.loads(calls[1]["messages"][-1]["content"])
    issue = next(item for item in feedback["details"]["issues"] if item.get("allowed_output_shapes"))
    assert issue["step_key"] == "write_document"
    assert all(path.startswith("plan_json.steps[0].") for path in issue["field_paths"])
    assert "ArtifactResult" in issue["allowed_output_shapes"]
    assert plan.steps[0].expected_output_schema == TASK_SCHEMA
    assert plan.steps[0].output_shape is None


@pytest.mark.asyncio
async def test_schema_validation_error_is_returned_as_tool_feedback(monkeypatch):
    calls = []
    invalid = _candidate(output_shape="TextResult")
    invalid["steps"][0]["params"] = {}

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        return _response(invalid if len(calls) == 1 else _candidate(output_shape="DocumentResult"))

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    await planner._generate_plan(_task(), _context())
    assert "params.prompt" in calls[1]["messages"][-1]["content"]
    assert "Plan accepted" not in calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_persistent_missing_contract_fails_at_turn_budget_without_guessing(monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        return _response(_candidate())

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    with pytest.raises(planner.PlannerError, match="after 8 turns"):
        await planner._generate_plan(_task(), _context())
    assert len(calls) == 8
    assert all("Plan accepted" not in call["messages"][-1]["content"] for call in calls[1:])


@pytest.mark.asyncio
async def test_real_task_schema_is_bound_to_the_unique_terminal_step(monkeypatch):
    async def complete(**kwargs):
        return _response(_candidate())

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    plan = await planner._generate_plan(_task(TASK_SCHEMA), _context())
    assert task_output_payload_schema(plan.steps[0].expected_output_schema) == TASK_SCHEMA
    assert plan.steps[0].output_shape is None
    assert not plan_contract_gaps(plan.topo_order(), task_expected_output=TASK_SCHEMA, require_explicit_agent_outputs=True)


@pytest.mark.asyncio
async def test_task_schema_exception_does_not_cover_nonterminal_steps(monkeypatch):
    calls = []
    plan = _candidate()
    plan["steps"].append({
        "key": "final", "kind": "llm", "service_key": "writer",
        "execution_contract": {"delivery": "inline", "effects": "read_only"},
        "params": {"prompt": "Return the contracted final payload."},
        "depends_on": ["write_document"],
    })

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        candidate = deepcopy(plan)
        if len(calls) > 1:
            candidate["steps"][0]["output_shape"] = "DocumentResult"
        return _response(candidate)

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    accepted = await planner._generate_plan(_task(TASK_SCHEMA), _context())
    assert len(calls) == 2
    assert "write_document" in calls[1]["messages"][-1]["content"]
    assert accepted.steps[0].output_shape == "DocumentResult"
    assert task_output_payload_schema(accepted.steps[1].expected_output_schema) == TASK_SCHEMA


@pytest.mark.parametrize("expected_output", [
    {"deliverables": [{"name": "Report", "format": "markdown"}]},
    {"description": "One saved document"},
    {"type": "object", "properties": {"broken": {"$ref": "#/$defs/Missing"}}},
])
def test_deliverable_metadata_or_invalid_schema_is_not_labeled_as_inheritable_task_schema(expected_output):
    prompt = runtime_planner_task_prompt(_task(expected_output))
    assert "Expected output (JSON Schema)" not in prompt
    assert "Expected deliverable requirements:" in prompt
    assert json.dumps(expected_output, ensure_ascii=False) in prompt


def test_mixed_task_schema_keeps_metadata_beside_the_enforceable_schema():
    expected = {**TASK_SCHEMA, "deliverables": [{"name": "Report"}]}
    prompt = runtime_planner_task_prompt(_task(expected))
    assert f"Expected output (JSON Schema): {json.dumps(TASK_SCHEMA)}" in prompt
    assert "Expected deliverable requirements:" in prompt
    assert json.dumps(expected) in prompt


@pytest.mark.asyncio
async def test_only_the_corrected_authored_contract_is_materialized(db_session, monkeypatch):
    from sqlalchemy import select

    from packages.core.contracts.task_output import OutputContractKind, output_contract_for_schema
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.service import create_plan_from_dag

    task = Task(
        id=generate_ulid(), entity_id=generate_ulid(), title="Save the requested report",
        status="in_progress", owner_service_key="writer", expected_output=None, details={},
    )
    db_session.add(task)
    await db_session.flush()
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        # No ExecutionPlan may be created for the first, unshaped candidate.
        assert not (await db_session.execute(select(ExecutionPlan.id).where(ExecutionPlan.task_id == task.id))).scalars().all()
        return _response(_candidate() if len(calls) == 1 else _candidate(expected_output_schema=TASK_SCHEMA))

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    candidate = await planner._generate_plan(task, _context())
    persisted = await create_plan_from_dag(
        db_session, entity_id=task.entity_id, workspace_id=None, task_id=task.id,
        agent_subscription_id=None, plan=candidate, enforce_contract=True,
    )
    step = (await db_session.execute(select(ExecutionStep).where(ExecutionStep.plan_id == persisted.id))).scalar_one()
    assert len(calls) == 2
    assert persisted.plan_dag["steps"][0]["expected_output_schema"] == TASK_SCHEMA
    assert output_contract_for_schema(step.expected_output_schema).kind is OutputContractKind.PLAN_CUSTOM
    assert step.expected_output_schema["required"] == ["heading"]
    assert step.expected_output_schema["properties"] == TASK_SCHEMA["properties"]
