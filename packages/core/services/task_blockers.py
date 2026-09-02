"""Task blockers — lease-origin HITL requests, answerable from chat.

A "task blocker" is a pending ``HitlRequest`` minted by
``lease_needs_human`` (lease-origin rows) when a running step hit
something only a human can supply: free-form input, form answers, a
destructive-action confirmation, or a login wall. The card the notifier
posts into chat can be resolved by clicking; this module gives the chat
*agent* the same power, so a user who answers the blocker in natural
language gets routed back to the original task instead of a fresh
delegation that lacks the task's tools.

Resolution semantics mirror ``resolve_chat_action``'s path-C branches
exactly (apps/api/routers/workspace_chat.py) — same grant+consume
lifecycle, same step mutations via ``services.step_resume`` — so the two
entry points cannot drift.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS
from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.models.hitl_request import HitlRequest

logger = logging.getLogger(__name__)

# Kinds this module can answer, and how. ``needs_login`` is deliberately
# NOT answerable here: signing in requires the card's headed-login flow
# (cookies must be captured by the frontend before the step can retry),
# and auto-granting it would mark "approved" something nobody performed.
KIND_HUMAN_INPUT = "human_input"
KIND_NEEDS_INPUT = "needs_input"
KIND_NEEDS_CONFIRMATION = "needs_confirmation"
KIND_NEEDS_LOGIN = "needs_login"

ANSWERABLE_BLOCKER_KINDS: frozenset[str] = LEASE_HITL_CLOSEABLE_KINDS - {KIND_NEEDS_LOGIN}


def _blocker_pending_kind(request: HitlRequest) -> str:
    context = request.context if isinstance(request.context, dict) else {}
    return str(context.get("pending_kind") or KIND_HUMAN_INPUT)


async def list_open_task_blockers(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Open lease-origin HITL requests for a workspace, oldest first.

    Shaped for prompt injection: each row carries what the model needs to
    recognise "the user is answering this" and to call
    ``answer_task_blocker`` — nothing more.
    """
    from packages.core.models.task import Task

    rows = list((await db.execute(
        select(HitlRequest, Task.title)
        .outerjoin(Task, Task.id == HitlRequest.origin_task_id)
        .where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.workspace_id == workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING.value,
            HitlRequest.origin_kind == ApprovalOriginKind.LEASE.value,
        )
        .order_by(HitlRequest.created_at.asc())
        .limit(max(1, min(limit, 25)))
    )).all())

    blockers: list[dict[str, Any]] = []
    for request, task_title in rows:
        pending_kind = _blocker_pending_kind(request)
        if pending_kind not in LEASE_HITL_CLOSEABLE_KINDS:
            continue
        blockers.append({
            "request_id": request.id,
            "task_id": request.origin_task_id,
            "task_title": task_title,
            "pending_kind": pending_kind,
            "hitl_type": request.hitl_type,
            "reason": (request.reason or "")[:500],
            "answerable_via_tool": pending_kind in ANSWERABLE_BLOCKER_KINDS,
            "created_at": (
                request.created_at.isoformat() if request.created_at else None
            ),
        })
    return blockers


async def answer_task_blocker(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    user_display: Optional[str] = None,
    request_id: str,
    answer: Optional[str] = None,
    answers: Optional[dict] = None,
    confirm: Optional[bool] = None,
    refuse: bool = False,
) -> dict[str, Any]:
    """Answer (or refuse) one open task blocker and resume the task.

    Three things happen in one transaction, mirroring the chat card's
    resolve endpoint: (1) the ``HitlRequest`` is decided — grant+consume
    for an answer (the user's answer IS the consumption; nothing
    downstream consumes a lease grant, and granted-unconsumed rows still
    count as live for dedup), deny for a refusal; (2) the waiting step is
    mutated per its pending kind; (3) the plan is revived and returned to the
    caller for post-commit dispatch.

    Caller commits.
    """
    from packages.core.governance.approvals import (
        consume_approval,
        deny_approval,
        grant_approval,
    )
    from packages.core.services.step_resume import cancel_step, resume_step_for_retry

    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == request_id,
            HitlRequest.entity_id == entity_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if request is None:
        return {"resolved": False, "reason": "request_not_found"}
    if request.status != ApprovalStatus.PENDING.value:
        # Idempotent: a second answer to an already-decided blocker reports
        # the real state instead of failing or double-spending.
        return {
            "resolved": False,
            "reason": f"already_{request.status}",
            "request_id": request.id,
        }
    if request.origin_kind != ApprovalOriginKind.LEASE.value:
        return {
            "resolved": False,
            "reason": "not_a_task_blocker",
            "detail": (
                "This request was not minted by a paused task step. "
                "Governance approvals must be decided on their card."
            ),
        }

    pending_kind = _blocker_pending_kind(request)
    if pending_kind not in LEASE_HITL_CLOSEABLE_KINDS:
        return {"resolved": False, "reason": f"unsupported_kind:{pending_kind}"}

    step_id = request.origin_step_id
    plan_id = request.origin_plan_id
    if not step_id:
        return {"resolved": False, "reason": "request_missing_step"}

    display = user_display or user_id

    if refuse:
        plan_to_run = await cancel_step(
            db,
            entity_id=entity_id,
            step_id=step_id,
            plan_id=plan_id,
            workspace_id=request.workspace_id,
            task_id=request.origin_task_id,
            reason=f"user declined via chat: {(answer or 'declined')[:200]}",
            enqueue=False,
        )
        if plan_to_run is None:
            return {"resolved": False, "reason": "origin_no_longer_waiting"}
        await deny_approval(db, request, by_user_id=user_id, via="chat_bridge")
        await _mark_blocker_card_resolved(
            db, request=request, user_id=user_id, choice="skip", note=answer,
        )
        return {
            "resolved": True,
            "decision": "refused",
            "request_id": request.id,
            "task_id": request.origin_task_id,
            "_plan_to_run": plan_to_run,
        }

    if pending_kind == KIND_NEEDS_LOGIN:
        # Answering text cannot complete a login. Leave the request PENDING
        # (nothing has been approved yet) and tell the model what the human
        # actually has to do.
        return {
            "resolved": False,
            "reason": "needs_login_requires_card_flow",
            "detail": (
                "This blocker is a login wall. The user must click 'Sign in' "
                "on the blocker card so the browser can capture a session; "
                "text answers cannot complete it."
            ),
        }

    resume_kwargs: dict[str, Any] = {}
    if pending_kind == KIND_HUMAN_INPUT:
        if not (answer and answer.strip()):
            return {"resolved": False, "reason": "answer_required"}
        resume_kwargs["human_input_response"] = {
            "choice": "respond",
            "note": answer.strip(),
            "user": display,
            "via": "chat_bridge",
        }
    elif pending_kind == KIND_NEEDS_INPUT:
        merged_answers = dict(answers or {})
        if not merged_answers and answer and answer.strip():
            merged_answers = {"response": answer.strip()}
        if not merged_answers:
            return {"resolved": False, "reason": "answers_required"}
        resume_kwargs["params_update"] = {"answers": merged_answers}
    elif pending_kind == KIND_NEEDS_CONFIRMATION:
        if confirm is not True:
            return {
                "resolved": False,
                "reason": "confirmation_required",
                "detail": (
                    "Pass confirm=true only after the user has explicitly "
                    "confirmed the destructive action, or refuse=true to "
                    "cancel it."
                ),
            }
        # Set both legacy and current confirmation flags — extras are
        # ignored by tools that don't recognize them.
        resume_kwargs["params_update"] = {"confirm": True, "confirm_destructive": True}

    plan_to_run = await resume_step_for_retry(
        db,
        entity_id=entity_id,
        user_id=user_id,
        step_id=step_id,
        plan_id=plan_id,
        workspace_id=request.workspace_id,
        task_id=request.origin_task_id,
        enqueue=False,
        **resume_kwargs,
    )
    if plan_to_run is None:
        return {"resolved": False, "reason": "origin_no_longer_waiting"}
    await grant_approval(db, request, by_user_id=user_id, via="chat_bridge")
    # Spend it immediately: the user's answer IS the consumption (see the
    # chat card resolve endpoint for the full rationale).
    await consume_approval(db, request)
    await _mark_blocker_card_resolved(
        db, request=request, user_id=user_id, choice="respond", note=answer,
    )
    await _log_blocker_answer(
        db, request=request, display=display, answer=answer, answers=answers,
    )
    return {
        "resolved": True,
        "decision": "answered",
        "request_id": request.id,
        "task_id": request.origin_task_id,
        "pending_kind": pending_kind,
        "resumed_step_id": step_id,
        "_plan_to_run": plan_to_run,
    }


async def _mark_blocker_card_resolved(
    db: AsyncSession,
    *,
    request: HitlRequest,
    user_id: str,
    choice: str,
    note: Optional[str],
) -> None:
    """Resolve the chat card mirroring this request, if one exists.

    The notifier posts a Message whose ``pending_action`` carries
    ``approval_request_id``; leaving it unresolved would keep a live,
    clickable card for a decision that has already been made.
    Best-effort: a missing card must not fail the answer.
    """
    from packages.core.models.task import Conversation, Message

    try:
        rows = list((await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.entity_id == request.entity_id,
                Message.pending_action.isnot(None),
                Message.resolved_at.is_(None),
            )
            .order_by(Message.created_at.desc())
            .limit(50)
        )).scalars().all())
        card = next(
            (
                row for row in rows
                if isinstance(row.pending_action, dict)
                and row.pending_action.get("approval_request_id") == request.id
            ),
            None,
        )
        if card is None:
            return
        from packages.core.workspace_chat import service as workspace_chat_service

        await workspace_chat_service.resolve_pending_action(
            db,
            message_id=card.id,
            user_id=user_id,
            resolution={
                "choice": choice,
                "note": (note or "")[:1000],
                "via": "chat_bridge",
            },
            emit_followup=False,
        )
    except Exception:
        logger.warning(
            "blocker card resolution failed for request %s (ignored)",
            request.id, exc_info=True,
        )


async def _log_blocker_answer(
    db: AsyncSession,
    *,
    request: HitlRequest,
    display: str,
    answer: Optional[str],
    answers: Optional[dict],
) -> None:
    """Task-log the answer so the execution timeline shows who unblocked
    the task and with what. Best-effort."""
    if not request.origin_task_id:
        return
    try:
        from packages.core.constants.task import TaskLogType
        from packages.core.constants.task_actors import TaskActor
        from packages.core.services.task_service import add_task_log

        summary = answer or ", ".join(
            f"{key}={value}" for key, value in (answers or {}).items()
        ) or "(confirmed)"
        await add_task_log(
            db,
            request.origin_task_id,
            TaskLogType.AI_HITL_RESUMED,
            f"Blocker answered via chat: {summary[:400]}",
            actor=TaskActor.USER,
            created_by=display,
        )
    except Exception:
        logger.warning(
            "blocker task log failed for request %s (ignored)",
            request.id, exc_info=True,
        )
