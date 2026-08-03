from types import SimpleNamespace

import pytest

from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_service import validate_workflow_steps


def _run(*, variables=None, step_results=None):
    return WorkflowRun(
        id="run-1",
        workflow_id="workflow-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        started_by="user-1",
        variables=variables or {},
        step_results=step_results or {},
        trigger_data={},
    )


def test_orchestration_nodes_are_canonical():
    assert {"workflow_project", "workflow_action_grant", "browser_effect", "stage"} <= (
        CANONICAL_NODE_TYPES
    )


def test_stage_config_accepts_local_operations_and_declared_external_routes():
    result = validate_workflow_steps([
        {
            "id": "start",
            "type": "trigger",
            "next": ["prepare"],
        },
        {
            "id": "prepare",
            "type": "stage",
            "config": {
                "entry_operation_id": "normalize",
                "operations": [
                    {
                        "id": "normalize",
                        "type": "transform",
                        "config": {"mapping": {"ready": True}},
                        "next": ["check"],
                    },
                    {
                        "id": "check",
                        "type": "condition",
                        "config": {"field": "ready", "operator": "eq", "value": True},
                        "true_next": ["continue"],
                        "false_next": ["needs_input"],
                    },
                ],
                "routes": {
                    "continue": "done",
                    "needs_input": None,
                },
            },
            "next": ["done"],
        },
        {"id": "done", "type": "end", "next": []},
    ])

    assert result["valid"] is True
    assert result["errors"] == []


@pytest.mark.parametrize("missing_field", ["entry_operation_id", "operations", "routes"])
def test_stage_config_requires_all_contract_fields(missing_field):
    config = {
        "entry_operation_id": "work",
        "operations": [{"id": "work", "type": "transform", "next": ["done"]}],
        "routes": {"done": None},
    }
    config.pop(missing_field)

    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "next": ["stage"]},
        {"id": "stage", "type": "stage", "config": config, "next": []},
    ])

    assert result["valid"] is False
    assert any(
        error["code"] == "invalid_node_config"
        and f"config.{missing_field}" in error["message"]
        for error in result["errors"]
    )


def test_stage_config_rejects_duplicate_operation_ids():
    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "next": ["stage"]},
        {
            "id": "stage",
            "type": "stage",
            "config": {
                "entry_operation_id": "work",
                "operations": [
                    {"id": "work", "type": "transform", "next": []},
                    {"id": "work", "type": "notify", "next": []},
                ],
                "routes": {},
            },
            "next": [],
        },
    ])

    assert result["valid"] is False
    assert any(error["code"] == "duplicate_stage_operation_id" for error in result["errors"])


@pytest.mark.parametrize("nested_type", ["stage", "subworkflow", "foreach_subworkflow"])
def test_stage_config_rejects_nested_orchestration_nodes(nested_type):
    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "next": ["stage"]},
        {
            "id": "stage",
            "type": "stage",
            "config": {
                "entry_operation_id": "nested",
                "operations": [{"id": "nested", "type": nested_type, "next": []}],
                "routes": {},
            },
            "next": [],
        },
    ])

    assert result["valid"] is False
    assert any(error["code"] == "invalid_stage_operation_type" for error in result["errors"])


def test_stage_config_rejects_undeclared_external_edges():
    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "next": ["stage"]},
        {
            "id": "stage",
            "type": "stage",
            "config": {
                "entry_operation_id": "work",
                "operations": [
                    {"id": "work", "type": "transform", "next": ["undeclared"]},
                ],
                "routes": {},
            },
            "next": [],
        },
    ])

    assert result["valid"] is False
    assert any(error["code"] == "undeclared_stage_route" for error in result["errors"])


def test_resolve_structure_preserves_nested_reference_types():
    from packages.core.ai.workflow_runner import _resolve_structure

    variables = {
        "request": {"product_name": "Acme"},
        "scenes": [{"scene_id": "scene-1"}],
    }

    assert _resolve_structure(
        {"request": "{{request}}", "plan": {"scenes": "{{scenes}}"}},
        variables,
    ) == {
        "request": {"product_name": "Acme"},
        "plan": {"scenes": [{"scene_id": "scene-1"}]},
    }


def test_parse_agent_json_output_returns_structured_value():
    from packages.core.ai.workflow_runner import _parse_agent_output

    assert _parse_agent_output('```json\n{"ready": true}\n```', "json") == {
        "ready": True,
    }
    assert _parse_agent_output("plain text", "text") == "plain text"

    with pytest.raises(ValueError, match="valid JSON object"):
        _parse_agent_output("not json", "json")


@pytest.mark.asyncio
async def test_workflow_project_node_creates_patches_and_reads_validated_state(db_session):
    runner = WorkflowRunner()
    schema = {
        "type": "object",
        "properties": {
            "request": {"type": "object"},
            "scenes": {"type": "array"},
            "plan": {"type": ["object", "null"]},
        },
        "required": ["request", "scenes", "plan"],
        "additionalProperties": False,
    }
    run = _run(variables={"request": {"product_name": "Acme"}})

    created = await runner._execute_workflow_project_step(
        {
            "id": "create_project",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "current_stage": "draft",
                "state": {
                    "request": "{{request}}",
                    "scenes": [],
                    "plan": None,
                },
                "state_schema": schema,
                "output_var": "project",
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert created["status"] == "completed"
    assert created["output"]["revision"] == 0
    assert created["output"]["state"]["request"] == {"product_name": "Acme"}

    variables = {"project": created["output"], "plan": {"title": "Launch"}}
    patched = await runner._execute_workflow_project_step(
        {
            "id": "save_plan",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "current_stage": "awaiting_plan_approval",
                "patch": {"plan": "{{plan}}"},
                "state_schema": schema,
                "output_var": "project",
            },
        },
        variables,
        run,
        db_session,
    )

    assert patched["status"] == "completed"
    assert patched["output"]["revision"] == 1
    assert patched["output"]["current_stage"] == "awaiting_plan_approval"
    assert patched["output"]["state"]["plan"] == {"title": "Launch"}

    loaded = await runner._execute_workflow_project_step(
        {
            "id": "load_project",
            "config": {
                "operation": "get",
                "project_id": patched["output"]["project_id"],
                "project_type": "product_video",
                "state_schema": schema,
            },
        },
        {},
        run,
        db_session,
    )
    assert loaded["output"] == patched["output"]


@pytest.mark.asyncio
async def test_workflow_project_patch_appends_traceable_history_event(db_session):
    runner = WorkflowRunner()
    schema = {
        "type": "object",
        "properties": {"history": {"type": "array", "items": {"type": "object"}}},
        "required": ["history"],
        "additionalProperties": False,
    }
    run = _run()
    run.trigger_data = {"attempt_number": 2}
    created = await runner._execute_workflow_project_step(
        {
            "id": "create_project",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "state": {"history": []},
                "state_schema": schema,
                "output_var": "project",
            },
        },
        {},
        run,
        db_session,
    )

    patched = await runner._execute_workflow_project_step(
        {
            "id": "save_capture",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "history_event": {
                    "phase": "capture",
                    "status": "completed",
                    "receipt_ids": ["artifact-1"],
                },
                "state_schema": schema,
                "output_var": "project",
            },
        },
        {"project": created["output"]},
        run,
        db_session,
    )

    event = patched["output"]["state"]["history"][-1]
    assert event["run_id"] == "run-1"
    assert event["attempt_number"] == 2
    assert event["step_id"] == "save_capture"
    assert event["phase"] == "capture"
    assert event["status"] == "completed"
    assert event["receipt_ids"] == ["artifact-1"]
    assert event["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_workflow_project_node_rejects_invalid_state_before_persisting(db_session):
    result = await WorkflowRunner()._execute_workflow_project_step(
        {
            "id": "create_project",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "state": {"scenes": "invalid"},
                "state_schema": {
                    "type": "object",
                    "properties": {"scenes": {"type": "array"}},
                    "required": ["scenes"],
                },
            },
        },
        {},
        _run(),
        db_session,
    )

    assert result["status"] == "failed"
    assert result["code"] == "workflow_project_state_validation_failed"


@pytest.mark.asyncio
async def test_workflow_project_node_atomically_updates_owned_list_items(db_session):
    runner = WorkflowRunner()
    run = _run(
        variables={
            "scene_result": {"scene_id": "scene-1", "status": "completed"},
            "artifacts": [
                {"artifact_id": "artifact-1", "kind": "video"},
                {"artifact_id": "artifact-2", "kind": "image"},
            ],
        }
    )
    created = await runner._execute_workflow_project_step(
        {
            "id": "create",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "state": {
                    "scenes": [{"scene_id": "scene-1", "status": "planned"}],
                    "artifacts": [{"artifact_id": "artifact-1", "kind": "pending"}],
                },
            },
        },
        dict(run.variables),
        run,
        db_session,
    )
    run.variables["project"] = created["output"]

    updated = await runner._execute_workflow_project_step(
        {
            "id": "update_scene",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "list_upserts": [
                    {"path": "scenes", "key": "scene_id", "item": "{{scene_result}}"}
                ],
                "list_appends": [
                    {"path": "artifacts", "key": "artifact_id", "items": "{{artifacts}}"}
                ],
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert updated["output"]["state"]["scenes"] == [
        {"scene_id": "scene-1", "status": "completed"}
    ]
    assert updated["output"]["state"]["artifacts"] == [
        {"artifact_id": "artifact-1", "kind": "video"},
        {"artifact_id": "artifact-2", "kind": "image"},
    ]


@pytest.mark.asyncio
async def test_workflow_project_node_reconciles_list_to_declared_keys(db_session):
    runner = WorkflowRunner()
    run = _run(
        variables={
            "plan_scene_ids": ["scene-1", "scene-3"],
            "selected_scenes": [
                {"scene_id": "scene-3", "status": "planned"},
            ],
        }
    )
    created = await runner._execute_workflow_project_step(
        {
            "id": "create",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "state": {
                    "scenes": [
                        {
                            "scene_id": "scene-1",
                            "status": "completed",
                            "artifact_ids": ["artifact-1"],
                        },
                        {"scene_id": "scene-2", "status": "planned"},
                    ],
                },
            },
        },
        dict(run.variables),
        run,
        db_session,
    )
    run.variables["project"] = created["output"]

    updated = await runner._execute_workflow_project_step(
        {
            "id": "reconcile_scenes",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "list_reconciles": [
                    {
                        "path": "scenes",
                        "key": "scene_id",
                        "keys": "{{plan_scene_ids}}",
                        "items": "{{selected_scenes}}",
                    }
                ],
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert updated["status"] == "completed"
    assert updated["output"]["state"]["scenes"] == [
        {
            "scene_id": "scene-1",
            "status": "completed",
            "artifact_ids": ["artifact-1"],
        },
        {"scene_id": "scene-3", "status": "planned"},
    ]


@pytest.mark.asyncio
async def test_workflow_project_node_removes_list_items_matching_field_values(db_session):
    runner = WorkflowRunner()
    run = _run(variables={"selected_scene_ids": ["scene-1", "scene-3"]})
    created = await runner._execute_workflow_project_step(
        {
            "id": "create",
            "config": {
                "operation": "create",
                "project_type": "product_video",
                "state": {
                    "artifacts": [
                        {"artifact_id": "artifact-1", "scene_id": "scene-1"},
                        {"artifact_id": "artifact-2", "scene_id": "scene-2"},
                        {"artifact_id": "artifact-3", "scene_id": "scene-3"},
                        {"artifact_id": "final-video", "scene_id": None},
                    ]
                },
            },
        },
        dict(run.variables),
        run,
        db_session,
    )
    run.variables["project"] = created["output"]

    updated = await runner._execute_workflow_project_step(
        {
            "id": "remove_selected_scene_artifacts",
            "config": {
                "operation": "patch",
                "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {},
                "list_removes": [
                    {
                        "path": "artifacts",
                        "field": "scene_id",
                        "values": "{{selected_scene_ids}}",
                    }
                ],
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert updated["status"] == "completed"
    assert updated["output"]["state"]["artifacts"] == [
        {"artifact_id": "artifact-2", "scene_id": "scene-2"},
        {"artifact_id": "final-video", "scene_id": None},
    ]


@pytest.mark.asyncio
async def test_workflow_action_grant_node_requires_completed_approval(db_session):
    from packages.core.services.workflow_project_service import create_workflow_project

    project = await create_workflow_project(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        project_type="product_video",
        state={
            "approved_plan_version": 1,
            "plan": {"scene_ids": ["scene-1", "scene-2"]},
        },
        created_by="user-1",
    )
    runner = WorkflowRunner()
    run = _run(
        variables={
            "project_id": project.id,
            "scene_ids": ["scene-1", "scene-2"],
        },
        step_results={
            "approve_plan": {
                "status": "completed",
                "resumed": True,
                "wait_type": "approval",
                "approved": True,
                "approved_by": "user-1",
            }
        },
    )
    result = await runner._execute_workflow_action_grant_step(
        {
            "id": "grant_capture",
            "config": {
                "operation": "create",
                "approval_step_id": "approve_plan",
                "project_id": "{{project_id}}",
                "grant_type": "browser_capture",
                "scope": {
                    "approved_plan_version": 1,
                    "scene_ids": "{{scene_ids}}",
                    "allowed_actions": ["start_tab_recording", "click_element"],
                },
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert result["status"] == "completed"
    assert result["output"]["scope"]["scene_ids"] == ["scene-1", "scene-2"]
    assert result["output"]["granted_by"] == "user-1"
    assert run.trigger_data["_workflow_runtime_context"]["workflow_batch_capture"] is True

    denied = await runner._execute_workflow_action_grant_step(
        {
            "id": "grant_capture",
            "config": {
                "operation": "create",
                "approval_step_id": "missing_approval",
                "project_id": project.id,
                "grant_type": "browser_capture",
                "scope": {},
            },
        },
        {},
        run,
        db_session,
    )
    assert denied["status"] == "failed"
    assert denied["code"] == "workflow_action_grant_failed"

    run.step_results = {
        "approve_plan": {
            "status": "completed",
            "resumed": True,
            "wait_type": "approval",
            "approved": False,
            "approved_by": "user-1",
        }
    }
    rejected = await runner._execute_workflow_action_grant_step(
        {
            "id": "grant_capture",
            "config": {
                "operation": "create",
                "approval_step_id": "approve_plan",
                "project_id": project.id,
                "grant_type": "browser_capture",
                "scope": {},
            },
        },
        {},
        run,
        db_session,
    )
    assert rejected["status"] == "failed"
    assert "approved decision" in rejected["error"]


@pytest.mark.asyncio
async def test_browser_effect_node_never_retries_unknown_effect():
    record = {
        "effect_id": "effect-1",
        "scene_id": "scene-1",
        "action": "click_element",
        "precondition": {"url": "/marketplace"},
        "expected_postcondition": {"text": "Installed"},
        "status": "unknown",
        "evidence": [],
        "attempt_count": 1,
    }
    result = await WorkflowRunner()._execute_browser_effect_step(
        {"id": "effect", "config": {"operation": "decide", "record": "{{effect}}"}},
        {"effect": record},
    )

    assert result == {
        "status": "completed",
        "output": {"record": record, "decision": "observe_or_pause"},
        "output_var": None,
    }


@pytest.mark.asyncio
async def test_wait_timer_resolves_duration_from_workflow_variables(monkeypatch):
    runner = WorkflowRunner()
    scheduled = {}
    monkeypatch.setattr(
        runner,
        "enqueue_resume",
        lambda run_id, delay: scheduled.update(run_id=run_id, delay=delay) or True,
    )
    run = _run(variables={"capture": {"wait_seconds": 120}})

    result = await runner._execute_step(
        {
            "id": "wait_for_result",
            "type": "wait",
            "config": {
                "wait_type": "timer",
                "duration_seconds": "{{capture.wait_seconds}}",
            },
        },
        run,
        None,
    )

    assert result["status"] == "paused"
    assert result["duration_seconds"] == 120
    assert scheduled == {"run_id": "run-1", "delay": 120.0}


@pytest.mark.asyncio
async def test_tool_step_preserves_structured_arguments_and_parses_json(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    captured = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output='{"status":"completed","sample_count":2}',
            envelope=None,
        )

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_tool_step", fake_execute)
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        fake_attach,
    )
    result = await WorkflowRunner()._execute_tool_step(
        {
            "id": "frames",
            "type": "tool",
            "config": {
                "tool": "render_frame_samples",
                "args": {
                    "input_path": "{{video.fs_path}}",
                    "scene_boundaries": "{{boundaries}}",
                    "max_samples": 24,
                },
                "output_format": "json",
                "output_var": "frames",
            },
        },
        {
            "video": {"fs_path": "project/final.mp4"},
            "boundaries": [0, 4.5, 9],
        },
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert captured["arguments"] == {
        "input_path": "project/final.mp4",
        "scene_boundaries": [0, 4.5, 9],
        "max_samples": 24,
    }
    assert result["output"] == {"status": "completed", "sample_count": 2}


@pytest.mark.asyncio
async def test_tool_step_converts_structured_tool_error_to_node_failure(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    async def fake_execute(**_kwargs):
        return SimpleNamespace(
            output='{"status":"error","code":"probe_media_failed","error":"bad media"}',
            envelope=None,
        )

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_tool_step", fake_execute)
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        fake_attach,
    )
    result = await WorkflowRunner()._execute_tool_step(
        {
            "id": "probe",
            "type": "tool",
            "config": {
                "tool": "probe_media",
                "args": {"input_path": "bad.mp4"},
                "output_format": "json",
            },
        },
        {},
        "entity-1",
    )

    assert result["status"] == "failed"
    assert result["code"] == "probe_media_failed"
    assert result["error"] == "bad media"


@pytest.mark.asyncio
async def test_agent_step_includes_declared_output_schema_in_model_prompt(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    captured = {}

    async def fake_prepare(*_args, **_kwargs):
        return SimpleNamespace(
            tool_schemas=[],
            allowed_tool_names=frozenset(),
            envelope=None,
            context_section="",
            availability_section="",
        )

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content='{"start_url":"https://example.test"}',
            usage={},
            tool_calls_made=[],
            rounds=1,
        )

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_prepare,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_execute_workflow_agent_loop",
        fake_loop,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        fake_attach,
    )

    schema = {
        "type": "object",
        "properties": {"start_url": {"type": "string"}},
        "required": ["start_url"],
        "additionalProperties": False,
    }
    forced_tool_calls = [
        {
            "name": "invoke_skill",
            "arguments": {
                "skill": "chrome",
                "input": "Inspect {{request}} read-only.",
            },
        }
    ]
    result = await WorkflowRunner()._execute_agent_step(
        {
            "id": "normalize",
            "type": "agent",
            "config": {
                "input": "Normalize {{request}}.",
                "output_format": "json",
                "output_schema": schema,
                "output_var": "normalized",
                "forced_tool_calls": forced_tool_calls,
            },
        },
        {"request": "Show the product"},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
        None,
    )

    assert result["status"] == "completed"
    assert captured["output_schema"] == schema
    assert captured["forced_tool_calls"] == [
        {
            "name": "invoke_skill",
            "arguments": {
                "skill": "chrome",
                "input": "Inspect Show the product read-only.",
            },
        }
    ]
    assert "Output contract:" in captured["user_message"]
    assert '"start_url"' in captured["user_message"]
    assert "Return ONLY a value that conforms to this JSON Schema" in captured[
        "user_message"
    ]


@pytest.mark.asyncio
async def test_skill_agent_step_forwards_resolved_forced_tool_calls(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    captured = {}

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_invoke_skill(_db, _skill, _entity_id, _input_text, **kwargs):
        captured.update(kwargs)
        return {
            "content": '{"available":true}',
            "usage": {},
            "tools_used": ["invoke_skill"],
            "rounds": 2,
            "stop_reason": "completed",
        }

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "async_session", FakeSessionContext)
    monkeypatch.setattr(runner_module, "runtime_invoke_skill", fake_invoke_skill)
    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_trace_envelope_for_turn",
        lambda _request: None,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        fake_attach,
    )

    result = await WorkflowRunner()._execute_agent_step(
        {
            "id": "preflight",
            "type": "agent",
            "config": {
                "skill": "product_experience_mapper",
                "input": "Inspect {{start_url}}.",
                "output_format": "json",
                "output_schema": {
                    "type": "object",
                    "properties": {"available": {"type": "boolean"}},
                    "required": ["available"],
                },
                "forced_tool_calls": [
                    {
                        "name": "invoke_skill",
                        "arguments": {
                            "skill": "chrome",
                            "input": "Open {{start_url}} read-only.",
                        },
                    }
                ],
            },
        },
        {"start_url": "https://example.test"},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert result["status"] == "completed"
    assert captured["forced_tool_calls"] == [
        {
            "name": "invoke_skill",
            "arguments": {
                "skill": "chrome",
                "input": "Open https://example.test read-only.",
            },
        }
    ]
