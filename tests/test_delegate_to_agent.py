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


async def _single_agent_workspace(db, entity_id):
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Solo", status="active")
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Writer", status="active")
    db.add_all([workspace, agent])
    if await db.get(Agent, MANOR_AGENT_ID) is None:
        db.add(Agent(id=MANOR_AGENT_ID, name="Manor AI", status="active"))
    await db.flush()
    db.add(AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id))
    await db.commit()
    return workspace, agent


@pytest.mark.asyncio
async def test_a_lone_agent_can_ask_manor_ai_without_it_joining_the_workspace(client, db_session, monkeypatch):
    from sqlalchemy import select

    from packages.core.ai.tools import workspace_agent_tools

    entity_id = await _entity(client, "delegate_manor_review")
    workspace, agent = await _single_agent_workspace(db_session, entity_id)
    other = {"type": "function", "function": {"name": "read_file"}}
    generic = delegation.delegate_to_agent_schema()

    tools = await delegation.runtime_specialize_delegate_to_agent(
        db_session, [other, generic], entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id,
    )
    assert tools[1]["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"] == [
        "Manor AI",
    ]
    # Manor AI does not ask itself.
    as_manor = await delegation.runtime_specialize_delegate_to_agent(
        db_session, [other, generic], entity_id=entity_id, workspace_id=workspace.id, agent_id=MANOR_AGENT_ID,
    )
    assert as_manor[1]["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"] == [
        "Writer",
    ]

    delegate = AsyncMock(return_value=json.dumps({"delegated": True}))
    monkeypatch.setattr(workspace_agent_tools, "runtime_workspace_delegate_service_action", delegate)
    result = await workspace_agent_tools._delegate_to_agent_handler(
        entity_id=entity_id, user_id="user-1", workspace_id=workspace.id, conversation_id="conv-1",
        agent="Manor AI", task="Review this video against its brief.", _agent_id_from_context=agent.id,
    )
    assert json.loads(result) == {"delegated": True}

    # A lone task names Manor AI; its child resolves the identity when it starts.
    assert delegate.await_args.kwargs["params"]["agent_id"] == MANOR_AGENT_ID
    # Parallel tasks are pinned first, to the same identity.
    batch = await delegation.runtime_delegation_batch(
        entity_id=entity_id, workspace_id=workspace.id,
        tasks=[{"agent": "Manor AI", "task": "Review this video against its brief."}],
        shared={}, caller_agent_id=agent.id,
    )
    assert batch.error is None and batch.items[0]["agent_id"] == MANOR_AGENT_ID
    subscription_id = batch.items[0]["params"]["agent_subscription_id"]
    manor = await db_session.get(AgentSubscription, subscription_id)
    assert manor.agent_id == MANOR_AGENT_ID and manor.workspace_id is None
    members = (await db_session.execute(
        select(AgentSubscription.agent_id).where(AgentSubscription.workspace_id == workspace.id)
    )).scalars().all()
    assert members == [agent.id]


@pytest.mark.asyncio
async def test_manor_ai_only_reads_when_it_reviews(client, db_session, monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai import context
    from packages.core.ai.runtime.approval_classifier import _runtime_tool_is_explicitly_read_only
    from packages.core.ai.runtime.envelope import RuntimeEnvelope, runtime_effective_tool_scope
    from packages.core.ai.runtime.human_input import REQUEST_INPUT_TOOL_NAME
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.runtime_principal import RuntimePrincipalKind

    entity_id = await _entity(client, "delegate_manor_reads")
    workspace, agent = await _single_agent_workspace(db_session, entity_id)
    write_tool = {"type": "function", "function": {"name": "write_file"}}

    async def build_context(*_args, **kwargs):
        return SimpleNamespace(
            tools=[write_tool],
            allowed_tool_names={"write_file", "search_tools"},
            extra_system_prompt=kwargs["extra_system_prompt"],
            runtime_envelope=RuntimeEnvelope(
                surface=ChatSurface.WORKSPACE_CHAT,
                principal=RuntimePrincipal(kind=RuntimePrincipalKind.AGENT),
                profile=RuntimeProfile.WORKSPACE_OPERATOR,
                tool_names=("write_file", "search_tools"),
                allowed_tool_names=("write_file", "search_tools"),
                mcp_scope_unrestricted=True,
            ),
        )

    monkeypatch.setattr(context, "build_agent_context", build_context)

    async def setup(**raw_params):
        return await delegation.runtime_delegated_child_setup(
            db_session, entity_id=entity_id, workspace_id=workspace.id, user_id="user-1",
            conversation_id=None, task_id="task-1", step_id="step-1",
            raw_params={"prompt": "Review it.", **raw_params}, prompt="Review it.", caller_agent_id=agent.id,
        )

    review = await setup(agent_id=MANOR_AGENT_ID)
    assert review.error is None
    names = set(delegation.MANOR_REVIEW_TOOL_NAMES)
    assert {tool["function"]["name"] for tool in review.tools} == names
    assert set(review.allowed_tool_names) == names and REQUEST_INPUT_TOOL_NAME not in names
    assert review.max_rounds <= delegation.MANOR_REVIEW_MAX_ROUNDS
    assert all(_runtime_tool_is_explicitly_read_only(name, {}) for name in names)
    # What execution actually enforces, not just what the model is shown.
    scope = runtime_effective_tool_scope(runtime_envelope=review.ctx.runtime_envelope)
    assert scope.allowed_tool_names == frozenset(names)
    assert review.ctx.runtime_envelope.mcp_scope_unrestricted is False
    assert "pass or fail" in review.ctx.extra_system_prompt

    # The pinned identity a parallel call resolves again gets the same limits.
    pinned = await setup(agent_subscription_id=review.sub.id)
    assert pinned.error is None and set(pinned.allowed_tool_names) == names

    # Only this entity's shared Manor AI identity is reachable that way.
    other_entity = await _entity(client, "delegate_manor_other")
    foreign = AgentSubscription(id=generate_ulid(), entity_id=other_entity, agent_id=MANOR_AGENT_ID)
    personal = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, agent_id=MANOR_AGENT_ID, owner_user_id="user-2", name="Personal",
    )
    db_session.add_all([foreign, personal])
    await db_session.commit()
    for subscription in (foreign, personal):
        refused = await setup(agent_subscription_id=subscription.id)
        assert json.loads(refused.error)["error"] == "workspace_service_agent_not_found"

    # A name-based lookup must honor cancellation just as its pinned id does.
    review.sub.status = "cancelled"
    await db_session.commit()
    for selector in ({"agent_id": MANOR_AGENT_ID}, {"agent_subscription_id": review.sub.id}):
        refused = await setup(**selector)
        assert json.loads(refused.error)["error"] == "workspace_service_agent_not_found"
