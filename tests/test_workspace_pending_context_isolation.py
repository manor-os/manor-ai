"""Pending HITL/task blockers must not hijack an unrelated Host Chat turn.

Chat pending cards stay conversation-scoped and are only injected when the
latest user message is an actual HITL reply. Lease-origin task blockers stay
workspace-wide for task runs (no conversation_id) and stay out of Host Chat
prompt context.
"""
from __future__ import annotations

import pytest

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_runtime import resolve_workspace_runtime


async def _seed_workspace(db, *, name: str):
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name=name)
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        title=name,
        channel="workspace",
        scope="workspace_main",
    )
    db.add_all([workspace, conversation])
    await db.flush()
    return workspace, conversation


def _hitl_message(conversation_id: str, *, draft_id: str = "draft_1") -> Message:
    return Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="Review workspace operation",
        author_kind="agent",
        message_kind="hitl_request",
        pending_action={
            "kind": "workspace_operation_review",
            "draft_id": draft_id,
            "action": "workspace.operation.apply",
            "tool": "workspace_operation",
            "operation": {"kind": "workspace_operation_review", "draft_id": draft_id},
            "options": ["approve", "reject"],
        },
    )


def _user_message(conversation_id: str, body: str) -> Message:
    return Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="user",
        content=body,
        author_kind="user",
        message_kind="chat",
    )


@pytest.mark.asyncio
async def test_non_reply_user_turn_does_not_inject_open_hitl(db_session):
    workspace, conversation = await _seed_workspace(db_session, name="HITL isolation")
    hitl = _hitl_message(conversation.id)
    db_session.add_all([hitl, _user_message(conversation.id, "I want to publish this song")])
    await db_session.commit()

    envelope = await resolve_workspace_runtime(
        db_session,
        entity_id=workspace.entity_id,
        user_id=generate_ulid(),
        conversation_id=conversation.id,
        workspace_id=workspace.id,
    )
    assert "Open Workspace HITL Requests" not in (envelope.extra_context or "")
    await db_session.refresh(hitl)
    assert hitl.resolved_at is None


@pytest.mark.asyncio
async def test_approve_reply_still_injects_open_hitl(db_session):
    workspace, conversation = await _seed_workspace(db_session, name="HITL reply")
    hitl = _hitl_message(conversation.id, draft_id="draft_approve")
    db_session.add_all([hitl, _user_message(conversation.id, "approve")])
    await db_session.commit()

    envelope = await resolve_workspace_runtime(
        db_session,
        entity_id=workspace.entity_id,
        user_id=generate_ulid(),
        conversation_id=conversation.id,
        workspace_id=workspace.id,
    )
    assert "Open Workspace HITL Requests" in (envelope.extra_context or "")
    assert hitl.id in (envelope.extra_context or "")
    await db_session.refresh(hitl)
    assert hitl.resolved_at is None


@pytest.mark.asyncio
async def test_conversation_turn_does_not_dump_workspace_task_blockers(db_session):
    workspace, conversation = await _seed_workspace(db_session, name="Blocker isolation")
    request = HitlRequest(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="social_post.publish",
        risk_level="medium",
        origin_kind=ApprovalOriginKind.LEASE.value,
        status=ApprovalStatus.PENDING.value,
        dedup_key=f"lease:{generate_ulid()}:human_input",
        reason="Which screenshot should the company post use?",
        context={"pending_kind": "human_input"},
        payload={"question": "Which screenshot?"},
    )
    db_session.add_all([
        request,
        _user_message(conversation.id, "continue the music release"),
    ])
    await db_session.commit()

    with_conversation = await resolve_workspace_runtime(
        db_session,
        entity_id=workspace.entity_id,
        user_id=generate_ulid(),
        conversation_id=conversation.id,
        workspace_id=workspace.id,
    )
    assert "Open Task Blockers" not in (with_conversation.extra_context or "")

    without_conversation = await resolve_workspace_runtime(
        db_session,
        entity_id=workspace.entity_id,
        user_id=generate_ulid(),
        workspace_id=workspace.id,
    )
    assert "Open Task Blockers" in (without_conversation.extra_context or "")
    assert request.id in (without_conversation.extra_context or "")
