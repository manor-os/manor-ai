"""Workspace chat HTTP API.

Endpoints (all scoped to ``/api/v1/workspaces/{workspace_id}/chat``):

  GET    /messages                   list messages (main + threads)
  POST   /messages                   user posts a message
  GET    /threads                    list active threads (per task / plan)
  POST   /messages/{id}/resolve      resolve a pending_action message

The chat is rendered live in the workspace UI; the same data is also
the substrate for sandbox demos (a sandbox workspace's chat IS the
demo). External channels (Telegram / WeChat) are **mirrors** of this —
when a workspace_chat post is high-priority (HITL / goal_alert), a
separate notification job fans it out to bound channels, but that
is not handled here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, require_plan
from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS
from packages.core.constants.pending_actions import (
    WORKFLOW_RUN_ACTION_KINDS,
    PendingActionKind,
)
from packages.core.constants.task import TaskStatus
from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.database import get_db
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    ERROR_CHOICE_RETRY,
)
from packages.core.services.workspace_access import (
    user_can_control_workspace_run,
    user_can_read_workspace,
)
from packages.core.workspace_chat import service as chat_service


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/chat",
    tags=["workspace-chat"],
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
# How many open action cards the first page will pin, however old they are.
# Reaching this cap means the client is NOT holding the whole open set, which
# it needs to know before it treats an absent card as answered.
_PINNED_ACTION_LIMIT = 50


# ── Schemas ────────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    body: Optional[str]
    tool_calls: Optional[Any] = None
    assistant_blocks: Optional[list[dict]] = None
    message_kind: str
    author_kind: str
    author_user_id: Optional[str] = None
    author_user_name: Optional[str] = None
    author_user_email: Optional[str] = None
    author_user_avatar_url: Optional[str] = None
    author_subscription_id: Optional[str]
    refs: Optional[list[dict]]
    attachments: Optional[Any]
    meta: Optional[dict]
    pending_action: Optional[dict]
    resolved_at: Optional[datetime]
    resolution: Optional[dict]
    resolved_by_user_id: Optional[str] = None
    resolved_by_user_name: Optional[str] = None
    resolved_by_user_email: Optional[str] = None
    resolved_by_user_avatar_url: Optional[str] = None


class MessagesPageResponse(BaseModel):
    items: list[MessageResponse]
    has_more: bool
    next_cursor: Optional[str] = None
    # Workspace-wide count of action cards still waiting on a human, counted in
    # the DB. The client cannot derive this from `items`: a card answered from
    # this very page leaves the pinned set rather than coming back marked
    # resolved, and the client's merge-by-id never removes what it has seen.
    open_action_count: int = 0
    # True when `items` carries EVERY open action card in the workspace, so an
    # open card the client remembers but that is absent here has been answered.
    open_actions_complete: bool = False


class PostMessageRequest(BaseModel):
    body: str
    thread_ref_kind: Optional[str] = None
    thread_ref_id: Optional[str] = None


class ThreadResponse(BaseModel):
    id: str
    title: Optional[str]
    thread_ref_kind: Optional[str]
    thread_ref_id: Optional[str]
    updated_at: Optional[datetime]


class ResolveActionRequest(BaseModel):
    choice: str
    note: Optional[str] = None
    payload: Optional[dict] = None
    # ``payload`` covers free-form input (e.g. HITL prompt response);
    # ``choice`` covers button-style proposals ("approve" / "reject").


class MessageFeedbackRequest(BaseModel):
    rating: str


# ── Helpers ────────────────────────────────────────────────────────────

def _user_display_name(user: User | None) -> str | None:
    if not user:
        return None
    full_name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        if part
    ).strip()
    return getattr(user, "display_name", None) or full_name or getattr(user, "email", None)


def _message_author_user_id(message: Message) -> str | None:
    meta = message.meta if isinstance(message.meta, dict) else {}
    value = meta.get("author_user_id")
    return str(value) if value else None


async def _load_message_authors(
    db: AsyncSession,
    messages: list[Message],
) -> dict[str, User]:
    """Load every user referenced by a message — its author and, for resolved
    interactive actions, whoever approved/resolved it. Keyed by user id."""
    user_ids = list(dict.fromkeys(
        user_id
        for message in messages
        for user_id in (
            _message_author_user_id(message),
            message.resolved_by_user_id,
        )
        if user_id
    ))
    if not user_ids:
        return {}
    rows = (await db.execute(
        select(User).where(User.id.in_(user_ids), User.deleted_at.is_(None))
    )).scalars().all()
    return {user.id: user for user in rows}


def _to_message(
    m: Message,
    *,
    refs: Optional[list[dict]] = None,
    author_user: User | None = None,
    resolved_by_user: User | None = None,
    updated_at: datetime | None = None,
) -> MessageResponse:
    pending_action = m.pending_action if isinstance(m.pending_action, dict) and m.pending_action.get("kind") else None
    author_user_id = _message_author_user_id(m)
    return MessageResponse(
        id=m.id,
        conversation_id=m.conversation_id,
        created_at=m.created_at,
        updated_at=updated_at,
        body=m.content,
        tool_calls=m.tool_calls,
        assistant_blocks=(m.meta or {}).get("assistant_blocks") if isinstance(m.meta, dict) else None,
        message_kind=m.message_kind,
        author_kind=m.author_kind,
        author_user_id=author_user_id,
        author_user_name=_user_display_name(author_user),
        author_user_email=getattr(author_user, "email", None) if author_user else None,
        author_user_avatar_url=getattr(author_user, "avatar_url", None) if author_user else None,
        author_subscription_id=m.author_subscription_id,
        refs=refs if refs is not None else m.refs,
        attachments=m.attachments,
        meta=m.meta or {},
        pending_action=pending_action,
        resolved_at=m.resolved_at,
        resolution=m.resolution,
        resolved_by_user_id=m.resolved_by_user_id,
        resolved_by_user_name=_user_display_name(resolved_by_user),
        resolved_by_user_email=getattr(resolved_by_user, "email", None) if resolved_by_user else None,
        resolved_by_user_avatar_url=getattr(resolved_by_user, "avatar_url", None) if resolved_by_user else None,
    )


def _encode_message_cursor(message: Message | None) -> str | None:
    if not message or not message.created_at or not message.id:
        return None
    return f"{message.created_at.isoformat()}|{message.id}"


def _decode_message_cursor(value: str | None) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    timestamp, separator, message_id = value.partition("|")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise HTTPException(400, "Invalid message cursor") from exc
    return parsed, message_id if separator and message_id else None


#: Re-exported under the router's historical name — ``apps/api/routers/
#: workflows.py`` imports it from here.
WORKSPACE_WORKFLOW_RUN_ACTION_KINDS = WORKFLOW_RUN_ACTION_KINDS

#: The cards that ask the user to type or confirm something directly, as
#: opposed to deciding a proposal or a policy. Their resolve branches share an
#: evidence type, a summary and an activity event.
_INPUT_CARD_KINDS: frozenset[str] = frozenset({
    PendingActionKind.HUMAN_INPUT.value,
    PendingActionKind.NEEDS_INPUT.value,
    PendingActionKind.NEEDS_CONFIRMATION.value,
    PendingActionKind.NEEDS_LOGIN.value,
})

#: A ``wait`` step inside a running workflow, of either flavour.
_WORKFLOW_WAIT_KINDS: frozenset[str] = frozenset({
    PendingActionKind.WORKFLOW_APPROVAL.value,
    PendingActionKind.WORKFLOW_INPUT.value,
})
_ACTIONABLE_WORKFLOW_STATUSES = {"queued", "pending", "running", "paused", "failed"}
_ACTIONABLE_WORKFLOW_OUTCOMES = {
    "needs_input",
    "revision_required",
    "ready_for_acceptance",
}


def _message_workflow_run_id(message: Message) -> str | None:
    pending_action = message.pending_action if isinstance(message.pending_action, dict) else {}
    meta = message.meta if isinstance(message.meta, dict) else {}
    run_id = pending_action.get("workflow_run_id") or meta.get("workflow_run_id")
    if run_id:
        return str(run_id)
    for ref in message.refs or []:
        if isinstance(ref, dict) and ref.get("type") == "workflow_run" and ref.get("id"):
            return str(ref["id"])
    return None


def _is_actionable_workflow_activity(message: Message) -> bool:
    if message.message_kind != "workflow_activity":
        return False
    meta = message.meta if isinstance(message.meta, dict) else {}
    status = str(meta.get("workflow_status") or "").strip().lower()
    if status in _ACTIONABLE_WORKFLOW_STATUSES:
        return True
    outcome = str(meta.get("workflow_business_outcome") or "").strip().lower()
    return status == "completed" and outcome in _ACTIONABLE_WORKFLOW_OUTCOMES


async def _augment_latest_page_with_workflow_runtime(
    db: AsyncSession,
    *,
    conversation_id: str,
    rows: list[Message],
) -> list[Message]:
    activity_rows = list((await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.message_kind == "workflow_activity",
        )
    )).scalars().all())
    actionable_activity = [
        message for message in activity_rows if _is_actionable_workflow_activity(message)
    ]
    actionable_run_ids = {
        run_id
        for message in actionable_activity
        if (run_id := _message_workflow_run_id(message))
    }
    if not actionable_run_ids:
        return rows

    unresolved_rows = list((await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
            Message.resolved_at.is_(None),
        )
    )).scalars().all())
    associated_actions = [
        message
        for message in unresolved_rows
        if isinstance(message.pending_action, dict)
        and message.pending_action.get("kind") in WORKSPACE_WORKFLOW_RUN_ACTION_KINDS
        and _message_workflow_run_id(message) in actionable_run_ids
    ]
    by_id = {message.id: message for message in rows}
    for message in [*actionable_activity, *associated_actions]:
        by_id.setdefault(message.id, message)
    return sorted(by_id.values(), key=lambda message: (message.created_at, message.id))


async def _verify_workspace(
    db: AsyncSession, workspace_id: str, user: User,
) -> Workspace:
    ws = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if ws is None:
        raise HTTPException(404, "workspace not found")
    if not await user_can_read_workspace(db, workspace=ws, user=user):
        raise HTTPException(404, "workspace not found")
    return ws


# M9.1 authority matrix for the strategist proposal card: proposal item
# kind → the participant permission approving it demands. A cohort needs
# EVERY distinct permission its kinds map to, so an editor (approve_tasks
# + approve_goal_changes by role default) cannot wave through an
# automation change riding the same card.
_PROPOSAL_PERMISSION_BY_ITEM_KIND: dict[str, str] = {
    "task": "approve_tasks",
    "automation_change": "approve_automation_changes",
    "workflow_change": "approve_automation_changes",
    "experiment": "approve_automation_changes",
    "goal_change": "approve_goal_changes",
}


def _pending_action_items(pending_action: dict | None) -> list[dict]:
    """The non-task items a proposal card carries (empty on older cards)."""
    if not isinstance(pending_action, dict):
        return []
    raw = pending_action.get("items")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _pending_action_item_ids(pending_action: dict | None) -> list[str]:
    return [
        str(item["item_id"])
        for item in _pending_action_items(pending_action)
        if item.get("item_id")
    ]


def _proposal_required_permissions(
    pending_action: dict | None,
    *,
    selected_task_ids: list[str] | None = None,
    selected_item_ids: list[str] | None = None,
) -> list[str]:
    """Every authority key this cohort's kinds demand, approve_tasks first.

    A task-only cohort (and any legacy card with no ``items`` key) requires
    exactly ``approve_tasks``, unchanged.

    With the unified card a user may approve a *subset* of the cohort. When
    ``selected_*`` are passed (the ``approve_selected`` path) only the picked
    rows count: someone who may approve tasks but not automation changes can
    still approve just the tasks on a mixed card. ``approve``/``approve_all``
    pass neither and keep requiring the union over the whole cohort.
    """
    partial = selected_task_ids is not None or selected_item_ids is not None
    item_filter = set(selected_item_ids or []) if partial else None
    required: list[str] = []
    for item in _pending_action_items(pending_action):
        if item_filter is not None and str(item.get("item_id") or "") not in item_filter:
            continue
        key = _PROPOSAL_PERMISSION_BY_ITEM_KIND.get(str(item.get("kind") or ""))
        if key and key not in required:
            required.append(key)
    action = pending_action if isinstance(pending_action, dict) else {}
    has_tasks = bool(selected_task_ids) if partial else bool(action.get("task_ids"))
    if has_tasks or not required:
        if "approve_tasks" in required:
            required.remove("approve_tasks")
        required.insert(0, "approve_tasks")
    return required


def _proposal_selection(
    pending_action: dict | None, payload: dict | None,
) -> tuple[list[str], list[str]]:
    """Effective ``approve_selected`` picks: (task_ids, item_ids).

    A missing key means "everything of that half" — an older frontend that
    only knows about tasks keeps every non-task item in the approved half.
    Shared by the authority gate and the approval branch so the permissions
    checked are always the ones actually acted on.
    """
    action = pending_action if isinstance(pending_action, dict) else {}
    data = payload if isinstance(payload, dict) else {}
    raw_tasks = data.get("selected_task_ids")
    task_ids = (
        [str(task_id) for task_id in raw_tasks]
        if isinstance(raw_tasks, list)
        else [str(task_id) for task_id in (action.get("task_ids") or [])]
    )
    raw_items = data.get("selected_item_ids")
    item_ids = (
        [str(item_id) for item_id in raw_items]
        if isinstance(raw_items, list)
        else _pending_action_item_ids(action)
    )
    return task_ids, item_ids


def _proposal_authority_error(permission_key: str) -> str:
    subject = "tasks" if permission_key == "approve_tasks" else "this proposal"
    return (
        f"You do not have authority to approve {subject} in this workspace. "
        f"Approving requires the '{permission_key}' permission (workspace "
        "owner or editor role, or an explicit authority grant on your "
        "participant profile)."
    )


def _pending_action_evidence_type(kind: str, choice: str) -> str:
    if kind == PendingActionKind.APPROVE_PROPOSALS:
        return "user_feedback" if choice == "feedback" else "proposal_decision"
    if kind in _INPUT_CARD_KINDS:
        return "hitl_resolution"
    if kind == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        return "workspace_operation_decision"
    if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        return "external_message_decision"
    if kind == PendingActionKind.RETRY_STRATEGIST_REVIEW:
        return "retry_request"
    return "pending_action_resolution"


def _pending_action_summary(kind: str, choice: str, note: str | None) -> str:
    normalized = (choice or "").lower()
    approved = normalized in {
        "approve", "approved", "approve_all", "approve_selected",
        "always_approve", "approve_always", "always_allow",
        "yes", "accept", "confirm",
    }
    rejected = normalized in {"reject", "rejected", "reject_all", "no", "decline", "cancel"}
    feedback = normalized in {"feedback", "request_changes", "changes"}

    if kind == PendingActionKind.APPROVE_PROPOSALS:
        if normalized in {"always_approve", "approve_always", "always_allow"}:
            base = "Strategist proposal auto-approval enabled"
        elif approved:
            base = "Strategist proposal approved"
        elif rejected:
            base = "Strategist proposal rejected"
        elif feedback:
            base = "Feedback sent to the strategist"
        else:
            base = "Strategist proposal reviewed"
    elif kind == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        base = "Workspace operation reviewed"
    elif kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        base = "External message approved" if approved else (
            "External message rejected" if rejected else "External message reviewed"
        )
    elif kind in _INPUT_CARD_KINDS:
        base = "Input request answered"
    elif kind == PendingActionKind.RETRY_STRATEGIST_REVIEW:
        base = "Strategist retry requested"
    else:
        base = "Workspace action reviewed"
    if note:
        return f"{base}: {note[:240]}"
    return base


def _pending_action_payload_shape(payload: dict | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "keys": sorted(str(key) for key in payload.keys())[:30],
        "answer_keys": sorted(str(key) for key in (payload.get("answers") or {}).keys())[:30]
        if isinstance(payload.get("answers"), dict)
        else [],
        "selected_task_ids": list(payload.get("selected_task_ids") or [])[:50]
        if isinstance(payload.get("selected_task_ids"), list)
        else [],
        "selected_item_ids": list(payload.get("selected_item_ids") or [])[:50]
        if isinstance(payload.get("selected_item_ids"), list)
        else [],
    }


def _pending_action_guidance_text(note: str | None, payload: dict | None) -> str:
    parts: list[str] = []
    if note and note.strip():
        parts.append(note.strip())
    if isinstance(payload, dict):
        for key in ("feedback", "guidance", "instruction", "comment", "message", "response", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        answers = payload.get("answers")
        if isinstance(answers, dict):
            for value in answers.values():
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    return "\n".join(parts)


def _pending_action_activity_event(kind: str, choice: str) -> str:
    normalized = (choice or "").lower()
    approved = normalized in {
        "approve", "approved", "approve_all", "approve_selected",
        "always_approve", "approve_always", "always_allow",
        "yes", "accept", "confirm",
    }
    rejected = normalized in {"reject", "rejected", "no", "decline", "cancel"}
    if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        return "external_message.approved" if approved else (
            "external_message.rejected" if rejected else "external_message.resolved"
        )
    if kind == PendingActionKind.APPROVE_PROPOSALS:
        if approved or normalized in {"approve_all", "approve_selected"}:
            return "strategist_proposal.approved"
        if rejected or normalized in {"reject_all"}:
            return "strategist_proposal.rejected"
        if normalized == "feedback":
            return "strategist_proposal.feedback"
    if kind == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        return "workspace_operation.resolved"
    if kind in _INPUT_CARD_KINDS:
        return "hitl.resolved"
    return "pending_action.resolved"


def _schedule_workspace_chat_processing(
    *,
    conversation_id: str,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    message: str,
    message_id: str | None,
) -> None:
    from packages.core.services.workspace_runtime import process_workspace_chat_message

    asyncio.get_running_loop().create_task(process_workspace_chat_message(
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
        message_id=message_id,
    ))


async def _record_pending_action_activity(
    db: AsyncSession,
    *,
    workspace_id: str,
    user: User,
    conversation_id: str,
    message_id: str,
    pending_action: dict,
    resolution: dict,
) -> None:
    """Surface important human decisions in the workspace Activity tab."""
    try:
        from packages.core.services.workspace_service import record_activity

        kind = str(pending_action.get("kind") or "")
        choice = str(resolution.get("choice") or "").lower()
        event_type = _pending_action_activity_event(kind, choice)
        note = resolution.get("note") if isinstance(resolution.get("note"), str) else None
        if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
            verb = "approved" if event_type.endswith(".approved") else (
                "rejected" if event_type.endswith(".rejected") else "resolved"
            )
            summary = f"External message {verb} by workspace operator."
        elif kind == PendingActionKind.APPROVE_PROPOSALS:
            summary = _pending_action_summary(kind, choice, note)
        elif kind == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
            summary = _pending_action_summary(kind, choice, note)
        elif kind in _INPUT_CARD_KINDS:
            summary = _pending_action_summary(kind, choice, note)
        else:
            summary = _pending_action_summary(kind, choice, note)

        await record_activity(
            db,
            workspace_id,
            user.entity_id,
            event_type=event_type,
            summary=summary,
            details={
                "pending_action_kind": kind,
                "choice": choice,
                "note": note,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "review_id": pending_action.get("review_id"),
                "task_ids": list(pending_action.get("task_ids") or [])[:50],
                "step_id": pending_action.get("step_id"),
                "plan_id": pending_action.get("plan_id"),
                "draft_id": pending_action.get("draft_id") or pending_action.get("approval_token"),
                "channel_type": pending_action.get("channel_type"),
                "channel_config_id": pending_action.get("channel_config_id"),
            },
            user_id=user.id,
            agent_id=pending_action.get("agent_subscription_id"),
        )
    except Exception:
        logger.debug("pending action workspace activity skipped", exc_info=True)


def _message_ref_id(message: Message, ref_type: str) -> str | None:
    refs = message.refs if isinstance(message.refs, list) else []
    for ref in refs:
        if isinstance(ref, dict) and ref.get("type") == ref_type and ref.get("id"):
            return str(ref["id"])
    return None


def _pending_action_task_ids(message: Message) -> list[str]:
    action = message.pending_action if isinstance(message.pending_action, dict) else {}
    ids: list[str] = []
    task_id = action.get("task_id")
    if task_id:
        ids.append(str(task_id))
    for value in action.get("task_ids") or []:
        if value:
            ids.append(str(value))
    return list(dict.fromkeys(ids))


def _message_refs_with_hydrated_task_ref(
    message: Message,
    plan_task_ids: dict[str, str],
    conversation_task_ids: dict[str, str],
    task_ref_details: dict[str, dict[str, Any]],
) -> list[dict] | None:
    refs = list(message.refs) if isinstance(message.refs, list) else []
    task_ids = _pending_action_task_ids(message)
    conversation_task_id = conversation_task_ids.get(message.conversation_id)
    if conversation_task_id:
        task_ids.append(conversation_task_id)
    plan_id = _message_ref_id(message, "plan")
    plan_task_id = plan_task_ids.get(plan_id or "")
    if plan_task_id:
        task_ids.append(plan_task_id)

    hydrated_refs: list[dict] = []
    existing_task_ids: set[str] = set()
    hydrated = False
    for ref in refs:
        if not isinstance(ref, dict):
            hydrated_refs.append(ref)
            continue
        if ref.get("type") != "task" or not ref.get("id"):
            hydrated_refs.append(ref)
            continue
        task_id = str(ref.get("id"))
        existing_task_ids.add(task_id)
        details = task_ref_details.get(task_id) or {}
        if details:
            merged = dict(ref)
            for key, value in details.items():
                if value is not None and not merged.get(key):
                    merged[key] = value
            hydrated_refs.append(merged)
            hydrated = hydrated or merged != ref
        else:
            hydrated_refs.append(ref)
    refs = hydrated_refs
    appended = False
    for task_id in dict.fromkeys(task_ids):
        if task_id in existing_task_ids:
            continue
        refs.append({"type": "task", "id": task_id, **(task_ref_details.get(task_id) or {})})
        existing_task_ids.add(task_id)
        appended = True
    if not refs:
        return None
    return refs if appended or hydrated or message.refs else None


async def _task_ref_details_for_messages(
    db: AsyncSession,
    messages: list[Message],
    *,
    entity_id: str,
    workspace_id: str,
    plan_task_ids: dict[str, str],
    conversation_task_ids: dict[str, str],
) -> dict[str, dict[str, Any]]:
    task_ids: set[str] = set()
    for message in messages:
        stored_refs = message.refs if isinstance(message.refs, list) else []
        for ref in stored_refs:
            if isinstance(ref, dict) and ref.get("type") == "task" and ref.get("id"):
                task_ids.add(str(ref["id"]))
        task_ids.update(_pending_action_task_ids(message))
        conversation_task_id = conversation_task_ids.get(message.conversation_id)
        if conversation_task_id:
            task_ids.add(conversation_task_id)
        plan_id = _message_ref_id(message, "plan")
        plan_task_id = plan_task_ids.get(plan_id or "")
        if plan_task_id:
            task_ids.add(plan_task_id)
    if not task_ids:
        return {}

    from packages.core.models.task import Task

    rows = (await db.execute(
        select(Task.id, Task.title, Task.status, Task.priority).where(
            Task.id.in_(task_ids),
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
        )
    )).all()
    return {
        str(task_id): {
            "title": title,
            "status": status,
            "priority": priority,
        }
        for task_id, title, status, priority in rows
    }


async def _conversation_task_ids_for_messages(
    db: AsyncSession,
    messages: list[Message],
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, str]:
    conversation_ids = {
        message.conversation_id
        for message in messages
        if not _message_ref_id(message, "task")
    }
    if not conversation_ids:
        return {}

    rows = (await db.execute(
        select(Conversation.id, Conversation.thread_ref_id).where(
            Conversation.id.in_(conversation_ids),
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Conversation.scope == "workspace_thread",
            Conversation.thread_ref_kind == "task",
            Conversation.thread_ref_id.isnot(None),
        )
    )).all()
    return {str(conversation_id): str(task_id) for conversation_id, task_id in rows if task_id}


async def _plan_task_ids_for_messages(
    db: AsyncSession,
    messages: list[Message],
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, str]:
    plan_ids = {
        plan_id
        for message in messages
        if not _message_ref_id(message, "task")
        for plan_id in [_message_ref_id(message, "plan")]
        if plan_id
    }
    if not plan_ids:
        return {}

    from packages.core.models.execution import ExecutionPlan

    rows = (await db.execute(
        select(ExecutionPlan.id, ExecutionPlan.task_id).where(
            ExecutionPlan.id.in_(plan_ids),
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.workspace_id == workspace_id,
            ExecutionPlan.task_id.isnot(None),
        )
    )).all()
    return {str(plan_id): str(task_id) for plan_id, task_id in rows if task_id}


async def _hydrate_messages(
    db: AsyncSession,
    rows: list[Message],
    *,
    entity_id: str,
    workspace_id: str,
) -> list[MessageResponse]:
    from packages.core.models.workflow import WorkflowRun

    workflow_run_ids = list(dict.fromkeys(
        run_id for message in rows if (run_id := _message_workflow_run_id(message))
    ))
    workflow_run_updated_at: dict[str, datetime | None] = {}
    if workflow_run_ids:
        run_rows = (await db.execute(
            select(WorkflowRun.id, WorkflowRun.updated_at).where(
                WorkflowRun.id.in_(workflow_run_ids),
                WorkflowRun.entity_id == entity_id,
                WorkflowRun.workspace_id == workspace_id,
            )
        )).all()
        workflow_run_updated_at = {
            str(run_id): updated_at for run_id, updated_at in run_rows
        }
    plan_task_ids = await _plan_task_ids_for_messages(
        db,
        rows,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    conversation_task_ids = await _conversation_task_ids_for_messages(
        db,
        rows,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    task_ref_details = await _task_ref_details_for_messages(
        db,
        rows,
        entity_id=entity_id,
        workspace_id=workspace_id,
        plan_task_ids=plan_task_ids,
        conversation_task_ids=conversation_task_ids,
    )
    authors_by_id = await _load_message_authors(db, rows)
    return [
        _to_message(
            m,
            refs=_message_refs_with_hydrated_task_ref(
                m,
                plan_task_ids,
                conversation_task_ids,
                task_ref_details,
            ),
            author_user=authors_by_id.get(_message_author_user_id(m) or ""),
            resolved_by_user=authors_by_id.get(m.resolved_by_user_id or ""),
            updated_at=workflow_run_updated_at.get(_message_workflow_run_id(m) or ""),
        )
        for m in rows
    ]


async def _enqueue_learning_candidate_applies(
    db: AsyncSession,
    *,
    user: User,
    workspace_id: str,
    candidate_ids: list[str],
) -> None:
    ids = list(dict.fromkeys(candidate_ids or []))
    if not ids:
        return
    try:
        from packages.core.services.runtime_learning import enqueue_learning_candidate_apply

        has_enqueue_failure = False
        for candidate_id in ids:
            failed_row = await enqueue_learning_candidate_apply(
                db,
                entity_id=user.entity_id,
                candidate_id=candidate_id,
                workspace_id=workspace_id,
                user_id=user.id,
            )
            has_enqueue_failure = has_enqueue_failure or failed_row is not None
        if has_enqueue_failure:
            await db.commit()
    except Exception:
        logger.warning("Failed to enqueue workspace chat learning candidate apply", exc_info=True)


async def _record_pending_action_resolution_evidence(
    db: AsyncSession,
    *,
    workspace_id: str,
    user: User,
    conversation_id: str,
    message_id: str,
    pending_action: dict,
    resolution: dict,
) -> list[str]:
    """Best-effort evidence row for user feedback / HITL decisions."""
    try:
        from packages.core.services.runtime_learning import (
            queued_learning_candidate_ids,
            record_user_signal_evidence,
        )

        kind = str(pending_action.get("kind") or "")
        choice = str(resolution.get("choice") or "").lower()
        note = resolution.get("note") if isinstance(resolution.get("note"), str) else None
        payload = resolution.get("payload") if isinstance(resolution.get("payload"), dict) else None
        details = {
            "pending_action_kind": kind,
            "choice": choice,
            "note": note,
            "review_id": pending_action.get("review_id"),
            "task_ids": list(pending_action.get("task_ids") or [])[:50],
            "step_id": pending_action.get("step_id"),
            "plan_id": pending_action.get("plan_id"),
            "draft_id": pending_action.get("draft_id") or pending_action.get("approval_token"),
            "channel_type": pending_action.get("channel_type"),
            "channel_config_id": pending_action.get("channel_config_id"),
            "payload_shape": _pending_action_payload_shape(payload),
        }
        if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
            reply_text = str(pending_action.get("reply_text") or "")
            details["reply_text_chars"] = len(reply_text)
            details["reply_text_preview"] = reply_text[:240]

        _evidence, candidates = await record_user_signal_evidence(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            user_id=user.id,
            conversation_id=conversation_id,
            message_id=message_id,
            evidence_type=_pending_action_evidence_type(kind, choice),
            source="workspace_chat",
            status="succeeded",
            summary=_pending_action_summary(kind, choice, note),
            details=details,
            metrics={
                "task_count": len(pending_action.get("task_ids") or []),
                "has_note": bool(note),
                "approved": 1 if choice in {"approve", "approved", "yes", "accept", "confirm"} else (
                    0 if choice in {"reject", "rejected", "no", "decline", "cancel"} else None
                ),
            },
            guidance_text=_pending_action_guidance_text(note, payload),
        )
        return queued_learning_candidate_ids(candidates)
    except Exception:
        logger.debug("pending action runtime evidence skipped", exc_info=True)
        return []


async def _record_task_completion_feedback_evidence(
    db: AsyncSession,
    *,
    workspace_id: str,
    user: User,
    conversation_id: str,
    message: Message,
    rating: str,
) -> list[str]:
    """Best-effort evidence row for thumbs feedback on completion messages."""
    try:
        from packages.core.services.runtime_learning import (
            queued_learning_candidate_ids,
            record_user_signal_evidence,
        )

        task_id = _message_ref_id(message, "task")
        plan_id = _message_ref_id(message, "plan")
        if not task_id and plan_id:
            from packages.core.models.execution import ExecutionPlan

            task_id = (await db.execute(
                select(ExecutionPlan.task_id).where(
                    ExecutionPlan.id == plan_id,
                    ExecutionPlan.entity_id == user.entity_id,
                    ExecutionPlan.workspace_id == workspace_id,
                )
            )).scalar_one_or_none()
        label = "helpful" if rating == "up" else "not helpful"
        _evidence, candidates = await record_user_signal_evidence(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            user_id=user.id,
            conversation_id=conversation_id,
            message_id=message.id,
            task_id=task_id,
            evidence_type="task_completion_feedback",
            source="workspace_chat",
            status="succeeded",
            summary=f"Workspace chat task completion marked {label}",
            details={
                "rating": rating,
                "task_id": task_id,
                "plan_id": plan_id,
                "message_body_preview": (message.content or "")[:240],
            },
            metrics={"helpful": 1 if rating == "up" else 0},
        )
        return queued_learning_candidate_ids(candidates)
    except Exception:
        logger.debug("task completion feedback evidence skipped", exc_info=True)
        return []


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("/entrypoints")
async def list_chat_entrypoints(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    from packages.core.services.workspace_workflow_router import (
        list_workspace_chat_entrypoints,
    )

    entrypoints = await list_workspace_chat_entrypoints(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    return [entrypoint.public_dict() for entrypoint in entrypoints]


async def _workspace_entrypoint_started_stream(started) -> Any:
    from packages.core.services.sse_events import format_sse

    content = started.activity_message.content or "Workflow started."
    yield format_sse(
        "stream_start",
        {
            "conversation_id": started.conversation.id,
            "message_id": started.activity_message.id,
        },
    )
    yield format_sse("text_delta", {"content": content, "status": "queued"})
    yield format_sse(
        "stream_end",
        {
            "conversation_id": started.conversation.id,
            "message_id": started.activity_message.id,
            "persisted": True,
            "usage": {},
            "rounds": 0,
            "tool_calls": [],
            "status": "queued",
        },
    )


@router.post("/entrypoints/{binding_id}/stream")
async def stream_chat_entrypoint(
    workspace_id: str,
    binding_id: str,
    message: str = Form(...),
    conversation_id: str | None = Form(None),
    document_ids: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    _gate=Depends(require_plan("ai_budget_usd")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    from apps.api.routers.chat import _build_attachments
    from packages.core.services.workspace_workflow_router import (
        get_workspace_chat_entrypoint,
        start_workspace_chat_entrypoint,
    )

    resolved = await get_workspace_chat_entrypoint(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
    )
    if resolved is None:
        raise HTTPException(404, "Workflow Starter not found")
    entrypoint, binding, _workflow = resolved
    file_context_turn = await _build_attachments(
        message,
        document_ids,
        files,
        user.entity_id,
        db,
        workspace_id=workspace_id,
        user_id=user.id,
    )
    started = await start_workspace_chat_entrypoint(
        db,
        entrypoint=entrypoint,
        binding=binding,
        entity_id=user.entity_id,
        user_id=user.id,
        workspace_id=workspace_id,
        message=file_context_turn.cleaned_message,
        attachments=file_context_turn.attachments,
        conversation_id=conversation_id,
        route_source="explicit",
    )
    return StreamingResponse(
        _workspace_entrypoint_started_stream(started),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

@router.get("/messages", response_model=list[MessageResponse])
async def list_chat_messages(
    workspace_id: str,
    thread_ref_kind: Optional[str] = None,
    thread_ref_id: Optional[str] = None,
    limit: int = 100,
    before: Optional[datetime] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    rows = await chat_service.list_messages(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        limit=min(limit, 500),
        before=before,
    )
    return await _hydrate_messages(
        db,
        rows,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )


@router.get("/messages/page", response_model=MessagesPageResponse)
async def list_chat_messages_page(
    workspace_id: str,
    thread_ref_kind: Optional[str] = None,
    thread_ref_id: Optional[str] = None,
    limit: int = Query(75, ge=1, le=200),
    before: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    before_created_at, before_id = _decode_message_cursor(before)
    conversation_filters = [
        Conversation.entity_id == user.entity_id,
        Conversation.workspace_id == workspace_id,
    ]
    if thread_ref_kind and thread_ref_id:
        conversation_filters.extend([
            Conversation.scope == "workspace_thread",
            Conversation.thread_ref_kind == thread_ref_kind,
            Conversation.thread_ref_id == thread_ref_id,
        ])
    else:
        conversation_filters.append(Conversation.scope == "workspace_main")
    conversation = (await db.execute(
        select(Conversation).where(*conversation_filters).limit(1)
    )).scalar_one_or_none()
    if conversation is None:
        return MessagesPageResponse(
            items=[],
            has_more=False,
            next_cursor=None,
            open_action_count=await chat_service.count_open_pending_actions(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
            ),
            open_actions_complete=True,
        )

    rows = await chat_service.list_messages(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        limit=limit + 1,
        before=before_created_at,
        before_id=before_id,
        # Merge the pinned cards AFTER windowing below — pinning before the
        # `rows[:limit]` truncation pushed them past the cut, so unresolved
        # approvals filed in plan threads never reached the client while the
        # sidebar badge kept counting them.
        pin_pending=False,
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    ordered_rows = list(reversed(rows))

    # The cursor must describe the PAGE WINDOW, so it is taken before any
    # pinned card joins the list. A pinned card is typically the oldest row
    # present; letting it set the cursor would make the next page ask for
    # history older than the card and skip the real remainder.
    next_cursor = _encode_message_cursor(ordered_rows[0]) if has_more and ordered_rows else None

    # First page of the main view: guarantee every unresolved action card is
    # present, however old, so the chat can always answer the badge.
    is_main_view = not (thread_ref_kind and thread_ref_id)
    open_actions_complete = False
    if is_main_view and before_created_at is None:
        pinned = await chat_service.unresolved_pending_messages(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            limit=_PINNED_ACTION_LIMIT,
        )
        # Tell the client whether it is holding the WHOLE open set, so it can
        # treat "card I know about, absent from this page" as "already
        # answered" and stop counting it. Without this the client cannot
        # distinguish resolved-elsewhere from merely-out-of-window.
        open_actions_complete = len(pinned) < _PINNED_ACTION_LIMIT
        if pinned:
            by_id = {m.id: m for m in ordered_rows}
            for msg in pinned:
                by_id.setdefault(msg.id, msg)
            ordered_rows = sorted(
                by_id.values(), key=lambda m: (m.created_at, m.id),
            )
    response_rows = ordered_rows
    if is_main_view and before_created_at is None:
        response_rows = await _augment_latest_page_with_workflow_runtime(
            db,
            conversation_id=conversation.id,
            rows=ordered_rows,
        )
    return MessagesPageResponse(
        items=await _hydrate_messages(
            db,
            response_rows,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
        ),
        has_more=has_more,
        next_cursor=next_cursor,
        open_action_count=await chat_service.count_open_pending_actions(
            db, entity_id=user.entity_id, workspace_id=workspace_id,
        ),
        open_actions_complete=open_actions_complete,
    )


@router.post("/messages", response_model=MessageResponse, status_code=201)
async def post_chat_message(
    workspace_id: str,
    req: PostMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    msg = await chat_service.post_message(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        body=req.body,
        message_kind="text",
        author_kind="user",
        author_user_id=user.id,
        thread_ref_kind=req.thread_ref_kind,
        thread_ref_id=req.thread_ref_id,
    )

    await db.commit()
    if (req.body or "").strip():
        _schedule_workspace_chat_processing(
            conversation_id=msg.conversation_id,
            workspace_id=workspace_id,
            entity_id=user.entity_id,
            user_id=user.id,
            message=req.body,
            message_id=msg.id,
        )
    return _to_message(msg, author_user=user)


@router.get("/threads", response_model=list[ThreadResponse])
async def list_threads(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    rows = list((await db.execute(
        select(Conversation).where(
            Conversation.entity_id == user.entity_id,
            Conversation.workspace_id == workspace_id,
            Conversation.scope == "workspace_thread",
        ).order_by(Conversation.updated_at.desc().nullslast())
    )).scalars().all())
    return [
        ThreadResponse(
            id=c.id, title=c.title,
            thread_ref_kind=c.thread_ref_kind,
            thread_ref_id=c.thread_ref_id,
            updated_at=c.updated_at,
        )
        for c in rows
    ]


@router.post("/messages/{message_id}/resolve", response_model=MessageResponse)
async def resolve_chat_action(
    workspace_id: str,
    message_id: str,
    req: ResolveActionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a ``pending_action`` (e.g. HITL response, plan approval)."""
    await _verify_workspace(db, workspace_id, user)

    msg = (await db.execute(
        select(Message).where(Message.id == message_id)
    )).scalar_one_or_none()
    if msg is None:
        raise HTTPException(404, "message not found")
    # Confirm message belongs to a conversation in this workspace.
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == msg.conversation_id)
    )).scalar_one_or_none()
    if conv is None or conv.workspace_id != workspace_id or conv.entity_id != user.entity_id:
        raise HTTPException(404, "message not found")

    resolution = {"choice": req.choice}
    if req.note:
        resolution["note"] = req.note
    if req.payload is not None:
        resolution["payload"] = req.payload

    # Side effects per pending_action.kind:
    pa = msg.pending_action or {}
    kind = pa.get("kind")
    normalized_choice = (req.choice or "").lower()
    workflow_run_to_enqueue: str | None = None
    if kind in WORKSPACE_WORKFLOW_RUN_ACTION_KINDS and pa.get("workflow_run_id"):
        from packages.core.models.workflow import WorkflowRun

        controlled_run = (await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == str(pa["workflow_run_id"]),
                WorkflowRun.entity_id == user.entity_id,
                WorkflowRun.workspace_id == workspace_id,
            )
        )).scalar_one_or_none()
        if controlled_run is None:
            raise HTTPException(404, "Workflow Run not found")
        if not await user_can_control_workspace_run(
            db,
            run=controlled_run,
            user_id=user.id,
            entity_role=user.role,
        ):
            raise HTTPException(403, "Workflow Run control permission required")
    proposal_always_approve = (
        kind == PendingActionKind.APPROVE_PROPOSALS
        and normalized_choice == APPROVAL_CHOICE_ALWAYS_APPROVE
    )
    if proposal_always_approve and not resolution.get("note"):
        resolution["note"] = "Future workspace proposals in this workspace will start automatically."
    # M9.1 authority gate — approving strategist proposals requires the
    # permission each item kind in the cohort maps to (profile authority >
    # workspace role map > entity owner/admin fallback). Checked BEFORE the
    # message is resolved so a denied attempt leaves the card actionable for
    # someone who can.
    if kind == PendingActionKind.APPROVE_PROPOSALS and normalized_choice in {
        APPROVAL_CHOICE_APPROVE,
        "approve_all",
        "approve_selected",
        APPROVAL_CHOICE_ALWAYS_APPROVE,
    }:
        from packages.core.humans import participant_can
        if normalized_choice == "approve_selected":
            # Unified card: only the picked rows need authority.
            picked_tasks, picked_items = _proposal_selection(pa, req.payload)
            required_permissions = _proposal_required_permissions(
                pa,
                selected_task_ids=picked_tasks,
                selected_item_ids=picked_items,
            )
        else:
            required_permissions = _proposal_required_permissions(pa)
        for permission_key in required_permissions:
            if not await participant_can(
                db,
                user=user,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                permission_key=permission_key,
            ):
                raise HTTPException(403, _proposal_authority_error(permission_key))
    if msg.resolved_at is not None and not _allow_side_effect_after_resolved(pa, req.choice):
        return _to_message(
            msg,
            resolved_by_user=user if msg.resolved_by_user_id == user.id else None,
        )

    resolved = await chat_service.resolve_pending_action(
        db, message_id=message_id, user_id=user.id, resolution=resolution,
    )
    if resolved is None:
        raise HTTPException(404, "message not found")

    # Mid-execution HITL cards (CAPTCHA / 2FA / confirmation walls) carry the
    # id of the unified HitlRequest minted for the pause. Each path-C
    # branch below records its verdict here — "grant" on the leg that resumes
    # the step, "deny" on the leg that cancels it — and the single block after
    # the chain applies it. Keeping the decision in the branch that owns the
    # choice means the choice vocabulary is never spelled twice.
    # Leaving this None (needs_login's `sign_in`) decides nothing.
    # Literal, not str: every write site below is a string literal and the read
    # site is `== "grant"` with deny as the fallback, so a typo at a grant site
    # would silently deny — step resumed, record says the user refused.
    _lease_request_id = pa.get("approval_request_id")
    _lease_decision: Literal["grant", "deny"] | None = None

    if kind == PendingActionKind.WORKFLOW_STARTER_INPUT and pa.get("workflow_run_id"):
        from packages.core.models.workflow import WorkflowRun
        from packages.core.services.workspace_workflow_router import (
            assemble_workspace_workflow_inputs,
            get_workspace_chat_entrypoint,
            preserve_server_captured_workflow_inputs,
            validate_workspace_workflow_inputs,
        )

        workflow_run = (await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == str(pa["workflow_run_id"]),
                WorkflowRun.entity_id == user.entity_id,
                WorkflowRun.workspace_id == workspace_id,
                WorkflowRun.binding_id == str(pa.get("workflow_binding_id") or ""),
            ).with_for_update()
        )).scalar_one_or_none()
        if workflow_run is None:
            raise HTTPException(404, "Workflow Run not found")
        entrypoint_context = (
            (workflow_run.trigger_data or {}).get("_workspace_chat_entrypoint")
            if isinstance(workflow_run.trigger_data, dict)
            else None
        )
        if not isinstance(entrypoint_context, dict) or entrypoint_context.get("conversation_id") != conv.id:
            raise HTTPException(409, "Workflow Run does not belong to this Chat")
        if workflow_run.status != "paused" or workflow_run.step_results:
            raise HTTPException(409, "Workflow Run is no longer waiting for its inputs")

        cancel_choices = {"cancel", "reject", "rejected", "decline", "deny", "no", "skip"}
        if normalized_choice in cancel_choices:
            workflow_run.status = "cancelled"
            workflow_run.completed_at = datetime.now(timezone.utc)
        else:
            if normalized_choice not in {"run", "start", "submit", "confirm"}:
                raise HTTPException(400, "Unsupported Workflow input choice")
            resolved_entrypoint = await get_workspace_chat_entrypoint(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                binding_id=str(pa.get("workflow_binding_id") or ""),
            )
            if resolved_entrypoint is None:
                raise HTTPException(404, "Workflow Starter not found")
            entrypoint, _binding, _workflow = resolved_entrypoint
            submitted_inputs = preserve_server_captured_workflow_inputs(
                entrypoint,
                (req.payload or {}).get("inputs"),
                workflow_run.trigger_data,
            )
            try:
                input_values = validate_workspace_workflow_inputs(
                    entrypoint,
                    submitted_inputs,
                )
            except ValueError as exc:
                try:
                    errors = json.loads(str(exc))
                except Exception:
                    errors = {"inputs": "Invalid workflow inputs."}
                raise HTTPException(422, {
                    "message": "Invalid workflow inputs",
                    "errors": errors,
                })
            mapped_input_values = assemble_workspace_workflow_inputs(
                entrypoint,
                input_values,
            )
            updated_trigger_data = dict(workflow_run.trigger_data or {})
            updated_trigger_data.update(input_values)
            updated_trigger_data.update(mapped_input_values)
            workflow_run.trigger_data = updated_trigger_data
            updated_variables = dict(workflow_run.variables or {})
            updated_variables.update(input_values)
            updated_variables.update(mapped_input_values)
            updated_variables["trigger"] = {
                **deepcopy(input_values),
                **deepcopy(mapped_input_values),
            }
            workflow_run.variables = updated_variables
            workflow_run.status = "running"
            workflow_run.error = None
            workflow_run_to_enqueue = workflow_run.id
        from packages.core.services.workflow_chat_projection import project_workflow_run_status

        await project_workflow_run_status(db, run=workflow_run)

    elif kind == PendingActionKind.WORKFLOW_RETRY and pa.get("workflow_run_id"):
        from packages.core.models.workflow import WorkflowRun
        from packages.core.services import workflow_service
        from packages.core.services.conversation_messages import add_message
        from packages.core.services.workflow_chat_projection import (
            project_workflow_run_status,
            workflow_progress_steps,
        )

        workflow_run = (await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == str(pa["workflow_run_id"]),
                WorkflowRun.entity_id == user.entity_id,
                WorkflowRun.workspace_id == workspace_id,
                WorkflowRun.binding_id == str(pa.get("workflow_binding_id") or ""),
            ).with_for_update()
        )).scalar_one_or_none()
        if workflow_run is None:
            raise HTTPException(404, "Workflow Run not found")
        entrypoint_context = (
            (workflow_run.trigger_data or {}).get("_workspace_chat_entrypoint")
            if isinstance(workflow_run.trigger_data, dict)
            else None
        )
        if not isinstance(entrypoint_context, dict) or entrypoint_context.get("conversation_id") != conv.id:
            raise HTTPException(409, "Workflow Run does not belong to this Chat")
        if normalized_choice not in {"retry", "retry_now"}:
            if normalized_choice not in {"cancel", "skip"}:
                raise HTTPException(400, "Unsupported Workflow retry choice")
            workflow_run.status = "cancelled"
            workflow_run.completed_at = datetime.now(timezone.utc)
            await project_workflow_run_status(db, run=workflow_run)
        else:
            variables = (req.payload or {}).get("variables")
            if variables is not None and not isinstance(variables, dict):
                raise HTTPException(422, "Workflow retry variables must be an object")
            try:
                retry = await workflow_service.retry_workflow_run(
                    db,
                    run_id=workflow_run.id,
                    entity_id=user.entity_id,
                    started_by=user.id,
                    from_step_id=str(pa.get("retry_from_step_id") or "") or None,
                    variables=variables,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            title = str((msg.meta or {}).get("workflow_title") or "Workflow")
            activity_message = await add_message(
                db,
                conv.id,
                role="system",
                content=f"{title} retry attempt {retry.effective_attempt_number} is starting.",
                message_kind="workflow_activity",
                refs=[
                    {"type": "workflow", "id": retry.workflow_id, "title": title},
                    {"type": "workflow_run", "id": retry.id},
                ],
                meta={
                    "workflow_run_id": retry.id,
                    "workflow_binding_id": retry.binding_id,
                    "workflow_title": title,
                    "workflow_status": "running",
                    "workflow_business_outcome": "in_progress",
                    "workflow_attempt_number": retry.effective_attempt_number,
                    "workflow_retry_of_run_id": workflow_run.id,
                    "workflow_steps": workflow_progress_steps(
                        retry,
                        activity_status="running",
                    ),
                },
            )
            retry_trigger = dict(retry.trigger_data or {})
            retry_context = dict(retry_trigger.get("_workspace_chat_entrypoint") or {})
            retry_context["activity_message_id"] = activity_message.id
            retry_trigger["_workspace_chat_entrypoint"] = retry_context
            retry.trigger_data = retry_trigger
            workflow_run = retry
            workflow_run_to_enqueue = retry.id

    elif kind in _WORKFLOW_WAIT_KINDS and pa.get("workflow_run_id"):
        from packages.core.ai.workflow_runner import (
            complete_workflow_stage_wait,
            workflow_approval_decision_metadata,
            workflow_stage_wait_context,
        )
        from packages.core.models.workflow import WorkflowDefinition, WorkflowRun

        workflow_run = (await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == str(pa["workflow_run_id"]),
                WorkflowRun.entity_id == user.entity_id,
                WorkflowRun.workspace_id == workspace_id,
                WorkflowRun.binding_id == str(pa.get("workflow_binding_id") or ""),
            ).with_for_update()
        )).scalar_one_or_none()
        if workflow_run is None:
            raise HTTPException(404, "Workflow Run not found")
        entrypoint_context = (
            (workflow_run.trigger_data or {}).get("_workspace_chat_entrypoint")
            if isinstance(workflow_run.trigger_data, dict)
            else None
        )
        if not isinstance(entrypoint_context, dict) or entrypoint_context.get("conversation_id") != conv.id:
            raise HTTPException(409, "Workflow Run does not belong to this Chat")
        if workflow_run.status != "paused" or workflow_run.current_step_id != pa.get("step_id"):
            raise HTTPException(409, "Workflow Run is no longer waiting for this response")

        cancel_choices = {"cancel", "reject", "rejected", "decline", "deny", "no", "skip"}
        if normalized_choice in cancel_choices:
            workflow_run.status = "cancelled"
            workflow_run.completed_at = datetime.now(timezone.utc)
        else:
            if kind == PendingActionKind.WORKFLOW_INPUT and normalized_choice not in {"respond", "submit", "provide_answers", "ok"}:
                raise HTTPException(400, "Unsupported Workflow input choice")
            response_variable = str(pa.get("response_variable") or f"{pa['step_id']}_response")
            response_value = {
                "choice": normalized_choice,
                **({"note": req.note} if req.note else {}),
                **({"payload": req.payload} if req.payload is not None else {}),
            }
            updated_variables = dict(workflow_run.variables or {})
            updated_variables[response_variable] = response_value
            workflow_run.variables = updated_variables
            workflow = (await db.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.id == workflow_run.workflow_id,
                    WorkflowDefinition.entity_id == user.entity_id,
                )
            )).scalar_one_or_none()
            current_step = next(
                (
                    step
                    for step in (workflow.steps if workflow else [])
                    if str(step.get("id") or "") == str(pa["step_id"])
                ),
                None,
            )
            stage_wait_context = workflow_stage_wait_context(
                workflow_run,
                current_step,
            )
            if (
                isinstance(current_step, dict)
                and current_step.get("type") == "stage"
                and stage_wait_context is None
            ):
                raise HTTPException(409, "Workflow stage is no longer waiting")
            current_config = (
                stage_wait_context[1].get("config")
                if stage_wait_context is not None
                and isinstance(stage_wait_context[1].get("config"), dict)
                else current_step.get("config")
                if isinstance(current_step, dict)
                and isinstance(current_step.get("config"), dict)
                else {"options": pa.get("options") or []}
            )
            approval_metadata: dict[str, Any] = {}
            if kind == PendingActionKind.WORKFLOW_APPROVAL:
                try:
                    approval_metadata = workflow_approval_decision_metadata(
                        current_config,
                        decision=normalized_choice,
                        actor_id=user.id,
                        decided_at=datetime.now(timezone.utc),
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if stage_wait_context is not None and isinstance(current_step, dict):
                complete_workflow_stage_wait(
                    workflow_run,
                    current_step,
                    stage_wait_context,
                    metadata={
                        "workflow_response": response_value,
                        **approval_metadata,
                    },
                )
            else:
                step_results = dict(workflow_run.step_results or {})
                previous = dict(step_results.get(str(pa["step_id"])) or {})
                previous.update({
                    "status": "completed",
                    "resumed": True,
                    "workflow_response": response_value,
                    "resumed_at": datetime.now(timezone.utc).isoformat(),
                    **approval_metadata,
                })
                step_results[str(pa["step_id"])] = previous
                workflow_run.step_results = step_results
            workflow_run.status = "running"
            workflow_run.error = None
            workflow_run_to_enqueue = workflow_run.id
        from packages.core.services.workflow_chat_projection import project_workflow_run_status

        await project_workflow_run_status(db, run=workflow_run)

    elif kind == PendingActionKind.HUMAN_INPUT and pa.get("step_id"):
        # Lease-level HITL (legacy free-form text input): stash the
        # response on the step row + flip back to pending.
        await _resume_step_for_retry(
            db, user,
            step_id=pa["step_id"],
            plan_id=pa.get("plan_id"),
            human_input_response=(req.payload or {"choice": req.choice, "note": req.note}),
        )
        # No decline path here — any answer is an answer.
        _lease_decision = "grant"

    elif kind == PendingActionKind.NEEDS_INPUT and pa.get("step_id"):
        # Tool returned _pending_action(kind="needs_input") —
        # blocking_questions on a form. Resolution choices:
        #   choice="provide_answers" + payload={answers: {...}}
        #     → merge answers into step.params['answers'], retry
        #   choice="skip" → cancel the step
        choice = (req.choice or "").lower()
        if choice in {"provide_answers", "submit", "ok"}:
            # Caller's payload is the answers dict — merge under
            # 'answers' key so tools can find them on retry.
            answers = (req.payload or {}).get("answers") or req.payload or {}
            await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                params_update={"answers": answers},
            )
            _lease_decision = "grant"
        else:
            # skip / cancel — fail the step so the plan can move on.
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user skipped needs_input",
            )
            _lease_decision = "deny"

    elif kind == PendingActionKind.NEEDS_CONFIRMATION and pa.get("step_id"):
        # Tool returned _pending_action(kind="needs_confirmation") —
        # destructive click was intercepted. Resolution:
        #   choice="confirm" → re-run with confirm flag set
        #   choice="cancel" → fail the step
        choice = (req.choice or "").lower()
        if choice in {"confirm", "ok", "yes", "approve"}:
            # Set both legacy and current confirmation flags — extras are
            # ignored by tools that don't recognize them.
            await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                params_update={"confirm": True, "confirm_destructive": True},
            )
            _lease_decision = "grant"
        else:
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user cancelled needs_confirmation",
            )
            _lease_decision = "deny"

    elif kind == PendingActionKind.NEEDS_LOGIN and pa.get("step_id"):
        # Tool returned _pending_action(kind="needs_login") — login
        # wall hit. Resolution:
        #   choice="sign_in" → mark message resolved; the frontend
        #     spawns a headed-login session via the existing
        #     /api/v1/integrations/headed-login/* endpoints, captures
        #     cookies, then re-calls THIS endpoint with
        #     choice="continue_after_login" to retry the step. The
        #     step stays waiting_human until then.
        #   choice="continue_after_login" → cookies have just been
        #     captured (Integration row updated upstream); retry the
        #     step so the dispatcher leases fresh credentials.
        #   choice="skip" → fail the step.
        choice = (req.choice or "").lower()
        if choice == "continue_after_login":
            await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
            )
            _lease_decision = "grant"
        elif choice == "sign_in":
            # No backend state change — frontend orchestrates the
            # headed-login flow. Step stays waiting_human; user calls
            # back with choice="continue_after_login" once cookies
            # are captured. The approval request stays PENDING for the
            # same reason: nothing has been approved yet.
            pass
        else:
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user skipped needs_login",
            )
            _lease_decision = "deny"

    elif kind == PendingActionKind.GOVERNANCE_APPROVAL and pa.get("step_id"):
        choice = (req.choice or "").lower()
        _always = choice == APPROVAL_CHOICE_ALWAYS_APPROVE
        # The card carries the unified HitlRequest id. A stale pre-upgrade
        # card has none — resuming still works: the dispatcher re-gates the
        # step, finds no grant, and posts a fresh card carrying a request id.
        _request_id = pa.get("approval_request_id")
        # An `error` card offers retry/cancel rather than approve/reject: the
        # step already ran, so there is nothing to authorize — the user went
        # and fixed something and now wants it run again. Same resume path,
        # honest label. Without this, "retry" would fall through to the else
        # branch and CANCEL the step the user just repaired.
        _resume = choice in {APPROVAL_CHOICE_APPROVE, ERROR_CHOICE_RETRY, "retry_now"}
        if _resume or _always:
            # "Always" is a PROMOTION of this action-scope request to a
            # tool-scope standing grant, and grant_approval(standing=True) is
            # the one place that performs it: it writes the workspace
            # auto-approve set and records the widened scope on the row.
            # Writing the auto-approve set here instead — which is what this
            # branch used to do — routed the step plane around both.
            _promoted = False
            if _request_id:
                from packages.core.governance.approvals import grant_approval
                from packages.core.models.hitl_request import HitlRequest
                request = (await db.execute(
                    select(HitlRequest).where(
                        HitlRequest.id == _request_id,
                        HitlRequest.entity_id == user.entity_id,
                    )
                )).scalar_one_or_none()
                if request is not None:
                    await grant_approval(
                        db, request, by_user_id=user.id, via="chat_card",
                        standing=_always, changed_by=user.id,
                    )
                    _promoted = True
            if _always and not _promoted and (
                pa.get("action") or pa.get("capability_id")
            ):
                # A pre-upgrade card carries no request id, so there is no row
                # to promote — the standing store still has to be written.
                from packages.core.governance import (
                    add_auto_approve_action,
                    add_auto_approve_capability,
                )
                if pa.get("action"):
                    await add_auto_approve_action(
                        db,
                        entity_id=user.entity_id,
                        workspace_id=workspace_id,
                        action_key=str(pa.get("action")),
                        changed_by=user.id,
                    )
                else:
                    await add_auto_approve_capability(
                        db,
                        entity_id=user.entity_id,
                        workspace_id=workspace_id,
                        capability_id=str(pa.get("capability_id")),
                        changed_by=user.id,
                    )
            await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
            )
        else:
            if _request_id:
                from packages.core.governance.approvals import deny_approval
                from packages.core.models.hitl_request import HitlRequest
                request = (await db.execute(
                    select(HitlRequest).where(
                        HitlRequest.id == _request_id,
                        HitlRequest.entity_id == user.entity_id,
                    )
                )).scalar_one_or_none()
                if request is not None:
                    await deny_approval(
                        db, request, by_user_id=user.id, via="chat_card",
                        reason="user rejected governance approval",
                    )
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user rejected governance approval",
            )

    elif kind == PendingActionKind.APPROVE_PROPOSALS and pa.get("review_id"):
        # Strategist proposal card: approve, approve_selected, reject, or feedback.
        from packages.core.strategist import approve_proposal, reject_proposal
        from packages.core.strategist.service import set_proposal_auto_approval
        review_id = pa["review_id"]
        all_ids = pa.get("task_ids") or []
        # Non-task items (change kinds / experiments) ride the same card.
        all_item_ids = _pending_action_item_ids(pa)
        choice = normalized_choice
        payload = req.payload or {}
        approved_ids: list[str] | None = None

        if choice == APPROVAL_CHOICE_ALWAYS_APPROVE:
            # Legacy workspace boolean — kept for compat with the flag-off
            # strategist path, which only consults this setting.
            await set_proposal_auto_approval(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                enabled=True,
                changed_by=user.id,
            )
            # M8: on the strategist_review_v2 path "always approve" is a
            # BLANKET grant — every Strategist proposal type stops asking,
            # not just the kinds on this card. Each key gets its own
            # auditable GovernanceRevision, and any single type can be put
            # back to human review in Settings → Approval automation.
            from packages.core.services.feature_flags import is_enabled
            if await is_enabled(
                db, "strategist_review_v2",
                entity_id=user.entity_id, fallback=False,
            ):
                from packages.core.governance import add_auto_approve_action
                from packages.core.proposals.constants import STRATEGIST_ACTION_KEYS
                for action_key in STRATEGIST_ACTION_KEYS:
                    await add_auto_approve_action(
                        db,
                        entity_id=user.entity_id,
                        workspace_id=workspace_id,
                        action_key=action_key,
                        changed_by=user.id,
                    )
            approved_ids = await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
                actor_id=user.id,
            )
        elif choice in {APPROVAL_CHOICE_APPROVE, "approve_all"}:
            # ProposalCard historically submits ``approve_all`` while the
            # shared approval schema uses ``approve``.  Accept both so the
            # message cannot be resolved without moving its tickets.
            approved_ids = await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
                actor_id=user.id,
            )
        elif choice == "approve_selected":
            # Approve only the selected tasks / items, reject the rest.
            # Same helper the authority gate above used, so the permissions
            # checked are exactly the rows acted on. A missing key still
            # means "all of that half" for older frontends.
            selected_ids, selected_item_ids = _proposal_selection(pa, payload)
            approved_ids = await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id,
                only_task_ids=list(selected_ids),
                only_item_ids=list(selected_item_ids),
                actor_id=user.id,
            )
            approved_set = set(approved_ids)
            rejected_ids = [t for t in all_ids if t not in approved_set]
            approved_item_set = set(selected_item_ids)
            rejected_item_ids = [
                item_id for item_id in all_item_ids
                if item_id not in approved_item_set
            ]
            if rejected_ids or rejected_item_ids:
                await reject_proposal(
                    db, entity_id=user.entity_id,
                    review_id=review_id,
                    only_task_ids=list(rejected_ids),
                    only_item_ids=list(rejected_item_ids),
                    reason="Not selected by user",
                    actor_id=user.id,
                )
        elif choice == "feedback":
            # User gave feedback — close the stale proposal cohort, then
            # re-run Strategist so a fresh proposal card is reviewed.
            feedback_text = req.note or ""
            await reject_proposal(
                db,
                entity_id=user.entity_id,
                review_id=review_id,
                only_task_ids=all_ids or None,
                reason=(
                    f"Feedback requested: {feedback_text}"
                    if feedback_text else
                    "Feedback requested"
                ),
                actor_id=user.id,
            )
            ws_id = msg.conversation_id and (await db.execute(
                select(Conversation.workspace_id).where(Conversation.id == msg.conversation_id)
            )).scalar_one_or_none()
            if ws_id:
                try:
                    from packages.core.strategist import (
                        ReviewTrigger, ReviewTriggerKind,
                    )
                    from packages.core.tasks.ai_tasks import run_strategist_review
                    run_strategist_review.apply_async(
                        args=[ws_id],
                        kwargs=ReviewTrigger(
                            kind=ReviewTriggerKind.HUMAN_REQUESTED,
                            detail=f"feedback on the last proposal: {feedback_text}",
                        ).celery_kwargs(),
                        countdown=3,
                    )
                except Exception:
                    pass
        elif choice in {"reject", "reject_all", "decline", "no"}:
            # M9.3: the reject dialog sends a machine-readable reason_code
            # (payload) + optional free-text comment (note). Only the
            # user-offerable vocabulary is accepted; anything else falls
            # back to OTHER rather than failing the resolution.
            from packages.core.proposals.constants import USER_REASON_CODES
            raw_code = payload.get("reason_code")
            reason_code: str | None = None
            if isinstance(raw_code, str) and raw_code.strip():
                candidate = raw_code.strip().upper()
                reason_code = (
                    candidate if candidate in USER_REASON_CODES else "OTHER"
                )
            await reject_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
                reason=req.note,
                reason_code=reason_code,
                actor_id=user.id,
            )

        if approved_ids is not None:
            if all_ids and choice != "approve_selected" and not approved_ids:
                # The proposal card and task cohort have drifted apart.  Do
                # not return a false-success resolution while every ticket is
                # still proposed; rolling back also keeps the card actionable.
                raise HTTPException(
                    409,
                    "No proposed tickets were approved. Refresh the workspace and try again.",
                )
            resolution_payload = dict(resolution.get("payload") or {})
            resolution_payload["approved_task_ids"] = approved_ids
            resolution["payload"] = resolution_payload
            msg.resolution = dict(resolution)

    elif kind == PendingActionKind.RETRY_STRATEGIST_REVIEW:
        choice = (req.choice or "").lower()
        if choice in {"retry", "retry_now", "approve", "yes"}:
            try:
                from packages.core.strategist import (
                    ReviewTrigger, ReviewTriggerKind,
                )
                from packages.core.tasks.ai_tasks import run_strategist_review
                original_trigger = pa.get("trigger") or "failed"
                run_strategist_review.apply_async(
                    args=[workspace_id],
                    kwargs=ReviewTrigger(
                        kind=ReviewTriggerKind.HUMAN_REQUESTED,
                        detail=f"manual retry after failure ({original_trigger})",
                    ).celery_kwargs(),
                    countdown=1,
                )
            except Exception as exc:
                raise HTTPException(500, f"failed to enqueue strategist retry: {exc}") from exc

    elif kind == PendingActionKind.WORKSPACE_OPERATION_REVIEW:
        from packages.core.services.workspace_operation_service import (
            resolve_workspace_operation_review,
        )

        result = await resolve_workspace_operation_review(
            db,
            conversation_id=conv.id,
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=workspace_id,
            hitl_id=str(pa.get("draft_id") or pa.get("approval_token") or ""),
            action=req.choice,
        )
        if result is None:
            raise HTTPException(400, "workspace operation review could not be resolved")
        db.add(Message(
            conversation_id=msg.conversation_id,
            role="system",
            content=str(result.get("message") or "Workspace operation review resolved."),
            author_kind="system",
            message_kind="system",
            refs=[
                {"type": "message", "id": msg.id},
                {"type": "workspace_operation_draft", "id": result.get("draft_id")},
            ],
        ))
        await db.flush()

    elif kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        choice = (req.choice or "").lower()
        always = choice == APPROVAL_CHOICE_ALWAYS_APPROVE
        if choice == APPROVAL_CHOICE_APPROVE or always:
            if always:
                from packages.core.governance import add_auto_approve_action

                await add_auto_approve_action(
                    db,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    action_key=str(pa.get("action_key") or "external_message.send"),
                    changed_by=user.id,
                )
            from packages.core.services.channel_outbound_delivery import deliver_approved_external_reply

            result = await deliver_approved_external_reply(
                db,
                entity_id=user.entity_id,
                channel_config_id=str(pa.get("channel_config_id") or ""),
                channel_type=str(pa.get("channel_type") or ""),
                channel_conversation_id=str(pa.get("channel_conversation_id") or ""),
                chat_id=str(pa.get("chat_id") or pa.get("sender_id") or ""),
                text=str(pa.get("reply_text") or ""),
                agent_subscription_id=pa.get("agent_subscription_id"),
            )
            body = (
                "Approved external message was sent."
                if result.get("sent")
                else f"Approved external message was recorded but not sent: {result.get('reason') or result.get('error') or 'unknown'}"
            )
            db.add(Message(
                conversation_id=msg.conversation_id,
                role="system",
                content=body,
                author_kind="system",
                message_kind="system",
                refs=[
                    {"type": "message", "id": msg.id},
                    {"type": "channel_conversation", "id": pa.get("channel_conversation_id")},
                    {"type": "message_log", "id": result.get("message_log_id")},
                ],
            ))
            await db.flush()
        elif choice in {"reject", "rejected", "no", "decline", "cancel"}:
            channel_conversation_id = str(pa.get("channel_conversation_id") or "")
            if channel_conversation_id:
                db.add(Message(
                    conversation_id=channel_conversation_id,
                    role="system",
                    content="External reply rejected by workspace operator.",
                    author_kind="system",
                    message_kind="system",
                    meta={
                        "channel_type": pa.get("channel_type"),
                        "chat_id": pa.get("chat_id"),
                        "rejected_external_message": True,
                    },
                ))
                await db.flush()

    # Apply the verdict the path-C branch above recorded. Same transaction as
    # the step mutation it accompanies (this handler commits once, below), so
    # the request decision and the step's fate land together or not at all.
    #
    # The kind gate is not redundant with `_lease_decision is not None`:
    # `approval_request_id` is read off `pa` unconditionally and the
    # governance_approval card carries that field too, but that branch grants
    # WITHOUT consuming on purpose (the dispatcher spends the grant when it
    # next leases the step). Only path-C cards may be decided here.
    #
    # LEASE_HITL_CLOSEABLE_KINDS is the very object lease_needs_human mints
    # against, so the mint set and the close set cannot drift apart into
    # minting a kind that nothing here can close.
    if (
        _lease_request_id
        and _lease_decision is not None
        and kind in LEASE_HITL_CLOSEABLE_KINDS
    ):
        from packages.core.constants.approvals import ApprovalStatus
        from packages.core.governance.approvals import (
            consume_approval,
            deny_approval,
            grant_approval,
        )
        from packages.core.models.hitl_request import HitlRequest

        _lease_request = (await db.execute(
            select(HitlRequest).where(
                HitlRequest.id == _lease_request_id,
                HitlRequest.entity_id == user.entity_id,
                HitlRequest.status == ApprovalStatus.PENDING.value,
            )
        )).scalar_one_or_none()
        if _lease_request is not None:
            if _lease_decision == "grant":
                await grant_approval(
                    db, _lease_request, by_user_id=user.id, via="chat_card",
                )
                # Spend it immediately: the user's answer IS the consumption.
                # Nothing downstream consumes a path-C grant, and
                # _find_open_request counts granted-unconsumed rows as still
                # live — so without this the row never leaves that state.
                await consume_approval(db, _lease_request)
            else:
                await deny_approval(
                    db, _lease_request, by_user_id=user.id, via="chat_card",
                )

    queued_learning_ids = await _record_pending_action_resolution_evidence(
        db,
        workspace_id=workspace_id,
        user=user,
        conversation_id=conv.id,
        message_id=msg.id,
        pending_action=pa,
        resolution=resolution,
    )
    await _record_pending_action_activity(
        db,
        workspace_id=workspace_id,
        user=user,
        conversation_id=conv.id,
        message_id=msg.id,
        pending_action=pa,
        resolution=resolution,
    )

    await db.commit()
    if workflow_run_to_enqueue:
        from packages.core.ai.workflow_runner import WorkflowRunner

        if WorkflowRunner.enqueue(workflow_run_to_enqueue) is False:
            workflow_run.status = "failed"
            workflow_run.error = "Workflow could not be queued. Please start it again."
            workflow_run.completed_at = datetime.now(timezone.utc)
            from packages.core.services.workflow_run_trace import (
                update_workflow_history_summary,
            )

            update_workflow_history_summary(workflow_run)
            from packages.core.services.workflow_chat_projection import (
                project_workflow_run_status,
            )

            await project_workflow_run_status(db, run=workflow_run)
            await db.commit()
    await _enqueue_learning_candidate_applies(
        db,
        user=user,
        workspace_id=workspace_id,
        candidate_ids=queued_learning_ids,
    )
    return _to_message(
        resolved,
        resolved_by_user=user if resolved.resolved_by_user_id == user.id else None,
    )


@router.post("/messages/{message_id}/feedback", response_model=MessageResponse)
async def record_chat_message_feedback(
    workspace_id: str,
    message_id: str,
    req: MessageFeedbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record thumbs feedback for a workspace chat message."""
    await _verify_workspace(db, workspace_id, user)

    rating = (req.rating or "").lower()
    if rating not in {"up", "down"}:
        raise HTTPException(400, "rating must be 'up' or 'down'")

    msg = (await db.execute(
        select(Message).where(Message.id == message_id)
    )).scalar_one_or_none()
    if msg is None:
        raise HTTPException(404, "message not found")

    conv = (await db.execute(
        select(Conversation).where(Conversation.id == msg.conversation_id)
    )).scalar_one_or_none()
    if conv is None or conv.workspace_id != workspace_id or conv.entity_id != user.entity_id:
        raise HTTPException(404, "message not found")

    meta = dict(msg.meta or {})
    feedback_by_user = meta.get("task_completion_feedback")
    if not isinstance(feedback_by_user, dict):
        feedback_by_user = {}
    feedback_by_user[user.id] = rating
    meta["task_completion_feedback"] = feedback_by_user
    meta["latest_task_completion_feedback"] = {
        "rating": rating,
        "user_id": user.id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    msg.meta = meta

    queued_learning_ids = await _record_task_completion_feedback_evidence(
        db,
        workspace_id=workspace_id,
        user=user,
        conversation_id=conv.id,
        message=msg,
        rating=rating,
    )

    await db.commit()
    await _enqueue_learning_candidate_applies(
        db,
        user=user,
        workspace_id=workspace_id,
        candidate_ids=queued_learning_ids,
    )
    return _to_message(msg)


# ── Helpers shared across pending_action.kind branches ────────────────────

def _allow_side_effect_after_resolved(pending_action: dict, choice: str | None) -> bool:
    """Return True for intentional callbacks or safe proposal recovery."""
    kind = pending_action.get("kind") if isinstance(pending_action, dict) else None
    normalized = (choice or "").lower()
    if kind == PendingActionKind.NEEDS_LOGIN and normalized == "continue_after_login":
        return True
    if kind == PendingActionKind.WORKFLOW_RETRY and normalized in {"retry", "retry_now"}:
        return True
    # Releases proposal cards affected by the historical approve_all/approve
    # mismatch.  approve_proposal() only selects tickets still in `proposed`,
    # so already-started tickets cannot be dispatched twice.
    return kind == PendingActionKind.APPROVE_PROPOSALS and normalized in {
        APPROVAL_CHOICE_APPROVE,
        "approve_all",
    }


def _apply_step_resume(
    step: Any,
    *,
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
) -> None:
    """Mutate a step row to flip from waiting_human back to pending.
    Pure — caller handles DB load, plan reset, and re-enqueue.

    Extracted from ``_resume_step_for_retry`` so the pure
    state-transition logic is unit-testable without a session."""
    if params_update:
        # Tool wrapper expects values inside step.params; preserve any
        # unrelated existing keys (cookies path, original args, etc.).
        merged = dict(step.params or {})
        merged.update(params_update)
        step.params = merged

    if human_input_response is not None:
        step.human_input_response = human_input_response

    step.step_status = ExecutionStepStatus.PENDING.value
    step.human_input_prompt = None
    step.current_lease_id = None
    step.error = None
    step.finished_at = None


def _apply_step_cancel(step: Any, reason: str) -> None:
    """Mutate a step row to fail it after a 'skip' / 'cancel'
    resolution. Pure — caller handles DB load + re-enqueue."""
    from datetime import datetime, timezone
    step.step_status = ExecutionStepStatus.FAILED.value
    step.error = {"type": "UserSkipped", "message": reason}
    step.human_input_prompt = None
    step.current_lease_id = None
    step.finished_at = datetime.now(timezone.utc)


async def _resume_step_for_retry(
    db: AsyncSession,
    user: User,
    *,
    step_id: str,
    plan_id: Optional[str] = None,
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
) -> None:
    """Reset a waiting_human step back to pending so PlanExecutor /
    Dispatcher pick it up next cycle. Optionally merges fresh values
    into ``step.params`` (answers / confirm flags) before retry, and
    optionally writes ``human_input_response`` for legacy free-form
    HITL replies.

    Caller commits.
    """
    from packages.core.models.execution import ExecutionStep, ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.services.task_state_machine import apply_task_status_transition

    step = (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()
    if step is None:
        return

    _apply_step_resume(
        step,
        params_update=params_update,
        human_input_response=human_input_response,
    )

    # M9.2 — a resumed step means the awaited human input arrived: fulfil
    # any open commitment rows for this step (best-effort, silent no-op).
    try:
        from packages.core.humans import resolve_commitments_for_step
        await resolve_commitments_for_step(
            db, step.id,
            {"kind": "hitl_response"},
        )
    except Exception:
        logger.warning(
            "human commitment resolve failed for step %s (ignored)",
            step.id, exc_info=True,
        )

    target_plan_id = plan_id or step.plan_id
    if not target_plan_id:
        return

    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == target_plan_id)
    )).scalar_one_or_none()
    if plan:
        plan.status = ExecutionPlanStatus.RUNNING.value
        plan.completed_at = None
        plan.last_error = None
        if plan.task_id:
            task = (await db.execute(
                select(Task).where(
                    Task.id == plan.task_id,
                    Task.entity_id == user.entity_id,
                )
            )).scalar_one_or_none()
            if task and task.status == TaskStatus.WAITING_ON_CUSTOMER:
                await apply_task_status_transition(
                    task, "in_progress", db=db, actor_kind="user", actor_id=user.id,
                )

    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(target_plan_id)
    except Exception:
        pass  # best-effort — next heartbeat will pick it up


async def _cancel_step(
    db: AsyncSession,
    user: User,  # noqa: ARG001 — accepted for parity with _resume_step_for_retry
    *,
    step_id: str,
    plan_id: Optional[str] = None,
    reason: str = "user skipped",
) -> None:
    """User chose 'skip' / 'cancel' on a pending_action — fail the
    step so the plan can finalize. The PlanExecutor's terminal
    summary handles the failed → replan-or-fail decision on the next
    cycle.

    Caller commits.
    """
    from packages.core.models.execution import ExecutionStep

    step = (await db.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one_or_none()
    if step is None:
        return

    _apply_step_cancel(step, reason)

    target_plan_id = plan_id or step.plan_id
    if not target_plan_id:
        return

    # Re-enqueue the executor so it sees the failed step and decides
    # whether to replan or terminate the plan.
    try:
        from packages.core.tasks.ai_tasks import run_plan
        run_plan.delay(target_plan_id)
    except Exception:
        pass
