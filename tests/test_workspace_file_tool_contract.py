"""New Workspace file workers receive readers; saved grants stay explicit."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.capability_bindings import (
    normalize_workspace_custom_agent_tool_bindings,
)
from packages.core.ai.runtime.task_requirements import task_runtime_capability_tools
from packages.core.ai.runtime.tool_bindings import runtime_agent_tool_scope
from packages.core.ai.runtime.tool_visibility import (
    WORKSPACE_AGENT_TOOL_PROFILE,
    runtime_tool_visible_for_profile,
)
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.models.workspace import Agent, ToolDefinition, Workspace
from packages.core.services import agent_service
from packages.core.services.agent_provisioning_service import (
    CustomAgentSpec,
    provision_custom_agent,
    spec_from_create_agent_draft,
)
from packages.core.services.runtime_authorization.binding_verifier import (
    CurrentRuntimeToolBindingVerifier,
)
from packages.core.services.runtime_authorization.domain import (
    RuntimeAuthorizationRequest,
    RuntimeToolBindingSource,
)
from packages.core.services.workspace_runtime import resolve_workspace_runtime
from packages.core.services.workspace_setup_service import (
    _ensure_workspace_custom_agent_tool_bindings,
)


READ_TOOLS = {"read_file", "list_files", "glob_files", "grep_files"}
WRITE_TOOLS = {"generate_file", "patch_file", "replace_file", "code_project"}
FILE_WRITE_CATALOG_TOOLS = {"generate_file", "patch_file", "replace_file"}


def _spec(entry):
    draft = {"agent_name": "Site author", "system_prompt": "Maintain website files."}
    if entry == "setup":
        _ensure_workspace_custom_agent_tool_bindings(draft)
        return CustomAgentSpec(
            agent_name=draft["agent_name"], system_prompt=draft["system_prompt"],
            tool_bindings=draft["tool_bindings"],
        )
    return spec_from_create_agent_draft(draft, service_key="site")


@pytest.mark.parametrize("entry", ["setup", "provisioning"])
def test_new_workspace_file_worker_declares_source_readers(entry):
    spec = _spec(entry)
    assert READ_TOOLS | WRITE_TOOLS <= set(spec.tool_bindings)
    assert normalize_workspace_custom_agent_tool_bindings(spec.tool_bindings) == tuple(
        spec.tool_bindings
    )


def test_file_write_and_existing_generate_binding_do_not_grant_reads():
    # Requirement catalog names are diagnostics, not saved worker bindings.
    # Project workers bind code_project explicitly; file.write does not add it.
    catalog_tools = task_runtime_capability_tools(["file.write"])
    assert catalog_tools == FILE_WRITE_CATALOG_TOOLS
    assert READ_TOOLS.isdisjoint(catalog_tools)
    assert "code_project" not in catalog_tools
    assert task_runtime_capability_tools(["media.music"]) == {"search_free_music", "import_free_music"}
    for name in READ_TOOLS:
        assert not runtime_tool_visible_for_profile(
            name, bound_tool_names={"generate_file"}, is_master=False,
            mcp_allowed_names=set(), tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
        )


async def _provision(db, entry="provisioning"):
    entity = Entity(id=generate_ulid(), name="File tool contract")
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity.id, name="Website", operating_model={},
    )
    db.add_all([entity, workspace])
    result = await provision_custom_agent(db, entity_id=entity.id, spec=_spec(entry))
    return workspace, result.agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["setup", "provisioning"])
async def test_new_workspace_file_worker_persists_readers_in_runtime(db_session, entry):
    workspace, agent_id = await _provision(db_session, entry)
    scope = await runtime_agent_tool_scope(db_session, agent_id=agent_id, is_master=False)
    assert READ_TOOLS | WRITE_TOOLS <= set(scope.bound_tool_names)
    assert READ_TOOLS <= {
        tool.name for tool in await agent_service.get_agent_tools(db_session, agent_id)
    }
    assert (await db_session.get(Agent, agent_id)).entity_id == workspace.entity_id


@pytest.mark.asyncio
@pytest.mark.parametrize("removal", ["unbind", "inactive"])
async def test_removed_persisted_reader_remains_run_local_companion_for_writer(
    db_session, monkeypatch, removal,
):
    workspace, agent_id = await _provision(db_session)
    reader = (await db_session.execute(
        select(ToolDefinition).where(ToolDefinition.name == "read_file")
    )).scalar_one()
    request = RuntimeAuthorizationRequest.create(
        entity_id=workspace.entity_id, user_id=None, workspace_id=workspace.id,
        action_key="file.read", capability_id="file.read", access="read",
        principal_kind="agent", principal_agent_id=agent_id, tool_name="read_file",
        bound_tool_names={"read_file"}, runtime_surface="scheduled_agent_run",
    )
    verifier = CurrentRuntimeToolBindingVerifier()
    assert (await verifier.verify(db_session, request)).current
    if removal == "unbind":
        assert await agent_service.unbind_tools(db_session, agent_id, [reader.id]) == 1
    else:
        reader.status = "inactive"
        await db_session.flush()

    # An ordinary Agent save must not run creation defaults or repair the
    # durable read grant. Writers still receive run-local read companions so
    # they can inspect and verify the files they are authorized to change.
    monkeypatch.setattr(agent_service.cache, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_service.cache, "set", AsyncMock())
    monkeypatch.setattr(agent_service.cache, "delete", AsyncMock())
    await agent_service.update_agent(
        db_session, agent_id, workspace.entity_id, name="Renamed site author",
    )
    runtime = await resolve_workspace_runtime(
        db_session, entity_id=workspace.entity_id, agent_id=agent_id,
        workspace_id=workspace.id, is_master=False, include_prompt_context=False,
    )
    assert "generate_file" in runtime.bound_tool_names
    assert "read_file" in runtime.bound_tool_names
    assert runtime_tool_visible_for_profile(
        "read_file", bound_tool_names=runtime.bound_tool_names, is_master=False,
        mcp_allowed_names=set(), tool_profile=runtime.tool_profile,
    )
    assert (await verifier.verify(db_session, request)).source is RuntimeToolBindingSource.CONTEXTUAL
    # The companion exists only in the runtime scope; it does not recreate the
    # removed or disabled ToolDefinition binding in storage.
    saved_tools = await agent_service.get_agent_tools(db_session, agent_id)
    assert ("read_file" in {tool.name for tool in saved_tools}) is (removal == "inactive")
