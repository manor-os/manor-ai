"""DAG position does not decide whether an Agent Step may write an artifact."""

from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from packages.core.contracts.step_execution import (
    STEP_EXECUTION_CONTRACT_PARAM,
    StepEffects,
    current_step_effects,
    step_contract_tool_error,
    step_execution_scope,
)
from packages.core.contracts.task_output import task_output_payload_schema
from packages.core.plans.refs import resolve_refs
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.service import (
    _coordinator_source_step,
    _step_from_pydantic,
    bind_task_output_contract,
    plan_contract_gaps,
)


pytestmark = pytest.mark.unit
WRITE = {"delivery": "artifact", "effects": "artifact_write"}
REUSE = {"delivery": "artifact", "effects": "read_only"}


def _document_step(key, contract, *, source=None):
    return PlanStep(
        key=key,
        kind="subagent",
        service_key="writer",
        params={
            "prompt": "Produce only this Step's declared document result.",
            **({"source": f"${{{{ steps.{source}.result }}}}"} if source else {}),
        },
        output_shape="DocumentResult",
        execution_contract=contract,
        expects=["files"],
        depends_on=[source] if source else [],
    )


async def test_planner_accepts_intermediate_artifact_writers_without_read_only_repair(monkeypatch):
    from packages.core.plans import planner
    from tests.test_planner_output_contract_feedback import _context, _response, _task

    candidate = Plan(steps=[
        _document_step("collect", WRITE),
        _document_step("transcribe", WRITE, source="collect"),
        _document_step("deliver", REUSE, source="transcribe"),
    ])
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return _response(candidate.model_dump(mode="json"))

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", complete)
    accepted = await planner._generate_plan(_task(), _context())
    assert len(calls) == 1
    assert [step.execution_contract.model_dump(mode="json") for step in accepted.steps] == [WRITE, WRITE, REUSE]
    assert [step.output_shape for step in accepted.steps] == ["DocumentResult"] * 3


@pytest.mark.parametrize("folded", [False, True])
def test_intermediate_writes_and_final_read_only_reuse_keep_independent_contracts(folded):
    """Final Task output binding must not turn upstream documents into text."""
    from packages.core.plans.coordinator import _checklist_item

    task_schema = {
        "type": "object",
        "required": ["document_id", "fs_path"],
        "properties": {"document_id": {"type": "string"}, "fs_path": {"type": "string"}},
    }
    plan = bind_task_output_contract(Plan(steps=[
        _document_step("collect", WRITE),
        _document_step("transcribe", WRITE, source="collect"),
        _document_step("deliver", REUSE, source="transcribe"),
    ]), task_schema)
    assert not plan_contract_gaps(
        plan.topo_order(), task_expected_output=task_schema,
        require_explicit_agent_outputs=True, require_explicit_execution_contract=True,
    )
    assert [step.output_shape for step in plan.steps] == ["DocumentResult", "DocumentResult", None]
    assert task_output_payload_schema(plan.steps[-1].expected_output_schema) == task_schema
    source_steps = [
        _coordinator_source_step(_checklist_item(step)) if folded else step
        for step in plan.steps
    ]
    plan_row = SimpleNamespace(id="plan", entity_id="entity", workspace_id="workspace")
    rows = [_step_from_pydantic(plan_row, step, output_shape=step.output_shape) for step in source_steps]
    assert [row.depends_on for row in rows] == [[], ["collect"], ["transcribe"]]
    assert [row.params[STEP_EXECUTION_CONTRACT_PARAM] for row in rows] == [WRITE, WRITE, REUSE]

    collected = {"document_id": "collected-document", "fs_path": "raw-posts.json"}
    transcript = {"document_id": "transcript-document", "fs_path": "transcript.md"}
    Draft202012Validator(rows[0].expected_output_schema).validate(collected)
    Draft202012Validator(rows[1].expected_output_schema).validate(transcript)
    assert resolve_refs(rows[1].params, {"collect": collected}, strict=True)["source"] == collected
    assert resolve_refs(rows[2].params, {"transcribe": transcript}, strict=True)["source"] == transcript
    for row, expected in zip(rows, (None, None, "read_only"), strict=True):
        with step_execution_scope(row.params[STEP_EXECUTION_CONTRACT_PARAM]):
            error = step_contract_tool_error("generate_file", {"kind": "document", "content": "body"})
            if expected is None:
                assert error is None
            else:
                assert expected in error
            assert step_contract_tool_error("read_file", {"path": "transcript.md"}) is None
    assert current_step_effects() is None


@pytest.mark.parametrize("delivery", ["inline", "artifact"])
@pytest.mark.parametrize("effects", ["read_only", "artifact_write", "authorized"])
async def test_worker_uses_effects_not_delivery_or_dag_position(monkeypatch, delivery, effects):
    """Exercise the worker wrapper for a Step with an upstream dependency."""
    from packages.core.workers import internal

    async def execute(snapshot):
        assert snapshot["step_key"] == "intermediate"
        assert current_step_effects() is StepEffects(effects)
        error = step_contract_tool_error("generate_file", {"kind": "document", "content": "body"})
        assert bool(error) is (effects == "read_only")
        return {"fs_path": "intermediate.md"} if delivery == "artifact" else {"text": "intermediate result"}

    monkeypatch.setattr(internal, "_exec_subagent_in_step_scope", execute)
    result = await internal._exec_subagent({
        "step_key": "intermediate",
        "depends_on": ["collect"],
        "params": {STEP_EXECUTION_CONTRACT_PARAM: {"delivery": delivery, "effects": effects}},
    })
    assert result == ({"fs_path": "intermediate.md"} if delivery == "artifact" else {"text": "intermediate result"})
    assert current_step_effects() is None
