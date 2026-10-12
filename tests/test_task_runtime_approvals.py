"""Durable approval checkpoints for background TaskRunner executions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from packages.core.ai.engine import ChatMessage, LLMConfig
from packages.core.ai.runtime import RuntimeTaskAgentTurnResult
from packages.core.ai.runtime.approval_messages import approval_args_hash
from packages.core.ai.task_runner import TaskRunner
from packages.core.ai.runtime.task_checkpoint import (
    RUNTIME_TASK_HITL_CHECKPOINT_MAX_BYTES,
    RuntimeTaskHitlCheckpoint,
    runtime_deserialize_task_messages,
    runtime_serialize_task_messages,
)
from packages.core.constants.approvals import ApprovalStatus
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription
from packages.core.services.task_runtime_approvals import (
    TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY,
    TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY,
    TASK_RUNTIME_EXECUTION_CONTEXT_KEY,
    TASK_RUNTIME_HITL_DETAIL_KEY,
    TASK_RUNTIME_RESOLUTION_CONTEXT_KEY,
    TASK_RUNTIME_RESUME_EVENT,
    TaskRuntimeApprovalError,
    claim_task_runtime_approval_execution,
    ensure_task_runtime_approval_resolvable,
    fail_task_runtime_approval_origin,
    load_task_runtime_approval_resume,
    mark_task_runtime_approval_execution_ambiguous,
    persist_task_runtime_approval_gate,
    stage_task_runtime_approval_resolution,
    task_runtime_approval_binding_digest,
    task_runtime_hitl_details,
    validate_task_runtime_resume_delivery,
)


def _checkpoint(
    *,
    task_id: str,
    conversation_id: str,
    hitl_id: str,
    entity_id: str = "entity-checkpoint",
    workspace_id: str | None = None,
    execution_user_id: str = "user-checkpoint",
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    scheduled_run_id: str | None = "run-checkpoint",
    scheduled_job_id: str | None = "job-checkpoint",
    approval_binding_digest: str = "0" * 64,
    generation: int = 1,
) -> RuntimeTaskHitlCheckpoint:
    return RuntimeTaskHitlCheckpoint(
        task_id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        execution_user_id=execution_user_id,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
        conversation_id=conversation_id,
        hitl_id=hitl_id,
        approval_binding_digest=approval_binding_digest,
        generation=generation,
        messages=runtime_serialize_task_messages(
            [
                ChatMessage(role="system", content="Follow policy."),
                ChatMessage(role="user", content="每天早上告诉我 AI 新闻。"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "call-create",
                            "name": "create_agent_initiative",
                            "arguments": {
                                "name": "AI news",
                                "schedule": {"kind": "cron", "hour": 8},
                            },
                        }
                    ],
                ),
            ]
        ),
        usage={"prompt_tokens": 19, "completion_tokens": 7},
        turns_used=2,
        tool_calls_made=["create_agent_initiative"],
        supervisor_evidence=[
            {"tool": "create_agent_initiative", "result": "approval_required"}
        ],
        remaining_tool_calls=[
            {
                "id": "call-read",
                "name": "list_agent_initiatives",
                "arguments": {},
            }
        ],
        disable_followup_tools=False,
    )


_APPROVED_TOOL_NAME = "create_agent_initiative"
_APPROVED_TOOL_ARGUMENTS = {"name": "AI news"}


def _approved_runtime_metadata(
    approval_token: str,
    *,
    tool_name: str = _APPROVED_TOOL_NAME,
    arguments: dict | None = None,
) -> dict:
    call_arguments = dict(
        _APPROVED_TOOL_ARGUMENTS if arguments is None else arguments
    )
    call_arguments["approval_token"] = approval_token
    return {
        "extra_tool_names": [tool_name],
        "forced_tool_calls": [
            {
                "name": tool_name,
                "arguments": call_arguments,
                "disable_followup_tools": True,
            }
        ],
        "approval_resume_guidance": "Execute only the exact approved call.",
    }


def _resume_delivery_identity(
    task: Task,
    request: HitlRequest,
    checkpoint: RuntimeTaskHitlCheckpoint,
) -> dict:
    resolution = request.context[TASK_RUNTIME_RESOLUTION_CONTEXT_KEY]
    return {
        "runtime_hitl_id": request.id,
        "runtime_hitl_generation": checkpoint.generation,
        "runtime_hitl_checkpoint_digest": checkpoint.digest(),
        "runtime_hitl_checkpoint_signature": request.context[
            TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY
        ],
        "runtime_hitl_resolution_signature": resolution["resolution_signature"],
        "agent_id": checkpoint.agent_id,
        "scheduled_run_id": checkpoint.scheduled_run_id,
        "scheduled_job_id": checkpoint.scheduled_job_id,
    }


def test_task_runtime_checkpoint_round_trips_exact_transcript_and_digest() -> None:
    checkpoint = _checkpoint(
        task_id="task-checkpoint",
        conversation_id="conversation-checkpoint",
        hitl_id="hitl-checkpoint",
    )

    payload = checkpoint.to_dict()
    restored = RuntimeTaskHitlCheckpoint.from_dict(payload)
    messages = runtime_deserialize_task_messages(restored.messages)

    assert payload["schema_version"] == 1
    assert restored == checkpoint
    assert restored.digest() == checkpoint.digest()
    assert [message.role for message in messages] == ["system", "user", "assistant"]
    assert messages[1].content == "每天早上告诉我 AI 新闻。"
    assert messages[2].tool_calls == checkpoint.messages[2]["tool_calls"]

    # Checkpoint data is detached from both the source messages and public copy.
    payload["messages"][2]["tool_calls"][0]["arguments"]["name"] = "tampered"
    assert restored.messages[2]["tool_calls"][0]["arguments"]["name"] == "AI news"
    assert RuntimeTaskHitlCheckpoint.from_dict(payload).digest() != checkpoint.digest()


def test_task_runtime_checkpoint_signature_rejects_tampered_payload() -> None:
    checkpoint = _checkpoint(
        task_id="task-signed",
        conversation_id="conversation-signed",
        hitl_id="hitl-signed",
    )
    signature = checkpoint.signature()
    payload = checkpoint.to_dict()
    payload["messages"][1]["content"] = "Run a different action."
    tampered = RuntimeTaskHitlCheckpoint.from_dict(payload)

    assert checkpoint.verify_signature(signature)
    assert not tampered.verify_signature(signature)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=2),
        lambda payload: payload.update(schema_version=999),
        lambda payload: payload.update(task_id=""),
        lambda payload: payload.update(generation=0),
        lambda payload: payload.update(messages="not-a-list"),
        lambda payload: payload["messages"][0].update(role="developer"),
        lambda payload: payload["messages"][2].update(tool_calls={"bad": "shape"}),
        lambda payload: payload["messages"][2].update(tool_calls=["not-a-call"]),
        lambda payload: payload.update(usage=[]),
        lambda payload: payload.update(turns_used=True),
        lambda payload: payload.update(turns_used=-1),
        lambda payload: payload.update(tool_calls_made="create_agent_initiative"),
        lambda payload: payload.update(remaining_tool_calls=["not-a-call"]),
        lambda payload: payload.update(remaining_tool_calls={}),
        lambda payload: payload.update(supervisor_evidence=["not-evidence"]),
        lambda payload: payload.update(supervisor_evidence={}),
        lambda payload: payload.update(disable_followup_tools="false"),
    ],
)
def test_task_runtime_checkpoint_rejects_non_executable_shapes(mutation) -> None:
    payload = _checkpoint(
        task_id="task-invalid",
        conversation_id="conversation-invalid",
        hitl_id="hitl-invalid",
    ).to_dict()
    mutation(payload)

    with pytest.raises((TypeError, ValueError)):
        RuntimeTaskHitlCheckpoint.from_dict(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": ""},
        {"generation": True},
        {"messages": "not-a-list"},
        {"usage": []},
        {"turns_used": True},
        {"turns_used": -1},
        {"tool_calls_made": "create_agent_initiative"},
        {"remaining_tool_calls": {}},
        {"supervisor_evidence": {}},
        {"disable_followup_tools": "false"},
    ],
)
def test_task_runtime_checkpoint_rejects_non_executable_shapes_on_write(
    changes,
) -> None:
    checkpoint = _checkpoint(
        task_id="task-invalid-write",
        conversation_id="conversation-invalid-write",
        hitl_id="hitl-invalid-write",
    )

    with pytest.raises((TypeError, ValueError)):
        replace(checkpoint, **changes).to_dict()


def test_task_runtime_checkpoint_rejects_oversized_write_and_restore() -> None:
    checkpoint = _checkpoint(
        task_id="task-oversized",
        conversation_id="conversation-oversized",
        hitl_id="hitl-oversized",
    )
    oversized_content = "x" * (RUNTIME_TASK_HITL_CHECKPOINT_MAX_BYTES + 1)
    checkpoint.messages[1]["content"] = oversized_content

    with pytest.raises(ValueError, match="too large"):
        checkpoint.to_dict()

    payload = _checkpoint(
        task_id="task-oversized",
        conversation_id="conversation-oversized",
        hitl_id="hitl-oversized",
    ).to_dict()
    payload["messages"][1]["content"] = oversized_content
    with pytest.raises(ValueError, match="too large"):
        RuntimeTaskHitlCheckpoint.from_dict(payload)


def test_public_task_update_cannot_overwrite_runtime_approval_pointer() -> None:
    from apps.api.routers.tasks import _without_task_runtime_details

    assert _without_task_runtime_details({
        "customer_note": "keep this",
        TASK_RUNTIME_HITL_DETAIL_KEY: {
            "state": "resume_queued",
            "choice": "approved",
        },
    }) == {"customer_note": "keep this"}


async def _seed_staged_approval(
    db_session,
    *,
    approval_status: str,
    checkpoint_task_id: str | None = None,
    checkpoint_conversation_id: str | None = None,
    checkpoint_hitl_id: str | None = None,
) -> tuple[Task, HitlRequest, RuntimeTaskHitlCheckpoint]:
    entity_id = generate_ulid()
    user_id = generate_ulid()
    task_id = generate_ulid()
    conversation_id = generate_ulid()
    hitl_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    scheduled_run_id = generate_ulid()
    scheduled_job_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        agent_subscription_id=subscription_id,
        title="Scheduled agent",
        status="active",
    )
    request = HitlRequest(
        id=hitl_id,
        entity_id=entity_id,
        action_key="agent_initiative.create",
        capability_id="agent.initiative.manage",
        hitl_type="authorize",
        risk_level="medium",
        origin_kind="tool_call",
        origin_conversation_id=conversation_id,
        origin_task_id=task_id,
        status=approval_status,
        dedup_key=f"task-runtime:{task_id}:{hitl_id}",
        context={
            "tool": _APPROVED_TOOL_NAME,
            "args_hash": approval_args_hash(_APPROVED_TOOL_ARGUMENTS),
            "requested_by": user_id,
            "continuation": {
                "version": 1,
                "tool": _APPROVED_TOOL_NAME,
                "arguments": dict(_APPROVED_TOOL_ARGUMENTS),
            },
        },
    )
    checkpoint = _checkpoint(
        task_id=checkpoint_task_id or task_id,
        entity_id=entity_id,
        workspace_id=None,
        execution_user_id=user_id,
        agent_id=agent_id,
        agent_subscription_id=subscription_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=scheduled_job_id,
        conversation_id=checkpoint_conversation_id or conversation_id,
        hitl_id=checkpoint_hitl_id or hitl_id,
        approval_binding_digest=task_runtime_approval_binding_digest(request),
    )
    checkpoint_signature = checkpoint.signature()
    request.context = {
        **request.context,
        TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY: checkpoint.to_dict(),
        TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY: checkpoint_signature,
    }
    marker = {
        "hitl_id": hitl_id,
        "tool_name": "create_agent_initiative",
        "generation": checkpoint.generation,
        "checkpoint_digest": checkpoint.digest(),
        "checkpoint_signature": checkpoint_signature,
        "state": "waiting",
    }
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Daily AI news",
        status="waiting_on_customer",
        task_type="agent",
        creator_id=user_id,
        agent_id=agent_id,
        owner_subscription_id=subscription_id,
        conversation_id=conversation_id,
        details={
            "scheduled_run_id": scheduled_run_id,
            "scheduled_job_id": scheduled_job_id,
            TASK_RUNTIME_HITL_DETAIL_KEY: marker,
        },
    )
    subscription = AgentSubscription(
        id=subscription_id,
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=None,
        owner_user_id=user_id,
        status="active",
    )
    owner = User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
        status="active",
    )
    db_session.add_all([owner, subscription, conversation, task, request])
    await db_session.flush()
    return task, request, checkpoint


async def _claim_and_consume_runtime_approval(
    db_session,
    *,
    task_id: str,
    request_id: str,
) -> tuple[bool, HitlRequest]:
    """Emulate the exact standard-tool boundary inside the fake Agent turn."""

    from packages.core.governance.approvals import consume_approval

    request = await db_session.get(HitlRequest, request_id)
    assert request is not None
    claimed = await claim_task_runtime_approval_execution(
        db_session,
        request=request,
        task_id=task_id,
    )
    await consume_approval(db_session, request)
    await db_session.flush()
    return claimed, request


@pytest.mark.integration
async def test_delete_scheduled_job_closes_waiting_runtime_approval(
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import delete_scheduled_job

    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    job_id = task.details["scheduled_job_id"]
    run_id = task.details["scheduled_run_id"]
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=task.entity_id,
        name="Delete suspended Agent job",
        execution_type="agent",
        enabled=True,
        manor_task_id=task.id,
    )
    run = ScheduledJobRun(
        id=run_id,
        job_id=job_id,
        status="running",
    )
    task_id = task.id
    request_id = request.id
    scheduled_job_id = job.id
    db_session.add_all([job, run])
    await db_session.commit()

    assert await delete_scheduled_job(db_session, scheduled_job_id, task.entity_id) is True
    await db_session.commit()

    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert persisted_task.status == "cancelled"
    assert persisted_task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "finished"
    assert persisted_task.details["scheduled_suppression_reason"] == (
        "scheduled_job_deleted"
    )
    assert persisted_request.status == ApprovalStatus.EXPIRED.value
    assert persisted_request.resolved_reason == "scheduled_job_deleted"
    assert await db_session.get(ScheduledJobRun, run_id) is None
    assert await db_session.get(ScheduledJob, scheduled_job_id) is None


@pytest.mark.integration
async def test_bulk_terminal_update_closes_waiting_runtime_approval(
    db_session,
) -> None:
    from packages.core.services.bulk_service import bulk_update_task_status

    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    task_id = task.id
    request_id = request.id
    entity_id = task.entity_id
    await db_session.commit()

    updated = await bulk_update_task_status(
        db_session,
        entity_id,
        [task_id],
        "cancelled",
    )
    await db_session.commit()

    assert updated == 1
    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert persisted_task.status == "cancelled"
    assert (
        persisted_task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"]
        == "finished"
    )
    assert persisted_request.status == ApprovalStatus.EXPIRED.value
    assert persisted_request.resolved_reason == "task_cancelled"


@pytest.mark.integration
async def test_delete_scheduled_job_preserves_executing_provider_lineage(
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        ScheduledAgentLineageDeletionBlockedError,
    )
    from packages.core.services.scheduler_service import delete_scheduled_job

    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    task.status = "pending"
    task.details = {
        **task.details,
        TASK_RUNTIME_HITL_DETAIL_KEY: {
            **task.details[TASK_RUNTIME_HITL_DETAIL_KEY],
            "state": "resuming",
        },
    }
    request.context = {
        **request.context,
        "provider_action_claim": {"claim_id": generate_ulid()},
    }
    job_id = task.details["scheduled_job_id"]
    run_id = task.details["scheduled_run_id"]
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=task.entity_id,
        name="Keep executing provider lineage",
        execution_type="agent",
        enabled=True,
        manor_task_id=task.id,
    )
    run = ScheduledJobRun(id=run_id, job_id=job_id, status="running")
    task_id = task.id
    entity_id = task.entity_id
    request_id = request.id
    scheduled_job_id = job.id
    db_session.add_all([job, run])
    await db_session.commit()

    with pytest.raises(ScheduledAgentLineageDeletionBlockedError) as exc_info:
        await delete_scheduled_job(db_session, scheduled_job_id, entity_id)
    assert exc_info.value.task_ids == (task_id,)
    await db_session.rollback()

    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert persisted_task.status == "pending"
    assert persisted_task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == (
        "resuming"
    )
    assert persisted_request.status == ApprovalStatus.GRANTED.value
    assert await db_session.get(ScheduledJobRun, run_id) is not None
    assert await db_session.get(ScheduledJob, scheduled_job_id) is not None


@pytest.mark.integration
async def test_delete_workspace_automations_closes_resume_queued_runtime_approval(
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace import Workspace
    from packages.core.services.scheduler_service import delete_workspace_automations

    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=task.entity_id,
        name="Delete runtime automation workspace",
        status="active",
        settings={},
        operating_model={},
    )
    db_session.add(workspace)
    await db_session.flush()
    task.workspace_id = workspace.id
    request.workspace_id = workspace.id
    conversation = await db_session.get(Conversation, task.conversation_id)
    conversation.workspace_id = workspace.id
    task.status = "pending"
    task.details = {
        **task.details,
        TASK_RUNTIME_HITL_DETAIL_KEY: {
            **task.details[TASK_RUNTIME_HITL_DETAIL_KEY],
            "state": "resume_queued",
            "choice": "approved",
        },
    }
    job_id = task.details["scheduled_job_id"]
    run_id = task.details["scheduled_run_id"]
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=task.entity_id,
        workspace_id=workspace.id,
        name="Delete suspended Workspace Agent job",
        execution_type="agent",
        enabled=True,
        manor_task_id=task.id,
    )
    run = ScheduledJobRun(
        id=run_id,
        job_id=job_id,
        status="running",
    )
    task_id = task.id
    entity_id = task.entity_id
    request_id = request.id
    scheduled_job_id = job.id
    workspace_id = workspace.id
    db_session.add_all([job, run])
    await db_session.commit()

    result = await delete_workspace_automations(
        db_session,
        workspace_id,
        entity_id,
    )
    await db_session.commit()

    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert result == {"scheduled_jobs": 1, "workflow_bindings": 0}
    assert persisted_task.status == "cancelled"
    assert persisted_task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "finished"
    assert persisted_task.details["scheduled_suppression_reason"] == (
        "workspace_automation_deleted"
    )
    assert persisted_request.status == ApprovalStatus.EXPIRED.value
    assert persisted_request.resolved_reason == "workspace_automation_deleted"
    assert await db_session.get(ScheduledJobRun, run_id) is None
    assert await db_session.get(ScheduledJob, scheduled_job_id) is None


@pytest.mark.integration
async def test_task_runner_repairs_status_tamper_without_reentering_model(
    db_session,
    monkeypatch,
) -> None:
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module

    task, _request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    task_id = task.id
    # Simulate a generic/bulk Task status update racing the approval card.
    task.status = "pending"
    await db_session.commit()

    async def forbidden_runtime_setup(*_args, **_kwargs):
        raise AssertionError("a waiting runtime gate must not re-enter setup")

    monkeypatch.setattr(
        task_runner_module,
        "runtime_resolve_task_billable_user_id",
        forbidden_runtime_setup,
    )

    result = await TaskRunner(session_factory=db_module.async_session).run(task_id)

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    assert result == {
        "task_id": task_id,
        "status": "waiting_on_customer",
        "waiting_for_runtime_approval": True,
    }
    assert persisted.status == "waiting_on_customer"
    assert persisted.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "waiting"


@pytest.mark.integration
async def test_persist_task_runtime_gate_keeps_checkpoint_private_and_card_idempotent(
    db_session,
) -> None:
    task, request, _checkpoint_before = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    task.details = {}
    blocking_result = {
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": request.id,
        "hitl": {
            "id": request.id,
            "type": "approval",
            "prompt": "Create this scheduled update?",
            "action": request.action_key,
            "capability_id": request.capability_id,
            "tool": "create_agent_initiative",
            "options": ["approve", "reject"],
        },
    }
    messages = [
        ChatMessage(role="user", content="每天早上告诉我 AI 新闻。"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-create",
                    "name": "create_agent_initiative",
                    "arguments": {"name": "AI news"},
                }
            ],
        ),
    ]

    first = await persist_task_runtime_approval_gate(
        db_session,
        task=task,
        execution_user_id=task.creator_id,
        blocking_control={
            "tool_name": "create_agent_initiative",
            "blocking_result": blocking_result,
        },
        tool_arguments={"name": "AI news"},
        messages=messages,
        usage={"total_tokens": 26},
        turns_used=2,
        tool_calls_made=["create_agent_initiative"],
        supervisor_evidence=[],
    )
    repeated = await persist_task_runtime_approval_gate(
        db_session,
        task=task,
        execution_user_id=task.creator_id,
        blocking_control={
            "tool_name": "create_agent_initiative",
            "blocking_result": blocking_result,
        },
        tool_arguments={"name": "AI news"},
        messages=messages,
        usage={"total_tokens": 26},
        turns_used=2,
        tool_calls_made=["create_agent_initiative"],
        supervisor_evidence=[],
    )
    await db_session.flush()

    assert first == repeated
    assert TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY in request.context
    assert TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY not in first
    assert set(task_runtime_hitl_details(first)) == {
        "checkpoint_digest",
        "checkpoint_signature",
        "generation",
        "hitl_id",
        "state",
        "tool_name",
    }
    card_count = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == task.conversation_id)
    )
    assert card_count == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("approval_status", "choice", "expected_choice"),
    [
        (
            ApprovalStatus.GRANTED.value,
            "approve",
            "approved",
        ),
        (ApprovalStatus.DENIED.value, "reject", "rejected"),
    ],
)
async def test_stage_and_load_task_runtime_resolution_preserve_exact_lineage(
    db_session,
    approval_status,
    choice,
    expected_choice,
) -> None:
    task, request, checkpoint = await _seed_staged_approval(
        db_session,
        approval_status=approval_status,
    )
    runtime_metadata = (
        _approved_runtime_metadata(request.id) if choice == "approve" else None
    )

    resolution = await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice=choice,
        runtime_metadata=runtime_metadata,
    )
    await db_session.flush()
    event = (
        await db_session.execute(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.dedupe_key
                == f"{TASK_RUNTIME_RESUME_EVENT}:approval:{request.id}"
            )
        )
    ).scalar_one()
    resume = await load_task_runtime_approval_resume(
        db_session,
        task,
        delivery_identity=dict(event.payload),
    )

    assert resolution == {
        "approval_kind": "task_runtime",
        "task_runtime_resume": {
            "task_id": task.id,
            "hitl_id": request.id,
            "generation": checkpoint.generation,
            "choice": expected_choice,
        },
    }
    assert task.status == "pending"
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "resume_queued"
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["choice"] == expected_choice
    assert request.context[TASK_RUNTIME_RESOLUTION_CONTEXT_KEY]["choice"] == (
        expected_choice
    )
    assert resume is not None
    assert resume.choice == expected_choice
    assert resume.checkpoint == checkpoint
    assert resume.messages[1].content == "每天早上告诉我 AI 新闻。"
    assert resume.runtime_metadata == (runtime_metadata or {})

    assert event.event_type == TASK_RUNTIME_RESUME_EVENT
    assert event.aggregate_id == task.id
    assert event.payload == _resume_delivery_identity(task, request, checkpoint) | {
        "task_id": task.id,
    }


@pytest.mark.integration
async def test_stage_task_runtime_resolution_deduplicates_resume_outbox(db_session) -> None:
    task, request, checkpoint = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    runtime_metadata = _approved_runtime_metadata(request.id)

    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=runtime_metadata,
    )
    await db_session.flush()

    # Simulate a resolver replay after the same event is already durable.  The
    # outbox identity, not process memory, must prevent a second delivery row.
    details = deepcopy(task.details)
    details[TASK_RUNTIME_HITL_DETAIL_KEY] = {
        **details[TASK_RUNTIME_HITL_DETAIL_KEY],
        "state": "waiting",
    }
    task.details = details
    task.status = "waiting_on_customer"
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=runtime_metadata,
    )
    await db_session.flush()

    count = await db_session.scalar(
        select(func.count())
        .select_from(RuntimeOutboxEvent)
        .where(
            RuntimeOutboxEvent.dedupe_key
            == f"{TASK_RUNTIME_RESUME_EVENT}:approval:{request.id}"
        )
    )
    assert count == 1
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["generation"] == (
        checkpoint.generation
    )


@pytest.mark.integration
async def test_stage_task_runtime_resolution_rejects_malformed_continuations(
    db_session,
) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    valid = _approved_runtime_metadata(request.id)
    invalid_values = [
        None,
        {},
        {**valid, "forced_tool_calls": []},
        {
            **valid,
            "forced_tool_calls": [
                *valid["forced_tool_calls"],
                valid["forced_tool_calls"][0],
            ],
        },
        {
            **valid,
            "forced_tool_calls": [{"name": "", "arguments": {}}],
        },
        {
            **valid,
            "forced_tool_calls": [
                {
                    "name": _APPROVED_TOOL_NAME,
                    "arguments": [],
                    "disable_followup_tools": True,
                }
            ],
        },
        {
            **valid,
            "forced_tool_calls": [
                {
                    "name": _APPROVED_TOOL_NAME,
                    "arguments": {},
                    "disable_followup_tools": False,
                }
            ],
        },
        {
            **valid,
            "forced_tool_calls": [
                {
                    **valid["forced_tool_calls"][0],
                    "disable_followup_tools": False,
                }
            ],
        },
        {**valid, "extra_tool_names": ["different_tool"]},
        {**valid, "extra_tool_names": _APPROVED_TOOL_NAME},
        {**valid, "not_json": object()},
        _approved_runtime_metadata(
            request.id,
            arguments={"oversized": "x" * 128_001},
        ),
        # Shape-valid continuations remain invalid if any approved field is
        # substituted after the user made the decision.
        _approved_runtime_metadata(request.id, tool_name="different_tool"),
        _approved_runtime_metadata(
            request.id,
            arguments={"name": "Different news"},
        ),
        _approved_runtime_metadata("different-approval-token"),
    ]

    for runtime_metadata in invalid_values:
        with pytest.raises(TaskRuntimeApprovalError):
            await stage_task_runtime_approval_resolution(
                db_session,
                request=request,
                choice="approve",
                runtime_metadata=runtime_metadata,
            )

    assert task.status == "waiting_on_customer"
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "waiting"
    count = await db_session.scalar(
        select(func.count())
        .select_from(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.aggregate_id == task.id)
    )
    assert count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("checkpoint_task_id", "checkpoint_conversation_id", "checkpoint_hitl_id"),
    [
        ("foreign-task", None, None),
        (None, "foreign-conversation", None),
        (None, None, "foreign-hitl"),
    ],
)
async def test_stage_task_runtime_resolution_rejects_checkpoint_identity_mismatch(
    db_session,
    checkpoint_task_id,
    checkpoint_conversation_id,
    checkpoint_hitl_id,
) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
        checkpoint_task_id=checkpoint_task_id,
        checkpoint_conversation_id=checkpoint_conversation_id,
        checkpoint_hitl_id=checkpoint_hitl_id,
    )

    with pytest.raises(TaskRuntimeApprovalError, match="lineage"):
        await stage_task_runtime_approval_resolution(
            db_session,
            request=request,
            choice="approve",
            runtime_metadata=_approved_runtime_metadata(request.id),
        )

    assert task.status == "waiting_on_customer"
    count = await db_session.scalar(
        select(func.count())
        .select_from(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.aggregate_id == task.id)
    )
    assert count == 0


@pytest.mark.integration
async def test_stage_task_runtime_resolution_rejects_tampered_checkpoint_signature(
    db_session,
) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    context = deepcopy(request.context)
    context[TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY]["messages"][1][
        "content"
    ] = "Run a different approved action."
    request.context = context

    with pytest.raises(TaskRuntimeApprovalError, match="signature"):
        await stage_task_runtime_approval_resolution(
            db_session,
            request=request,
            choice="approve",
            runtime_metadata=_approved_runtime_metadata(request.id),
        )

    assert task.status == "waiting_on_customer"
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["state"] == "waiting"
    count = await db_session.scalar(
        select(func.count())
        .select_from(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.aggregate_id == task.id)
    )
    assert count == 0


@pytest.mark.integration
async def test_task_origin_without_checkpoint_fails_closed(db_session) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    context = deepcopy(request.context)
    context.pop(TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY)
    context.pop(TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY)
    request.context = context

    with pytest.raises(TaskRuntimeApprovalError, match="no durable checkpoint"):
        await ensure_task_runtime_approval_resolvable(
            db_session,
            request=request,
        )

    changed = await fail_task_runtime_approval_origin(
        db_session,
        request=request,
        reason="runtime_approval_checkpoint_invalid",
    )
    await db_session.flush()

    assert changed is True
    assert request.status == ApprovalStatus.EXPIRED.value
    assert request.resolved_reason == "runtime_approval_checkpoint_invalid"
    assert task.status == "failed"
    marker = task.details[TASK_RUNTIME_HITL_DETAIL_KEY]
    assert marker["state"] == "finished"
    assert marker["result_status"] == "failed"
    assert marker["failure_reason"] == "runtime_approval_checkpoint_invalid"
    events = (await db_session.execute(
        select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == task.id)
    )).scalars().all()
    # Exactly one terminal gateway delivery: staged by the Task terminal
    # transition hook, not duplicated by this function.
    assert len(events) == 1
    assert events[0].dedupe_key == (
        f"scheduled-task-terminal:{task.details['scheduled_run_id']}:{task.id}"
    )
    assert events[0].payload["task_id"] == task.id
    assert events[0].payload["scheduled_run_id"] == task.details["scheduled_run_id"]


@pytest.mark.integration
async def test_load_task_runtime_resolution_rejects_choice_mismatch(db_session) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=_approved_runtime_metadata(request.id),
    )
    await db_session.flush()
    details = deepcopy(task.details)
    details[TASK_RUNTIME_HITL_DETAIL_KEY]["choice"] = "rejected"
    task.details = details

    with pytest.raises(TaskRuntimeApprovalError, match="lineage|choice"):
        await load_task_runtime_approval_resume(
            db_session,
            task,
            delivery_identity=_resume_delivery_identity(
                task,
                request,
                _checkpoint_value,
            ),
        )


@pytest.mark.integration
async def test_load_task_runtime_resolution_rejects_tampered_runtime_metadata(
    db_session,
) -> None:
    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=_approved_runtime_metadata(request.id),
    )
    await db_session.flush()

    context = deepcopy(request.context)
    context[TASK_RUNTIME_RESOLUTION_CONTEXT_KEY]["runtime_metadata"][
        "forced_tool_calls"
    ][0]["arguments"]["name"] = "Different news"
    request.context = context

    with pytest.raises(
        TaskRuntimeApprovalError,
        match="resolution signature is invalid|does not match",
    ):
        await load_task_runtime_approval_resume(
            db_session,
            task,
            delivery_identity=_resume_delivery_identity(
                task,
                request,
                _checkpoint_value,
            ),
        )


@pytest.mark.integration
async def test_claimed_task_runtime_execution_can_be_quarantined_as_ambiguous(
    db_session,
) -> None:
    task, request, checkpoint = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=_approved_runtime_metadata(request.id),
    )

    claimed, request = await _claim_and_consume_runtime_approval(
        db_session,
        task_id=task.id,
        request_id=request.id,
    )
    attempt = request.context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY]
    marker = task.details[TASK_RUNTIME_HITL_DETAIL_KEY]

    assert claimed is True
    assert request.status == ApprovalStatus.CONSUMED.value
    assert attempt["phase"] == "executing"
    assert attempt["completed_at"] is None
    assert attempt["checkpoint_digest"] == checkpoint.digest()
    assert attempt["resolution_signature"] == marker["resolution_signature"]
    assert marker["state"] == "resuming"
    assert marker["execution_attempt_id"] == attempt["attempt_id"]
    assert marker["execution_signature"] == attempt["execution_signature"]

    changed = await mark_task_runtime_approval_execution_ambiguous(
        db_session,
        task=task,
        reason="worker_execution_claim_lost",
    )
    await db_session.flush()

    ambiguous = request.context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY]
    marker = task.details[TASK_RUNTIME_HITL_DETAIL_KEY]
    assert changed is True
    assert ambiguous["attempt_id"] == attempt["attempt_id"]
    assert ambiguous["phase"] == "ambiguous"
    assert ambiguous["completed_at"] is not None
    assert ambiguous["result_status"] == "failed"
    assert len(ambiguous["result_digest"]) == 64
    assert marker["state"] == "finished"
    assert marker["execution_phase"] == "ambiguous"
    assert marker["execution_signature"] == ambiguous["execution_signature"]
    assert marker["result_status"] == "failed"
    assert marker["failure_reason"] == "runtime_approval_execution_ambiguous"


@pytest.mark.integration
async def test_provider_claimed_grant_can_be_quarantined_before_consumption(
    db_session,
) -> None:
    """Provider I/O is ambiguous after its durable claim, even while granted."""

    task, request, _checkpoint_value = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=_approved_runtime_metadata(request.id),
    )
    assert await claim_task_runtime_approval_execution(
        db_session,
        request=request,
        task_id=task.id,
    ) is True
    context = dict(request.context)
    context["kind"] = "provider"
    context["provider_action_claim"] = {
        "tool": "provider_retry",
        "args_hash": "provider-args",
        "claimed_at": "2026-09-06T00:00:00+00:00",
    }
    request.context = context
    await db_session.flush()

    assert request.status == ApprovalStatus.GRANTED.value
    assert await mark_task_runtime_approval_execution_ambiguous(
        db_session,
        task=task,
        reason="provider_worker_lost_after_claim",
    ) is True
    assert request.context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY]["phase"] == (
        "ambiguous"
    )
    assert task.details[TASK_RUNTIME_HITL_DETAIL_KEY]["execution_phase"] == (
        "ambiguous"
    )


@pytest.mark.integration
async def test_exact_redelivery_quarantines_abandoned_execution_without_replay(
    db_session,
) -> None:
    import packages.core.database as db_module

    task, request, checkpoint = await _seed_staged_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice="approve",
        runtime_metadata=_approved_runtime_metadata(request.id),
    )
    identity = _resume_delivery_identity(task, request, checkpoint)
    claimed, _request = await _claim_and_consume_runtime_approval(
        db_session,
        task_id=task.id,
        request_id=request.id,
    )
    assert claimed is True
    assert await validate_task_runtime_resume_delivery(
        db_session,
        task_id=task.id,
        agent_id=identity["agent_id"],
        scheduled_run_id=identity["scheduled_run_id"],
        scheduled_job_id=identity["scheduled_job_id"],
        runtime_hitl_id=identity["runtime_hitl_id"],
        runtime_hitl_generation=identity["runtime_hitl_generation"],
        runtime_hitl_checkpoint_digest=identity[
            "runtime_hitl_checkpoint_digest"
        ],
        runtime_hitl_checkpoint_signature=identity[
            "runtime_hitl_checkpoint_signature"
        ],
        runtime_hitl_resolution_signature=identity[
            "runtime_hitl_resolution_signature"
        ],
    ) is False
    assert await validate_task_runtime_resume_delivery(
        db_session,
        task_id=task.id,
        agent_id=identity["agent_id"],
        scheduled_run_id=identity["scheduled_run_id"],
        scheduled_job_id=identity["scheduled_job_id"],
        runtime_hitl_id=identity["runtime_hitl_id"],
        runtime_hitl_generation=identity["runtime_hitl_generation"],
        runtime_hitl_checkpoint_digest=identity[
            "runtime_hitl_checkpoint_digest"
        ],
        runtime_hitl_checkpoint_signature=identity[
            "runtime_hitl_checkpoint_signature"
        ],
        runtime_hitl_resolution_signature=identity[
            "runtime_hitl_resolution_signature"
        ],
        allow_resuming=True,
    ) is True
    task_id = task.id
    request_id = request.id
    await db_session.commit()

    result = await TaskRunner(session_factory=db_module.async_session).run(
        task_id,
        runtime_hitl_delivery_identity=identity,
    )

    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert result["status"] == "failed"
    assert result["error_type"] == "RuntimeApprovalExecutionAmbiguous"
    assert persisted_task.status == "failed"
    assert persisted_task.details[TASK_RUNTIME_HITL_DETAIL_KEY][
        "execution_phase"
    ] == "ambiguous"
    assert persisted_request.context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY][
        "phase"
    ] == "ambiguous"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("approval_status", "choice", "expected_status"),
    [
        (
            ApprovalStatus.GRANTED.value,
            "approve",
            "completed",
        ),
        (ApprovalStatus.DENIED.value, "reject", "cancelled"),
    ],
)
async def test_task_runner_closes_resume_marker_at_terminal_state(
    db_session,
    monkeypatch,
    approval_status,
    choice,
    expected_status,
) -> None:
    import packages.core.ai.task_runner as task_runner_module
    import packages.core.database as db_module
    import packages.core.services.model_resolver as model_resolver
    import packages.core.services.workspace_runtime as workspace_runtime

    class _NoopBillingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class _Engine:
        def __init__(self):
            self.config = LLMConfig(model="checkpoint-test")

    task, request, checkpoint = await _seed_staged_approval(
        db_session,
        approval_status=approval_status,
    )
    if choice == "reject":
        # Rejection is a deterministic no-execution terminal path. It must not
        # depend on runtime setup that may have changed while the user waited.
        details = deepcopy(task.details)
        details["scheduled_skill_id"] = generate_ulid()
        task.details = details
    task_id = task.id
    request_id = request.id
    checkpoint_digest = checkpoint.digest()
    checkpoint_signature = request.context[
        TASK_RUNTIME_CHECKPOINT_SIGNATURE_CONTEXT_KEY
    ]
    runtime_metadata = (
        _approved_runtime_metadata(request.id) if choice == "approve" else None
    )
    await stage_task_runtime_approval_resolution(
        db_session,
        request=request,
        choice=choice,
        runtime_metadata=runtime_metadata,
    )
    event = (
        await db_session.execute(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.dedupe_key
                == f"{TASK_RUNTIME_RESUME_EVENT}:approval:{request.id}"
            )
        )
    ).scalar_one()
    delivery_identity = dict(event.payload)
    resolution_signature = request.context[
        TASK_RUNTIME_RESOLUTION_CONTEXT_KEY
    ]["resolution_signature"]
    await db_session.commit()

    async def fake_runtime(_db, **_kwargs):
        return workspace_runtime.WorkspaceRuntimeEnvelope(
            workspace_id=None,
            task_id=task.id,
            is_master=False,
            bound_tool_names=set(),
            mcp_allowed_names=set(),
        )

    async def fake_prompt(*_args, **_kwargs):
        return SimpleNamespace(
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=None,
        )

    execution_claims: list[bool] = []

    async def fake_agent_turn(**kwargs):
        if choice == "approve":
            async with db_module.async_session() as execution_db:
                claimed, _claimed_request = (
                    await _claim_and_consume_runtime_approval(
                        execution_db,
                        task_id=task_id,
                        request_id=request_id,
                    )
                )
                execution_claims.append(claimed)
                await execution_db.commit()
        messages = [
            *kwargs["messages"],
            ChatMessage(role="assistant", content="Approved action completed."),
        ]
        return RuntimeTaskAgentTurnResult(
            messages=messages,
            tools=list(kwargs["tools"]),
            loaded_tool_names=set(kwargs["loaded_tool_names"]),
            response_text="Approved action completed.",
            tool_names=["approved_tool"],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            had_tool_calls=False,
        )

    async def fake_supervisor(**_kwargs):
        return {"verdict": "done", "reason": "complete"}

    async def fake_noop(*_args, **_kwargs):
        return None

    billable_user_calls: list[bool] = []

    async def fake_billable_user(*_args, **_kwargs):
        billable_user_calls.append(True)
        return task.creator_id

    monkeypatch.setattr(
        task_runner_module,
        "runtime_resolve_task_billable_user_id",
        fake_billable_user,
    )
    monkeypatch.setattr(
        model_resolver,
        "resolve_model_for_user",
        fake_noop,
    )
    monkeypatch.setattr(
        model_resolver,
        "resolve_llm_metadata_for_user",
        fake_noop,
    )
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", fake_runtime)
    monkeypatch.setattr(
        task_runner_module,
        "runtime_prepare_prompt_appendix_for_turn",
        fake_prompt,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_task_llm_billing_context",
        lambda **_kwargs: _NoopBillingContext(),
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_execute_task_agent_turn",
        fake_agent_turn,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_review_task_agent_output",
        fake_supervisor,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_record_task_runner_execution_evidence",
        fake_noop,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_persist_task_runner_runtime_events",
        fake_noop,
    )
    monkeypatch.setattr(
        task_runner_module,
        "runtime_emit_task_runner_status_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(TaskRunner, "_log", fake_noop)

    result = await TaskRunner(
        engine=_Engine(),
        session_factory=db_module.async_session,
    ).run(
        task_id,
        runtime_hitl_delivery_identity=delivery_identity,
    )

    db_session.expire_all()
    persisted = await db_session.get(Task, task_id)
    persisted_request = await db_session.get(HitlRequest, request_id)
    marker = persisted.details[TASK_RUNTIME_HITL_DETAIL_KEY]
    assert result["status"] == expected_status
    assert persisted.status == expected_status
    assert {
        key: marker[key]
        for key in (
            "hitl_id",
            "tool_name",
            "generation",
            "checkpoint_digest",
            "checkpoint_signature",
            "state",
            "choice",
            "resolution_signature",
            "result_status",
        )
    } == {
        "hitl_id": request_id,
        "tool_name": "create_agent_initiative",
        "generation": checkpoint.generation,
        "checkpoint_digest": checkpoint_digest,
        "checkpoint_signature": checkpoint_signature,
        "state": "finished",
        "choice": "approved" if choice == "approve" else "rejected",
        "resolution_signature": resolution_signature,
        "result_status": expected_status,
    }
    assert persisted_request is not None
    if choice == "approve":
        execution = persisted_request.context[TASK_RUNTIME_EXECUTION_CONTEXT_KEY]
        assert execution_claims == [True]
        assert persisted_request.status == ApprovalStatus.CONSUMED.value
        assert execution["phase"] == "completed"
        assert execution["completed_at"] is not None
        assert execution["result_status"] == expected_status
        assert len(execution["result_digest"]) == 64
        assert marker["execution_attempt_id"] == execution["attempt_id"]
        assert marker["execution_phase"] == "completed"
        assert marker["execution_signature"] == execution["execution_signature"]
    else:
        assert execution_claims == []
        assert persisted_request.status == ApprovalStatus.DENIED.value
        assert TASK_RUNTIME_EXECUTION_CONTEXT_KEY not in persisted_request.context
        assert "execution_attempt_id" not in marker
        assert "execution_phase" not in marker
    assert bool(billable_user_calls) is (choice == "approve")
    assert TASK_RUNTIME_CHECKPOINT_CONTEXT_KEY not in marker
    assert TASK_RUNTIME_RESOLUTION_CONTEXT_KEY not in marker
