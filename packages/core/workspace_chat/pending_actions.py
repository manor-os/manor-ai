"""Read-only counts of distinct pending decisions; never change chat or HITL state."""
from __future__ import annotations

from hashlib import sha256

from sqlalchemy import and_, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import aliased, load_only

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.pending_actions import PendingActionKind

from packages.core.constants.execution import COORDINATOR_CHILD_PARAM_KEY
from packages.core.governance.approvals import is_coordinator_owned_nested_approval
from packages.core.models.execution import ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment
from packages.core.models.proposal import ProposalRecord
from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services.workflow_chat_projection import _workflow_business_state


def _sources(message: Message):
    action = message.pending_action if isinstance(message.pending_action, dict) else {}
    meta = message.meta if isinstance(message.meta, dict) else {}
    raw_requests = meta.get("hitl_requests")
    requests = [
        request for request in raw_requests if isinstance(request, dict) and request.get("id")
    ] if isinstance(raw_requests, list) else []
    mirror_ids = set()
    if action.get("kind"):
        # A card's metadata mirror uses its request/token or its message ID.
        # Share the card's identity and resolution without swallowing other
        # independent requests carried by the same message.
        mirror_ids = {
            str(action[key]) for key in ("approval_request_id", "approval_token") if action.get(key)
        } or {str(message.id)}
        mirror_resolved = any(
            str(request["id"]) in mirror_ids and request.get("resolved") for request in requests
        )
        if message.resolved_at is None and not mirror_resolved:
            yield action, str(action.get("approval_request_id") or action.get("approval_token") or "")
    for request in requests:
        if str(request["id"]) not in mirror_ids and not request.get("resolved"):
            yield request, str(request["id"])


def _identity(message_id: str | None, action: dict, request_id: str) -> str:
    kind = str(action.get("kind") or "hitl")
    if kind == PendingActionKind.APPROVE_PROPOSALS and action.get("review_id"):
        return f"approve_proposals:review_id:{action['review_id']}"
    if request_id:
        return f"request:{request_id}"
    if action.get("workflow_run_id"):
        return f"{kind}:{action['workflow_run_id']}:{action.get('step_id') or ''}"
    for key in ("review_id", "draft_id", "commitment_id", "step_id", "task_id"):
        if action.get(key):
            return f"{kind}:{key}:{action[key]}"
    return f"message:{message_id}"


async def pending_actions_by_workspace(
    db, *, entity_id: str, workspace_ids: list[str],
) -> dict[str, dict[str, dict]]:
    """Project canonical decisions and their chat mirrors without resolving them."""
    counts: dict[str, dict[str, dict]] = {wid: {} for wid in workspace_ids}
    if not workspace_ids:
        return counts
    messages = (await db.execute(
        select(Message, Conversation.workspace_id).options(load_only(
            Message.id, Message.pending_action, Message.meta, Message.resolved_at,
        )).join(Conversation, Conversation.id == Message.conversation_id).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id.in_(workspace_ids),
            or_(
                and_(Message.resolved_at.is_(None), Message.pending_action["kind"].as_string().isnot(None)),
                func.jsonb_path_exists(
                    Message.meta,
                    cast('$.hitl_requests[*] ? (exists(@.id) && (!exists(@.resolved) || @.resolved == false))', JSONPATH),
                ),
            ),
        )
    )).all()
    candidates = [
        (message.id, wid, action, request_id)
        for message, wid in messages
        for action, request_id in _sources(message)
    ]
    request_ids = {entry[3] for entry in candidates if entry[3]}
    requests = {
        request.id: request
        for request in (await db.execute(select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            or_(HitlRequest.id.in_(request_ids), and_(
                HitlRequest.workspace_id.in_(workspace_ids), HitlRequest.status == ApprovalStatus.PENDING,
            )),
        ))).scalars()
    }
    child_step_ids = {request.origin_step_id for request in requests.values()
                      if request.origin_kind == "tool_call" and request.origin_step_id}
    coordinator_steps = set((await db.execute(select(ExecutionStep.id).where(
        ExecutionStep.entity_id == entity_id,
        ExecutionStep.id.in_(child_step_ids),
        ExecutionStep.params[COORDINATOR_CHILD_PARAM_KEY].as_boolean().is_(True),
    ))).scalars()) if child_step_ids else set()
    suppressed = {
        request.id for request in requests.values()
        if request.status != ApprovalStatus.PENDING
        or is_coordinator_owned_nested_approval(request)
        or (request.origin_kind == "tool_call" and request.origin_step_id in coordinator_steps)
    }
    # An approval whose Workflow run has ended can never be decided into
    # anything; counting it shows "needs you" with nothing to open.
    from packages.core.services.workflow_runtime_approvals import ENDED_RUN_STATUSES, workflow_approval_run_id

    approval_runs = {request.id: workflow_approval_run_id(request) for request in requests.values()}
    if any(approval_runs.values()):
        ended_runs = set((await db.execute(select(WorkflowRun.id).where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.id.in_({run_id for run_id in approval_runs.values() if run_id}),
            WorkflowRun.status.in_(tuple(ENDED_RUN_STATUSES)),
        ))).scalars())
        suppressed |= {request_id for request_id, run_id in approval_runs.items() if run_id in ended_runs}
    # Likewise a request whose Task was cancelled (a failed Task keeps its
    # recovery card, a completed one its review).
    task_ids = {request.origin_task_id for request in requests.values() if request.origin_task_id}
    if task_ids:
        from packages.core.constants.task import TaskStatus
        from packages.core.models.task import Task

        cancelled_tasks = set((await db.execute(select(Task.id).where(
            Task.entity_id == entity_id, Task.id.in_(task_ids), Task.status == TaskStatus.CANCELLED.value,
        ))).scalars())
        suppressed |= {request.id for request in requests.values() if request.origin_task_id in cancelled_tasks}

    # Missing message mirrors do not make a canonical decision disappear.
    represented = {(wid, request_id) for _, wid, _, request_id in candidates}
    # An Agent's request_input question is saved as its own chat message (the
    # live card exists only while the turn streams); that message is its card.
    unmirrored = {
        request.id: request for request in requests.values()
        if request.status == ApprovalStatus.PENDING and request.workspace_id in counts
        and (request.workspace_id, request.id) not in represented
    }
    if unmirrored:
        question_of = Message.meta["runtime_input_request_id"].as_string()
        for message_id, wid, request_id in (await db.execute(
            select(Message.id, Conversation.workspace_id, question_of)
            .join(Conversation, Conversation.id == Message.conversation_id).where(
                Conversation.entity_id == entity_id, Conversation.workspace_id.in_(workspace_ids),
                question_of.in_(list(unmirrored)),
                Message.meta["runtime_input_role"].as_string() == "question",
            )
        )).all():
            request = unmirrored[request_id]
            if wid != request.workspace_id:
                continue
            question = (request.context if isinstance(request.context, dict) else {}).get("question") or {}
            candidates.append((message_id, wid, {
                "kind": "runtime_input", "title": str(question.get("question") or "").strip(),
            }, request_id))
            represented.add((wid, request_id))
    cohort_actions = {}
    for request in requests.values():
        context = request.context if isinstance(request.context, dict) else {}
        if context.get("plane") == "strategist_proposal" and context.get("review_id"):
            # Task/experiment/change requests share one proposal review.
            cohort_actions[request.id] = {"kind": PendingActionKind.APPROVE_PROPOSALS.value, "review_id": context["review_id"]}
        if request.workspace_id in counts and (request.workspace_id, request.id) not in represented:
            candidates.append((None, request.workspace_id, {"kind": context.get("pending_kind") or "hitl"}, request.id))
    candidates = [(mid, wid, cohort_actions.get(rid, action), rid) for mid, wid, action, rid in candidates]

    # Available durable owner state excludes stale legacy cards; pre-unified
    # cards without an owner retain their explicit resolution and identity.
    owners = {}
    for key, model, column in (
        ("review_id", ProposalRecord, ProposalRecord.review_id),
        ("draft_id", WorkspaceOperationDraft, WorkspaceOperationDraft.id),
        ("commitment_id", HumanCommitment, HumanCommitment.id),
        ("workflow_run_id", WorkflowRun, WorkflowRun.id),
    ):
        ids = {action[key] for _, _, action, _ in candidates if action.get(key)}
        owners[key] = {
            getattr(owner, column.key): owner
            for owner in (await db.execute(select(model).where(
                model.entity_id == entity_id, column.in_(ids),
            ))).scalars()
        } if ids else {}
    run_ids = set(owners["workflow_run_id"])
    retry_parent = func.coalesce(
        WorkflowRun.retry_of_run_id, WorkflowRun.trigger_data["retry_of_run_id"].as_string(),
    )
    superseded_runs = set((await db.execute(select(retry_parent).where(
        WorkflowRun.entity_id == entity_id,
        WorkflowRun.workspace_id.in_(workspace_ids),
        retry_parent.in_(run_ids),
    ))).scalars()) if run_ids else set()
    if run_ids:
        superseded_runs |= set((await db.execute(select(WorkflowRun.id).where(
            WorkflowRun.entity_id == entity_id, WorkflowRun.id.in_(run_ids), ended_run_replaced(WorkflowRun),
        ))).scalars())

    for message_id, wid, action, request_id in candidates:
        request = requests.get(request_id)
        if request is not None:
            if request.workspace_id != wid or request_id in suppressed:
                continue
        elif action.get("approval_request_id"):
            continue
        if action.get("workflow_run_id") in superseded_runs or not _owner_is_actionable(action, wid, owners):
            continue
        identity = _identity(message_id, action, request_id)
        bucket = {
            PendingActionKind.APPROVE_PROPOSALS.value: "proposal_actions",
            PendingActionKind.RETRY_STRATEGIST_REVIEW.value: "failed_actions",
        }.get(action.get("kind"), "chat_pending_actions")
        decision = counts[wid].setdefault(identity, {
            "bucket": bucket, "messages": {}, "occurrences": set(), "request_ids": set(),
            # What the decision is when its message carries no card of its own.
            "action": action,
        })
        if message_id:
            decision["messages"].setdefault(message_id, set()).add(request_id or message_id)
        decision["occurrences"].add(request_id or message_id or identity)
        if request_id:
            decision["request_ids"].add(request_id)
    for decisions in counts.values():
        for identity, decision in decisions.items():
            # A new decision occurrence must notify again, while request mirrors
            # share one receipt even when a new mirror is projected later.
            occurrence = decision["request_ids"] or decision["occurrences"]
            decision["key"] = sha256((identity + "|" + "|".join(sorted(occurrence))).encode()).hexdigest()
    return counts


async def count_pending_actions_by_workspace(db, *, entity_id: str, workspace_ids: list[str]):
    decisions = await pending_actions_by_workspace(db, entity_id=entity_id, workspace_ids=workspace_ids)
    return {wid: pending_action_counts(items) for wid, items in decisions.items()}


PENDING_ACTION_BUCKETS = ("chat_pending_actions", "proposal_actions", "failed_actions")


def pending_action_counts(decisions: dict) -> dict[str, int]:
    counts = dict.fromkeys(PENDING_ACTION_BUCKETS, 0)
    for decision in decisions.values():
        counts[decision["bucket"]] += 1
    return counts


def pending_action_total(counts: dict) -> int:
    """What needs the person: the sidebar badge, the chat banner, ``pending_actions``."""
    return sum(counts[bucket] for bucket in PENDING_ACTION_BUCKETS)


def ended_run_replaced(run):
    """An ended run whose Flow has run again since: its cards need nothing.

    A newer run of the same Flow from the same entry replaces it, whatever that
    run's outcome, so a Flow that keeps failing is one problem: its latest run.
    """
    from packages.core.services.workflow_runtime_approvals import ENDED_RUN_STATUSES

    newer = aliased(WorkflowRun)
    return and_(run.status.in_(tuple(ENDED_RUN_STATUSES)), exists().where(
        newer.entity_id == run.entity_id,
        newer.workspace_id == run.workspace_id,
        newer.workflow_id == run.workflow_id,
        newer.binding_id.is_not_distinct_from(run.binding_id),
        or_(newer.created_at > run.created_at, and_(newer.created_at == run.created_at, newer.id > run.id)),
    ))


def _owner_is_actionable(action: dict, workspace_id: str, owners: dict) -> bool:
    for key, status in (("draft_id", "open"), ("commitment_id", "waiting")):
        owner = owners[key].get(action.get(key))
        if owner is not None and (owner.workspace_id != workspace_id or owner.status != status):
            return False
    if action.get("kind") == PendingActionKind.APPROVE_PROPOSALS:
        proposal = owners["review_id"].get(action.get("review_id"))
        if proposal is not None and (proposal.workspace_id != workspace_id or proposal.status != "open"):
            return False
    run = owners["workflow_run_id"].get(action.get("workflow_run_id"))
    if run is not None:
        if run.workspace_id != workspace_id:
            return False
        kind = action.get("kind")
        if kind == PendingActionKind.WORKFLOW_RETRY:
            # The projection offers retry after a failed run or a completed
            # run whose business result still needs input.
            return run.status == "failed" or (
                run.status == "completed" and _workflow_business_state(run)[0] == "needs_input"
            )
        if kind == PendingActionKind.WORKFLOW_STARTER_INPUT:
            return run.status == "paused" and not run.step_results
        if kind in {PendingActionKind.WORKFLOW_INPUT, PendingActionKind.WORKFLOW_APPROVAL}:
            return run.status == "paused" and run.current_step_id == action.get("step_id")
        # Any other card a run raised can only be answered while the run lives.
        from packages.core.services.workflow_runtime_approvals import ENDED_RUN_STATUSES

        return run.status not in ENDED_RUN_STATUSES
    return True
