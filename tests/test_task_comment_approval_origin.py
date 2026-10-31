"""Foreground Task comments keep Task policy without borrowing worker grants."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.approval_settings_test_utils import (
    action_rule, initialize_test_approval_owners, set_workspace_test_approval,
)
from sqlalchemy import func, select

from packages.core.ai.runtime.approval_service import (
    guard_runtime_tool_action,
    resolve_runtime_approval_turn,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import (
    Agent, AgentSubscription, AgentToolBinding, ToolDefinition, Workspace,
)
from packages.core.services.workspace_runtime import ensure_workspace_task_conversation


TOOL = "mcp__twitter_x__create_tweet"
ARGUMENTS = {"text": "A reviewed test-only draft"}


async def _scope(db, *, canonical_thread=False, paused=False):
    entity_id, user_id, workspace_id = (generate_ulid() for _ in range(3))
    db.add(Entity(id=entity_id, name="Task comment origin test", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
        password_hash="x", role="owner", status="active",
    ))
    db.add(UserMembership(
        user_id=user_id, entity_id=entity_id, role="owner",
        status="active", is_primary=True,
    ))
    db.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="Task comments",
        status="paused" if paused else "active", operating_model={},
    ))
    agent_id = generate_ulid()
    db.add(Agent(id=agent_id, entity_id=entity_id, name="Test publisher", status="active"))
    db.add(AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent_id, name="Test publisher", status="active",
    ))
    tool = (await db.execute(select(ToolDefinition).where(ToolDefinition.name == TOOL))).scalar_one_or_none()
    if tool is None:
        tool = ToolDefinition(id=generate_ulid(), name=TOOL, status="active")
        db.add(tool)
    db.add(AgentToolBinding(agent_id=agent_id, tool_id=tool.id))
    main = Conversation(
        id=generate_ulid(), entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, scope="workspace_main", channel="workspace",
        title="Creation source",
    )
    db.add(main)
    await db.flush()
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        conversation_id=main.id, creator_id=user_id, title="General task", agent_id=agent_id,
        task_type="general", status="in_progress", details={},
    )
    db.add(task)
    await db.flush()
    thread = await ensure_workspace_task_conversation(
        db, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task.id, title=task.title,
    )
    if canonical_thread:
        task.conversation_id = thread.id
    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db, workspace, user_id=user_id)
    await set_workspace_test_approval(
        db, entity_id=entity_id, workspace_id=workspace_id,
        actor_id=user_id, user_rules=[action_rule("social_post.publish", effect="ask")],
    )
    await db.commit()
    envelope = RuntimeEnvelope(
        surface=ChatSurface.TASK_COMMENT_THREAD,
        profile=RuntimeProfile.TASK_WORKER_FEEDBACK,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER,
            entity_id=entity_id, actor_user_id=user_id, execution_user_id=user_id,
            agent_id=agent_id, workspace_id=workspace_id,
        ),
        entity_id=entity_id, user_id=user_id, agent_id=agent_id,
        workspace_id=workspace_id, conversation_id=thread.id, task_id=task.id,
        thread_ref_kind="task", thread_ref_id=task.id,
        allowed_tool_names=(TOOL,),
    )
    return task, thread, envelope


async def _guard(envelope, *, arguments=None, runtime_envelope=True):
    return await guard_runtime_tool_action(
        name=TOOL, arguments=dict(ARGUMENTS if arguments is None else arguments),
        entity_id=envelope.entity_id, user_id=envelope.user_id,
        workspace_id=envelope.workspace_id, conversation_id=envelope.conversation_id,
        task_id=envelope.task_id,
        operation_id=f"test:task-comment:{envelope.conversation_id}",
        runtime_envelope=envelope if runtime_envelope else None,
    )


async def _resolve(db, envelope, token, *, action="approve"):
    result = await resolve_runtime_approval_turn(
        db, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, hitl_id=token, action=action,
    )
    await db.commit()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_thread", [False, True])
@pytest.mark.parametrize("paused", [False, True])
async def test_task_comment_approval_resumes_once_without_rebinding_task(
    db_session, canonical_thread, paused,
):
    task, thread, envelope = await _scope(
        db_session, canonical_thread=canonical_thread, paused=paused,
    )
    original_conversation = task.conversation_id
    blocked = json.loads(await _guard(envelope) or "{}")
    assert blocked.get("__hitl__") is True, blocked
    token = blocked["approval_token"]
    again = json.loads(await _guard(envelope) or "{}")
    assert again["approval_token"] == token

    row = await db_session.get(HitlRequest, token)
    assert row.origin_task_id == task.id
    assert row.origin_conversation_id == thread.id
    resolution = await _resolve(db_session, envelope, token)
    metadata = resolution.runtime_metadata or {}
    assert "task_runtime_resume" not in metadata
    forced = metadata["forced_tool_calls"]
    assert len(forced) == 1
    assert forced[0]["name"] == TOOL
    assert forced[0]["arguments"] == {**ARGUMENTS, "approval_token": token}
    assert await _guard(envelope, arguments=forced[0]["arguments"]) is None
    replay = json.loads(await _guard(envelope, arguments=forced[0]["arguments"]) or "{}")
    assert replay.get("error"), replay
    await db_session.refresh(row)
    await db_session.refresh(task)
    assert row.status == "consumed"
    assert task.status == "in_progress"
    assert task.conversation_id == original_conversation
    workspace = await db_session.get(Workspace, envelope.workspace_id)
    assert workspace.status == ("paused" if paused else "active")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "missing_envelope", "background", "foreign_actor", "foreign_envelope_task",
    "foreign_thread_task", "wrong_thread_scope", "inactive_conversation",
    "missing_conversation", "terminal_task", "deleted_workspace",
    "archived_workspace", "draft_workspace",
])
async def test_task_comment_origin_rejects_invalid_admission(db_session, invalid):
    from datetime import datetime, timezone

    task, thread, envelope = await _scope(db_session)
    if invalid == "background":
        envelope = replace(envelope, surface=ChatSurface.SCHEDULED_AGENT_RUN,
                           profile=RuntimeProfile.BACKGROUND_WORKER)
    elif invalid == "foreign_actor":
        envelope = replace(envelope, principal=replace(
            envelope.principal, actor_user_id=generate_ulid(),
        ))
    elif invalid == "foreign_envelope_task":
        envelope = replace(envelope, thread_ref_id=generate_ulid())
    elif invalid == "foreign_thread_task":
        thread.thread_ref_id = generate_ulid()
    elif invalid == "wrong_thread_scope":
        thread.scope = "workspace_main"
    elif invalid == "inactive_conversation":
        thread.status = "archived"
    elif invalid == "missing_conversation":
        await db_session.delete(thread)
    elif invalid == "terminal_task":
        task.status = "completed"
    elif invalid == "deleted_workspace":
        workspace = await db_session.get(Workspace, envelope.workspace_id)
        workspace.deleted_at = datetime.now(timezone.utc)
    elif invalid in {"archived_workspace", "draft_workspace"}:
        workspace = await db_session.get(Workspace, envelope.workspace_id)
        workspace.status = invalid.removesuffix("_workspace")
    await db_session.commit()

    result = json.loads(await _guard(
        envelope, runtime_envelope=invalid != "missing_envelope",
    ) or "{}")
    assert result.get("error"), result
    assert not result.get("__hitl__"), result
    assert await db_session.scalar(select(func.count()).select_from(HitlRequest).where(
        HitlRequest.entity_id == envelope.entity_id,
    )) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_thread", [False, True])
async def test_background_cannot_spend_foreground_task_comment_grant(
    db_session, canonical_thread,
):
    task, _, envelope = await _scope(db_session, canonical_thread=canonical_thread)
    blocked = json.loads(await _guard(envelope) or "{}")
    token = blocked["approval_token"]
    await _resolve(db_session, envelope, token)
    background = replace(envelope, surface=ChatSurface.SCHEDULED_AGENT_RUN,
                         profile=RuntimeProfile.BACKGROUND_WORKER)
    result = json.loads(await _guard(
        background, arguments={**ARGUMENTS, "approval_token": token},
    ) or "{}")
    assert result.get("error"), result
    row = await db_session.get(HitlRequest, token, populate_existing=True)
    assert row.status != "consumed"
    await db_session.refresh(task)
    assert task.status == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["retargeted_thread", "terminal_task", "malformed_marker"])
async def test_task_comment_approval_revalidates_persisted_origin_before_resolution(
    db_session, invalid,
):
    from packages.core.services.task_comment_approval_origin import TASK_COMMENT_ORIGIN_CONTEXT_KEY

    task, thread, envelope = await _scope(db_session)
    blocked = json.loads(await _guard(envelope) or "{}")
    token = blocked["approval_token"]
    row = await db_session.get(HitlRequest, token)
    if invalid == "retargeted_thread":
        thread.thread_ref_id = generate_ulid()
    elif invalid == "terminal_task":
        task.status = "completed"
    else:
        row.context = {**row.context, TASK_COMMENT_ORIGIN_CONTEXT_KEY: {}}
    await db_session.commit()
    resolution = await _resolve(db_session, envelope, token)
    assert resolution is not None
    assert not (resolution.runtime_metadata or {}).get("forced_tool_calls")
    await db_session.refresh(row)
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_task_comment_rejection_does_not_fail_or_dispatch_task(db_session):
    task, _, envelope = await _scope(db_session)
    blocked = json.loads(await _guard(envelope) or "{}")
    resolution = await _resolve(db_session, envelope, blocked["approval_token"], action="reject")
    assert not (resolution.runtime_metadata or {}).get("task_runtime_resume")
    assert not (resolution.runtime_metadata or {}).get("forced_tool_calls")
    await db_session.refresh(task)
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_task_comment_payload_change_never_stops_parent_task(db_session):
    task, _, envelope = await _scope(db_session, canonical_thread=True)
    blocked = json.loads(await _guard(envelope) or "{}")
    token = blocked["approval_token"]
    await _resolve(db_session, envelope, token)
    result = json.loads(await _guard(
        envelope, arguments={"text": "Changed after approval", "approval_token": token},
    ) or "{}")
    assert result.get("error"), result
    await db_session.refresh(task)
    assert task.status == "in_progress"
    row = await db_session.get(HitlRequest, token, populate_existing=True)
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_task_terminal_cleanup_expires_comment_thread_approval(db_session):
    from packages.core.services.task_runtime_approvals import expire_task_runtime_approval_for_terminal

    task, thread, envelope = await _scope(db_session)
    assert task.conversation_id != thread.id
    token = json.loads(await _guard(envelope) or "{}")["approval_token"]
    task.status = "completed"
    await db_session.flush()
    await expire_task_runtime_approval_for_terminal(db_session, task=task, reason="task_completed")
    await db_session.commit()
    row = await db_session.get(HitlRequest, token, populate_existing=True)
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_real_task_comment_and_chat_card_resume_keep_foreground_scope(db_session, monkeypatch):
    import packages.core.ai.runtime as runtime_pkg
    from packages.core.models.task import Message
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context
    from packages.core.services.workspace_runtime import process_workspace_task_comment

    task, thread, envelope = await _scope(db_session)
    original_conversation = task.conversation_id
    captured = {}

    async def run_turn(message, conversation_id, **kwargs):
        # Replace only model execution: real comment persistence, runtime
        # context, permission checks and HITL store stay in the call chain.
        assert kwargs["surface"] is ChatSurface.TASK_COMMENT_THREAD
        _, _, _, ctx = await resolve_runtime_chat_context(
            kwargs["db"], message, conversation_id=conversation_id,
            entity_id=kwargs["entity_id"], user_id=kwargs["user_id"],
            workspace_id=kwargs["workspace_id"], agent_id=kwargs["agent_id"],
            runtime_surface=kwargs["surface"],
        )
        captured["envelope"] = ctx.runtime_envelope
        captured["blocked"] = json.loads(await _guard(ctx.runtime_envelope) or "{}")
        return {"content": "This draft needs approval.", "tool_calls_made": []}

    monkeypatch.setattr(runtime_pkg, "runtime_run_chat_turn", run_turn)
    await process_workspace_task_comment(
        task_id=task.id, entity_id=envelope.entity_id, user_id=envelope.user_id,
        author_label="Owner", comment="Publish the reviewed draft.",
    )
    assert captured["envelope"].conversation_id == thread.id
    assert captured["envelope"].task_id == task.id
    blocked = captured["blocked"]
    assert blocked.get("__hitl__") is True, blocked
    messages = list((await db_session.execute(select(Message).where(
        Message.conversation_id == thread.id, Message.role == "user",
    ))).scalars())
    assert [message.content for message in messages] == ["Publish the reviewed draft."]

    resolution = await _resolve(db_session, envelope, blocked["approval_token"])
    _, _, _, resumed = await resolve_runtime_chat_context(
        db_session, resolution.message, entity_id=envelope.entity_id,
        user_id=envelope.user_id, workspace_id=envelope.workspace_id,
        agent_id=envelope.agent_id,
        conversation_id=thread.id, runtime_surface=ChatSurface.WORKSPACE_CHAT,
        runtime_metadata=resolution.runtime_metadata,
    )
    assert resumed.runtime_envelope.surface is ChatSurface.TASK_COMMENT_THREAD
    assert resumed.runtime_envelope.profile is RuntimeProfile.TASK_WORKER_FEEDBACK
    forced = resolution.runtime_metadata["forced_tool_calls"][0]
    # Release context reads before the guard reacquires its lifecycle locks.
    await db_session.commit()
    assert await _guard(resumed.runtime_envelope, arguments=forced["arguments"]) is None
    await db_session.refresh(task)
    assert task.conversation_id == original_conversation
    assert task.status == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ChatSurface.SCHEDULED_AGENT_RUN, ChatSurface.WORKFLOW_AGENT_STEP])
async def test_task_thread_context_does_not_promote_background_surface(db_session, surface):
    from packages.core.services.runtime_chat_context import resolve_runtime_chat_context

    _, thread, envelope = await _scope(db_session)
    _, _, _, context = await resolve_runtime_chat_context(
        db_session, "background execution", entity_id=envelope.entity_id,
        user_id=envelope.user_id, workspace_id=envelope.workspace_id,
        conversation_id=thread.id, runtime_surface=surface,
    )
    assert context.runtime_envelope.surface is surface
    assert context.runtime_envelope.profile is not RuntimeProfile.TASK_WORKER_FEEDBACK


@pytest.mark.asyncio
@pytest.mark.parametrize("first_background", [False, True])
@pytest.mark.parametrize("status", ["pending", "granted"])
async def test_same_thread_dedup_never_converts_task_execution_origin(
    db_session, first_background, status,
):
    task, _, foreground = await _scope(db_session, canonical_thread=True)
    background = replace(foreground, surface=ChatSurface.SCHEDULED_AGENT_RUN,
                         profile=RuntimeProfile.BACKGROUND_WORKER)
    first, second = (background, foreground) if first_background else (foreground, background)
    blocked = json.loads(await _guard(first) or "{}")
    token = blocked["approval_token"]
    request = await db_session.get(HitlRequest, token)
    original_context = dict(request.context)
    # Exercise both dedup branches, including a grant awaiting execution.
    request.status = status
    await db_session.commit()
    result = json.loads(await _guard(second) or "{}")
    assert result.get("error"), result
    assert not result.get("__hitl__"), result
    await db_session.refresh(request)
    await db_session.refresh(task)
    assert request.status == status
    assert request.context == original_context
    assert task.status == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["workspace_delete", "task_terminal"])
async def test_task_comment_admission_waits_for_lifecycle_mutation(db_session, boundary):
    import asyncio
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    task, _, envelope = await _scope(db_session)
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with session_factory() as mutation_db:
        if boundary == "workspace_delete":
            workspace = await lock_workspace_access_boundary(
                mutation_db, entity_id=envelope.entity_id,
                workspace_id=envelope.workspace_id,
            )
            workspace.deleted_at = datetime.now(timezone.utc)
        else:
            await lock_task_runtime_approval_boundary(mutation_db, task_id=task.id)
            locked_task = (await mutation_db.execute(select(Task).where(
                Task.id == task.id,
            ).with_for_update())).scalar_one()
            locked_task.status = "completed"
        await mutation_db.flush()
        admission = asyncio.create_task(_guard(envelope))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(admission), timeout=0.25)
        finally:
            await mutation_db.commit()
        result = json.loads(await asyncio.wait_for(admission, timeout=10) or "{}")
    assert result.get("error"), result
    assert not result.get("__hitl__"), result
    assert await db_session.scalar(select(func.count()).select_from(HitlRequest).where(
        HitlRequest.entity_id == envelope.entity_id,
    )) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["resolve", "reply", "cancel"])
async def test_public_chat_approval_adapter_locks_lifecycle_before_task_and_conversation(
    db_session, operation,
):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.core.services.chat_approvals import cancel_chat_approvals, resolve_chat_approval_turn
    from packages.core.services.conversation_lifecycle import get_or_create_conversation
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    task, thread, envelope = await _scope(db_session)
    token = json.loads(await _guard(envelope) or "{}")["approval_token"]
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def public_adapter():
        async with session_factory() as adapter_db:
            if operation == "cancel":
                result = await cancel_chat_approvals(
                    adapter_db, entity_id=envelope.entity_id,
                    conversation_id=thread.id, user_id=envelope.user_id,
                    hitl_ids=[token], cancel_turn=True,
                )
            else:
                # Both HTTP Chat routes execute this prelude before resolving
                # a card; testing the adapter alone misses its Task-row lock.
                await get_or_create_conversation(
                    adapter_db, envelope.entity_id, envelope.user_id,
                    conversation_id=thread.id, workspace_id=envelope.workspace_id,
                    agent_id=envelope.agent_id,
                )
                result = await resolve_chat_approval_turn(
                    adapter_db, entity_id=envelope.entity_id,
                    conversation_id=thread.id, user_id=envelope.user_id,
                    message="yes" if operation == "reply" else json.dumps({
                        "hitl_id": token, "action": "approve",
                    }),
                )
            await adapter_db.commit()
            return result

    async with session_factory() as blocker_db:
        # This is the first row lock taken by foreground tool admission.
        await lock_workspace_access_boundary(
            blocker_db, entity_id=envelope.entity_id,
            workspace_id=envelope.workspace_id,
        )
        adapter = asyncio.create_task(public_adapter())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(adapter), timeout=0.25)
            async with session_factory() as observer_db:
                # If the HTTP/file-approval prelude locked either row first,
                # these NOWAIT reads fail: an admission waiting for that row
                # would otherwise form the opposite half of a deadlock.
                await observer_db.execute(select(Task).where(
                    Task.id == task.id,
                ).with_for_update(nowait=True))
                await observer_db.execute(select(Conversation).where(
                    Conversation.id == thread.id,
                ).with_for_update(nowait=True))
                await observer_db.rollback()
        finally:
            await blocker_db.rollback()
            result = await asyncio.wait_for(adapter, timeout=10)

    row = await db_session.get(HitlRequest, token, populate_existing=True)
    assert row.status == ("expired" if operation == "cancel" else "granted")
    if operation == "cancel":
        assert result["runtime_cancelled"] == 1
        assert result["cancel_requested"] == 1
    else:
        assert result[3]["forced_tool_calls"][0]["arguments"]["approval_token"] == token


@pytest.mark.asyncio
async def test_foreground_cannot_claim_a_background_task_approval(db_session):
    task, _, foreground = await _scope(db_session, canonical_thread=True)
    background = replace(foreground, surface=ChatSurface.SCHEDULED_AGENT_RUN,
                         profile=RuntimeProfile.BACKGROUND_WORKER)
    token = json.loads(await _guard(background) or "{}")["approval_token"]
    request = await db_session.get(HitlRequest, token)
    request.status = "granted"
    await db_session.commit()
    # A foreground call rejects the wrong execution plane before any Task
    # origin check (which would expire the grant).
    result = json.loads(await _guard(
        foreground, arguments={**ARGUMENTS, "approval_token": token},
    ) or "{}")
    assert result.get("error"), result
    await db_session.refresh(request)
    await db_session.refresh(task)
    assert request.status == "granted"
    assert task.status == "in_progress"
