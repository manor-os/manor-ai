"""Route a person's answer or approval back into a parked Task agent loop.

A Task agent loop (``TaskCoordinatorRun.engine == "agent_loop"``) that asks a
question (``request_input``), hits an approval-gated action, or whose
delegated agent asks a question parks in phase WAITING_HUMAN with its Task in
WAITING_ON_CUSTOMER. The cards are the ones users already have: the Task
input card (``ensure_task_input_hitl``) and the runtime approval card in the
Task thread. Only the routing back into this run is new:

* an answer reaches ``apply_task_input_answer`` from every answer surface; for
  a parked loop it is written into the parked checkpoint as the pending
  ``request_input`` call's result (chat's shape), or, for a delegated agent's
  question, as a note for the loop's next turn;
* an approval reaches ``approval_service``; the request carries a signed tag
  naming this run, call and checkpoint. Approved: the run wakes and re-executes
  the gated call, whose guard finds and spends this exact grant. Rejected (or
  closed without approval): the run wakes with a host-written result saying
  the action did not run.

Every resolution re-checks, under the Task -> Run locks, that the run is still
parked on exactly that request and call; a stale card never resumes a newer
park.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
from packages.core.constants.task import TaskLogType, TaskStatus
from packages.core.constants.task_actors import TaskActor
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services.durable_run import reopen_run
from packages.core.services.task_coordinator_wakeup import (
    TASK_COORDINATOR_RUN_SPEC,
    TASK_LOOP_WAIT_KEY,
)
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.services.task_state_machine import TERMINAL_STATUSES


#: HitlRequest.context key: the signed park this approval resumes.
TASK_AGENT_LOOP_HITL_CONTEXT_KEY = "task_agent_loop_hitl_v1"
#: Run checkpoint key of the parked RuntimeAgentCheckpoint (as the worker's).
RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
#: Run checkpoint key: a host note the next model turn reads (delegated answer).
HUMAN_NOTE_KEY = "human_resume_note"
#: ``task_loop_wait.kind`` of each human wait.
INPUT_WAIT_KIND = "waiting_human"
APPROVAL_WAIT_KIND = "waiting_approval"
DELEGATED_INPUT_WAIT_KIND = "waiting_delegated_input"
HUMAN_WAIT_KINDS = frozenset({INPUT_WAIT_KIND, APPROVAL_WAIT_KIND, DELEGATED_INPUT_WAIT_KIND})
HUMAN_WAIT_VERSION = 1
#: ``apply_task_input_answer``'s ``dispatch_agent_id`` for a parked loop. Its
#: wake is staged in the answer's own transaction; there is no Celery task to
#: dispatch after commit (``dispatch_task_input_resume`` treats it as done).
TASK_AGENT_LOOP_INPUT_DISPATCH = "task_agent_loop:wake_staged"

_SIGNATURE_DOMAIN = b"manor.task-agent-loop-hitl.v1\0"
_SUMMARY_MAX_CHARS = 2000
_NOTE_MAX_CHARS = 6000

DECLINED_INPUT_INSTRUCTION = (
    "The user declined to answer this question. Continue without it if you can; "
    "otherwise finish with complete_task(outcome=blocked) stating exactly what is "
    "blocked. Do not ask the same question again."
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def runtime_checkpoint_digest(runtime_checkpoint: Any) -> str:
    """Identity of one parked continuation; any change makes it a new park."""

    return hashlib.sha256(_canonical(runtime_checkpoint or {})).hexdigest()


_SIGNED_TAG_FIELDS = (
    "version", "run_id", "task_id", "tool_call_id", "hitl_request_id", "checkpoint_digest",
)


def _tag_signature(tag: dict[str, Any]) -> str:
    # Keyed by JWT_SECRET_KEY, as TaskRunner's checkpoint signatures: rotating
    # the secret makes every parked approval "not current", so approving it
    # expires the card instead of resuming (the Task can be retried).
    from packages.core.config import get_settings

    signed = {key: tag.get(key) for key in _SIGNED_TAG_FIELDS}
    return hmac.new(
        get_settings().JWT_SECRET_KEY.encode("utf-8"),
        _SIGNATURE_DOMAIN + _canonical(signed),
        hashlib.sha256,
    ).hexdigest()


def approval_tag(
    *, run_id: str, task_id: str, tool_call_id: str, hitl_request_id: str, checkpoint_digest: str,
) -> dict[str, Any]:
    tag = {
        "version": HUMAN_WAIT_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "hitl_request_id": hitl_request_id,
        "checkpoint_digest": checkpoint_digest,
    }
    tag["signature"] = _tag_signature(tag)
    return tag


def is_task_agent_loop_request(request: Any) -> bool:
    """Whether this approval was parked by a Task agent loop (no DB access)."""

    context = getattr(request, "context", None)
    return isinstance(context, dict) and isinstance(context.get(TASK_AGENT_LOOP_HITL_CONTEXT_KEY), dict)


def verified_request_tag(request: Any) -> dict[str, Any] | None:
    """The request's park tag when its signature and own identity hold."""

    context = getattr(request, "context", None)
    tag = context.get(TASK_AGENT_LOOP_HITL_CONTEXT_KEY) if isinstance(context, dict) else None
    if not isinstance(tag, dict):
        return None
    signature = tag.get("signature")
    if (
        tag.get("version") != HUMAN_WAIT_VERSION
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, _tag_signature(tag))
        or tag.get("hitl_request_id") != getattr(request, "id", None)
        or tag.get("task_id") != getattr(request, "origin_task_id", None)
    ):
        return None
    return tag


def human_wait(
    *,
    kind: str,
    tool_call_id: str,
    tool_name: str,
    hitl_request_id: str,
    checkpoint_digest: str | None,
    summary: str,
    parked_at: str | None = None,
) -> dict[str, Any]:
    return {
        "version": HUMAN_WAIT_VERSION,
        "kind": kind,
        "tool_call_id": tool_call_id,
        "tool_name": str(tool_name or "")[:100],
        "hitl_request_id": hitl_request_id,
        "checkpoint_digest": checkpoint_digest,
        "summary": str(summary or "")[:_SUMMARY_MAX_CHARS],
        "parked_at": parked_at or utcnow().isoformat(),
    }


def parked_human_wait(run: Any, *, kinds: frozenset[str] = HUMAN_WAIT_KINDS) -> dict[str, Any] | None:
    """The human wait a WAITING_HUMAN agent loop is parked on, if any."""

    if (
        run is None
        or getattr(run, "engine", None) != TaskCoordinatorEngine.AGENT_LOOP.value
        or getattr(run, "phase", None) != TaskCoordinatorPhase.WAITING_HUMAN.value
    ):
        return None
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    wait = checkpoint.get(TASK_LOOP_WAIT_KEY)
    if (
        not isinstance(wait, dict)
        or wait.get("version") != HUMAN_WAIT_VERSION
        or wait.get("kind") not in kinds
        or not wait.get("hitl_request_id")
        or wait.get("resolution") is not None
    ):
        return None
    if wait.get("checkpoint_digest") is not None and wait["checkpoint_digest"] != runtime_checkpoint_digest(
        checkpoint.get(RUNTIME_CHECKPOINT_KEY)
    ):
        return None
    return wait


async def task_agent_loop_run(
    db: AsyncSession, task_id: str, *, for_update: bool = False,
) -> TaskCoordinatorRun | None:
    """The Task's coordinator run when it is an agent loop."""

    statement = select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id == task_id,
        TaskCoordinatorRun.engine == TaskCoordinatorEngine.AGENT_LOOP.value,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one_or_none()


async def task_agent_loop_waiting_on_input(db: AsyncSession, task: Task) -> bool:
    """Whether this Task's agent loop is parked on a question (any input kind)."""

    run = await task_agent_loop_run(db, str(task.id))
    return parked_human_wait(run, kinds=frozenset({INPUT_WAIT_KIND, DELEGATED_INPUT_WAIT_KIND})) is not None


# ── answer and rejection results the model reads ───────────────────────


def input_answer_result(*, response_text: str, payload: dict[str, Any], answered_by: str) -> str:
    """Chat's request_input result for an answer; a declined result for a refusal."""

    from packages.core.ai.runtime.human_input import human_input_answer_result

    if str(payload.get("choice") or "").strip().lower() == "refuse":
        return json.dumps({
            "status": "declined",
            "answer": "",
            "choice": "refuse",
            "answered_by": answered_by,
            "instruction": DECLINED_INPUT_INSTRUCTION,
        }, ensure_ascii=False)
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else None
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
    answer = str(response_text or "").strip()
    if not answer and fields and len(fields) == 1:
        # A single-question form: its one value IS the answer (as plan steps).
        answer = str(next(iter(fields.values())) or "").strip()
    return human_input_answer_result(
        answer=answer,
        fields=fields,
        choice=payload.get("choice"),
        answered_by=answered_by,
        attachments=attachments,
    )


def delegated_answer_note(*, wait: dict[str, Any], response_text: str, payload: dict[str, Any]) -> str:
    """The host note a loop reads after its delegated agent's question was answered."""

    agent = str(wait.get("tool_name") or "").strip()
    who = f"The delegated agent ({agent})" if agent else "The delegated agent"
    if str(payload.get("choice") or "").strip().lower() == "refuse":
        answer = "The user declined to answer it."
    else:
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        parts = [str(response_text or "").strip()] + [
            f"{key}: {value}" for key, value in fields.items() if str(key).strip()
        ]
        answer = "The user answered: " + "; ".join(part for part in parts if part)
    question = str(wait.get("summary") or "").strip()
    note = (
        f"{who} asked a question that was shown to the user"
        + (f": {question}" if question else ".")
        + f" {answer} The delegated agent's request was answered; delegate again to the "
        "same agent with continue_interrupted=true and give it this answer so it can "
        "continue. Do not ask the user the same question again."
    )
    return note[:_NOTE_MAX_CHARS]


def approval_not_run_result(request: HitlRequest, *, reason: str) -> dict[str, Any]:
    """The stored result for a gated call whose approval was not granted.

    Chat's rejection text, as a tool result. ``gate: true`` tells the loop the
    host decided this call: it never ran (no effect in the ledger).
    """

    from packages.core.ai.runtime.approval_messages import runtime_approval_rejected_message

    context = request.context if isinstance(request.context, dict) else {}
    item = {"tool": context.get("tool"), "action_key": request.action_key}
    rejected = reason == "rejected"
    body = {
        "status": "rejected" if rejected else "not_approved",
        "error": "approval_rejected" if rejected else "approval_not_granted",
        "approval_id": request.id,
        "tool": context.get("tool"),
        "action_key": request.action_key,
        "action_executed": False,
        "reason": reason,
        "message": (
            runtime_approval_rejected_message(item)
            if rejected
            else f"The approval for `{item['tool']}` was closed without approval ({reason}). "
            "The action did not run."
        ),
        "instruction": (
            "This action did not run. Continue the Task without it if you can, or "
            "finish with complete_task(outcome=blocked) explaining what the person "
            "declined. Do not retry the same action."
        ),
    }
    return body


# ── resuming the run ────────────────────────────────────────────────────


async def _resume_parked_run(
    db: AsyncSession,
    *,
    task: Task,
    run: TaskCoordinatorRun,
    wait: dict[str, Any],
    resolution: str,
    pending_tool_result: dict[str, Any] | None = None,
    pending_arguments: dict[str, Any] | None = None,
    note: str | None = None,
    reason: str,
) -> bool:
    """Write the decision into the park and wake the run when its Task waits.

    The caller holds Task -> Run locks. A paused Task (not waiting on the
    person any more) only records the decision: resuming the Task rebinds the
    run with this checkpoint and its wake replays it.
    """

    checkpoint = copy.deepcopy(run.checkpoint if isinstance(run.checkpoint, dict) else {})
    parked = checkpoint.get(RUNTIME_CHECKPOINT_KEY)
    if pending_tool_result is not None or pending_arguments is not None:
        if not isinstance(parked, dict) or not isinstance(parked.get("pending_tool_call"), dict):
            return False
        parked = copy.deepcopy(parked)
        if pending_tool_result is not None:
            parked["pending_tool_result"] = pending_tool_result
        if pending_arguments is not None:
            parked["pending_tool_call"] = {**parked["pending_tool_call"], "arguments": dict(pending_arguments)}
        checkpoint[RUNTIME_CHECKPOINT_KEY] = parked
    if note:
        checkpoint[HUMAN_NOTE_KEY] = note[:_NOTE_MAX_CHARS]
    checkpoint[TASK_LOOP_WAIT_KEY] = {
        **wait,
        "resolution": resolution,
        "resolved_at": utcnow().isoformat(),
    }
    run.checkpoint = checkpoint
    run.state_version += 1
    if task.status != TaskStatus.WAITING_ON_CUSTOMER.value:
        return True
    from packages.core.services.task_state_machine import apply_task_status_transition

    await apply_task_status_transition(task, TaskStatus.IN_PROGRESS.value, db=db, actor_kind="system")
    # The status change made a new Task lifecycle; the run continues it.
    run.lifecycle_generation = task_execution_intent_generation(task)
    wake = await reopen_run(
        db, TASK_COORDINATOR_RUN_SPEC, run,
        phase=TaskCoordinatorPhase.READY.value, reason=reason,
    )
    return wake is not None


async def apply_task_agent_loop_input_answer(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    response_text: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """``apply_task_input_answer`` for an agent-loop Task; None for any other Task.

    The answer is the pending call's result, written into the parked
    checkpoint. The Task is re-read under its lock (Task -> Run, as the park)
    and its status re-checked there, so an answer never revives a Task that
    was cancelled after the answering surface loaded it.

    A Task waiting on the person resumes in this same transaction. A paused
    Task (on hold, not terminal) keeps the answer in its checkpoint, as an
    approval decided while paused is kept: resuming the Task rebinds the run
    and its wake replays the answer instead of asking again.
    """

    from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY, resolve_task_input_hitl
    from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY
    from packages.core.services.task_service import add_task_log

    if await task_agent_loop_run(db, str(task.id)) is None:
        return None
    not_resumed = {"resumed": False, "dispatch_agent_id": None}
    task = (await db.execute(
        select(Task).where(Task.id == task.id, Task.entity_id == task.entity_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status in TERMINAL_STATUSES:
        return not_resumed
    run = await task_agent_loop_run(db, str(task.id), for_update=True)
    wait = parked_human_wait(run, kinds=frozenset({INPUT_WAIT_KIND, DELEGATED_INPUT_WAIT_KIND}))
    if run is None or wait is None:
        return not_resumed
    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == wait["hitl_request_id"],
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.action_key == TASK_INPUT_ACTION_KEY,
        )
    )).scalar_one_or_none()
    refused = str(payload.get("choice") or "").strip().lower() == "refuse"
    if request is None or not (
        request.status == ApprovalStatus.PENDING.value
        # The chat bridge denies a refused question before resuming with it.
        or (refused and request.status == ApprovalStatus.DENIED.value)
    ):
        return not_resumed
    clean_payload = dict(payload or {})
    if wait["kind"] == INPUT_WAIT_KIND:
        resumed = await _resume_parked_run(
            db, task=task, run=run, wait=wait, resolution="declined" if refused else "answered",
            pending_tool_result={
                "id": wait["tool_call_id"],
                "content": input_answer_result(
                    response_text=response_text, payload=clean_payload, answered_by=user_label,
                ),
            },
            reason="human_input_answered",
        )
    else:
        resumed = await _resume_parked_run(
            db, task=task, run=run, wait=wait, resolution="declined" if refused else "answered",
            note=delegated_answer_note(wait=wait, response_text=response_text, payload=clean_payload),
            reason="human_input_answered",
        )
    if not resumed:
        return not_resumed
    details = dict(task.details or {})
    details[TASK_INPUT_QUESTION_KEY] = None
    details["_hitl_response"] = response_text
    details["_hitl_payload"] = clean_payload
    details["_hitl_responded_by"] = user_label
    task.details = details
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_HITL_RESUMED,
        f"Human input received: {response_text[:300]}" if response_text else "Human input received.",
        actor=TaskActor.USER,
        created_by=user_label,
        metadata={
            "response": response_text,
            "choice": clean_payload.get("choice"),
            "fields": clean_payload.get("fields") or {},
            "submitted_by": user_id,
            "submitted_at": utcnow().isoformat(),
            "mode": "agent_loop",
            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
        },
    )
    await resolve_task_input_hitl(
        db, task=task, user_id=user_id, answer=response_text, payload=clean_payload,
    )
    return {"resumed": True, "dispatch_agent_id": TASK_AGENT_LOOP_INPUT_DISPATCH}


# ── approvals ──────────────────────────────────────────────────────────


async def task_agent_loop_owns_request(db: AsyncSession, *, request: HitlRequest) -> bool:
    """Whether a Task-origin request belongs to a Task run by an agent loop.

    Such a request is never resolved through TaskRunner's checkpoint path: an
    untagged or stale one is closed without failing the Task.
    """

    if is_task_agent_loop_request(request):
        return True
    task_id = str(getattr(request, "origin_task_id", None) or "").strip()
    if not task_id:
        return False
    return await task_agent_loop_run(db, task_id) is not None


async def _locked_approval_park(
    db: AsyncSession, *, request: HitlRequest,
) -> tuple[Task, TaskCoordinatorRun, dict[str, Any], dict[str, Any]] | None:
    """Task, run, wait and tag when the run is parked on exactly this request.

    Lock order Task -> Run (the caller already holds the request).
    """

    tag = verified_request_tag(request)
    if tag is None or request.origin_kind != ApprovalOriginKind.TOOL_CALL.value:
        return None
    task = (await db.execute(
        select(Task).where(
            Task.id == tag["task_id"], Task.entity_id == request.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status in TERMINAL_STATUSES:
        return None
    run = await task_agent_loop_run(db, str(task.id), for_update=True)
    wait = parked_human_wait(run, kinds=frozenset({APPROVAL_WAIT_KIND}))
    if (
        run is None
        or wait is None
        or run.id != tag["run_id"]
        or wait.get("hitl_request_id") != request.id
        or wait.get("tool_call_id") != tag["tool_call_id"]
        or wait.get("checkpoint_digest") != tag["checkpoint_digest"]
    ):
        return None
    return task, run, wait, tag


async def task_agent_loop_approval_is_current(db: AsyncSession, *, request: HitlRequest) -> bool:
    """Whether this approval still resumes the run that parked on it."""

    return await _locked_approval_park(db, request=request) is not None


def existing_task_agent_loop_resolution(request: Any) -> dict[str, Any] | None:
    """An already-decided parked approval, for idempotent card clicks."""

    tag = verified_request_tag(request)
    resolution = tag.get("resolution") if isinstance(tag, dict) else None
    if not isinstance(resolution, dict) or resolution.get("choice") not in {"approved", "rejected"}:
        return None
    decided = (
        {ApprovalStatus.GRANTED.value, ApprovalStatus.CONSUMED.value}
        if resolution["choice"] == "approved"
        else {ApprovalStatus.DENIED.value}
    )
    if getattr(request, "status", None) not in decided:
        return None
    return _resolution_metadata(tag, choice=resolution["choice"])


def _resolution_metadata(tag: dict[str, Any], *, choice: str) -> dict[str, Any]:
    # The same kind as TaskRunner's: a background Task owns the continuation,
    # so Chat never replays it and a rejection never reaches a chat model.
    return {
        "approval_kind": "task_runtime",
        "task_runtime_resume": {
            "task_id": tag["task_id"],
            "hitl_id": tag["hitl_request_id"],
            "choice": choice,
            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
        },
    }


def _approved_call_arguments(request: HitlRequest, pending_call: dict[str, Any]) -> dict[str, Any] | None:
    """The parked call's arguments for its approved re-execution.

    Built as chat and TaskRunner build an approved retry
    (``runtime_approval_retry_args``): the request's frozen continuation (a
    review may have finalized it) plus ``approval_token=<request id>``. The
    guard then spends exactly this request on its token path, which checks
    the tool and the approved ``args_hash`` before consuming the row. A call
    made through ``call_tool`` carries them in its inner arguments.
    """

    from packages.core.ai.runtime.approval_messages import runtime_approval_retry_args
    from packages.core.ai.runtime.tool_gateway import (
        CALL_TOOL_NAME,
        runtime_gateway_arguments,
        runtime_tool_call_target,
    )

    context = request.context if isinstance(request.context, dict) else {}
    name, _arguments = runtime_tool_call_target(pending_call.get("name"), pending_call.get("arguments"))
    if name != context.get("tool"):
        return None
    retry = runtime_approval_retry_args({
        "continuation": context.get("continuation"),
        "retry_args": context.get("retry_args"),
        # A truncated preview is never replayed as the approved call.
        "args_preview": None,
    }, request.id)
    if set(retry) == {"approval_token"}:
        return None
    if pending_call.get("name") == CALL_TOOL_NAME:
        return runtime_gateway_arguments(name, retry)
    return retry


async def stage_task_agent_loop_approval_resolution(
    db: AsyncSession,
    *,
    request: HitlRequest,
    choice: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Resume a parked loop after its approval was granted or denied.

    Called by ``approval_service`` after it recorded the decision on the
    request (grant, standing grant or deny). Returns the resolution metadata,
    or None when the request does not resume a parked loop (the caller then
    reports it as no longer current).
    """

    from packages.core.services.task_service import add_task_log

    if choice not in {"approve", "always_approve", "reject"}:
        return None
    approved = choice in {"approve", "always_approve"}
    expected = ApprovalStatus.GRANTED.value if approved else ApprovalStatus.DENIED.value
    if request.status != expected:
        return None
    park = await _locked_approval_park(db, request=request)
    if park is None:
        return None
    task, run, wait, tag = park
    normalized = "approved" if approved else "rejected"
    if approved:
        parked = (run.checkpoint or {}).get(RUNTIME_CHECKPOINT_KEY) or {}
        arguments = _approved_call_arguments(request, parked.get("pending_tool_call") or {})
        if arguments is None:
            return None
        resumed = await _resume_parked_run(
            db, task=task, run=run, wait=wait, resolution=normalized,
            pending_arguments=arguments,
            reason="runtime_approval_approved",
        )
    else:
        resumed = await _resume_parked_run(
            db, task=task, run=run, wait=wait, resolution=normalized,
            pending_tool_result={
                "id": wait["tool_call_id"],
                "content": json.dumps(
                    approval_not_run_result(request, reason=reason or "rejected"), ensure_ascii=False,
                ),
                "gate": True,
            },
            reason="runtime_approval_rejected",
        )
    if not resumed:
        return None
    context = dict(request.context or {})
    context[TASK_AGENT_LOOP_HITL_CONTEXT_KEY] = {
        **tag,
        "resolution": {"choice": normalized, "reason": reason, "at": utcnow().isoformat()},
    }
    request.context = context
    tool = str(wait.get("tool_name") or "the action")
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_AGENT_TURN,
        (
            f"Approval granted for {tool}; the Task continues and runs it."
            if approved
            else f"Approval not granted for {tool} ({reason or 'rejected'}); the Task continues without it."
        ),
        actor=TaskActor.USER if request.decided_by_user_id else TaskActor.SYSTEM,
        created_by=request.decided_by_user_id,
        metadata={
            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
            "hitl_request_id": request.id,
            "choice": normalized,
            "reason": reason,
        },
    )
    return _resolution_metadata(tag, choice=normalized)


# ── cleanup ────────────────────────────────────────────────────────────


async def lock_agent_loop_input_requests(db: AsyncSession, *, task: Task) -> list[HitlRequest]:
    """An agent-loop Task's open input requests, locked; [] for any other Task.

    Taken in the HitlRequest -> Task order of the terminal approval cleanup.
    """

    from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY

    if await task_agent_loop_run(db, str(task.id)) is None:
        return []
    return list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == task.entity_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id,
            HitlRequest.action_key == TASK_INPUT_ACTION_KEY,
            HitlRequest.status == ApprovalStatus.PENDING.value,
        ).order_by(HitlRequest.id.asc()).with_for_update().execution_options(populate_existing=True)
    )).scalars().all())


async def close_agent_loop_input_requests(
    db: AsyncSession, *, task: Task, requests: list[HitlRequest], reason: str,
) -> int:
    """Expire locked input requests of a terminal Task and close their cards.

    The caller holds the Task's approval boundary (advisory lock) and the
    requests; a question can no longer resume a stopped loop.
    """

    from packages.core.services.task_runtime_approvals import _expire_locked_runtime_request

    expired = [request for request in requests if await _expire_locked_runtime_request(db, request, reason=reason)]
    if expired and task.workspace_id:
        from packages.core.constants.pending_actions import PendingActionKind
        from packages.core.models.task import Conversation, Message
        from packages.core.workspace_chat import service as chat_service

        ids = {request.id for request in expired}
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
        for message in rows:
            action = message.pending_action if isinstance(message.pending_action, dict) else {}
            if (
                action.get("kind") == PendingActionKind.HUMAN_INPUT.value
                and action.get("approval_request_id") in ids
            ):
                await chat_service.resolve_pending_action(
                    db, message_id=message.id, user_id=None,
                    resolution={"choice": "expired", "note": reason}, emit_followup=False,
                )
    return len(expired)


async def expire_task_agent_loop_input_requests(db: AsyncSession, *, task: Task, reason: str) -> int:
    """Backstop: close a terminal agent-loop Task's input cards in their own transaction."""

    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    await lock_task_runtime_approval_boundary(db, task_id=str(task.id))
    requests = await lock_agent_loop_input_requests(db, task=task)
    return await close_agent_loop_input_requests(db, task=task, requests=requests, reason=reason)


__all__ = [
    "APPROVAL_WAIT_KIND",
    "DELEGATED_INPUT_WAIT_KIND",
    "HUMAN_NOTE_KEY",
    "HUMAN_WAIT_KINDS",
    "INPUT_WAIT_KIND",
    "TASK_AGENT_LOOP_HITL_CONTEXT_KEY",
    "TASK_AGENT_LOOP_INPUT_DISPATCH",
    "apply_task_agent_loop_input_answer",
    "approval_not_run_result",
    "approval_tag",
    "delegated_answer_note",
    "existing_task_agent_loop_resolution",
    "close_agent_loop_input_requests",
    "expire_task_agent_loop_input_requests",
    "lock_agent_loop_input_requests",
    "human_wait",
    "input_answer_result",
    "is_task_agent_loop_request",
    "parked_human_wait",
    "runtime_checkpoint_digest",
    "stage_task_agent_loop_approval_resolution",
    "task_agent_loop_approval_is_current",
    "task_agent_loop_owns_request",
    "task_agent_loop_run",
    "task_agent_loop_waiting_on_input",
    "verified_request_tag",
]
