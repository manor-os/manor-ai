"""A Planner selects Agent contracts; it does not receive their entire runtime."""
import json
from copy import deepcopy
from types import SimpleNamespace

import jsonschema
import pytest

from packages.core.ai.runtime.planning import (
    runtime_execute_planner_tool_call,
    runtime_planner_system_prompt,
    runtime_planner_task_prompt,
    runtime_planner_tool_schemas,
)
from packages.core.plans.schema import PlanStep

pytestmark = pytest.mark.unit


def _context():
    return SimpleNamespace(
        subscriptions=[SimpleNamespace(service_key="writer", id="sub-1", agent_id="agent-1")],
        agents_by_id={"agent-1": SimpleNamespace(
            name="Writer", description="Research and write customer reports.",
            system_prompt="PRIVATE_EXECUTION_INSTRUCTIONS", config={"input_schema": {"secret": "not a contract"}},
        )},
        allowed_service_keys={"writer"},
        service_tool_names={"writer": ["generate_file"]},
        document_groups=[{"id": "doc-1", "name": "Unrelated giant resource index"}],
        staff=[{"staff_id": "staff-1", "role": "editor"}],
    )


def test_initial_prompt_is_agent_introductions_not_internal_inventory():
    context = _context()
    prompt = runtime_planner_system_prompt(**vars(context))
    assert "Research and write customer reports." in prompt
    assert "PRIVATE_EXECUTION_INSTRUCTIONS" not in prompt
    assert "generate_file" not in prompt
    assert "Unrelated giant resource index" not in prompt
    assert "staff-1" not in prompt
    assert len(prompt) < 5500
    context.service_tool_names["writer"] = [f"arbitrary_tool_{n}" for n in range(1000)]
    assert runtime_planner_system_prompt(**vars(context)) == prompt


def test_agent_schema_uses_real_delegation_input_and_step_defined_output():
    result = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "writer"}, context=_context())
    assert result["description"] == "Research and write customer reports."
    assert result["output_contract"]["scope"] == "per_step"
    assert result["output_contract"]["declaration"] == "output_shape or expected_output_schema"
    assert "file.write" in {item["id"] for item in result["capabilities"]}
    assert "PRIVATE_EXECUTION_INSTRUCTIONS" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    params = {"prompt": "Write a report from the supplied inputs."}
    jsonschema.validate(params, result["input_schema"])
    PlanStep(key="write", kind=result["kind"], service_key=result["service_key"], params=params, output_shape="TextResult")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, result["input_schema"])


@pytest.mark.parametrize("change", ["outside_scope", "missing_agent", "inactive", "inactive_subscription", "ambiguous"])
def test_agent_schema_cannot_borrow_another_or_ambiguous_deployment(change):
    context = _context()
    if change == "outside_scope":
        context.allowed_service_keys = set()
    elif change == "missing_agent":
        context.agents_by_id = {}
    elif change == "inactive":
        context.agents_by_id["agent-1"].status = "disabled"
    elif change == "inactive_subscription":
        context.subscriptions[0].status = "paused"
    else:
        context.subscriptions.append(SimpleNamespace(service_key="writer", id="sub-2", agent_id="agent-1"))
    assert "error" in runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "writer"}, context=context)


def test_agent_schema_does_not_project_an_entity_provider_union():
    context = _context()
    context.provider_actions = {"instagram": ["publish_post"]}
    context.service_provider_actions = {}
    result = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "writer"}, context=context)
    assert "external.social" not in {item["id"] for item in result["capabilities"]}


def test_task_prompt_keeps_contracts_and_recovery_but_loads_business_inputs_on_demand():
    task = SimpleNamespace(title="Prepare report", required_skills=["report-skill"], details={
        "customer_context": {"business": "LONG_PRIVATE_BUSINESS_INPUT"},
        "created_from": "OPERATIONAL_BOOKKEEPING",
        "_claim_token": "PRIVATE_CLAIM",
        "runtime_context": {
            "instructions": "Keep customer quotes unchanged.",
            "required_refs": ["document:doc-1"], "required_capabilities": ["file.write"],
            "captured_from": {"user_id": "PRIVATE_REQUESTER"},
        },
        "completion_requirements": {"artifact": True},
        "required_plan_steps": [{"key": "report", "kind": "subagent"}],
        "_replan_context": {
            "reason": "missing_artifact", "succeeded_steps": [{"step_key": "research", "result_summary": "FACTS_READY"}],
            "claim_token": "PRIVATE_REPLAN_TOKEN",
        },
    })
    prompt = runtime_planner_task_prompt(task)
    for visible in ("Keep customer quotes unchanged.", "document:doc-1", "file.write", "FACTS_READY", "report-skill", "customer_context", "required_plan_steps"):
        assert visible in prompt
    for hidden in ("LONG_PRIVATE_BUSINESS_INPUT", "OPERATIONAL_BOOKKEEPING", "PRIVATE_CLAIM", "PRIVATE_REQUESTER", "PRIVATE_REPLAN_TOKEN", "Details JSON"):
        assert hidden not in prompt
    loaded = runtime_execute_planner_tool_call("get_task_context", {"section": "input", "field": "customer_context"}, context=_context(), task=task)
    assert loaded["value"] == task.details["customer_context"]
    for field in ("_claim_token", "runtime_context", "created_from", "missing"):
        assert "error" in runtime_execute_planner_tool_call("get_task_context", {"section": "input", "field": field}, context=_context(), task=task)


def test_resource_discovery_returns_only_the_requested_section():
    context = _context()
    assert runtime_execute_planner_tool_call("get_task_context", {"section": "documents"}, context=context) == {"documents": context.document_groups}
    assert runtime_execute_planner_tool_call("get_task_context", {"section": "staff"}, context=context) == {"staff": context.staff}
    assert "error" in runtime_execute_planner_tool_call("get_task_context", {"section": "all"}, context=context)
    schemas = {schema["function"]["name"]: schema["function"] for schema in runtime_planner_tool_schemas()}
    assert {"get_agent_schema", "get_task_context", "submit_plan"} <= set(schemas)
    for schema in schemas.values():
        jsonschema.Draft202012Validator.check_schema(schema["parameters"])


def test_legacy_tool_discovery_never_uses_agent_system_prompt_as_description():
    result = runtime_execute_planner_tool_call("list_tools", {"service_key": "writer"}, context=_context())
    assert result["agent_description"] == "Research and write customer reports."
    assert "PRIVATE_EXECUTION_INSTRUCTIONS" not in json.dumps(result)
    assert not {"available_actions", "available_documents", "assigned_staff", "allowed_services"} & result.keys()


def test_external_action_target_is_a_binding_requirement_not_an_authorization_dump():
    task = SimpleNamespace(title="Publish the approved video", details={
        "external_action": {
            "provider": "youtube", "action": "publish_video",
            "destination": "studio.youtube.com", "visibility": "public",
            "intended_channel": "Customer Channel", "predecessor_task_key": "final_video",
            "max_executions": 1, "internal_note": "NOT_PLANNING_CONTEXT",
        },
        "proposal_external_authorization": {"authorization_id": "PRIVATE_RECEIPT", "approved_by": "PRIVATE_ACTOR"},
    })
    prompt = runtime_planner_task_prompt(task)
    for required in ("publish_video", "studio.youtube.com", "Customer Channel", "final_video", "max_executions"):
        assert required in prompt
    for hidden in ("PRIVATE_RECEIPT", "PRIVATE_ACTOR", "NOT_PLANNING_CONTEXT", "proposal_external_authorization"):
        assert hidden not in prompt
    assert "error" in runtime_execute_planner_tool_call(
        "get_task_context", {"section": "input", "field": "proposal_external_authorization"},
        context=_context(), task=task,
    )


@pytest.mark.parametrize("args", [[], ["writer"], "writer", 1, False])
def test_malformed_discovery_arguments_are_repair_feedback_not_a_crash(args):
    result = runtime_execute_planner_tool_call("get_agent_schema", args, context=_context())
    assert "JSON object" in result["error"]


@pytest.mark.asyncio
async def test_planner_repairs_discovery_call_and_fetches_only_the_needed_task_input(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    context = planner._Context(
        workspace=None, subscriptions=_context().subscriptions,
        agents_by_id=_context().agents_by_id, allowed_service_keys={"writer"},
        provider_actions={},
    )
    task = SimpleNamespace(
        title="Write a report", description="Use the customer context.",
        details={"customer_context": "SUPPLIED_CUSTOMER_FACTS", "unrelated": "UNNEEDED_DATA"},
        expected_output=None,
    )
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        if len(calls) == 1:
            tool_calls = [{"id": "bad", "name": "get_agent_schema", "arguments": '["writer"]'}]
        elif len(calls) == 2:
            tool_calls = [
                {"id": "agent", "name": "get_agent_schema", "arguments": '{"service_key":"writer"}'},
                {"id": "input", "name": "get_task_context", "arguments": '{"section":"input","field":"customer_context"}'},
            ]
        else:
            tool_calls = [{"id": "plan", "name": "submit_plan", "arguments": json.dumps({
                "plan_json": {"steps": [{
                    "key": "write", "kind": "subagent", "service_key": "writer",
                    "params": {"prompt": "Write a report using SUPPLIED_CUSTOMER_FACTS."},
                    "output_shape": "TextResult",
                    "execution_contract": {"delivery": "inline", "effects": "read_only"},
                }]},
            })}]
        return RuntimePlannerChatTurnResult("", tool_calls, {})

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    plan = await planner._generate_plan(task, context)
    assert len(calls) == 3
    assert "JSON object" in calls[1]["messages"][-1]["content"]
    assert "SUPPLIED_CUSTOMER_FACTS" not in calls[0]["messages"][0]["content"]
    assert "SUPPLIED_CUSTOMER_FACTS" in calls[2]["messages"][-1]["content"]
    assert "UNNEEDED_DATA" not in json.dumps(calls)
    assert plan.steps[0].service_key == "writer"
