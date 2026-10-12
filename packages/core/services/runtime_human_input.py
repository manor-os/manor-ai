"""Park a durable chat run on a ``request_input`` question; resume on the answer.

Mirrors the sandbox-reservation suspension one to one:

* the tool returns a ``waiting_human`` suspension → the loop checkpoints;
* :func:`mint_runtime_human_input_request` writes the question as an open
  ``HitlRequest`` (``hitl_type=input``) and the run is persisted
  ``waiting_resource`` / ``awaiting_human_input`` pointing at it;
* :func:`answer_runtime_human_input` grants + consumes the request, writes
  the answer into the checkpoint as the pending tool call's result and queues
  ``runtime.resume_run`` through the outbox;
* the resumed loop appends that result and continues — the question is never
  re-asked and the transcript is never replayed from scratch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.control import (
    HUMAN_INPUT_ANSWERED_REASON,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeAgentCheckpoint,
    is_human_input_suspension_control,
)
from packages.core.ai.runtime.human_input import (
    HUMAN_INPUT_TIMEOUT_REASON,
    REQUEST_INPUT_DEADLINE_HOURS,
    human_input_answer_result,
)
from packages.core.constants.approvals import (
    ApprovalOriginKind,
    ApprovalStatus,
    HitlType,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
)

#: ``HitlRequest.action_key`` for a question asked by a chat run.
RUNTIME_INPUT_ACTION_KEY = "chat.request_input"
#: ``HitlRequest.decided_via`` for an answer submitted against the run.
RUNTIME_INPUT_DECIDED_VIA = "chat_input"


class RuntimeHumanInputError(ValueError):
    """The run is not parked on a question, or the answer cannot be applied."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _question_payload(control: dict[str, Any]) -> dict[str, Any]:
    payload = control.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _pending_call_id(control: dict[str, Any]) -> str:
    checkpoint = control.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return ""
    pending = checkpoint.get("pending_tool_call")
    return str(pending.get("id") or "") if isinstance(pending, dict) else ""


def _hitl_payload(question: dict[str, Any], *, conversation_id: str | None) -> dict[str, Any]:
    """The card copy every INPUT request must carry (question + why)."""
    justification = question.get("justification")
    justification = justification if isinstance(justification, dict) else {}
    payload: dict[str, Any] = {
        "question": str(question.get("question") or ""),
        "why": str(
            question.get("context")
            or justification.get("why_human_required")
            or "The assistant needs this to continue the current request."
        ),
        "action_to_take": "Reply in the chat; the assistant continues with your answer.",
    }
    for key in ("options", "fields", "context", "justification"):
        if question.get(key):
            payload[key] = question[key]
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


async def mint_runtime_human_input_request(
    db: AsyncSession,
    *,
    run: RuntimeRun,
    control: dict[str, Any],
) -> HitlRequest:
    """Write the parked question as an open ``HitlRequest`` for ``run``."""
    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        mint_approval_request,
    )

    if not is_human_input_suspension_control(control):
        raise ValueError("control is not a human-input suspension")
    question = _question_payload(control)
    if not str(question.get("question") or "").strip():
        raise ValueError("human-input suspension carries no question")
    tool_call_id = _pending_call_id(control)
    request = await mint_approval_request(
        db,
        subject=ApprovalSubject(
            entity_id=run.entity_id,
            workspace_id=run.workspace_id,
            action_key=RUNTIME_INPUT_ACTION_KEY,
            resource_kind="runtime_run",
            resource_id=run.id,
            risk_level="low",
        ),
        origin=ApprovalOrigin(
            kind=ApprovalOriginKind.TOOL_CALL.value,
            conversation_id=run.conversation_id,
            message_id=run.assistant_message_id,
            context={
                "pending_kind": PendingActionKind.NEEDS_INPUT.value,
                "runtime_run_id": run.id,
                "root_run_id": run.root_run_id,
                "tool_call_id": tool_call_id,
                "question": question,
                "deadline_at": control.get("deadline_at"),
            },
        ),
        dedup_key=f"runtime_input:{run.id}:{tool_call_id or 'pending'}",
        reason=str(question.get("question") or ""),
        matched_rule="runtime.request_input",
        hitl_type=HitlType.INPUT.value,
        payload=_hitl_payload(question, conversation_id=run.conversation_id),
    )
    return request


async def human_input_projection(
    db: AsyncSession,
    run: RuntimeRun,
) -> dict[str, Any] | None:
    """Public view of the question a run is parked on (no checkpoint leaks)."""
    if not run.human_input_request_id:
        return None
    request = await db.get(HitlRequest, run.human_input_request_id)
    if request is None:
        return None
    context = request.context if isinstance(request.context, dict) else {}
    question = context.get("question") if isinstance(context.get("question"), dict) else {}
    projection: dict[str, Any] = {
        "request_id": request.id,
        "status": request.status,
        "question": str(question.get("question") or request.reason or ""),
        "deadline_at": context.get("deadline_at"),
    }
    for key in ("options", "fields", "context", "justification"):
        if question.get(key):
            projection[key] = question[key]
    answer = context.get("answer")
    if isinstance(answer, dict):
        projection["answer"] = answer
    return projection


async def answer_runtime_human_input(
    db: AsyncSession,
    *,
    run_id: str,
    entity_id: str,
    user_id: str,
    answer: str,
    request_id: str | None = None,
    fields: dict[str, Any] | None = None,
    choice: str | None = None,
    answered_by: str | None = None,
    now: datetime | None = None,
) -> RuntimeRun:
    """Apply a human answer to a parked run and queue its resume.

    Idempotent per request: a second answer to an already-answered question
    returns the run untouched (the first answer is the one the model sees).

    Callers must have verified that ``user_id`` owns the run (the chat router
    does, via the run's entity + user); this function does not re-check
    entity membership.
    """
    from packages.core.governance.approvals import consume_approval, grant_approval

    current_time = now or datetime.now(timezone.utc)
    run = (
        await db.execute(
            select(RuntimeRun)
            .where(
                RuntimeRun.id == run_id,
                RuntimeRun.entity_id == entity_id,
                # The question was asked of the person who started the run;
                # only they may answer it (enforced here, not just in the
                # router, so every caller gets the same rule).
                RuntimeRun.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise RuntimeHumanInputError("run_not_found", "Chat run not found")
    if run.status in RuntimeRunStatus.terminal():
        raise RuntimeHumanInputError("run_finished", "This run has already finished")
    if not run.human_input_request_id:
        raise RuntimeHumanInputError("not_waiting", "This run is not waiting for an answer")
    if request_id is not None and request_id != run.human_input_request_id:
        raise RuntimeHumanInputError("question_changed", "This question is no longer current; refresh before answering")
    if run.status_reason == HUMAN_INPUT_ANSWERED_REASON:
        return run
    if run.status != RuntimeRunStatus.WAITING_RESOURCE.value:
        raise RuntimeHumanInputError("not_waiting", "This run is not waiting for an answer")

    answer_text = str(answer or "").strip()
    clean_fields = {
        str(key): value for key, value in (fields or {}).items() if str(key).strip()
    } if isinstance(fields, dict) else {}
    if not answer_text and not clean_fields and not str(choice or "").strip():
        raise RuntimeHumanInputError("answer_required", "An answer is required")

    request = (
        await db.execute(
            select(HitlRequest)
            .where(HitlRequest.id == run.human_input_request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if request is None:
        raise RuntimeHumanInputError("request_missing", "The question is no longer open")
    if request.status != ApprovalStatus.PENDING.value:
        raise RuntimeHumanInputError("request_closed", "The question is no longer open")

    try:
        checkpoint = RuntimeAgentCheckpoint.from_dict(dict(run.checkpoint or {}))
    except ValueError as exc:
        raise RuntimeHumanInputError("checkpoint_invalid", str(exc)) from exc

    resolution = {
        "answer": answer_text,
        "fields": clean_fields,
        "choice": str(choice or "").strip() or None,
        "answered_by": answered_by or user_id,
        "answered_at": current_time.isoformat(),
    }
    request.context = {**(request.context or {}), "answer": resolution}
    # The caller has already proven it owns the run (entity + user match on
    # the locked row above); the question was asked of exactly that person,
    # so membership re-checks add nothing here.
    await grant_approval(
        db, request, by_user_id=user_id, via=RUNTIME_INPUT_DECIDED_VIA,
        authority_prechecked=True,
    )
    await consume_approval(db, request)

    checkpoint.pending_tool_result = {
        "id": str(checkpoint.pending_tool_call.get("id") or ""),
        "content": human_input_answer_result(
            answer=answer_text,
            fields=clean_fields or None,
            choice=resolution["choice"],
            answered_by=resolution["answered_by"],
        ),
    }
    run.checkpoint = checkpoint.to_dict()
    run.status_reason = HUMAN_INPUT_ANSWERED_REASON
    run.version += 1
    db.add(
        RuntimeOutboxEvent(
            event_type="runtime.resume_run",
            aggregate_id=run.id,
            dedupe_key=f"runtime.resume_run:human_input:{request.id}",
            payload={"run_id": run.id, "reservation_id": None, "reservation_version": None},
            available_at=current_time,
            created_at=current_time,
        )
    )
    return run


async def close_runtime_human_input_request(
    db: AsyncSession,
    *,
    run: RuntimeRun,
    user_id: str | None,
    reason: str,
) -> None:
    """Deny the open question when its run is cancelled or fails."""
    from packages.core.governance.approvals import deny_approval

    if not run.human_input_request_id:
        return
    request = await db.get(HitlRequest, run.human_input_request_id)
    if request is None or request.status != ApprovalStatus.PENDING.value:
        return
    await deny_approval(
        db, request, by_user_id=user_id, via=RUNTIME_INPUT_DECIDED_VIA, reason=reason,
        authority_prechecked=True,
    )


def _request_deadline(request: HitlRequest) -> datetime | None:
    context = request.context if isinstance(request.context, dict) else {}
    raw = context.get("deadline_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    created = request.created_at
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + timedelta(hours=REQUEST_INPUT_DEADLINE_HOURS)


async def expire_runtime_human_input_requests(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[str]:
    """Fail runs whose question went unanswered past its deadline.

    Returns the ids of the runs that were failed. The terminal transition
    finalizes each run's assistant Message and stages its client notice; the
    caller commits.
    """
    current_time = now or datetime.now(timezone.utc)
    runs = list(
        (
            await db.execute(
                select(RuntimeRun)
                .where(
                    RuntimeRun.status == RuntimeRunStatus.WAITING_RESOURCE.value,
                    RuntimeRun.status_reason == HUMAN_INPUT_SUSPENSION_REASON,
                    RuntimeRun.human_input_request_id.is_not(None),
                )
                .order_by(RuntimeRun.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    expired: list[str] = []
    for run in runs:
        request = await db.get(HitlRequest, run.human_input_request_id)
        deadline = _request_deadline(request) if request is not None else None
        if request is not None and deadline is not None and deadline > current_time:
            continue
        if request is not None and request.status == ApprovalStatus.PENDING.value:
            await close_runtime_human_input_request(
                db, run=run, user_id=None, reason=HUMAN_INPUT_TIMEOUT_REASON,
            )
        from packages.core.services.runtime_run_terminal import (
            finish_runtime_run_out_of_loop,
        )

        if await finish_runtime_run_out_of_loop(
            db,
            run,
            status=RuntimeRunStatus.FAILED,
            reason=HUMAN_INPUT_TIMEOUT_REASON,
            now=current_time,
        ):
            expired.append(run.id)
    return expired


def human_input_question_text(question: dict[str, Any]) -> str:
    """The question as transcript text (what a reload shows in place of the card)."""
    lines = [str(question.get("question") or "").strip()]
    options = question.get("options")
    if isinstance(options, list) and options:
        lines.append("Options: " + " / ".join(str(item) for item in options))
    fields = question.get("fields")
    if isinstance(fields, list) and fields:
        labels = [
            str(item.get("label") or item.get("name") or "").strip()
            for item in fields
            if isinstance(item, dict)
        ]
        if any(labels):
            lines.append("Please provide: " + ", ".join(label for label in labels if label))
    context = str(question.get("context") or "").strip()
    if context:
        lines.append(context)
    return "\n".join(line for line in lines if line)


async def record_human_input_question_message(
    db: AsyncSession,
    *,
    run: RuntimeRun,
) -> Any:
    """Persist the parked question as an assistant message.

    The live client renders the question as a card inside the streaming
    bubble; after a reload only messages exist, so the question must be one.
    """
    if not run.conversation_id or not run.human_input_request_id:
        return None
    request = await db.get(HitlRequest, run.human_input_request_id)
    if request is None:
        return None
    context = request.context if isinstance(request.context, dict) else {}
    question = context.get("question") if isinstance(context.get("question"), dict) else {}
    text = human_input_question_text(question) or str(request.reason or "").strip()
    if not text:
        return None
    from packages.core.services.conversation_messages import add_message

    message = await add_message(
        db,
        run.conversation_id,
        role="assistant",
        content=text,
        meta={
            "runtime_input_request_id": request.id,
            "runtime_run_id": run.id,
            "runtime_input_role": "question",
        },
    )
    message.created_at = datetime.now(timezone.utc)
    return message


async def record_human_input_answer_message(
    db: AsyncSession,
    *,
    run: RuntimeRun,
    answer_text: str,
) -> Any:
    """Persist the answer as a user message and place the assistant's reply
    after it.

    The assistant placeholder was created when the turn started, before the
    question; ordered by ``created_at`` it would render above the answer.
    The reply the run produces after resuming is the reply TO the answer, so
    the placeholder moves just past it.
    """
    from packages.core.models.task import Message
    from packages.core.services.conversation_messages import add_message

    text = str(answer_text or "").strip()
    if not text or not run.conversation_id:
        return None
    # The caller holds the run lock through answer + message + outbox commit.
    # A retransmitted HTTP answer must not add a second, possibly different,
    # user message when only the first answer will reach the model.
    existing = await db.scalar(select(Message).where(
        Message.conversation_id == run.conversation_id,
        Message.meta["runtime_input_request_id"].astext == run.human_input_request_id,
        Message.meta["runtime_input_role"].astext == "answer",
    ))
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    message = await add_message(
        db,
        run.conversation_id,
        role="user",
        content=text,
        meta={
            "runtime_input_request_id": run.human_input_request_id,
            "runtime_run_id": run.id,
            "runtime_input_role": "answer",
        },
    )
    message.created_at = now
    if run.assistant_message_id:
        placeholder = await db.get(Message, run.assistant_message_id)
        if placeholder is not None:
            placeholder.created_at = now + timedelta(milliseconds=1)
    return message


def is_awaiting_human_input(run: RuntimeRun) -> bool:
    return (
        run.status == RuntimeRunStatus.WAITING_RESOURCE.value
        and bool(run.human_input_request_id)
        and run.status_reason == HUMAN_INPUT_SUSPENSION_REASON
    )


__all__ = [
    "RUNTIME_INPUT_ACTION_KEY",
    "RuntimeHumanInputError",
    "answer_runtime_human_input",
    "close_runtime_human_input_request",
    "expire_runtime_human_input_requests",
    "human_input_projection",
    "human_input_question_text",
    "is_awaiting_human_input",
    "record_human_input_answer_message",
    "record_human_input_question_message",
    "mint_runtime_human_input_request",
]
