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
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
)
from packages.core.services.workspace_access import user_can_read_workspace
from packages.core.workspace_chat import service as chat_service


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/chat",
    tags=["workspace-chat"],
)


# ── Schemas ────────────────────────────────────────────────────────────

class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    created_at: datetime
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
) -> MessageResponse:
    pending_action = m.pending_action if isinstance(m.pending_action, dict) and m.pending_action.get("kind") else None
    author_user_id = _message_author_user_id(m)
    return MessageResponse(
        id=m.id,
        conversation_id=m.conversation_id,
        created_at=m.created_at,
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


def _pending_action_evidence_type(kind: str, choice: str) -> str:
    if kind == "approve_proposals":
        return "user_feedback" if choice == "feedback" else "proposal_decision"
    if kind in {"human_input", "needs_input", "needs_confirmation", "needs_login"}:
        return "hitl_resolution"
    if kind == "workspace_operation_review":
        return "workspace_operation_decision"
    if kind == "external_message_approval":
        return "external_message_decision"
    if kind == "retry_strategist_review":
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

    if kind == "approve_proposals":
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
    elif kind == "workspace_operation_review":
        base = "Workspace operation reviewed"
    elif kind == "external_message_approval":
        base = "External message approved" if approved else (
            "External message rejected" if rejected else "External message reviewed"
        )
    elif kind in {"human_input", "needs_input", "needs_confirmation", "needs_login"}:
        base = "Input request answered"
    elif kind == "retry_strategist_review":
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
    if kind == "external_message_approval":
        return "external_message.approved" if approved else (
            "external_message.rejected" if rejected else "external_message.resolved"
        )
    if kind == "approve_proposals":
        if approved or normalized in {"approve_all", "approve_selected"}:
            return "strategist_proposal.approved"
        if rejected or normalized in {"reject_all"}:
            return "strategist_proposal.rejected"
        if normalized == "feedback":
            return "strategist_proposal.feedback"
    if kind == "workspace_operation_review":
        return "workspace_operation.resolved"
    if kind in {"human_input", "needs_input", "needs_confirmation", "needs_login"}:
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
        if kind == "external_message_approval":
            verb = "approved" if event_type.endswith(".approved") else (
                "rejected" if event_type.endswith(".rejected") else "resolved"
            )
            summary = f"External message {verb} by workspace operator."
        elif kind == "approve_proposals":
            summary = _pending_action_summary(kind, choice, note)
        elif kind == "workspace_operation_review":
            summary = _pending_action_summary(kind, choice, note)
        elif kind in {"human_input", "needs_input", "needs_confirmation", "needs_login"}:
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
        if kind == "external_message_approval":
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
    plan_task_ids = await _plan_task_ids_for_messages(
        db,
        rows,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    conversation_task_ids = await _conversation_task_ids_for_messages(
        db,
        rows,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    task_ref_details = await _task_ref_details_for_messages(
        db,
        rows,
        entity_id=user.entity_id,
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
        )
        for m in rows
    ]


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
    proposal_always_approve = (
        kind == "approve_proposals"
        and normalized_choice == APPROVAL_CHOICE_ALWAYS_APPROVE
    )
    if proposal_always_approve and not resolution.get("note"):
        resolution["note"] = "Future workspace proposals in this workspace will start automatically."
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

    if kind == "human_input" and pa.get("step_id"):
        # Lease-level HITL (legacy free-form text input): stash the
        # response on the step row + flip back to pending.
        await _resume_step_for_retry(
            db, user,
            step_id=pa["step_id"],
            plan_id=pa.get("plan_id"),
            human_input_response=(req.payload or {"choice": req.choice, "note": req.note}),
        )

    elif kind == "needs_input" and pa.get("step_id"):
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
        else:
            # skip / cancel — fail the step so the plan can move on.
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user skipped needs_input",
            )

    elif kind == "needs_confirmation" and pa.get("step_id"):
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
        else:
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user cancelled needs_confirmation",
            )

    elif kind == "needs_login" and pa.get("step_id"):
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
        elif choice == "sign_in":
            # No backend state change — frontend orchestrates the
            # headed-login flow. Step stays waiting_human; user calls
            # back with choice="continue_after_login" once cookies
            # are captured.
            pass
        else:
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user skipped needs_login",
            )

    elif kind == "governance_approval" and pa.get("step_id"):
        choice = (req.choice or "").lower()
        _always = choice == APPROVAL_CHOICE_ALWAYS_APPROVE
        if choice == APPROVAL_CHOICE_APPROVE or _always:
            if _always and (pa.get("action") or pa.get("capability_id")):
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
                params_update={
                    "_governance_approval": {
                        "status": "approved",
                        "step_id": pa.get("step_id"),
                        "action_key": pa.get("action"),
                        "capability_id": pa.get("capability_id"),
                        "matched_rule": pa.get("matched_rule"),
                        "approved_by": user.id,
                        "approved_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
        else:
            await _cancel_step(
                db, user,
                step_id=pa["step_id"],
                plan_id=pa.get("plan_id"),
                reason="user rejected governance approval",
            )

    elif kind == "approve_proposals" and pa.get("review_id"):
        # Strategist proposal card: approve, approve_selected, reject, or feedback.
        from packages.core.strategist import approve_proposal, reject_proposal
        from packages.core.strategist.service import set_proposal_auto_approval
        review_id = pa["review_id"]
        all_ids = pa.get("task_ids") or []
        choice = normalized_choice
        payload = req.payload or {}

        if choice == APPROVAL_CHOICE_ALWAYS_APPROVE:
            await set_proposal_auto_approval(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                enabled=True,
                changed_by=user.id,
            )
            await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
            )
        elif choice == APPROVAL_CHOICE_APPROVE:
            await approve_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
            )
        elif choice == "approve_selected":
            # Approve only selected tasks, reject the rest.
            selected_ids = payload.get("selected_task_ids") or all_ids
            approved_ids: list[str] = []
            if selected_ids:
                approved_ids = await approve_proposal(
                    db, entity_id=user.entity_id,
                    review_id=review_id, only_task_ids=selected_ids,
                )
            approved_set = set(approved_ids)
            rejected_ids = [t for t in all_ids if t not in approved_set]
            if rejected_ids:
                await reject_proposal(
                    db, entity_id=user.entity_id,
                    review_id=review_id, only_task_ids=rejected_ids,
                    reason="Not selected by user",
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
            )
            ws_id = msg.conversation_id and (await db.execute(
                select(Conversation.workspace_id).where(Conversation.id == msg.conversation_id)
            )).scalar_one_or_none()
            if ws_id:
                try:
                    from packages.core.tasks.ai_tasks import run_strategist_review
                    run_strategist_review.apply_async(
                        args=[ws_id, f"user_feedback: {feedback_text}"],
                        countdown=3,
                    )
                except Exception:
                    pass
        elif choice in {"reject", "reject_all", "decline", "no"}:
            await reject_proposal(
                db, entity_id=user.entity_id,
                review_id=review_id, only_task_ids=all_ids or None,
                reason=req.note,
            )

    elif kind == "retry_strategist_review":
        choice = (req.choice or "").lower()
        if choice in {"retry", "retry_now", "approve", "yes"}:
            try:
                from packages.core.tasks.ai_tasks import run_strategist_review
                original_trigger = pa.get("trigger") or "failed"
                run_strategist_review.apply_async(
                    args=[workspace_id, f"manual_retry_after_failure: {original_trigger}"],
                    countdown=1,
                )
            except Exception as exc:
                raise HTTPException(500, f"failed to enqueue strategist retry: {exc}") from exc

    elif kind == "workspace_operation_review":
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

    elif kind == "external_message_approval":
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
    """Return True for intentional multi-stage pending_action callbacks."""
    kind = pending_action.get("kind") if isinstance(pending_action, dict) else None
    normalized = (choice or "").lower()
    return kind == "needs_login" and normalized == "continue_after_login"


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

    step.step_status = "pending"
    step.human_input_prompt = None
    step.current_lease_id = None
    step.error = None
    step.finished_at = None


def _apply_step_cancel(step: Any, reason: str) -> None:
    """Mutate a step row to fail it after a 'skip' / 'cancel'
    resolution. Pure — caller handles DB load + re-enqueue."""
    from datetime import datetime, timezone
    step.step_status = "failed"
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

    target_plan_id = plan_id or step.plan_id
    if not target_plan_id:
        return

    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == target_plan_id)
    )).scalar_one_or_none()
    if plan:
        plan.status = "running"
        plan.completed_at = None
        plan.last_error = None
        if plan.task_id:
            task = (await db.execute(
                select(Task).where(
                    Task.id == plan.task_id,
                    Task.entity_id == user.entity_id,
                )
            )).scalar_one_or_none()
            if task and task.status == "waiting_on_customer":
                apply_task_status_transition(task, "in_progress")

    if human_input_response is not None and step.kind == "human":
        try:
            from packages.core.temporal_app import signal_human_input
            await signal_human_input(step.plan_id, step.step_key, human_input_response)
        except Exception:
            pass

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
