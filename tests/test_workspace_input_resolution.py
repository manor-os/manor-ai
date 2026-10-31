"""Real PostgreSQL contracts for the shared ordinary Workspace input resolver."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY
from packages.core.services.workspace_input_resolution import (
    WorkspaceInputResolutionError,
    dispatch_workspace_input_resolution,
    resolve_workspace_input_message,
)

pytestmark = pytest.mark.asyncio


async def _seed(db, *, kind="human_input", task_card=False):
    entity = Entity(id=generate_ulid(), name="Input resolution test")
    user = User(id=generate_ulid(), entity_id=entity.id, role="owner", status="active",
                email=f"{generate_ulid().lower()}@test.invalid", password_hash="test-only",
                display_name="Input owner")
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Input workspace", status="active")
    conversation = Conversation(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                                user_id=user.id, scope="workspace_main", channel="workspace")
    task = Task(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                conversation_id=conversation.id, title="Original task", status="waiting_on_customer",
                agent_id=MANOR_AGENT_ID, details={TASK_INPUT_QUESTION_KEY: {"question": "Which audio?"}})
    db.add_all([entity, user, workspace, conversation, task,
                UserMembership(user_id=user.id, entity_id=entity.id, role="owner", status="active"),
                WorkspaceStaff(workspace_id=workspace.id, user_id=user.id, role="owner", status="active")])
    pending = {"kind": kind, "task_id": task.id, "prompt": "Which audio?"}
    plan = step = request = None
    if not task_card:
        plan = ExecutionPlan(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                             task_id=task.id, status="paused", plan_dag={})
        checkpoint = RuntimeAgentCheckpoint(
            messages=[], rounds=1, usage={}, tool_calls_made=[],
            pending_tool_call={"id": "original-input-call", "name": "request_input", "arguments": {}},
        ).to_dict()
        step = ExecutionStep(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                             plan_id=plan.id, step_key="audio", kind="subagent",
                             step_status="waiting_human", params={"prompt": "Use the selected audio."},
                             runtime_checkpoint=checkpoint)
        request = HitlRequest(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                              origin_kind=ApprovalOriginKind.LEASE.value, origin_task_id=task.id,
                              origin_plan_id=plan.id, origin_step_id=step.id,
                              origin_conversation_id=conversation.id, hitl_type=HitlType.INPUT.value,
                              status=ApprovalStatus.PENDING.value, dedup_key=generate_ulid(),
                              context={"pending_kind": kind})
        pending.update(step_id=step.id, plan_id=plan.id, approval_request_id=request.id)
        db.add_all([plan, step, request])
    if task_card:
        from packages.core.services.task_chat_hitl import ensure_task_input_hitl

        await db.flush()
        message = await ensure_task_input_hitl(db, task, question={"question": "Which audio?"})
        assert message is not None
        request = await db.get(HitlRequest, message.pending_action["approval_request_id"])
    else:
        message = Message(id=generate_ulid(), conversation_id=conversation.id, role="assistant",
                          author_kind="agent", message_kind="hitl_request", pending_action=pending)
    message.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.add(message)
    await db.commit()
    return SimpleNamespace(user=user, workspace=workspace, conversation=conversation,
                           task=task, plan=plan, step=step, request=request, message=message)


async def _resolve(db, fx, **kwargs):
    return await resolve_workspace_input_message(
        db, workspace_id=fx.workspace.id, conversation_id=fx.conversation.id,
        message_id=fx.message.id, user=fx.user, **kwargs,
    )


def _capture_dispatch(monkeypatch):
    calls = []

    monkeypatch.setattr("packages.core.services.task_input_resume.dispatch_task_input_resume",
                        lambda *args: calls.append(("task", *args)))
    monkeypatch.setattr("packages.core.services.human_queue_resume.dispatch_strategist_for_cleared_human_queue",
                        lambda **kwargs: calls.append(("strategist", kwargs)))
    return calls


@pytest.mark.parametrize("kind", ["human_input", "needs_input"])
async def test_response_resumes_exact_step_and_task_once(db_session, monkeypatch, kind):
    calls = _capture_dispatch(monkeypatch)
    fx = await _seed(db_session, kind=kind)
    checkpoint = copy.deepcopy(fx.step.runtime_checkpoint)
    payload = {"answers": {"audio": "original", "add_music": False}}
    result = await _resolve(db_session, fx, response="  Use the original audio.  ", payload=payload)
    assert result.changed and result.plan_id == fx.plan.id
    assert calls == []
    await db_session.commit()
    await dispatch_workspace_input_resolution(db_session, result)

    assert calls == []
    assert fx.message.resolved_by_user_id == fx.user.id
    assert fx.message.resolution["choice"] == "respond"
    assert fx.message.resolution["payload"]["response"] == "Use the original audio."
    assert fx.step.runtime_checkpoint == checkpoint
    assert fx.step.step_status == "pending"
    assert fx.plan.status == "running"
    assert fx.request.status == ApprovalStatus.CONSUMED.value
    assert fx.request.hitl_type == HitlType.INPUT.value
    assert fx.request.decided_by_user_id == fx.user.id
    if kind == "human_input":
        assert fx.step.human_input_response["response"] == "Use the original audio."
        assert fx.step.human_input_response["answers"] == payload["answers"]
    else:
        assert fx.step.params["answers"] == payload["answers"]

    saved_response = copy.deepcopy(fx.message.resolution)
    repeated = await _resolve(db_session, fx, response="A later duplicate must not replace the answer.")
    await db_session.commit()
    await dispatch_workspace_input_resolution(db_session, repeated)
    assert repeated.changed is False
    assert fx.message.resolution == saved_response
    assert calls == []
    receipts = (await db_session.scalars(select(Message).where(
        Message.conversation_id == fx.conversation.id, Message.author_kind == "system",
    ))).all()
    # The answered card shows the response; no separate receipt is posted.
    assert receipts == []


async def test_concurrent_responses_consume_original_input_once(db_session, monkeypatch):
    calls = _capture_dispatch(monkeypatch)
    fx = await _seed(db_session)
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    async def submit(answer):
        async with factory() as db:
            result = await _resolve(db, fx, response=answer)
            await db.commit()
            await dispatch_workspace_input_resolution(db, result)
            return result.changed

    changed = await asyncio.wait_for(asyncio.gather(submit("First"), submit("Second")), timeout=15)
    assert sorted(changed) == [False, True]
    assert calls == []
    await db_session.refresh(fx.message)
    await db_session.refresh(fx.step)
    await db_session.refresh(fx.request)
    assert fx.message.resolution["payload"]["response"] in {"First", "Second"}
    assert fx.step.human_input_response["response"] == fx.message.resolution["payload"]["response"]
    assert fx.request.status == ApprovalStatus.CONSUMED.value


@pytest.mark.parametrize("reply", ["missing", "before_card", "different_user", "different_conversation", "matching"])
async def test_tool_resolution_requires_later_reply_by_same_user(db_session, reply):
    fx = await _seed(db_session)
    if reply != "missing":
        db_session.add(Message(
            conversation_id=generate_ulid() if reply == "different_conversation" else fx.conversation.id,
            role="user", author_kind="user", message_kind="text", content="Use the original audio.",
            meta={"author_user_id": generate_ulid() if reply == "different_user" else fx.user.id},
            created_at=fx.message.created_at + timedelta(seconds=-1 if reply == "before_card" else 1),
        ))
        await db_session.commit()
    if reply == "matching":
        result = await _resolve(db_session, fx, response="Use the original audio.", require_user_reply=True)
        assert result.changed
        assert fx.step.human_input_response["response"] == "Use the original audio."
    else:
        with pytest.raises(WorkspaceInputResolutionError) as error:
            await _resolve(db_session, fx, response="Use the original audio.", require_user_reply=True)
        assert error.value.status_code == 409
        assert fx.message.resolved_at is None
        assert fx.request.status == ApprovalStatus.PENDING.value
        assert fx.step.step_status == "waiting_human"


@pytest.mark.parametrize("boundary", ["conversation", "workspace", "entity", "step_workspace", "plan", "task", "request_type", "nested_approval", "simulation", "approval_card", "deleted_workspace", "viewer"])
async def test_scope_or_approval_mismatch_does_not_consume_input(db_session, boundary):
    fx = await _seed(db_session)
    pending = dict(fx.message.pending_action)
    expected_status = 409
    if boundary == "conversation":
        fx.message.conversation_id = generate_ulid()
        expected_status = 404
    elif boundary == "workspace":
        fx.conversation.workspace_id = generate_ulid()
        expected_status = 404
    elif boundary == "entity":
        fx.conversation.entity_id = generate_ulid()
        expected_status = 404
    elif boundary == "step_workspace":
        fx.step.workspace_id = generate_ulid()
    elif boundary == "plan":
        pending["plan_id"] = generate_ulid()
    elif boundary == "task":
        pending["task_id"] = generate_ulid()
    elif boundary == "request_type":
        fx.request.hitl_type = HitlType.AUTHORIZE.value
    elif boundary == "nested_approval":
        from packages.core.constants.execution import COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY

        fx.request.context = {**fx.request.context, COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY: {"id": generate_ulid()}}
        expected_status = 422
    elif boundary == "simulation":
        pending["simulation_runtime"] = {"id": "simulation"}
        expected_status = 422
    elif boundary == "approval_card":
        pending["kind"] = "needs_confirmation"
        expected_status = 422
    elif boundary == "deleted_workspace":
        fx.workspace.deleted_at = datetime.now(timezone.utc)
        expected_status = 404
    elif boundary == "viewer":
        fx.user.role = "viewer"
        membership = await db_session.scalar(select(UserMembership).where(UserMembership.user_id == fx.user.id))
        membership.role = "viewer"
        access = await db_session.scalar(select(WorkspaceStaff).where(WorkspaceStaff.user_id == fx.user.id))
        access.role = "viewer"
        expected_status = 403
    fx.message.pending_action = pending
    await db_session.commit()
    message_id, step_id, request_id = fx.message.id, fx.step.id, fx.request.id
    with pytest.raises(WorkspaceInputResolutionError) as error:
        await _resolve(db_session, fx, response="Use the original audio.")
    assert error.value.status_code == expected_status
    await db_session.rollback()
    message = await db_session.get(Message, message_id)
    step = await db_session.get(ExecutionStep, step_id)
    request = await db_session.get(HitlRequest, request_id)
    assert message.resolved_at is None
    assert step.step_status == "waiting_human"
    assert request.status == ApprovalStatus.PENDING.value


@pytest.mark.parametrize("choice,payload", [("approve", {}), ("reject", {}), ("respond", {}), ("respond", {"answers": {"audio": " "}})])
async def test_nonresponse_and_empty_response_leave_card_open(db_session, choice, payload):
    fx = await _seed(db_session)
    with pytest.raises(WorkspaceInputResolutionError) as error:
        await _resolve(db_session, fx, choice=choice, payload=payload)
    assert error.value.status_code == 422
    assert fx.message.resolved_at is None
    assert fx.request.status == ApprovalStatus.PENDING.value
    assert fx.step.step_status == "waiting_human"


async def test_workspace_task_card_cannot_enter_legacy_runner(db_session, monkeypatch):
    calls = _capture_dispatch(monkeypatch)
    fx = await _seed(db_session, task_card=True)
    message_id, task_id, request_id = fx.message.id, fx.task.id, fx.request.id
    with pytest.raises(WorkspaceInputResolutionError) as error:
        await _resolve(db_session, fx, response="Use the original audio.")
    assert error.value.status_code == 409
    assert "no longer waiting" in error.value.detail
    await db_session.rollback()
    message = await db_session.get(Message, message_id)
    task = await db_session.get(Task, task_id)
    request = await db_session.get(HitlRequest, request_id)
    assert message.resolved_at is None
    assert task.status == "waiting_on_customer"
    assert "_hitl_response" not in task.details
    assert request.status == ApprovalStatus.PENDING.value
    assert calls == []


@pytest.mark.parametrize("target", ["message", "hitl", "conflicting", "wrong_message", "missing"])
async def test_runtime_facade_persists_response_only_for_exact_target(db_session, monkeypatch, target):
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_resolve_hitl_action

    fx = await _seed(db_session)
    calls = _capture_dispatch(monkeypatch)
    db_session.add(Message(
        conversation_id=fx.conversation.id, role="user", author_kind="user", content="Use original audio.",
        meta={"author_user_id": fx.user.id}, created_at=fx.message.created_at + timedelta(seconds=1),
    ))
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("packages.core.database.async_session", factory)
    targets = {
        "message": {"message_id": fx.message.id},
        "hitl": {"hitl_id": fx.request.id},
        "conflicting": {"message_id": fx.message.id, "hitl_id": generate_ulid()},
        "wrong_message": {"message_id": generate_ulid(), "hitl_id": fx.request.id},
        "missing": {},
    }
    result = json.loads(await runtime_workspace_resolve_hitl_action(
        entity_id=fx.user.entity_id, workspace_id=fx.workspace.id,
        conversation_id=fx.conversation.id, user_id=fx.user.id,
        params={**targets[target], "action": "respond", "response": "Use original audio."},
    ))
    await db_session.refresh(fx.message)
    await db_session.refresh(fx.step)
    await db_session.refresh(fx.request)
    if target in {"message", "hitl"}:
        assert result["resolved"] is True, result
        assert result["message_id"] == fx.message.id
        assert fx.message.resolution["payload"]["response"] == "Use original audio."
        assert fx.step.human_input_response["response"] == "Use original audio."
        assert fx.request.status == ApprovalStatus.CONSUMED.value
        assert calls == []
    else:
        assert result["resolved"] is False, result
        assert result["reason"] == "target_not_found_or_ambiguous"
        assert fx.message.resolved_at is None
        assert fx.step.step_status == "waiting_human"
        assert fx.request.status == ApprovalStatus.PENDING.value
        assert calls == []


@pytest.mark.parametrize("actor_reply", [False, True])
async def test_runtime_facade_requires_current_users_reply(db_session, monkeypatch, actor_reply):
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_resolve_hitl_action

    fx = await _seed(db_session)
    calls = _capture_dispatch(monkeypatch)
    db_session.add(Message(
        conversation_id=fx.conversation.id, role="user", author_kind="user", content="Use original audio.",
        meta={"author_user_id": fx.user.id if actor_reply else generate_ulid()},
        created_at=fx.message.created_at + timedelta(seconds=1),
    ))
    await db_session.commit()
    monkeypatch.setattr("packages.core.database.async_session", async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    ))
    result = json.loads(await runtime_workspace_resolve_hitl_action(
        entity_id=fx.user.entity_id, workspace_id=fx.workspace.id,
        conversation_id=fx.conversation.id, user_id=fx.user.id,
        params={"message_id": fx.message.id, "action": "respond", "response": "Use original audio."},
    ))
    assert result["resolved"] is actor_reply, result
    await db_session.refresh(fx.message)
    await db_session.refresh(fx.step)
    assert (fx.message.resolved_at is not None) is actor_reply
    assert fx.step.step_status == ("pending" if actor_reply else "waiting_human")
    assert calls == []


@pytest.mark.parametrize("attachment_state", ["deleted", "private"])
async def test_runtime_facade_unavailable_attachment_preserves_input(db_session, monkeypatch, attachment_state):
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_resolve_hitl_action

    fx = await _seed(db_session)
    calls = _capture_dispatch(monkeypatch)
    document = Document(id=generate_ulid(), entity_id=fx.user.entity_id, owner_id=generate_ulid(),
                        name="Private audio.wav", source="upload", fs_path="audio.wav", visibility="private")
    db_session.add_all([document, Message(
        conversation_id=fx.conversation.id, role="user", author_kind="user", content="Use this file.",
        meta={"author_user_id": fx.user.id}, created_at=fx.message.created_at + timedelta(seconds=1),
    )])
    await db_session.flush()
    document_id = document.id
    if attachment_state == "deleted":
        await db_session.delete(document)
    else:
        fx.user.role = "member"
        membership = await db_session.scalar(select(UserMembership).where(UserMembership.user_id == fx.user.id))
        membership.role = "member"
        access = await db_session.scalar(select(WorkspaceStaff).where(WorkspaceStaff.user_id == fx.user.id))
        access.role = "contributor"
    await db_session.commit()
    monkeypatch.setattr("packages.core.database.async_session", async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    ))
    result = json.loads(await runtime_workspace_resolve_hitl_action(
        entity_id=fx.user.entity_id, workspace_id=fx.workspace.id,
        conversation_id=fx.conversation.id, user_id=fx.user.id,
        params={"message_id": fx.message.id, "action": "respond",
                "payload": {"attachments": [{"id": document_id}]}},
    ))
    assert result["resolved"] is False, result
    assert result["reason"] == "input_not_resolved"
    assert "no longer available" in result["message"]
    await db_session.refresh(fx.message)
    await db_session.refresh(fx.step)
    await db_session.refresh(fx.request)
    assert fx.message.resolved_at is None
    assert fx.step.step_status == "waiting_human"
    assert fx.request.status == ApprovalStatus.PENDING.value
    assert calls == []


@pytest.mark.parametrize("winner", ["queue", "chat"])
async def test_queue_and_shared_resolver_share_one_commitment_lock(db_session, monkeypatch, winner):
    from apps.api.routers import humans
    from packages.core.humans import open_commitment

    fx = await _seed(db_session, task_card=True)
    calls = _capture_dispatch(monkeypatch)
    monkeypatch.setattr(humans, "_dispatch_strategist_for_cleared_human_queue",
                        lambda **kwargs: calls.append(("strategist", kwargs)))
    commitment = await open_commitment(
        db_session, entity_id=fx.user.entity_id, workspace_id=fx.workspace.id,
        request_kind="input", source_kind="chat", source_id=generate_ulid(),
        expected_input="Which audio?", participant_id=fx.user.id,
    )
    fx.message.pending_action = {"kind": "human_input", "commitment_id": commitment.id}
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    async def resolve(db, surface):
        try:
            if surface == "queue":
                await humans.respond_to_commitment(
                    workspace_id=fx.workspace.id, commitment_id=commitment.id,
                    req=humans.CommitmentRespondRequest(action="fulfill", response=f"{surface} answer"),
                    user=fx.user, db=db,
                )
                return True
            result = await _resolve(db, fx, response=f"{surface} answer")
            await db.commit()
            await dispatch_workspace_input_resolution(db, result)
            return result.changed
        except (HTTPException, WorkspaceInputResolutionError) as exc:
            await db.rollback()
            assert exc.status_code == 409
            return False

    async def compete():
        async with factory() as first, factory() as second:
            await first.scalar(select(HumanCommitment).where(
                HumanCommitment.id == commitment.id,
            ).with_for_update())
            other = asyncio.create_task(resolve(second, "chat" if winner == "queue" else "queue"))
            # Let the other entrypoint observe the open card before the
            # first transaction completes its already-held commitment lock.
            await asyncio.sleep(0.03)
            first_changed = await resolve(first, winner)
            second_changed = await other
            return first_changed, second_changed

    assert await asyncio.wait_for(compete(), timeout=15) == (True, False)
    await db_session.refresh(commitment)
    await db_session.refresh(fx.message)
    assert commitment.status == "fulfilled"
    assert commitment.response["text"] == f"{winner} answer"
    assert fx.message.resolved_at is not None
    assert len(calls) == 1
    assert calls[0][0] == "strategist"
