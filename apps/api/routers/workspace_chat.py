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
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from celery.exceptions import SoftTimeLimitExceeded
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    get_current_user,
    require_plan,
    require_workspace_authority,
    require_workspace_writable,
)
from packages.core.workspaces import is_sandbox_workspace
from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS
from packages.core.constants.approvals import (
    LEASE_KIND_HITL_TYPES,
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
)
from packages.core.constants.channels import ExternalMessageActionKey
from packages.core.constants.pending_actions import (
    WORKFLOW_RUN_ACTION_KINDS,
    PendingActionKind,
)
from packages.core.constants.task import TaskStatus
from packages.core.database import get_db
from packages.core.ai.runtime.output_policy import (
    runtime_public_assistant_message_content,
    runtime_public_failure_payload,
    runtime_public_tool_calls,
    runtime_public_tool_payload,
)
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    HumanDecisionIntent,
    decision_intent_for_hitl,
    external_reply_decision_intent,
)
from packages.core.services.chat_feedback import (
    ChatFeedbackEvidenceType,
    ChatFeedbackIntegrityErrorKind,
    ChatFeedbackMutationStatus,
    ChatFeedbackRating,
    ChatFeedbackSubject,
    ChatFeedbackTargetDeletedError,
    ChatFeedbackTargetKind,
    ChatFeedbackTargetPolicyFactory,
    build_chat_feedback_content_preview,
    classify_chat_feedback_target_kind,
    classify_chat_feedback_integrity_error,
    lock_completion_feedback_subject,
    persist_chat_message_feedback,
)
from packages.core.services.step_resume import (
    apply_step_cancel,
    apply_step_resume,
    cancel_step,
    lock_waiting_step_for_decision,
    resume_step_for_retry,
)
from packages.core.services.workspace_access import (
    user_can_read_workspace,
)
from packages.core.blueprints.simulation_runtime import (
    SimulationRuntimeError,
    get_simulation_run,
    resolve_simulation_action,
    start_simulation_run,
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
    # The *other* HITL channel. `pending_action` carries cards posted by the
    # governance/step gate; a tool that returns a `__hitl__` envelope mid-turn
    # is recorded by chat_service into `messages.metadata->'hitl_requests'`
    # instead, and never touches `pending_action`. Workspace chat used to read
    # only the former, so a gated tool call (e.g. `email.send`) produced a
    # record, a badge, and nothing the user could click. Typed here with the
    # same name and shape the main-chat `MessageResponse` uses so the frontend
    # contract stays single.
    hitl_requests: Optional[list[dict]] = None
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


class SimulationRunResponse(BaseModel):
    workspace_id: str
    enabled: bool
    run_id: Optional[str] = None
    status: Literal["idle", "running", "waiting", "completed"]
    title: str
    goal_title: str
    stage_index: int
    stage_count: int
    stage_id: Optional[str] = None
    stage_title: str
    waiting_message_id: Optional[str] = None
    last_message_id: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    decision_count: int = 0
    runtime_version: str


class MessageFeedbackRequest(BaseModel):
    rating: str


class CompletionFeedbackResponse(MessageResponse):
    rating: ChatFeedbackRating
    mutation_sequence: int
    mutation_status: ChatFeedbackMutationStatus
    feedback_updated_at: datetime
    feedback_target_kind: ChatFeedbackTargetKind
    feedback_target_id: str
    feedback_task_id: Optional[str] = None
    feedback_plan_id: Optional[str] = None


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


def _message_hitl_requests(m: Message) -> list[dict] | None:
    """Tool-call HITL cards recorded in message metadata.

    Mirrors ``apps/api/routers/chat.py::_message_hitl_requests`` — the same
    column, the same shape, so both chat surfaces render from one contract.
    """
    meta = m.meta if isinstance(m.meta, dict) else {}
    requests = meta.get("hitl_requests")
    if not isinstance(requests, list):
        return None
    entries = [item for item in requests if isinstance(item, dict) and item.get("id")]
    return entries or None


def _to_message(
    m: Message,
    *,
    refs: Optional[list[dict]] = None,
    author_user: User | None = None,
    resolved_by_user: User | None = None,
    updated_at: datetime | None = None,
    recovery_reason: str | None = None,
) -> MessageResponse:
    pending_action = m.pending_action if isinstance(m.pending_action, dict) and m.pending_action.get("kind") else None
    if recovery_reason and pending_action:
        pending_action = {
            **pending_action,
            "payload": {**pending_action.get("payload", {}), "why": recovery_reason},
        }
    author_user_id = _message_author_user_id(m)
    raw_meta = m.meta if isinstance(m.meta, dict) else {}
    public_meta = runtime_public_tool_payload(raw_meta)
    if "limit_detail" in raw_meta:
        public_meta["limit_detail"] = runtime_public_failure_payload(
            raw_meta["limit_detail"]
        )
    stop_reason = str(raw_meta.get("stop_reason") or "").lower()
    has_terminal_error = (
        raw_meta.get("stream_error") is True
        or str(raw_meta.get("stream_status") or "").lower() == "error"
        or stop_reason == "error"
        or bool(raw_meta.get("error"))
    )
    has_interrupted_stream = (
        raw_meta.get("stream_interrupted") is True
        or str(raw_meta.get("stream_status") or "").lower() == "interrupted"
    )
    if has_terminal_error or has_interrupted_stream:
        public_meta.pop("assistant_blocks", None)
    body = runtime_public_assistant_message_content(
        m.content,
        raw_meta,
        m.tool_calls,
    )
    return MessageResponse(
        id=m.id,
        conversation_id=m.conversation_id,
        created_at=m.created_at,
        updated_at=updated_at,
        body=body,
        tool_calls=runtime_public_tool_calls(m.tool_calls),
        assistant_blocks=public_meta.get("assistant_blocks"),
        message_kind=m.message_kind,
        author_kind=m.author_kind,
        author_user_id=author_user_id,
        author_user_name=_user_display_name(author_user),
        author_user_email=getattr(author_user, "email", None) if author_user else None,
        author_user_avatar_url=getattr(author_user, "avatar_url", None) if author_user else None,
        author_subscription_id=m.author_subscription_id,
        refs=refs if refs is not None else m.refs,
        attachments=m.attachments,
        meta=public_meta,
        pending_action=pending_action,
        hitl_requests=_message_hitl_requests(m),
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

_ACTIONABLE_WORKFLOW_STATUSES = {"queued", "pending", "running", "paused", "failed"}
_ACTIONABLE_WORKFLOW_OUTCOMES = {
    "needs_input",
    "revision_required",
    "ready_for_acceptance",
}


def _workspace_entrypoint_http_error(exc: ValueError) -> HTTPException | None:
    """Expose launch-time Workspace preflight blockers to the chat client.

    Only the deterministic service-dependency preflight is translated here.
    Other ``ValueError`` instances keep their existing generic error boundary
    so this route does not accidentally disclose unrelated internals.
    """
    detail = str(exc).strip()
    if detail.startswith("Workflow preflight missing active Workspace services:"):
        return HTTPException(status_code=409, detail=detail)
    return None


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
    "workflow_run": "approve_automation_changes",
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

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())

    add(note)
    if isinstance(payload, dict):
        for key in ("feedback", "guidance", "instruction", "comment", "message", "response", "text"):
            add(payload.get(key))
        answers = payload.get("answers")
        if isinstance(answers, dict):
            for value in answers.values():
                add(value)
        review = payload.get("review")
        if isinstance(review, dict):
            add(review.get("revision_request"))
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


async def _legacy_recovery_reasons(
    db: AsyncSession,
    messages: list[Message],
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, str]:
    """Repair pre-fix display projections without rewriting approval payloads.

    Old recovery cards used the generic missing-artifact check even when the
    same Plan recorded a concrete execution failure. Only that legacy text is
    replaced, and only from the exact Task/Plan's durable supervisor evidence.
    """
    candidates = {}
    for message in messages:
        action = message.pending_action if isinstance(message.pending_action, dict) else {}
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        if (
            not message.resolved_at
            and action.get("kind") == PendingActionKind.TASK_RECOVERY.value
            and action.get("task_id") and action.get("plan_id")
            and str(payload.get("why") or "").startswith("This workspace task needs a saved file/media/document deliverable,")
        ):
            candidates[message.id] = action
    if not candidates:
        return {}

    from packages.core.models.task import Task

    rows = (await db.execute(select(Task.id, Task.actual_output).where(
        Task.id.in_({action["task_id"] for action in candidates.values()}),
        Task.entity_id == entity_id, Task.workspace_id == workspace_id,
        Task.status == TaskStatus.WAITING_ON_CUSTOMER,
    ))).all()
    outputs = {task_id: output for task_id, output in rows if isinstance(output, dict)}
    reasons = {}
    for message_id, action in candidates.items():
        output = outputs.get(action["task_id"], {})
        evidence = output.get("supervisor_evidence")
        if (
            output.get("plan_id") == action["plan_id"]
            and output.get("supervisor_verdict") == "needs_human"
            and isinstance(evidence, str) and evidence.strip()
        ):
            reasons[message_id] = evidence
    return reasons


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
    recovery_reasons = await _legacy_recovery_reasons(
        db, rows, entity_id=entity_id, workspace_id=workspace_id,
    )
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
            recovery_reason=recovery_reasons.get(m.id),
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


async def _record_completion_feedback_evidence(
    db: AsyncSession,
    *,
    workspace_id: str,
    user: User,
    conversation_id: str,
    message: Message,
    rating: ChatFeedbackRating,
    subject: ChatFeedbackSubject,
) -> list[str]:
    """Replace the current evidence without poisoning accepted feedback.

    Removing the old signal is part of the canonical transaction. Inserting
    its derived replacement is best-effort behind a savepoint, so a failed
    insertion cannot leave either a stale rating or an aborted session.
    """
    from packages.core.models.runtime_learning import RuntimeEvidence

    evidence_type = (
        ChatFeedbackEvidenceType.TASK_COMPLETION
        if subject.target_kind == ChatFeedbackTargetKind.TASK_COMPLETION
        else ChatFeedbackEvidenceType.PLAN_COMPLETION
    )

    await db.execute(
        sa_delete(RuntimeEvidence).where(
            RuntimeEvidence.workspace_id == workspace_id,
            RuntimeEvidence.user_id == user.id,
            RuntimeEvidence.evidence_type.in_(
                tuple(item.value for item in ChatFeedbackEvidenceType)
            ),
            RuntimeEvidence.details["target_kind"].as_string()
            == subject.target_kind.value,
            RuntimeEvidence.details["target_id"].as_string()
            == subject.target_id,
        )
    )

    try:
        from packages.core.services.runtime_learning import (
            queued_learning_candidate_ids,
            record_user_signal_evidence,
        )

        label = (
            "helpful" if rating == ChatFeedbackRating.UP else "not helpful"
        )
        async with db.begin_nested():
            _evidence, candidates = await record_user_signal_evidence(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                user_id=user.id,
                conversation_id=conversation_id,
                message_id=message.id,
                task_id=subject.task_id,
                evidence_type=evidence_type.value,
                source="workspace_chat",
                status="succeeded",
                summary=(
                    "Workspace chat "
                    f"{subject.target_kind.value.replace('_', ' ')} marked {label}"
                ),
                details={
                    "rating": rating.value,
                    "target_kind": subject.target_kind.value,
                    "target_id": subject.target_id,
                    "task_id": subject.task_id,
                    "plan_id": subject.plan_id,
                    "message_body_preview": (message.content or "")[:240],
                },
                metrics={
                    "helpful": 1
                    if rating == ChatFeedbackRating.UP
                    else 0
                },
            )
        return queued_learning_candidate_ids(candidates)
    except Exception:
        logger.debug("completion feedback evidence skipped", exc_info=True)
        return []


async def _resolve_completion_feedback_subject(
    db: AsyncSession,
    *,
    message: Message,
    entity_id: str,
    workspace_id: str,
    lock_rows: bool = True,
) -> ChatFeedbackSubject | None:
    """Resolve one completed Plan/Task into its canonical feedback subject."""
    from packages.core.constants.execution import ExecutionPlanStatus
    from packages.core.constants.task import TaskStatus
    from packages.core.models.execution import ExecutionPlan

    target_kind = classify_chat_feedback_target_kind(message)
    if target_kind not in {
        ChatFeedbackTargetKind.TASK_COMPLETION,
        ChatFeedbackTargetKind.PLAN_COMPLETION,
    }:
        return None
    task_ref_id = _message_ref_id(message, "task")
    plan_ref_id = _message_ref_id(message, "plan")

    plan_task_id: str | None = None
    if plan_ref_id:
        plan_statement = select(
            ExecutionPlan.id,
            ExecutionPlan.task_id,
        ).where(
            ExecutionPlan.id == plan_ref_id,
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.workspace_id == workspace_id,
            ExecutionPlan.status == ExecutionPlanStatus.COMPLETED.value,
        )
        if lock_rows:
            plan_statement = plan_statement.with_for_update()
        plan_row = (
            await db.execute(plan_statement)
        ).one_or_none()
        if plan_row is None:
            return None
        plan_task_id = plan_row.task_id

    task_id = task_ref_id or plan_task_id
    if task_ref_id and plan_task_id and task_ref_id != plan_task_id:
        return None
    if task_id:
        task_statement = select(Task.id).where(
            Task.id == task_id,
            Task.entity_id == entity_id,
            Task.workspace_id == workspace_id,
            Task.status == TaskStatus.COMPLETED.value,
        )
        if lock_rows:
            task_statement = task_statement.with_for_update()
        valid_task_id = (
            await db.execute(task_statement)
        ).scalar_one_or_none()
        if valid_task_id is None:
            return None

    if target_kind == ChatFeedbackTargetKind.TASK_COMPLETION and not task_id:
        return None
    if target_kind == ChatFeedbackTargetKind.PLAN_COMPLETION and not plan_ref_id:
        return None

    target_id = plan_ref_id or task_id
    if target_id is None:
        return None
    canonical_target_kind = (
        ChatFeedbackTargetKind.TASK_COMPLETION
        if task_id
        else ChatFeedbackTargetKind.PLAN_COMPLETION
    )
    if target_kind != canonical_target_kind:
        return None
    return ChatFeedbackSubject(
        target_kind=canonical_target_kind,
        target_id=target_id,
        task_id=task_id,
        plan_id=plan_ref_id,
    )


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
        user=user,
        require_control=True,
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
    local_worker_id: str | None = Form(None),
    thread_ref_kind: str | None = Form(None),
    thread_ref_id: str | None = Form(None),
    document_ids: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    _gate=Depends(require_plan("ai_budget_usd")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _verify_workspace(db, workspace_id, user)
    from packages.core.services.workspace_access import (
        user_can_write_workspace_artifacts,
    )

    if not await user_can_write_workspace_artifacts(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        entity_role=user.role,
    ):
        raise HTTPException(403, "User cannot run Flows in this Workspace")
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
    try:
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
            thread_ref_kind=thread_ref_kind,
            thread_ref_id=thread_ref_id,
            route_source="explicit",
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        http_error = _workspace_entrypoint_http_error(exc)
        if http_error is not None:
            raise http_error from exc
        raise
    if local_worker_id:
        from packages.core.services.local_worker_targeting import (
            select_conversation_local_worker_target,
        )

        try:
            await select_conversation_local_worker_target(
                db,
                conversation_id=started.conversation.id,
                entity_id=user.entity_id,
                user_id=user.id,
                worker_id=local_worker_id,
            )
            await db.commit()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    return StreamingResponse(
        _workspace_entrypoint_started_stream(started),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

@router.get("/simulation-run", response_model=SimulationRunResponse)
async def simulation_run_status(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await _verify_workspace(db, workspace_id, user)
    try:
        state = await get_simulation_run(db, workspace=workspace)
    except SimulationRuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    # Legacy sandboxes may be repaired while their status is read.
    await db.commit()
    return state


@router.post("/simulation-run/start", response_model=SimulationRunResponse)
async def start_workspace_simulation_run(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await _verify_workspace(db, workspace_id, user)
    try:
        state = await start_simulation_run(
            db,
            workspace=workspace,
            user_id=user.id,
        )
    except SimulationRuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return state


@router.post("/simulation-run/restart", response_model=SimulationRunResponse)
async def restart_workspace_simulation_run(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await _verify_workspace(db, workspace_id, user)
    try:
        state = await start_simulation_run(
            db,
            workspace=workspace,
            user_id=user.id,
            restart=True,
        )
    except SimulationRuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return state


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
    is_main_view = not (thread_ref_kind and thread_ref_id)
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
        # A background Plan can reach HITL before anyone has sent the first
        # Workspace message, so there may be no ``workspace_main``
        # Conversation yet. The actionable card lives in its Plan thread and
        # must still be projected into the main view; otherwise the sidebar
        # badge says work is waiting while the chat is blank.
        pinned: list[Message] = []
        if is_main_view and before_created_at is None:
            pinned = await chat_service.unresolved_pending_messages(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                limit=_PINNED_ACTION_LIMIT,
            )
        return MessagesPageResponse(
            items=await _hydrate_messages(
                db,
                sorted(pinned, key=lambda m: (m.created_at, m.id)),
                entity_id=user.entity_id,
                workspace_id=workspace_id,
            ),
            has_more=False,
            next_cursor=None,
            open_action_count=await chat_service.count_open_pending_actions(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
            ),
            open_actions_complete=(
                len(pinned) < _PINNED_ACTION_LIMIT
                if is_main_view and before_created_at is None
                else True
            ),
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
    workspace = await _verify_workspace(db, workspace_id, user)
    if is_sandbox_workspace(workspace):
        raise HTTPException(
            409,
            "Workspace simulation only accepts guided simulation actions",
        )
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


@router.get("/messages/{message_id}", response_model=MessageResponse)
async def get_chat_message(
    workspace_id: str,
    message_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one workspace-scoped message for realtime reconciliation."""
    await _verify_workspace(db, workspace_id, user)
    message = (
        await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Conversation.entity_id == user.entity_id,
                Conversation.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(404, "message not found")
    hydrated = await _hydrate_messages(
        db,
        [message],
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    response = hydrated[0]
    meta = dict(response.meta or {})
    activity = meta.get("strategist_activity")
    if not isinstance(activity, dict) or activity.get("state") != "running":
        return response
    review_id = activity.get("review_id")
    if not review_id:
        return response

    from packages.core.constants.review import ReviewRunStatus
    from packages.core.models.review_run import ReviewRun
    from packages.core.review import review_lease_is_expired

    review = (
        await db.execute(
            select(ReviewRun).where(
                ReviewRun.id == str(review_id),
                ReviewRun.entity_id == user.entity_id,
                ReviewRun.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if review is None:
        return response

    projected_state = {
        ReviewRunStatus.SUCCEEDED: "completed",
        ReviewRunStatus.SKIPPED: "skipped",
        ReviewRunStatus.FAILED: "failed",
    }.get(review.status)
    if (
        projected_state is None
        and review.status == ReviewRunStatus.RUNNING
        and review_lease_is_expired(review)
    ):
        # An expired owner cannot renew or pass the final ReviewRun fence, so
        # this activity is no longer live even before the next trigger records
        # the abandoned ReviewRun as failed.
        projected_state = "failed"
    if projected_state is None:
        return response

    finished_at = review.completed_at or review.lease_expires_at or datetime.now(timezone.utc)
    meta["strategist_activity"] = {
        **activity,
        "state": projected_state,
        "finished_at": finished_at.isoformat(),
    }
    return response.model_copy(update={"meta": meta})


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
    workspace = await _verify_workspace(db, workspace_id, user)
    if workspace.status == "paused":
        raise HTTPException(409, "workspace is paused; pending actions cannot be resolved")

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
    normalized_choice = (req.choice or "").strip().lower()
    external_decision_intent = (
        external_reply_decision_intent(normalized_choice)
        if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL
        else None
    )
    if (
        kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL
        and external_decision_intent
        not in {
            HumanDecisionIntent.APPROVE,
            HumanDecisionIntent.APPROVE_STANDING,
            HumanDecisionIntent.DENY,
            HumanDecisionIntent.CANCEL,
        }
    ):
        raise HTTPException(400, "invalid external reply decision")
    external_approval_requested = (
        kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL
        and external_decision_intent in {
            HumanDecisionIntent.APPROVE,
            HumanDecisionIntent.APPROVE_STANDING,
        }
    )
    login_started = (
        kind == PendingActionKind.NEEDS_LOGIN
        and normalized_choice == "sign_in"
    )
    task_retry_result = None
    task_runtime_update_id: str | None = None
    task_runtime_update_plan_id: str | None = None
    plan_to_run_after_commit: str | None = None
    governance_request = None
    governance_step = None
    lease_request = None
    task_approval_task = None
    task_recovery_task = None
    governance_hitl_type = str(
        pa.get("hitl_type") or HitlType.AUTHORIZE.value
    ).strip().lower()
    if pa.get("simulation_runtime") is True:
        if msg.resolved_at is not None:
            return _to_message(
                msg,
                resolved_by_user=user if msg.resolved_by_user_id == user.id else None,
            )
        try:
            resolved = await resolve_simulation_action(
                db,
                workspace=workspace,
                message=msg,
                user_id=user.id,
                choice=req.choice,
                note=req.note,
                payload=req.payload,
            )
        except SimulationRuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        await db.commit()
        return _to_message(resolved, resolved_by_user=user)
    if kind in WORKSPACE_WORKFLOW_RUN_ACTION_KINDS:
        from packages.core.services.workflow_message_actions import (
            WorkflowMessageActionError,
            resolve_workflow_message_action,
        )

        try:
            workflow_action = await resolve_workflow_message_action(
                db,
                message=msg,
                conversation=conv,
                choice=req.choice,
                note=req.note,
                payload=req.payload,
                user=user,
            )
        except WorkflowMessageActionError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        if workflow_action is not None:
            return _to_message(
                workflow_action.message,
                resolved_by_user=(
                    user
                    if workflow_action.message.resolved_by_user_id == user.id
                    else None
                ),
            )
    proposal_always_approve = (
        kind == PendingActionKind.APPROVE_PROPOSALS
        and normalized_choice == APPROVAL_CHOICE_ALWAYS_APPROVE
    )
    if proposal_always_approve and not resolution.get("note"):
        resolution["note"] = "Future workspace proposals in this workspace will start automatically."
    if proposal_always_approve:
        await require_workspace_authority(
            db,
            user,
            workspace_id,
            "manage_standing_grants",
        )
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
    if kind == PendingActionKind.TASK_APPROVAL and pa.get("task_id"):
        await require_workspace_authority(
            db,
            user,
            workspace_id,
            "approve_tasks",
        )
        # All Task decision surfaces own Task before Message. The shared
        # approval service rechecks the same row lock before consuming the
        # decision, so direct API and Chat cannot apply conflicting choices.
        task_approval_task = (await db.execute(
            select(Task)
            .where(
                Task.id == str(pa["task_id"]),
                Task.entity_id == user.entity_id,
                Task.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task_approval_task is None:
            raise HTTPException(404, "task not found")
    if kind == PendingActionKind.TASK_RECOVERY and pa.get("task_id"):
        await require_workspace_writable(db, user, workspace_id)
        # Task retry owns the Task row before it resolves the shared Message.
        # Keep this Task -> Message order consistent with the direct Task API
        # so concurrent retry surfaces cannot deadlock or dispatch twice.
        task_recovery_task = (await db.execute(
            select(Task)
            .where(
                Task.id == str(pa["task_id"]),
                Task.entity_id == user.entity_id,
                Task.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task_recovery_task is None:
            raise HTTPException(404, "task not found")
    if kind == PendingActionKind.GOVERNANCE_APPROVAL and pa.get("step_id"):
        request_id = pa.get("approval_request_id")
        if request_id:
            from packages.core.models.hitl_request import HitlRequest

            governance_request = (await db.execute(
                select(HitlRequest).where(
                    HitlRequest.id == request_id,
                    HitlRequest.entity_id == user.entity_id,
                    HitlRequest.workspace_id == workspace_id,
                ).with_for_update()
            )).scalar_one_or_none()
            if governance_request is None:
                raise HTTPException(409, "approval request is stale or outside this workspace")
    if kind in LEASE_HITL_CLOSEABLE_KINDS and pa.get("step_id"):
        request_id = pa.get("approval_request_id")
        if request_id:
            from packages.core.models.hitl_request import HitlRequest

            lease_request = (await db.execute(
                select(HitlRequest).where(
                    HitlRequest.id == request_id,
                    HitlRequest.entity_id == user.entity_id,
                    HitlRequest.workspace_id == workspace_id,
                    HitlRequest.origin_kind == ApprovalOriginKind.LEASE.value,
                    HitlRequest.origin_step_id == pa.get("step_id"),
                    HitlRequest.origin_plan_id == pa.get("plan_id"),
                ).with_for_update()
            )).scalar_one_or_none()
            if lease_request is None:
                raise HTTPException(409, "human-input request is stale or outside this workspace")
            if pa.get("task_id") is not None and str(
                lease_request.origin_task_id
            ) != str(pa.get("task_id")):
                raise HTTPException(409, "human-input request origin does not match this card")
            request_context = (
                lease_request.context
                if isinstance(lease_request.context, dict)
                else {}
            )
            request_pending_kind = request_context.get("pending_kind")
            if request_pending_kind and str(request_pending_kind) != str(kind):
                raise HTTPException(409, "human-input request type does not match this card")

    # Serialize every card decision. Approval-backed cards use request ->
    # message -> Step lock order, matching the other approval surfaces and
    # preventing a stale Chat click from racing a Task/Plan-level decision.
    decision_message_statement = (
        select(Message)
        .where(Message.id == message_id)
        .execution_options(populate_existing=True)
    )
    if not external_approval_requested:
        decision_message_statement = decision_message_statement.with_for_update()
    msg = (
        await db.execute(decision_message_statement)
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(404, "message not found")
    if msg.resolved_at is not None and not _allow_side_effect_after_resolved(pa, req.choice):
        return _to_message(
            msg,
            resolved_by_user=user if msg.resolved_by_user_id == user.id else None,
        )
    if kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        from packages.core.humans.authority import ParticipantAuthority

        await require_workspace_authority(
            db,
            user,
            workspace_id,
            ParticipantAuthority.APPROVE_EXTERNAL_PUBLISH.value,
        )
        if external_decision_intent is HumanDecisionIntent.APPROVE_STANDING:
            await require_workspace_authority(
                db,
                user,
                workspace_id,
                ParticipantAuthority.MANAGE_STANDING_GRANTS.value,
            )
            if not str(pa.get("channel_config_id") or "").strip():
                raise HTTPException(
                    409,
                    "External reply account scope is missing; reconnect the "
                    "channel before creating a standing grant.",
                )
    if (
        kind == PendingActionKind.EXTERNAL_MESSAGE_APPROVAL
        and not external_approval_requested
    ):
        from packages.core.services.channel_outbound_delivery import (
            external_reply_approval_claim_conflict_reason,
        )

        conflict_reason = external_reply_approval_claim_conflict_reason(msg)
        if conflict_reason is not None:
            detail = (
                "External reply delivery outcome is unknown and requires "
                "manual reconciliation; automatic retry is disabled."
                if conflict_reason == "approval_delivery_outcome_unknown"
                else "External reply delivery is already in progress; retry shortly."
            )
            raise HTTPException(
                409,
                detail,
            )
    if (
        lease_request is not None
        and lease_request.status != ApprovalStatus.PENDING.value
    ):
        raise HTTPException(409, "human-input request has already been resolved")

    if governance_request is not None:
        if governance_request.status != ApprovalStatus.PENDING.value:
            raise HTTPException(409, "approval request has already been resolved")
        if governance_request.origin_kind != ApprovalOriginKind.STEP.value:
            raise HTTPException(409, "approval request origin does not match this card")
        origin_pairs = (
            (governance_request.origin_step_id, pa.get("step_id")),
            (governance_request.origin_plan_id, pa.get("plan_id")),
            (governance_request.origin_task_id, pa.get("task_id")),
        )
        if any(
            (stored is None) != (projected is None)
            or (
                stored is not None
                and projected is not None
                and str(stored) != str(projected)
            )
            for stored, projected in origin_pairs
        ):
            raise HTTPException(409, "approval request origin does not match this card")

        request_hitl_type = str(
            governance_request.hitl_type or HitlType.AUTHORIZE.value
        ).strip().lower()
        projected_hitl_type = str(pa.get("hitl_type") or "").strip().lower()
        if projected_hitl_type and projected_hitl_type != request_hitl_type:
            raise HTTPException(409, "approval request type does not match this card")
        governance_hitl_type = request_hitl_type

    if kind in LEASE_HITL_CLOSEABLE_KINDS and lease_request is None:
        # Pre-unified input/login/confirmation cards have no HitlRequest row,
        # but resolving them still mutates a Step. Apply the same response or
        # governance authority as their typed successor before touching state.
        from packages.core.services.runtime_authorization import (
            authorize_hitl_action,
        )

        authority = await authorize_hitl_action(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            by_user_id=user.id,
            action_key=pa.get("action"),
            capability_id=pa.get("capability_id"),
            hitl_type=LEASE_KIND_HITL_TYPES[str(kind)],
            origin_conversation_id=conv.id,
        )
        if not authority.allowed:
            raise HTTPException(
                403,
                authority.reason
                or "You do not have permission to resolve this human request.",
            )

    if kind == PendingActionKind.GOVERNANCE_APPROVAL and pa.get("step_id"):
        if governance_request is None:
            # Inline Plan reviews and pre-unified cards have no HitlRequest
            # row, but they are still governance decisions. Route them through
            # the same authority contract before mutating Message or Step.
            from packages.core.services.runtime_authorization import (
                authorize_hitl_action,
            )

            authority = await authorize_hitl_action(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                by_user_id=user.id,
                action_key=pa.get("action"),
                capability_id=pa.get("capability_id"),
                hitl_type=governance_hitl_type,
                standing=(normalized_choice == APPROVAL_CHOICE_ALWAYS_APPROVE),
                origin_conversation_id=conv.id,
            )
            if not authority.allowed:
                raise HTTPException(
                    403,
                    authority.reason
                    or "You do not have permission to resolve this human decision.",
                )
        governance_step = await lock_waiting_step_for_decision(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            task_id=(
                governance_request.origin_task_id
                if governance_request else pa.get("task_id")
            ),
            plan_id=pa.get("plan_id"),
            step_id=pa["step_id"],
        )
        if governance_step is None:
            raise HTTPException(409, "approval request is no longer waiting for a decision")

    if login_started:
        # Opening the login flow is not a human decision. Keep the Message and
        # HitlRequest actionable so a failed/abandoned popup or a page refresh
        # still leaves Continue and Skip available.
        return _to_message(msg)

    # External provider delivery is a three-phase operation: durable attempt,
    # provider call without a locked approval row, then single consumption.
    # Every other action retains the original atomic resolve path.
    resolved = msg if external_approval_requested else await chat_service.resolve_pending_action(
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
    _lease_decision: Literal["grant", "deny"] | None = None

    if kind == PendingActionKind.HUMAN_INPUT and pa.get("step_id"):
        # Lease-level HITL (legacy free-form text input): stash the
        # response on the step row + flip back to pending.
        plan_to_run_after_commit = await _resume_step_for_retry(
            db, user,
            step_id=pa["step_id"],
            plan_id=pa.get("plan_id"),
            human_input_response=(req.payload or {"choice": req.choice, "note": req.note}),
            workspace_id=workspace_id,
            task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
            enqueue=False,
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
            plan_to_run_after_commit = await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                params_update={"answers": answers},
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                enqueue=False,
            )
            _lease_decision = "grant"
        else:
            # skip / cancel — fail the step so the plan can move on.
            plan_to_run_after_commit = await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                reason="user skipped needs_input",
                enqueue=False,
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
            plan_to_run_after_commit = await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                params_update={"confirm": True, "confirm_destructive": True},
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                enqueue=False,
            )
            _lease_decision = "grant"
        else:
            plan_to_run_after_commit = await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                reason="user cancelled needs_confirmation",
                enqueue=False,
            )
            _lease_decision = "deny"

    elif kind == PendingActionKind.NEEDS_LOGIN and pa.get("step_id"):
        # Tool returned _pending_action(kind="needs_login") — login
        # wall hit. Resolution:
        #   choice="sign_in" is handled above without resolving the card.
        #   choice="continue_after_login" → cookies have just been
        #     captured (Integration row updated upstream); retry the
        #     step so the dispatcher leases fresh credentials.
        #   choice="skip" → fail the step.
        choice = (req.choice or "").lower()
        if choice == "continue_after_login":
            plan_to_run_after_commit = await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                enqueue=False,
            )
            _lease_decision = "grant"
        else:
            plan_to_run_after_commit = await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                workspace_id=workspace_id,
                task_id=lease_request.origin_task_id if lease_request else pa.get("task_id"),
                reason="user skipped needs_login",
                enqueue=False,
            )
            _lease_decision = "deny"

    elif kind == PendingActionKind.GOVERNANCE_APPROVAL and pa.get("step_id"):
        choice = (req.choice or "").lower()
        _intent = decision_intent_for_hitl(governance_hitl_type, choice)
        if _intent is HumanDecisionIntent.OTHER:
            raise HTTPException(400, "choice is not valid for this human decision")
        _always = _intent is HumanDecisionIntent.APPROVE_STANDING
        _change_request = _intent is HumanDecisionIntent.REQUEST_CHANGES
        # The card carries the unified HitlRequest id. A stale pre-upgrade
        # card has none — resuming still works: the dispatcher re-gates the
        # step, finds no grant, and posts a fresh card carrying a request id.
        # An `error` card offers retry/cancel rather than approve/reject: the
        # step already ran, so there is nothing to authorize — the user went
        # and fixed something and now wants it run again. Same resume path,
        # honest label. Without this, "retry" would fall through to the else
        # branch and CANCEL the step the user just repaired.
        _resume = _intent in {
            HumanDecisionIntent.APPROVE,
            HumanDecisionIntent.RETRY,
        }
        if _resume or _always:
            # "Always" is a PROMOTION of this action-scope request to a
            # tool-scope standing grant, and grant_approval(standing=True) is
            # the one place that performs it: it writes the workspace
            # auto-approve set and records the widened scope on the row.
            # Writing the auto-approve set here instead — which is what this
            # branch used to do — routed the step plane around both.
            _promoted = False
            if governance_request is not None:
                from packages.core.governance.approvals import grant_approval

                await grant_approval(
                    db, governance_request, by_user_id=user.id, via="chat_card",
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
            # A retry note is guidance for the re-run, not commentary: write
            # it into human_input_response so the worker renders it into the
            # retried step's prompt. Without it, "Retry with guidance" would
            # re-run the exact attempt the user just watched fail.
            _retry_note = (req.note or "").strip()
            _human_review_response = (
                governance_step is not None and governance_step.kind == "human"
            )
            plan_to_run_after_commit = await _resume_step_for_retry(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                human_input_response=(
                    {
                        "choice": choice,
                        "note": _retry_note,
                        "user": _user_display_name(user),
                        "via": "chat_card",
                    }
                    if _retry_note or _human_review_response
                    else None
                ),
                workspace_id=workspace_id,
                task_id=(
                    governance_request.origin_task_id
                    if governance_request else pa.get("task_id")
                ),
                enqueue=False,
            )
        else:
            _guidance = _pending_action_guidance_text(req.note, req.payload)
            _error_type = (
                "UserRequestedChanges"
                if _change_request
                else "UserDeniedApproval"
                if _intent is HumanDecisionIntent.DENY
                else "UserSkipped"
            )
            _decision_reason = (
                "user requested changes"
                if _change_request
                else "user denied governance approval"
                if _intent is HumanDecisionIntent.DENY
                else "user cancelled review"
                if governance_hitl_type == HitlType.REVIEW.value
                else "user cancelled step recovery"
            )
            if governance_request is not None:
                from packages.core.governance.approvals import deny_approval

                await deny_approval(
                    db, governance_request, by_user_id=user.id, via="chat_card",
                    reason=_decision_reason,
                )
            plan_to_run_after_commit = await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                workspace_id=workspace_id,
                task_id=(
                    governance_request.origin_task_id
                    if governance_request else pa.get("task_id")
                ),
                reason=(
                    f"{_decision_reason}: {_guidance}"
                    if _guidance else _decision_reason
                ),
                error_type=_error_type,
                human_decision={
                    "choice": choice,
                    "guidance": _guidance or None,
                    "via": "chat_card",
                },
                enqueue=False,
            )

    elif kind == PendingActionKind.TASK_APPROVAL and pa.get("task_id"):
        from packages.core.services.task_approval_service import (
            TaskApprovalDecisionError,
            apply_task_approval_decision,
        )
        from packages.core.services.task_chat_hitl import resolve_task_hitl
        task = task_approval_task
        if task is None:
            raise HTTPException(409, "task approval card is stale")
        try:
            await apply_task_approval_decision(
                db,
                task=task,
                user_id=user.id,
                actor=_user_display_name(user),
                choice=req.choice,
                note=req.note,
            )
        except TaskApprovalDecisionError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await resolve_task_hitl(
            db,
            task=task,
            kind=PendingActionKind.TASK_APPROVAL,
            choice=req.choice,
            user_id=user.id,
            note=req.note,
        )
        task_runtime_update_id = task.id
        task_runtime_update_plan_id = pa.get("plan_id")

    elif kind == PendingActionKind.TASK_RECOVERY and pa.get("task_id"):
        from packages.core.constants.task import TaskRecoveryChoice, TaskStatus
        from packages.core.services.task_chat_hitl import resolve_task_hitl
        from packages.core.services.task_retry_service import (
            TaskRetryError,
            prepare_task_retry,
        )
        from packages.core.services.task_service import update_task

        task = task_recovery_task
        if task is None:
            raise HTTPException(409, "task recovery card is stale")
        recovery_choices = {member.value for member in TaskRecoveryChoice}
        if normalized_choice not in recovery_choices:
            raise HTTPException(400, "choice must be retry or cancel")
        if normalized_choice == TaskRecoveryChoice.RETRY.value:
            try:
                task_retry_result = await prepare_task_retry(
                    db,
                    task=task,
                    user_id=user.id,
                    user_label=_user_display_name(user),
                    note=req.note,
                )
            except TaskRetryError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc
            task = task_retry_result.task
        else:
            cancelled = await update_task(
                db,
                task.id,
                task.entity_id,
                user_id=user.id,
                status=TaskStatus.CANCELLED.value,
            )
            if cancelled is None:
                raise HTTPException(404, "task not found")
        await resolve_task_hitl(
            db,
            task=task,
            kind=PendingActionKind.TASK_RECOVERY,
            choice=normalized_choice,
            user_id=user.id,
            note=req.note,
        )
        task_runtime_update_id = task.id
        task_runtime_update_plan_id = (
            task_retry_result.plan_id
            if task_retry_result is not None
            else pa.get("plan_id")
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
        approved_item_ids: list[str] | None = None
        rejected_ids: list[str] = []
        rejected_item_ids: list[str] = []

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
            approved_item_ids = list(all_item_ids)
        elif choice in {APPROVAL_CHOICE_APPROVE, "approve_all"}:
            # ProposalCard historically submits ``approve_all`` while the
            # shared approval schema uses ``approve``.  Accept both so the
            # message cannot be resolved without moving its tickets.
            approved_ids = await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
                actor_id=user.id,
            )
            approved_item_ids = list(all_item_ids)
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
            approved_item_ids = list(selected_item_ids)
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
            resolution_payload["approved_item_ids"] = approved_item_ids or []
            resolution_payload["rejected_task_ids"] = rejected_ids
            resolution_payload["rejected_item_ids"] = rejected_item_ids
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
        always = external_decision_intent is HumanDecisionIntent.APPROVE_STANDING
        if external_approval_requested:
            from packages.core.services.channel_outbound_delivery import (
                ApprovedExternalReplyDeliveryDisposition,
                ApprovedExternalReplyDeliveryError,
                ApprovedExternalReplyOutcomeUnknownError,
                ApprovedExternalReplySameKeyRetryRequired,
                approved_external_reply_delivery_disposition,
                approved_external_reply_outcome_message,
                claim_external_reply_approval,
                deliver_approved_external_reply,
                external_reply_approval_claim_owner_conflict_reason,
                mark_external_reply_approval_same_key_retry_required,
                release_external_reply_approval_claim,
            )

            claim = await claim_external_reply_approval(
                db,
                message_id=msg.id,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                user_id=user.id,
            )
            if not claim["acquired"]:
                detail = (
                    "External reply delivery outcome is unknown and requires "
                    "manual reconciliation; automatic retry is disabled."
                    if claim["reason"] == "approval_delivery_outcome_unknown"
                    else "External reply delivery is already in progress or resolved."
                )
                raise HTTPException(
                    409,
                    detail,
                )
            claim_id = claim["claim_id"]
            assert claim_id is not None
            claimed_pending_action = claim.get("pending_action")
            if not isinstance(claimed_pending_action, dict):
                await release_external_reply_approval_claim(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                raise HTTPException(
                    503,
                    "External reply approval payload could not be frozen; "
                    "the approval is still open.",
                )
            pa = claimed_pending_action
            if always and not str(pa.get("channel_config_id") or "").strip():
                await release_external_reply_approval_claim(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                raise HTTPException(
                    409,
                    "External reply account scope is missing; reconnect the "
                    "channel before creating a standing grant.",
                )
            try:
                result = await deliver_approved_external_reply(
                    db,
                    entity_id=user.entity_id,
                    channel_config_id=str(pa.get("channel_config_id") or ""),
                    channel_type=str(pa.get("channel_type") or ""),
                    channel_conversation_id=str(
                        pa.get("channel_conversation_id") or ""
                    ),
                    chat_id=str(pa.get("chat_id") or pa.get("sender_id") or ""),
                    text=str(pa.get("reply_text") or ""),
                    channel_binding_id=pa.get("channel_binding_id"),
                    channel_contact_id=pa.get("channel_contact_id"),
                    agent_id=pa.get("agent_id"),
                    agent_subscription_id=pa.get("agent_subscription_id"),
                    route_snapshot=pa.get("route_snapshot"),
                    workspace_id=pa.get("workspace_id"),
                    thread_ts=pa.get("thread_ts"),
                    idempotency_key=msg.id,
                    approval_claim_id=claim_id,
                    retry_mode=claim.get("retry_mode"),
                )
            except SoftTimeLimitExceeded as exc:
                await mark_external_reply_approval_same_key_retry_required(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                    error="external reply delivery timed out",
                )
                raise HTTPException(
                    503,
                    "The channel provider timed out before confirming the reply. "
                    "Retrying will reuse the same delivery key.",
                ) from exc
            except ApprovedExternalReplyOutcomeUnknownError as exc:
                raise HTTPException(
                    409,
                    "External reply delivery outcome is unknown and requires "
                    "manual reconciliation; automatic retry is disabled.",
                ) from exc
            except ApprovedExternalReplySameKeyRetryRequired as exc:
                await mark_external_reply_approval_same_key_retry_required(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                    error=str(exc),
                )
                raise HTTPException(
                    503,
                    "The channel provider did not confirm the reply. "
                    "Retrying will reuse the same delivery key.",
                ) from exc
            except ApprovedExternalReplyDeliveryError as exc:
                await release_external_reply_approval_claim(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                if exc.reason_code == "whatsapp_template_required":
                    raise HTTPException(
                        409,
                        "The 24-hour WhatsApp customer-service window has closed. "
                        "Send an approved WhatsApp template instead; the approval "
                        "remains open.",
                    ) from exc
                raise HTTPException(
                    503,
                    "The channel provider did not accept the reply. "
                    "The approval is still open; please retry.",
                ) from exc
            delivery_disposition = approved_external_reply_delivery_disposition(result)
            if (
                delivery_disposition
                is ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
            ):
                await release_external_reply_approval_claim(
                    db,
                    message_id=message_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                raise HTTPException(
                    503,
                    "The external reply was not sent. The approval is still open; "
                    "please retry.",
                )
            if (
                delivery_disposition
                is ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN
            ):
                resolution.update({
                    "delivery_outcome": "unknown",
                    "message_log_id": result.get("message_log_id"),
                    "delivery_error": result.get("error"),
                })
            # Provider acceptance (or a durable at-least-once quarantine) is
            # now established. Re-lock the exact scoped Message before the
            # decision and any standing grant become visible.
            msg = (await db.execute(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Conversation.entity_id == user.entity_id,
                    Conversation.workspace_id == workspace_id,
                )
                .with_for_update(of=Message)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if msg is None:
                await db.rollback()
                raise HTTPException(404, "message not found")
            if msg.resolved_at is not None:
                await db.rollback()
                msg = await db.get(Message, message_id)
                if msg is None:
                    raise HTTPException(404, "message not found")
                return _to_message(
                    msg,
                    resolved_by_user=(
                        user if msg.resolved_by_user_id == user.id else None
                    ),
                )
            claim_conflict = external_reply_approval_claim_owner_conflict_reason(
                msg,
                claim_id,
            )
            if claim_conflict is not None:
                await db.rollback()
                raise HTTPException(
                    409,
                    "External reply approval ownership changed; this result "
                    "was not applied.",
                )
            if always:
                from packages.core.governance import add_auto_approve_action

                await add_auto_approve_action(
                    db,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    action_key=ExternalMessageActionKey.SEND.value,
                    resource_id=str(pa["channel_config_id"]),
                    changed_by=user.id,
                )
            resolved = await chat_service.resolve_pending_action(
                db,
                message_id=message_id,
                user_id=user.id,
                resolution=resolution,
            )
            if resolved is None:
                await db.rollback()
                raise HTTPException(404, "message not found")
            body = approved_external_reply_outcome_message(result)
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
        elif external_decision_intent in {
            HumanDecisionIntent.DENY,
            HumanDecisionIntent.CANCEL,
        }:
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
    step_decision_expected = (
        kind in LEASE_HITL_CLOSEABLE_KINDS
        or kind == PendingActionKind.GOVERNANCE_APPROVAL
    ) and pa.get("step_id")
    if step_decision_expected and plan_to_run_after_commit is None:
        raise HTTPException(409, "execution step is no longer waiting for this decision")

    if (
        lease_request is not None
        and _lease_decision is not None
        and kind in LEASE_HITL_CLOSEABLE_KINDS
    ):
        from packages.core.governance.approvals import (
            consume_approval,
            deny_approval,
            grant_approval,
        )

        if _lease_decision == "grant":
            await grant_approval(
                db, lease_request, by_user_id=user.id, via="chat_card",
            )
            # Spend it immediately: the user's answer IS the consumption.
            # Nothing downstream consumes a path-C grant, and
            # _find_open_request counts granted-unconsumed rows as still
            # live — so without this the row never leaves that state.
            await consume_approval(db, lease_request)
        else:
            await deny_approval(
                db, lease_request, by_user_id=user.id, via="chat_card",
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
    if plan_to_run_after_commit:
        try:
            from packages.core.tasks.ai_tasks import run_plan

            run_plan.delay(plan_to_run_after_commit)
        except Exception:
            logger.warning(
                "Plan continuation dispatch failed after chat resolution: plan=%s",
                plan_to_run_after_commit,
                exc_info=True,
            )
            try:
                recovery_marked = await _mark_plan_continuation_dispatch_failed(
                    db,
                    plan_id=plan_to_run_after_commit,
                    user_id=user.id,
                )
                if recovery_marked:
                    try:
                        async with db.begin_nested():
                            db.add(Message(
                                conversation_id=conv.id,
                                role="system",
                                content=(
                                    "Your decision was saved, but execution could not resume. "
                                    "Retry the task when the worker queue is available."
                                ),
                                author_kind="system",
                                message_kind="system",
                                refs=[
                                    {"type": "message", "id": msg.id},
                                    {"type": "plan", "id": plan_to_run_after_commit},
                                ],
                            ))
                            await db.flush()
                    except Exception:
                        logger.error(
                            "Could not project Plan continuation dispatch failure into Chat: plan=%s",
                            plan_to_run_after_commit,
                            exc_info=True,
                        )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.error(
                    "Could not persist Plan continuation dispatch failure: plan=%s",
                    plan_to_run_after_commit,
                    exc_info=True,
                )
    if task_retry_result is not None:
        try:
            from packages.core.services.task_retry_service import (
                dispatch_task_retry,
                mark_task_retry_dispatch_failed,
            )

            dispatch_task_retry(task_retry_result)
        except Exception as exc:
            logger.warning(
                "Task retry dispatch failed after chat resolution: task=%s",
                task_retry_result.task.id,
                exc_info=True,
            )
            try:
                await mark_task_retry_dispatch_failed(
                    db,
                    result=task_retry_result,
                    error=exc,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.error(
                    "Could not persist Task retry dispatch failure: task=%s",
                    task_retry_result.task.id,
                    exc_info=True,
                )
    runtime_task_id = task_runtime_update_id
    runtime_plan_id = task_runtime_update_plan_id
    if runtime_task_id is None and plan_to_run_after_commit:
        runtime_task_id = pa.get("task_id")
        runtime_plan_id = plan_to_run_after_commit
    if runtime_task_id:
        from packages.core.services.realtime import broadcast_task_runtime_update

        await broadcast_task_runtime_update(
            user.entity_id,
            task_id=str(runtime_task_id),
            workspace_id=workspace_id,
            plan_id=runtime_plan_id,
            event="workspace_chat_action_resolved",
        )
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


@router.post(
    "/messages/{message_id}/feedback",
    response_model=CompletionFeedbackResponse,
)
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
    try:
        rating = ChatFeedbackRating(rating)
    except ValueError:
        raise HTTPException(400, "rating must be 'up' or 'down'")

    scoped_statement = (
        select(Message, Conversation)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.workspace_id == workspace_id,
            Conversation.entity_id == user.entity_id,
        )
    )
    scoped_message = (await db.execute(scoped_statement)).one_or_none()
    if scoped_message is None:
        raise HTTPException(404, "message not found")
    msg, conv = scoped_message

    target_kind = classify_chat_feedback_target_kind(msg)
    target_decision = ChatFeedbackTargetPolicyFactory.create(
        target_kind
    ).evaluate(msg, workspace_scoped=True)
    if not target_decision.eligible:
        raise HTTPException(422, target_decision.detail)

    preliminary_subject = await _resolve_completion_feedback_subject(
        db,
        message=msg,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        lock_rows=False,
    )
    if preliminary_subject is None:
        raise HTTPException(
            422,
            "Completion message lineage is invalid for this Workspace",
        )
    # Use one lock order everywhere: subject -> Message -> Plan/Task. Task
    # deletion takes the same subject lock before it locks and removes Task.
    await lock_completion_feedback_subject(
        db,
        task_id=preliminary_subject.task_id,
        plan_id=preliminary_subject.plan_id,
    )
    scoped_message = (
        await db.execute(
            scoped_statement
            .with_for_update(of=Message)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if scoped_message is None:
        raise HTTPException(404, "message not found")
    msg, conv = scoped_message
    target_kind = classify_chat_feedback_target_kind(msg)
    target_decision = ChatFeedbackTargetPolicyFactory.create(
        target_kind
    ).evaluate(msg, workspace_scoped=True)
    if not target_decision.eligible:
        raise HTTPException(422, target_decision.detail)
    subject = await _resolve_completion_feedback_subject(
        db,
        message=msg,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    if subject is None:
        raise HTTPException(
            422,
            "Completion message lineage is invalid for this Workspace",
        )
    if subject != preliminary_subject:
        raise HTTPException(
            409,
            "Completion message lineage changed; retry feedback",
        )

    content_preview = build_chat_feedback_content_preview(
        requested_preview=None,
        message_content=msg.content,
        assistant_blocks=(msg.meta or {}).get("assistant_blocks")
        if isinstance(msg.meta, dict)
        else None,
    )
    # Completion receipts are autonomous runtime projections, not replies to
    # the nearest chat turn. Persisting that turn would misattribute another
    # member's request to this reviewer signal.
    request_preview = None
    try:
        result = await persist_chat_message_feedback(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            conversation_id=conv.id,
            message_id=message_id,
            rating=rating,
            content_preview=content_preview,
            request_preview=request_preview,
            target_kind=subject.target_kind,
            target_id=subject.target_id,
            task_id=subject.task_id,
            plan_id=subject.plan_id,
            commit=False,
        )
    except ChatFeedbackTargetDeletedError as exc:
        await db.rollback()
        raise HTTPException(404, "message not found") from exc
    except IntegrityError as exc:
        await db.rollback()
        if (
            classify_chat_feedback_integrity_error(exc)
            == ChatFeedbackIntegrityErrorKind.TARGET_DELETED
        ):
            raise HTTPException(404, "message not found") from exc
        raise

    queued_learning_ids = await _record_completion_feedback_evidence(
        db,
        workspace_id=workspace_id,
        user=user,
        conversation_id=conv.id,
        message=msg,
        rating=rating,
        subject=subject,
    )

    await db.commit()
    await _enqueue_learning_candidate_applies(
        db,
        user=user,
        workspace_id=workspace_id,
        candidate_ids=queued_learning_ids,
    )
    message_response = _to_message(msg)
    return CompletionFeedbackResponse(
        **message_response.model_dump(),
        rating=result.rating,
        mutation_sequence=result.mutation_sequence,
        mutation_status=result.mutation_status,
        feedback_updated_at=result.updated_at,
        feedback_target_kind=subject.target_kind,
        feedback_target_id=subject.target_id,
        feedback_task_id=subject.task_id,
        feedback_plan_id=subject.plan_id,
    )


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
    """Pure step-resume mutation. Logic lives in
    ``packages.core.services.step_resume`` so non-HTTP callers (the
    ``answer_task_blocker`` chat tool) share the exact same transitions;
    this alias keeps the router's public (test-imported) name stable."""
    apply_step_resume(
        step,
        params_update=params_update,
        human_input_response=human_input_response,
    )


def _apply_step_cancel(
    step: Any,
    reason: str,
    *,
    error_type: str = "UserSkipped",
    human_decision: Optional[dict] = None,
) -> None:
    """Pure step-cancel mutation — see ``_apply_step_resume`` docstring."""
    apply_step_cancel(
        step,
        reason,
        error_type=error_type,
        human_decision=human_decision,
    )


async def _resume_step_for_retry(
    db: AsyncSession,
    user: User,
    *,
    step_id: str,
    plan_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    params_update: Optional[dict] = None,
    human_input_response: Optional[dict] = None,
    enqueue: bool = True,
) -> Optional[str]:
    """Reset a waiting_human step back to pending. Delegates to
    ``packages.core.services.step_resume``. Caller commits."""
    return await resume_step_for_retry(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        task_id=task_id,
        params_update=params_update,
        human_input_response=human_input_response,
        enqueue=enqueue,
    )


async def _mark_plan_continuation_dispatch_failed(
    db: AsyncSession,
    *,
    plan_id: str,
    user_id: str,
) -> bool:
    """Make a post-commit queue failure visible and manually recoverable."""

    from packages.core.services.task_retry_service import (
        mark_plan_continuation_dispatch_failed,
    )

    return await mark_plan_continuation_dispatch_failed(
        db,
        plan_id=plan_id,
        user_id=user_id,
        reason="chat_resolution_dispatch_failed",
    )


async def _cancel_step(
    db: AsyncSession,
    user: User,  # noqa: ARG001 — accepted for parity with _resume_step_for_retry
    *,
    step_id: str,
    plan_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    reason: str = "user skipped",
    error_type: str = "UserSkipped",
    human_decision: Optional[dict] = None,
    enqueue: bool = True,
) -> Optional[str]:
    """Fail a waiting step after 'skip'/'cancel'. Delegates to
    ``packages.core.services.step_resume``. Caller commits."""
    return await cancel_step(
        db,
        entity_id=user.entity_id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=workspace_id,
        task_id=task_id,
        reason=reason,
        error_type=error_type,
        human_decision=human_decision,
        enqueue=enqueue,
    )
