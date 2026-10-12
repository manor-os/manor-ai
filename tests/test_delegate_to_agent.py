"""delegate_to_agent: Workspace agents are part of the Workspace chat tool list.

Before this, handing work to another agent meant calling the generic
``manor(action='workspace', params={action:'delegate_service', ...})`` gateway
with a service_key or subscription id the model had to find first, from a
context list capped at 8 agents that printed keyless agents as the
non-matching ``service_key=general``.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as delegation
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription, Workspace


async def _workspace_with_agents(db, entity_id):
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Launch", status="active")
    db.add(workspace)
    agents = {
        "writer": Agent(id=generate_ulid(), entity_id=entity_id, name="Writer", description="Drafts posts and replies.", status="active"),
        "video_a": Agent(id=generate_ulid(), entity_id=entity_id, name="Video Operator", description="Plans videos.", status="active"),
        "video_b": Agent(id=generate_ulid(), entity_id=entity_id, name="Video Operator", status="active"),
        "retired": Agent(id=generate_ulid(), entity_id=entity_id, name="Retired", status="inactive"),
    }
    db.add_all(agents.values())
    if await db.get(Agent, MANOR_AGENT_ID) is None:
        db.add(Agent(id=MANOR_AGENT_ID, name="Manor AI", status="active"))
    await db.flush()
    subs = {
        "writer": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=agents["writer"].id, status="active"),
        "video_a": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=agents["video_a"].id, service_key="douyin", status="active"),
        "video_b": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=agents["video_b"].id, service_key="xhs", status="active"),
        "retired": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=agents["retired"].id, service_key="old", status="active"),
        "manor": AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=MANOR_AGENT_ID, status="active"),
    }
    db.add_all(subs.values())
    await db.commit()
    return workspace, agents, subs


async def _entity(client, name):
    response = await client.post("/api/v1/auth/register", json={
        "username": name, "email": f"{name}@example.com", "password": "securepass123", "entity_name": f"{name} Co",
    })
    assert response.status_code == 200, response.text
    return response.json()["entity_id"]


@pytest.mark.asyncio
async def test_targets_are_every_other_agent_by_the_name_people_see(client, db_session):
    entity_id = await _entity(client, "delegate_targets")
    workspace, agents, subs = await _workspace_with_agents(db_session, entity_id)

    targets = await delegation.runtime_workspace_delegation_targets(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
    )
    labels = {target["label"]: target for target in targets}
    assert set(labels) == {"Writer", "Video Operator (douyin)", "Video Operator (xhs)"}
    assert labels["Writer"]["agent_subscription_id"] == subs["writer"].id
    assert labels["Writer"]["summary"] == "Drafts posts and replies."

    as_writer = await delegation.runtime_workspace_delegation_targets(
        db_session, entity_id=entity_id, workspace_id=workspace.id, exclude_agent_id=agents["writer"].id,
    )
    assert "Writer" not in {target["label"] for target in as_writer}


@pytest.mark.asyncio
async def test_the_tool_lists_the_workspace_agents_or_disappears_without_any(client, db_session):
    entity_id = await _entity(client, "delegate_schema")
    workspace, _agents, _subs = await _workspace_with_agents(db_session, entity_id)
    generic = delegation.delegate_to_agent_schema()
    other = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}

    tools = await delegation.runtime_specialize_delegate_to_agent(
        db_session, [other, generic], entity_id=entity_id, workspace_id=workspace.id, agent_id=None,
    )
    schema = tools[1]["function"]
    assert schema["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"] == [
        "Video Operator (douyin)", "Video Operator (xhs)", "Writer",
    ]
    assert "- Writer: Drafts posts and replies." in schema["description"]
    # The same Workspace gives the same schema every turn (cacheable prefix).
    again = await delegation.runtime_specialize_delegate_to_agent(
        db_session, [other, generic], entity_id=entity_id, workspace_id=workspace.id, agent_id=None,
    )
    assert again == tools

    empty = Workspace(id=generate_ulid(), entity_id=entity_id, name="Empty", status="active")
    db_session.add(empty)
    await db_session.commit()
    for workspace_id in (empty.id, None):
        dropped = await delegation.runtime_specialize_delegate_to_agent(
            db_session, [other, generic], entity_id=entity_id, workspace_id=workspace_id, agent_id=None,
        )
        assert dropped == [other]


@pytest.mark.asyncio
async def test_handler_delegates_to_the_named_agent_through_delegate_service(client, db_session, monkeypatch):
    from packages.core.ai.tools import workspace_agent_tools

    entity_id = await _entity(client, "delegate_handler")
    workspace, agents, subs = await _workspace_with_agents(db_session, entity_id)
    delegate = AsyncMock(return_value=json.dumps({"delegated": True}))
    monkeypatch.setattr(workspace_agent_tools, "runtime_workspace_delegate_service_action", delegate)

    result = await workspace_agent_tools._delegate_to_agent_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        agent="Video Operator (xhs)", task="Plan three posts for the launch.",
        _agent_id_from_context=agents["writer"].id,
    )
    assert json.loads(result) == {"delegated": True}
    params = delegate.await_args.kwargs["params"]
    assert params["agent_subscription_id"] == subs["video_b"].id
    assert params["prompt"] == "Plan three posts for the launch."
    assert params["_agent_id_from_context"] == agents["writer"].id

    unknown = json.loads(await workspace_agent_tools._delegate_to_agent_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        agent="Writer", task="x", _agent_id_from_context=agents["writer"].id,
    ))
    assert unknown["error"] == "unknown_agent" and "Writer" not in unknown["available_agents"]
    assert delegate.await_count == 1


def test_delegation_beyond_two_levels_is_refused_without_running(monkeypatch):
    impl = AsyncMock(return_value=json.dumps({"delegated": True}))
    monkeypatch.setattr(delegation, "_runtime_workspace_delegate_service_action", impl)

    async def nested(levels: int) -> list[dict]:
        results = []

        async def call(remaining: int):
            result = json.loads(await delegation.runtime_workspace_delegate_service_action(
                entity_id="e", workspace_id="w", params={},
            ))
            results.append(result)
            if remaining:
                # A delegated agent delegating again happens inside the call.
                token = delegation._delegation_depth.set(delegation._delegation_depth.get() + 1)
                try:
                    await call(remaining - 1)
                finally:
                    delegation._delegation_depth.reset(token)

        await call(levels)
        return results

    results = asyncio.run(nested(2))
    assert results[:2] == [{"delegated": True}, {"delegated": True}]
    assert results[2]["error"] == "delegation_depth_limit"
    assert impl.await_count == 2
    # The depth is scoped to the call: a fresh top-level delegation works again.
    assert json.loads(asyncio.run(delegation.runtime_workspace_delegate_service_action(
        entity_id="e", workspace_id="w", params={},
    ))) == {"delegated": True}


def test_delegate_to_agent_is_governed_like_delegate_service():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    call = RuntimeCompositeToolCallFactory.create("delegate_to_agent", {"agent": "Writer", "task": "draft"})
    assert call.tool_name == "workspace_agent" and call.arguments["action"] == "delegate_service"
    gateway = classify_runtime_tool("delegate_to_agent", {"agent": "Writer", "task": "draft"})
    direct = classify_runtime_tool("workspace_agent", {"action": "delegate_service", "params": {"prompt": "draft"}})
    assert gateway.effect == direct.effect


def test_the_delegated_agent_is_told_who_called_it():
    from types import SimpleNamespace

    sub = SimpleNamespace(service_key="xhs", name=None, custom_prompt=None)
    agent = SimpleNamespace(name="Video Operator")
    assert "called by Manor AI" in delegation._service_prompt_appendix(sub, agent)
    peer = delegation._service_prompt_appendix(sub, agent, "the Workspace agent Writer")
    assert "called by the Workspace agent Writer" in peer and "relay to the user" in peer


def test_workspace_surfaces_load_delegate_to_agent():
    from packages.core.ai.runtime.tool_visibility import WORKSPACE_AGENT_TOOL_PROFILE, runtime_tool_surface_spec

    for is_master in (True, False):
        spec = runtime_tool_surface_spec(is_master=is_master, tool_profile=WORKSPACE_AGENT_TOOL_PROFILE)
        assert "delegate_to_agent" in spec.eager_tool_names
    assert "delegate_to_agent" not in runtime_tool_surface_spec(is_master=True).eager_tool_names


@pytest.mark.asyncio
async def test_workspace_chat_assembly_offers_the_workspace_agents(client, db_session):
    from packages.core.ai.runtime import AIRuntimeRequest
    from packages.core.ai.runtime.prompt_assembly import runtime_assemble_prompt_for_turn
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_visibility import WORKSPACE_AGENT_TOOL_PROFILE

    response = await client.post("/api/v1/auth/register", json={
        "username": "delegate_assembly", "email": "delegate_assembly@example.com",
        "password": "securepass123", "entity_name": "delegate_assembly Co",
    })
    entity_id, user_id = response.json()["entity_id"], response.json()["user_id"]
    workspace, _agents, _subs = await _workspace_with_agents(db_session, entity_id)

    assembled = await runtime_assemble_prompt_for_turn(
        db_session,
        request=AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
        ),
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE, is_master=True, active_user_message="who can help?",
    )
    schema = next(tool["function"] for tool in assembled.tool_schemas if tool["function"]["name"] == "delegate_to_agent")
    assert set(schema["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"]) == {
        "Writer", "Video Operator (douyin)", "Video Operator (xhs)",
    }
