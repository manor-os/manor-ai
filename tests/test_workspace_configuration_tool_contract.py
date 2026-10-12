"""Agent configuration must reach a complete, atomic Workspace operation."""

from contextlib import asynccontextmanager
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.workspace_operation_actions import (
    _normalise_workspace_operation_patches,
    runtime_workspace_operation_action,
)
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA, manor_action_manifests
from packages.core.ai.tools.workspace_agent_tools import (
    WORKSPACE_AGENT_SCHEMA,
    WORKSPACE_OPERATION_SCHEMA,
)
from packages.core.services.workspace_operation_service import _apply_single_patch


@pytest.mark.parametrize(
    "description",
    [
        MANOR_SCHEMA["function"]["parameters"]["properties"]["params"]["description"],
        WORKSPACE_AGENT_SCHEMA["function"]["parameters"]["properties"]["params"]["description"],
        WORKSPACE_OPERATION_SCHEMA["function"]["parameters"]["properties"]["patches"]["description"],
        next(
            action["description"]
            for action in manor_action_manifests()
            if action["invoke_with"]["arguments"]["action"] == "workspace"
        ),
    ],
    ids=["manor", "workspace-agent", "workspace-operation", "manor-catalog"],
)
def test_workspace_entrypoints_explain_complete_additive_agent_configuration(description):
    # Staging accepted services={marketing_video:{agent:...,skills:[...]}} as
    # an empty replacement list. The ordinary Manor gateway must expose the
    # real payload contract even when the narrow Workspace tool is not loaded.
    for required in (
        "replaces that entire collection",
        "arrays of objects",
        "process metadata only",
        "not live ScheduledJob schedules",
        "create_scheduled_job",
        "new service_key",
        "agent_mapping.upsert",
        "strategy:'create_custom'",
        "create_agent_draft:{agent_name,system_prompt",
        "skill_binding.upsert",
        "owner_service_key",
        "get_current",
        "services-only diff does not add an Agent",
    ):
        assert required in description


def test_manor_description_does_not_route_automation_to_workspace_operations():
    description = MANOR_SCHEMA["function"]["description"]
    assert "use configuration drafts" in description
    assert "Use dedicated actions for ScheduledJobs/Agent Initiatives, never action='workspace'" in description


def test_documented_agent_and_skill_patch_shapes_preserve_existing_configuration():
    existing_services = [{"key": key, "name": key.title()} for key in ("research", "leads", "content")]
    existing_mappings = [{"service_key": row["key"], "agent_id": row["key"]} for row in existing_services]
    state = {
        "operating_model": {},
        "services": deepcopy(existing_services),
        "agent_mappings": deepcopy(existing_mappings),
        "skill_bindings": [],
        "capability_bindings": [],
    }
    patches = [
        {"op": "service_role.upsert", "payload": {"service": {"key": "marketing_video", "name": "Marketing Video"}}},
        {"op": "agent_mapping.upsert", "payload": {"mapping": {"service_key": "marketing_video", "agent_id": "video-agent"}}},
        {"op": "skill_binding.upsert", "payload": {"binding": {"owner_scope": "service", "owner_service_key": "marketing_video", "skill_id": "video-skill"}}},
        {"op": "capability_binding.upsert", "payload": {"binding": {"owner_scope": "service", "owner_service_key": "marketing_video", "capability_type": "tool", "tool_name": "invoke_skill"}}},
    ]
    for patch in patches:
        state = _apply_single_patch(state, patch)

    assert state["services"][:3] == existing_services
    assert state["agent_mappings"][:3] == existing_mappings
    assert len(state["services"]) == len(state["agent_mappings"]) == 4
    assert state["agent_mappings"][-1]["agent_id"] == "video-agent"
    assert state["skill_bindings"][0]["skill_id"] == "video-skill"
    assert state["skill_bindings"][0]["owner_service_key"] == "marketing_video"
    assert state["capability_bindings"][0]["tool_name"] == "invoke_skill"


@pytest.mark.parametrize("invalid_item", [None, "add a video Agent", ["skill"], 7])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("parameter_name", ["patches", "operations", "ops"])
@pytest.mark.asyncio
async def test_malformed_patch_batch_never_partially_creates_a_draft(monkeypatch, invalid_item, nested, parameter_name):
    create = AsyncMock(side_effect=AssertionError("a malformed patch batch must not create a draft"))
    monkeypatch.setattr("packages.core.services.workspace_operation_service.create_operation_draft", create)

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr("packages.core.database.async_session", session)
    patches = [
        {"op": "service_role.upsert", "payload": {"service": {"key": "video"}}},
        invalid_item,
    ]
    raw = await runtime_workspace_operation_action(
        entity_id="entity",
        workspace_id="workspace",
        user_id="user",
        params={
            "action": "create_draft",
            parameter_name: {"patches": patches} if nested else patches,
        },
    )
    payload = json.loads(raw)
    assert "patches must be an array" in payload["error"]
    assert "draft_id" not in payload
    assert "__hitl__" not in payload
    create.assert_not_awaited()


def test_nested_valid_batch_preserves_every_patch():
    patches = [
        {"op": "service_role.upsert", "payload": {"service": {"key": "video"}}},
        {"op": "agent_mapping.upsert", "payload": {"mapping": {"service_key": "video", "agent_id": "agent"}}},
    ]
    assert _normalise_workspace_operation_patches({"patches": patches}) == patches


def test_workspace_chat_routes_app_changes_to_live_catalog_before_knowledge_search():
    from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
    from packages.core.ai.runtime.prompt_guidance import runtime_workspace_agent_mode_guidance

    envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
        surface=ChatSurface.WORKSPACE_CHAT, entity_id="entity", user_id="user", workspace_id="workspace",
    ))
    guidance = runtime_workspace_agent_mode_guidance(envelope=envelope, workspace_id="workspace")

    assert "App configuration" in guidance
    assert "'action':'operation', 'params':{'action':'get_current'}" in guidance
    assert "empty search results do not prove" in guidance
    for catalog in ("available_actions", "workflow_options", "agent_options", "automation_options", "attachable_workflow_options"):
        assert catalog in guidance
    assert "app_view.update" in guidance
    assert "workflow_binding.attach" in guidance
    assert "apply only after approval" in guidance
    assert "configuring the App does not run its Flow or Automation" in guidance


def test_eager_manor_app_guidance_matches_all_live_target_types():
    guidance = MANOR_SCHEMA["function"]["parameters"]["properties"]["params"]["description"]

    assert "first call get_current before Workspace/Knowledge search" in guidance
    assert "empty search hits do not prove" in guidance
    for target in ("agent:<id>", "agent:default", "workflow:<binding_id>", "automation:<job_id>"):
        assert target in guidance
    assert "automation_options" in guidance
    assert "action_key:<optional existing App action key>" in guidance
    assert "binding only on approved apply and never runs the Workflow" in guidance
