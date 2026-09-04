"""Runtime-owned tools must survive fresh binding checks without granting arbitrary tools."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_visibility import eager_tool_names_for_profile
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentToolBinding, ToolDefinition, Workspace
from packages.core.services.runtime_authorization import authorize_runtime_action


async def _seed(db):
    entity_id = generate_ulid()
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Diagnostic coach", status="active")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="AI SDE preparation")
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Create the diagnostic packet", status="in_progress",
    )
    db.add_all([agent, workspace, task])
    await db.flush()
    return SimpleNamespace(entity_id=entity_id, agent=agent, workspace=workspace, task=task)


async def _authorize(db, scope, name, **overrides):
    kwargs = dict(
        entity_id=scope.entity_id, user_id=None, workspace_id=scope.workspace.id,
        task_id=scope.task.id, principal_kind="delegated", principal_agent_id=scope.agent.id,
        tool_name=name, bound_tool_names={name}, action_key="", capability_id="",
        access="read", runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
    )
    kwargs.update(overrides)
    return await authorize_runtime_action(db, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [
    ChatSurface.WORKSPACE_CHAT, ChatSurface.TASK_COMMENT_THREAD,
    ChatSurface.SCHEDULED_AGENT_RUN, ChatSurface.WORKFLOW_AGENT_STEP,
])
async def test_workspace_runtime_defaults_pass_fresh_authorization(db_session, surface):
    scope = await _seed(db_session)
    # No AgentToolBinding rows: these grants come from the same server policy
    # that exposes the schemas, not from a model-supplied allowlist.
    for name in eager_tool_names_for_profile(is_master=False, tool_profile="workspace_agent"):
        decision = await _authorize(db_session, scope, name, runtime_surface=surface)
        assert decision.allowed, (name, decision)


@pytest.mark.asyncio
async def test_agent_dm_can_discover_its_bound_tools(db_session):
    scope = await _seed(db_session)
    decision = await _authorize(
        db_session, scope, "search_tools", workspace_id=None, task_id=None,
        runtime_surface=ChatSurface.AGENT_DM,
    )
    assert decision.allowed


@pytest.mark.asyncio
async def test_runtime_policy_does_not_widen_a_restricted_loop(db_session):
    scope = await _seed(db_session)
    decision = await _authorize(db_session, scope, "workspace_update_task_runtime", bound_tool_names={"search_tools"})
    assert not decision.allowed
    assert decision.matched_rule == "permission.agent_tool_binding"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT, "unknown"])
async def test_external_or_unknown_surface_gets_no_implicit_workspace_control(db_session, surface):
    scope = await _seed(db_session)
    decision = await _authorize(db_session, scope, "workspace_update_task_runtime", runtime_surface=surface)
    assert not decision.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["inactive_agent", "deleted_agent", "foreign_agent", "deleted_workspace", "foreign_workspace"])
async def test_runtime_defaults_do_not_bypass_identity_or_workspace_scope(db_session, invalid):
    scope = await _seed(db_session)
    if invalid == "inactive_agent":
        scope.agent.status = "inactive"
    elif invalid == "deleted_agent":
        scope.agent.deleted_at = datetime.now(timezone.utc)
    elif invalid == "foreign_agent":
        scope.agent.entity_id = generate_ulid()
    elif invalid == "deleted_workspace":
        scope.workspace.deleted_at = datetime.now(timezone.utc)
    else:
        scope.workspace.entity_id = generate_ulid()
    await db_session.flush()
    decision = await _authorize(db_session, scope, "search_tools")
    assert not decision.allowed


@pytest.mark.asyncio
async def test_business_tool_revocation_is_not_replaced_by_visibility(db_session):
    scope = await _seed(db_session)
    tool = (await db_session.execute(select(ToolDefinition).where(ToolDefinition.name == "generate_file"))).scalar_one_or_none()
    if tool is None:
        tool = ToolDefinition(id=generate_ulid(), name="generate_file", status="active")
        db_session.add(tool)
    db_session.add(AgentToolBinding(agent_id=scope.agent.id, tool_id=tool.id))
    await db_session.flush()
    assert (await _authorize(db_session, scope, "generate_file")).allowed
    await db_session.execute(delete(AgentToolBinding).where(AgentToolBinding.agent_id == scope.agent.id))
    # A stale loop snapshot may still contain generate_file, but it is not a
    # runtime-owned default and must not reinstate the revoked binding.
    assert not (await _authorize(db_session, scope, "generate_file")).allowed
    assert not (await _authorize(db_session, scope, "mcp__mail__send")).allowed
    assert not (await _authorize(db_session, scope, "arbitrary_unbound_tool")).allowed


@pytest.mark.asyncio
async def test_delegated_discovery_to_saved_workspace_file_uses_real_guards(db_session, monkeypatch, tmp_path):
    """Exercise the failing production chain, without mocking authorization or persistence."""
    import json
    from pathlib import Path
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        runtime_execute_registered_tool,
        runtime_prepare_prompt_appendix_for_turn,
    )
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
    from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler
    from packages.core.ai.tools.generate_file_tool import _generate_file_handler
    from packages.core.ai.tools.workspace_agent_tools import _workspace_update_task_runtime_handler
    from packages.core.config import get_settings
    from packages.core.constants.agent_capabilities import AGENT_EAGER_BOUND_TOOL_LIMIT
    from packages.core.models.document import Document
    from packages.core.models.user import Entity
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr("packages.core.database.async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    # Only asynchronous external embedding work is excluded from this local test.
    monkeypatch.setattr("packages.core.services.knowledge_sync._schedule_document_reembed", lambda _id: None)
    scope = await _seed(db_session)
    db_session.add_all([
        Entity(id=scope.entity_id, name="Diagnostic test entity"),
        AgentSubscription(entity_id=scope.entity_id, agent_id=scope.agent.id, workspace_id=scope.workspace.id),
    ])
    registry_schemas = runtime_registered_tool_schemas()
    registry_names = [
        name
        for name, _schema in registry_schemas
        if not name.startswith("mcp__")
        and name not in {"search_tools", "render_response_surface", "notify_user"}
    ]
    wide_bound_names = [
        "generate_file",
        *[
            name
            for name in registry_names
            if name != "generate_file"
        ][:AGENT_EAGER_BOUND_TOOL_LIMIT],
    ]
    assert len(wide_bound_names) == AGENT_EAGER_BOUND_TOOL_LIMIT + 1
    unbound_name = next(
        name for name in registry_names if name not in wide_bound_names
    )
    existing_tools = {
        tool.name: tool
        for tool in (
            await db_session.scalars(
                select(ToolDefinition).where(
                    ToolDefinition.name.in_(wide_bound_names)
                )
            )
        ).all()
    }
    for name in wide_bound_names:
        tool = existing_tools.get(name)
        if tool is None:
            tool = ToolDefinition(id=generate_ulid(), name=name, status="active")
            db_session.add(tool)
            existing_tools[name] = tool
        db_session.add(
            AgentToolBinding(agent_id=scope.agent.id, tool_id=tool.id)
        )
    await ensure_workspace_artifact_folder(db_session, scope.workspace)
    await db_session.commit()
    entity_root = Path(provision_entity_filesystem(scope.entity_id))
    workspace_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=scope.entity_id,
        agent_id=scope.agent.id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        is_master=False,
        runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
        include_prompt_context=False,
    )
    assert set(wide_bound_names) <= set(workspace_runtime.bound_tool_names or ())
    assert unbound_name not in set(workspace_runtime.bound_tool_names or ())
    request = AIRuntimeRequest(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        entity_id=scope.entity_id,
        agent_id=scope.agent.id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
    )
    prepared = await runtime_prepare_prompt_appendix_for_turn(
        db_session,
        request=request,
        tool_profile=workspace_runtime.tool_profile,
        runtime_profile=workspace_runtime.runtime_profile,
        agent_id=scope.agent.id,
        bound_tool_names=workspace_runtime.bound_tool_names,
        is_master=workspace_runtime.is_master,
        mcp_allowed_names=workspace_runtime.mcp_allowed_names,
    )
    envelope = prepared.envelope
    prompt_tool_names = {
        schema.get("function", {}).get("name")
        for schema in prepared.tool_schemas
    }
    assert "search_tools" in prompt_tool_names
    assert "generate_file" not in prompt_tool_names
    assert set(wide_bound_names) <= set(prepared.allowed_tool_names)
    assert unbound_name not in set(prepared.allowed_tool_names)

    async def search_handler(**kwargs):
        return await runtime_execute_search_tools_handler(
            arguments=kwargs, entity_id=scope.entity_id,
            tool_schemas=registry_schemas,
            available_tool_names=[name for name, _schema in registry_schemas],
        )

    handlers = {
        "search_tools": search_handler,
        "generate_file": _generate_file_handler,
        "workspace_update_task_runtime": _workspace_update_task_runtime_handler,
    }

    async def execute(name, arguments):
        return json.loads(await runtime_execute_registered_tool(
            tool_name=name, arguments=arguments, handler_resolver=handlers.get,
            entity_id=scope.entity_id, agent_id=scope.agent.id,
            workspace_id=scope.workspace.id, task_id=scope.task.id,
            tool_profile="workspace_agent", runtime_envelope=envelope,
        ))

    updated = await execute("workspace_update_task_runtime", {
        "task_id": scope.task.id, "runtime_instructions": "Save the diagnostic packet in this task's artifact folder.",
    })
    assert updated.get("updated") is True, updated
    await db_session.refresh(scope.task)
    assert "diagnostic packet" in str(scope.task.details["runtime_context"])
    discovered = await execute(
        "search_tools",
        {"query": f"select:generate_file,{unbound_name}"},
    )
    assert not discovered.get("error"), discovered
    assert discovered["loaded_tools"] == ["generate_file"], discovered
    assert unbound_name not in {
        match.get("name") for match in discovered.get("matches", [])
    }
    content = "# AI SDE 基线诊断\n\nSeven modules, 90–120 minutes.\n"
    arguments = {"kind": "document", "name": "baseline_diagnostic_packet.md", "content": content, "file_type": "md"}
    generated = await execute("generate_file", arguments)
    assert generated.get("created") is True, generated
    artifact = generated["document"]
    document_id = artifact.get("document_id") or artifact["id"]
    document = await db_session.get(Document, document_id)
    assert document is not None
    assert (entity_root / document.fs_path).read_text() == content
    assert document.metadata_["origin"]["workspace_id"] == scope.workspace.id
    assert document.metadata_["origin"]["task_id"] == scope.task.id
    assert document_id in artifact["viewer_url"]

    # The same stale envelope cannot write another file after the actual
    # business-tool grant is removed. Discovery is not a replacement grant.
    await db_session.execute(
        delete(AgentToolBinding).where(
            AgentToolBinding.agent_id == scope.agent.id,
            AgentToolBinding.tool_id == existing_tools["generate_file"].id,
        )
    )
    await db_session.commit()
    refreshed_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=scope.entity_id,
        agent_id=scope.agent.id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        is_master=False,
        runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
        include_prompt_context=False,
    )
    assert "generate_file" not in set(refreshed_runtime.bound_tool_names or ())
    blocked = await execute("generate_file", {**arguments, "name": "must_not_exist.md"})
    assert blocked.get("error") == "blocked_by_permission", blocked
    assert not list(entity_root.rglob("must_not_exist.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_actions", "expected_loaded"),
    [
        (None, True),
        (["list_customers"], False),
    ],
)
async def test_workspace_runtime_preserves_semantic_mcp_scope_through_search(
    db_session,
    monkeypatch,
    binding_actions,
    expected_loaded,
):
    """Workspace -> Prompt -> RuntimeEnvelope -> search_tools keeps MCP semantics."""
    import json

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        runtime_prepare_prompt_appendix_for_turn,
    )
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
    from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler
    from packages.core.ai.runtime import tool_search
    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    scope = await _seed(db_session)
    server = (
        await db_session.execute(
            select(MCPServer).where(MCPServer.server_key == "stripe")
        )
    ).scalar_one_or_none()
    if server is None:
        server = MCPServer(
            id=generate_ulid(),
            server_key="stripe",
            name="Stripe",
            transport="http",
            endpoint="https://example.invalid/mcp",
            auth_type="oauth2",
            default_allowed_tools=None,
            status="active",
        )
        db_session.add(server)
        await db_session.flush()
    else:
        server.default_allowed_tools = None
        server.status = "active"
    binding = AgentMCPBinding(
        id=generate_ulid(),
        agent_id=scope.agent.id,
        mcp_server_id=server.id,
        allowed_tools=binding_actions,
        status="active",
    )
    db_session.add(binding)
    await db_session.flush()

    workspace_runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=scope.entity_id,
        agent_id=scope.agent.id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        is_master=False,
        runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
        include_prompt_context=False,
    )
    provider_scope = {
        item.provider: item.allowed_actions
        for item in workspace_runtime.mcp_provider_scopes
    }
    assert provider_scope == {
        "stripe": None if binding_actions is None else frozenset(binding_actions)
    }

    request = AIRuntimeRequest(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        entity_id=scope.entity_id,
        agent_id=scope.agent.id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
    )
    prepared = await runtime_prepare_prompt_appendix_for_turn(
        db_session,
        request=request,
        tool_profile=workspace_runtime.tool_profile,
        runtime_profile=workspace_runtime.runtime_profile,
        agent_id=scope.agent.id,
        bound_tool_names=workspace_runtime.bound_tool_names,
        is_master=workspace_runtime.is_master,
        mcp_allowed_names=workspace_runtime.mcp_allowed_names,
        mcp_provider_scopes=workspace_runtime.mcp_provider_scopes,
        mcp_scope_unrestricted=workspace_runtime.mcp_scope_unrestricted,
    )
    assert prepared.envelope.mcp_provider_scopes == workspace_runtime.mcp_provider_scopes

    public_name = "mcp__stripe__future_stripe_tool"

    async def load_live(provider_keys):
        assert provider_keys == frozenset({"stripe"})
        return {
            "stripe": [
                (
                    public_name,
                    {
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": "Live future Stripe action",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                )
            ],
        }

    async def annotate(matches, _entity_id, _user_id):
        for match in matches:
            match["available"] = True
        return matches

    monkeypatch.setattr(tool_search, "runtime_annotate_tool_availability", annotate)
    registry_schemas = runtime_registered_tool_schemas()
    payload = json.loads(
        await runtime_execute_search_tools_handler(
            arguments={
                "query": f"select:{public_name}",
                "_runtime_envelope_from_context": prepared.envelope,
            },
            # This test exercises Runtime-envelope scope propagation, not the
            # independently covered v2 account-availability prefilter.
            entity_id="",
            tool_schemas=registry_schemas,
            available_tool_names=[name for name, _schema in registry_schemas],
            live_mcp_schema_loader=load_live,
        )
    )

    assert (public_name in payload["loaded_tools"]) is expected_loaded
    authorization = await authorize_runtime_action(
        db_session,
        entity_id=scope.entity_id,
        user_id=None,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        principal_kind="delegated",
        principal_agent_id=scope.agent.id,
        tool_name=public_name,
        bound_tool_names=set(prepared.allowed_tool_names) | {public_name},
        action_key="",
        capability_id="",
        access="read",
        runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
    )
    assert authorization.allowed is expected_loaded

    if expected_loaded:
        binding.status = "inactive"
        await db_session.flush()
        revoked = await authorize_runtime_action(
            db_session,
            entity_id=scope.entity_id,
            user_id=None,
            workspace_id=scope.workspace.id,
            task_id=scope.task.id,
            principal_kind="delegated",
            principal_agent_id=scope.agent.id,
            tool_name=public_name,
            bound_tool_names=set(prepared.allowed_tool_names) | {public_name},
            action_key="",
            capability_id="",
            access="read",
            runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
        )
        assert not revoked.allowed


@pytest.mark.asyncio
async def test_master_task_service_provider_scope_is_not_unrestricted(
    db_session,
    monkeypatch,
):
    from packages.core.ai.runtime.tool_bindings import (
        RuntimeMCPProviderToolScopeFactory,
    )
    from packages.core.services import workspace_runtime as workspace_runtime_module

    scope = await _seed(db_session)
    provider_scopes = RuntimeMCPProviderToolScopeFactory.create({"stripe": None})
    active_provider_scopes = provider_scopes

    async def load_service_agent_ids(*_args, **_kwargs):
        return ["service-agent"]

    async def resolve_service_scope(*_args, **_kwargs):
        return set(), set(), active_provider_scopes

    monkeypatch.setattr(
        workspace_runtime_module,
        "_load_task_service_agent_ids",
        load_service_agent_ids,
    )
    monkeypatch.setattr(
        workspace_runtime_module,
        "_resolve_service_agent_tool_scope",
        resolve_service_scope,
    )

    runtime = await workspace_runtime_module.resolve_workspace_runtime(
        db_session,
        entity_id=scope.entity_id,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        is_master=True,
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
        include_prompt_context=False,
    )

    # Master is the host identity, not an implicit MCP wildcard. A semantic
    # service scope with no currently registered schemas must close the exact
    # name set so unrelated providers cannot enter the prompt surface.
    assert runtime.mcp_allowed_names == set()
    assert runtime.mcp_provider_scopes == provider_scopes
    assert runtime.mcp_scope_unrestricted is False

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        runtime_prepare_prompt_appendix_for_turn,
    )

    prepared = await runtime_prepare_prompt_appendix_for_turn(
        db_session,
        request=AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id=scope.entity_id,
            workspace_id=scope.workspace.id,
            task_id=scope.task.id,
        ),
        tool_profile=runtime.tool_profile,
        runtime_profile=runtime.runtime_profile,
        bound_tool_names=runtime.bound_tool_names,
        is_master=runtime.is_master,
        mcp_allowed_names=runtime.mcp_allowed_names,
        mcp_provider_scopes=runtime.mcp_provider_scopes,
        mcp_scope_unrestricted=runtime.mcp_scope_unrestricted,
    )
    prompt_names = {
        str(schema.get("function", {}).get("name") or "")
        for schema in prepared.tool_schemas
    }
    assert not any(name.startswith("mcp__paypal__") for name in prompt_names)
    assert not any(name.startswith("mcp__stripe__") for name in prompt_names)
    assert prepared.envelope.mcp_provider_scopes == provider_scopes

    from packages.core.constants.agents import MANOR_AGENT_ID

    stripe = await authorize_runtime_action(
        db_session,
        entity_id=scope.entity_id,
        user_id=None,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        principal_kind="delegated",
        principal_agent_id=MANOR_AGENT_ID,
        tool_name="mcp__stripe__future_action",
        bound_tool_names={"mcp__stripe__future_action"},
        action_key="",
        capability_id="",
        access="read",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )
    paypal = await authorize_runtime_action(
        db_session,
        entity_id=scope.entity_id,
        user_id=None,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        principal_kind="delegated",
        principal_agent_id=MANOR_AGENT_ID,
        tool_name="mcp__paypal__future_action",
        bound_tool_names={"mcp__paypal__future_action"},
        action_key="",
        capability_id="",
        access="read",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )
    assert stripe.allowed
    assert not paypal.allowed

    from packages.core.models.user import Entity, User, UserMembership

    user_id = generate_ulid()
    db_session.add_all(
        [
            Entity(id=scope.entity_id, name="Master scope test entity"),
            User(
                id=user_id,
                entity_id=scope.entity_id,
                email=f"{user_id}@example.test",
                password_hash="test",
                role="owner",
                status="active",
            ),
            UserMembership(
                id=generate_ulid(),
                user_id=user_id,
                entity_id=scope.entity_id,
                role="owner",
                status="active",
                is_primary=True,
            ),
        ]
    )
    await db_session.flush()

    async def authorize_human(tool_name):
        return await authorize_runtime_action(
            db_session,
            entity_id=scope.entity_id,
            user_id=user_id,
            workspace_id=scope.workspace.id,
            task_id=scope.task.id,
            tool_name=tool_name,
            bound_tool_names={tool_name},
            action_key="",
            capability_id="",
            access="read",
            runtime_surface=ChatSurface.WORKSPACE_CHAT,
        )

    assert (await authorize_human("mcp__stripe__future_action")).allowed
    assert not (await authorize_human("mcp__paypal__future_action")).allowed

    # Revoking the final service-Agent MCP binding must close the Task scope;
    # it must never restore the master Host's legacy global MCP wildcard.
    active_provider_scopes = ()
    revoked_master = await authorize_runtime_action(
        db_session,
        entity_id=scope.entity_id,
        user_id=None,
        workspace_id=scope.workspace.id,
        task_id=scope.task.id,
        principal_kind="delegated",
        principal_agent_id=MANOR_AGENT_ID,
        tool_name="mcp__stripe__future_action",
        bound_tool_names={"mcp__stripe__future_action"},
        action_key="",
        capability_id="",
        access="read",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )
    assert not revoked_master.allowed
    assert not (await authorize_human("mcp__stripe__future_action")).allowed
