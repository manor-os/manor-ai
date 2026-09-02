"""Strict Agent hosting for interactive Task sessions.

Interactive Tasks reuse Workspace Chat for their user-facing runtime, but the
Task remains the authority for who conducts the session.  This module keeps
that invariant separate from ordinary task threads, where an explicit
@mention or the most recent execution step may still choose the responder.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
from packages.core.constants.task import TaskType
from packages.core.models.task import Conversation, Task
from packages.core.models.workspace import Agent, AgentSubscription


class TaskSessionHostError(ValueError):
    """The interactive Task does not identify one valid Host Agent."""


@dataclass(frozen=True)
class TaskSessionHost:
    agent_id: str
    agent_subscription_id: str | None = None
    custom_prompt: str | None = None


def is_interactive_task(task: Task) -> bool:
    return task.task_type == TaskType.INTERACTIVE.value


def validate_new_task_session(task: Task) -> None:
    """Reject an invalid interactive Task before its first flush."""

    if not is_interactive_task(task):
        return
    if not task.workspace_id:
        raise _host_error(task, "a Workspace is required")
    if task.conversation_id:
        raise _host_error(
            task,
            "the session Conversation cannot be pre-bound at Task creation",
        )
    if not (
        task.agent_id
        or is_master_agent(task.agent_id, task.agent_type)
        or task.owner_subscription_id
        or task.owner_service_key
    ):
        raise _host_error(task, "assign an Agent or owner service first")


def _host_error(task: Task, detail: str) -> TaskSessionHostError:
    return TaskSessionHostError(
        f"Interactive Task {task.id} cannot start its session: {detail}"
    )


async def _validate_bound_conversation(
    db: AsyncSession,
    task: Task,
) -> None:
    if not task.workspace_id:
        raise _host_error(task, "a Workspace is required")

    if task.conversation_id:
        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.id == task.conversation_id,
                Conversation.entity_id == task.entity_id,
                Conversation.workspace_id == task.workspace_id,
                Conversation.scope == "workspace_thread",
                Conversation.thread_ref_kind == "task",
                Conversation.thread_ref_id == task.id,
            )
        )).scalar_one_or_none()
        if not conversation:
            raise _host_error(
                task,
                "the bound Conversation does not belong to this Task session",
            )


async def _active_subscriptions(
    db: AsyncSession,
    task: Task,
) -> list[AgentSubscription]:
    return list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == task.entity_id,
            AgentSubscription.workspace_id == task.workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())


async def resolve_task_session_host(
    db: AsyncSession,
    task: Task,
) -> TaskSessionHost | None:
    """Resolve the sole Host Agent for an interactive Task.

    Resolution is deliberately strict and never inspects execution steps or
    falls back to Manor AI.  Manor AI remains valid only when the Task names it
    explicitly.  A deployed Agent can be selected directly, by the Task's
    cached owner subscription, or by its owner service key.
    """

    if not is_interactive_task(task):
        return None
    if not task.workspace_id:
        raise _host_error(task, "a Workspace is required")
    await _validate_bound_conversation(db, task)

    if is_master_agent(task.agent_id, task.agent_type):
        if task.owner_subscription_id or task.owner_service_key:
            raise _host_error(
                task,
                "the explicit Manor AI host conflicts with a service owner",
            )
        return TaskSessionHost(agent_id=MANOR_AGENT_ID)

    subscriptions = await _active_subscriptions(db, task)
    candidates = subscriptions

    if task.owner_subscription_id:
        candidates = [
            sub for sub in subscriptions if sub.id == task.owner_subscription_id
        ]
        if not candidates:
            raise _host_error(task, "the owner Agent subscription is not active")
    elif task.owner_service_key:
        candidates = [
            sub for sub in subscriptions
            if sub.service_key == task.owner_service_key
        ]
        if not candidates:
            raise _host_error(task, "the owner service has no active Agent")
    elif task.agent_id:
        candidates = [sub for sub in subscriptions if sub.agent_id == task.agent_id]
        if not candidates:
            raise _host_error(task, "the assigned Agent is not active in this Workspace")
    else:
        raise _host_error(task, "assign an Agent or owner service first")

    if task.agent_id:
        candidates = [sub for sub in candidates if sub.agent_id == task.agent_id]
        if not candidates:
            raise _host_error(task, "the assigned Agent conflicts with the owner service")

    if len(candidates) != 1:
        raise _host_error(task, "the Host Agent assignment is ambiguous")

    subscription = candidates[0]
    agent = (await db.execute(
        select(Agent).where(
            Agent.id == subscription.agent_id,
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not agent:
        raise _host_error(task, "the Host Agent is unavailable")

    return TaskSessionHost(
        agent_id=subscription.agent_id,
        agent_subscription_id=subscription.id,
        custom_prompt=(subscription.custom_prompt or "").strip() or None,
    )


async def task_session_host_for_thread(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    thread_ref_kind: str | None,
    thread_ref_id: str | None,
) -> TaskSessionHost | None:
    """Resolve a Host only when the requested thread is an interactive Task."""

    if thread_ref_kind != "task" or not thread_ref_id or not workspace_id:
        return None
    task = (await db.execute(
        select(Task).where(
            Task.id == thread_ref_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if not task:
        raise TaskSessionHostError(
            f"Task is unavailable for Workspace thread {thread_ref_id}"
        )
    return await resolve_task_session_host(db, task)


async def task_session_host_for_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str | None,
    workspace_id: str | None = None,
) -> TaskSessionHost | None:
    """Resolve an interactive Task Host from a persisted Conversation."""

    filters = [Conversation.id == conversation_id]
    if entity_id:
        filters.append(Conversation.entity_id == entity_id)
    if workspace_id:
        filters.append(Conversation.workspace_id == workspace_id)
    conversation = (await db.execute(
        select(Conversation).where(*filters)
    )).scalar_one_or_none()
    if not conversation:
        raise LookupError("Conversation not found")
    return await task_session_host_for_thread(
        db,
        entity_id=conversation.entity_id,
        workspace_id=conversation.workspace_id,
        thread_ref_kind=conversation.thread_ref_kind,
        thread_ref_id=conversation.thread_ref_id,
    )


async def bind_task_session_conversation(
    db: AsyncSession,
    conversation: Conversation,
) -> TaskSessionHost | None:
    """Bind one interactive Task Conversation and cache its resolved Host."""

    if (
        conversation.thread_ref_kind != "task"
        or not conversation.thread_ref_id
        or not conversation.workspace_id
    ):
        return None
    task = (await db.execute(
        select(Task).where(
            Task.id == conversation.thread_ref_id,
            Task.entity_id == conversation.entity_id,
            Task.workspace_id == conversation.workspace_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if not task:
        raise TaskSessionHostError(
            f"Task is unavailable for Workspace thread {conversation.thread_ref_id}"
        )
    if not is_interactive_task(task):
        return None
    if task.conversation_id and task.conversation_id != conversation.id:
        raise _host_error(task, "a different Conversation is already bound")

    host = await resolve_task_session_host(db, task)
    if not host:
        return None

    task.conversation_id = conversation.id
    task.agent_id = host.agent_id
    if host.agent_subscription_id:
        task.owner_subscription_id = host.agent_subscription_id
    conversation.agent_id = host.agent_id
    conversation.agent_subscription_id = host.agent_subscription_id
    await db.flush()
    return host


async def delete_task_session_conversations(
    db: AsyncSession,
    task: Task,
) -> None:
    """Remove every durable Conversation owned by one interactive Task."""

    # Thread creation locks the Task before its check/create sequence. Take the
    # same lock before cleanup so a first turn cannot create a Conversation
    # after deletion has already taken its snapshot.
    locked_task = (await db.execute(
        select(Task).where(
            Task.id == task.id,
            Task.entity_id == task.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not locked_task:
        raise LookupError("Task not found")
    task = locked_task

    if not is_interactive_task(task) or not task.workspace_id:
        return

    conversation_ids = list((await db.execute(
        select(Conversation.id).where(
            Conversation.entity_id == task.entity_id,
            Conversation.workspace_id == task.workspace_id,
            Conversation.scope == "workspace_thread",
            Conversation.thread_ref_kind == "task",
            Conversation.thread_ref_id == task.id,
        )
    )).scalars().all())
    if not conversation_ids:
        return

    from packages.core.services.conversation_lifecycle import delete_conversation

    for conversation_id in conversation_ids:
        await delete_conversation(db, conversation_id, task.entity_id)
