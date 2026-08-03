from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path

import pytest
from types import SimpleNamespace
from httpx import AsyncClient

from packages.core.services.workflow_chat_projection import (
    _workflow_retry_input_schema,
)
from sqlalchemy import text


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_VIDEO_BLUEPRINT = (
    REPO_ROOT
    / "packages/core/blueprints/configs/solo_company/product-video-studio-v1.json"
)
PRODUCT_EXPERIENCE_MAPPER_SKILL = (
    REPO_ROOT
    / "packages/core/ai/marketplace_skills/product-experience-mapper/SKILL.md"
)


@dataclass
class _Binding:
    id: str
    workflow_id: str
    workspace_id: str | None
    config: dict


@dataclass
class _Workflow:
    id: str
    name: str
    description: str = ""
    variables: dict | None = None
    steps: list[dict] | None = None


def _entrypoint(binding_id: str, *, threshold: float = 0.85):
    from packages.core.services.workspace_workflow_router import normalize_chat_entrypoint

    binding = _Binding(
        id=binding_id,
        workflow_id=f"workflow-{binding_id}",
        workspace_id="workspace-a",
        config={
            "chat_entrypoint": {
                "enabled": True,
                "title": f"Starter {binding_id}",
                "description": "Run the configured workflow.",
                "intent": {
                    "enabled": True,
                    "description": "Use for a matching production request.",
                    "minimum_confidence": threshold,
                },
                "projection": {"progress": True, "step_outputs": "explicit"},
                "wait_bridge": True,
            }
        },
    )
    workflow = _Workflow(
        id=binding.workflow_id,
        name=f"Workflow {binding_id}",
        variables={"request": "", "attachments": []},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [
                    {"key": "request", "type": "string", "required": True},
                    {"key": "attachments", "type": "json", "required": False},
                ],
            },
        }],
    )
    return normalize_chat_entrypoint(binding, workflow)


def test_normalize_chat_entrypoint_exposes_only_safe_config() -> None:
    entrypoint = _entrypoint("binding-a")

    assert entrypoint is not None
    assert entrypoint.binding_id == "binding-a"
    assert entrypoint.title == "Starter binding-a"
    assert [item["key"] for item in entrypoint.run_inputs] == ["request", "attachments"]
    assert entrypoint.intent_enabled is True
    assert entrypoint.minimum_confidence == 0.85
    assert entrypoint.projection["step_outputs"] == "explicit"


def test_product_video_blueprint_exposes_three_generic_workflow_starters() -> None:
    payload = json.loads(PRODUCT_VIDEO_BLUEPRINT.read_text(encoding="utf-8"))
    workflows = payload["recipe"]["workflows"]
    starters = [
        workflow for workflow in workflows
        if workflow.get("binding_config", {}).get("chat_entrypoint", {}).get("enabled")
    ]

    assert [workflow["slug"] for workflow in starters] == [
        "create-product-video-v1",
        "plan-product-video-v1",
        "revise-product-video-v1",
    ]
    assert all(
        not workflow["binding_config"]["chat_entrypoint"]["enabled"]
        for workflow in workflows
        if workflow.get("internal")
    )

    create, plan, revise = starters
    for workflow in (create, plan):
        inputs = workflow["run_inputs"]
        assert [item["key"] for item in inputs] == [
            "product_name",
            "start_url",
            "audience",
            "video_type",
            "promotion_goal",
            "must_show",
            "must_not_show",
            "final_cta",
            "narration_instructions",
            "subtitle_instructions",
            "production_constraints",
            "failure_policy",
            "output_profile",
            "reference_documents",
            "reference_assets",
            "browser_session",
            "source_brief",
        ]
        by_key = {item["key"]: item for item in inputs}
        assert by_key["product_name"]["target"] == "request.product_name"
        assert by_key["start_url"]["schema"]["format"] == "uri"
        assert by_key["video_type"]["schema"]["enum"] == [
            "walkthrough",
            "feature_promotion",
            "launch",
            "onboarding",
            "support",
        ]
        assert by_key["must_show"]["schema"]["x-ui"]["control"] == "line_list"
        assert by_key["output_profile"]["schema"]["type"] == "object"
        assert by_key["output_profile"]["default"]["aspect_ratio"] == "16:9"
        assert by_key["browser_session"]["hidden"] is True
        assert by_key["browser_session"]["default"] == "current_paired_chrome_session"
        assert by_key["source_brief"]["hidden"] is True
        assert by_key["source_brief"]["prefill"] == {
            "source": "chat_message",
            "mode": "raw",
        }
        input_prefill = workflow["steps"][0]["config"]["run_input_prefill"]
        assert input_prefill["source"] == "chat_message"
        assert input_prefill["mode"] == "structured"
        assert "clean start URL" in input_prefill["instructions"]
        entrypoint = workflow["binding_config"]["chat_entrypoint"]
        assert "input_mapping" not in entrypoint
        assert entrypoint["intent"]["enabled"] is True
        assert entrypoint["intent"]["minimum_confidence"] == 0.85
        assert entrypoint["wait_bridge"] is True

    assert [item["key"] for item in revise["run_inputs"]] == [
        "project_id",
        "retry_segment_ids",
        "finding_ids",
        "revision_notes",
        "request_patch",
    ]
    assert revise["run_inputs"][0]["required"] is True
    assert all(not item.get("required", False) for item in revise["run_inputs"][1:])

    create_stages = {step["id"]: step for step in create["steps"]}
    create_steps = {
        operation["id"]: operation
        for stage in create["steps"]
        for operation in (stage.get("config") or {}).get("operations") or []
    }
    assert len(create["steps"]) == 9
    assert create_stages["approve_plan"]["type"] == "stage"
    assert create_steps["approve_plan"]["config"]["wait_type"] == "approval"
    assert create_steps["grant_capture"]["type"] == "workflow_action_grant"
    assert create_steps["collect_assets"]["type"] == "agent"
    assert create_steps["collect_assets"]["next"] == [
        "validate_collection_contract"
    ]
    collection_validator = create_steps["validate_collection_contract"]
    assert collection_validator["type"] == "code"
    assert collection_validator["config"]["output_var"] == "collection_validation"
    assert collection_validator["next"] == ["collection_valid"]
    assert create_steps["collection_valid"]["config"]["expression"] == (
        "collection_validation.valid == true"
    )
    assert create_steps["collection_valid"]["false_next"] == [
        "save_capture_handoff"
    ]
    assert create_steps["save_capture_handoff"]["next"] == [
        "build_capture_handoff"
    ]
    assert create_steps["build_capture_handoff"]["next"] == ["needs_input"]
    retry_state = create_steps["save_capture_handoff"]["config"]["patch"][
        "retry_state"
    ]
    assert retry_state["retry_from_step_id"] == "collect_assets"
    assert retry_state["preserved_receipts"] == (
        "{{collection_validation.blocker.preserved_receipts}}"
    )
    assert "recover_assets" not in create_steps
    assert "operator_acceptance" not in create_steps
    assert len(workflows) == 3
    assert all(not workflow["internal"] for workflow in workflows)
    assert all(
        step["type"] not in {"subworkflow", "foreach_subworkflow"}
        and "model" not in (step.get("config") or {})
        for workflow in workflows
        for top_level_step in workflow["steps"]
        for step in (
            (top_level_step.get("config") or {}).get("operations") or []
            if top_level_step.get("type") == "stage"
            else [top_level_step]
        )
    )

    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in (
        "stickman",
        "localhost:3010",
        "product demo video studio",
        "product-demo-video-studio",
        "capture_authorized",
        "test_account",
    ):
        assert forbidden not in serialized


def test_product_experience_mapper_is_read_only_and_delegates_to_chrome() -> None:
    prompt = PRODUCT_EXPERIENCE_MAPPER_SKILL.read_text(encoding="utf-8")

    assert 'invoke_skill` with `skill="chrome"' in prompt
    assert "read-only" in prompt.lower()
    assert "Do not record" in prompt
    assert "Do not take screenshots" in prompt
    assert "Do not submit" in prompt
    assert "candidate product paths" in prompt


def test_hidden_workflow_inputs_keep_chat_context_without_entering_the_form() -> None:
    from packages.core.services.workspace_workflow_router import (
        normalize_chat_entrypoint,
        prefill_workspace_workflow_inputs,
    )

    binding = _Binding(
        id="binding-hidden",
        workflow_id="workflow-hidden",
        workspace_id="workspace-a",
        config={
            "chat_entrypoint": {
                "enabled": True,
                "title": "Create a product demo",
                "intent": {"enabled": True},
            }
        },
    )
    workflow = _Workflow(
        id="workflow-hidden",
        name="Product demo",
        variables={"request": "", "attachments": [], "product_name": "Manor"},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [
                    {
                        "key": "request",
                        "type": "string",
                        "required": False,
                        "hidden": True,
                        "default": "{{request}}",
                    },
                    {
                        "key": "attachments",
                        "type": "json",
                        "required": False,
                        "hidden": True,
                        "default": "{{attachments}}",
                    },
                    {
                        "key": "product_name",
                        "type": "string",
                        "required": True,
                        "default": "{{product_name}}",
                    },
                ],
            },
        }],
    )

    entrypoint = normalize_chat_entrypoint(binding, workflow)

    assert entrypoint is not None
    assert entrypoint.run_inputs[0]["hidden"] is True
    assert entrypoint.run_inputs[1]["hidden"] is True
    values = prefill_workspace_workflow_inputs(
        entrypoint,
        message="Create the configured Stickman product demo.",
        attachment_refs=[{"name": "reference.png", "kind": "image"}],
    )
    assert values == {
        "request": "Create the configured Stickman product demo.",
        "attachments": [{"name": "reference.png", "kind": "image"}],
        "product_name": "Manor",
    }


def test_legacy_multi_field_workflow_prefills_its_first_string_input() -> None:
    from packages.core.services.workspace_workflow_router import (
        normalize_chat_entrypoint,
        prefill_workspace_workflow_inputs,
    )

    binding = _Binding(
        id="binding-legacy-prefill",
        workflow_id="workflow-legacy-prefill",
        workspace_id="workspace-a",
        config={"chat_entrypoint": {"enabled": True}},
    )
    workflow = _Workflow(
        id=binding.workflow_id,
        name="Legacy intake",
        variables={"topic": "", "notes": ""},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [
                    {"key": "topic", "type": "string", "required": True},
                    {"key": "notes", "type": "string", "required": False},
                ],
            },
        }],
    )

    entrypoint = normalize_chat_entrypoint(binding, workflow)
    assert entrypoint is not None
    assert prefill_workspace_workflow_inputs(
        entrypoint,
        message="Prepare a launch brief.",
        attachment_refs=[],
    ) == {"topic": "Prepare a launch brief."}


@pytest.mark.asyncio
async def test_structured_workflow_input_prefill_extracts_an_editable_chat_draft(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.completions import RuntimeTextCompletionResult
    from packages.core.services.workspace_workflow_router import (
        assemble_workspace_workflow_inputs,
        normalize_chat_entrypoint,
        prepare_workspace_workflow_inputs,
    )

    request_schema = {
        "type": "object",
        "properties": {
            "aspect_ratio": {"type": "string"},
            "target_duration_seconds": {
                "type": "object",
                "properties": {
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                },
            },
            "language": {"type": "string"},
        },
    }
    binding = _Binding(
        id="binding-structured-prefill",
        workflow_id="workflow-structured-prefill",
        workspace_id="workspace-a",
        config={"chat_entrypoint": {"enabled": True}},
    )
    workflow = _Workflow(
        id=binding.workflow_id,
        name="Create product video",
        variables={
            "request": {
                "product_name": "",
                "start_url": "",
                "must_show": [],
                "output_profile": {
                    "aspect_ratio": "16:9",
                    "target_duration_seconds": {"min": 60, "max": 120},
                    "language": "request_language",
                },
            },
        },
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_input_prefill": {
                    "source": "chat_message",
                    "mode": "structured",
                    "instructions": "Map duration and required product moments.",
                },
                "run_inputs": [
                    {
                        "key": "product_name",
                        "type": "string",
                        "required": True,
                        "schema": {"type": "string"},
                        "target": "request.product_name",
                    },
                    {
                        "key": "start_url",
                        "type": "string",
                        "required": True,
                        "schema": {"type": "string", "format": "uri"},
                        "target": "request.start_url",
                    },
                    {
                        "key": "must_show",
                        "type": "json",
                        "required": True,
                        "schema": {"type": "array", "items": {"type": "string"}},
                        "target": "request.must_show",
                    },
                    {
                        "key": "output_profile",
                        "type": "json",
                        "required": True,
                        "schema": request_schema,
                        "default": "{{request.output_profile}}",
                        "target": "request.output_profile",
                    },
                    {
                        "key": "source_brief",
                        "type": "string",
                        "required": False,
                        "hidden": True,
                        "target": "request.source_brief",
                        "prefill": {
                            "source": "chat_message",
                            "mode": "raw",
                        },
                    },
                ],
            },
        }],
    )
    entrypoint = normalize_chat_entrypoint(binding, workflow)
    assert entrypoint is not None
    assert entrypoint.run_inputs[0]["prefill"]["mode"] == "structured"
    assert entrypoint.run_inputs[0]["target"] == "request.product_name"
    assert entrypoint.run_inputs[-1]["prefill"]["mode"] == "raw"

    captured: dict = {}

    async def fake_completion(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return RuntimeTextCompletionResult(
            content=json.dumps({
                "inputs": {
                    "product_name": {"value": "Manor Workspace"},
                    "start_url": {"value": (
                        "http://localhost:3010/workspaces%E3%80%82"
                        "%E8%A7%86%E9%A2%91%E7%94%BB%E9%9D%A2"
                    )},
                    "must_show": {"value": [
                        "展示 Workspace 列表",
                        "进入 Four Seasons Hotel workspace 详情",
                    ]},
                    "output_profile": {"value": {
                        "target_duration_seconds": {"min": 60, "max": 90},
                        "language": "zh-CN",
                    }},
                },
            }, ensure_ascii=False),
            usage={},
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.completions.runtime_execute_text_completion",
        fake_completion,
    )

    values = await prepare_workspace_workflow_inputs(
        entrypoint,
        message=(
            "请创建一条中文 Manor Workspace 功能介绍视频，时长 60-90 秒。"
            "Product start URL 使用 [http://localhost:3010/workspaces。视频画面]"
            "(http://localhost:3010/workspaces%E3%80%82%E8%A7%86%E9%A2%91)。"
        ),
        attachment_refs=[],
        entity_id="entity-a",
        user_id="user-a",
        workspace_id="workspace-a",
    )

    assert values == {
        "product_name": "Manor Workspace",
        "start_url": "http://localhost:3010/workspaces",
        "must_show": [
            "展示 Workspace 列表",
            "进入 Four Seasons Hotel workspace 详情",
        ],
        "output_profile": {
            "aspect_ratio": "16:9",
            "target_duration_seconds": {"min": 60, "max": 90},
            "language": "zh-CN",
        },
        "source_brief": (
            "请创建一条中文 Manor Workspace 功能介绍视频，时长 60-90 秒。"
            "Product start URL 使用 [http://localhost:3010/workspaces。视频画面]"
            "(http://localhost:3010/workspaces%E3%80%82%E8%A7%86%E9%A2%91)。"
        ),
    }
    assert assemble_workspace_workflow_inputs(entrypoint, values) == {
        "request": values,
    }
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}
    assert captured["kwargs"]["source"] == "chat"
    assert "Map duration and required product moments." in json.dumps(
        captured["messages"],
        ensure_ascii=False,
    )


def test_prefilled_uri_cleanup_preserves_valid_url_encoding() -> None:
    from packages.core.services.workspace_workflow_router import _clean_prefilled_uri

    assert _clean_prefilled_uri(
        "https://example.test/a%2Fb?q=hello%20world"
    ) == "https://example.test/a%2Fb?q=hello%20world"
    assert _clean_prefilled_uri(
        "https://example.test/path(with-parentheses)"
    ) == "https://example.test/path(with-parentheses)"
    assert _clean_prefilled_uri(
        "http://localhost:3010/workspaces%E3%80%82%E8%A7%86%E9%A2%91%E7%94%BB%E9%9D%A2"
    ) == "http://localhost:3010/workspaces"


def test_optional_workflow_string_can_explicitly_clear_its_default() -> None:
    from packages.core.services.workspace_workflow_router import (
        normalize_chat_entrypoint,
        validate_workspace_workflow_inputs,
    )

    binding = _Binding(
        id="binding-clear",
        workflow_id="workflow-clear",
        workspace_id="workspace-a",
        config={"chat_entrypoint": {"enabled": True, "title": "Clear a default"}},
    )
    workflow = _Workflow(
        id="workflow-clear",
        name="Clear a default",
        variables={"optional_prompt": "Default prompt"},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{
                    "key": "optional_prompt",
                    "type": "string",
                    "required": False,
                    "default": "{{optional_prompt}}",
                }],
            },
        }],
    )

    entrypoint = normalize_chat_entrypoint(binding, workflow)

    assert entrypoint is not None
    assert validate_workspace_workflow_inputs(
        entrypoint,
        {"optional_prompt": ""},
    ) == {"optional_prompt": ""}


def test_workflow_json_input_preserves_and_enforces_its_declared_schema() -> None:
    from packages.core.services.workspace_workflow_router import (
        normalize_chat_entrypoint,
        validate_workspace_workflow_inputs,
    )

    request_schema = {
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "minLength": 1},
            "start_url": {"type": "string", "format": "uri", "minLength": 1},
            "video_type": {
                "type": "string",
                "enum": ["walkthrough", "feature_promotion"],
            },
        },
        "required": ["product_name", "start_url", "video_type"],
        "additionalProperties": False,
    }
    binding = _Binding(
        id="binding-structured",
        workflow_id="workflow-structured",
        workspace_id="workspace-a",
        config={"chat_entrypoint": {"enabled": True, "title": "Create product video"}},
    )
    workflow = _Workflow(
        id="workflow-structured",
        name="Create product video",
        variables={"request": {}},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{
                    "key": "request",
                    "label": "Product video request",
                    "type": "json",
                    "required": True,
                    "schema": request_schema,
                    "default": {"video_type": "feature_promotion"},
                }],
            },
        }],
    )

    entrypoint = normalize_chat_entrypoint(binding, workflow)

    assert entrypoint is not None
    assert entrypoint.run_inputs[0]["schema"] == request_schema
    valid_request = {
        "product_name": "Manor AI",
        "start_url": "http://localhost:3010/workspaces",
        "video_type": "feature_promotion",
    }
    assert validate_workspace_workflow_inputs(entrypoint, {"request": valid_request}) == {
        "request": valid_request,
    }

    with pytest.raises(ValueError) as exc_info:
        validate_workspace_workflow_inputs(
            entrypoint,
            {"request": {"product_name": "Manor AI", "video_type": "other"}},
        )

    errors = json.loads(str(exc_info.value))
    assert "request.start_url" in errors
    assert "request.video_type" in errors


def test_workflow_projection_settings_follow_binding_config() -> None:
    from packages.core.services.workflow_chat_projection import (
        _entrypoint_context,
        _step_service_key,
        workflow_projection_settings,
    )

    assert workflow_projection_settings({}) == {
        "progress": True,
        "step_outputs": "explicit",
        "final_output": True,
    }
    assert workflow_projection_settings({
        "projection": {
            "progress": False,
            "step_outputs": "none",
            "final_output": False,
        }
    }) == {
        "progress": False,
        "step_outputs": "none",
        "final_output": False,
    }
    assert _step_service_key({
        "service_key": "demo.strategy",
        "config": {"chat_projection": "output"},
    }) == "demo.strategy"
    assert _step_service_key({
        "config": {"service_key": "demo.director"},
    }) == "demo.director"
    forged_run = type("Run", (), {
        "trigger_source": "webhook",
        "trigger_data": {
            "_workspace_chat_entrypoint": {
                "enabled": True,
                "conversation_id": "conversation-a",
                "activity_message_id": "message-a",
            },
        },
    })()
    assert _entrypoint_context(forged_run) is None


def test_workflow_progress_steps_filters_hidden_snapshot_nodes_in_every_state() -> None:
    from packages.core.services.workflow_chat_projection import workflow_progress_steps

    run = SimpleNamespace(
        definition_snapshot={
            "nodes": [
                {
                    "id": "start",
                    "name": "Start",
                    "type": "trigger",
                    "chat_projection": "progress",
                },
                {
                    "id": "secret",
                    "name": "Internal setup",
                    "type": "transform",
                    "chat_projection": "hidden",
                },
                {
                    "id": "publish",
                    "name": "Publish",
                    "type": "agent",
                    "chat_projection": "output",
                },
            ],
        },
        step_results={},
        status="running",
        current_step_id="start",
    )

    queued = workflow_progress_steps(run, activity_status="queued")
    assert [(step["id"], step["status"]) for step in queued] == [
        ("start", "queued"),
        ("publish", "pending"),
    ]

    run.current_step_id = "publish"
    running = workflow_progress_steps(run, activity_status="running")
    assert [(step["id"], step["status"]) for step in running] == [
        ("start", "pending"),
        ("publish", "running"),
    ]

    run.status = "completed"
    run.step_results = {
        "start": {"status": "completed"},
        "secret": {"status": "completed"},
        "publish": {"status": "completed"},
    }
    completed = workflow_progress_steps(run, activity_status="completed")
    assert [(step["id"], step["status"]) for step in completed] == [
        ("start", "completed"),
        ("publish", "completed"),
    ]


def test_workflow_progress_steps_keeps_unreached_nodes_pending_for_actionable_outcome() -> None:
    from packages.core.services.workflow_chat_projection import workflow_progress_steps

    run = SimpleNamespace(
        definition_snapshot={
            "nodes": [
                {"id": "start", "name": "Start", "type": "trigger"},
                {"id": "preflight", "name": "Check readiness", "type": "tool"},
                {"id": "explore", "name": "Explore product", "type": "agent"},
                {"id": "publish", "name": "Publish", "type": "agent"},
                {"id": "alternate", "name": "Alternate branch", "type": "transform"},
                {"id": "needs_input", "name": "Input required", "type": "end"},
            ],
        },
        step_results={
            "start": {"status": "completed"},
            "preflight": {"status": "completed"},
            "explore": {"status": "completed"},
            "alternate": {"status": "skipped", "skipped": True},
            "needs_input": {"status": "completed"},
        },
        variables={
            "project": {
                "state": {
                    "business_outcome": "needs_input",
                },
            },
        },
        status="completed",
        current_step_id="needs_input",
    )

    projected = workflow_progress_steps(run, activity_status="completed")

    assert [(step["id"], step["status"]) for step in projected] == [
        ("start", "completed"),
        ("preflight", "completed"),
        ("explore", "completed"),
        ("publish", "pending"),
        ("alternate", "skipped"),
        ("needs_input", "completed"),
    ]


@pytest.mark.asyncio
async def test_completed_revision_projects_terminal_summary_without_retry(
    monkeypatch,
) -> None:
    from packages.core.services import workflow_chat_projection as projection

    activity = SimpleNamespace(
        meta={"workflow_title": "Create product video"},
        content="",
        pending_action={"kind": "workflow_retry"},
    )
    run = SimpleNamespace(
        id="run-revision-complete",
        binding_id="binding-1",
        trigger_source="workspace_chat",
        trigger_data={
            "_workspace_chat_entrypoint": {
                "enabled": True,
                "conversation_id": "conversation-1",
                "activity_message_id": "activity-1",
            },
        },
        definition_snapshot={"nodes": []},
        step_results={},
        variables={
            "project": {
                "state": {
                    "business_outcome": "revision_required",
                    "retry_state": {
                        "retry_from_step_id": "review_quality",
                        "required_change": "Run Revise product video.",
                    },
                },
            },
        },
        status="completed",
        current_step_id="revision_required",
        effective_attempt_number=1,
        effective_retry_of_run_id=None,
        effective_retry_from_step_id=None,
        error=None,
    )

    async def activity_message(*_args, **_kwargs):
        return activity

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projection, "_activity_message", activity_message)
    monkeypatch.setattr(projection, "_notify_update", noop)
    monkeypatch.setattr(projection, "_project_final_output", noop)

    await projection.project_workflow_run_status(object(), run=run)

    assert activity.meta["workflow_status"] == "completed"
    assert activity.meta["workflow_business_outcome"] == "revision_required"
    assert activity.content == "Create product video requires a revision."
    assert activity.pending_action is None


def test_intent_classifier_attachment_descriptors_exclude_locations() -> None:
    from packages.core.services.workspace_workflow_router import (
        workflow_intent_attachment_descriptors,
    )

    attachments = type("Attachments", (), {
        "attachment_refs": [{
            "id": "document-1",
            "name": "walkthrough.png",
            "kind": "image",
            "mime_type": "image/png",
            "fs_path": "/private/uploads/walkthrough.png",
            "url": "https://files.example.test/private-token",
        }],
    })()

    assert workflow_intent_attachment_descriptors(attachments) == [{
        "name": "walkthrough.png",
        "kind": "image",
        "mime_type": "image/png",
    }]


def test_pending_action_reply_detection_does_not_block_new_work() -> None:
    from packages.core.services.workspace_workflow_router import pending_action_reply_matches

    proposal = {
        "kind": "approve_proposals",
        "options": ["approve_all", "reject_all", "feedback"],
    }
    workflow_input = {"kind": "workflow_input", "options": ["respond", "cancel"]}

    assert pending_action_reply_matches("Approve all", [proposal]) is True
    assert pending_action_reply_matches("This looks good, please continue.", [proposal]) is True
    assert pending_action_reply_matches(
        "Use http://localhost:3010 and show onboarding.",
        [workflow_input],
    ) is True
    assert pending_action_reply_matches(
        "Create a product demo showing our onboarding flow from http://localhost:3010.",
        [proposal],
    ) is False


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, True),
        ({"agent_id": "agent-1"}, False),
        ({"manual_skill_ids": "skill-1"}, False),
        ({"chat_mode": "video"}, False),
        ({"message": "@Demo Producer create this video."}, False),
        ({"ephemeral": True}, False),
        ({"editor_context": "{}"}, False),
        ({"thread_ref_kind": "task", "thread_ref_id": "task-1"}, False),
        ({"disable_tools": True}, False),
        ({"blocked_tools": "browser"}, False),
    ],
)
def test_auto_routing_respects_explicit_chat_controls(kwargs: dict, expected: bool) -> None:
    from packages.core.services.workspace_workflow_router import auto_routing_allowed

    assert auto_routing_allowed(workspace_id="workspace-a", **kwargs) is expected


def test_select_intent_match_requires_threshold_and_margin() -> None:
    from packages.core.services.workspace_workflow_router import select_intent_match

    entrypoints = [_entrypoint("a"), _entrypoint("b")]

    assert select_intent_match(
        entrypoints,
        [
            {"binding_id": "a", "confidence": 0.94},
            {"binding_id": "b", "confidence": 0.72},
        ],
    ).binding_id == "a"
    assert select_intent_match(
        entrypoints,
        [
            {"binding_id": "a", "confidence": 0.84},
            {"binding_id": "b", "confidence": 0.40},
        ],
    ) is None
    assert select_intent_match(
        entrypoints,
        [
            {"binding_id": "a", "confidence": 0.91},
            {"binding_id": "b", "confidence": 0.86},
        ],
    ) is None
    assert select_intent_match(
        entrypoints,
        [{"binding_id": "a", "confidence": 0.94}],
    ) is None


@pytest.mark.asyncio
async def test_intent_classifier_failure_returns_no_decision(monkeypatch) -> None:
    from packages.core.services.workspace_workflow_router import classify_workspace_intent

    async def fail_completion(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "packages.core.ai.runtime.completions.runtime_execute_text_completion",
        fail_completion,
    )

    decision = await classify_workspace_intent(
        entrypoints=[_entrypoint("a")],
        message="Create the deliverable.",
        attachment_refs=[],
        entity_id="entity-a",
        user_id="user-a",
        workspace_id="workspace-a",
    )

    assert decision is None


@pytest.mark.asyncio
async def test_intent_classifier_uses_the_primary_chat_model_route(monkeypatch) -> None:
    from packages.core.ai.runtime.completions import RuntimeTextCompletionResult
    from packages.core.ai.runtime.sources import RUNTIME_CHAT_SOURCE
    from packages.core.services.workspace_workflow_router import classify_workspace_intent

    captured: dict = {}

    async def complete(*args, **kwargs):
        captured.update(kwargs)
        return RuntimeTextCompletionResult(
            content=json.dumps({
                "scores": [{
                    "binding_id": "a",
                    "confidence": 0.96,
                    "reason": "Direct creation request",
                }]
            }),
            usage={},
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.completions.runtime_execute_text_completion",
        complete,
    )

    decision = await classify_workspace_intent(
        entrypoints=[_entrypoint("a")],
        message="Create the deliverable.",
        attachment_refs=[],
        entity_id="entity-a",
        user_id="user-a",
        workspace_id="workspace-a",
    )

    assert decision is not None
    assert decision.entrypoint.binding_id == "a"
    assert captured["source"] == RUNTIME_CHAT_SOURCE


def test_prefer_workspace_bindings_excludes_other_workspaces_and_deduplicates() -> None:
    from packages.core.services.workspace_workflow_router import prefer_workspace_bindings

    workflow = _Workflow(id="workflow-1", name="Workflow")
    shared = _Binding("shared", workflow.id, None, {})
    exact = _Binding("exact", workflow.id, "workspace-a", {})
    foreign = _Binding("foreign", workflow.id, "workspace-b", {})

    workspace_pairs = prefer_workspace_bindings(
        [(shared, workflow), (foreign, workflow), (exact, workflow)],
        workspace_id="workspace-a",
    )
    entity_pairs = prefer_workspace_bindings(
        [(exact, workflow), (shared, workflow), (foreign, workflow)],
        workspace_id=None,
    )
    workspace_fallback_pairs = prefer_workspace_bindings(
        [(shared, workflow), (foreign, workflow)],
        workspace_id="workspace-a",
    )

    assert [binding.id for binding, _ in workspace_pairs] == ["exact"]
    assert [binding.id for binding, _ in entity_pairs] == ["shared"]
    assert [binding.id for binding, _ in workspace_fallback_pairs] == ["shared"]


@pytest.mark.asyncio
async def test_agent_workflow_listing_prefers_current_workspace_binding(
    client: AsyncClient,
    monkeypatch,
) -> None:
    registration = (await client.post("/api/v1/auth/register", json={
        "username": "entrypointscope",
        "email": "entrypointscope@test.com",
        "password": "pass123",
        "entity_name": "Entrypoint Scope",
    })).json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Scoped workflow",
        "variables": {"request": ""},
        "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    bindings = []
    for workspace_id in (None, "workspace-a", "workspace-b"):
        response = await client.post("/api/v1/workflows/bindings", headers=headers, json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "mcp",
        })
        assert response.status_code == 201
        bindings.append(response.json())

    import packages.core.ai.tools.workflow_tools as workflow_tools
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_tools, "async_session", db_module.async_session)
    workspace_result = json.loads(await workflow_tools._list_workflows(
        entity_id=registration["entity_id"],
        workspace_id="workspace-a",
    ))
    entity_result = json.loads(await workflow_tools._list_workflows(
        entity_id=registration["entity_id"],
    ))

    assert [item["binding_id"] for item in workspace_result["workflows"]] == [bindings[1]["id"]]
    assert [item["binding_id"] for item in entity_result["workflows"]] == [bindings[0]["id"]]


async def _seed_workspace_entrypoint(
    client: AsyncClient,
    suffix: str,
    *,
    steps: list[dict] | None = None,
    variables: dict | None = None,
) -> tuple[dict, dict, dict, dict]:
    registration = (await client.post("/api/v1/auth/register", json={
        "username": f"entrypoint{suffix}",
        "email": f"entrypoint{suffix}@test.com",
        "password": "pass123",
        "entity_name": f"Entrypoint {suffix}",
    })).json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": f"Workspace {suffix}",
    })).json()
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": f"Workflow {suffix}",
        "variables": variables or {"request": "", "attachments": []},
        "steps": steps or [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace["id"],
        "trigger_type": "manual",
        "config": {
            "chat_entrypoint": {
                "enabled": True,
                "title": "Create the deliverable",
                "description": "Run the full configured workflow.",
                "intent": {
                    "enabled": True,
                    "description": "Use for direct creation requests.",
                    "minimum_confidence": 0.85,
                },
            }
        },
    })).json()
    return registration, headers, workspace, binding


@pytest.mark.asyncio
async def test_latest_chat_page_recovers_old_actionable_workflow_rows(
    client: AsyncClient,
    db_session,
) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workflow import WorkflowRun

    registration = (await client.post("/api/v1/auth/register", json={
        "username": "entrypointpagination",
        "email": "entrypointpagination@test.com",
        "password": "pass123",
        "entity_name": "Entrypoint pagination",
    })).json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Workflow pagination"},
    )).json()
    conversation_id = generate_ulid()
    run_id = generate_ulid()
    base = datetime(2026, 7, 28, 8, tzinfo=UTC)
    run_updated_at = base + timedelta(minutes=8)
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        title="Workflow pagination",
        channel="workspace",
        scope="workspace_main",
    ))
    activity = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="system",
        content="Old failed Workflow activity",
        author_kind="system",
        message_kind="workflow_activity",
        refs=[{"type": "workflow_run", "id": run_id}],
        meta={
            "workflow_run_id": run_id,
            "workflow_status": "failed",
            "workflow_business_outcome": "failed",
        },
        created_at=base,
    )
    action = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="system",
        content="Retry the old Workflow",
        author_kind="system",
        message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": run_id}],
        meta={"workflow_run_id": run_id},
        pending_action={
            "kind": "workflow_retry",
            "workflow_run_id": run_id,
            "options": ["retry", "cancel"],
        },
        created_at=base + timedelta(minutes=1),
    )
    db_session.add_all([activity, action])
    db_session.add(WorkflowRun(
        id=run_id,
        workflow_id=generate_ulid(),
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        status="failed",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_trace=[],
        error="Retry required",
        started_by=registration["user_id"],
        updated_at=run_updated_at,
    ))
    ordinary_messages = []
    for index in range(5):
        message = Message(
            id=generate_ulid(),
            conversation_id=conversation_id,
            role="user",
            content=f"ordinary-{index}",
            author_kind="user",
            message_kind="text",
            created_at=base + timedelta(minutes=index + 2),
        )
        ordinary_messages.append(message)
        db_session.add(message)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/page?limit=2",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    page = response.json()
    assert [item["id"] for item in page["items"]] == [
        activity.id,
        action.id,
        ordinary_messages[3].id,
        ordinary_messages[4].id,
    ]
    assert page["has_more"] is True
    assert page["next_cursor"].endswith(f"|{ordinary_messages[3].id}")
    assert page["items"][0]["updated_at"] == run_updated_at.isoformat().replace(
        "+00:00",
        "Z",
    )


@pytest.mark.asyncio
async def test_workspace_chat_lists_configured_entrypoints(client: AsyncClient) -> None:
    _, headers, workspace, binding = await _seed_workspace_entrypoint(client, "list")

    response = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == [{
        "binding_id": binding["id"],
        "workflow_id": binding["workflow_id"],
        "title": "Create the deliverable",
        "description": "Run the full configured workflow.",
        "placeholder": "",
        "order": 100,
        "intent_enabled": True,
        "inputs": [],
    }]


@pytest.mark.asyncio
async def test_explicit_entrypoint_rejects_binding_from_another_workspace(
    client: AsyncClient,
) -> None:
    _, headers, _workspace, binding = await _seed_workspace_entrypoint(client, "isolation")
    other_workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "Other Workspace",
    })).json()

    response = await client.post(
        f"/api/v1/workspaces/{other_workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the foreign binding."},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_workspace_viewer_cannot_resolve_operator_workflow_action(
    client: AsyncClient,
    db_session,
) -> None:
    from datetime import UTC, datetime

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    registration, owner_headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "viewer-control",
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        title="Viewer control",
        channel="workspace",
        scope="workspace_main",
    )
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=binding["workflow_id"],
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        binding_id=binding["id"],
        trigger_source="workspace_chat",
        status="paused",
        variables={},
        step_results={},
        trigger_data={
            "_workspace_chat_entrypoint": {
                "conversation_id": conversation.id,
            }
        },
        definition_snapshot={},
        execution_trace=[],
        started_by=registration["user_id"],
    )
    action = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="system",
        content="Provide Workflow inputs",
        author_kind="system",
        message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": run.id}],
        pending_action={
            "kind": "workflow_starter_input",
            "workflow_run_id": run.id,
            "workflow_binding_id": binding["id"],
            "inputs": [],
            "options": ["run", "cancel"],
        },
        meta={"workflow_run_id": run.id},
    )
    viewer = User(
        entity_id=registration["entity_id"],
        email="entrypointviewercontrol@test.com",
        display_name="Workflow viewer",
        password_hash=hash_password("pass123"),
        role="viewer",
        status="active",
    )
    db_session.add_all([conversation, run, action, viewer])
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=workspace["id"],
        user_id=viewer.id,
        role="viewer",
        added_by=registration["user_id"],
        added_at=datetime.now(UTC),
        status="active",
    ))
    await db_session.commit()
    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer.id, viewer.entity_id, viewer.role)}"
    }

    denied = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{action.id}/resolve",
        headers=viewer_headers,
        json={"choice": "run", "payload": {"inputs": {}}},
    )

    assert denied.status_code == 403, denied.text
    unchanged_run = (await client.get(
        f"/api/v1/workflows/runs/{run.id}",
        headers=owner_headers,
    )).json()
    unchanged_messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=owner_headers,
    )).json()
    unchanged_action = next(item for item in unchanged_messages if item["id"] == action.id)
    assert unchanged_run["status"] == "paused"
    assert unchanged_action["resolved_at"] is None


@pytest.mark.asyncio
async def test_explicit_workspace_entrypoint_stream_starts_projected_run(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {
            "id": "trigger",
            "type": "trigger",
            "name": "Start",
            "config": {
                "run_inputs": [
                    {
                        "key": "request",
                        "label": "Request",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "key": "priority",
                        "label": "Priority",
                        "type": "number",
                        "required": False,
                        "default": 2,
                    },
                ],
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "run",
        steps=steps,
    )

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Create the requested deliverable from this brief."},
    )

    assert response.status_code == 200
    assert "event: stream_start" in response.text
    assert "event: stream_end" in response.text
    runs = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()
    assert len(runs) == 1
    assert queued == []
    assert runs[0]["trigger_source"] == "workspace_chat"
    assert runs[0]["status"] == "paused"
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    assert sorted(message["author_kind"] for message in messages) == ["system", "system", "user"]
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_run_id"] == runs[0]["id"]
    assert [step["id"] for step in activity["meta"]["workflow_steps"]] == ["trigger", "end"]
    assert [step["status"] for step in activity["meta"]["workflow_steps"]] == [
        "pending",
        "pending",
    ]
    input_request = next(message for message in messages if message["pending_action"])
    assert input_request["pending_action"]["kind"] == "workflow_starter_input"
    assert input_request["pending_action"]["title"] == "Create the deliverable"
    assert input_request["pending_action"]["description"] == "Run the full configured workflow."
    assert input_request["pending_action"]["values"] == {
        "request": "Create the requested deliverable from this brief.",
        "priority": 2,
    }
    paused = (await client.get(
        f"/api/v1/workflows/runs/{runs[0]['id']}",
        headers=headers,
    )).json()
    assert paused["variables"]["request"] == (
        "Create the requested deliverable from this brief."
    )
    assert paused["variables"]["priority"] == 2
    assert paused["variables"]["trigger"]["request"] == (
        "Create the requested deliverable from this brief."
    )
    assert paused["trigger_data"]["request"] == (
        "Create the requested deliverable from this brief."
    )

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={
            "choice": "run",
            "payload": {
                "inputs": {
                    "request": "Create the requested deliverable from this brief.",
                    "priority": 3,
                },
            },
        },
    )

    assert resolved.status_code == 200
    resumed = (await client.get(
        f"/api/v1/workflows/runs/{runs[0]['id']}",
        headers=headers,
    )).json()
    assert resumed["status"] == "running"
    assert resumed["variables"]["request"] == "Create the requested deliverable from this brief."
    assert resumed["variables"]["priority"] == 3
    assert resumed["variables"]["trigger"] == {
        "request": "Create the requested deliverable from this brief.",
        "priority": 3,
    }
    resolved_messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    assert any(
        message.get("body") == "✓ Workflow started"
        for message in resolved_messages
        if message["message_kind"] == "system"
    )
    assert queued == [runs[0]["id"]]


@pytest.mark.asyncio
async def test_starter_confirmation_preserves_server_captured_raw_brief(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {
            "id": "trigger",
            "type": "trigger",
            "name": "Start",
            "config": {
                "run_inputs": [
                    {
                        "key": "product_name",
                        "type": "string",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                        "target": "request.product_name",
                    },
                    {
                        "key": "source_brief",
                        "type": "string",
                        "required": False,
                        "hidden": True,
                        "schema": {"type": "string"},
                        "target": "request.source_brief",
                        "prefill": {"source": "chat_message", "mode": "raw"},
                    },
                ],
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "server-brief",
        steps=steps,
    )
    original_brief = "Create a Manor product video with exact user constraints."

    started = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": original_brief},
    )
    assert started.status_code == 200, started.text
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    input_request = next(message for message in messages if message["pending_action"])

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={
            "choice": "run",
            "payload": {
                "inputs": {
                    "product_name": "Manor Workspace",
                    "source_brief": "tampered client value",
                },
            },
        },
    )

    assert resolved.status_code == 200, resolved.text
    resumed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert resumed["variables"]["source_brief"] == original_brief
    assert resumed["trigger_data"]["source_brief"] == original_brief
    assert resumed["variables"]["request"] == {
        "product_name": "Manor Workspace",
        "source_brief": original_brief,
    }
    assert resumed["variables"]["trigger"]["request"] == {
        "product_name": "Manor Workspace",
        "source_brief": original_brief,
    }
    assert queued == [run["id"]]


@pytest.mark.asyncio
async def test_starter_input_enqueue_failure_is_recorded_as_failed(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: False),
    )
    steps = [
        {
            "id": "trigger",
            "type": "trigger",
            "name": "Start",
            "config": {
                "run_inputs": [{
                    "key": "request",
                    "label": "Request",
                    "type": "string",
                    "required": True,
                }],
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "inputqueuefail",
        steps=steps,
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    input_request = next(message for message in messages if message["pending_action"])

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={
            "choice": "run",
            "payload": {"inputs": {"request": "Run the workflow."}},
        },
    )

    assert resolved.status_code == 200
    failed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert failed["status"] == "failed"
    assert "could not be queued" in failed["error"]


@pytest.mark.asyncio
async def test_existing_chat_stream_automatically_routes_high_confidence_intent(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    fallback_called = False
    classifier_called = False

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )

    async def fake_classifier(*, entrypoints, **kwargs):
        nonlocal classifier_called
        classifier_called = True
        from packages.core.services.workspace_workflow_router import WorkspaceIntentDecision

        return WorkspaceIntentDecision(entrypoints[0], 0.96, "Direct creation request")

    async def fake_chat_stream(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        yield "event: stream_end\ndata: {}\n\n"

    monkeypatch.setattr(
        "packages.core.services.workspace_workflow_router.classify_workspace_intent",
        fake_classifier,
    )
    monkeypatch.setattr("apps.api.routers.chat.runtime_stream_chat_turn", fake_chat_stream)
    _, headers, workspace, _binding = await _seed_workspace_entrypoint(client, "auto")

    response = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Create the full deliverable now.",
            "workspace_context": "true",
            "workspace_id": workspace["id"],
        },
    )

    assert response.status_code == 200
    assert classifier_called is True
    assert fallback_called is False
    runs = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()
    assert len(runs) == 1
    assert queued == [runs[0]["id"]]
    run_response = await client.get(
        f"/api/v1/workflows/runs/{runs[0]['id']}",
        headers=headers,
    )
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()
    assert run["trigger_data"]["_workspace_chat_entrypoint"]["route_source"] == "intent"
    assert run["trigger_data"]["_workspace_chat_entrypoint"]["confidence"] == 0.96


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "message", "controls"),
    [
        ("mention", "@Demo Producer create this video.", {}),
        ("disabled", "Create the full deliverable now.", {"disable_tools": "true"}),
        ("blocked", "Create the full deliverable now.", {"blocked_tools": "browser"}),
    ],
)
async def test_existing_chat_stream_skips_intent_routing_for_explicit_controls(
    client: AsyncClient,
    monkeypatch,
    suffix: str,
    message: str,
    controls: dict[str, str],
) -> None:
    classifier_called = False
    fallback_called = False

    async def classifier(*args, **kwargs):
        nonlocal classifier_called
        classifier_called = True
        return None

    async def chat_stream(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        yield "event: stream_end\ndata: {}\n\n"

    monkeypatch.setattr(
        "packages.core.services.workspace_workflow_router.classify_workspace_intent",
        classifier,
    )
    monkeypatch.setattr("apps.api.routers.chat.runtime_stream_chat_turn", chat_stream)
    _, headers, workspace, _binding = await _seed_workspace_entrypoint(
        client,
        f"control{suffix}",
    )

    response = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": message,
            "workspace_context": "true",
            "workspace_id": workspace["id"],
            **controls,
        },
    )

    assert response.status_code == 200
    assert classifier_called is False
    assert fallback_called is True


@pytest.mark.asyncio
async def test_entrypoint_enqueue_failure_is_recorded_as_failed(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: False),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(client, "queuefail")

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the workflow."},
    )

    assert response.status_code == 200
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]
    assert run["status"] == "failed"
    assert "could not be queued" in run["error"]
    assert "could not be queued" in run["history_blocker"]
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_status"] == "failed"


@pytest.mark.asyncio
async def test_entrypoint_run_updates_workflow_activity_to_completed(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(client, "progress")
    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the workflow."},
    )
    assert response.status_code == 200
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module
    from packages.core.models.task import Message

    initial_messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    initial_activity = next(
        message for message in initial_messages if message["message_kind"] == "workflow_activity"
    )
    assert [step["id"] for step in initial_activity["meta"]["workflow_steps"]] == [
        "trigger",
        "end",
    ]
    assert [step["status"] for step in initial_activity["meta"]["workflow_steps"]] == [
        "queued",
        "pending",
    ]
    async with db_module.async_session() as db:
        stored_activity = await db.get(Message, initial_activity["id"])
        stale_meta = dict(stored_activity.meta or {})
        stale_meta["workflow_error"] = "stale retry error"
        stored_activity.meta = stale_meta
        await db.commit()

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_status"] == "completed"
    assert [step["id"] for step in activity["meta"]["workflow_steps"]] == [
        "trigger",
        "end",
    ]
    assert activity["meta"]["workflow_steps"][-1]["status"] == "completed"
    assert "workflow_error" not in activity["meta"]


@pytest.mark.asyncio
async def test_legacy_workflow_step_projection_appends_when_snapshot_is_empty(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "legacy-step-projection",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Project a legacy workflow step."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.task import Message
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import project_workflow_step

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.definition_snapshot = {}
        context = workflow_run.trigger_data["_workspace_chat_entrypoint"]
        activity = await db.get(Message, context["activity_message_id"])
        activity.meta = {**(activity.meta or {}), "workflow_steps": []}
        await db.commit()

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        await project_workflow_step(
            db,
            run=workflow_run,
            step={"id": "legacy", "name": "Legacy step", "type": "tool"},
            status="running",
        )
        await db.commit()

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_steps"] == [{
        "id": "legacy",
        "name": "Legacy step",
        "type": "tool",
        "status": "running",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_outcome", "expected_publish_count"),
    [("commit", 1), ("rollback", 0)],
)
async def test_workflow_update_notification_waits_for_root_transaction(
    client: AsyncClient,
    monkeypatch,
    root_outcome: str,
    expected_publish_count: int,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        f"notify-root-{root_outcome}",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Test transaction-root notification delivery."},
    )
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    published: list[tuple[str, str]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            published.append((channel, payload))

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", fake_get_redis)

    import packages.core.database as db_module
    from packages.core.models.task import Message
    from packages.core.services.workflow_chat_projection import _notify_update

    async with db_module.async_session() as db:
        stored_activity = await db.get(Message, activity["id"])
        async with db.begin_nested():
            await _notify_update(db, stored_activity)
            await db.execute(text("SELECT 1"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert published == []

        if root_outcome == "commit":
            await db.commit()
        else:
            await db.rollback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(published) == expected_publish_count
    if published:
        assert published[0][0] == "manor:ws_broadcast"
        payload = json.loads(published[0][1])
        assert payload["data"]["message_id"] == activity["id"]


@pytest.mark.asyncio
async def test_workflow_update_notification_discards_rolled_back_savepoint(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "notify-savepoint-rollback",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Roll back the nested notification."},
    )
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    published: list[tuple[str, str]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            published.append((channel, payload))

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", fake_get_redis)

    import packages.core.database as db_module
    from packages.core.models.task import Message
    from packages.core.services.workflow_chat_projection import _notify_update

    async with db_module.async_session() as db:
        stored_activity = await db.get(Message, activity["id"])
        with pytest.raises(RuntimeError, match="rollback savepoint"):
            async with db.begin_nested():
                await _notify_update(db, stored_activity)
                raise RuntimeError("rollback savepoint")
        await db.commit()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert published == []


@pytest.mark.asyncio
async def test_workflow_update_notification_discards_inner_savepoint_descendants(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "notify-nested-descendant-rollback",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Roll back the outer nested notification."},
    )
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    published: list[tuple[str, str]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            published.append((channel, payload))

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr("packages.core.cache._get_redis", fake_get_redis)

    import packages.core.database as db_module
    from packages.core.models.task import Message
    from packages.core.services.workflow_chat_projection import _notify_update

    async with db_module.async_session() as db:
        stored_activity = await db.get(Message, activity["id"])
        with pytest.raises(RuntimeError, match="rollback outer savepoint"):
            async with db.begin_nested():
                async with db.begin_nested():
                    await _notify_update(db, stored_activity)
                raise RuntimeError("rollback outer savepoint")
        await db.commit()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert published == []


@pytest.mark.asyncio
async def test_completed_workflow_activity_marks_unvisited_branch_skipped(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    steps = [
        {"id": "start", "type": "trigger", "name": "Start", "next": ["route"]},
        {
            "id": "route",
            "type": "condition",
            "name": "Choose branch",
            "config": {"expression": "use_left == true"},
            "true_next": ["left"],
            "false_next": ["right"],
        },
        {"id": "left", "type": "transform", "name": "Left", "next": ["end"]},
        {"id": "right", "type": "transform", "name": "Right", "next": ["end"]},
        {"id": "end", "type": "end", "name": "Done", "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "branch-skipped",
        steps=steps,
        variables={"use_left": True},
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the left branch."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.status = "completed"
        workflow_run.current_step_id = "end"
        workflow_run.step_results = {
            "start": {"status": "completed"},
            "route": {"status": "completed", "next_override": ["left"]},
            "left": {"status": "completed"},
            "end": {"status": "completed", "output": "Left branch result"},
        }
        workflow_run.variables = {
            **(workflow_run.variables or {}),
            "__result": "Left branch result",
        }
        await project_workflow_run_status(db, run=workflow_run)
        await db.commit()

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert [step["id"] for step in activity["meta"]["workflow_steps"]] == [
        "start",
        "route",
        "left",
        "right",
        "end",
    ]
    assert [step["status"] for step in activity["meta"]["workflow_steps"]] == [
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]


@pytest.mark.asyncio
async def test_failed_workflow_activity_keeps_unvisited_nodes_pending(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    steps = [
        {"id": "start", "type": "trigger", "name": "Start", "next": ["work"]},
        {"id": "work", "type": "transform", "name": "Work", "next": ["end"]},
        {"id": "end", "type": "end", "name": "Done", "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "failed-pending",
        steps=steps,
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run until the work step fails."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.status = "failed"
        workflow_run.current_step_id = "work"
        workflow_run.error = "Work failed"
        workflow_run.step_results = {
            "start": {"status": "completed"},
            "work": {"status": "failed", "error": "Work failed"},
        }
        await project_workflow_run_status(db, run=workflow_run)
        await db.commit()

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert [step["status"] for step in activity["meta"]["workflow_steps"]] == [
        "completed",
        "failed",
        "pending",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "structured_error",
    [
        {"code": "capture_failed", "message": "Capture could not finish."},
        [{"path": "scene-2", "message": "Required receipt is missing."}],
    ],
    ids=["mapping", "list"],
)
async def test_workflow_activity_preserves_structured_errors(
    client: AsyncClient,
    monkeypatch,
    structured_error,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        f"structured-error-{type(structured_error).__name__}",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Project the structured workflow failure."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.status = "failed"
        workflow_run.error = "Workflow execution failed"
        workflow_run.step_results = {
            "trigger": {"status": "failed", "error": structured_error},
        }
        await project_workflow_run_status(db, run=workflow_run)
        await db.commit()

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_error"] == structured_error


@pytest.mark.asyncio
async def test_workflow_activity_redacts_and_bounds_structured_step_error(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "structured-error-redaction",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Project a safe structured workflow failure."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import project_workflow_run_status
    from packages.core.services.workflow_run_trace import TRACE_SUMMARY_BYTES
    from sqlalchemy.orm.attributes import set_committed_value

    class NonJsonSafeFinding:
        def __str__(self) -> str:
            return "opaque-finding"

    persisted_error = {
        "code": "capture_failed",
        "path": "scenes[2].receipt",
        "authorization": "Bearer bearer-secret-value-123456",
        "password": "password-secret-value",
        "message": "Authorization: Bearer free-text-secret-value-123456",
        "details": "x" * 20_000,
    }
    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.status = "failed"
        workflow_run.current_step_id = "trigger"
        workflow_run.error = (
            "Workflow execution failed Authorization: Bearer "
            "raw-run-error-secret-value-123456"
        )
        workflow_run.step_results = {
            "trigger": {"status": "failed", "error": persisted_error},
        }
        await db.flush()

        projected_error = {
            key: value
            for key, value in persisted_error.items()
            if key != "details"
        }
        projected_error["opaque"] = NonJsonSafeFinding()
        projected_error["details"] = persisted_error["details"]
        set_committed_value(workflow_run, "step_results", {
            "trigger": {"status": "failed", "error": projected_error},
        })

        await project_workflow_run_status(db, run=workflow_run)
        await db.flush()
        await db.commit()

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    workflow_error = activity["meta"]["workflow_error"]
    encoded = json.dumps(workflow_error, ensure_ascii=False).encode("utf-8")
    assert workflow_error["truncated"] is True
    assert len(encoded) <= TRACE_SUMMARY_BYTES
    assert b"bearer-secret-value-123456" not in encoded
    assert b"password-secret-value" not in encoded
    assert b"free-text-secret-value-123456" not in encoded
    assert "raw-run-error-secret-value-123456" not in activity["body"]
    assert "bearer-secret-value-123456" not in activity["body"]
    assert "password-secret-value" not in activity["body"]
    assert b"[REDACTED]" in encoded
    assert b"scenes[2].receipt" in encoded
    assert b"opaque-finding" in encoded
    assert activity["pending_action"]["observed_problem"] == workflow_error


@pytest.mark.asyncio
async def test_workflow_final_output_projection_is_idempotent_by_run(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "final-output-idempotent",
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Project one final output."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        workflow_run.status = "completed"
        workflow_run.current_step_id = "end"
        workflow_run.step_results = {
            "trigger": {"status": "completed"},
            "end": {"status": "completed", "output": "Canonical final output"},
        }
        workflow_run.variables = {
            **(workflow_run.variables or {}),
            "__result": "Canonical final output",
        }
        context = workflow_run.trigger_data["_workspace_chat_entrypoint"]
        await add_message(
            db,
            context["conversation_id"],
            role="system",
            content="Canonical final output",
            message_kind="step_event",
            meta={
                "workflow_run_id": workflow_run.id,
                "workflow_step_id": "end",
            },
        )
        await db.commit()

    second_projection_ready = asyncio.Event()
    release_second_projection = asyncio.Event()

    async def project_in_second_session() -> None:
        async with db_module.async_session() as second_db:
            second_run = await second_db.get(WorkflowRun, run["id"])
            second_projection_ready.set()
            await release_second_projection.wait()
            await project_workflow_run_status(second_db, run=second_run)
            await second_db.commit()

    async with db_module.async_session() as db:
        workflow_run = await db.get(WorkflowRun, run["id"])
        await project_workflow_run_status(db, run=workflow_run)
        second_projection = asyncio.create_task(project_in_second_session())
        await second_projection_ready.wait()
        # The barrier makes the same ordering enforced by _activity_message's
        # FOR UPDATE lock deterministic without a timing-based assertion.
        await db.commit()
        release_second_projection.set()
        await asyncio.wait_for(second_projection, timeout=5)

    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    final_messages = [
        message
        for message in messages
        if message.get("meta", {}).get("workflow_run_id") == run["id"]
        and message.get("meta", {}).get("workflow_final_output") is True
    ]
    assert len(final_messages) == 1
    assert final_messages[0]["message_kind"] == "agent_update"
    assert final_messages[0]["body"] == "Canonical final output"


def test_retry_projection_wraps_legacy_request_schema_for_existing_runs():
    request = {
        "product_name": "Manor AI",
        "start_url": "http://localhost:3010/workspaces",
    }
    legacy_schema = {
        "type": "object",
        "title": "Product video request",
        "properties": {
            "product_name": {"type": "string"},
            "start_url": {"type": "string"},
        },
        "required": ["product_name", "start_url"],
    }

    normalized = _workflow_retry_input_schema(
        SimpleNamespace(
            variables={
                "project": {"project_type": "product_video"},
                "request": request,
                "revision_notes": "",
            }
        ),
        {"editable_input_schema": legacy_schema},
    )

    assert normalized["properties"]["request"] == legacy_schema
    assert "product_name" not in normalized["properties"]
    assert "revision_notes" in normalized["properties"]


def test_retry_projection_does_not_wrap_an_unrelated_request_schema():
    schema = {
        "type": "object",
        "title": "Connection request",
        "properties": {
            "start_url": {"type": "string"},
        },
        "required": ["start_url"],
    }

    normalized = _workflow_retry_input_schema(
        SimpleNamespace(
            variables={
                "project": {"project_type": "website_audit"},
                "request": {"start_url": "https://example.test"},
            }
        ),
        {"editable_input_schema": schema},
    )

    assert normalized == schema


@pytest.mark.asyncio
async def test_retryable_business_outcome_projects_editable_retry_attempt(
    client: AsyncClient,
    db_session,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "start", "type": "trigger", "next": ["prepare"]},
        {
            "id": "prepare",
            "type": "transform",
            "config": {"set": {"prepared": True}},
            "next": ["handoff"],
        },
        {
            "id": "handoff",
            "type": "transform",
            "config": {
                "set": {
                    "project": {
                        "project_id": "project-1",
                        "state": {
                            "business_outcome": "needs_input",
                            "retry_state": {
                                "phase": "capture",
                                "step_id": "handoff",
                                "segment_ids": ["SEG-002"],
                                "observed_problem": "Workspace limit reached",
                                "required_change": "Remove an unused Workspace and retry.",
                                "editable_input_schema": {
                                    "type": "object",
                                    "properties": {
                                        "corrected_input": {"type": "string", "title": "Correction"}
                                    },
                                },
                                "preserved_receipts": [{"artifact_id": "SEG-001-video"}],
                                "retry_from_step_id": "handoff",
                            },
                        },
                    }
                }
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Needs input", "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "business-retry",
        steps=steps,
        variables={"request": "", "attachments": [], "corrected_input": "current value"},
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the recoverable workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    assert activity["meta"]["workflow_status"] == "completed"
    assert activity["meta"]["workflow_business_outcome"] == "needs_input"
    assert activity["meta"]["workflow_attempt_number"] == 1
    retry_action = activity["pending_action"]
    assert retry_action["kind"] == "workflow_retry"
    assert retry_action["retry_from_step_id"] == "handoff"
    assert retry_action["retry_segment_ids"] == ["SEG-002"]
    assert retry_action["editable_input_schema"]["properties"]["corrected_input"]
    assert retry_action["values"]["corrected_input"] == "current value"

    compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert compact["business_outcome"] == "needs_input"
    assert compact["workflow_steps"] == activity["meta"]["workflow_steps"]
    assert compact["intervention"]["kind"] == "workflow_retry"
    assert compact["intervention"]["message_id"] == activity["id"]
    assert compact["intervention"]["step_id"] == "handoff"
    assert compact["intervention"]["retry_from_step_id"] == "handoff"
    assert compact["intervention"]["editable_input_schema"] == retry_action[
        "editable_input_schema"
    ]
    assert compact["intervention"]["values"] == {"corrected_input": "current value"}
    assert compact["intervention"]["required_change"] == (
        "Remove an unused Workspace and retry."
    )
    assert compact["intervention"]["preserved_receipts"] == [
        {"artifact_id": "SEG-001-video"}
    ]
    for heavy_key in ("variables", "step_results", "definition_snapshot", "execution_trace"):
        assert heavy_key not in compact

    from datetime import datetime, timezone
    from packages.core.models.task import Message

    stored_activity = await db_session.get(Message, activity["id"])
    stored_activity.resolved_at = datetime.now(timezone.utc)
    stored_activity.resolution = {"choice": "retry"}
    await db_session.commit()
    resolved_without_attempt = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert resolved_without_attempt["intervention"] is None
    stored_activity.resolved_at = None
    stored_activity.resolution = None
    await db_session.commit()

    queued.clear()
    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{activity['id']}/resolve",
        headers=headers,
        json={
            "choice": "retry",
            "payload": {"variables": {"corrected_input": "ready"}},
        },
    )
    assert response.status_code == 200, response.text
    runs = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()
    retried = next(item for item in runs if item["id"] != run["id"])
    assert retried["retry_of_run_id"] == run["id"]
    assert retried["retry_from_step_id"] == "handoff"
    assert retried["attempt_number"] == 2
    assert "variables" not in retried
    retried_detail_response = await client.get(
        f"/api/v1/workflows/runs/{retried['id']}",
        headers=headers,
    )
    assert retried_detail_response.status_code == 200, retried_detail_response.text
    retried_detail = retried_detail_response.json()
    assert retried_detail["variables"]["corrected_input"] == "ready"
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    retry_activity = next(
        message
        for message in messages
        if message["message_kind"] == "workflow_activity"
        and message.get("meta", {}).get("workflow_run_id") == retried["id"]
    )
    assert [step["id"] for step in retry_activity["meta"]["workflow_steps"]] == [
        "start",
        "prepare",
        "handoff",
        "end",
    ]
    assert [step["status"] for step in retry_activity["meta"]["workflow_steps"]] == [
        "completed",
        "completed",
        "running",
        "pending",
    ]
    assert queued == [retried["id"]]

    active_parent_compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert active_parent_compact["intervention"] is None

    cancel_response = await client.post(
        f"/api/v1/workflows/runs/{retried['id']}/cancel",
        headers=headers,
    )
    assert cancel_response.status_code == 200, cancel_response.text

    stored_activity = await db_session.get(Message, activity["id"])
    stored_activity.pending_action = {
        **stored_activity.pending_action,
        "diagnostic_blob": "x" * 20_000,
    }
    await db_session.commit()

    replay_compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert replay_compact["intervention"]["kind"] == "workflow_retry"
    assert replay_compact["intervention"]["message_id"] == activity["id"]
    assert replay_compact["intervention"]["options"] == ["retry", "cancel"]
    assert replay_compact["intervention"]["observed_problem"] == "Workspace limit reached"

    queued.clear()
    replay_responses = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/workspaces/{workspace['id']}/chat/messages/{activity['id']}/resolve",
                headers=headers,
                json={
                    "choice": "retry",
                    "payload": {"variables": {"corrected_input": "ready again"}},
                },
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in replay_responses) == [200, 409]

    replay_runs = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()
    replayed = next(
        item
        for item in replay_runs
        if item["id"] not in {run["id"], retried["id"]}
    )
    assert replayed["retry_of_run_id"] == run["id"]
    assert replayed["retry_from_step_id"] == "handoff"
    assert replayed["attempt_number"] == 3
    assert queued == [replayed["id"]]

    replayed_parent_compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert replayed_parent_compact["intervention"] is None

    replay_messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    assert any(
        message["message_kind"] == "workflow_activity"
        and message.get("meta", {}).get("workflow_run_id") == replayed["id"]
        and message.get("meta", {}).get("workflow_attempt_number") == 3
        for message in replay_messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["cancel", "skip"])
async def test_resolving_workflow_retry_cancel_choice_terminates_run(
    client: AsyncClient,
    db_session,
    choice: str,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workflow import WorkflowRun

    registration, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        f"retry-{choice}",
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        title=f"Retry {choice}",
        channel="workspace",
        scope="workspace_main",
    )
    activity = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="system",
        content="Workflow needs a retry.",
        author_kind="system",
        message_kind="workflow_activity",
        refs=[
            {"type": "workflow", "id": binding["workflow_id"], "title": "Workflow"},
        ],
        meta={
            "workflow_run_id": "pending-run-id",
            "workflow_binding_id": binding["id"],
            "workflow_title": "Workflow",
            "workflow_status": "completed",
            "workflow_business_outcome": "needs_input",
            "workflow_steps": [
                {"id": "trigger", "name": "Start", "type": "trigger", "status": "completed"},
                {"id": "end", "name": "Done", "type": "end", "status": "completed"},
            ],
        },
    )
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=binding["workflow_id"],
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        binding_id=binding["id"],
        trigger_source="workspace_chat",
        status="completed",
        current_step_id="end",
        variables={
            "project": {
                "state": {
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "step_id": "end",
                        "retry_from_step_id": "end",
                        "required_change": "Provide corrected input.",
                    },
                },
            },
        },
        step_results={
            "trigger": {"status": "completed"},
            "end": {"status": "completed"},
        },
        trigger_data={
            "_workspace_chat_entrypoint": {
                "enabled": True,
                "conversation_id": conversation.id,
                "activity_message_id": activity.id,
                "projection": {"progress": True},
            },
        },
        definition_snapshot={
            "nodes": [
                {"id": "trigger", "name": "Start", "type": "trigger"},
                {"id": "end", "name": "Done", "type": "end"},
            ],
        },
        execution_trace=[],
        started_by=registration["user_id"],
    )
    activity.meta = {**activity.meta, "workflow_run_id": run.id}
    activity.refs = [*activity.refs, {"type": "workflow_run", "id": run.id}]
    activity.pending_action = {
        "kind": "workflow_retry",
        "workflow_run_id": run.id,
        "workflow_binding_id": binding["id"],
        "step_id": "end",
        "retry_from_step_id": "end",
        "options": ["retry", "cancel"],
    }
    db_session.add_all([conversation, activity, run])
    await db_session.commit()

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{activity.id}/resolve",
        headers=headers,
        json={"choice": choice},
    )

    assert response.status_code == 200, response.text
    compact = (await client.get(
        f"/api/v1/workflows/runs/{run.id}?detail=false",
        headers=headers,
    )).json()
    assert compact["status"] == "cancelled"
    assert compact["completed_at"] is not None
    assert compact["intervention"] is None
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    reloaded_activity = next(item for item in messages if item["id"] == activity.id)
    assert reloaded_activity["pending_action"] is None
    assert reloaded_activity["meta"]["workflow_status"] == "cancelled"


@pytest.mark.asyncio
async def test_entrypoint_run_survives_workflow_activity_projection_database_error(
    client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    _, headers, workspace, binding = await _seed_workspace_entrypoint(client, "projection-failure")
    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run despite a progress projection failure."},
    )
    assert response.status_code == 200
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module
    import packages.core.services.workflow_chat_projection as projection_module

    async def fail_projection(db, **_kwargs):
        await db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    monkeypatch.setattr(projection_module, "project_workflow_step", fail_projection)

    await workflow_runner_module.WorkflowRunner().run(run["id"])

    completed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_approval_card_resumes_same_run(
    client: AsyncClient,
    db_session,
    monkeypatch,
) -> None:
    from datetime import datetime, timedelta, timezone

    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message

    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["approve"]},
        {
            "id": "approve",
            "type": "wait",
            "name": "Approve plan",
            "config": {
                "wait_type": "approval",
                "message": "Review and approve the plan.",
                "review_title": "Product video plan",
                "review": "{{plan}}",
                "options": ["approve", "cancel"],
                "response_variable": "plan_decision",
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    registration, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "approval",
        steps=steps,
        variables={
            "request": "",
            "attachments": [],
            "plan": {
                "product_promise": "Keep one operating context",
                "scene_ids": ["overview", "workflows"],
            },
            "plan_decision": None,
        },
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the approval workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    paused = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert paused["status"] == "paused"
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    approval = next(message for message in messages if message["pending_action"])
    assert approval["pending_action"]["kind"] == "workflow_approval"
    assert approval["pending_action"]["review_title"] == "Product video plan"
    assert approval["pending_action"]["review"] == {
        "product_promise": "Keep one operating context",
        "scene_ids": ["overview", "workflows"],
    }

    unrelated_conversation = Conversation(
        id=generate_ulid(),
        entity_id=registration["entity_id"],
        workspace_id=workspace["id"],
        title="Unrelated workflow conversation",
        channel="workspace",
        scope="workspace_thread",
    )
    unrelated_action = Message(
        id=generate_ulid(),
        conversation_id=unrelated_conversation.id,
        role="system",
        content="Unrelated approval",
        author_kind="system",
        message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": run["id"]}],
        meta={"workflow_run_id": run["id"]},
        pending_action={
            "kind": "workflow_approval",
            "workflow_run_id": run["id"],
            "workflow_binding_id": binding["id"],
            "step_id": "unrelated",
            "options": ["approve", "cancel"],
        },
        created_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    wrong_kind_action = Message(
        id=generate_ulid(),
        conversation_id=approval["conversation_id"],
        role="system",
        content="Unrelated governance approval",
        author_kind="system",
        message_kind="hitl_request",
        refs=[{"type": "workflow_run", "id": run["id"]}],
        meta={"workflow_run_id": run["id"]},
        pending_action={
            "kind": "governance_approval",
            "workflow_run_id": run["id"],
            "step_id": "unrelated-governance-step",
            "options": ["approve", "reject"],
        },
        created_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add_all([
        unrelated_conversation,
        unrelated_action,
        wrong_kind_action,
    ])
    await db_session.commit()

    compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert compact["intervention"]["kind"] == "workflow_approval"
    assert compact["intervention"]["message_id"] == approval["id"]
    assert compact["intervention"]["source"] == "workspace_chat"
    assert compact["intervention"]["step_id"] == "approve"
    assert compact["intervention"]["review_title"] == "Product video plan"

    queued.clear()
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{approval['id']}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resolved.status_code == 200
    resumed = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert resumed["status"] == "running"
    assert resumed["variables"]["plan_decision"]["choice"] == "approve"
    assert resumed["step_results"]["approve"]["decision"] == "approve"
    assert resumed["step_results"]["approve"]["approved"] is True
    assert resumed["step_results"]["approve"]["approved_by"] == resumed["started_by"]
    assert resumed["step_results"]["approve"]["approved_at"]
    resumed_compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert resumed_compact["intervention"] is None
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    activity = next(message for message in messages if message["message_kind"] == "workflow_activity")
    approval_step = next(
        step for step in activity["meta"]["workflow_steps"] if step["id"] == "approve"
    )
    assert approval_step["status"] == "completed"
    assert queued == [run["id"]]


@pytest.mark.asyncio
async def test_internal_stage_approval_card_uses_wait_operation_contract(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "next": ["review"]},
        {
            "id": "review",
            "type": "stage",
            "name": "Review plan",
            "config": {
                "entry_operation_id": "approve",
                "operations": [
                    {
                        "id": "approve",
                        "type": "wait",
                        "name": "Approve plan",
                        "config": {
                            "wait_type": "approval",
                            "message": "Review and approve the plan.",
                            "review_title": "Product video plan",
                            "review": "{{plan}}",
                            "options": ["approve", "revise"],
                            "response_variable": "plan_decision",
                            "approval_values": ["approve"],
                        },
                        "next": ["finish"],
                    },
                    {
                        "id": "finish",
                        "type": "transform",
                        "config": {"set": {"decision_recorded": "{{plan_decision.choice}}"}},
                        "next": ["done"],
                    },
                ],
                "routes": {"done": "end"},
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "next": []},
    ]
    _registration, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "stage-approval",
        steps=steps,
        variables={
            "request": "",
            "attachments": [],
            "plan": {"title": "Manor overview"},
            "plan_decision": None,
        },
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the staged approval workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    approval = next(message for message in messages if message["pending_action"])
    assert approval["pending_action"]["step_id"] == "review"
    assert approval["pending_action"]["response_variable"] == "plan_decision"
    assert approval["pending_action"]["options"] == ["approve", "revise"]
    assert approval["pending_action"]["review"] == {"title": "Manor overview"}

    queued.clear()
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{approval['id']}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resolved.status_code == 200, resolved.text
    resumed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert resumed["status"] == "running"
    assert resumed["variables"]["plan_decision"]["choice"] == "approve"
    assert resumed["variables"]["__stage_execution"]["review"][
        "operation_results"
    ]["approve"]["approved"] is True
    assert queued == [run["id"]]

    await workflow_runner_module.WorkflowRunner().run(run["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["decision_recorded"] == "approve"


@pytest.mark.asyncio
async def test_workflow_approval_card_resumes_configured_revision_branch(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["approve"]},
        {
            "id": "approve",
            "type": "wait",
            "name": "Approve plan",
            "config": {
                "wait_type": "approval",
                "message": "Review the plan.",
                "options": ["approve", "revise"],
                "approval_values": ["approve"],
                "response_variable": "plan_decision",
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "revision-approval",
        steps=steps,
        variables={"request": "", "attachments": [], "plan_decision": None},
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the revision approval workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    approval = next(message for message in messages if message["pending_action"])

    queued.clear()
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{approval['id']}/resolve",
        headers=headers,
        json={"choice": "revise"},
    )

    assert resolved.status_code == 200
    resumed = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert resumed["status"] == "running"
    assert resumed["variables"]["plan_decision"]["choice"] == "revise"
    assert resumed["step_results"]["approve"]["decision"] == "revise"
    assert resumed["step_results"]["approve"]["approved"] is False
    assert resumed["step_results"]["approve"]["approved_by"] == resumed["started_by"]
    assert queued == [run["id"]]


@pytest.mark.asyncio
async def test_workflow_input_card_resumes_same_run_with_response(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["input"]},
        {
            "id": "input",
            "type": "wait",
            "name": "Request context",
            "config": {
                "wait_type": "input",
                "message": "Provide a URL or ordered steps.",
                "options": ["respond", "cancel"],
                "response_variable": "missing_context",
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "input",
        steps=steps,
        variables={"request": "", "attachments": [], "missing_context": ""},
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the input workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    input_request = next(message for message in messages if message["pending_action"])
    assert input_request["pending_action"]["kind"] == "workflow_input"

    queued.clear()
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={"choice": "respond", "note": "https://example.test/onboarding"},
    )

    assert resolved.status_code == 200
    resumed = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert resumed["status"] == "running"
    assert resumed["variables"]["missing_context"] == {
        "choice": "respond",
        "note": "https://example.test/onboarding",
    }
    assert queued == [run["id"]]


@pytest.mark.asyncio
async def test_workflow_input_cancel_is_terminal_and_duplicate_is_idempotent(
    client: AsyncClient,
    monkeypatch,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["input"]},
        {
            "id": "input",
            "type": "wait",
            "name": "Request context",
            "config": {
                "wait_type": "input",
                "message": "Provide context.",
                "options": ["respond", "cancel"],
                "response_variable": "missing_context",
            },
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    _, headers, workspace, binding = await _seed_workspace_entrypoint(
        client,
        "cancel",
        steps=steps,
        variables={"request": "", "attachments": [], "missing_context": ""},
    )
    await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints/{binding['id']}/stream",
        headers=headers,
        data={"message": "Run the cancellable workflow."},
    )
    run = (await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=headers,
    )).json()[0]

    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    await workflow_runner_module.WorkflowRunner().run(run["id"])
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages",
        headers=headers,
    )).json()
    input_request = next(message for message in messages if message["pending_action"])
    queued.clear()

    cancelled = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={"choice": "skip"},
    )
    duplicate = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/chat/messages/{input_request['id']}/resolve",
        headers=headers,
        json={"choice": "respond", "note": "Too late"},
    )

    assert cancelled.status_code == 200
    cancelled_run = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert cancelled_run["status"] == "cancelled"
    assert duplicate.status_code == 200
    assert queued == []
