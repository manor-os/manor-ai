from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.constants.approvals import HitlType
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Message
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
    normalize_approval_choice,
    one_time_approval_options,
)


def hitl_requests_from_data(hitl_data: dict | None) -> list[dict] | None:
    if not isinstance(hitl_data, dict):
        return None
    hitl = hitl_data.get("hitl")
    if not isinstance(hitl, dict):
        return None
    item = dict(hitl)
    operation = hitl_data.get("operation")
    if isinstance(operation, dict) and "operation" not in item:
        item["operation"] = operation
    if not item.get("id") and hitl_data.get("approval_token"):
        item["id"] = hitl_data.get("approval_token")
    return [item] if item.get("id") else None


def workspace_operation_pending_action_from_data(
    hitl_data: dict | None,
) -> dict | None:
    """Build a durable pending action for workspace operation review HITL."""

    if not isinstance(hitl_data, dict):
        return None
    hitl = hitl_data.get("hitl") if isinstance(hitl_data.get("hitl"), dict) else {}
    operation = hitl_data.get("operation")
    if not isinstance(operation, dict):
        operation = (
            hitl.get("operation")
            if isinstance(hitl.get("operation"), dict)
            else {}
        )
    if operation.get("kind") != PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        return None
    draft_id = str(
        operation.get("draft_id")
        or hitl.get("id")
        or hitl_data.get("approval_token")
        or ""
    ).strip()
    if not draft_id:
        return None
    return {
        "kind": PendingActionKind.WORKSPACE_OPERATION_REVIEW.value,
        "draft_id": draft_id,
        "approval_token": draft_id,
        "prompt": hitl.get("prompt") or "Apply this workspace operation draft?",
        "action": hitl.get("action") or "workspace.operation.apply",
        "tool": hitl.get("tool") or "workspace_operation",
        "content": hitl.get("content"),
        "args_preview": hitl.get("args_preview"),
        "operation": operation,
        # The typed review payload — the diff the card renders and, when the
        # draft removes a hard block, the sentence that says so. This blob is
        # the DURABLE card: a payload dropped here is a warning that exists in
        # the tool result and nowhere the operator will ever look.
        "hitl_type": hitl.get("hitl_type") or HitlType.REVIEW.value,
        "payload": hitl.get("payload") if isinstance(hitl.get("payload"), dict) else None,
        # A review is a verdict on this specific diff; there is no standing
        # version of it. See the producer in workspace_operation_actions.
        "options": one_time_approval_options(
            hitl.get("options") if isinstance(hitl.get("options"), list) else None,
        ),
    }


def _is_approval_action(action: str) -> bool:
    normalized = normalize_approval_choice(action)
    return normalized in {APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_ALWAYS_APPROVE}


def _is_rejection_action(action: str) -> bool:
    return normalize_approval_choice(action) == APPROVAL_CHOICE_REJECT


def user_visible_hitl_action_text(action: str) -> str:
    """Return a stable, user-facing transcript line for approval-card clicks."""

    if _is_approval_action(action):
        return "Approved the requested action."
    if _is_rejection_action(action):
        return "Rejected the requested action."
    return "Responded to the approval request."


async def mark_hitl_request_resolved(
    db: AsyncSession,
    *,
    conversation_id: str,
    hitl_id: str,
    choice: str,
) -> int:
    """Mark persisted HITL cards as resolved in conversation message metadata."""

    if not hitl_id:
        return 0
    rows = list((await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(100)
    )).scalars().all())
    updated = 0
    for msg in rows:
        meta = dict(msg.meta or {})
        requests = meta.get("hitl_requests")
        if not isinstance(requests, list):
            continue
        changed = False
        next_requests: list[dict] = []
        for req in requests:
            if isinstance(req, dict) and str(req.get("id") or "") == str(hitl_id):
                req = {**req, "resolved": True, "resolution": choice}
                changed = True
            next_requests.append(req)
        if changed:
            meta["hitl_requests"] = next_requests
            msg.meta = meta
            flag_modified(msg, "meta")
            updated += 1
    return updated
