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


def _retried_production_step(unit_count: int = 19) -> dict:
    """A retried clip-making step: a long attempt history and finished clips."""
    from packages.core.services.workflow_agent_recovery import agent_retry_context

    recovery = None
    for attempt in range(4):
        recovery = agent_retry_context(
            {**({"agent_recovery": recovery} if recovery else {}), "status": "completed",
             "runtime": {"events": [f"event-{attempt}-{index}-" + "x" * 300 for index in range(30)]},
             "output": {"ready": False, "blocker": "The master could not be saved."}},
            run_id=f"attempt-{attempt}", step_id="produce", error="The master could not be saved.",
        )
    made = [{"unit_id": f"{index:02d}", "title": f"Shot {index}", "clip": {
        "document_id": f"clip-document-{index:02d}", "output_name": f"shot-{index:02d}.mp4",
        "duration_seconds": 6, "job_id": f"job-{index:02d}",
        "result_url": f"/api/v1/fs/entity/Workspaces/_by_id/ws/workflow/run/videos/shot-{index:02d}.mp4",
    }} for index in range(1, unit_count + 1)]
    return {
        "status": "failed", "resume_strategy": "reexecute", "agent_retry_recovery": True,
        "output": {"ready": False, "blocker": "The master could not be saved."},
        "agent_recovery": recovery,
        "production_progress": {"units_made": made, "units_to_make": [{"unit_id": "20", "title": "End card"}]},
    }


def test_small_siblings_leave_their_budget_to_larger_evidence():
    from packages.core.services.workflow_agent_recovery import recovery_prompt_evidence

    value = {"flag": False, "step": "produce", "receipts": [f"clip-document-{i:03d}" for i in range(150)],
             "history": "h" * 60_000}
    compact = recovery_prompt_evidence(value, max_chars=8000)

    # An equal split gave each of the four fields 2,000 characters and cut the
    # receipts although the two small fields used almost none of theirs.
    assert compact["receipts"] == value["receipts"]
    assert compact["history"]["truncated"] is True
    assert len(json.dumps(compact)) <= 8000


def test_retry_prompt_keeps_every_finished_clip_identity():
    from packages.core.ai.workflow_runner import _workflow_agent_resume_prompt

    prompt = _workflow_agent_resume_prompt(
        _retried_production_step(), runtime_context={"workflow_run_id": "retry"}, step_id="produce",
    )

    for index in range(1, 20):
        assert f"clip-document-{index:02d}" in prompt
    assert '"units_to_make": ["20"]' in prompt
    assert "read_step_recovery" not in prompt


@pytest.mark.asyncio
async def test_retried_agent_step_reads_its_complete_evidence(monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai import workflow_runner as module

    async def appendix(*args, **kwargs):
        schema = {"type": "function", "function": {"name": "read_file"}}
        return SimpleNamespace(prompt_appendix="", tool_schemas=[schema],
                               allowed_tool_names={"read_file"}, envelope=None)

    calls = []

    async def loop(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content='{"ready": true}', usage={}, tool_calls_made=[], rounds=1,
                               stop_reason="completed", error=None, error_detail=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", loop)
    step = {"id": "produce", "type": "agent", "config": {"prompt": "Make the clips", "output_format": "json"}}
    runner = WorkflowRunner()

    fresh = WorkflowRun(id="fresh", entity_id="entity", variables={}, step_results={})
    await runner._execute_step_safe(step, fresh, None)
    assert "read_step_recovery" not in calls[-1]["dynamic_tool_handlers"]
    assert "read_step_recovery" not in calls[-1]["allowed_tool_names"]

    retry = WorkflowRun(id="retry", entity_id="entity", variables={},
                        step_results={"produce": _retried_production_step()})
    await runner._execute_step_safe(step, retry, None)
    kwargs = calls[-1]
    assert "read_step_recovery" in kwargs["allowed_tool_names"]
    assert "read_step_recovery" in {tool["function"]["name"] for tool in kwargs["tools"]}
    assert "Call read_step_recovery" in kwargs["user_message"]

    read = kwargs["dynamic_tool_handlers"]["read_step_recovery"]
    content, offset = "", 0
    while offset is not None:
        page = json.loads(await read({"section": "production_progress", "offset": offset}))
        content += page["content"]
        offset = page["next_offset"]
    progress = json.loads(content)
    assert progress["units_made"][18]["clip"]["result_url"].endswith("shot-19.mp4")
    history = json.loads(await read({"section": "agent_recovery"}))
    assert history["total_chars"] > 32_000
    assert "error" in json.loads(await read({"section": "unknown"}))
