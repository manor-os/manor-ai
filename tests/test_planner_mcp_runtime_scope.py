"""Planner MCP catalogs must describe each service's executable runtime scope."""

import pytest

from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
from packages.core.plans.planner import (
    CapabilityError,
    _enforce_allowlists,
    _filter_service_provider_actions_to_runtime_scope,
    _gather_context,
)
from packages.core.plans.schema import Plan


def test_cached_actions_respect_exact_names_and_disabled_provider_scope():
    actions = {"gmail": ["list_messages", "send_message"], "youtube": ["publish_video"]}
    specs = {
        provider: {action: {"name": action} for action in names}
        for provider, names in actions.items()
    }

    filtered, filtered_specs = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names={"mcp__gmail__send_message"},
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"list_messages"})),
        ),
    )

    assert filtered == {"gmail": ["list_messages", "send_message"]}
    assert filtered_specs == {"gmail": specs["gmail"]}


def test_dynamic_provider_wildcard_keeps_actions_before_static_schema_registration():
    actions = {"apify": ["run_actor", "get_result"]}
    specs = {"apify": {action: {"name": action} for action in actions["apify"]}}

    filtered, filtered_specs = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(RuntimeMCPProviderToolScope("apify", None),),
    )

    assert filtered == actions
    assert filtered_specs == specs


def test_same_cached_provider_does_not_transfer_actions_between_services():
    actions = {"gmail": ["list_messages", "send_message"]}
    specs = {"gmail": {action: {"name": action} for action in actions["gmail"]}}
    reader, _ = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"list_messages"})),
        ),
    )
    writer, _ = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"send_message"})),
        ),
    )

    assert reader == {"gmail": ["list_messages"]}
    assert writer == {"gmail": ["send_message"]}


@pytest.mark.asyncio
async def test_workspace_disabled_provider_cannot_reenter_planner_action_catalog(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    entity_id, workspace_id, agent_id, server_id = (generate_ulid() for _ in range(4))
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Scoped provider",
        operating_model={"integration_scope": {"disabled_providers": ["demo_mcp"]}},
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        title="Post an update", owner_service_key="publisher",
    )
    db_session.add_all([
        workspace, task,
        Agent(id=agent_id, entity_id=entity_id, name="Publisher", status="active"),
        AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
            agent_id=agent_id, service_key="publisher", status="active",
        ),
        MCPServer(
            id=server_id, server_key="demo_mcp", name="Demo MCP",
            transport="http", endpoint="https://mcp.example.test", auth_type="none",
            tools_cached={"tools": [{"name": "post_update", "inputSchema": {
                "type": "object", "properties": {"text": {"type": "string"}},
            }}]},
            status="active",
        ),
        AgentMCPBinding(
            id=generate_ulid(), agent_id=agent_id, mcp_server_id=server_id,
            allowed_tools=["post_update"], status="active",
        ),
    ])
    await db_session.flush()

    plan = Plan.model_validate({"steps": [{
        "key": "post", "kind": "action", "service_key": "publisher",
        "provider": "demo_mcp", "action_key": "post_update", "params": {"text": "hello"},
    }]})
    blocked = await _gather_context(db_session, task)
    assert blocked.service_provider_actions == {}
    with pytest.raises(CapabilityError, match="not available"):
        _enforce_allowlists(plan, blocked)

    workspace.operating_model = {}
    await db_session.flush()
    allowed = await _gather_context(db_session, task)
    assert allowed.service_provider_actions == {"publisher": {"demo_mcp": ["post_update"]}}
