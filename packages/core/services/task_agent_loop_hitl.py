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

With durable delegation a delegated agent's child run parks the same way on
its own cards ("Asked by {agent}"): its question has its own dedup key and
names its run, its approval carries a v2 tag (``run_kind="child"`` and the
root run). Only that child waits; the Task stays in progress while anything
else in the run tree can move, and waits on the person once nothing can
(``sync_task_human_wait_status``). An answer or decision reaches exactly the
run that asked (a card names its request; a comment answers the earliest
question) and wakes only it; its parent keeps waiting on it. Each card is
judged as before: a question's answer is the user's own input, an approval
is the guarded operation's own request under the user's mode and rules.
"""
from __future__ import annotations

import contextlib
import contextvars
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
from packages.core.models.task_run_journal import TaskRunJournalKind
from packages.core.services.durable_run import DurableRunSpec, reopen_run
from packages.core.services.task_run_journal import append_run_journal, wake_payload
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


#: Approval park tag versions: v1 names a Task's root run, v2 a delegated
#: child run (``run_kind="child"``) and the root run of its tree.
ROOT_APPROVAL_TAG_VERSION = 1
CHILD_APPROVAL_TAG_VERSION = 2
RUN_KIND_ROOT = "root"
RUN_KIND_CHILD = "child"
_SIGNED_TAG_FIELDS = {
    ROOT_APPROVAL_TAG_VERSION: (
        "version", "run_id", "task_id", "tool_call_id", "hitl_request_id", "checkpoint_digest",
    ),
    CHILD_APPROVAL_TAG_VERSION: (
        "version", "run_kind", "run_id", "root_run_id", "task_id", "tool_call_id",
        "hitl_request_id", "checkpoint_digest",
    ),
}
_SIGNATURE_DOMAINS = {
    ROOT_APPROVAL_TAG_VERSION: _SIGNATURE_DOMAIN,
    CHILD_APPROVAL_TAG_VERSION: b"manor.task-agent-loop-hitl.v2\0",
}


def _tag_signature(tag: dict[str, Any]) -> str | None:
    # Keyed by JWT_SECRET_KEY: rotating
    # the secret makes every parked approval "not current", so approving it
    # expires the card instead of resuming (the Task can be retried).
    from packages.core.config import get_settings

    version = tag.get("version")
    fields = _SIGNED_TAG_FIELDS.get(version) if isinstance(version, int) else None
    if fields is None:
        return None
    signed = {key: tag.get(key) for key in fields}
    return hmac.new(
        get_settings().JWT_SECRET_KEY.encode("utf-8"),
        _SIGNATURE_DOMAINS[version] + _canonical(signed),
        hashlib.sha256,
    ).hexdigest()


def approval_tag(
    *,
    run_id: str,
    task_id: str,
    tool_call_id: str,
    hitl_request_id: str,
    checkpoint_digest: str,
    run_kind: str = RUN_KIND_ROOT,
    root_run_id: str | None = None,
) -> dict[str, Any]:
    """The signed park tag an approval request carries (v1 root, v2 child)."""

    if run_kind == RUN_KIND_CHILD:
        if not root_run_id:
            raise ValueError("a child run's approval tag names its root run")
        tag = {
            "version": CHILD_APPROVAL_TAG_VERSION,
            "run_kind": RUN_KIND_CHILD,
            "run_id": run_id,
            "root_run_id": root_run_id,
        }
    elif run_kind == RUN_KIND_ROOT:
        tag = {"version": ROOT_APPROVAL_TAG_VERSION, "run_id": run_id}
    else:
        raise ValueError(f"unknown run kind {run_kind!r}")
    tag.update({
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "hitl_request_id": hitl_request_id,
        "checkpoint_digest": checkpoint_digest,
    })
    tag["signature"] = _tag_signature(tag)
    return tag


def tag_run_kind(tag: dict[str, Any]) -> str:
    """Which run a verified tag parks: the root run (v1) or a child run (v2)."""

    return RUN_KIND_CHILD if tag.get("version") == CHILD_APPROVAL_TAG_VERSION else RUN_KIND_ROOT


def verified_request_tag(request: Any) -> dict[str, Any] | None:
    """The request's park tag when its signature and own identity hold."""

    context = getattr(request, "context", None)
    tag = context.get(TASK_AGENT_LOOP_HITL_CONTEXT_KEY) if isinstance(context, dict) else None
    if not isinstance(tag, dict):
        return None
    signature = tag.get("signature")
    expected = _tag_signature(tag)
    if (
        expected is None
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, expected)
        or tag.get("hitl_request_id") != getattr(request, "id", None)
        or tag.get("task_id") != getattr(request, "origin_task_id", None)
        or (
            tag.get("version") == CHILD_APPROVAL_TAG_VERSION
            and (tag.get("run_kind") != RUN_KIND_CHILD or not tag.get("root_run_id"))
        )
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
    run_kind: str | None = None,
    agent_label: str | None = None,
) -> dict[str, Any]:
    wait = {
        "version": HUMAN_WAIT_VERSION,
        "kind": kind,
        "tool_call_id": tool_call_id,
        "tool_name": str(tool_name or "")[:100],
        "hitl_request_id": hitl_request_id,
        "checkpoint_digest": checkpoint_digest,
        "summary": str(summary or "")[:_SUMMARY_MAX_CHARS],
        "parked_at": parked_at or utcnow().isoformat(),
    }
    if run_kind == RUN_KIND_CHILD:
        # A delegated child's park: its own card, asked by its own agent.
        wait["run_kind"] = RUN_KIND_CHILD
        if agent_label:
            wait["agent_label"] = str(agent_label)[:100]
    return wait


def _hosted_by_task_loop(run: Any, spec: DurableRunSpec) -> bool:
    """Whether the Task agent loop drives this run row.

    A Task's root run is loop-hosted only when its frozen engine is
    ``agent_loop`` (the Plan/Step DAG coordinator owns the others).
    """

    if spec.model is TaskCoordinatorRun:
        return getattr(run, "engine", None) == TaskCoordinatorEngine.AGENT_LOOP.value
    return True


def parked_human_wait(
    run: Any,
    *,
    kinds: frozenset[str] = HUMAN_WAIT_KINDS,
    spec: DurableRunSpec = TASK_COORDINATOR_RUN_SPEC,
) -> dict[str, Any] | None:
    """The human wait a WAITING_HUMAN loop-hosted run of ``spec`` is parked on, if any."""

    if (
        run is None
        or not _hosted_by_task_loop(run, spec)
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
    # Always the row as stored (a long-lived session may hold an older copy).
    statement = statement.execution_options(populate_existing=True)
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


INPUT_WAIT_KINDS = frozenset({INPUT_WAIT_KIND, DELEGATED_INPUT_WAIT_KIND})


def _child_spec() -> DurableRunSpec:
    from packages.core.services.task_agent_child_wakeup import TASK_AGENT_CHILD_RUN_SPEC

    return TASK_AGENT_CHILD_RUN_SPEC


def parked_child_human_wait(child: Any, *, kinds: frozenset[str] = HUMAN_WAIT_KINDS) -> dict[str, Any] | None:
    """The human wait a delegated child run is parked on, if any."""

    return parked_human_wait(child, kinds=kinds, spec=_child_spec())


async def _live_children(db: AsyncSession, task_id: str) -> list[Any]:
    from packages.core.models.task_agent_child import (
        TASK_AGENT_CHILD_TERMINAL_PHASES,
        TaskAgentChildRun,
    )

    return list((await db.execute(
        select(TaskAgentChildRun).where(
            TaskAgentChildRun.task_id == task_id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        ).order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def task_agent_loop_waiting_on_input(db: AsyncSession, task: Task) -> bool:
    """Whether this Task's agent loop, or one of its delegated agents, waits on a question."""

    run = await task_agent_loop_run(db, str(task.id))
    if run is None:
        return False
    if parked_human_wait(run, kinds=INPUT_WAIT_KINDS) is not None:
        return True
    return any(
        parked_child_human_wait(child, kinds=frozenset({INPUT_WAIT_KIND})) is not None
        for child in await _live_children(db, str(task.id))
    )


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


def rebind_task_lifecycle(task: Task, run: Any, children: Any = ()) -> str:
    """Bind a loop-hosted run tree to its Task's current lifecycle.

    A Task status change makes a new lifecycle (``task_execution_intent_generation``)
    and admission refuses a run of an older one. A run that continues across
    such a change (a parked run resumed by its person, a paused Task resumed)
    is rebound here, in the transaction that changed the status, together
    with every non-terminal child row of the Task (``children``, locked by
    the caller). This is the only writer of an agent-loop run's
    ``lifecycle_generation`` (guarded in test_task_agent_durable_delegation).
    Returns the new generation.
    """

    from packages.core.models.task_agent_child import TASK_AGENT_CHILD_TERMINAL_PHASES

    generation = task_execution_intent_generation(task)
    run.lifecycle_generation = generation
    for child in children:
        if child.phase not in TASK_AGENT_CHILD_TERMINAL_PHASES:
            child.lifecycle_generation = generation
    return generation


async def lock_live_task_children(db: AsyncSession, task_id: str) -> list[Any]:
    """The Task's non-terminal child rows, locked in (depth, id) order (after its root run)."""

    from packages.core.models.task_agent_child import (
        TASK_AGENT_CHILD_TERMINAL_PHASES,
        TaskAgentChildRun,
    )

    return list((await db.execute(
        select(TaskAgentChildRun)
        .where(
            TaskAgentChildRun.task_id == task_id,
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        )
        .order_by(TaskAgentChildRun.depth, TaskAgentChildRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())


#: Child phases that move forward on their own (a wake is due or running).
_SELF_DRIVEN_PHASES = frozenset({
    TaskCoordinatorPhase.READY.value,
    TaskCoordinatorPhase.RUNNING.value,
    TaskCoordinatorPhase.WAITING_TIMER.value,
    TaskCoordinatorPhase.WAITING_JOB.value,
})


def _parked_order(entry: tuple[Any, dict[str, Any]]) -> tuple[str, str]:
    run, wait = entry
    return str(wait.get("parked_at") or ""), str(getattr(run, "id", "") or "")


def input_parks(root: Any, children: list[Any]) -> list[tuple[Any, dict[str, Any]]]:
    """Every unanswered question of a run tree, earliest park first."""

    parks: list[tuple[Any, dict[str, Any]]] = []
    root_wait = parked_human_wait(root, kinds=INPUT_WAIT_KINDS)
    if root_wait is not None:
        parks.append((root, root_wait))
    for child in children:
        wait = parked_child_human_wait(child, kinds=frozenset({INPUT_WAIT_KIND}))
        if wait is not None:
            parks.append((child, wait))
    return sorted(parks, key=_parked_order)


def tree_waits_on_a_person(root: Any, children: list[Any]) -> bool:
    """Whether no run of the tree can move forward without a person (Q3).

    The root waits on its delegated children, no child is runnable or parked
    on a timer or a job, no run holds a live claim, and at least one child
    waits, undecided, on a person. Children waiting on their own children
    are waiting on whatever those wait on.
    """

    from packages.core.services.durable_run import claim_is_live

    if root is None or root.phase != TaskCoordinatorPhase.WAITING_CHILD.value or claim_is_live(root):
        return False
    waiting_on_person = False
    for child in children:
        if claim_is_live(child) or child.phase in _SELF_DRIVEN_PHASES:
            return False
        if child.phase == TaskCoordinatorPhase.WAITING_HUMAN.value:
            if parked_child_human_wait(child) is None:
                # Decided (e.g. while the Task was paused): it runs next.
                return False
            waiting_on_person = True
    return waiting_on_person


async def _earliest_question(db: AsyncSession, task: Task, parks: list[tuple[Any, dict[str, Any]]]) -> dict | None:
    """The card question of the earliest unanswered park (what the Task page shows)."""

    for _run, wait in parks:
        context = (await db.execute(
            select(HitlRequest.context).where(
                HitlRequest.id == wait["hitl_request_id"],
                HitlRequest.entity_id == task.entity_id,
                HitlRequest.origin_task_id == task.id,
                HitlRequest.status == ApprovalStatus.PENDING.value,
            )
        )).scalar_one_or_none()
        question = context.get("question") if isinstance(context, dict) else None
        if isinstance(question, dict) and str(question.get("question") or "").strip():
            return question
    return None


async def sync_task_human_wait_status(db: AsyncSession, *, task: Task) -> bool:
    """Put a Task on its person exactly when its whole run tree waits on one.

    Called whenever a delegated child parks on a person or ends, and after a
    person's answer reached a child. The caller holds the Task lock (and the
    root run's, or it is taken here, then every open child in (depth, id)
    order). While any run can still move forward the Task stays in progress
    (a status change would make a new lifecycle and fence its running
    children); once none can, the Task goes to WAITING_ON_CUSTOMER in this
    transaction and the whole tree is rebound to that lifecycle
    (``rebind_task_lifecycle``). The Task page's question is the earliest
    unanswered one of the tree. Returns True when the status changed.
    """

    from packages.core.services.task_input_resume import TASK_INPUT_QUESTION_KEY

    root = await task_agent_loop_run(db, str(task.id), for_update=True)
    if root is None:
        return False
    children = await lock_live_task_children(db, str(task.id))
    # Always current, None once no question of the tree is open.
    question = await _earliest_question(db, task, input_parks(root, children))
    details = dict(task.details or {})
    if details.get(TASK_INPUT_QUESTION_KEY) != question:
        details[TASK_INPUT_QUESTION_KEY] = question
        task.details = details
    if (
        not children
        or task.status != TaskStatus.IN_PROGRESS.value
        or not tree_waits_on_a_person(root, children)
    ):
        return False
    from packages.core.services.task_state_machine import apply_task_status_transition

    await apply_task_status_transition(task, TaskStatus.WAITING_ON_CUSTOMER.value, db=db, actor_kind="system")
    rebind_task_lifecycle(task, root, children)
    return True


async def _resume_parked_run(
    db: AsyncSession,
    *,
    task: Task,
    run: Any,
    wait: dict[str, Any],
    resolution: str,
    pending_tool_result: dict[str, Any] | None = None,
    pending_arguments: dict[str, Any] | None = None,
    note: str | None = None,
    reason: str,
    decided_by: str | None = None,
    spec: DurableRunSpec = TASK_COORDINATOR_RUN_SPEC,
) -> bool:
    """Write the decision into the park and wake the run when its Task waits.

    ``run`` is any loop-hosted run row of ``spec``: the Task's root run or a
    delegated child run. The caller holds the Task -> root Run -> child rows
    locks.

    * Root run: it wakes when its Task waits on the person (the Task goes
      back in progress and the run tree is rebound to the new lifecycle).
    * Child run: when its Task waits on the person (every runnable run of
      the tree was waiting on someone) the Task goes back in progress and
      the whole tree is rebound; in progress or back in progress, only this
      child wakes (ready, its wake staged). Its parent keeps waiting on it.

    A paused Task only records the decision: resuming the Task rebinds the
    run with this checkpoint and its wake (or, for a child, its parent's
    re-park) replays it.

    Journaled as one ``wake`` row (``decided_by`` is the deciding user), after
    the rebind: the row carries the lifecycle the run continues in.
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
    is_child = spec is not TASK_COORDINATOR_RUN_SPEC
    task_resumes = task.status == TaskStatus.WAITING_ON_CUSTOMER.value
    wake = None
    if task_resumes:
        from packages.core.services.task_state_machine import apply_task_status_transition

        await apply_task_status_transition(task, TaskStatus.IN_PROGRESS.value, db=db, actor_kind="system")
        # The status change made a new Task lifecycle; the run tree (the root
        # run and every child of the Task still open) continues it.
        root = run if not is_child else await task_agent_loop_run(db, str(task.id), for_update=True)
        rebind_task_lifecycle(task, root, await lock_live_task_children(db, str(task.id)))
    wakes = task_resumes or (is_child and task.status == TaskStatus.IN_PROGRESS.value)
    if wakes:
        # Only the decided run wakes: a child's parent keeps waiting on it.
        wake = await reopen_run(
            db, spec, run,
            phase=TaskCoordinatorPhase.READY.value, reason=reason,
        )
    # The decision reaching the run is its own journaled transition.
    await append_run_journal(
        db, run=run, kind=TaskRunJournalKind.WAKE,
        payload=wake_payload(
            wait, resolution=resolution, task_resumed=task_resumes, decided_by=decided_by,
        ),
    )
    return wake is not None if wakes else True


async def apply_task_agent_loop_input_answer(
    db: AsyncSession,
    *,
    task: Task,
    user_id: str,
    user_label: str,
    response_text: str,
    payload: dict[str, Any],
    request_id: str | None = None,
) -> dict[str, Any] | None:
    """``apply_task_input_answer`` for an agent-loop Task; None for any other Task.

    The answer is the pending call's result, written into the parked
    checkpoint. The Task is re-read under its lock (Task -> root Run ->
    child rows, as the parks) and its status re-checked there, so an answer
    never revives a Task that was cancelled after the answering surface
    loaded it.

    Which park it answers: the one waiting on ``request_id`` (a card names
    its request), else the earliest unanswered question of the run tree (a
    comment or the Task page). The root run's question, or a delegated
    child's: only that run wakes; the other questions stay open.

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
    root = await task_agent_loop_run(db, str(task.id), for_update=True)
    if root is None:
        return not_resumed
    parks = input_parks(root, await lock_live_task_children(db, str(task.id)))
    if request_id:
        parks = [park for park in parks if park[1].get("hitl_request_id") == request_id]
    if not parks:
        return not_resumed
    run, wait = parks[0]
    is_child = run is not root
    spec = _child_spec() if is_child else TASK_COORDINATOR_RUN_SPEC
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
            decided_by=user_id,
            spec=spec,
        )
    else:
        resumed = await _resume_parked_run(
            db, task=task, run=run, wait=wait, resolution="declined" if refused else "answered",
            note=delegated_answer_note(wait=wait, response_text=response_text, payload=clean_payload),
            reason="human_input_answered",
            decided_by=user_id,
        )
    if not resumed:
        return not_resumed
    details = dict(task.details or {})
    details[TASK_INPUT_QUESTION_KEY] = None
    details["_hitl_response"] = response_text
    details["_hitl_payload"] = clean_payload
    details["_hitl_responded_by"] = user_label
    task.details = details
    agent = str(wait.get("agent_label") or "").strip() if is_child else ""
    received = f"Human input received for {agent}" if agent else "Human input received"
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_HITL_RESUMED,
        f"{received}: {response_text[:300]}" if response_text else f"{received}.",
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
            "hitl_request_id": wait["hitl_request_id"],
            **({"run_id": run.id, "run_kind": RUN_KIND_CHILD, "agent_label": agent} if is_child else {}),
        },
    )
    await resolve_task_input_hitl(
        db, task=task, user_id=user_id, answer=response_text, payload=clean_payload,
        # A child's question closes alone: its siblings' cards stay open.
        request_id=wait["hitl_request_id"] if is_child else None,
    )
    if is_child:
        # The Task page now shows the next open question of the tree, if any.
        await sync_task_human_wait_status(db, task=task)
    return {"resumed": True, "dispatch_agent_id": TASK_AGENT_LOOP_INPUT_DISPATCH}


# ── approvals ──────────────────────────────────────────────────────────


async def _locked_approval_park(
    db: AsyncSession, *, request: HitlRequest,
) -> tuple[Task, Any, dict[str, Any], dict[str, Any], DurableRunSpec] | None:
    """Task, run, wait, tag and run spec when the run is parked on exactly this request.

    The tag says which run: the Task's root run (v1) or a delegated child
    run of that root (v2). Lock order Task -> root Run -> child rows (the
    caller already holds the request). A child that ended (cancelled with its
    Task or by its parent) is no longer parked: its card is not current. A
    child that holds a claim while its unresolved wait still names this
    request (woken, about to re-park on the same card) is current: the
    decision reopens it with a fresh wake and the racing owner's re-park
    loses its CAS, so an approval never turns into a denial by racing it.
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
    root = await task_agent_loop_run(db, str(task.id), for_update=True)
    if root is None:
        return None
    if tag_run_kind(tag) == RUN_KIND_CHILD:
        if root.id != tag["root_run_id"]:
            return None
        spec = _child_spec()
        run = next(
            (child for child in await lock_live_task_children(db, str(task.id)) if child.id == tag["run_id"]),
            None,
        )
        wait = parked_child_human_wait(run, kinds=frozenset({APPROVAL_WAIT_KIND})) if run is not None else None
    else:
        spec, run = TASK_COORDINATOR_RUN_SPEC, root
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
    return task, run, wait, tag, spec


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
    # A background Task owns the continuation,
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

    Built as chat builds an approved retry
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
    task, run, wait, tag, spec = park
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
            decided_by=request.decided_by_user_id,
            spec=spec,
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
            decided_by=request.decided_by_user_id,
            spec=spec,
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
    is_child = tag_run_kind(tag) == RUN_KIND_CHILD
    agent = str(wait.get("agent_label") or "").strip() if is_child else ""
    who = f"{agent} continues" if agent else "the Task continues"
    await add_task_log(
        db,
        task.id,
        TaskLogType.AI_AGENT_TURN,
        (
            f"Approval granted for {tool}; {who} and runs it."
            if approved
            else f"Approval not granted for {tool} ({reason or 'rejected'}); {who} without it."
        ),
        actor=TaskActor.USER if request.decided_by_user_id else TaskActor.SYSTEM,
        created_by=request.decided_by_user_id,
        metadata={
            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
            "hitl_request_id": request.id,
            "choice": normalized,
            "reason": reason,
            **({"run_id": run.id, "run_kind": RUN_KIND_CHILD, "agent_label": agent} if is_child else {}),
        },
    )
    if is_child:
        await sync_task_human_wait_status(db, task=task)
    return _resolution_metadata(tag, choice=normalized)


# ── cleanup ────────────────────────────────────────────────────────────


async def lock_agent_loop_input_requests(db: AsyncSession, *, task: Task) -> list[HitlRequest]:
    """A durable Task loop's open input requests; [] for legacy Task runners.

    Taken in the HitlRequest -> Task order of the terminal approval cleanup.
    """

    from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY

    run = (await db.execute(select(TaskCoordinatorRun).where(
        TaskCoordinatorRun.task_id == str(task.id),
    ))).scalar_one_or_none()
    if run is None or run.engine != TaskCoordinatorEngine.AGENT_LOOP.value:
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
    if expired and (task.workspace_id or task.conversation_id):
        from packages.core.constants.pending_actions import PendingActionKind
        from packages.core.models.task import Conversation, Message
        from packages.core.workspace_chat import service as chat_service

        ids = {request.id for request in expired}
        # Outside a Workspace the cards are in the Task's own conversation.
        card_scope = (
            (Conversation.entity_id == task.entity_id, Conversation.workspace_id == task.workspace_id)
            if task.workspace_id
            else (Conversation.id == task.conversation_id,)
        )
        rows = list((await db.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                *card_scope,
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
                    resolution={"choice": "expired", "note": reason}, publish_resolution=False,
                )
    return len(expired)


def request_child_run_id(request: Any) -> str | None:
    """The delegated child run a card belongs to (its question or its approval), if any."""

    context = request.context if isinstance(getattr(request, "context", None), dict) else {}
    if (
        getattr(request, "origin_kind", None) == ApprovalOriginKind.TASK.value
        and context.get("run_kind") == RUN_KIND_CHILD
        and context.get("run_id")
    ):
        return str(context["run_id"])
    tag = verified_request_tag(request)
    if tag is not None and tag_run_kind(tag) == RUN_KIND_CHILD:
        return str(tag["run_id"])
    return None


ENDED_CHILD_CARD_REASON = "agent_child_ended"

#: ``task_loop_wait.resolution`` of a child park whose card closed undecided.
CLOSED_RESOLUTION = "closed"

CLOSED_INPUT_INSTRUCTION = (
    "This question was closed without an answer. Continue without it if you can; "
    "otherwise say exactly what is blocked. Ask again only if the answer is still required."
)


def closed_input_result(request: HitlRequest) -> str:
    """The stored result of a question whose card closed without an answer."""

    return json.dumps({
        "status": "unanswered",
        "answer": "",
        "reason": str(request.resolved_reason or request.status or "closed")[:200],
        "instruction": CLOSED_INPUT_INSTRUCTION,
    }, ensure_ascii=False)


async def settle_closed_park(
    db: AsyncSession,
    *,
    task: Task,
    run: Any,
    wait: dict[str, Any],
    request: HitlRequest,
    spec: DurableRunSpec,
) -> bool:
    """Wake a run whose card is no longer pending although no decision reached it.

    ``run`` is the Task's root run (``spec`` the coordinator's) or a
    delegated child run. The caller holds HitlRequest -> Task -> root Run ->
    child rows. What the request says decides the pending call's result,
    exactly as the card would have:

    * an approval granted elsewhere (still unspent): the call re-runs with
      its approval token and spends the grant once;
    * denied: the host-written "rejected, did not run" gate result;
    * expired, withdrawn, spent elsewhere, or anything else: the "closed
      without approval, did not run" gate result (the model may ask again);
    * a question answered elsewhere (the answer is on the request): that
      answer; refused: the declined result; otherwise "closed unanswered".

    Only this run wakes. A root run's Task goes back in progress with it
    (``_resume_parked_run``); a child's Task follows its run tree
    (``sync_task_human_wait_status``). Returns True when the run was woken
    (or, while its Task is paused, when the result was recorded).
    """

    is_child = spec is not TASK_COORDINATOR_RUN_SPEC
    status = request.status
    context = request.context if isinstance(request.context, dict) else {}
    if wait.get("kind") == APPROVAL_WAIT_KIND:
        resumed = False
        if status == ApprovalStatus.GRANTED.value:
            parked = (run.checkpoint or {}).get(RUNTIME_CHECKPOINT_KEY) or {}
            arguments = _approved_call_arguments(request, parked.get("pending_tool_call") or {})
            if arguments is not None:
                resumed = await _resume_parked_run(
                    db, task=task, run=run, wait=wait, resolution="approved",
                    pending_arguments=arguments, reason="runtime_approval_approved",
                    decided_by=request.decided_by_user_id, spec=spec,
                )
        if not resumed and status != ApprovalStatus.GRANTED.value:
            denied = status == ApprovalStatus.DENIED.value
            reason = "rejected" if denied else str(request.resolved_reason or status or CLOSED_RESOLUTION)
            resumed = await _resume_parked_run(
                db, task=task, run=run, wait=wait,
                resolution="rejected" if denied else CLOSED_RESOLUTION,
                pending_tool_result={
                    "id": wait["tool_call_id"],
                    "content": json.dumps(approval_not_run_result(request, reason=reason), ensure_ascii=False),
                    "gate": True,
                },
                reason="runtime_approval_closed",
                decided_by=request.decided_by_user_id, spec=spec,
            )
    else:
        answer = context.get("answer") if isinstance(context.get("answer"), dict) else None
        payload: dict[str, Any] = {}
        response_text = ""
        if status in {ApprovalStatus.GRANTED.value, ApprovalStatus.CONSUMED.value} and answer is not None:
            payload = answer.get("payload") if isinstance(answer.get("payload"), dict) else {}
            response_text = str(answer.get("answer") or "")
            resolution = "answered"
            content = input_answer_result(
                response_text=response_text, payload=payload,
                answered_by=str(answer.get("answered_by") or "the user"),
            )
        elif status == ApprovalStatus.DENIED.value:
            resolution, payload = "declined", {"choice": "refuse"}
            content = input_answer_result(response_text="", payload=payload, answered_by="the user")
        else:
            resolution, content = CLOSED_RESOLUTION, closed_input_result(request)
        if wait.get("kind") == DELEGATED_INPUT_WAIT_KIND:
            # An inline-delegated agent's question (no pending call of its
            # own): the loop reads a note, as for an answer.
            note = (
                delegated_answer_note(wait=wait, response_text=response_text, payload=payload)
                if resolution != CLOSED_RESOLUTION
                else (
                    "The delegated agent's question was closed without an answer. Continue "
                    "without it if you can; otherwise say exactly what is blocked."
                )
            )
            resumed = await _resume_parked_run(
                db, task=task, run=run, wait=wait, resolution=resolution, note=note,
                reason="human_input_closed", decided_by=request.decided_by_user_id, spec=spec,
            )
        else:
            resumed = await _resume_parked_run(
                db, task=task, run=run, wait=wait, resolution=resolution,
                pending_tool_result={"id": wait["tool_call_id"], "content": content},
                reason="human_input_closed", decided_by=request.decided_by_user_id, spec=spec,
            )
    if not resumed:
        return False
    from packages.core.services.task_service import add_task_log

    what = "approval" if wait.get("kind") == APPROVAL_WAIT_KIND else "question"
    if is_child:
        agent = str(wait.get("agent_label") or "The delegated agent")
        line = f"{agent}'s {what} card closed ({request.status}); {agent} continues."
    else:
        line = f"The Task's {what} card closed ({request.status}); the Task continues."
    await add_task_log(
        db, task.id, TaskLogType.AI_AGENT_TURN, line,
        actor=TaskActor.SYSTEM,
        metadata={
            "engine": TaskCoordinatorEngine.AGENT_LOOP.value,
            "hitl_request_id": request.id,
            "request_status": request.status,
            "run_id": run.id,
            "run_kind": RUN_KIND_CHILD if is_child else RUN_KIND_ROOT,
            **({"agent_label": str(wait.get("agent_label") or "")} if is_child else {}),
        },
    )
    if is_child:
        await sync_task_human_wait_status(db, task=task)
    return True


#: Set while a Task's own terminal cleanup closes its cards: its runs stop
#: with it (reconcile), so a closed card must not wake them.
_CLOSING_FOR_TERMINAL_TASK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "task_agent_loop_closing_for_terminal_task", default=False,
)


@contextlib.contextmanager
def closing_for_terminal_task():
    """Close a terminal Task's cards without waking the runs parked on them."""

    token = _CLOSING_FOR_TERMINAL_TASK.set(True)
    try:
        yield
    finally:
        _CLOSING_FOR_TERMINAL_TASK.reset(token)


async def wake_run_for_closed_request(db: AsyncSession, request: Any) -> bool:
    """A Task agent loop run's card left PENDING without its decision reaching the run.

    Called in the same transaction by every writer that closes a request
    undecided (expiry, withdrawal, an approval that can no longer run),
    right after it set the request's status; the caller holds the request
    (HitlRequest -> Task -> root Run -> child rows follow here). The run
    parked on exactly this request (the Task's root run, or the delegated
    child the request names) wakes with the outcome the request carries
    (``settle_closed_park``). A request no run waits on, a terminal Task:
    nothing to do. The recovery sweep (``recover_runs_on_closed_cards_once``)
    is the backstop for any close path that did not call this.
    """

    if (
        _CLOSING_FOR_TERMINAL_TASK.get()
        or getattr(request, "status", None) == ApprovalStatus.PENDING.value
        or not getattr(request, "origin_task_id", None)
    ):
        return False
    run_id = request_child_run_id(request)
    task = (await db.execute(
        select(Task).where(
            Task.id == request.origin_task_id, Task.entity_id == request.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or task.status in TERMINAL_STATUSES:
        return False
    root = await task_agent_loop_run(db, str(task.id), for_update=True)
    if root is None:
        return False
    if run_id is None:
        wait = parked_human_wait(root)
        if wait is None or wait.get("hitl_request_id") != request.id:
            return False
        return await settle_closed_park(
            db, task=task, run=root, wait=wait, request=request, spec=TASK_COORDINATOR_RUN_SPEC,
        )
    child = next(
        (row for row in await lock_live_task_children(db, str(task.id)) if row.id == run_id), None,
    )
    wait = parked_child_human_wait(child) if child is not None else None
    if wait is None or wait.get("hitl_request_id") != request.id:
        return False
    return await settle_closed_park(db, task=task, run=child, wait=wait, request=request, spec=_child_spec())


async def close_ended_child_cards(db: AsyncSession, *, task_id: str, reason: str = ENDED_CHILD_CARD_REASON) -> int:
    """Expire the open cards of a Task's delegated child runs that have ended.

    A child stopped by its parent or its Task can no longer be resumed by its
    question or approval. Runs in its own transaction after the one that
    stopped the child (the approval-boundary -> HitlRequest -> Task lock
    order does not allow it inside), and from the recovery sweep as the
    backstop. Approving such a card later finds no park either and expires
    it. Returns how many cards it closed.
    """

    from packages.core.constants.approvals import APPROVAL_LIVE_STATUSES
    from packages.core.models.task_agent_child import (
        TASK_AGENT_CHILD_TERMINAL_PHASES,
        TaskAgentChildRun,
    )
    from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY
    from packages.core.services.task_runtime_approvals import (
        _expire_locked_runtime_request,
        lock_task_runtime_approval_boundary,
    )

    from sqlalchemy import or_

    await lock_task_runtime_approval_boundary(db, task_id=task_id)
    # Only child-owned cards are locked (a question naming its run, or a v2
    # approval tag): the Task's other requests stay free for their own
    # resolvers (e.g. answer_task_blocker takes HitlRequest -> Task).
    requests = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.origin_task_id == task_id,
            HitlRequest.status.in_(tuple(status.value for status in APPROVAL_LIVE_STATUSES)),
            HitlRequest.origin_kind.in_((ApprovalOriginKind.TASK.value, ApprovalOriginKind.TOOL_CALL.value)),
            or_(
                HitlRequest.context["run_kind"].astext == RUN_KIND_CHILD,
                HitlRequest.context[(TASK_AGENT_LOOP_HITL_CONTEXT_KEY, "run_kind")].astext == RUN_KIND_CHILD,
            ),
        ).order_by(HitlRequest.id.asc()).with_for_update().execution_options(populate_existing=True)
    )).scalars().all())
    owned = {
        request.id: run_id for request in requests
        if (run_id := request_child_run_id(request)) is not None
        and (
            request.origin_kind == ApprovalOriginKind.TOOL_CALL.value
            or (
                request.action_key == TASK_INPUT_ACTION_KEY
                and request.status == ApprovalStatus.PENDING.value
            )
        )
    }
    if not owned:
        return 0
    task = (await db.execute(
        select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        return 0
    live = set((await db.execute(
        select(TaskAgentChildRun.id).where(
            TaskAgentChildRun.task_id == task_id,
            TaskAgentChildRun.id.in_(sorted(set(owned.values()))),
            TaskAgentChildRun.phase.not_in(tuple(TASK_AGENT_CHILD_TERMINAL_PHASES)),
        )
    )).scalars().all())
    ended = [request for request in requests if request.id in owned and owned[request.id] not in live]
    closed = 0
    questions = [request for request in ended if request.origin_kind == ApprovalOriginKind.TASK.value]
    if questions:
        closed += await close_agent_loop_input_requests(db, task=task, requests=questions, reason=reason)
    for request in ended:
        if request.origin_kind == ApprovalOriginKind.TOOL_CALL.value and await _expire_locked_runtime_request(
            db, request, reason=reason,
        ):
            closed += 1
    return closed


async def expire_task_agent_loop_input_requests(db: AsyncSession, *, task: Task, reason: str) -> int:
    """Backstop: close a terminal agent-loop Task's input cards in their own transaction."""

    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    await lock_task_runtime_approval_boundary(db, task_id=str(task.id))
    requests = await lock_agent_loop_input_requests(db, task=task)
    with closing_for_terminal_task():
        return await close_agent_loop_input_requests(db, task=task, requests=requests, reason=reason)


__all__ = [
    "APPROVAL_WAIT_KIND",
    "CHILD_APPROVAL_TAG_VERSION",
    "DELEGATED_INPUT_WAIT_KIND",
    "ENDED_CHILD_CARD_REASON",
    "HUMAN_NOTE_KEY",
    "HUMAN_WAIT_KINDS",
    "INPUT_WAIT_KIND",
    "INPUT_WAIT_KINDS",
    "ROOT_APPROVAL_TAG_VERSION",
    "RUN_KIND_CHILD",
    "RUN_KIND_ROOT",
    "CLOSED_RESOLUTION",
    "close_ended_child_cards",
    "closing_for_terminal_task",
    "settle_closed_park",
    "wake_run_for_closed_request",
    "input_parks",
    "parked_child_human_wait",
    "request_child_run_id",
    "sync_task_human_wait_status",
    "tag_run_kind",
    "tree_waits_on_a_person",
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
    "lock_live_task_children",
    "human_wait",
    "input_answer_result",
    "parked_human_wait",
    "rebind_task_lifecycle",
    "runtime_checkpoint_digest",
    "stage_task_agent_loop_approval_resolution",
    "task_agent_loop_approval_is_current",
    "task_agent_loop_run",
    "task_agent_loop_waiting_on_input",
    "verified_request_tag",
]
