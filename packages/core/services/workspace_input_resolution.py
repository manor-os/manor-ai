"""One transaction for Workspace input cards, shared by Chat tools and HTTP.

Only ordinary input belongs here. Approval, login and effect reconciliation
retain their own decision contracts. Callers commit before publishing wakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import Workspace

INPUT_KINDS = frozenset({PendingActionKind.HUMAN_INPUT, PendingActionKind.NEEDS_INPUT})
INPUT_CHOICES = frozenset({"respond", "provide_answers", "submit", "ok"})


class WorkspaceInputResolutionError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class WorkspaceInputResolution:
    message: Message
    changed: bool = False
    plan_id: str | None = None
    task_dispatch: tuple[str, str | None] | None = None
    strategist_dispatch: dict[str, str] | None = None


def human_commitment_id(message: Message, pending: dict) -> str | None:
    if pending.get("commitment_id"):
        return str(pending["commitment_id"])
    for ref in message.refs or []:
        if isinstance(ref, dict) and ref.get("type") == "human_commitment" and ref.get("id"):
            return str(ref["id"])
    return None


def _has_answer(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_answer(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_answer(item) for item in value)
    return value is not None


async def resolve_workspace_input_message(
    db, *, workspace_id: str, conversation_id: str, message_id: str,
    user, choice: str = "respond", response: str = "", payload: dict | None = None,
    require_user_reply: bool = False,
) -> WorkspaceInputResolution:
    """Validate exact origin, save an answer and resume once; never commit."""
    from packages.core.services.workspace_access import user_can_read_workspace
    from packages.core.services.runtime_authorization import authorize_hitl_action

    if choice not in INPUT_CHOICES:
        raise WorkspaceInputResolutionError(422, "Use respond to answer an input request, not approve/reject.")
    data = dict(payload or {})
    text = str(response or data.get("response") or "").strip()
    if not text and not any(_has_answer(data.get(key)) for key in ("fields", "answers", "attachments")):
        raise WorkspaceInputResolutionError(422, "A response, form answers or attachments are required.")
    data["response"] = text
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.entity_id == user.entity_id,
        Workspace.deleted_at.is_(None),
    ))
    if workspace is None or not await user_can_read_workspace(db, workspace=workspace, user=user):
        raise WorkspaceInputResolutionError(404, "Workspace not found.")
    if workspace.status not in {"active", "paused"}:
        raise WorkspaceInputResolutionError(409, "This Workspace is no longer active.")
    message_query = select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
        Message.id == message_id, Message.conversation_id == conversation_id,
        Conversation.workspace_id == workspace_id, Conversation.entity_id == user.entity_id,
    )
    preview = await db.scalar(message_query)
    if preview is None:
        raise WorkspaceInputResolutionError(404, "Input request not found in this conversation.")
    pending = dict(preview.pending_action or {})
    kind = pending.get("kind")
    if kind not in INPUT_KINDS or pending.get("simulation_runtime"):
        raise WorkspaceInputResolutionError(422, "This card is not an ordinary input request. Use its decision controls.")
    if pending.get("approval_token") or pending.get("hitl_type") not in {None, "", HitlType.INPUT.value}:
        raise WorkspaceInputResolutionError(422, "This card requires a decision, not an ordinary reply.")
    authority = await authorize_hitl_action(
        db, entity_id=user.entity_id, workspace_id=workspace_id, by_user_id=user.id,
        action_key=pending.get("action"), capability_id=pending.get("capability_id"),
        hitl_type=HitlType.INPUT.value, origin_conversation_id=conversation_id,
    )
    if not authority.allowed:
        raise WorkspaceInputResolutionError(403, authority.reason or "You cannot answer this request.")

    # Other response surfaces start from the owning Task/commitment and then
    # close its Message. Use the same order instead of introducing a cycle.
    task = None
    commitment = None
    commitment_id = human_commitment_id(preview, pending)
    if pending.get("task_id") and not pending.get("step_id"):
        # A Task's input card: the per-Task approval boundary first, as the
        # terminal path (boundary -> input request -> Task) and the parks take
        # it, so an answer never holds the Task while they hold the request.
        from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

        await lock_task_runtime_approval_boundary(db, task_id=str(pending["task_id"]))
        task = await db.scalar(select(Task).where(
            Task.id == pending["task_id"], Task.entity_id == user.entity_id,
            Task.workspace_id == workspace_id,
        ).with_for_update().execution_options(populate_existing=True))
        if task is None:
            raise WorkspaceInputResolutionError(404, "Task not found in this Workspace.")
    elif commitment_id and not pending.get("step_id"):
        commitment = await db.scalar(select(HumanCommitment).where(
            HumanCommitment.id == commitment_id, HumanCommitment.workspace_id == workspace_id,
            HumanCommitment.entity_id == user.entity_id,
        ).with_for_update().execution_options(populate_existing=True))

    # Match the card resolver's request -> Message -> Task/Plan/Step order.
    request = None
    if pending.get("step_id") and pending.get("approval_request_id"):
        request = await db.scalar(select(HitlRequest).where(
            HitlRequest.id == pending["approval_request_id"],
            HitlRequest.entity_id == user.entity_id, HitlRequest.workspace_id == workspace_id,
            HitlRequest.origin_kind == ApprovalOriginKind.LEASE.value,
            HitlRequest.origin_step_id == pending["step_id"],
            HitlRequest.origin_plan_id == pending.get("plan_id"),
        ).with_for_update().execution_options(populate_existing=True))
        if request is None or request.hitl_type != HitlType.INPUT.value:
            raise WorkspaceInputResolutionError(409, "The input request no longer matches this card.")
        if pending.get("task_id") and request.origin_task_id != pending["task_id"]:
            raise WorkspaceInputResolutionError(409, "The input request task does not match this card.")
        context = request.context if isinstance(request.context, dict) else {}
        from packages.core.constants.execution import COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY
        if COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY in context:
            raise WorkspaceInputResolutionError(422, "This request requires an explicit approval decision, not a text response.")
        if context.get("pending_kind") and context["pending_kind"] != kind:
            raise WorkspaceInputResolutionError(409, "The input request type does not match this card.")
    task_request = None
    if task is not None:
        from packages.core.services.task_chat_hitl import TASK_INPUT_ACTION_KEY
        task_request = await db.scalar(select(HitlRequest).where(
            HitlRequest.id == pending.get("approval_request_id"),
            HitlRequest.entity_id == user.entity_id, HitlRequest.workspace_id == workspace_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
            HitlRequest.origin_task_id == task.id, HitlRequest.hitl_type == HitlType.INPUT.value,
            HitlRequest.action_key == TASK_INPUT_ACTION_KEY,
        ).with_for_update().execution_options(populate_existing=True))
        if task_request is None:
            raise WorkspaceInputResolutionError(409, "This task input request is stale. Use its current input card.")
    message = await db.scalar(message_query.with_for_update(of=Message).execution_options(populate_existing=True))
    if message is None or message.pending_action != pending:
        raise WorkspaceInputResolutionError(409, "The input request changed. Refresh it before replying.")
    result = WorkspaceInputResolution(message)
    if message.resolved_at is not None:
        return result
    if request is not None and request.status != ApprovalStatus.PENDING.value:
        raise WorkspaceInputResolutionError(409, "This input request has already been answered.")
    if task_request is not None and task_request.status != ApprovalStatus.PENDING.value:
        raise WorkspaceInputResolutionError(409, "This task input request has already been answered.")
    if require_user_reply:
        reply = await db.scalar(select(Message.id).where(
            Message.conversation_id == conversation_id, Message.author_kind == "user",
            Message.meta["author_user_id"].as_string() == user.id, Message.created_at > message.created_at,
        ).limit(1))
        if reply is None:
            raise WorkspaceInputResolutionError(409, "Wait for this user to answer the input request before resolving it.")
    if "attachments" in data:
        from fastapi import HTTPException
        from packages.core.services.file_context import resolve_workspace_input_attachments
        try:
            data["attachments"] = await resolve_workspace_input_attachments(
                db, entity_id=user.entity_id, workspace_id=workspace_id,
                user_id=user.id, attachments=data["attachments"],
            )
        except HTTPException as exc:
            raise WorkspaceInputResolutionError(exc.status_code, str(exc.detail)) from exc

    if pending.get("step_id"):
        from packages.core.governance.approvals import ApprovalAuthorityError, consume_approval, grant_approval
        from packages.core.services.step_resume import resume_step_for_retry
        # This INPUT grant records consumption of an answer, never a tool
        # authorization. Nested runtime binding is validated by grant_approval.
        if request is not None:
            try:
                await grant_approval(db, request, by_user_id=user.id, via="chat_card")
            except ApprovalAuthorityError as exc:
                raise WorkspaceInputResolutionError(403, str(exc)) from exc
            await consume_approval(db, request)
        structured = kind == PendingActionKind.NEEDS_INPUT
        result.plan_id = await resume_step_for_retry(
            db, entity_id=user.entity_id, user_id=user.id, workspace_id=workspace_id,
            step_id=pending["step_id"], plan_id=pending.get("plan_id"),
            task_id=request.origin_task_id if request else pending.get("task_id"),
            params_update={"answers": data.get("answers") or data.get("fields") or data} if structured else None,
            human_input_response=None if structured else {
                **data, "choice": choice, "note": text, "user": user.display_name or user.email,
            }, enqueue=False,
        )
        if not result.plan_id:
            raise WorkspaceInputResolutionError(409, "This step is no longer waiting for an answer.")
    elif pending.get("task_id"):
        from packages.core.services.task_input_resume import apply_task_input_answer
        from packages.core.workspace_chat.service import resolve_pending_action
        # The Task helper also projects generic card receipts. Persist the
        # complete answer first, as on the commitment lane, in this transaction.
        await resolve_pending_action(db, message_id=message.id, user_id=user.id,
                                     resolution={"choice": choice, "note": text, "payload": data})
        applied = await apply_task_input_answer(
            db, task=task, user_id=user.id, user_label=user.display_name or user.email,
            response_text=text, payload={**data, "choice": choice, "source": "workspace_chat"},
        )
        if not applied["resumed"]:
            raise WorkspaceInputResolutionError(409, "This task is no longer waiting for an answer.")
        result.task_dispatch = (task.id, applied["dispatch_agent_id"])
    elif commitment_id:
        from packages.core.humans import participant_can, resolve_commitment
        from packages.core.services.human_queue_resume import workspace_awaits_proposal_decision
        if commitment is None or commitment.status != "waiting":
            raise WorkspaceInputResolutionError(409, "This human request is no longer waiting for an answer.")
        if commitment.participant_id and commitment.participant_id != user.id:
            raise WorkspaceInputResolutionError(403, "This request is assigned to another participant.")
        if not commitment.participant_id and commitment.role_required and not await participant_can(
            db, user=user, entity_id=user.entity_id, workspace_id=workspace_id, permission_key="approve_tasks",
        ):
            raise WorkspaceInputResolutionError(403, "You do not have the required participant role.")
        # Close the exact Message first so the commitment projection does not
        # replace this response with its generic terminal card receipt.
        from packages.core.workspace_chat.service import resolve_pending_action
        await resolve_pending_action(db, message_id=message.id, user_id=user.id,
                                     resolution={"choice": choice, "note": text, "payload": data})
        await resolve_commitment(db, commitment, status="fulfilled", participant_id=user.id,
                                 response={**data, "text": text, "choice": choice})
        remaining = await db.scalar(select(func.count()).select_from(HumanCommitment).where(
            HumanCommitment.workspace_id == workspace_id, HumanCommitment.entity_id == user.entity_id,
            HumanCommitment.status == "waiting",
        ))
        if not remaining and not await workspace_awaits_proposal_decision(db, workspace_id):
            result.strategist_dispatch = dict(workspace_id=workspace_id, user_id=user.id,
                                             commitment_id=commitment.id, status="fulfilled")
    elif kind == PendingActionKind.NEEDS_INPUT:
        raise WorkspaceInputResolutionError(409, "This form has no resumable step.")

    from packages.core.workspace_chat.service import resolve_pending_action
    await resolve_pending_action(db, message_id=message.id, user_id=user.id,
                                 resolution={"choice": choice, "note": text, "payload": data})
    result.changed = True
    return result


async def dispatch_workspace_input_resolution(db, result: WorkspaceInputResolution) -> None:
    """Publish hints only after the caller has committed the response."""
    if not result.changed:
        return
    if result.strategist_dispatch:
        from packages.core.services.human_queue_resume import dispatch_strategist_for_cleared_human_queue
        dispatch_strategist_for_cleared_human_queue(**result.strategist_dispatch)
    if result.task_dispatch:
        from packages.core.services.task_input_resume import dispatch_task_input_resume
        dispatch_task_input_resume(*result.task_dispatch)
    if result.plan_id:
        from packages.core.plans.wakeup import hint_pending_plan_cycle
        await hint_pending_plan_cycle(db, result.plan_id, reason="workspace_input_post_commit_hint")
