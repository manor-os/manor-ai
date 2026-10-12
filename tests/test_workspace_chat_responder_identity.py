"""Operator-chat identity is per turn, not inherited from channel bindings.

These tests use ORM-shaped in-memory rows. No database, Redis, model provider,
filesystem provisioning, or live Workspace state is accessed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.prompt_adapter import ChatContext
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.document import Channel
from packages.core.models.task import Conversation, Task
from packages.core.models.workspace import Agent, AgentSubscription
from packages.core.services import runtime_chat_context, workspace_runtime


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry():
    # Override the suite's Redis cleanup: this module has no sandbox runtime.
    yield


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _IdentityDB:
    def __init__(self, rows):
        self.rows = rows
        self.queried_models = []

    async def execute(self, statement):
        model = statement.column_descriptions[0]["entity"]
        self.queried_models.append(model)
        assert model in self.rows, f"Unexpected identity query: {model}"
        return _Rows(self.rows[model])


@pytest.fixture
def identity_runtime(monkeypatch):
    agent = Agent(
        id="production-agent", entity_id="entity", status="active",
        name="Short-Form Video Production Planner",
    )
    subscription = AgentSubscription(
        id="production-subscription", entity_id="entity", workspace_id="workspace",
        agent_id=agent.id, service_key="production_planning", status="active",
    )
    conversation = Conversation(
        id="conversation", entity_id="entity", workspace_id="workspace",
        scope="workspace_main", channel="workspace", status="active",
    )
    channel = Channel(
        id="internal-channel", entity_id="entity", workspace_id="workspace",
        type="internal_chat", status="active", agent_id=agent.id,
        agent_subscription_id=subscription.id,
        config={"linked_service_key": "production_planning"},
    )
    db = _IdentityDB({
        Conversation: [conversation], Agent: [agent],
        AgentSubscription: [subscription], Task: [], Channel: [channel],
    })
    assembled_calls = []
    tool_scope_calls = []

    async def tool_scope(_db, *, agent_id, is_master, **_kwargs):
        tool_scope_calls.append((agent_id, is_master))
        return {"manor_tool" if is_master else "specialist_tool"}, set(), ()

    async def assemble(_db, *, request, **kwargs):
        assembled_calls.append((request, kwargs))
        return SimpleNamespace(
            context=ChatContext(
                db=_db, agent_id=request.agent_id,
                agent=agent if request.agent_id == agent.id else None,
                entity_id=request.entity_id, workspace_id=request.workspace_id,
                conversation_id=request.conversation_id, task_id=request.task_id,
                thread_ref_kind=request.thread_ref_kind,
                thread_ref_id=request.thread_ref_id,
            ),
            tool_schemas=[], prompt="identity-test prompt",
            envelope=SimpleNamespace(metadata={}, principal=SimpleNamespace(actor_user_id="user")),
        )

    history = [{"role": "assistant", "content": "I am the production planner."}]
    monkeypatch.setattr(workspace_runtime, "_resolve_agent_tool_scope", tool_scope)
    monkeypatch.setattr(workspace_runtime, "_agent_has_visible_skills", AsyncMock(return_value=False))
    monkeypatch.setattr(workspace_runtime, "_resolve_workspace_operation_tool_scope", AsyncMock(return_value=(set(), set(), set())))
    monkeypatch.setattr(workspace_runtime, "_pending_hitl_extra_context", AsyncMock(return_value=None))
    monkeypatch.setattr(workspace_runtime, "user_can_write_workspace_artifacts", AsyncMock(return_value=True))
    monkeypatch.setattr(runtime_chat_context, "runtime_assemble_prompt_for_turn", assemble)
    monkeypatch.setattr(runtime_chat_context, "load_conversation_history", AsyncMock(return_value=history))
    # Identity only: keep history on the rendered path this fake DB answers.
    monkeypatch.setattr(
        "packages.core.services.conversation_transcript.transcript_scope_for_turn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runtime_chat_context, "_recent_chat_capability_hints", AsyncMock(return_value=[]))
    monkeypatch.setattr(runtime_chat_context, "runtime_auto_skill_forced_tool_calls", AsyncMock(return_value=[]))
    monkeypatch.setattr("packages.core.services.agent_files.ensure_agent_workspace", lambda *_args: None)
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_model_for_user", AsyncMock(return_value="test-model"))
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_llm_metadata_for_user", AsyncMock(return_value={}))
    monkeypatch.setattr("packages.core.services.tool_path_memory.lookup_paths", AsyncMock(return_value=[]))

    async def resolve(agent_id=None, *, surface=ChatSurface.WORKSPACE_CHAT, channel_context=None):
        return await runtime_chat_context.resolve_runtime_chat_context(
            db, "What can this workspace do?", entity_id="entity", user_id="user",
            workspace_id="workspace", conversation_id=conversation.id,
            agent_id=agent_id, runtime_surface=surface, channel_context=channel_context,
        )

    return SimpleNamespace(
        db=db, agent=agent, subscription=subscription, conversation=conversation,
        channel=channel, history=history, resolve=resolve,
        assembled_calls=assembled_calls, tool_scope_calls=tool_scope_calls,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_conversation_agent", [None, "production-agent"])
async def test_workspace_main_defaults_to_manor_despite_specialist_channel_and_history(
    identity_runtime, legacy_conversation_agent,
):
    scope = identity_runtime
    scope.conversation.agent_id = legacy_conversation_agent

    _prompt, _tools, history, context = await scope.resolve()

    request, assembly = scope.assembled_calls[-1]
    assert context.agent_id is None
    assert request.agent_id is None
    assert request.workspace_id == "workspace"
    assert request.surface == ChatSurface.WORKSPACE_CHAT
    assert assembly["is_master"] is True
    assert scope.tool_scope_calls == [(None, True)]
    assert "manor_tool" in assembly["bound_tool_names"]
    assert history == scope.history  # History remains readable, not an identity grant.
    assert Channel not in scope.db.queried_models
    assert scope.channel.agent_id == scope.agent.id  # No channel rebinding needed.


@pytest.mark.asyncio
async def test_workspace_explicit_agent_is_one_turn_only(identity_runtime):
    scope = identity_runtime

    *_, specialist = await scope.resolve(scope.agent.id)
    assert specialist.agent_id == scope.agent.id
    assert scope.assembled_calls[-1][1]["is_master"] is False
    assert "specialist_tool" in scope.assembled_calls[-1][1]["bound_tool_names"]

    *_, manor = await scope.resolve()
    assert manor.agent_id is None
    assert scope.assembled_calls[-1][1]["is_master"] is True
    assert scope.tool_scope_calls == [(scope.agent.id, False), (None, True)]
    assert scope.conversation.agent_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_agent", [None, "another-mentioned-agent"])
async def test_interactive_task_still_owns_its_host(identity_runtime, requested_agent):
    scope = identity_runtime
    scope.conversation.scope = "workspace_thread"
    scope.conversation.thread_ref_kind = "task"
    scope.conversation.thread_ref_id = "interactive-task"
    scope.db.rows[Task] = [Task(
        id="interactive-task", entity_id="entity", workspace_id="workspace",
        title="Production coaching", task_type="interactive", details={},
        owner_subscription_id=scope.subscription.id,
    )]

    *_, context = await scope.resolve(requested_agent)

    request, assembly = scope.assembled_calls[-1]
    assert context.agent_id == scope.agent.id
    assert request.task_id == "interactive-task"
    assert request.metadata["agent_subscription_id"] == scope.subscription.id
    assert assembly["is_master"] is False
    assert scope.tool_scope_calls == [(scope.agent.id, False)]


@pytest.mark.asyncio
async def test_external_channel_keeps_its_explicit_agent(identity_runtime):
    scope = identity_runtime
    scope.channel.type = "email"
    scope.conversation.scope = "channel"
    scope.conversation.channel = "email"
    scope.conversation.agent_id = scope.agent.id

    *_, context = await scope.resolve(
        scope.agent.id, surface=ChatSurface.EXTERNAL_CHANNEL_CHAT,
        channel_context={"channel_type": "email", "source_id": "email-channel"},
    )

    request, assembly = scope.assembled_calls[-1]
    assert context.agent_id == scope.agent.id
    assert request.surface == ChatSurface.EXTERNAL_CHANNEL_CHAT
    assert assembly["is_master"] is False
    assert scope.tool_scope_calls == [(scope.agent.id, False)]
