"""Task-origin runtime approvals: the shared lock, card and close-out boundary."""

from __future__ import annotations

import pytest

from packages.core.ai.runtime.approval_messages import approval_args_hash
from packages.core.constants.approvals import ApprovalStatus
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Task
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription

_APPROVED_TOOL_NAME = "create_agent_initiative"
_APPROVED_TOOL_ARGUMENTS = {"name": "AI news"}


async def _seed_task_approval(
    db_session,
    *,
    approval_status: str,
) -> tuple[Task, HitlRequest]:
    """A scheduled Agent Task waiting on one Task-origin runtime approval."""

    entity_id = generate_ulid()
    user_id = generate_ulid()
    task_id = generate_ulid()
    conversation_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
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
        id=generate_ulid(),
        entity_id=entity_id,
        action_key="agent_initiative.create",
        capability_id="agent.initiative.manage",
        hitl_type="authorize",
        risk_level="medium",
        origin_kind="tool_call",
        origin_conversation_id=conversation_id,
        origin_task_id=task_id,
        status=approval_status,
        dedup_key=f"task-runtime:{task_id}",
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
            "scheduled_run_id": generate_ulid(),
            "scheduled_job_id": generate_ulid(),
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
    return task, request


@pytest.mark.integration
async def test_delete_scheduled_job_closes_waiting_runtime_approval(
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import delete_scheduled_job

    task, request = await _seed_task_approval(
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

    task, request = await _seed_task_approval(
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

    task, request = await _seed_task_approval(
        db_session,
        approval_status=ApprovalStatus.GRANTED.value,
    )
    task.status = "pending"
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
    assert persisted_request.status == ApprovalStatus.GRANTED.value
    assert await db_session.get(ScheduledJobRun, run_id) is not None
    assert await db_session.get(ScheduledJob, scheduled_job_id) is not None


@pytest.mark.integration
async def test_delete_workspace_automations_closes_granted_runtime_approval(
    db_session,
) -> None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workspace import Workspace
    from packages.core.services.scheduler_service import delete_workspace_automations

    task, request = await _seed_task_approval(
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
    assert persisted_task.details["scheduled_suppression_reason"] == (
        "workspace_automation_deleted"
    )
    assert persisted_request.status == ApprovalStatus.EXPIRED.value
    assert persisted_request.resolved_reason == "workspace_automation_deleted"
    assert await db_session.get(ScheduledJobRun, run_id) is None
    assert await db_session.get(ScheduledJob, scheduled_job_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_task_approval_no_agent_loop_parked_on_closes_without_acting(
    db_session,
    action,
) -> None:
    """Every Task runs on the agent loop: an untagged request resumes nothing."""

    from packages.core.ai.runtime.approval_service import resolve_runtime_approval_turn

    task, request = await _seed_task_approval(
        db_session,
        approval_status=ApprovalStatus.PENDING.value,
    )
    task_id, request_id = task.id, request.id
    await db_session.commit()

    result = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=request.origin_conversation_id,
        entity_id=request.entity_id,
        user_id=request.context["requested_by"],
        hitl_id=request_id,
        action=action,
    )
    await db_session.commit()

    assert result.runtime_metadata == {
        "approval_kind": "runtime_terminal",
        "approval_outcome": "expired",
    }
    db_session.expire_all()
    persisted_request = await db_session.get(HitlRequest, request_id)
    assert persisted_request.status == ApprovalStatus.EXPIRED.value
    assert persisted_request.resolved_reason == "agent_loop_approval_not_current"
    assert persisted_request.consumed_at is None
    # The Task is not failed: nothing was wrong with its work.
    assert (await db_session.get(Task, task_id)).status == "waiting_on_customer"

