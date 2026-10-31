"""Foreground provider approvals use mocked receipts, never provider I/O."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from packages.core.ai.runtime.approval_service import (
    prepare_provider_runtime_approval_execution,
    register_provider_runtime_approval,
    settle_provider_runtime_approval_execution,
)
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.hitl_request import HitlRequest
from packages.core.services.chat_approvals import register_chat_provider_approval
from packages.core.services.task_comment_approval_origin import (
    TASK_COMMENT_ORIGIN_CONTEXT_KEY,
    TaskCommentApprovalOriginError,
)
from test_provider_runtime_approvals import _chrome_approval_request
from test_task_comment_approval_origin import _resolve, _scope


async def _provider_gate(db, envelope):
    request = _chrome_approval_request(approval_id=f"provider-{envelope.task_id}")
    card = await register_chat_provider_approval(
        db, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, request=request,
        runtime_envelope=envelope,
    )
    await db.commit()
    assert card is not None
    row = await db.get(HitlRequest, card["hitl"]["id"])
    assert row.origin_task_id == envelope.task_id
    assert row.context[TASK_COMMENT_ORIGIN_CONTEXT_KEY]["conversation_id"] == envelope.conversation_id
    resolution = await _resolve(db, envelope, row.id)
    assert "task_runtime_resume" not in (resolution.runtime_metadata or {})
    return request, row, resolution.runtime_metadata


@pytest.mark.asyncio
async def test_foreground_provider_confirmation_and_retry_are_exactly_once(db_session):
    task, _, envelope = await _scope(db_session)
    original_conversation = task.conversation_id
    request, row, metadata = await _provider_gate(db_session, envelope)
    scope = dict(
        runtime_metadata=metadata, entity_id=envelope.entity_id,
        conversation_id=envelope.conversation_id, task_id=task.id,
        runtime_envelope=envelope,
    )
    confirmation = await prepare_provider_runtime_approval_execution(
        db_session, **scope, tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
    )
    assert confirmation.result is None
    assert await settle_provider_runtime_approval_execution(
        db_session, **scope, tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
        result={"ok": True, "status": "approved", "approvalId": request["provider_approval_id"],
                "approvalToken": "synthetic-test-token"},
    )
    retry = {**request["retry_arguments"], "approvalToken": "synthetic-test-token"}
    first = await prepare_provider_runtime_approval_execution(
        db_session, **scope, tool_name=request["retry_tool"], arguments=retry,
    )
    assert first.result is None and first.changed
    duplicate = await prepare_provider_runtime_approval_execution(
        db_session, **scope, tool_name=request["retry_tool"], arguments=retry,
    )
    assert json.loads(duplicate.result)["status"] == "ambiguous"
    assert await settle_provider_runtime_approval_execution(
        db_session, **scope, tool_name=request["retry_tool"], arguments=retry,
        result={"ok": True, "status": "clicked"},
    )
    await db_session.commit()
    await db_session.refresh(row)
    await db_session.refresh(task)
    assert row.status == "consumed"
    assert task.status == "in_progress"
    assert task.conversation_id == original_conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "background", "foreign_task"])
async def test_foreground_provider_requires_its_current_runtime(db_session, invalid):
    task, _, envelope = await _scope(db_session)
    request, row, metadata = await _provider_gate(db_session, envelope)
    current = envelope
    if invalid == "missing":
        current = None
    elif invalid == "background":
        current = replace(envelope, surface=ChatSurface.SCHEDULED_AGENT_RUN,
                          profile=RuntimeProfile.BACKGROUND_WORKER)
    else:
        current = replace(envelope, thread_ref_id="foreign-task")
    preflight = await prepare_provider_runtime_approval_execution(
        db_session, runtime_metadata=metadata, entity_id=envelope.entity_id,
        conversation_id=envelope.conversation_id, task_id=task.id,
        runtime_envelope=current, tool_name=request["confirmation_tool"],
        arguments=request["confirmation_arguments"],
    )
    assert json.loads(preflight.result)["status"] == "invalid"
    assert row.status == "granted"
    assert not row.context.get("provider_action_claim")


@pytest.mark.asyncio
async def test_foreground_provider_dedup_does_not_reuse_a_worker_gate(db_session):
    task, _, envelope = await _scope(db_session, canonical_thread=True)
    request = _chrome_approval_request(approval_id=f"shared-{task.id}")
    background = await register_provider_runtime_approval(
        db_session, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, task_id=task.id, request=request,
    )
    foreground = await register_chat_provider_approval(
        db_session, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, request=request, runtime_envelope=envelope,
    )
    assert background is not None
    assert foreground is None
    row = await db_session.get(HitlRequest, background["hitl"]["id"])
    assert TASK_COMMENT_ORIGIN_CONTEXT_KEY not in row.context


@pytest.mark.asyncio
async def test_chat_provider_adapter_preserves_every_explicit_task_id(db_session, monkeypatch):
    import packages.core.ai.runtime.approval_service as approvals

    _, _, envelope = await _scope(db_session)
    received = {}

    async def register(_db, **kwargs):
        received.update(kwargs)
        return None

    monkeypatch.setattr(approvals, "register_provider_runtime_approval", register)
    await register_chat_provider_approval(
        db_session, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, request={}, task_id="explicit-task",
        runtime_envelope=envelope,
    )
    assert received["task_id"] == "explicit-task"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing_task", "foreign_workspace"])
async def test_chat_provider_adapter_does_not_mint_an_orphan_origin(db_session, invalid):
    from packages.core.models.base import generate_ulid

    _, _, envelope = await _scope(db_session)
    envelope = replace(envelope, **(
        {"task_id": generate_ulid()} if invalid == "missing_task"
        else {"workspace_id": generate_ulid()}
    ))
    with pytest.raises(TaskCommentApprovalOriginError):
        await register_chat_provider_approval(
            db_session, entity_id=envelope.entity_id, user_id=envelope.user_id,
            conversation_id=envelope.conversation_id,
            request=_chrome_approval_request(approval_id=f"orphan-{envelope.task_id}"),
            runtime_envelope=envelope,
        )


@pytest.mark.asyncio
async def test_foreground_provider_never_claims_a_ready_worker_approval(db_session):
    task, _, envelope = await _scope(db_session, canonical_thread=True)
    request = _chrome_approval_request(approval_id=f"worker-{task.id}")
    card = await register_provider_runtime_approval(
        db_session, entity_id=envelope.entity_id, user_id=envelope.user_id,
        conversation_id=envelope.conversation_id, task_id=task.id, request=request,
    )
    row = await db_session.get(HitlRequest, card["hitl"]["id"])
    row.status = "granted"
    await db_session.commit()
    preflight = await prepare_provider_runtime_approval_execution(
        db_session, entity_id=envelope.entity_id, conversation_id=envelope.conversation_id,
        task_id=task.id, runtime_envelope=envelope,
        runtime_metadata={"provider_approval_execution": {
            "hitl_id": row.id, "confirmation_tool": request["confirmation_tool"],
            "retry_tool": request["retry_tool"],
        }},
        tool_name=request["confirmation_tool"], arguments=request["confirmation_arguments"],
    )
    assert json.loads(preflight.result)["status"] == "invalid"
    assert row.status == "granted"
