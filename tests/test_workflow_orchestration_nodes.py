import json
from types import SimpleNamespace

import pytest

from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_action_grant_service import (
    YOUTUBE_PUBLICATION_GRANT_TYPE,
    create_workflow_action_grant,
)
from packages.core.services.workflow_service import validate_workflow_steps


class _FakeSkillSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


def _stub_prompt_skill_definition(monkeypatch):
    """Control-outcome tests simulate a Prompt Skill, not its database lookup."""
    async def prompt_skill(*_args, **_kwargs):
        return SimpleNamespace(config={"type": "prompt"})

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", prompt_skill)


async def _execute_chrome_skill_result(monkeypatch, skill_result):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    async def invoke_skill(*_args, **_kwargs):
        return skill_result

    async def attach(result, _envelope):
        return result

    def fail_if_parsed(*_args, **_kwargs):
        raise AssertionError("Chrome control outcomes must precede business JSON")

    monkeypatch.setattr(runner_module, "runtime_invoke_skill", invoke_skill)
    monkeypatch.setattr(runner_module, "_parse_agent_output", fail_if_parsed)
    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_trace_envelope_for_turn",
        lambda _request: None,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        attach,
    )
    return await WorkflowRunner(
        session_factory=lambda: _FakeSkillSession()
    )._execute_agent_step(
        {
            "id": "upload_youtube_video",
            "type": "agent",
            "config": {
                "skill": "chrome",
                "input": "Upload the approved video.",
                "output_format": "json",
                "output_schema": {"type": "object"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )


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


@pytest.mark.asyncio
async def test_skill_step_reuses_runner_session_instead_of_opening_nested_session(
    monkeypatch,
):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    class _NestedSession:
        async def __aenter__(self):
            raise AssertionError(
                "worker-heavy cannot nest a second workflow session"
            )

        async def __aexit__(self, *_args):
            return None

    captured = {}

    async def invoke_skill(db, *_args, **_kwargs):
        captured["db"] = db
        return {
            "content": "ok",
            "usage": {},
            "tools_used": [],
            "rounds": 1,
            "stop_reason": "completed",
        }

    async def attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "runtime_invoke_skill", invoke_skill)
    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_trace_envelope_for_turn",
        lambda _request: None,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        attach,
    )

    outer = object()
    result = await WorkflowRunner(
        session_factory=lambda: _NestedSession()
    )._execute_agent_step(
        {
            "id": "intake_release_identity",
            "type": "agent",
            "config": {
                "skill": "jr-release-intake-state",
                "input": "Collect release materials.",
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
        outer,
    )

    assert captured["db"] is outer
    assert result["status"] == "completed"
    assert result["output"] == "ok"


def test_orchestration_nodes_are_canonical():
    assert {"workflow_project", "workflow_action_grant", "browser_effect", "stage"} <= (CANONICAL_NODE_TYPES)


def test_stage_config_accepts_local_operations_and_declared_external_routes():
    result = validate_workflow_steps(
        [
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
        ]
    )

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

    result = validate_workflow_steps(
        [
            {"id": "start", "type": "trigger", "next": ["stage"]},
            {"id": "stage", "type": "stage", "config": config, "next": []},
        ]
    )

    assert result["valid"] is False
    assert any(
        error["code"] == "invalid_node_config" and f"config.{missing_field}" in error["message"]
        for error in result["errors"]
    )


def test_stage_config_rejects_duplicate_operation_ids():
    result = validate_workflow_steps(
        [
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
        ]
    )

    assert result["valid"] is False
    assert any(error["code"] == "duplicate_stage_operation_id" for error in result["errors"])


@pytest.mark.parametrize("nested_type", ["stage", "subworkflow", "foreach_subworkflow"])
def test_stage_config_rejects_nested_orchestration_nodes(nested_type):
    result = validate_workflow_steps(
        [
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
        ]
    )

    assert result["valid"] is False
    assert any(error["code"] == "invalid_stage_operation_type" for error in result["errors"])


def test_stage_config_rejects_undeclared_external_edges():
    result = validate_workflow_steps(
        [
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
        ]
    )

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

    publication_grant = await create_workflow_action_grant(
        db_session,
        entity_id=run.entity_id,
        workspace_id=run.workspace_id,
        workflow_run_id=run.id,
        project_id=patched["output"]["project_id"],
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope={},
        granted_by=run.started_by,
    )
    run.trigger_data = {
        "_workflow_runtime_context": {
            "workflow_project_id": patched["output"]["project_id"],
            "workflow_action_grant_id": publication_grant.id,
        },
    }
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
    assert run.trigger_data["_workflow_runtime_context"] == {
        "workflow_project_id": patched["output"]["project_id"],
        "workflow_action_grant_id": publication_grant.id,
    }


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
                "list_upserts": [{"path": "scenes", "key": "scene_id", "item": "{{scene_result}}"}],
                "list_appends": [{"path": "artifacts", "key": "artifact_id", "items": "{{artifacts}}"}],
            },
        },
        dict(run.variables),
        run,
        db_session,
    )

    assert updated["output"]["state"]["scenes"] == [{"scene_id": "scene-1", "status": "completed"}]
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
async def test_workflow_action_grant_node_revokes_only_trusted_current_grant(
    db_session,
):
    from packages.core.services.workflow_project_service import create_workflow_project

    project = await create_workflow_project(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        project_type="stickman_video",
        state={},
        created_by="user-1",
    )
    other_project = await create_workflow_project(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        project_type="stickman_video",
        state={},
        created_by="user-1",
    )
    current_grant = await create_workflow_action_grant(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        workflow_run_id="run-1",
        project_id=project.id,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope={"source": "workflow_wait"},
        granted_by="user-1",
    )
    other_grant = await create_workflow_action_grant(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        workflow_run_id="run-2",
        project_id=other_project.id,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope={"source": "workflow_wait"},
        granted_by="user-1",
    )
    run = _run()
    run.trigger_data = {
        "_workflow_runtime_context": {
            "workflow_project_id": project.id,
            "workflow_action_grant_id": current_grant.id,
        }
    }

    result = await WorkflowRunner()._execute_workflow_action_grant_step(
        {
            "id": "release_publication_grant",
            "config": {"operation": "revoke_current"},
        },
        {},
        run,
        db_session,
    )

    await db_session.refresh(current_grant)
    await db_session.refresh(other_grant)
    assert result["status"] == "completed"
    assert result["output"]["grant_id"] == current_grant.id
    assert current_grant.revoked_at is not None
    assert other_grant.revoked_at is None
    assert (
        run.trigger_data["_workflow_runtime_context"].get(
            "workflow_action_grant_id"
        )
        is None
    )


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
async def test_wait_timer_resolves_duration_from_workflow_variables():
    runner = WorkflowRunner()
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
    assert result["auto_resume_scheduled"] is True
    assert run.continuation_token
    assert run.continuation_due_at is not None
    assert run.continuation_next_attempt_at == run.continuation_due_at


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
@pytest.mark.parametrize(
    ("tool_output", "expected_error"),
    [
        ("Error: provider unavailable", "Error: provider unavailable"),
        (
            '{"error":"credentials_unavailable","reason":"Connect Stripe"}',
            "credentials_unavailable",
        ),
    ],
)
async def test_tool_step_rejects_default_text_tool_errors(
    monkeypatch,
    tool_output,
    expected_error,
):
    from packages.core.ai import workflow_runner as runner_module

    async def fake_execute(**_kwargs):
        return SimpleNamespace(
            output=tool_output,
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
            "id": "connector_error",
            "type": "connector",
            "config": {
                "tool": "mcp__stripe__list_customers",
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
    )

    assert result["status"] == "failed"
    assert expected_error in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fanout_status", "expected_status"),
    [("partial", "paused"), ("failed", "failed")],
)
async def test_tool_step_keeps_incomplete_account_fanout_nonterminal(
    monkeypatch,
    fanout_status,
    expected_status,
):
    from packages.core.ai import workflow_runner as runner_module

    async def fake_execute(**_kwargs):
        return SimpleNamespace(
            output=json.dumps(
                {
                    "integration_account_selection": "all",
                    "status": fanout_status,
                    "total_account_count": 2,
                    "account_count": 1,
                    "failed_count": 1 if fanout_status == "failed" else 0,
                    "result_truncated_count": 0,
                    "omitted_account_count": 1 if fanout_status == "partial" else 0,
                    "continuation": (
                        {
                            "mode": "all",
                            "cursor_account_id": "account-2",
                            "remaining_account_count": 1,
                        }
                        if fanout_status == "partial"
                        else None
                    ),
                    "results": [],
                }
            ),
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
            "id": "fanout",
            "type": "connector",
            "config": {
                "tool": "mcp__stripe__list_customers",
                "args": {"integration_account_selection": "all"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
    )

    assert result["status"] == expected_status
    assert result["code"] == f"tool_fanout_{fanout_status}"
    if fanout_status == "partial":
        assert result["resume_strategy"] == "reexecute"
        assert result["auto_resume"] is True
    else:
        assert result["partial"] is False


@pytest.mark.asyncio
async def test_tool_step_routes_nonresumable_account_partial_through_failure_retry(
    monkeypatch,
):
    from packages.core.ai import workflow_runner as runner_module

    async def fake_execute(**_kwargs):
        return SimpleNamespace(
            output=json.dumps(
                {
                    "integration_account_selection": "all",
                    "status": "partial",
                    "total_account_count": 2,
                    "account_count": 2,
                    "failed_count": 1,
                    "result_truncated_count": 0,
                    "omitted_account_count": 0,
                    "continuation": None,
                    "results": [
                        {
                            "integration_account_id": "account-1",
                            "ok": True,
                            "result": {"id": "one"},
                        },
                        {
                            "integration_account_id": "account-2",
                            "ok": False,
                            "result": {"error": "provider_unavailable"},
                        },
                    ],
                }
            ),
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
            "id": "fanout",
            "type": "connector",
            "config": {
                "tool": "mcp__stripe__list_customers",
                "args": {"integration_account_selection": "all"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
    )

    assert result["status"] == "failed"
    assert result["code"] == "tool_fanout_partial"
    assert result["partial"] is True
    assert "resume_strategy" not in result


@pytest.mark.asyncio
async def test_tool_step_resumes_and_merges_account_fanout_cursor(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module
    from packages.core.constants.integrations import (
        INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
    )

    captured = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output=json.dumps(
                {
                    "integration_account_selection": "all",
                    "status": "complete",
                    "total_account_count": 2,
                    "account_count": 1,
                    "failed_count": 0,
                    "result_truncated_count": 0,
                    "omitted_account_count": 0,
                    "continuation": None,
                    "results": [
                        {
                            "integration_account_id": "account-2",
                            "ok": True,
                            "result_truncated": False,
                            "result": {"id": "cus_2"},
                        }
                    ],
                }
            ),
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
    previous = {
        "status": "paused",
        "resume_strategy": "reexecute",
        "output": {
            "integration_account_selection": "all",
            "status": "partial",
            "total_account_count": 2,
            "account_count": 1,
            "failed_count": 0,
            "result_truncated_count": 0,
            "omitted_account_count": 1,
            "continuation": {
                "mode": "all",
                "cursor_account_id": "account-2",
            },
            "results": [
                {
                    "integration_account_id": "account-1",
                    "ok": True,
                    "result_truncated": False,
                    "result": {"id": "cus_1"},
                }
            ],
        },
    }

    result = await WorkflowRunner()._execute_tool_step(
        {
            "id": "fanout",
            "type": "connector",
            "config": {
                "tool": "mcp__stripe__list_customers",
                "args": {"integration_account_selection": "all"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
        previous_result=previous,
    )

    assert captured["arguments"][INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT] == "account-2"
    assert result["status"] == "completed"
    assert [item["integration_account_id"] for item in result["output"]["results"]] == ["account-1", "account-2"]


@pytest.mark.asyncio
async def test_tool_step_uses_schema_backed_structured_output(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    structured_output = {
        "customer_id": "cus_structured",
        "created": True,
    }

    async def fake_execute(**_kwargs):
        return SimpleNamespace(
            output="Error: stale display text",
            structured_output=structured_output,
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
            "id": "create_customer",
            "type": "connector",
            "config": {
                "tool": "mcp__stripe__create_customer",
                "output_schema": {
                    "type": "object",
                    "required": ["customer_id", "created"],
                    "properties": {
                        "customer_id": {"type": "string"},
                        "created": {"type": "boolean"},
                    },
                },
            },
        },
        {},
        "entity-1",
        "user-1",
        {},
    )

    assert result["status"] == "completed"
    assert result["output"] == structured_output


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
    assert "Return ONLY a value that conforms to this JSON Schema" in captured["user_message"]


@pytest.mark.asyncio
async def test_skill_agent_step_forwards_resolved_forced_tool_calls(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

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


@pytest.mark.asyncio
async def test_skill_agent_step_forwards_workflow_authorization_context(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    captured = {}
    output_schema = {
        "type": "object",
        "properties": {"success": {"type": "boolean"}},
        "required": ["success"],
        "additionalProperties": False,
    }

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    def fake_prepare(_request):
        captured["used_trace_envelope"] = True
        return "workflow-envelope"

    def fail_named_surface(*_args, **_kwargs):
        raise AssertionError("a Skill-owned tool surface must not be narrowed by the node")

    async def fake_invoke_skill(_db, skill, _entity_id, _input_text, **kwargs):
        captured["skill"] = skill
        captured.update(kwargs)
        return {
            "content": '{"success":true}',
            "usage": {},
            "tools_used": ["mcp__chrome__read_page"],
            "rounds": 1,
            "stop_reason": "completed",
        }

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_trace_envelope_for_turn",
        fake_prepare,
    )
    monkeypatch.setattr(
        runner_module,
        "runtime_prepare_named_tool_surface_for_turn",
        fail_named_surface,
    )
    monkeypatch.setattr(runner_module, "runtime_invoke_skill", fake_invoke_skill)
    monkeypatch.setattr(
        runner_module,
        "runtime_attach_and_persist_workflow_runner_result",
        fake_attach,
    )

    result = await WorkflowRunner(
        session_factory=lambda: FakeSessionContext()
    )._execute_agent_step(
        {
            "id": "upload_youtube_video",
            "type": "agent",
            "config": {
                "skill": "chrome",
                "input": "Upload the approved video and return its identity.",
                "output_format": "json",
                "output_schema": output_schema,
            },
        },
        {},
        "entity-1",
        "user-1",
        {
            "workspace_id": "workspace-1",
            "workflow_run_id": "run-1",
            "workflow_lineage_root_run_id": "root-1",
            "workflow_project_id": "project-1",
            "workflow_action_grant_id": "grant-1",
            "workflow_step_id": "upload_youtube_video",
        },
    )

    assert result["status"] == "completed"
    assert captured["skill"] == "chrome"
    assert captured["used_trace_envelope"] is True
    assert captured["allowed_tool_names"] is None
    assert captured["runtime_tool_context"] == {
        "_workflow_run_id_from_context": "run-1",
        "_workflow_lineage_root_run_id_from_context": "root-1",
        "_workflow_project_id_from_context": "project-1",
        "_workflow_action_grant_id_from_context": "grant-1",
        "_workflow_step_id_from_context": "upload_youtube_video",
        "_performance_run_id_from_context": "run-1",
    }
    assert captured["output_schema"] == output_schema


@pytest.mark.oss_regression
def test_visibility_retry_inherits_upload_receipt_without_reupload():
    from packages.core.services.workflow_service import (
        _retry_variables,
        retry_inherited_step_ids,
    )

    workflow = SimpleNamespace(
        steps=[
            {"id": "upload_asset", "type": "agent", "next": ["upload_succeeded"]},
            {
                "id": "upload_succeeded",
                "type": "condition",
                "depends_on": ["upload_asset"],
                "true_next": ["finalize_visibility"],
                "false_next": [],
            },
            {
                "id": "finalize_visibility",
                "type": "agent",
                "depends_on": ["upload_succeeded"],
                "next": [],
            },
        ],
        variables={},
    )
    upload_receipt = {
        "success": True,
        "upload_occurred": True,
        "prepared_filename": "reviewed-asset.mp4",
        "video_id": "abcdefghijk",
        "watch_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "dialog": "",
        "blocker": "",
    }
    prior_results = {
        "upload_asset": {
            "status": "completed",
            "output": upload_receipt,
        },
        "upload_succeeded": {
            "status": "completed",
            "condition_result": True,
            "next_override": ["finalize_visibility"],
        },
    }
    inherited = retry_inherited_step_ids(
        workflow.steps,
        prior_results,
        "finalize_visibility",
    )
    assert {"upload_asset", "upload_succeeded"} <= inherited

    prior = SimpleNamespace(
        variables={},
        step_results=prior_results,
    )
    retry_variables = _retry_variables(
        workflow,
        prior,
        inherited,
        None,
        retry_from_step_id="finalize_visibility",
    )
    assert retry_variables["upload_asset"] == upload_receipt

    retry_run = _run(
        variables=retry_variables,
        step_results={step_id: prior_results[step_id] for step_id in inherited},
    )
    retry_run.current_step_id = "finalize_visibility"
    retry_run.retry_from_step_id = "finalize_visibility"
    runnable = WorkflowRunner()._find_runnable_steps(workflow, retry_run)

    assert [step["id"] for step in runnable] == ["finalize_visibility"]






@pytest.mark.asyncio
async def test_skill_agent_chrome_user_takeover_pauses_before_business_json(monkeypatch):
    chrome_outcome = {
        "status": "needs_user",
        "skill": "chrome",
        "run_id": "chrome-run-1",
        "summary": "Browser control stopped because the user took over Chrome.",
        "goal_status": "needs_user",
        "evidence": [{"kind": "chrome_runtime", "chrome_call_count": 3}],
        "pending_action": {
            "reason": "user_takeover",
            "group_id": "root-1",
        },
        "resume_context": {"next": "reobserve_and_continue"},
        "browser_group": {"group_id": "root-1"},
        "tabs": [],
        "artifacts": [],
    }

    result = await _execute_chrome_skill_result(monkeypatch, {
        "content": '{"success":false,"upload_occurred":true}',
        "usage": {},
        "tools_used": ["mcp__chrome__wait"],
        "rounds": 4,
        "stop_reason": "completed",
        "control": {},
        "chrome_outcome": chrome_outcome,
    })

    assert result["status"] == "paused"
    assert result["code"] == "chrome_user_intervention_required"
    assert result["resume_strategy"] == "reexecute"
    assert result["retryable"] is True
    assert result["chrome_outcome"] == chrome_outcome
    assert result["output"] == '{"success":false,"upload_occurred":true}'


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_retryable", [True, False])
async def test_skill_agent_chrome_block_preserves_outcome_retryability(
    monkeypatch,
    outcome_retryable,
):
    chrome_outcome = {
        "status": "blocked",
        "skill": "chrome",
        "run_id": "chrome-run-2",
        "summary": "Chrome is temporarily unavailable. Reconnect and resume.",
        "goal_status": "blocked",
        "evidence": [{"kind": "chrome_runtime", "chrome_call_count": 2}],
        "pending_action": None,
        "resume_context": {
            "next": "reobserve_and_continue",
            "block_reason": "chrome_cli_worker_not_paired",
        },
        "browser_group": {"group_id": "root-1"},
        "tabs": [],
        "artifacts": [],
        "retryable": outcome_retryable,
    }

    result = await _execute_chrome_skill_result(monkeypatch, {
        "content": '{"success":false,"blocker":"temporary Chrome failure"}',
        "usage": {},
        "tools_used": ["mcp__chrome__status"],
        "rounds": 2,
        "stop_reason": "completed",
        "control": {
            "kind": "tool_error",
            "blocked_capability": "chrome",
            "error_reason": "chrome_cli_worker_not_paired",
        },
        "chrome_outcome": chrome_outcome,
    })

    assert result["retryable"] is outcome_retryable
    assert result["chrome_outcome"] == chrome_outcome
    assert result["output"] == '{"success":false,"blocker":"temporary Chrome failure"}'
    if outcome_retryable:
        assert result["status"] == "paused"
        assert result["code"] == "chrome_recovery_required"
        assert result["resume_strategy"] == "reexecute"
    else:
        assert result["status"] == "failed"
        assert "retry_blocked" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill_result",
    [
        # Terminal unconfirmed tool error: the replay fields ride on the control.
        {
            "content": "x",
            "stop_reason": "error",
            "control": {
                "kind": "tool_error",
                "error_reason": "tool_outcome_unconfirmed",
                "terminal_failure": True,
                "retryable": False,
                "do_not_replay": True,
                "effect_state": "unconfirmed",
            },
        },
        # Skill error with no outcome control: only the receipt ledger knows.
        {
            "content": "x",
            "stop_reason": "error",
            "error": "publish timed out",
            "control": {},
            "tool_evidence": {"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1"}]},
        },
    ],
)
async def test_skill_agent_step_failure_carries_replay_fields(monkeypatch, skill_result):
    result = await _execute_chrome_skill_result(monkeypatch, skill_result)

    assert result["status"] == "failed"
    assert result["do_not_replay"] is True
    if "retryable" in skill_result["control"]:
        assert result["retryable"] is False
    assert result["effect_state"] == "unconfirmed"


async def _run_stickman_shaped_skill_step(monkeypatch, failure_control):
    """A Stickman video step: Skill agent node, fail_on_stop_reasons, retry_on_fail x3."""
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)
    calls = {"n": 0}

    async def invoke_skill(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "The video provider kept failing.",
                "usage": {}, "tools_used": ["generate_video"], "rounds": 3,
                "stop_reason": "tool_error",
                "control": failure_control,
            }
        return {
            "content": '{"video_url": "https://example.test/v.mp4"}',
            "usage": {}, "tools_used": ["generate_video"], "rounds": 1,
            "stop_reason": "completed", "control": {},
        }

    async def attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "runtime_invoke_skill", invoke_skill)
    monkeypatch.setattr(runner_module, "runtime_prepare_trace_envelope_for_turn", lambda _request: None)
    monkeypatch.setattr(runner_module, "runtime_attach_and_persist_workflow_runner_result", attach)
    runner = WorkflowRunner(session_factory=lambda: _FakeSkillSession())
    step = {
        "id": "generate_stickman_video",
        "type": "agent",
        "config": {
            "skill": "stickman-video-creator",
            "input": "Render the storyboard.",
            "output_format": "json",
            "fail_on_stop_reasons": ["error", "max_rounds", "tool_error"],
            "retry_on_fail": True,
            "max_tries": 3,
            # The blueprint waits 30 s between tries; the wait is not under test.
            "retry_wait_ms": 0,
        },
    }

    async def execute(step, run, db):
        return await runner._execute_agent_step(step, {}, "entity-1", "user-1", {"workspace_id": "workspace-1"})

    monkeypatch.setattr(runner, "_execute_step", execute)
    result = await runner._execute_step_safe(step, _run(), None)
    return result, calls["n"]


@pytest.mark.asyncio
async def test_stickman_step_retries_after_an_exhausted_in_loop_budget(monkeypatch):
    result, calls = await _run_stickman_shaped_skill_step(monkeypatch, {
        "stop_reason": "repeated_provider_tool_errors",
        "blocked_capability": "video",
        "retry_budget_exhausted": True,
        "retryable": False,
    })

    assert calls == 2
    assert result["status"] == "completed"
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_stickman_step_never_replays_an_unconfirmed_attempt(monkeypatch):
    result, calls = await _run_stickman_shaped_skill_step(monkeypatch, {
        "kind": "tool_error",
        "error_reason": "tool_outcome_unconfirmed",
        "retryable": False,
        "do_not_replay": True,
        "effect_state": "unconfirmed",
    })

    assert calls == 1
    assert result["status"] == "failed"
    assert result["do_not_replay"] is True
    assert result["attempts"] == 1


@pytest.mark.asyncio
async def test_fail_on_stop_reasons_failure_carries_an_unconfirmed_ledger(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    async def invoke_skill(*_args, **_kwargs):
        return {
            "content": "Ran out of rounds after the upload call.",
            "usage": {}, "tools_used": ["upload_video"], "rounds": 8,
            "stop_reason": "max_rounds",
            "control": {},
            "tool_evidence": {"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1"}]},
        }

    async def attach(result, _envelope):
        return result

    monkeypatch.setattr(runner_module, "runtime_invoke_skill", invoke_skill)
    monkeypatch.setattr(runner_module, "runtime_prepare_trace_envelope_for_turn", lambda _request: None)
    monkeypatch.setattr(runner_module, "runtime_attach_and_persist_workflow_runner_result", attach)
    result = await WorkflowRunner(session_factory=lambda: _FakeSkillSession())._execute_agent_step(
        {
            "id": "upload",
            "type": "agent",
            "config": {"skill": "uploader", "input": "Upload.", "fail_on_stop_reasons": ["max_rounds"]},
        },
        {}, "entity-1", "user-1", {"workspace_id": "workspace-1"},
    )

    assert result["status"] == "failed"
    assert result["stop_reason"] == "max_rounds"
    assert result["do_not_replay"] is True
    assert result["effect_state"] == "unconfirmed"


@pytest.mark.asyncio
async def test_skill_agent_pending_approval_is_not_reparsed_as_empty_json(monkeypatch):
    approval_control = {
        "kind": "approval_required",
        "tool_name": "mcp__chrome__upload",
        "reason": "action_confirmation_required",
        "retryable": True,
        "blocking_result": (
            '{"status":"approval_required","approvalToken":"secret-token"}'
        ),
        "provider_continuation": {"approvalToken": "secret-token"},
    }

    result = await _execute_chrome_skill_result(monkeypatch, {
        "content": "",
        "usage": {},
        "tools_used": ["mcp__chrome__upload"],
        "rounds": 1,
        "stop_reason": "hitl_required",
        "control": approval_control,
        "chrome_outcome": {
            "status": "needs_approval",
            "retryable": True,
        },
    })

    assert result == {
        "status": "paused",
        "output": "",
        "code": "chrome_approval_required",
        "error": "Chrome action approval is required before this Workflow step can continue.",
        "stop_reason": "hitl_required",
        "retryable": True,
        "resume_strategy": "reexecute",
        "usage": {},
        "tools_used": ["mcp__chrome__upload"],
        "chrome_outcome": {
            "status": "needs_approval",
            "retryable": True,
        },
    }
    assert "secret-token" not in str(result)


@pytest.mark.asyncio
async def test_skill_agent_terminal_stop_parent_is_not_reparsed_as_json(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_invoke_skill(_db, _skill, _entity_id, _input_text, **kwargs):
        return {
            "content": "Local Chrome control is unavailable.",
            "usage": {},
            "tools_used": ["mcp__chrome__read_page"],
            "rounds": 1,
            "stop_reason": "chrome_cli_worker_unavailable",
            "stop_parent": True,
            "control": {
                "terminal_failure": True,
                "retryable": False,
            },
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
            "id": "browser-skill",
            "type": "agent",
            "config": {
                "skill": "chrome",
                "input": "Inspect YouTube Studio.",
                "output_format": "json",
                "output_schema": {"type": "object"},
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert result == {
        "status": "failed",
        "output": "Local Chrome control is unavailable.",
        "error": "Local Chrome control is unavailable.",
        "stop_reason": "chrome_cli_worker_unavailable",
        "usage": {},
        "tools_used": ["mcp__chrome__read_page"],
        # The control's retryable=False rides on the failure. retry_on_fail
        # gates only on do_not_replay, which Chrome sets itself when an
        # action's execution is unknown.
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_skill_agent_uncertain_mutation_keeps_operation_non_replayable(monkeypatch):
    result = await _execute_chrome_skill_result(monkeypatch, {
        "content": "Chrome mutation outcome is unknown; reconcile it first.",
        "usage": {},
        "tools_used": ["mcp__chrome__upload"],
        "rounds": 1,
        "stop_reason": "uncertain_result",
        "stop_parent": True,
        "control": {},
        "chrome_outcome": {
            "status": "failed",
            "summary": "Chrome mutation outcome is unknown; reconcile it first.",
            "retryable": False,
            "do_not_replay": True,
        },
    })

    assert result["status"] == "failed"
    assert result["stop_reason"] == "uncertain_result"
    assert result["retryable"] is False
    assert result["do_not_replay"] is True
    assert "retry_blocked" not in result


@pytest.mark.asyncio
async def test_skill_agent_successful_stop_parent_is_not_treated_as_failure(monkeypatch):
    from packages.core.ai import workflow_runner as runner_module

    _stub_prompt_skill_definition(monkeypatch)

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def fake_invoke_skill(_db, _skill, _entity_id, _input_text, **kwargs):
        return {
            "content": "The remote coding task has been dispatched.",
            "usage": {},
            "tools_used": ["mcp__codex_cli__run"],
            "rounds": 1,
            "stop_reason": "local_coding_dispatched",
            "stop_parent": True,
            "control": {
                "terminal": True,
                "stop_parent": True,
            },
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
            "id": "coding-skill",
            "type": "agent",
            "config": {
                "skill": "local-coding-operations",
                "input": "Update the project.",
                "output_format": "text",
            },
        },
        {},
        "entity-1",
        "user-1",
        {"workspace_id": "workspace-1"},
    )

    assert result["status"] == "completed"
    assert result["output"] == "The remote coding task has been dispatched."
