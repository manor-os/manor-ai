from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_exact_subscription_binding_takes_precedence_over_agent_identity():
    from packages.core.ai.runtime.capability_bindings import (
        runtime_binding_owner_matches,
    )

    binding = {
        "owner_scope": "agent",
        "agent_id": "multi-role-agent",
        "agent_subscription_id": "teacher-subscription",
    }

    assert not runtime_binding_owner_matches(
        binding,
        agent_id="multi-role-agent",
        is_master=False,
        current_subscription_id="interview-subscription",
    )
    assert runtime_binding_owner_matches(
        binding,
        agent_id="multi-role-agent",
        is_master=False,
        current_subscription_id="teacher-subscription",
    )


@pytest.mark.asyncio
async def test_interactive_task_binds_its_host_agent_to_conversation(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services.task_session import bind_task_session_conversation

    db_session.add(Agent(
        id="session-host-agent",
        entity_id="session-entity",
        name="Interview Coach",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="session-host-subscription",
        entity_id="session-entity",
        workspace_id="session-workspace",
        agent_id="session-host-agent",
        service_key="interview_coach",
        status="active",
    ))
    task = Task(
        id="interactive-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="Practice the product interview",
        task_type="interactive",
        agent_id="session-host-agent",
        details={},
    )
    db_session.add(task)
    conversation = Conversation(
        id="interactive-conversation",
        entity_id="session-entity",
        workspace_id="session-workspace",
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id="interactive-task",
    )
    db_session.add(conversation)
    await db_session.flush()

    host = await bind_task_session_conversation(db_session, conversation)

    assert host is not None
    assert host.agent_id == "session-host-agent"
    assert host.agent_subscription_id == "session-host-subscription"
    assert conversation.agent_id == "session-host-agent"
    assert conversation.agent_subscription_id == "session-host-subscription"
    assert task.conversation_id == "interactive-conversation"


@pytest.mark.asyncio
async def test_workspace_message_binds_interactive_task_session(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.workspace_chat.service import post_message

    db_session.add(Agent(
        id="message-session-agent",
        entity_id="message-session-entity",
        name="Interview Coach",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="message-session-sub",
        entity_id="message-session-entity",
        workspace_id="message-session-workspace",
        agent_id="message-session-agent",
        service_key="interview_coach",
        status="active",
    ))
    task = Task(
        id="message-session-task",
        entity_id="message-session-entity",
        workspace_id="message-session-workspace",
        title="Interview from Workspace message",
        task_type="interactive",
        agent_id="message-session-agent",
        owner_subscription_id="message-session-sub",
        details={},
    )
    db_session.add(task)
    await db_session.flush()

    message = await post_message(
        db_session,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        body="Start the interview",
        thread_ref_kind="task",
        thread_ref_id=task.id,
        publish_event=False,
    )

    conversation = await db_session.get(Conversation, message.conversation_id)
    assert conversation is not None
    assert conversation.agent_id == "message-session-agent"
    assert conversation.agent_subscription_id == "message-session-sub"
    assert task.conversation_id == conversation.id


@pytest.mark.asyncio
async def test_interactive_task_creation_requires_workspace_and_host(db_session):
    from packages.core.services import task_service
    from packages.core.services.task_session import TaskSessionHostError

    with pytest.raises(TaskSessionHostError, match="Workspace is required"):
        await task_service.create_task(
            db_session,
            "session-entity",
            title="Broken interview",
            task_type="interactive",
        )

    with pytest.raises(TaskSessionHostError, match="assign an Agent"):
        await task_service.create_task(
            db_session,
            "session-entity",
            title="Still broken interview",
            task_type="interactive",
            workspace_id="session-workspace",
        )


@pytest.mark.asyncio
async def test_interactive_task_creation_rejects_a_source_conversation(db_session):
    from packages.core.services import task_service
    from packages.core.services.task_session import TaskSessionHostError

    with pytest.raises(TaskSessionHostError, match="cannot be pre-bound"):
        await task_service.create_task(
            db_session,
            "session-entity",
            title="Interview from workspace chat",
            task_type="interactive",
            workspace_id="session-workspace",
            agent_id="manor-master",
            agent_type="manor_agent",
            conversation_id="source-conversation",
        )


@pytest.mark.asyncio
async def test_workspace_action_keeps_source_chat_out_of_task_session(
    db_session,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.workspace_task_actions import (
        runtime_workspace_create_task_action,
    )
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    db_session.add(Workspace(
        id="source-session-workspace",
        entity_id="source-session-entity",
        name="Interview Workspace",
        status="active",
    ))
    db_session.add(Agent(
        id="source-session-agent",
        entity_id="source-session-entity",
        name="Interview Coach",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="source-session-sub",
        entity_id="source-session-entity",
        workspace_id="source-session-workspace",
        agent_id="source-session-agent",
        service_key="interview_coach",
        status="active",
    ))
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "async_session", session_factory)

    result = json.loads(await runtime_workspace_create_task_action(
        entity_id="source-session-entity",
        workspace_id="source-session-workspace",
        conversation_id="source-workspace-chat",
        actor_agent_id="source-session-agent",
        params={
            "title": "Mock product interview",
            "task_type": "interactive",
            "agent_id": "source-session-agent",
        },
    ))

    assert result["created"] is True
    assert result["dispatched"] is False
    task = (await db_session.execute(
        select(Task).where(Task.id == result["task"]["id"])
    )).scalar_one()
    assert task.conversation_id is None
    assert task.details["runtime_context"]["captured_from"]["conversation_id"] == (
        "source-workspace-chat"
    )


@pytest.mark.asyncio
async def test_generic_runtime_rejects_interactive_task_creation():
    from packages.core.ai.runtime.task_actions import runtime_create_task_action

    result = json.loads(await runtime_create_task_action(
        entity_id="session-entity",
        params={"title": "Interview", "task_type": "interactive"},
    ))

    assert result["error"] == "interactive_task_requires_workspace_host"


@pytest.mark.asyncio
async def test_owner_subscription_disambiguates_reused_host_agent(db_session):
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import task_service

    db_session.add(Agent(
        id="reused-session-agent",
        entity_id="session-entity",
        name="Reusable Coach",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id="reused-interview-sub",
            entity_id="session-entity",
            workspace_id="session-workspace",
            agent_id="reused-session-agent",
            service_key="interview_coach",
            status="active",
        ),
        AgentSubscription(
            id="reused-teacher-sub",
            entity_id="session-entity",
            workspace_id="session-workspace",
            agent_id="reused-session-agent",
            service_key="teacher",
            status="active",
        ),
    ])
    await db_session.flush()

    task = await task_service.create_task(
        db_session,
        "session-entity",
        title="Mock interview",
        task_type="interactive",
        workspace_id="session-workspace",
        agent_id="reused-session-agent",
        owner_subscription_id="reused-interview-sub",
    )

    assert task.owner_subscription_id == "reused-interview-sub"


@pytest.mark.asyncio
async def test_owner_service_is_pinned_to_one_session_host(db_session):
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import task_service

    db_session.add(Agent(
        id="service-session-agent",
        entity_id="session-entity",
        name="Service Coach",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="service-session-sub",
        entity_id="session-entity",
        workspace_id="session-workspace",
        agent_id="service-session-agent",
        service_key="interview_coach",
        status="active",
    ))
    await db_session.flush()

    task = await task_service.create_task(
        db_session,
        "session-entity",
        title="Service-owned interview",
        task_type="interactive",
        workspace_id="session-workspace",
        owner_service_key="interview_coach",
    )

    assert task.agent_id == "service-session-agent"
    assert task.owner_subscription_id == "service-session-sub"


@pytest.mark.asyncio
async def test_task_response_returns_exact_session_host_for_reused_agent(
    db_session,
):
    from apps.api.routers.tasks import _resolve_lookups, _to_response
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import task_service

    entity_id = "api-session-entity"
    workspace_id = "api-session-workspace"
    agent_id = "api-reused-host-agent"
    interview_sub_id = "api-interview-host-sub"
    teacher_sub_id = "api-teacher-host-sub"
    db_session.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Multi-role Coach",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id=interview_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            name="Product Interviewer",
            service_key="interview_coach",
            status="active",
        ),
        AgentSubscription(
            id=teacher_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            name="SQL Teacher",
            service_key="teacher",
            status="active",
        ),
    ])
    await db_session.flush()
    task = await task_service.create_task(
        db_session,
        entity_id,
        title="Product manager mock interview",
        task_type="interactive",
        workspace_id=workspace_id,
        agent_id=agent_id,
        agent_type="agent",
        owner_subscription_id=interview_sub_id,
    )
    lookups = await _resolve_lookups(db_session, [task])
    response = _to_response(task, *lookups)

    assert response.session_host_agent_id == agent_id
    assert response.session_host_subscription_id == interview_sub_id
    assert response.session_host_name == "Multi-role Coach"
    assert response.session_host_role_label == "Product Interviewer"
    assert response.session_host_available is True
    assert response.session_host_error is None


@pytest.mark.asyncio
async def test_task_response_does_not_fallback_when_session_host_is_inactive(
    db_session,
):
    from apps.api.routers.tasks import _resolve_lookups, _to_response
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription

    entity_id = "inactive-host-entity"
    workspace_id = "inactive-host-workspace"
    agent_id = "inactive-session-agent"
    subscription_id = "inactive-session-sub"
    db_session.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Former Interview Coach",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id=subscription_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        service_key="interview_coach",
        status="inactive",
    ))
    task = Task(
        id="inactive-host-task",
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Unavailable interview session",
        task_type="interactive",
        agent_id=agent_id,
        agent_type="agent",
        owner_subscription_id=subscription_id,
        details={},
    )
    db_session.add(task)
    await db_session.flush()

    lookups = await _resolve_lookups(db_session, [task])
    response = _to_response(task, *lookups)

    assert response.agent_name == "Former Interview Coach"
    assert response.session_host_name is None
    assert response.session_host_available is False
    assert response.session_host_error == "host_unavailable"


@pytest.mark.asyncio
async def test_task_response_fails_closed_for_conflicting_session_hosts(db_session):
    from apps.api.routers.tasks import _resolve_lookups, _to_response
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription

    entity_id = "conflicting-host-entity"
    workspace_id = "conflicting-host-workspace"
    db_session.add_all([
        Agent(
            id="assigned-session-agent",
            entity_id=entity_id,
            name="Assigned Agent",
            status="active",
        ),
        Agent(
            id="subscribed-session-agent",
            entity_id=entity_id,
            name="Subscribed Agent",
            status="active",
        ),
        AgentSubscription(
            id="conflicting-session-sub",
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id="subscribed-session-agent",
            service_key="interview_coach",
            status="active",
        ),
    ])
    conflicting_agent_task = Task(
        id="conflicting-agent-task",
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Conflicting Agent host",
        task_type="interactive",
        agent_id="assigned-session-agent",
        agent_type="agent",
        owner_subscription_id="conflicting-session-sub",
        details={},
    )
    conflicting_manor_task = Task(
        id="conflicting-manor-task",
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Conflicting Manor host",
        task_type="interactive",
        agent_id="manor-master",
        agent_type="manor_agent",
        owner_subscription_id="conflicting-session-sub",
        details={},
    )
    db_session.add_all([conflicting_agent_task, conflicting_manor_task])
    await db_session.flush()

    lookups = await _resolve_lookups(
        db_session,
        [conflicting_agent_task, conflicting_manor_task],
    )
    agent_response = _to_response(conflicting_agent_task, *lookups)
    manor_response = _to_response(conflicting_manor_task, *lookups)

    assert agent_response.session_host_name is None
    assert agent_response.session_host_available is False
    assert agent_response.session_host_error == "host_unavailable"
    assert manor_response.session_host_name is None
    assert manor_response.session_host_available is False
    assert manor_response.session_host_error == "host_unavailable"


@pytest.mark.asyncio
async def test_interactive_task_rejects_a_second_conversation(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.services.task_session import (
        TaskSessionHostError,
        bind_task_session_conversation,
    )

    task = Task(
        id="single-conversation-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="One interview session",
        task_type="interactive",
        agent_id="manor-master",
        agent_type="manor_agent",
        conversation_id="original-conversation",
        details={},
    )
    conversation = Conversation(
        id="second-conversation",
        entity_id="session-entity",
        workspace_id="session-workspace",
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id=task.id,
    )
    db_session.add_all([task, conversation])
    await db_session.flush()

    with pytest.raises(TaskSessionHostError, match="different Conversation"):
        await bind_task_session_conversation(db_session, conversation)


@pytest.mark.asyncio
async def test_task_thread_spawn_is_serialized_for_concurrent_first_turns(db_session):
    from packages.core.models.task import Task
    from packages.core.workspace_chat.service import spawn_thread

    task = Task(
        id="concurrent-session-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="Concurrent interview",
        task_type="interactive",
        agent_id="manor-master",
        agent_type="manor_agent",
        details={},
    )
    db_session.add(task)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def create_from_first_turn() -> str:
        async with session_factory() as session:
            conversation = await spawn_thread(
                session,
                entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                thread_ref_kind="task",
                thread_ref_id=task.id,
                title=task.title,
            )
            await session.commit()
            return conversation.id

    first_id, second_id = await asyncio.gather(
        create_from_first_turn(),
        create_from_first_turn(),
    )

    assert first_id == second_id


@pytest.mark.asyncio
async def test_task_session_delete_serializes_with_concurrent_first_turn(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.services.task_session import delete_task_session_conversations
    from packages.core.workspace_chat.service import spawn_thread

    task = Task(
        id="delete-race-session-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="Delete while opening interview",
        task_type="interactive",
        agent_id="manor-master",
        agent_type="manor_agent",
        details={},
    )
    db_session.add(task)
    await db_session.commit()

    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    spawn_started = asyncio.Event()

    async def create_from_first_turn() -> str:
        async with session_factory() as session:
            spawn_started.set()
            conversation = await spawn_thread(
                session,
                entity_id=task.entity_id,
                workspace_id=task.workspace_id,
                thread_ref_kind="task",
                thread_ref_id=task.id,
                title=task.title,
            )
            await session.commit()
            return conversation.id

    async with session_factory() as deletion_session:
        deleting_task = await deletion_session.get(Task, task.id)
        assert deleting_task is not None
        await delete_task_session_conversations(deletion_session, deleting_task)

        spawn_task = asyncio.create_task(create_from_first_turn())
        await spawn_started.wait()
        done, _ = await asyncio.wait({spawn_task}, timeout=0.1)
        assert not done, "first turn must wait for the Task deletion transaction"

        await deletion_session.delete(deleting_task)
        await deletion_session.commit()

    with pytest.raises(LookupError, match="Task not found"):
        await spawn_task

    assert (await db_session.execute(
        select(Conversation.id).where(
            Conversation.entity_id == task.entity_id,
            Conversation.workspace_id == task.workspace_id,
            Conversation.thread_ref_kind == "task",
            Conversation.thread_ref_id == task.id,
        )
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_interactive_task_without_host_fails_closed(db_session):
    from packages.core.models.task import Task
    from packages.core.services.task_session import (
        TaskSessionHostError,
        resolve_task_session_host,
    )

    task = Task(
        id="unhosted-interactive-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="Unhosted interview",
        task_type="interactive",
        details={},
    )
    db_session.add(task)
    await db_session.flush()

    with pytest.raises(TaskSessionHostError, match="assign an Agent"):
        await resolve_task_session_host(db_session, task)


@pytest.mark.asyncio
async def test_missing_task_thread_fails_closed(db_session):
    from packages.core.services.task_session import (
        TaskSessionHostError,
        task_session_host_for_thread,
    )

    with pytest.raises(TaskSessionHostError, match="Task is unavailable"):
        await task_session_host_for_thread(
            db_session,
            entity_id="session-entity",
            workspace_id="session-workspace",
            thread_ref_kind="task",
            thread_ref_id="deleted-session-task",
        )


@pytest.mark.asyncio
async def test_deleting_task_session_cancels_runs_and_removes_conversation(
    db_session,
):
    from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.services.task_session import (
        delete_task_session_conversations,
        task_session_host_for_conversation,
    )
    from packages.core.services.workspace_runtime import (
        load_conversation_runtime_context,
    )

    task = Task(
        id="deleted-session-task",
        entity_id="session-entity",
        workspace_id="session-workspace",
        title="Disposable interview",
        task_type="interactive",
        agent_id="manor-master",
        agent_type="manor_agent",
        conversation_id="deleted-session-conv",
        details={},
    )
    conversation = Conversation(
        id="deleted-session-conv",
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id=task.id,
    )
    message = Message(
        id="deleted-session-message",
        conversation_id=conversation.id,
        role="assistant",
        content="Question one",
    )
    root_run = RuntimeRun(
        id="deleted-session-root-run",
        root_run_id="deleted-session-root-run",
        conversation_id=conversation.id,
        entity_id=task.entity_id,
        user_id="deleted-session-user",
        status=RuntimeRunStatus.RUNNING.value,
        execution_payload={},
    )
    child_run = RuntimeRun(
        id="deleted-session-child-run",
        root_run_id=root_run.id,
        parent_run_id=root_run.id,
        conversation_id=conversation.id,
        entity_id=task.entity_id,
        user_id="deleted-session-user",
        status=RuntimeRunStatus.QUEUED.value,
        execution_payload={},
    )
    db_session.add_all([task, conversation, message, root_run, child_run])
    await db_session.flush()

    await delete_task_session_conversations(db_session, task)

    assert await db_session.get(Conversation, conversation.id) is None
    assert (
        await db_session.execute(
            select(Message.id).where(Message.id == message.id)
        )
    ).scalar_one_or_none() is None
    assert root_run.status == RuntimeRunStatus.CANCEL_REQUESTED.value
    assert child_run.status == RuntimeRunStatus.CANCELLED.value
    with pytest.raises(LookupError, match="Conversation not found"):
        await load_conversation_runtime_context(
            db_session,
            conversation_id=conversation.id,
            entity_id=task.entity_id,
        )
    with pytest.raises(LookupError, match="Conversation not found"):
        await task_session_host_for_conversation(
            db_session,
            conversation_id=conversation.id,
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
        )


@pytest.mark.asyncio
async def test_chat_request_agent_uses_bound_task_session_host(db_session):
    from apps.api.routers.chat import _resolve_task_session_request_agent
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription

    entity_id = "request-host-entity"
    workspace_id = "request-host-workspace"
    task_id = "request-host-task"
    conversation_id = "request-host-conversation"
    host_agent_id = "request-host-agent"
    db_session.add(Agent(
        id=host_agent_id,
        entity_id=entity_id,
        name="Interview Host",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="request-host-subscription",
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=host_agent_id,
        service_key="interview_host",
        status="active",
    ))
    db_session.add(Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Host-owned interview",
        task_type="interactive",
        agent_id=host_agent_id,
        owner_subscription_id="request-host-subscription",
        details={},
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id=task_id,
        agent_id=host_agent_id,
        agent_subscription_id="request-host-subscription",
    ))
    await db_session.flush()

    resolved_agent_id = await _resolve_task_session_request_agent(
        db_session,
        SimpleNamespace(entity_id=entity_id),
        requested_agent_id="request-selected-other-agent",
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        thread_ref_kind="task",
        thread_ref_id=task_id,
    )

    assert resolved_agent_id == host_agent_id


@pytest.mark.asyncio
async def test_unhosted_interactive_conversation_stops_before_runtime_resolution(
    db_session,
    monkeypatch,
):
    from packages.core.models.task import Conversation, Task
    from packages.core.services import runtime_chat_context, workspace_runtime
    from packages.core.services.task_session import TaskSessionHostError

    db_session.add(Task(
        id="unhosted-runtime-task",
        entity_id="unhosted-runtime-entity",
        workspace_id="unhosted-runtime-ws",
        title="Unhosted coaching",
        task_type="interactive",
        details={},
    ))
    db_session.add(Conversation(
        id="unhosted-runtime-conv",
        entity_id="unhosted-runtime-entity",
        workspace_id="unhosted-runtime-ws",
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id="unhosted-runtime-task",
    ))
    await db_session.flush()

    async def runtime_must_not_start(*_args, **_kwargs):
        raise AssertionError("runtime resolution must not start without a Host Agent")

    monkeypatch.setattr(
        workspace_runtime,
        "resolve_workspace_runtime",
        runtime_must_not_start,
    )

    with pytest.raises(TaskSessionHostError, match="assign an Agent"):
        await runtime_chat_context.resolve_runtime_chat_context(
            db_session,
            "Start",
            conversation_id="unhosted-runtime-conv",
        )


@pytest.mark.asyncio
async def test_runtime_context_ignores_requested_agent_for_interactive_task(
    db_session,
    monkeypatch,
):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import runtime_chat_context, workspace_runtime

    db_session.add(Agent(
        id="runtime-session-host",
        entity_id="runtime-session-entity",
        name="Teaching Agent",
        status="active",
    ))
    db_session.add(AgentSubscription(
        id="runtime-session-sub",
        entity_id="runtime-session-entity",
        workspace_id="runtime-session-workspace",
        agent_id="runtime-session-host",
        service_key="teacher",
        status="active",
    ))
    db_session.add(Task(
        id="runtime-interactive-task",
        entity_id="runtime-session-entity",
        workspace_id="runtime-session-workspace",
        title="Learn SQL",
        task_type="interactive",
        owner_subscription_id="runtime-session-sub",
        owner_service_key="teacher",
        details={},
    ))
    db_session.add(Conversation(
        id="runtime-session-conv",
        entity_id="runtime-session-entity",
        workspace_id="runtime-session-workspace",
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id="runtime-interactive-task",
    ))
    await db_session.flush()

    resolved_agents: list[str | None] = []

    async def fake_resolve_workspace_runtime(_db, **kwargs):
        resolved_agents.append(kwargs.get("agent_id"))
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id="runtime-session-workspace",
            task_id="runtime-interactive-task",
            thread_ref_kind="task",
            thread_ref_id="runtime-interactive-task",
            runtime_profile="workspace_operator",
            tool_profile="workspace_agent",
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_assemble(_db, *, request, **_kwargs):
        return SimpleNamespace(
            context=ChatContext(
                db=_db,
                agent_id=request.agent_id,
                workspace_id=request.workspace_id,
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                thread_ref_kind=request.thread_ref_kind,
                thread_ref_id=request.thread_ref_id,
            ),
            tool_schemas=[],
            prompt="session prompt",
        )

    monkeypatch.setattr(
        workspace_runtime,
        "resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(
        runtime_chat_context,
        "runtime_assemble_prompt_for_turn",
        fake_assemble,
    )
    monkeypatch.setattr(
        runtime_chat_context,
        "runtime_auto_skill_forced_tool_calls",
        lambda *_args, **_kwargs: _empty_async_list(),
    )
    monkeypatch.setattr(
        runtime_chat_context,
        "load_conversation_history",
        lambda *_args, **_kwargs: _empty_async_list(),
    )

    _prompt, _tools, _history, context = await (
        runtime_chat_context.resolve_runtime_chat_context(
            db_session,
            "Start the lesson",
            agent_id="request-selected-other-agent",
            conversation_id="runtime-session-conv",
        )
    )

    assert resolved_agents == ["runtime-session-host"]
    assert context.agent_id == "runtime-session-host"


@pytest.mark.asyncio
async def test_task_session_subscription_limits_workspace_role_tools(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    entity_id = "role-scope-entity"
    workspace_id = "role-scope-workspace"
    agent_id = "multi-role-session-agent"
    interview_sub_id = "interview-role-sub"
    teacher_sub_id = "teacher-role-sub"
    db_session.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Role-scoped sessions",
        status="active",
        operating_model={
            "capability_bindings": [
                {
                    "owner_scope": "service",
                    "owner_service_key": "interview_coach",
                    "capability_type": "tool",
                    "tool_name": "interview_scorecard",
                },
                {
                    "owner_scope": "service",
                    "owner_service_key": "teacher",
                    "capability_type": "tool",
                    "tool_name": "grade_sql_homework",
                },
                {
                    "owner_scope": "agent",
                    "agent_subscription_id": teacher_sub_id,
                    "capability_type": "tool",
                    "tool_name": "teacher_private_notes",
                },
            ],
        },
    ))
    db_session.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Multi-role Coach",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id=interview_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="interview_coach",
            status="active",
        ),
        AgentSubscription(
            id=teacher_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="teacher",
            status="active",
        ),
    ])
    db_session.add(Task(
        id="role-session-task",
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Product interview",
        task_type="interactive",
        agent_id=agent_id,
        owner_subscription_id=interview_sub_id,
        details={},
    ))
    db_session.add(Conversation(
        id="role-session-conv",
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id="role-session-task",
        agent_id=agent_id,
        agent_subscription_id=interview_sub_id,
    ))
    await db_session.flush()

    runtime = await resolve_workspace_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        conversation_id="role-session-conv",
        agent_id=agent_id,
        is_master=False,
    )

    assert "interview_scorecard" in (runtime.bound_tool_names or set())
    assert "grade_sql_homework" not in (runtime.bound_tool_names or set())
    assert "teacher_private_notes" not in (runtime.bound_tool_names or set())


@pytest.mark.asyncio
async def test_task_session_subscription_limits_workspace_role_skills(db_session):
    from packages.core.models.skill import Skill
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.skill_service import list_skills_for_agent

    entity_id = "role-skill-entity"
    workspace_id = "role-skill-workspace"
    agent_id = "role-skill-agent"
    interview_sub_id = "role-skill-interview-sub"
    teacher_sub_id = "role-skill-teacher-sub"
    interview_skill_id = "interview-score-skill"
    teacher_skill_id = "teacher-homework-skill"
    db_session.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Role-scoped skills",
        status="active",
        operating_model={
            "skill_bindings": [
                {
                    "owner_scope": "service",
                    "owner_service_key": "interview_coach",
                    "skill_id": interview_skill_id,
                },
                {
                    "owner_scope": "service",
                    "owner_service_key": "teacher",
                    "skill_id": teacher_skill_id,
                },
            ],
        },
    ))
    db_session.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Multi-role Skill Coach",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id=interview_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="interview_coach",
            status="active",
        ),
        AgentSubscription(
            id=teacher_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="teacher",
            status="active",
        ),
        Skill(
            id=interview_skill_id,
            entity_id=entity_id,
            name="Interview scorecard",
            slug="session_interview_scorecard",
            system_prompt="Score the interview answer.",
            is_public=False,
            status="active",
        ),
        Skill(
            id=teacher_skill_id,
            entity_id=entity_id,
            name="Teacher homework",
            slug="session_teacher_homework",
            system_prompt="Grade the homework.",
            is_public=False,
            status="active",
        ),
    ])
    await db_session.flush()

    skills = await list_skills_for_agent(
        db_session,
        entity_id,
        agent_id,
        workspace_id=workspace_id,
        agent_subscription_id=interview_sub_id,
    )
    skill_ids = {skill.id for skill in skills}

    assert interview_skill_id in skill_ids
    assert teacher_skill_id not in skill_ids


@pytest.mark.asyncio
async def test_reassigning_task_session_host_rebinds_its_conversation(db_session):
    from packages.core.models.task import Conversation, Task
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.services import task_service

    entity_id = "rebind-session-entity"
    workspace_id = "rebind-session-workspace"
    agent_id = "rebind-multi-role-agent"
    old_sub_id = "rebind-interview-sub"
    new_sub_id = "rebind-teacher-sub"
    db_session.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Multi-role Coach",
        status="active",
    ))
    db_session.add_all([
        AgentSubscription(
            id=old_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="interview_coach",
            status="active",
        ),
        AgentSubscription(
            id=new_sub_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            service_key="teacher",
            status="active",
        ),
    ])
    task = Task(
        id="rebind-task",
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Change session role",
        task_type="interactive",
        agent_id=agent_id,
        owner_subscription_id=old_sub_id,
        conversation_id="rebind-session-conv",
        details={},
    )
    conversation = Conversation(
        id="rebind-session-conv",
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_thread",
        thread_ref_kind="task",
        thread_ref_id=task.id,
        agent_id=agent_id,
        agent_subscription_id=old_sub_id,
    )
    db_session.add_all([task, conversation])
    await db_session.flush()

    updated = await task_service.update_task(
        db_session,
        task.id,
        entity_id,
        agent_id=agent_id,
        agent_type="agent",
        owner_subscription_id=new_sub_id,
        owner_service_key=None,
    )

    assert updated is not None
    assert updated.owner_subscription_id == new_sub_id
    assert conversation.agent_id == agent_id
    assert conversation.agent_subscription_id == new_sub_id


async def _empty_async_list():
    return []
