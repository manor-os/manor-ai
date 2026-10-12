"""Deterministic Workflow tools carry host-owned, stable approval identities."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import workflow_tools


@pytest.mark.asyncio
async def test_workflow_tool_identity_survives_retry_and_separates_runs_and_nodes(monkeypatch):
    execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(workflow_tools, "runtime_execute_tool", execute)
    monkeypatch.setattr(workflow_tools, "runtime_prepare_named_tool_surface_for_turn",
        lambda *args, **kwargs: SimpleNamespace(allowed_tool_names={"prepare_recording"}, envelope=None))
    request = SimpleNamespace(entity_id="account", user_id="actor", workspace_id="workspace",
        conversation_id=None, task_id=None, input_preview="prepare source")
    for run, node in (("run-a", "prepare"), ("run-a", "prepare"), ("run-b", "prepare"), ("run-a", "render")):
        await workflow_tools.runtime_execute_workflow_tool_step(
            request=request, tool_name="prepare_recording",
            arguments={"_runtime_run_id_from_context": "forged", "_runtime_tool_call_id_from_context": "forged"},
            workflow_context={"workflow_run_id": run, "workflow_step_id": node},
        )
    identities = [(call.args[1]["_runtime_run_id_from_context"],
                   call.args[1]["_runtime_tool_call_id_from_context"])
                  for call in execute.await_args_list]
    assert identities == [("workflow:run-a", "node:prepare"), ("workflow:run-a", "node:prepare"),
                          ("workflow:run-b", "node:prepare"), ("workflow:run-a", "node:render")]
    await workflow_tools.runtime_execute_workflow_tool_step(
        request=request, tool_name="prepare_recording",
        arguments={"_runtime_run_id_from_context": "forged", "_runtime_tool_call_id_from_context": "forged"},
        workflow_context={"workflow_run_id": "run-a"},
    )
    assert "_runtime_run_id_from_context" not in execute.await_args.args[1]
    assert "_runtime_tool_call_id_from_context" not in execute.await_args.args[1]


@pytest.mark.asyncio
async def test_named_nested_loop_nodes_have_distinct_stable_host_owned_identities(monkeypatch):
    from copy import deepcopy
    from packages.core.ai import workflow_runner
    from packages.core.models.workflow import WorkflowRun

    execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(workflow_tools, "runtime_execute_tool", execute)
    monkeypatch.setattr(workflow_tools, "runtime_prepare_named_tool_surface_for_turn",
        lambda *args, **kwargs: SimpleNamespace(allowed_tool_names={"send"}, envelope=None))
    monkeypatch.setattr(workflow_runner, "runtime_request_for_surface_turn", lambda **kwargs: SimpleNamespace(
        entity_id=kwargs["entity_id"], user_id=kwargs["user_id"], workspace_id=kwargs["workspace_id"],
        conversation_id=kwargs["conversation_id"], task_id=kwargs["task_id"], input_preview="test"))
    async def no_projection(result, envelope):
        return result
    monkeypatch.setattr(workflow_runner, "runtime_attach_and_persist_workflow_runner_result", no_projection)
    leaf = {"id": "send", "type": "tool", "workflow_execution_node_id": "forged",
        "config": {"tool": "send", "args": {
            "_runtime_run_id_from_context": "forged", "_runtime_tool_call_id_from_context": "forged",
        }}}
    step = {"id": "loop", "type": "loop", "config": {"items": [1, 2], "steps": [
        leaf, {"id": "nested", "type": "loop", "config": {"items": [3, 4], "steps": [leaf]}},
    ]}}
    original = deepcopy(step)
    runner = workflow_runner.WorkflowRunner()
    batches = []
    for run_id in ("run-a", "run-a", "run-b"):
        run = WorkflowRun(id=run_id, workflow_id="workflow", entity_id="account", started_by="actor",
            variables={}, step_results={}, trigger_data={})
        before = execute.await_count
        result = await runner._execute_step(step, run, None)
        assert result["status"] == "completed", result
        calls = execute.await_args_list[before:]
        assert len(calls) == 6
        assert all(call.args[1]["_workflow_step_id_from_context"] == "send" for call in calls)
        identities = [(call.args[1]["_runtime_run_id_from_context"],
            call.args[1]["_runtime_tool_call_id_from_context"]) for call in calls]
        assert len(set(identities)) == 6
        assert all(identity[1].startswith("execution:") and "forged" not in str(identity)
            for identity in identities)
        batches.append(identities)
    assert batches[0] == batches[1]
    assert set(batches[0]).isdisjoint(batches[2])
    assert step == original, "execution identity must not modify the saved template"


@pytest.mark.asyncio
async def test_loop_node_automatic_retry_keeps_execution_identity(monkeypatch):
    from packages.core.ai import workflow_runner
    from packages.core.models.workflow import WorkflowRun

    runner = workflow_runner.WorkflowRunner()
    contexts = []
    async def tool(step, variables, entity_id, user_id, runtime_context):
        contexts.append(dict(runtime_context))
        return {"status": "failed", "error": "retryable"} if len(contexts) == 1 else {"status": "completed", "output": "ok"}
    monkeypatch.setattr(runner, "_execute_tool_step", tool)
    run = WorkflowRun(id="run", workflow_id="workflow", entity_id="account", started_by="actor",
        variables={}, step_results={}, trigger_data={})
    result = await runner._execute_step({"id": "loop", "type": "loop", "config": {
        "items": [1], "steps": [{"id": "send", "type": "tool", "config": {
            "retry_on_fail": True, "max_tries": 2, "retry_wait_ms": 0,
        }}],
    }}, run, None)
    assert result["status"] == "completed", result
    assert len(contexts) == 2
    assert contexts[0] == contexts[1]
    assert contexts[0]["workflow_execution_node_id"]
    assert contexts[0]["workflow_step_id"] == "send"
