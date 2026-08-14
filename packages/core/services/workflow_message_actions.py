"""Conversation-agnostic resolution for persisted Workflow action cards."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import (
    WORKFLOW_RUN_ACTION_KINDS,
    PendingActionKind,
)
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.services.hitl_options import APPROVAL_CHOICE_ALWAYS_APPROVE
from packages.core.services.workspace_access import user_can_control_workspace_run
from packages.core.workspace_chat import service as chat_service


_CANCEL_CHOICES = {"cancel", "reject", "rejected", "decline", "deny", "no", "skip"}
_WORKFLOW_WAIT_KINDS = {
    PendingActionKind.WORKFLOW_APPROVAL.value,
    PendingActionKind.WORKFLOW_INPUT.value,
}


class WorkflowMessageActionError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class WorkflowMessageActionResult:
    message: Message
    run: WorkflowRun
    workspace_id: str


def _fail(status_code: int, detail: Any) -> None:
    raise WorkflowMessageActionError(status_code, detail)


def _run_origin_context(run: WorkflowRun) -> dict[str, Any]:
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    context = trigger_data.get("_workflow_chat_origin")
    if not isinstance(context, dict):
        context = trigger_data.get("_workspace_chat_entrypoint")
    return context if isinstance(context, dict) else {}


async def _load_controlled_run(
    db: AsyncSession,
    *,
    message: Message,
    conversation: Conversation,
    pending_action: dict[str, Any],
    user: User,
) -> WorkflowRun:
    run_id = str(pending_action.get("workflow_run_id") or "")
    run = (await db.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.entity_id == user.entity_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if run is None or not run.workspace_id:
        _fail(404, "Workflow Run not found")
    if pending_action.get("workflow_binding_id") and str(run.binding_id or "") != str(
        pending_action["workflow_binding_id"]
    ):
        _fail(404, "Workflow Run not found")
    if conversation.id != message.conversation_id or conversation.entity_id != user.entity_id:
        _fail(404, "message not found")
    if (
        not conversation.workspace_id
        and conversation.user_id
        and str(conversation.user_id) != str(user.id)
    ):
        _fail(404, "message not found")
    if conversation.workspace_id and str(conversation.workspace_id) != str(run.workspace_id):
        _fail(409, "Workflow Run belongs to another Workspace")
    if str(_run_origin_context(run).get("conversation_id") or "") != str(conversation.id):
        _fail(409, "Workflow Run does not belong to this Chat")
    if not await user_can_control_workspace_run(
        db,
        run=run,
        user_id=user.id,
        entity_role=user.role,
    ):
        _fail(403, "Workflow Run control permission required")
    return run


async def _project_run(db: AsyncSession, run: WorkflowRun) -> None:
    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    await project_workflow_run_status(db, run=run)


async def _resolve_starter(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    pending_action: dict[str, Any],
    choice: str,
    payload: dict[str, Any] | None,
    user: User,
) -> str | None:
    from packages.core.services.workspace_workflow_router import (
        assemble_workspace_workflow_inputs,
        get_workspace_chat_entrypoint,
        preserve_server_captured_workflow_inputs,
        validate_workspace_workflow_inputs,
    )

    if run.status != "paused" or run.step_results:
        _fail(409, "Workflow Run is no longer waiting for its inputs")
    if choice in _CANCEL_CHOICES:
        run.status = "cancelled"
        run.completed_at = datetime.now(timezone.utc)
        await _project_run(db, run)
        return None
    if choice not in {"run", "start", "submit", "confirm"}:
        _fail(400, "Unsupported Workflow input choice")
    resolved_entrypoint = await get_workspace_chat_entrypoint(
        db,
        entity_id=user.entity_id,
        workspace_id=str(run.workspace_id),
        binding_id=str(pending_action.get("workflow_binding_id") or ""),
    )
    if resolved_entrypoint is None:
        _fail(404, "Workflow Starter not found")
    entrypoint, _binding, _workflow = resolved_entrypoint
    submitted_inputs = preserve_server_captured_workflow_inputs(
        entrypoint,
        (payload or {}).get("inputs"),
        run.trigger_data,
    )
    try:
        input_values = validate_workspace_workflow_inputs(entrypoint, submitted_inputs)
    except ValueError as exc:
        try:
            errors = json.loads(str(exc))
        except Exception:
            errors = {"inputs": "Invalid workflow inputs."}
        _fail(422, {"message": "Invalid workflow inputs", "errors": errors})
    mapped = assemble_workspace_workflow_inputs(entrypoint, input_values)
    trigger_data = dict(run.trigger_data or {})
    trigger_data.update(input_values)
    trigger_data.update(mapped)
    run.trigger_data = trigger_data
    variables = dict(run.variables or {})
    variables.update(input_values)
    variables.update(mapped)
    variables["trigger"] = {**deepcopy(input_values), **deepcopy(mapped)}
    run.variables = variables
    run.status = "running"
    run.error = None
    await _project_run(db, run)
    return run.id


async def _resolve_retry(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    message: Message,
    conversation: Conversation,
    pending_action: dict[str, Any],
    choice: str,
    payload: dict[str, Any] | None,
    user: User,
) -> tuple[WorkflowRun, str | None]:
    from packages.core.services import workflow_service
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.workflow_chat_projection import workflow_progress_steps

    if choice not in {"retry", "retry_now"}:
        if choice not in {"cancel", "skip"}:
            _fail(400, "Unsupported Workflow retry choice")
        run.status = "cancelled"
        run.completed_at = datetime.now(timezone.utc)
        await _project_run(db, run)
        return run, None
    variables = (payload or {}).get("variables")
    if variables is not None and not isinstance(variables, dict):
        _fail(422, "Workflow retry variables must be an object")
    try:
        retry = await workflow_service.retry_workflow_run(
            db,
            run_id=run.id,
            entity_id=user.entity_id,
            started_by=user.id,
            from_step_id=str(pending_action.get("retry_from_step_id") or "") or None,
            variables=variables,
        )
    except ValueError as exc:
        _fail(409, str(exc))
    title = str((message.meta or {}).get("workflow_title") or "Workflow")
    activity = await add_message(
        db,
        conversation.id,
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
            "workflow_retry_of_run_id": run.id,
            "workflow_steps": workflow_progress_steps(retry, activity_status="running"),
        },
    )
    retry_trigger = dict(retry.trigger_data or {})
    for context_key in ("_workflow_chat_origin", "_workspace_chat_entrypoint"):
        context = dict(retry_trigger.get(context_key) or {})
        context["activity_message_id"] = activity.id
        retry_trigger[context_key] = context
    retry.trigger_data = retry_trigger
    return retry, retry.id


async def _resolve_wait(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    message: Message,
    pending_action: dict[str, Any],
    kind: str,
    choice: str,
    note: str | None,
    payload: dict[str, Any] | None,
    user: User,
) -> str | None:
    from packages.core.ai.workflow_runner import (
        complete_workflow_stage_wait,
        workflow_approval_decision_metadata,
        workflow_stage_wait_context,
    )

    if run.status != "paused" or run.current_step_id != pending_action.get("step_id"):
        _fail(409, "Workflow Run is no longer waiting for this response")
    if choice in _CANCEL_CHOICES:
        run.status = "cancelled"
        run.completed_at = datetime.now(timezone.utc)
        await _project_run(db, run)
        return None
    if kind == PendingActionKind.WORKFLOW_INPUT.value and choice not in {
        "respond", "submit", "provide_answers", "ok",
    }:
        _fail(400, "Unsupported Workflow input choice")
    workflow = (await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == run.workflow_id,
            WorkflowDefinition.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    current_step = next(
        (
            step for step in (workflow.steps if workflow else [])
            if str(step.get("id") or "") == str(pending_action["step_id"])
        ),
        None,
    )
    stage_wait = workflow_stage_wait_context(run, current_step)
    if isinstance(current_step, dict) and current_step.get("type") == "stage" and stage_wait is None:
        _fail(409, "Workflow stage is no longer waiting")
    current_config = (
        stage_wait[1].get("config")
        if stage_wait is not None and isinstance(stage_wait[1].get("config"), dict)
        else current_step.get("config")
        if isinstance(current_step, dict) and isinstance(current_step.get("config"), dict)
        else {"options": pending_action.get("options") or []}
    )
    review_edited = False
    submitted_review = (
        payload.get("review")
        if isinstance(payload, dict) and "review" in payload
        else None
    )
    if (
        kind == PendingActionKind.WORKFLOW_APPROVAL.value
        and submitted_review is not None
    ):
        from packages.core.services.workflow_chat_approvals import (
            _replace_projected_review,
            _review_matches_original,
            _review_variable,
        )

        review_variable = _review_variable(current_config)
        if not review_variable:
            _fail(422, "This approval material is not editable")
        original_review = (run.variables or {}).get(review_variable)
        if not _review_matches_original(original_review, submitted_review):
            _fail(422, "Edited approval material must keep the same packet shape")
        run.variables = {
            **dict(run.variables or {}),
            review_variable: deepcopy(submitted_review),
        }
        _replace_projected_review(message, submitted_review)
        review_edited = submitted_review != original_review

    response_variable = str(
        pending_action.get("response_variable")
        or f"{pending_action['step_id']}_response"
    )
    response_value = {
        "choice": choice,
        **({"note": note} if note else {}),
        **({"payload": payload} if payload is not None else {}),
        **({"review_edited": True} if review_edited else {}),
    }
    variables = dict(run.variables or {})
    variables[response_variable] = response_value
    run.variables = variables
    approval_metadata: dict[str, Any] = {}
    if kind == PendingActionKind.WORKFLOW_APPROVAL.value:
        if choice == APPROVAL_CHOICE_ALWAYS_APPROVE:
            from packages.core.services.workflow_chat_approvals import (
                grant_workflow_wait_standing_approval,
            )

            await grant_workflow_wait_standing_approval(
                db,
                run=run,
                user_id=user.id,
                config=current_config,
            )
            current_config = {
                **current_config,
                "options": [
                    *[str(item) for item in current_config.get("options") or []],
                    APPROVAL_CHOICE_ALWAYS_APPROVE,
                ],
            }
        try:
            approval_metadata = workflow_approval_decision_metadata(
                current_config,
                decision=choice,
                actor_id=user.id,
                decided_at=datetime.now(timezone.utc),
            )
        except ValueError as exc:
            _fail(400, str(exc))
    if stage_wait is not None and isinstance(current_step, dict):
        complete_workflow_stage_wait(
            run,
            current_step,
            stage_wait,
            metadata={
                "workflow_response": response_value,
                **(
                    {"review": deepcopy(submitted_review)}
                    if submitted_review is not None
                    else {}
                ),
                **approval_metadata,
            },
        )
    else:
        step_results = dict(run.step_results or {})
        previous = dict(step_results.get(str(pending_action["step_id"])) or {})
        previous.update({
            "status": "completed",
            "resumed": True,
            "workflow_response": response_value,
            **(
                {"review": deepcopy(submitted_review)}
                if submitted_review is not None
                else {}
            ),
            "resumed_at": datetime.now(timezone.utc).isoformat(),
            **approval_metadata,
        })
        step_results[str(pending_action["step_id"])] = previous
        run.step_results = step_results
    run.status = "running"
    run.error = None
    await _project_run(db, run)
    return run.id


async def _enqueue_run(db: AsyncSession, run: WorkflowRun, run_id: str | None) -> None:
    if not run_id:
        return
    from packages.core.ai.workflow_runner import WorkflowRunner

    if WorkflowRunner.enqueue(run_id) is not False:
        return
    run.status = "failed"
    run.error = "Workflow could not be queued. Please start it again."
    run.completed_at = datetime.now(timezone.utc)
    from packages.core.services.workflow_run_trace import update_workflow_history_summary

    update_workflow_history_summary(run)
    await _project_run(db, run)
    await db.commit()


async def resolve_workflow_message_action(
    db: AsyncSession,
    *,
    message: Message,
    conversation: Conversation,
    choice: str,
    note: str | None,
    payload: dict[str, Any] | None,
    user: User,
) -> WorkflowMessageActionResult | None:
    """Resolve one Workflow card for either Workspace or Global Chat."""
    pending_action = message.pending_action if isinstance(message.pending_action, dict) else {}
    kind = str(pending_action.get("kind") or "")
    if kind not in WORKFLOW_RUN_ACTION_KINDS or not pending_action.get("workflow_run_id"):
        return None
    normalized_choice = str(choice or "").strip().lower()
    run = await _load_controlled_run(
        db,
        message=message,
        conversation=conversation,
        pending_action=pending_action,
        user=user,
    )
    if message.resolved_at is not None and not (
        kind == PendingActionKind.WORKFLOW_RETRY.value
        and normalized_choice in {"retry", "retry_now"}
    ):
        return WorkflowMessageActionResult(message, run, str(run.workspace_id))
    resolution: dict[str, Any] = {"choice": choice}
    if note:
        resolution["note"] = note
    if payload is not None:
        resolution["payload"] = payload
    resolved = await chat_service.resolve_pending_action(
        db,
        message_id=message.id,
        user_id=user.id,
        resolution=resolution,
    )
    if resolved is None:
        _fail(404, "message not found")

    run_to_enqueue: str | None
    if kind == PendingActionKind.WORKFLOW_STARTER_INPUT.value:
        run_to_enqueue = await _resolve_starter(
            db,
            run=run,
            pending_action=pending_action,
            choice=normalized_choice,
            payload=payload,
            user=user,
        )
    elif kind == PendingActionKind.WORKFLOW_RETRY.value:
        run, run_to_enqueue = await _resolve_retry(
            db,
            run=run,
            message=message,
            conversation=conversation,
            pending_action=pending_action,
            choice=normalized_choice,
            payload=payload,
            user=user,
        )
    elif kind in _WORKFLOW_WAIT_KINDS:
        run_to_enqueue = await _resolve_wait(
            db,
            run=run,
            message=message,
            pending_action=pending_action,
            kind=kind,
            choice=normalized_choice,
            note=note,
            payload=payload,
            user=user,
        )
    else:
        return None

    await db.commit()
    await _enqueue_run(db, run, run_to_enqueue)
    return WorkflowMessageActionResult(resolved, run, str(run.workspace_id))
