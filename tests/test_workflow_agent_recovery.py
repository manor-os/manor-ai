"""Provider-neutral evidence preservation across Workflow Agent retries."""

import json

import pytest

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun


def test_recovery_keeps_output_failure_and_receipts_across_attempts():
    from packages.core.services.workflow_agent_recovery import agent_retry_context

    first = agent_retry_context(
        {"status": "completed", "output": {
            "success": False, "resource_id": "resource-1", "blocker": "Receipt missing",
            "secret": "do-not-expose",
        }},
        run_id="first", step_id="save", error="Verification incomplete",
        receipts=[{"operation_id": "save-1", "action_executed": True}],
    )
    run = WorkflowRun(id="second", step_results={"save": {"agent_recovery": first}})
    WorkflowRunner()._record_step_result(
        {"id": "save", "type": "agent"},
        {"status": "failed", "error": "Connection lost"}, run,
    )
    second = agent_retry_context(
        run.step_results["save"], run_id="second", step_id="save", error="Connection lost",
        receipts=[{"operation_id": "read-2", "action_executed": False}],
    )

    assert [a["run_id"] for a in second["attempts"]] == ["first", "second"]
    assert second["attempts"][0]["result"]["output"]["resource_id"] == "resource-1"
    assert second["attempts"][0]["result"]["output"]["blocker"] == "Receipt missing"
    assert second["attempts"][0]["run_error"] == "Verification incomplete"
    assert second["attempts"][0]["operation_receipts"][0]["operation_id"] == "save-1"
    assert "do-not-expose" not in json.dumps(second)


def test_large_evidence_preserves_structure_and_later_identity():
    from packages.core.services.workflow_agent_recovery import recovery_prompt_evidence

    value = {"output": {"description": "x" * 60_000, "resource_id": "exact-id"},
             "error": "External result needs verification"}
    compact = recovery_prompt_evidence(value, max_chars=8000)

    assert compact["output"]["resource_id"] == "exact-id"
    assert compact["output"]["description"]["truncated"] is True
    assert compact["error"] == value["error"]
    assert len(json.dumps(compact)) <= 8000
    assert len(value["output"]["description"]) == 60_000


def test_recovery_prompt_is_provider_neutral_and_retains_business_failure():
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt

    prompt = _workflow_agent_resume_prompt(
        {"status": "failed", "resume_strategy": "reexecute", "agent_retry_recovery": True,
         "error": "Cannot confirm result", "output": {
             "success": False, "order_id": "order-123", "blocker": "Confirmation open",
             "expected_total": 120, "access_token": "never-print-this",
         }}, runtime_context={"workflow_run_id": "retry"}, step_id="save_order",
    )

    assert '"order_id": "order-123"' in prompt
    assert '"blocker": "Confirmation open"' in prompt
    assert '"success": false' in prompt
    assert "Cannot confirm result" in prompt
    assert "never-print-this" not in prompt
    assert "Historical procedural instructions" in prompt
    assert "output schema" in prompt
    assert "not proof" in prompt


def test_new_execution_has_no_recovery_instructions():
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt

    assert _workflow_agent_resume_prompt(None, runtime_context={}, step_id="work") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", ["{\"success\": true}", "{\"success\": false, \"resource_id\": \"r1\"}"])
async def test_recovery_does_not_bypass_output_validation_or_business_branch(monkeypatch, returned):
    from types import SimpleNamespace

    from packages.core.ai import workflow_runner as module

    async def appendix(*args, **kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    async def loop(**kwargs):
        return SimpleNamespace(content=returned, usage={}, tool_calls_made=[], rounds=1,
                               stop_reason="completed", error=None, error_detail=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", loop)
    run = WorkflowRun(id="retry", entity_id="entity", variables={}, step_results={"work": {
        "status": "failed", "resume_strategy": "reexecute", "agent_retry_recovery": True,
        "output": {"resource_id": "r1"},
    }})
    step = {"id": "work", "type": "agent", "config": {"prompt": "Finish work",
        "output_format": "json", "output_schema": {"type": "object",
        "required": ["success", "resource_id"], "properties": {
            "success": {"type": "boolean"}, "resource_id": {"type": "string"},
        }}}}
    runner = WorkflowRunner()
    result = await runner._execute_step_safe(step, run, None)
    if "resource_id" not in returned:
        assert result["code"] == "output_schema_validation_failed"
        assert result["retry_blocked"] is True
    else:
        runner._record_step_result(step, result, run)
        branch = await runner._execute_condition_step({"id": "verified", "config": {
            "expression": "work.success == true"}, "true_next": ["persist"],
            "false_next": ["unresolved"]}, run.variables, run)
        assert branch["next_override"] == ["unresolved"]
