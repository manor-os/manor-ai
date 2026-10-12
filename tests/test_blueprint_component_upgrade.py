"""Component v3 upgrades preserve Workspace identity and local edit boundaries."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.blueprints import component_upgrade as components
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.skill import AgentSkillBinding
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.workflow import WorkflowDefinition, WorkflowBinding
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, ToolDefinition, AgentToolBinding


def _payload():
    return {
        "manifest": {"slug": "component-test", "blueprint_version": "2.0"},
        "contract": {"requires": {}},
        "embedded": {
            "skills": [{"slug": "writer", "system_prompt": "Write carefully.", "config": {"format": "short"}}],
            "agents": [{"slug": "author", "system_prompt": "Use the writing Skill.", "skill_bindings": ["writer"]}],
        },
        "recipe": {"workflows": [{"slug": "publish", "name": "Publish draft", "steps": [{"id": "start", "type": "trigger", "config": {}, "next": []}]}]},
    }


async def _workspace(db):
    row = Workspace(entity_id=generate_ulid(), name="Original Workspace", settings={"_blueprint": {"blueprint_id": "builtin:component-test", "blueprint_slug": "component-test"}})
    db.add(row)
    await db.flush()
    return row


async def _install(db, workspace, payload):
    plan = await components.plan_components(db, workspace=workspace, payload=payload)
    return await components.apply_components(db, workspace=workspace, payload=payload, plan=plan)


def test_portable_config_replacement_keeps_local_credentials_and_removes_old_fields():
    previous = {"format": "short", "obsolete": True, "api_token": "private-value", "source_blueprint_id": "builtin:x", "nested": {"old": 1, "password": "private"}}
    assert components._replace_portable(previous, {"format": "long", "nested": {"new": 2}}) == {
        "format": "long", "api_token": "private-value", "source_blueprint_id": "builtin:x",
        "nested": {"new": 2, "password": "private"},
    }
    assert "private" not in str(components._portable(previous))
    assert components._replace_portable(previous, {}) == {
        "api_token": "private-value", "source_blueprint_id": "builtin:x", "nested": {"password": "private"},
    }


def test_contract_exact_skill_requirement_is_in_the_agent_source():
    payload = _payload()
    payload["contract"]["requires"]["skills"] = [{"slug": "writer", "marketplace_id": "marketplace-writer", "marketplace_source": "platform"}]
    agent = components._specs(payload)["agent:author"]
    desired = components._desired("agent", agent)
    assert desired["skill_binding_refs"] == [{"slug": "writer", "marketplace_id": "marketplace-writer", "marketplace_source": "platform"}]


def test_workflow_schema_property_names_survive_portable_component_projection():
    payload = _payload()
    payload["recipe"]["workflows"][0]["variables"] = [
        {
            "key": "project_schema",
            "default": {
                "type": "object",
                "properties": {
                    "artifact": {
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string"},
                            "filename": {"type": "string"},
                        },
                    },
                },
            },
        },
        {
            "key": "runtime_receipt",
            "default": {
                "document_id": "01LOCALRUNTIMEIDENTIFIER00",
                "filename": "final.mp4",
            },
        },
    ]

    desired = components._desired(
        "workflow",
        payload["recipe"]["workflows"][0],
    )

    artifact_properties = desired["variables"]["project_schema"]["properties"][
        "artifact"
    ]["properties"]
    assert set(artifact_properties) == {"document_id", "filename"}
    assert desired["variables"]["runtime_receipt"] == {"filename": "final.mp4"}


@pytest.mark.asyncio
async def test_blueprint_repair_updates_stale_binding_schema_when_definition_is_current(
    db_session,
):
    workspace = await _workspace(db_session)
    original = _payload()
    project_schema = {
        "type": "object",
        "properties": {
            "artifact": {
                "type": "object",
                "properties": {"filename": {"type": "string"}},
            },
        },
    }
    original["recipe"]["workflows"][0]["variables"] = [
        {"key": "project_schema", "default": project_schema},
    ]
    installed = await _install(db_session, workspace, original)
    workflow_id = installed["component_ids"]["workflow:publish"]
    binding = (
        await db_session.execute(
            select(WorkflowBinding).where(
                WorkflowBinding.workflow_id == workflow_id,
                WorkflowBinding.workspace_id == workspace.id,
            )
        )
    ).scalar_one()
    binding_id = binding.id

    changed = deepcopy(original)
    changed_schema = changed["recipe"]["workflows"][0]["variables"][0][
        "default"
    ]
    changed_schema["properties"]["artifact"]["properties"]["document_id"] = {
        "type": "string"
    }
    definition = await db_session.get(WorkflowDefinition, workflow_id)
    definition.variables = {"project_schema": deepcopy(changed_schema)}
    await db_session.flush()

    plan = await components.plan_components(
        db_session,
        workspace=workspace,
        payload=changed,
        baseline=installed["baseline"],
    )
    workflow_item = next(
        item for item in plan["items"] if item["key"] == "workflow:publish"
    )
    assert workflow_item["action"] == "keep_yours"

    repaired = await components.apply_components(
        db_session,
        workspace=workspace,
        payload=changed,
        plan=plan,
        choices={"workflow:publish": "use_blueprint"},
    )

    repaired_binding = await db_session.get(WorkflowBinding, binding_id)
    assert repaired_binding.workflow_id == workflow_id
    artifact_properties = repaired_binding.variables["project_schema"]["properties"][
        "artifact"
    ]["properties"]
    assert set(artifact_properties) == {"document_id", "filename"}
    repaired_baseline = repaired["baseline"]["workflow:publish"]
    assert repaired_baseline["content"] == repaired_baseline["source"]


@pytest.mark.asyncio
async def test_unknown_baseline_requires_review_even_for_cosmetic_changes(monkeypatch):
    payload = _payload()
    payload["embedded"]["agents"] = []
    payload["recipe"]["workflows"] = []
    desired = components._desired("skill", payload["embedded"]["skills"][0])
    current = {"skill:writer": {"id": "existing", "kind": "skill", "slug": "writer", "content": {**desired, "name": "My personal writer"}}}
    monkeypatch.setattr(components, "capture_components", AsyncMock(return_value=current))
    plan = await components.plan_components(None, workspace=SimpleNamespace(), payload=payload)
    assert plan["items"][0]["action"] == "keep_yours"
    assert plan["items"][0]["current_content"]["name"] == "My personal writer"
    assert plan["items"][0]["new_content"]["name"] == "writer"
    assert plan["items"][0]["baseline_known"] is False


@pytest.mark.asyncio
async def test_a_kept_customization_is_not_overwritten_on_the_next_upgrade(monkeypatch):
    payload = _payload()
    payload["embedded"]["agents"] = []
    payload["recipe"]["workflows"] = []
    source = components._desired("skill", payload["embedded"]["skills"][0])
    actual = {**source, "system_prompt": "My deliberately retained procedure"}
    current = {"skill:writer": {"id": "existing", "kind": "skill", "slug": "writer", "content": actual}}
    baseline = {"skill:writer": {**current["skill:writer"], "source": source}}
    monkeypatch.setattr(components, "capture_components", AsyncMock(return_value=current))
    plan = await components.plan_components(None, workspace=SimpleNamespace(), payload=payload, baseline=baseline)
    assert plan["items"][0]["action"] == "unchanged"
    changed = deepcopy(payload)
    changed["embedded"]["skills"][0]["system_prompt"] = "Author changed the procedure again"
    plan = await components.plan_components(None, workspace=SimpleNamespace(), payload=changed, baseline=baseline)
    assert plan["items"][0]["action"] == "keep_yours"
    assert plan["items"][0]["baseline_content"] == source


@pytest.mark.asyncio
async def test_preview_checks_read_access_before_projecting_shared_component_contents(monkeypatch):
    payload = _payload()
    actor = SimpleNamespace(id="operator", role="staff")
    workspace = SimpleNamespace(entity_id="entity")
    row = SimpleNamespace(id="shared-resource", slug="writer")
    monkeypatch.setattr(components, "_inventory", AsyncMock(return_value={"skill:writer": row}))
    access = AsyncMock(side_effect=PermissionError("Private component access was revoked"))
    content = AsyncMock()
    monkeypatch.setattr(components, "_require_component_access", access)
    monkeypatch.setattr(components, "_content", content)
    with pytest.raises(PermissionError, match="revoked"):
        await components.plan_components(None, workspace=workspace, payload=payload, actor=actor)
    assert access.await_args.kwargs["capability"] == "view"
    assert access.await_args.kwargs["actor"] is actor
    content.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_missing_components_in_place_and_revert(db_session):
    workspace = await _workspace(db_session)
    original_id = workspace.id
    payload = _payload()
    result = await _install(db_session, workspace, payload)
    assert workspace.id == original_id
    assert set(result["component_ids"]) == {"skill:writer", "agent:author", "workflow:publish"}
    snapshot = await components.capture_components(db_session, workspace=workspace, payload=payload)
    assert snapshot["agent:author"]["content"]["skill_bindings"] == ["writer"]
    assert snapshot["workflow:publish"]["content"]["binding"]["name"] == "Publish draft"
    assert (await components.plan_components(db_session, workspace=workspace, payload=payload, baseline=result["baseline"]))["items"][0]["action"] == "unchanged"
    await components.validate_components_restore(db_session, workspace=workspace, restore=result["restore"])
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"], validated=True)
    assert (await db_session.get(Skill, result["component_ids"]["skill:writer"])).status == "inactive"
    assert (await db_session.get(WorkflowDefinition, result["component_ids"]["workflow:publish"])).is_active is False
    assert workspace.id == original_id


@pytest.mark.asyncio
async def test_metadata_binding_removal_and_revert_are_complete(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    tool = ToolDefinition(name=f"test_tool_{generate_ulid()}", status="active")
    db_session.add(tool)
    await db_session.flush()
    payload["embedded"]["agents"][0]["tool_bindings"] = [tool.name]
    initial = await _install(db_session, workspace, payload)
    changed = deepcopy(payload)
    changed["embedded"]["skills"][0].update(name="New writer label", config={"format": "long"})
    changed["embedded"]["agents"][0].update(description="A new role", tool_bindings=[])
    changed["recipe"]["workflows"][0].update(name="Review draft", description="Updated Flow", binding_config={"new_setting": True})
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed, baseline=initial["baseline"])
    result = await components.apply_components(db_session, workspace=workspace, payload=changed, plan=plan)
    agent_id = initial["component_ids"]["agent:author"]
    assert (await db_session.execute(select(AgentToolBinding).where(AgentToolBinding.agent_id == agent_id))).scalars().all() == []
    current = await components.capture_components(db_session, workspace=workspace, payload=changed)
    assert current["workflow:publish"]["content"]["binding"]["config"] == {"new_setting": True}
    assert current["skill:writer"]["content"]["name"] == "New writer label"
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"])
    assert (await db_session.execute(select(AgentToolBinding).where(AgentToolBinding.agent_id == agent_id))).scalar_one().tool_id == tool.id
    restored = await components.capture_components(db_session, workspace=workspace, payload=payload)
    assert restored["workflow:publish"]["content"]["binding"]["name"] == "Publish draft"


@pytest.mark.asyncio
async def test_cosmetic_edit_after_preview_rejected_before_any_mutation(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    initial = await _install(db_session, workspace, payload)
    changed = deepcopy(payload)
    changed["embedded"]["skills"][0]["system_prompt"] = "A new procedure"
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed, baseline=initial["baseline"])
    skill = await db_session.get(Skill, initial["component_ids"]["skill:writer"])
    skill.description = "Operator change after preview"
    await db_session.flush()
    with pytest.raises(components.BlueprintUpgradePlanChangedError):
        await components.apply_components(db_session, workspace=workspace, payload=changed, plan=plan)
    assert skill.system_prompt == "Write carefully."


@pytest.mark.asyncio
async def test_shared_agent_is_isolated_and_original_workspace_subscription_is_restorable(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    initial = await _install(db_session, workspace, payload)
    agent_id = initial["component_ids"]["agent:author"]
    mine = AgentSubscription(entity_id=workspace.entity_id, workspace_id=workspace.id, agent_id=agent_id, status="active")
    other = AgentSubscription(entity_id=workspace.entity_id, workspace_id=generate_ulid(), agent_id=agent_id, status="active")
    db_session.add_all([mine, other])
    await db_session.flush()
    changed = deepcopy(payload)
    changed["embedded"]["agents"][0]["system_prompt"] = "A new role"
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed, baseline=initial["baseline"])
    result = await components.apply_components(db_session, workspace=workspace, payload=changed, plan=plan)
    replacement = result["component_ids"]["agent:author"]
    assert replacement != agent_id and mine.agent_id == replacement
    assert other.agent_id == agent_id
    assert (await db_session.get(Agent, agent_id)).system_prompt == "Use the writing Skill."
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"])
    assert mine.agent_id == other.agent_id == agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_setup", [False, True])
async def test_view_only_shared_copy_never_transfers_another_owners_hidden_setup(db_session, monkeypatch, protected_setup):
    from packages.core.services import resource_access

    workspace = await _workspace(db_session)
    payload = _payload()
    initial = await _install(db_session, workspace, payload)
    source_owner_id, workspace_owner_id = generate_ulid(), generate_ulid()
    actor = SimpleNamespace(id=workspace_owner_id, role="staff")
    agent_id = initial["component_ids"]["agent:author"]
    agent = await db_session.get(Agent, agent_id)
    agent.owner_user_id = source_owner_id
    agent.visibility = "entity"
    subscription = AgentSubscription(entity_id=workspace.entity_id, workspace_id=workspace.id, agent_id=agent_id, status="active")
    db_session.add(subscription)
    if protected_setup:
        agent.config = {**agent.config, "api_token": "another-owners-private-token"}
        server = (await db_session.execute(select(MCPServer).limit(1))).scalars().first()
        db_session.add(AgentMCPBinding(agent_id=agent_id, mcp_server_id=server.id,
                                       allowed_tools=[], config_override={"private_region": "another-owners-runtime-setup"}))
    await db_session.flush()

    async def access(_db, *, descriptor, capability, **_kwargs):
        return descriptor.owner_user_id != source_owner_id or capability == "view"

    monkeypatch.setattr(resource_access, "user_can_access_resource", access)
    changed = deepcopy(payload)
    changed["embedded"]["skills"][0]["system_prompt"] = "A concurrently reviewed Skill update"
    changed["embedded"]["agents"][0]["system_prompt"] = "A newly approved support role"
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed,
                                             baseline=initial["baseline"], actor=actor)
    kwargs = {"workspace": workspace, "payload": changed, "plan": plan, "actor": actor, "by_user_id": workspace_owner_id,
              "choices": {item["key"]: "use_blueprint" for item in plan["items"]}}
    if protected_setup:
        with pytest.raises(components.BlueprintUpgradeAccessDeniedError, match="private or local runtime setup"):
            await components.apply_components(db_session, **kwargs)
        assert subscription.agent_id == agent_id
        assert (await db_session.get(Skill, initial["component_ids"]["skill:writer"])).system_prompt == "Write carefully."
        assert len((await db_session.execute(select(Agent).where(Agent.entity_id == workspace.entity_id))).scalars().all()) == 1
        assert agent.config["api_token"] == "another-owners-private-token"
    else:
        result = await components.apply_components(db_session, **kwargs)
        replacement_id = result["component_ids"]["agent:author"]
        assert replacement_id != agent_id and subscription.agent_id == replacement_id
        clone = await db_session.get(Agent, replacement_id)
        assert clone.owner_user_id == workspace_owner_id and agent.owner_user_id == source_owner_id
        assert agent.system_prompt == "Use the writing Skill."
        assert clone.system_prompt == "A newly approved support role"


@pytest.mark.asyncio
async def test_only_baseline_owned_removals_are_retired_and_revert_preserves_identity(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    initial = await _install(db_session, workspace, payload)
    removed = deepcopy(payload)
    removed["recipe"]["workflows"] = []
    plan = await components.plan_components(db_session, workspace=workspace, payload=removed, baseline=initial["baseline"])
    result = await components.apply_components(db_session, workspace=workspace, payload=removed, plan=plan)
    flow_id = initial["component_ids"]["workflow:publish"]
    flow = await db_session.get(WorkflowDefinition, flow_id)
    assert flow.status == "inactive"
    assert (await db_session.execute(select(WorkflowBinding).where(WorkflowBinding.workflow_id == flow_id))).scalar_one().enabled is False
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"])
    assert flow.id == flow_id and flow.is_active is True


@pytest.mark.asyncio
async def test_shared_skill_rebinds_unchanged_agent_without_overwriting_local_prompt(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    payload["embedded"]["agents"].append({"slug": "observer", "system_prompt": "Observe without the writing Skill"})
    initial = await _install(db_session, workspace, payload)
    skill_id = initial["component_ids"]["skill:writer"]
    agent_id = initial["component_ids"]["agent:author"]
    foreign = Agent(entity_id=workspace.entity_id, workspace_id=generate_ulid(), name="Other Agent", visibility="workspace", status="active")
    db_session.add(foreign)
    await db_session.flush()
    db_session.add(AgentSkillBinding(agent_id=foreign.id, skill_id=skill_id, status="active"))
    observer_id = initial["component_ids"]["agent:observer"]
    db_session.add(AgentSubscription(entity_id=workspace.entity_id, workspace_id=foreign.workspace_id,
                                      agent_id=observer_id, status="active"))
    agent = await db_session.get(Agent, agent_id)
    agent.system_prompt = "An operator's local prompt"
    await db_session.flush()
    changed = deepcopy(payload)
    changed["embedded"]["skills"][0]["system_prompt"] = "New upstream skill instructions"
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed, baseline=initial["baseline"])
    result = await components.apply_components(db_session, workspace=workspace, payload=changed, plan=plan)
    replacement = result["component_ids"]["skill:writer"]
    assert replacement != skill_id
    assert result["component_ids"]["agent:observer"] == observer_id
    assert agent.system_prompt == "An operator's local prompt"
    binding = (await db_session.execute(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent_id, AgentSkillBinding.status == "active"))).scalar_one()
    assert binding.skill_id == replacement
    assert (await db_session.get(Skill, skill_id)).system_prompt == "Write carefully."
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"])
    assert binding.skill_id == skill_id
    assert agent.system_prompt == "An operator's local prompt"


@pytest.mark.asyncio
async def test_stable_flow_component_rename_preserves_binding_and_parent_dependency(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    payload["recipe"]["workflows"] = [
        {"slug": "parent", "steps": [{"id": "run-child", "type": "subworkflow", "config": {"source_workflow_key": "stable-child"}}]},
        {"component_id": "stable-child", "slug": "child-before", "steps": [{"id": "work", "type": "trigger", "config": {}, "next": []}]},
    ]
    initial = await _install(db_session, workspace, payload)
    child_id = initial["component_ids"]["workflow:stable-child"]
    original_binding = (await db_session.execute(select(WorkflowBinding).where(WorkflowBinding.workflow_id == child_id))).scalar_one()
    original_binding_id = original_binding.id
    changed = deepcopy(payload)
    changed["recipe"]["workflows"][1]["slug"] = "child-after"
    changed["recipe"]["workflows"][1]["name"] = "Renamed child"
    plan = await components.plan_components(db_session, workspace=workspace, payload=changed, baseline=initial["baseline"])
    result = await components.apply_components(db_session, workspace=workspace, payload=changed, plan=plan,
                                               choices={item["key"]: "use_blueprint" for item in plan["items"]})
    assert result["component_ids"]["workflow:stable-child"] == child_id
    binding = (await db_session.execute(select(WorkflowBinding).where(WorkflowBinding.workflow_id == child_id))).scalar_one()
    assert binding.id == original_binding_id and binding.name == "Renamed child"
    parent = await db_session.get(WorkflowDefinition, result["component_ids"]["workflow:parent"])
    assert next(step for step in parent.steps if step["id"] == "run-child")["config"]["workflow_id"] == child_id
    await components.revert_components(db_session, workspace=workspace, restore=result["restore"])
    assert (await db_session.get(WorkflowBinding, original_binding_id)).name == "child-before"


@pytest.mark.asyncio
async def test_mcp_override_is_never_exposed_and_later_edit_blocks_destructive_undo(db_session):
    workspace = await _workspace(db_session)
    payload = _payload()
    server = (await db_session.execute(select(MCPServer).limit(1))).scalars().first()
    assert server is not None
    payload["embedded"]["agents"][0]["mcp_bindings"] = [{"server_slug": server.server_key, "allowed_tools": ["read"]}]
    result = await _install(db_session, workspace, payload)
    agent_id = result["component_ids"]["agent:author"]
    binding = (await db_session.execute(select(AgentMCPBinding).where(AgentMCPBinding.agent_id == agent_id))).scalar_one()
    binding.config_override = {"api_token": "private-connection-value", "region": "west"}
    await db_session.flush()
    capture = await components.capture_components(db_session, workspace=workspace, payload=payload)
    assert "private-connection-value" not in str(capture)
    assert "region" not in str(capture)
    with pytest.raises(components.BlueprintUpgradePlanChangedError):
        await components.validate_components_restore(db_session, workspace=workspace, restore=result["restore"])
    assert binding.config_override["api_token"] == "private-connection-value"


@pytest.mark.asyncio
async def test_agent_mcp_upgrade_to_provider_scope_updates_durable_authorization(
    db_session,
):
    from packages.core.services.runtime_authorization.binding_verifier import (
        CurrentRuntimeToolBindingVerifier,
    )
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationAccess,
        RuntimeAuthorizationRequest,
        RuntimeToolBindingSource,
    )

    workspace = await _workspace(db_session)
    chrome = (
        await db_session.execute(
            select(MCPServer).where(MCPServer.server_key == "chrome")
        )
    ).scalar_one_or_none()
    if chrome is None:
        chrome = MCPServer(
            id=generate_ulid(),
            server_key="chrome",
            name="Chrome",
            transport="builtin",
            auth_type="none",
            status="active",
            default_allowed_tools=None,
        )
        db_session.add(chrome)
    else:
        chrome.status = "active"
        chrome.default_allowed_tools = None
    other_provider_key = f"unbound_{generate_ulid().lower()}"
    db_session.add(
        MCPServer(
            id=generate_ulid(),
            server_key=other_provider_key,
            name="Unbound provider",
            transport="builtin",
            auth_type="none",
            status="active",
            default_allowed_tools=None,
        )
    )
    await db_session.flush()

    payload = _payload()
    payload["embedded"]["agents"][0]["mcp_bindings"] = [
        {
            "server_slug": "chrome",
            "allowed_tools": ["mcp__chrome__open_or_reuse"],
        }
    ]
    initial = await _install(db_session, workspace, payload)
    changed = deepcopy(payload)
    changed["embedded"]["agents"][0]["mcp_bindings"][0]["allowed_tools"] = None
    changed["embedded"]["agents"][0]["version"] = "1.1"

    plan = await components.plan_components(
        db_session,
        workspace=workspace,
        payload=changed,
        baseline=initial["baseline"],
    )
    assert {
        item["key"] for item in plan["items"] if item["action"] != "unchanged"
    } == {"agent:author"}
    result = await components.apply_components(
        db_session,
        workspace=workspace,
        payload=changed,
        plan=plan,
    )
    agent_id = result["component_ids"]["agent:author"]
    binding = (
        await db_session.execute(
            select(AgentMCPBinding).where(
                AgentMCPBinding.agent_id == agent_id,
                AgentMCPBinding.mcp_server_id == chrome.id,
            )
        )
    ).scalar_one()
    assert binding.allowed_tools is None

    verifier = CurrentRuntimeToolBindingVerifier()

    def request(tool_name: str) -> RuntimeAuthorizationRequest:
        return RuntimeAuthorizationRequest.create(
            entity_id=workspace.entity_id,
            user_id=None,
            workspace_id=None,
            action_key="",
            capability_id="",
            access=RuntimeAuthorizationAccess.READ,
            principal_kind="agent",
            principal_agent_id=agent_id,
            tool_name=tool_name,
            bound_tool_names={tool_name},
        )

    for tool_name in ("mcp__chrome__status", "mcp__chrome__open_new_tab"):
        decision = await verifier.verify(db_session, request(tool_name))
        assert decision.source is RuntimeToolBindingSource.MCP
    denied = await verifier.verify(
        db_session,
        request(f"mcp__{other_provider_key}__read"),
    )
    assert denied.source is RuntimeToolBindingSource.REVOKED
