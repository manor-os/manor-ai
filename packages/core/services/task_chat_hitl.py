"""Project task-level human decisions into Workspace Chat.

Task Detail and Workspace Chat are two views of the same decision.  This
module owns the bridge so a task cannot be waiting for a person while the
workspace chat has no actionable card, and resolving either view closes the
other one in the same transaction.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import (
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.task import (
    TaskApprovalChoice,
    TaskRecoveryChoice,
    TaskType,
)
from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    consume_approval,
    deny_approval,
    grant_approval,
    mint_approval_request,
)
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message, Task
from packages.core.workspace_chat import service as chat_service


def _task_url(task_id: str) -> str:
    return f"/tasks/{task_id}"


def _review_material(task: Task) -> object:
    details = dict(task.details or {})
    for key in ("review_material", "review", "approval_material", "deliverable"):
        if details.get(key) is not None:
            return details[key]
    if task.actual_output is not None:
        return task.actual_output
    if task.expected_output is not None:
        return task.expected_output
    return {
        "title": task.title,
        "description": task.description or "Open the task to review its material.",
    }


async def _message_for_request(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    request_id: str,
) -> Message | None:
    rows = list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Message.resolved_at.is_(None),
            Message.pending_action.isnot(None),
        )
    )).scalars().all())
    for message in rows:
        action = message.pending_action if isinstance(message.pending_action, dict) else {}
        if action.get("approval_request_id") == request_id:
            return message
    return None


async def ensure_task_approval_hitl(
    db: AsyncSession,
    task: Task,
) -> Message | None:
    """Create the one review card for an explicit approval-type task."""
    if task.task_type != TaskType.APPROVAL.value or not task.workspace_id:
        return None

    payload = {
        "action_description": f"Review “{task.title}”",
        "diff": _review_material(task),
        "why": "This task is waiting for your review before it can be closed.",
        "action_to_take": "Open the task to inspect the material, then approve it or request changes.",
        "action_link": _task_url(task.id),
    }
    request = await mint_approval_request(
        db,
        subject=ApprovalSubject(
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            action_key="task.approve",
            resource_kind="task",
            resource_id=task.id,
            requires_approval=True,
        ),
        origin=ApprovalOrigin(
            kind=ApprovalOriginKind.TASK.value,
            task_id=task.id,
            context={"task_title": task.title},
        ),
        reason=payload["why"],
        matched_rule="task.explicit_approval",
        hitl_type=HitlType.REVIEW.value,
        payload=payload,
    )
    existing = await _message_for_request(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        request_id=request.id,
    )
    if existing is not None:
        return existing

    return await chat_service.post_message(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        body=f"Review required for task: {task.title}",
        message_kind="hitl_request",
        author_kind="agent",
        refs=[{"type": "task", "id": task.id, "title": task.title}],
        pending_action={
            "kind": PendingActionKind.TASK_APPROVAL.value,
            "task_id": task.id,
            "approval_request_id": request.id,
            "hitl_type": HitlType.REVIEW.value,
            "payload": payload,
            "prompt": payload["action_description"],
            "review": payload["diff"],
            "review_title": task.title,
            "options": [
                TaskApprovalChoice.APPROVE.value,
                TaskApprovalChoice.REQUEST_CHANGES.value,
            ],
        },
        meta={"task_id": task.id, "task_hitl": True},
    )


async def ensure_task_recovery_hitl(
    db: AsyncSession,
    task: Task,
    *,
    plan_id: Optional[str],
    prompt: str,
    issue: Optional[str] = None,
) -> Message | None:
    """Create the one actionable recovery card for a stopped task."""
    if not task.workspace_id:
        return None

    what_happened = "This task stopped before it finished."
    payload = {
        "what_happened": what_happened,
        "why": (issue or prompt or "The run needs operator guidance before it can continue."),
        "action_to_take": "Add guidance if needed, then retry the task, or cancel it.",
        "action_link": _task_url(task.id),
    }
    request = await mint_approval_request(
        db,
        subject=ApprovalSubject(
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            action_key="task.recover",
            resource_kind="task",
            resource_id=task.id,
        ),
        origin=ApprovalOrigin(
            kind=ApprovalOriginKind.TASK.value,
            task_id=task.id,
            plan_id=plan_id,
            context={"task_title": task.title},
        ),
        reason=payload["why"],
        matched_rule="task.needs_recovery",
        hitl_type=HitlType.ERROR.value,
        payload=payload,
    )
    existing = await _message_for_request(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        request_id=request.id,
    )
    if existing is not None:
        return existing

    return await chat_service.post_message(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        body=prompt or what_happened,
        message_kind="hitl_request",
        author_kind="agent",
        refs=[
            {"type": "task", "id": task.id, "title": task.title},
            *([{"type": "plan", "id": plan_id}] if plan_id else []),
        ],
        pending_action={
            "kind": PendingActionKind.TASK_RECOVERY.value,
            "task_id": task.id,
            "plan_id": plan_id,
            "approval_request_id": request.id,
            "hitl_type": HitlType.ERROR.value,
            "payload": payload,
            "prompt": what_happened,
            "options": [
                TaskRecoveryChoice.RETRY.value,
                TaskRecoveryChoice.CANCEL.value,
            ],
        },
        meta={"task_id": task.id, "plan_id": plan_id, "task_hitl": True},
    )


async def resolve_task_hitl(
    db: AsyncSession,
    *,
    task: Task,
    kind: PendingActionKind,
    choice: str,
    user_id: str,
    note: Optional[str] = None,
) -> None:
    """Close the shared request and every still-open card for this task."""
    normalized = (choice or "").strip().lower()
    requests = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.status == ApprovalStatus.PENDING.value,
        )
    )).scalars().all())
    action_key = (
        "task.approve"
        if kind == PendingActionKind.TASK_APPROVAL
        else "task.recover"
    )
    accepted = normalized in {
        TaskApprovalChoice.APPROVE.value,
        TaskApprovalChoice.APPROVED.value,
        TaskApprovalChoice.YES.value,
        TaskApprovalChoice.ACCEPT.value,
        TaskRecoveryChoice.RETRY.value,
    }
    for request in requests:
        if request.action_key != action_key:
            continue
        if accepted:
            await grant_approval(db, request, by_user_id=user_id, via="task_hitl")
            await consume_approval(db, request)
        else:
            await deny_approval(
                db,
                request,
                by_user_id=user_id,
                via="task_hitl",
                reason=note or normalized or "user declined",
            )

    if not task.workspace_id:
        return
    rows = list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == task.entity_id,
            Conversation.workspace_id == task.workspace_id,
            Message.resolved_at.is_(None),
            Message.pending_action.isnot(None),
        )
    )).scalars().all())
    resolution = {"choice": normalized}
    if note:
        resolution["note"] = note
    for message in rows:
        action = message.pending_action if isinstance(message.pending_action, dict) else {}
        if action.get("kind") == kind.value and action.get("task_id") == task.id:
            await chat_service.resolve_pending_action(
                db,
                message_id=message.id,
                user_id=user_id,
                resolution=resolution,
                emit_followup=False,
            )
