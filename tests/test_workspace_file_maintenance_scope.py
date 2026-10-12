"""Workspace file maintenance grants remain explicit, scoped, and revocable.

Use real ORM queries against SQLite in memory: no configured PostgreSQL database,
network, model, or filesystem writes. Only copied DDL types and the database
session factory are adapted. Partial-index predicates are preserved in SQLite.
Resolver, capability expansion, current binding
verification, and runtime execution gates are the production implementations.
File bytes/Document atomicity are covered by the existing file-tool integration
suite; the handler spy here proves denied calls never reach a file handler.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, MetaData
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
from packages.core.governance.approval_policy_service import account_defaults, initialize_workspace_approval
from packages.core.models.execution import ExecutionStep
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.user import Entity
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.runtime_authorization import authorize_runtime_action
from packages.core.services.workspace_runtime import resolve_workspace_runtime


ENTITY = "maintenance-entity"
WORKSPACE = "maintenance-workspace"
OTHER_WORKSPACE = "other-workspace"
AGENT = "maintenance-agent"
OTHER_AGENT = "other-agent"
SERVICE = "site-maintenance"
READ_TOOLS = {"read_file", "list_files", "glob_files", "grep_files"}
WRITE_TOOLS = {"generate_file", "patch_file", "replace_file"}
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS
SURFACES = (
    ChatSurface.WORKSPACE_CHAT,
    ChatSurface.TASK_COMMENT_THREAD,
    ChatSurface.SCHEDULED_AGENT_RUN,
    ChatSurface.WORKFLOW_AGENT_STEP,
)


@pytest.fixture
async def maintenance_sessions(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (
        Agent, Workspace, AgentSubscription, ToolDefinition, AgentToolBinding,
        MCPServer, AgentMCPBinding, Skill, AgentSkillBinding,
        Entity, ExecutionStep, GovernancePolicy, GovernanceRevision, HitlRequest,
    ):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, (JSONB, ARRAY)):
                column.type = JSON()
        for index in table.indexes:
            where = index.dialect_options["postgresql"].get("where")
            if where is not None:
                index.dialect_options["sqlite"]["where"] = where
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("packages.core.database.async_session", sessions)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with sessions.begin() as db:
            db.add_all([
                Entity(id=ENTITY, name="Maintenance test", settings={"approval_defaults": account_defaults()}),
                Agent(id=AGENT, entity_id=ENTITY, name="HTML maintainer", status="active"),
                Agent(id=OTHER_AGENT, entity_id=ENTITY, name="Other agent", status="active"),
                Workspace(id=WORKSPACE, entity_id=ENTITY, name="Website"),
                Workspace(id=OTHER_WORKSPACE, entity_id=ENTITY, name="Other website"),
                AgentSubscription(id="target-subscription", entity_id=ENTITY, agent_id=AGENT,
                                  workspace_id=WORKSPACE, service_key=SERVICE, status="active"),
                AgentSubscription(id="other-service-subscription", entity_id=ENTITY, agent_id=OTHER_AGENT,
                                  workspace_id=WORKSPACE, service_key="reporting", status="active"),
                AgentSubscription(id="other-workspace-subscription", entity_id=ENTITY, agent_id=AGENT,
                                  workspace_id=OTHER_WORKSPACE, service_key=SERVICE, status="active"),
                ToolDefinition(id="generate-tool", name="generate_file", status="active"),
                AgentToolBinding(agent_id=AGENT, tool_id="generate-tool"),
                AgentToolBinding(agent_id=OTHER_AGENT, tool_id="generate-tool"),
            ])
            await db.flush()
            for workspace_id in (WORKSPACE, OTHER_WORKSPACE):
                await initialize_workspace_approval(db, await db.get(Workspace, workspace_id))
        yield sessions
    finally:
        await engine.dispose()


async def _set_bindings(sessions, capabilities=("file.read", "file.write"), *, enabled=True):
    async with sessions.begin() as db:
        workspace = await db.get(Workspace, WORKSPACE)
        workspace.operating_model = {
            "capability_bindings": [
                {
                    "binding_key": f"maintenance-{capability}",
                    "capability_type": "capability",
                    "capability": capability,
                    "owner_scope": "service",
                    "service_key": SERVICE,
                    "enabled": enabled,
                }
                for capability in capabilities
            ],
        }


async def _scope(sessions, *, workspace_id=WORKSPACE, agent_id=AGENT,
                 surface=ChatSurface.SCHEDULED_AGENT_RUN, **kwargs):
    async with sessions() as db:
        return await resolve_workspace_runtime(
            db, entity_id=ENTITY, agent_id=agent_id, workspace_id=workspace_id,
            is_master=False, runtime_surface=surface, include_prompt_context=False,
            **kwargs,
        )


async def _authorize(sessions, tool, allowed, *, workspace_id=WORKSPACE,
                     agent_id=AGENT, surface=ChatSurface.SCHEDULED_AGENT_RUN):
    async with sessions() as db:
        return await authorize_runtime_action(
            db, entity_id=ENTITY, user_id=None, workspace_id=workspace_id,
            principal_kind="delegated", principal_agent_id=agent_id,
            tool_name=tool, bound_tool_names=allowed, action_key="", capability_id="",
            access="read" if tool in READ_TOOLS else "action", runtime_surface=surface,
        )


def _envelope(scope, surface=ChatSurface.SCHEDULED_AGENT_RUN):
    return RuntimeEnvelope(
        surface=surface,
        principal=resolve_runtime_principal(
            surface=surface, entity_id=ENTITY, agent_id=AGENT, workspace_id=WORKSPACE,
        ),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        tool_profile="workspace_agent", entity_id=ENTITY,
        agent_id=AGENT, workspace_id=WORKSPACE,
        allowed_tool_names=tuple(scope.bound_tool_names or ()),
    )


async def _execute(envelope, tool, handler, arguments=None):
    raw = await runtime_execute_registered_tool(
        tool_name=tool, arguments={"path": "site/index.html", **(arguments or {})},
        handler_resolver=lambda _name: handler, runtime_envelope=envelope,
        entity_id=ENTITY, agent_id=AGENT, workspace_id=WORKSPACE,
        tool_profile="workspace_agent",
    )
    return json.loads(raw)


@pytest.mark.parametrize("surface", SURFACES)
async def test_existing_generate_only_agent_gets_read_companions_not_file_maintenance(maintenance_sessions, surface):
    scope = await _scope(maintenance_sessions, surface=surface)
    assert set(scope.bound_tool_names) & FILE_TOOLS == {"generate_file"} | READ_TOOLS
    assert (await _authorize(maintenance_sessions, "generate_file", {"generate_file"}, surface=surface)).allowed
    for tool in READ_TOOLS:
        assert (await _authorize(
            maintenance_sessions, tool, scope.bound_tool_names, surface=surface,
        )).allowed, tool
    for tool in WRITE_TOOLS - {"generate_file"}:
        # Even a stale/forged allowlist cannot manufacture the missing grant.
        decision = await _authorize(maintenance_sessions, tool, FILE_TOOLS, surface=surface)
        assert not decision.allowed, tool
        assert decision.matched_rule == "permission.agent_tool_binding"


@pytest.mark.parametrize("surface", SURFACES)
async def test_explicit_workspace_service_binding_grants_only_matching_runtime(maintenance_sessions, surface):
    await _set_bindings(maintenance_sessions)
    scope = await _scope(maintenance_sessions, surface=surface)
    assert FILE_TOOLS <= set(scope.bound_tool_names)
    for tool in FILE_TOOLS:
        assert (await _authorize(maintenance_sessions, tool, scope.bound_tool_names, surface=surface)).allowed, tool

    for workspace_id, agent_id in ((OTHER_WORKSPACE, AGENT), (WORKSPACE, OTHER_AGENT)):
        other = await _scope(maintenance_sessions, workspace_id=workspace_id, agent_id=agent_id, surface=surface)
        assert set(other.bound_tool_names) & FILE_TOOLS == {"generate_file"} | READ_TOOLS
        for tool in ("patch_file", "replace_file"):
            decision = await _authorize(
                maintenance_sessions, tool, scope.bound_tool_names,
                workspace_id=workspace_id, agent_id=agent_id, surface=surface,
            )
            assert not decision.allowed, (workspace_id, agent_id, tool)


async def test_same_agent_other_service_does_not_inherit_exact_subscription_grant(maintenance_sessions):
    await _set_bindings(maintenance_sessions)
    async with maintenance_sessions.begin() as db:
        db.add(AgentSubscription(
            id="second-service-same-agent", entity_id=ENTITY, agent_id=AGENT,
            workspace_id=WORKSPACE, service_key="reporting", status="active",
        ))
    matching = await _scope(maintenance_sessions, agent_subscription_id="target-subscription")
    other = await _scope(maintenance_sessions, agent_subscription_id="second-service-same-agent")
    assert FILE_TOOLS <= set(matching.bound_tool_names)
    assert set(other.bound_tool_names) & FILE_TOOLS == {"generate_file"} | READ_TOOLS


async def test_explicit_read_grant_reaches_runtime_handler(maintenance_sessions):
    await _set_bindings(maintenance_sessions, ("file.read",))
    envelope = _envelope(await _scope(maintenance_sessions))
    handler = AsyncMock(return_value=json.dumps({"content": "<h1>Existing page</h1>"}))
    result = await _execute(envelope, "read_file", handler)
    assert result == {"content": "<h1>Existing page</h1>"}
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["path"] == "site/index.html"


@pytest.mark.parametrize("revocation", ["remove", "disable", "unsubscribe"])
@pytest.mark.parametrize("tool", ["patch_file", "replace_file"])
async def test_revoked_workspace_grant_blocks_stale_execution_before_handler(maintenance_sessions, revocation, tool):
    await _set_bindings(maintenance_sessions)
    stale_scope = await _scope(maintenance_sessions)
    envelope = _envelope(stale_scope)
    assert tool in envelope.allowed_tool_names
    assert (await _authorize(maintenance_sessions, tool, envelope.allowed_tool_names)).allowed

    if revocation == "unsubscribe":
        async with maintenance_sessions.begin() as db:
            subscription = await db.get(AgentSubscription, "target-subscription")
            subscription.status = "inactive"
    else:
        await _set_bindings(maintenance_sessions, () if revocation == "remove" else ("file.read", "file.write"), enabled=False)
    fresh_scope = await _scope(maintenance_sessions)
    assert set(fresh_scope.bound_tool_names) & FILE_TOOLS == {"generate_file"} | READ_TOOLS

    handler = AsyncMock(return_value=json.dumps({"executed": True}))
    result = await _execute(envelope, tool, handler)
    assert result.get("error") == "blocked_by_permission", result
    assert result["matched_rule"] == "permission.agent_tool_binding"
    handler.assert_not_awaited()


async def test_write_grant_preserves_required_read_companions(maintenance_sessions):
    await _set_bindings(maintenance_sessions)
    stale_scope = await _scope(maintenance_sessions)
    await _set_bindings(maintenance_sessions, ("file.write",))
    current = await _scope(maintenance_sessions)
    assert set(current.bound_tool_names) & FILE_TOOLS == FILE_TOOLS
    assert (await _authorize(maintenance_sessions, "read_file", stale_scope.bound_tool_names)).allowed
    assert (await _authorize(maintenance_sessions, "patch_file", stale_scope.bound_tool_names)).allowed


@pytest.mark.parametrize("arguments", [
    pytest.param({}, id="stale-context"),
    pytest.param({"agent_subscription_id": "second-service-same-agent"}, id="forged-subscription"),
    pytest.param({"_agent_subscription_id": "second-service-same-agent"}, id="forged-hidden-subscription"),
])
async def test_fresh_execution_cannot_borrow_another_subscription_after_revocation(maintenance_sessions, arguments):
    await _set_bindings(maintenance_sessions, ("file.write",))
    async with maintenance_sessions.begin() as db:
        db.add(AgentSubscription(
            id="second-service-same-agent", entity_id=ENTITY, agent_id=AGENT,
            workspace_id=WORKSPACE, service_key="reporting", status="active",
        ))
    stale_scope = await _scope(maintenance_sessions, agent_subscription_id="target-subscription")
    envelope = replace(
        _envelope(stale_scope), metadata={"agent_subscription_id": "target-subscription"},
    )
    async with maintenance_sessions.begin() as db:
        workspace = await db.get(Workspace, WORKSPACE)
        # Revoke this service's write grant while an independently authorized
        # service using the same reusable Agent retains its own grant.
        workspace.operating_model = {"capability_bindings": [{
            "binding_key": "reporting-write", "capability_type": "capability",
            "capability": "file.write", "owner_scope": "service", "service_key": "reporting",
        }]}
    current = await _scope(maintenance_sessions, agent_subscription_id="target-subscription")
    assert "patch_file" not in current.bound_tool_names
    other = await _scope(maintenance_sessions, agent_subscription_id="second-service-same-agent")
    assert "patch_file" in other.bound_tool_names
    handler = AsyncMock(return_value=json.dumps({"executed": True}))
    # Model-authored arguments cannot replace the trusted deployment identity.
    result = await _execute(envelope, "patch_file", handler, arguments)
    assert result.get("error") == "blocked_by_permission", result
    assert result["matched_rule"] == "permission.agent_tool_binding"
    handler.assert_not_awaited()


async def test_explicit_runtime_block_wins_over_workspace_read_grant(maintenance_sessions):
    await _set_bindings(maintenance_sessions)
    envelope = replace(_envelope(await _scope(maintenance_sessions)), blocked_tool_names=("read_file",))
    handler = AsyncMock(return_value=json.dumps({"executed": True}))
    result = await _execute(envelope, "read_file", handler)
    assert result.get("error") == "blocked_by_runtime_policy", result
    handler.assert_not_awaited()


@pytest.mark.parametrize("surface,profile", [
    (ChatSurface.PUBLIC_CUSTOMER_CHAT, RuntimeProfile.EXTERNAL_CUSTOMER_SAFE),
    (ChatSurface.EXTERNAL_CHANNEL_CHAT, RuntimeProfile.EXTERNAL_CHANNEL_SAFE),
])
async def test_workspace_grant_does_not_expose_file_reader_on_external_surface(maintenance_sessions, surface, profile):
    await _set_bindings(maintenance_sessions)
    envelope = replace(_envelope(await _scope(maintenance_sessions), surface), profile=profile)
    handler = AsyncMock(return_value=json.dumps({"executed": True}))
    result = await _execute(envelope, "read_file", handler)
    assert result.get("error") in {"external_principal_tool_denied", "tool_not_in_runtime_surface"}, result
    handler.assert_not_awaited()
