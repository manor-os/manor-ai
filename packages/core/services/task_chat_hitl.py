"""Project task-level human decisions into Workspace Chat.

Task Detail and Workspace Chat are two views of the same decision.  This
module owns the bridge so a task cannot be waiting for a person while the
workspace chat has no actionable card, and resolving either view closes the
other one in the same transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import (
    APPROVAL_LIVE_STATUSES,
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
from packages.core.contracts.tool_effects import (
    EffectReconciliationDecisionFactory,
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


async def _supersede_conflicting_task_recovery_surfaces(
    db: AsyncSession,
    *,
    task: Task,
    keep_request_id: str,
    plan_id: str,
) -> None:
    """Close older Task recovery surfaces before exposing a Plan-bound one."""

    conflicts = list((await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.workspace_id == task.workspace_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.action_key == "task.recover",
            HitlRequest.status.in_([
                status.value for status in APPROVAL_LIVE_STATUSES
            ]),
            HitlRequest.id != keep_request_id,
        )
        .order_by(HitlRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    if not conflicts:
        return

    now = datetime.now(timezone.utc)
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    conflict_ids = [request.id for request in conflicts]
    for request in conflicts:
        request.status = ApprovalStatus.EXPIRED.value
        request.resolved_reason = "superseded_by_effect_reconciliation"
        request.decided_at = now
        request.decided_via = "effect_reconciliation"
        await ledger_adapters.record_approval_event(
            db,
            request,
            ledger_et.APPROVAL_EXPIRED,
        )

    messages = list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == task.entity_id,
            Conversation.workspace_id == task.workspace_id,
            Message.resolved_at.is_(None),
            Message.pending_action["approval_request_id"].as_string().in_(
                conflict_ids
            ),
        )
        .order_by(Message.id)
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalars().all())
    for message in messages:
        message.resolved_at = now
        message.resolution = {
            "choice": "superseded",
            "reason": "superseded_by_effect_reconciliation",
            "plan_id": plan_id,
        }


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
    reconciliation: Optional[dict] = None,
) -> Message | None:
    """Create the one actionable recovery card for a stopped task."""
    if not task.workspace_id:
        return None

    # Recovery ownership is Task-scoped even though effect reconciliation is
    # bound to one Plan. Serializing on the Task keeps two Plan-specific dedup
    # keys from each creating an independently actionable request/card pair.
    locked_task_id = (await db.execute(
        select(Task.id)
        .where(
            Task.id == task.id,
            Task.entity_id == task.entity_id,
            Task.workspace_id == task.workspace_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if locked_task_id is None:
        return None

    what_happened = "This task stopped before it finished."
    needs_reconciliation = bool(
        isinstance(reconciliation, dict) and reconciliation.get("effects")
    )
    reconciliation_snapshot = (
        dict(reconciliation) if needs_reconciliation and reconciliation else {}
    )
    reconciliation_options = (
        EffectReconciliationDecisionFactory.card_options(
            reconciliation_snapshot
        )
        if needs_reconciliation
        else []
    )
    if needs_reconciliation and not plan_id:
        raise ValueError("effect reconciliation requires an exact Plan binding")
    payload = {
        "what_happened": what_happened,
        "why": (issue or prompt or "The run needs operator guidance before it can continue."),
        "action_to_take": (
            EffectReconciliationDecisionFactory.action_to_take(
                reconciliation_snapshot
            )
            if needs_reconciliation
            else "Add guidance if needed, then retry the task, or cancel it."
        ),
        "action_link": _task_url(task.id),
        **(
            {"reconciliation": reconciliation_snapshot}
            if needs_reconciliation
            else {}
        ),
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
            context={
                "task_title": task.title,
                **(
                    {"effect_reconciliation": reconciliation_snapshot}
                    if needs_reconciliation
                    else {}
                ),
            },
        ),
        reason=payload["why"],
        matched_rule="task.needs_recovery",
        hitl_type=HitlType.ERROR.value,
        payload=payload,
        dedup_key=(
            f"task:{task.id}:task.recover:effect_reconciliation:plan:{plan_id}"
            if needs_reconciliation
            else None
        ),
    )
    if needs_reconciliation:
        if (
            str(request.origin_task_id or "") != str(task.id)
            or str(request.origin_plan_id or "") != str(plan_id)
        ):
            raise RuntimeError(
                "effect reconciliation request lost its exact Task/Plan binding"
            )
        await _supersede_conflicting_task_recovery_surfaces(
            db,
            task=task,
            keep_request_id=request.id,
            plan_id=str(plan_id),
        )
        # A dedup hit may be an older recovery card. Refresh the authoritative
        # snapshot in place so a late/new effect cannot leave the Task with a
        # permanently stale but still-PENDING decision surface.
        request_context = dict(request.context or {})
        request_context["effect_reconciliation"] = reconciliation_snapshot
        request.context = request_context
        request.payload = dict(payload)
    existing = await _message_for_request(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        request_id=request.id,
    )
    if existing is not None:
        if needs_reconciliation and isinstance(existing.pending_action, dict):
            pending_action = dict(existing.pending_action)
            card_payload = (
                dict(pending_action.get("payload") or {})
                if isinstance(pending_action.get("payload"), dict)
                else {}
            )
            card_payload["reconciliation"] = reconciliation_snapshot
            pending_action["payload"] = card_payload
            pending_action["options"] = reconciliation_options
            existing.pending_action = pending_action
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
            "options": (
                reconciliation_options
                if needs_reconciliation
                else [
                    TaskRecoveryChoice.RETRY.value,
                    TaskRecoveryChoice.CANCEL.value,
                ]
            ),
        },
        meta={"task_id": task.id, "plan_id": plan_id, "task_hitl": True},
    )


#: ``HitlRequest.action_key`` for a question a legacy agent task asked.
TASK_INPUT_ACTION_KEY = "task.request_input"


async def ensure_task_input_hitl(
    db: AsyncSession,
    task: Task,
    *,
    question: dict,
) -> Message | None:
    """Create the one input card for a task parked by ``request_input``.

    Reuses the free-form ``HUMAN_INPUT`` card shape, keyed by ``task_id``
    (no step): the workspace-chat resolve endpoint routes such a card to the
    legacy task resume rather than to a plan step.
    """
    if not task.workspace_id:
        return None
    text = str(question.get("question") or "").strip()
    if not text:
        return None
    payload = {
        "question": text,
        "why": str(question.get("context") or "The agent needs this to finish the task."),
        "action_to_take": "Answer here or on the task page; the agent resumes with your answer.",
        "action_link": _task_url(task.id),
    }
    for key in ("options", "fields", "context"):
        if question.get(key):
            payload[key] = question[key]
    request = await mint_approval_request(
        db,
        subject=ApprovalSubject(
            entity_id=task.entity_id,
            workspace_id=task.workspace_id,
            action_key=TASK_INPUT_ACTION_KEY,
            resource_kind="task",
            resource_id=task.id,
            risk_level="low",
        ),
        origin=ApprovalOrigin(
            kind=ApprovalOriginKind.TASK.value,
            task_id=task.id,
            context={
                "task_title": task.title,
                "pending_kind": PendingActionKind.HUMAN_INPUT.value,
                "question": dict(question),
            },
        ),
        reason=text,
        matched_rule="task.request_input",
        hitl_type=HitlType.INPUT.value,
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

    body_lines = [f"Question from the agent working on “{task.title}”:", "", text]
    options = question.get("options")
    if isinstance(options, list) and options:
        body_lines.append("")
        body_lines.append("Options: " + " / ".join(str(item) for item in options))
    return await chat_service.post_message(
        db,
        entity_id=task.entity_id,
        workspace_id=task.workspace_id,
        body="\n".join(body_lines),
        message_kind="hitl_request",
        author_kind="agent",
        refs=[{"type": "task", "id": task.id, "title": task.title}],
        pending_action={
            "kind": PendingActionKind.HUMAN_INPUT.value,
            "task_id": task.id,
            "approval_request_id": request.id,
            "hitl_type": HitlType.INPUT.value,
            "payload": payload,
            "prompt": text,
            **({"options": list(options)} if isinstance(options, list) and options else {}),
            "input_schema": {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            },
        },
        meta={"task_id": task.id, "task_hitl": True},
    )


async def resolve_task_input_hitl(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    answer: str,
    payload: Optional[dict] = None,
) -> None:
    """Grant + consume the task's open input request and close its cards."""
    requests = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.action_key == TASK_INPUT_ACTION_KEY,
            HitlRequest.status == ApprovalStatus.PENDING.value,
        )
    )).scalars().all())
    for request in requests:
        request.context = {
            **(request.context or {}),
            "answer": {"answer": answer, "payload": dict(payload or {}), "answered_by": user_id},
        }
        await grant_approval(db, request, by_user_id=user_id, via="task_hitl")
        await consume_approval(db, request)

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
    resolution = {"choice": "respond", "note": answer}
    for message in rows:
        action = message.pending_action if isinstance(message.pending_action, dict) else {}
        if (
            action.get("kind") == PendingActionKind.HUMAN_INPUT.value
            and action.get("task_id") == task.id
            and not action.get("step_id")
        ):
            await chat_service.resolve_pending_action(
                db,
                message_id=message.id,
                user_id=user_id,
                resolution=resolution,
                emit_followup=False,
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
