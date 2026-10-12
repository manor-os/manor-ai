"""Task requirements do not create Agent or Host execution grants."""
from __future__ import annotations

import pytest

from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.task import Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.runtime_authorization import authorize_runtime_action
from packages.core.services.skill_service import list_skills_for_agent
from packages.core.services.workspace_runtime import resolve_workspace_runtime


async def _scope(db, *, operating_model=None, required_capabilities=None):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Runtime scope boundary",
        operating_model=operating_model or {},
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Service Agent", status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, agent_id=agent.id,
        workspace_id=workspace.id, service_key="research", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Prepare report", status="in_progress", owner_service_key="research",
        delegate_service_keys=["research"],
        details={"runtime_context": {"required_capabilities": required_capabilities or []}},
    )
    db.add_all([workspace, agent, subscription, task])
    await db.flush()
    return entity_id, workspace, agent, subscription, task


@pytest.mark.asyncio
async def test_task_requirement_does_not_grant_unbound_agent_tool(db_session):
    entity_id, workspace, agent, subscription, task = await _scope(
        db_session, required_capabilities=["file.write"],
    )
    own_tool = ToolDefinition(
        id=generate_ulid(), name="test_agent_own_tool", status="active",
    )
    db_session.add(own_tool)
    db_session.add(AgentToolBinding(agent_id=agent.id, tool_id=own_tool.id))
    await db_session.flush()

    runtime = await resolve_workspace_runtime(
        db_session, entity_id=entity_id, agent_id=agent.id,
        agent_subscription_id=subscription.id, workspace_id=workspace.id,
        task_id=task.id, is_master=False, include_prompt_context=False,
    )
    assert "test_agent_own_tool" in (runtime.bound_tool_names or set())
    assert "generate_file" not in (runtime.bound_tool_names or set())
    assert "file.write" not in runtime.capability_ids

    # A stale or forged loop allowlist is not sufficient at the execution gate.
    decision = await authorize_runtime_action(
        db_session, entity_id=entity_id, user_id=None, workspace_id=workspace.id,
        task_id=task.id, principal_kind="delegated", principal_agent_id=agent.id,
        tool_name="generate_file", bound_tool_names={"generate_file"},
        action_key="", capability_id="", access="write",
        runtime_surface=ChatSurface.SCHEDULED_AGENT_RUN,
    )
    assert not decision.allowed


@pytest.mark.asyncio
async def test_task_owned_binding_is_not_grant_but_deployment_bindings_remain(db_session):
    entity_id, workspace, agent, subscription, task = await _scope(db_session)
    workspace.operating_model = {
        "capability_bindings": [
            {"owner_scope": "task", "task_id": task.id, "capability_type": "tool", "tool_name": "test_task_tool"},
            {"owner_scope": "service", "owner_service_key": "research", "capability_type": "tool", "tool_name": "test_service_tool"},
            {"owner_scope": "agent", "agent_id": agent.id, "capability_type": "tool", "tool_name": "test_agent_tool"},
            {"owner_scope": "workspace_agent", "capability_type": "tool", "tool_name": "test_host_tool"},
            {"owner_scope": "service", "owner_service_key": "research", "capability_type": "mcp", "integration_key": "mail", "allowed_tools": ["read"]},
        ],
        "skill_bindings": [
            {"owner_scope": "task", "task_id": task.id, "skill_id": "test_task_skill"},
        ],
    }
    await db_session.flush()

    child = await resolve_workspace_runtime(
        db_session, entity_id=entity_id, agent_id=agent.id,
        agent_subscription_id=subscription.id, workspace_id=workspace.id,
        task_id=task.id, is_master=False, include_prompt_context=False,
    )
    host = await resolve_workspace_runtime(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        task_id=task.id, is_master=True, include_prompt_context=False,
    )
    assert {"test_service_tool", "test_agent_tool"} <= set(child.bound_tool_names or ())
    assert "test_task_tool" not in set(child.bound_tool_names or ())
    assert "test_host_tool" not in set(child.bound_tool_names or ())
    assert "mcp__mail__read" in set(child.mcp_allowed_names or ())
    assert "test_host_tool" in set(host.bound_tool_names or ())
    assert not {"test_task_tool", "test_service_tool", "test_agent_tool"} & set(host.bound_tool_names or ())
    assert "mcp__mail__read" not in set(host.mcp_allowed_names or ())
    assert not host.mcp_scope_unrestricted
    assert host.service_agent_ids == [agent.id]


@pytest.mark.asyncio
async def test_task_owned_private_skill_is_not_agent_skill_grant(db_session):
    entity_id, workspace, agent, subscription, task = await _scope(db_session)
    task_skill = Skill(
        id=generate_ulid(), entity_id=entity_id, name="Task skill",
        slug="test_task_skill", description="Task-only skill",
        system_prompt="Test", status="active", is_public=False,
    )
    service_skill = Skill(
        id=generate_ulid(), entity_id=entity_id, name="Service skill",
        slug="test_service_skill", description="Service skill",
        system_prompt="Test", status="active", is_public=False,
    )
    db_session.add_all([task_skill, service_skill])
    workspace.operating_model = {
        "skill_bindings": [
            {"owner_scope": "task", "task_id": task.id, "skill_id": task_skill.id},
            {"owner_scope": "service", "owner_service_key": "research", "skill_id": service_skill.id},
        ],
    }
    await db_session.flush()

    skills = await list_skills_for_agent(
        db_session, entity_id, agent.id, workspace_id=workspace.id,
        agent_subscription_id=subscription.id,
    )
    ids = {skill.id for skill in skills}
    assert service_skill.id in ids
    assert task_skill.id not in ids
