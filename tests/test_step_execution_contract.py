"""A Step's payload is not a permission; nested work may only narrow effects."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.core.ai.runtime.planning import runtime_planner_validation_feedback
from packages.core.contracts.step_execution import (
    STEP_EXECUTION_CONTRACT_PARAM,
    StepEffects,
    StepExecutionContract,
    current_step_effects,
    step_contract_tool_error,
    step_execution_prompt,
    step_execution_scope,
)
from packages.core.plans.schema import PlanStep
from packages.core.plans.service import (
    PlanContractError,
    _coordinator_source_step,
    _step_from_pydantic,
    persisted_plan_contract_gaps,
    plan_contract_gaps,
)


pytestmark = pytest.mark.unit
INLINE = {"delivery": "inline", "effects": "read_only"}
ARTIFACT = {"delivery": "artifact", "effects": "artifact_write"}


def _step(**updates):
    return PlanStep.model_validate({
        "key": "choose_color", "kind": "subagent", "service_key": "writer",
        "params": {"prompt": "Ask blue or green, then return only the selected color."},
        "output_shape": "TextResult", **updates,
    })


@pytest.mark.parametrize("value", [
    {"delivery": "inline"}, {"delivery": "inline", "effects": "anything"},
    {**INLINE, "allowed_tools": ["generate_file"]},
])
def test_execution_contract_is_a_closed_explicit_declaration(value):
    with pytest.raises(ValidationError):
        StepExecutionContract.model_validate(value)


def test_fresh_planning_requires_exact_execution_fields_and_legacy_remains_readable():
    step = _step()
    gaps = plan_contract_gaps([step], require_explicit_execution_contract=True)
    assert [gap.kind for gap in gaps] == ["missing_execution_contract"]
    feedback = runtime_planner_validation_feedback(
        PlanContractError(gaps), {"steps": [step.model_dump()]},
    )
    assert feedback["details"]["issues"][0]["field_paths"] == [
        "plan_json.steps[0].execution_contract.delivery",
        "plan_json.steps[0].execution_contract.effects",
    ]
    assert not persisted_plan_contract_gaps({"steps": [step.model_dump()]})
    assert current_step_effects() is None
    with step_execution_scope(None):
        assert step_contract_tool_error("generate_file", {"kind": "document"}) is None


@pytest.mark.parametrize("contract,shape,expects,expected", [
    (INLINE, "TextResult", ["files"], "conflicting_delivery_contract"),
    (INLINE, "TextResult", ["publish"], "conflicting_effect_contract"),
    (ARTIFACT, "DocumentResult", ["publish"], "conflicting_effect_contract"),
])
def test_only_explicit_mutually_exclusive_contracts_are_rejected(contract, shape, expects, expected):
    gaps = plan_contract_gaps([_step(execution_contract=contract, output_shape=shape, expects=expects)])
    assert expected in [gap.kind for gap in gaps]


@pytest.mark.parametrize("contract,shape,expects", [
    ({"delivery": "artifact", "effects": "read_only"}, "DocumentResult", ["files"]),
    ({"delivery": "inline", "effects": "authorized"}, "TextResult", ["publish"]),
    (INLINE, "DocumentResult", None),
    (ARTIFACT, "DocumentResult", ["files"]),
])
def test_output_shape_does_not_infer_effect_permissions(contract, shape, expects):
    assert not plan_contract_gaps([
        _step(execution_contract=contract, output_shape=shape, expects=expects),
    ], require_explicit_agent_outputs=True, require_explicit_execution_contract=True)


def test_materializer_owns_hidden_contract_and_fold_roundtrip_preserves_each_child():
    from packages.core.plans.coordinator import _checklist_item

    row = SimpleNamespace(id="plan", entity_id="entity", workspace_id="workspace")
    for contract in (INLINE, ARTIFACT):
        step = _step(execution_contract=contract, params={
            "prompt": "Only this step", STEP_EXECUTION_CONTRACT_PARAM: {
                "delivery": "inline", "effects": "authorized",
            },
        })
        restored = _coordinator_source_step(_checklist_item(step))
        assert restored.execution_contract == step.execution_contract
        materialized = _step_from_pydantic(row, restored)
        assert materialized.params[STEP_EXECUTION_CONTRACT_PARAM] == contract
    legacy = _step(params={"prompt": "old", STEP_EXECUTION_CONTRACT_PARAM: INLINE})
    assert STEP_EXECUTION_CONTRACT_PARAM not in _step_from_pydantic(row, legacy).params


def test_full_coordinator_fold_keeps_each_step_contract_without_root_widening():
    from packages.core.plans.coordinator import coordinator_plan_from
    from packages.core.plans.schema import Plan
    from packages.core.services.task_execution import CoordinatorRuntimeSelection, TaskPlanServiceBinding

    binding = TaskPlanServiceBinding(subscription_id="sub", service_key="writer", agent_id="agent")
    source = Plan(steps=[
        _step(key="A", execution_contract=INLINE),
        _step(key="C", execution_contract=ARTIFACT, output_shape="DocumentResult", depends_on=["A"]),
    ])
    folded = coordinator_plan_from(
        source, SimpleNamespace(title="Test", description="A chooses, C saves", details={}),
        coordinator=CoordinatorRuntimeSelection(root=binding, bindings=(binding,)),
    )
    assert folded.steps[0].execution_contract is None  # legacy coordinator is not a business grant
    children = [_coordinator_source_step(item) for item in folded.steps[0].params["checklist"]]
    assert [step.execution_contract for step in children] == [step.execution_contract for step in source.steps]
    with step_execution_scope(children[0].execution_contract):
        with step_execution_scope(folded.steps[0].execution_contract):
            assert current_step_effects() is StepEffects.READ_ONLY


def test_invalid_typed_contract_feedback_identifies_the_exact_field():
    from packages.core.plans.schema import Plan

    candidate = {"steps": [{**_step().model_dump(), "execution_contract": {
        "delivery": "inline", "effects": "write_everything",
    }}]}
    with pytest.raises(ValidationError) as caught:
        Plan.model_validate(candidate)
    feedback = runtime_planner_validation_feedback(caught.value, candidate)
    assert feedback["details"]["issues"][0]["field_paths"] == ["plan_json.steps[0].execution_contract.effects"]


async def test_planner_repairs_intermediate_file_delivery_before_any_plan_is_accepted(monkeypatch):
    from copy import deepcopy
    from packages.core.plans import planner
    from tests.test_planner_output_contract_feedback import _context, _response, _task

    candidate = {"steps": [
        _step(key="A", output_shape="DocumentResult", execution_contract=INLINE, expects=["files"]).model_dump(mode="json"),
        _step(key="B", execution_contract=INLINE).model_dump(mode="json"),
        _step(key="C", output_shape="DocumentResult", execution_contract=ARTIFACT,
              depends_on=["A", "B"], expects=["files"], params={"prompt": "Combine the two branch results into one file."}).model_dump(mode="json"),
    ]}
    turns = []

    async def model(**kwargs):
        turns.append(deepcopy(kwargs))
        if len(turns) == 1:
            return _response(candidate)
        feedback = json.loads(kwargs["messages"][-1]["content"])
        issue = next(item for item in feedback["details"]["issues"] if item["code"] == "conflicting_delivery_contract")
        assert issue["step_key"] == "A"
        assert issue["field_paths"] == ["plan_json.steps[0].execution_contract.delivery", "plan_json.steps[0].expects"]
        assert feedback["contract"] == "tool_error"
        assert issue["execution_contract_choices"] == {
            "delivery": ["inline", "artifact"],
            "effects": ["read_only", "artifact_write", "authorized"],
        }
        assert "repair" not in feedback and "repair" not in feedback["details"]
        repaired = deepcopy(candidate)
        repaired["steps"][0].update(output_shape="TextResult", expects=None)
        return _response(repaired)

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", model)
    accepted = await planner._generate_plan(_task(), _context())
    assert len(turns) == 2
    assert accepted.steps[0].output_shape == "TextResult"
    assert accepted.steps[0].execution_contract.effects is StepEffects.READ_ONLY
    assert accepted.steps[2].execution_contract.effects is StepEffects.ARTIFACT_WRITE


@pytest.mark.parametrize("tool,arguments", [
    ("generate_file", {"kind": "document", "content": "not allowed"}),
    ("replace_file", {"path": "a.md", "content": "no"}),
    ("patch_file", {"path": "a.md", "patch": "no"}),
    ("update_goal_value", {"value": 3}),
    ("workspace_agent", {"action": "update_goal_value", "value": 3}),
    ("mcp__unknown__send", {}),
    ("unclassified_tool", {}),
    ("execute_planned_action", {}),
])
def test_read_only_rejects_writes_unknown_and_mutating_control(tool, arguments):
    with step_execution_scope(INLINE):
        assert "No action was executed" in step_contract_tool_error(tool, arguments)


@pytest.mark.parametrize("tool,arguments", [
    ("read_file", {"path": "existing.md"}),
    ("search_tools", {"query": "read file"}),
    ("request_input", {"question": "Blue or green?"}),
    ("submit_result", {"result": {"text": "blue"}}),
    ("invoke_skill", {"skill": "file_engine"}),
])
def test_framework_continuations_and_individually_gated_skill_entry_remain_available(tool, arguments):
    with step_execution_scope(INLINE):
        assert step_contract_tool_error(tool, arguments) is None


def test_artifact_scope_does_not_allow_business_writes_or_sandbox_escape():
    with step_execution_scope(ARTIFACT):
        assert step_contract_tool_error("generate_file", {"kind": "document", "content": "body"}) is None
        assert step_contract_tool_error("update_goal_value", {"value": 1})
        assert step_contract_tool_error("bash", {"command": "anything"})
        assert step_contract_tool_error("mcp__unknown__send", {}, declared_effect="write")
        assert step_contract_tool_error("mcp__connected__get_record", {}, declared_effect="read") is None


def test_nested_scopes_cannot_widen_and_legacy_child_cannot_drop_parent_boundary():
    with step_execution_scope(INLINE):
        with step_execution_scope({"delivery": "artifact", "effects": "authorized"}):
            assert current_step_effects() is StepEffects.READ_ONLY
            assert step_contract_tool_error("generate_file", {"content": "no"})
        with step_execution_scope(None):
            assert current_step_effects() is StepEffects.READ_ONLY
        assert "later Steps own their own work" in step_execution_prompt()
    assert current_step_effects() is None


async def test_concurrent_steps_have_isolated_contract_scopes():
    barrier = asyncio.Event()
    ready = 0

    async def run(contract):
        nonlocal ready
        with step_execution_scope(contract):
            ready += 1
            if ready == 2:
                barrier.set()
            await barrier.wait()
            await asyncio.sleep(0)
            return step_contract_tool_error("generate_file", {"kind": "document", "content": "x"})

    readonly, writer = await asyncio.gather(run(INLINE), run(ARTIFACT))
    assert readonly and writer is None
    assert current_step_effects() is None


@pytest.mark.parametrize("dynamic", [False, True])
async def test_real_runtime_gate_rejects_before_approval_intent_and_handler(monkeypatch, dynamic):
    from packages.core.ai.runtime.approvals import RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
    from packages.core.ai.runtime.tool_execution import (
        runtime_execute_registered_tool, runtime_execute_scoped_dynamic_tool_handler,
    )
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    async def forbidden(*args, **kwargs):
        raise AssertionError("Restricted Step must not consume approval or enter a handler")

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", forbidden)
    with step_execution_scope(INLINE), runtime_tool_effect_intent_scope(forbidden):
        call = {
            "tool_name": "generate_file", "arguments": {
                "kind": "document", "content": "no", "approval_token": "do-not-consume",
                "execution_contract": {"delivery": "artifact", "effects": "authorized"},
            },
            "entity_id": "entity", "user_id": "user", "workspace_id": "workspace", "task_id": "task",
        }
        if dynamic:
            result = await runtime_execute_scoped_dynamic_tool_handler(**call, handler=forbidden)
        else:
            result = await runtime_execute_registered_tool(**call, handler_resolver=lambda _: forbidden)
    assert isinstance(result, RuntimePreflightValidationError)
    assert "effects=read_only" in result


async def test_prompt_skill_child_inherits_outer_scope_without_model_permission(monkeypatch):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    calls = []

    async def guard(self, request):
        calls.append(request.tool_name)
        return RuntimeApprovalDecision.allow(classify_runtime_tool(request.tool_name, request.arguments), request)

    async def child_write(**kwargs):
        raise AssertionError("Nested write escaped the parent Step")

    async def skill(**kwargs):
        # Like the actual prompt Skill loop: tools re-enter Runtime in the same
        # async context. Its broader local declaration cannot grant permission.
        with step_execution_scope({"delivery": "artifact", "effects": "authorized"}):
            return await runtime_execute_registered_tool(
                tool_name="generate_file", arguments={"kind": "document", "content": "no"},
                handler_resolver=lambda _: child_write, entity_id="entity", user_id="user",
                workspace_id="workspace", task_id="task",
            )

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    with step_execution_scope(INLINE):
        result = await runtime_execute_registered_tool(
            tool_name="invoke_skill", arguments={"skill": "test"},
            handler_resolver=lambda _: skill, entity_id="entity", user_id="user",
            workspace_id="workspace", task_id="task",
        )
    assert isinstance(result, RuntimePreflightValidationError)
    assert calls == ["invoke_skill"]


async def test_hitl_checkpoint_resume_keeps_step_limit_and_corrects_in_same_loop(monkeypatch):
    import packages.core.ai.agentic_loop as loop_module
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool

    calls = []

    def tool(name):
        return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}

    async def handler(**kwargs):
        raise AssertionError("choose_color must never create the delivery Step's file")

    async def execute(name, arguments):
        calls.append(name)
        assert name == "generate_file"
        return await runtime_execute_registered_tool(
            tool_name=name, arguments=arguments, handler_resolver=lambda _: handler,
            entity_id="entity", user_id="user", workspace_id="workspace", task_id="task",
        )

    async def model(messages, tools, **kwargs):
        if not calls:
            assert any("蓝色" in str(message.get("content")) for message in messages)
            return "", [{"id": "premature-file", "name": "generate_file", "arguments": {
                "kind": "document", "name": "downstream.md", "content": "蓝色",
            }}], {}
        assert any("effects=read_only" in str(message.get("content")) for message in messages)
        return '{"text":"蓝色"}', [], {}

    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "Complete the whole Task by delivering a document."},
            {"role": "user", "content": "This Step only asks blue or green."},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "choose", "type": "function", "function": {
                    "name": "request_input", "arguments": '{"question":"Blue or green?"}',
                },
            }]},
        ], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "choose", "name": "request_input", "arguments": {"question": "Blue or green?"}},
        pending_tool_result={"id": "choose", "name": "request_input", "content": '{"answer":"蓝色"}'},
    )
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    with step_execution_scope(INLINE):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="resume", max_rounds=4,
            tools=[tool("request_input"), tool("generate_file")], tool_executor=execute,
            resume_checkpoint=checkpoint.to_dict(),
        )
    assert result.error is None
    assert json.loads(result.content) == {"text": "蓝色"}
    assert calls == ["generate_file"]


async def test_worker_installs_materialized_limit_around_full_attempt(monkeypatch):
    from packages.core.workers import internal

    seen = []

    async def attempt(snapshot):
        seen.append(current_step_effects())
        assert "read-only" in step_execution_prompt()
        await asyncio.sleep(0)
        assert step_contract_tool_error("generate_file", {"content": "no"})
        return {"text": "蓝色"}

    monkeypatch.setattr(internal, "_exec_subagent_in_step_scope", attempt)
    result = await internal._exec_subagent({
        "params": {"prompt": "Only choose a color", STEP_EXECUTION_CONTRACT_PARAM: INLINE},
        "runtime_checkpoint": {"messages": [{"role": "user", "content": "resume"}]},
    })
    assert result == {"text": "蓝色"}
    assert seen == [StepEffects.READ_ONLY]
    assert current_step_effects() is None


@pytest.mark.parametrize("replacement", [None, {}, {"delivery": "inline", "effects": "authorized"}])
def test_human_resume_cannot_remove_or_widen_the_materialized_contract(replacement):
    from packages.core.services.step_resume import apply_step_resume

    step = SimpleNamespace(params={STEP_EXECUTION_CONTRACT_PARAM: INLINE}, step_status="waiting_human")
    with pytest.raises(ValueError, match="server-owned"):
        apply_step_resume(step, params_update={STEP_EXECUTION_CONTRACT_PARAM: replacement})
    assert step.params == {STEP_EXECUTION_CONTRACT_PARAM: INLINE}
    assert step.step_status == "waiting_human"
    apply_step_resume(step, params_update={"answers": {"color": "blue"}})
    assert step.params[STEP_EXECUTION_CONTRACT_PARAM] == INLINE
    assert step.params["answers"] == {"color": "blue"}
