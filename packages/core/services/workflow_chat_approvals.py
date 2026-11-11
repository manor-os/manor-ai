"""Resolve Workflow wait-node approvals from the Chat that started the run."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun


_ENQUEUE_KEY = "workflow_chat_approval_enqueues"
_ENQUEUE_LISTENER_KEY = "workflow_chat_approval_enqueue_listener"
_REVIEW_VARIABLE_PATTERN = re.compile(
    r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$"
)


def _enqueue_workflows_after_commit(session) -> None:
    run_ids = session.info.pop(_ENQUEUE_KEY, [])
    if not run_ids:
        return
    from packages.core.ai.workflow_runner import WorkflowRunner

    for run_id in dict.fromkeys(run_ids):
        WorkflowRunner.enqueue(run_id)


def _clear_workflow_enqueues_after_rollback(session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_ENQUEUE_KEY, None)


def _enqueue_after_commit(db: AsyncSession, run_id: str) -> None:
    sync_session = db.sync_session
    sync_session.info.setdefault(_ENQUEUE_KEY, []).append(run_id)
    if sync_session.info.get(_ENQUEUE_LISTENER_KEY):
        return
    event.listen(sync_session, "after_commit", _enqueue_workflows_after_commit)
    event.listen(sync_session, "after_rollback", _clear_workflow_enqueues_after_rollback)
    sync_session.info[_ENQUEUE_LISTENER_KEY] = True


def workflow_wait_approval_scope(
    config: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    action_key = str(
        config.get("approval_action_key") or config.get("action_key") or ""
    ).strip() or None
    resource_id = str(
        config.get("approval_resource_id") or config.get("resource_id") or ""
    ).strip() or None
    capability_id = str(
        config.get("approval_capability_id") or config.get("capability_id") or ""
    ).strip() or None
    return action_key, resource_id, capability_id


async def workflow_wait_has_standing_approval(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    config: dict[str, Any],
) -> bool:
    return False


async def grant_workflow_wait_standing_approval(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    user_id: str,
    config: dict[str, Any],
    authority_prechecked: bool = False,
) -> None:
    raise ValueError("Explicit Workflow reviews cannot create standing consent; use approval settings")


def _review_variable(config: dict[str, Any]) -> str | None:
    if config.get("review_read_only") is True:
        return None
    match = _REVIEW_VARIABLE_PATTERN.match(str(config.get("review") or ""))
    return match.group(1) if match else None


def workflow_review_is_editable(config: dict[str, Any]) -> bool:
    """Edits apply only to a review that is exactly one Workflow variable."""
    return _review_variable(config) is not None


def workflow_review_editable_fields(config: dict[str, Any]) -> tuple[str, ...] | None:
    """Configured review fields that may change; ``None`` keeps legacy edits."""
    fields = config.get("review_editable_fields")
    if fields is None:
        return None
    if not isinstance(fields, list):
        return ()
    return tuple(dict.fromkeys(
        item.strip() for item in fields if isinstance(item, str) and item.strip()
    ))


def workflow_review_revision_field(config: dict[str, Any]) -> str | None:
    field = config.get("review_revision_field")
    return field.strip() if isinstance(field, str) and field.strip() else None


def workflow_review_is_revision_choice(choice: str) -> bool:
    normalized = re.sub(r"[-\s]+", "_", choice.strip().lower())
    return normalized in {"revise", "request_revision", "request_changes", "changes_requested"}


def _review_matches_original(original: Any, submitted: Any) -> bool:
    if original is None:
        return isinstance(submitted, (dict, list, str))
    if isinstance(original, dict):
        return isinstance(submitted, dict) and set(submitted) == set(original)
    if isinstance(original, list):
        return isinstance(submitted, list)
    return isinstance(submitted, type(original))


def _review_value_matches_type(original: Any, submitted: Any) -> bool:
    if type(submitted) is not type(original):
        return False
    if isinstance(original, list):
        item_types = {type(item) for item in original}
        return not item_types or all(type(item) in item_types for item in submitted)
    return True


def workflow_review_matches_original(
    original: Any, submitted: Any, config: dict[str, Any],
) -> bool:
    """Keep packet identity immutable while allowing configured editorial edits."""
    if not _review_matches_original(original, submitted):
        return False
    editable = workflow_review_editable_fields(config)
    if editable is None:
        return True
    if not isinstance(original, dict) or not isinstance(submitted, dict):
        return False
    allowed = set(editable)
    revision_field = workflow_review_revision_field(config)
    if revision_field:
        allowed.add(revision_field)
    return all(
        submitted[key] == value if key not in allowed
        else _review_value_matches_type(value, submitted[key])
        for key, value in original.items()
    )


def workflow_review_revision_note(config: dict[str, Any], review: Any) -> str | None:
    field = workflow_review_revision_field(config)
    if not field or not isinstance(review, dict):
        return None
    value = review.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def workflow_chat_wait_config(
    run: WorkflowRun,
    step: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the effective config only for a human-resumable wait."""
    if not isinstance(step, dict):
        return None
    if step.get("type") == "wait":
        config = step.get("config")
        return config if isinstance(config, dict) else {}
    if step.get("type") != "stage":
        return None

    from packages.core.ai.workflow_runner import workflow_stage_wait_context

    stage_wait = workflow_stage_wait_context(run, step)
    if stage_wait is None:
        return None
    operation = stage_wait[1]
    config = operation.get("config")
    return config if isinstance(config, dict) else {}


def _replace_projected_review(message: Message, review: Any) -> None:
    pending = dict(message.pending_action or {})
    pending["review"] = deepcopy(review)
    message.pending_action = pending
    flag_modified(message, "pending_action")

    meta = dict(message.meta or {})
    requests = []
    for request in meta.get("hitl_requests") or []:
        if isinstance(request, dict):
            requests.append({**request, "review": deepcopy(review)})
        else:
            requests.append(request)
    meta["hitl_requests"] = requests
    message.meta = meta
    flag_modified(message, "meta")


async def resolve_chat_workflow_wait(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
    submitted_review: Any = None,
) -> str | None:
    """Resolve a projected Workflow approval card in personal or Agent Chat."""
    message = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == hitl_id,
            Message.conversation_id == conversation_id,
            Conversation.entity_id == entity_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    pending = message.pending_action if message and isinstance(message.pending_action, dict) else {}
    if pending.get("kind") != PendingActionKind.WORKFLOW_APPROVAL.value:
        return None
    if message.resolved_at is not None:
        return "This Workflow request was already resolved."

    run = (await db.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == str(pending.get("workflow_run_id") or ""),
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.started_by == user_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if run is None:
        return None
    from packages.core.services.workflow_chat_projection import workflow_chat_context

    context = workflow_chat_context(run)
    if not context or str(context.get("conversation_id") or "") != conversation_id:
        return None
    step_id = str(pending.get("step_id") or "")
    if run.status != "paused" or str(run.current_step_id or "") != step_id:
        return "This Workflow is no longer waiting for that response."

    normalized_choice = str(action or "").strip().lower()
    options = [str(item).strip().lower() for item in pending.get("options") or []]
    if options and normalized_choice not in options:
        return None
    cancel_choices = {"cancel", "reject", "rejected", "decline", "deny", "no", "skip"}
    workflow = (await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == run.workflow_id,
            WorkflowDefinition.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if workflow is None:
        return "The Workflow definition is no longer available."
    from packages.core.services.workflow_run_trace import (
        DEFINITION_CHANGED_ERROR,
        workflow_definition_changed,
        workflow_execution_view,
    )

    if workflow_definition_changed(workflow, run):
        return DEFINITION_CHANGED_ERROR
    execution_workflow = workflow_execution_view(workflow, run)
    step = next(
        (
            candidate
            for candidate in (execution_workflow.steps or [])
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == step_id
        ),
        None,
    )
    config = workflow_chat_wait_config(run, step)
    if config is None:
        return "This Workflow step is not a human wait."

    from packages.core.services.runtime_authorization import authorize_hitl_action

    action_key, resource_id, capability_id = workflow_wait_approval_scope(config)
    authority = await authorize_hitl_action(
        db,
        entity_id=entity_id,
        workspace_id=run.workspace_id,
        by_user_id=user_id,
        action_key=action_key,
        capability_id=capability_id,
        standing=normalized_choice == "always_approve",
        requested_by=run.started_by,
        origin_conversation_id=conversation_id,
    )
    if not authority.allowed:
        return authority.reason or "You do not have permission to resolve this Workflow approval."

    from packages.core.services.workflow_message_actions import split_revision_feedback

    submitted_review, note = split_revision_feedback(submitted_review, None)
    if submitted_review is not None and submitted_review == pending.get("review"):
        # Sending back the review as shown is not an edit.
        submitted_review = None
    review_edited = False
    review_variable = _review_variable(config)
    original_review = (run.variables or {}).get(review_variable) if review_variable else None
    revising = workflow_review_is_revision_choice(normalized_choice)
    if submitted_review is not None and normalized_choice not in cancel_choices:
        if not review_variable:
            return "This approval material is not editable."
        if not workflow_review_matches_original(original_review, submitted_review, config):
            return "The edited approval material no longer matches the Workflow packet shape."
        if revising:
            note = workflow_review_revision_note(config, submitted_review) or note
    review_for_decision = submitted_review if submitted_review is not None else original_review
    if revising and workflow_review_revision_field(config) and not note:
        return "A change request is required before revising this post."
    if normalized_choice == "approve" and workflow_review_revision_note(config, review_for_decision):
        return "Submit the video change request or clear it before approving this post."
    if submitted_review is not None and normalized_choice not in cancel_choices:
        run.variables = {
            **dict(run.variables or {}),
            review_variable: deepcopy(submitted_review),
        }
        _replace_projected_review(message, submitted_review)
        review_edited = submitted_review != original_review

    approval_metadata: dict[str, Any] = {}
    if normalized_choice not in cancel_choices:
        from packages.core.ai.workflow_runner import workflow_approval_decision_metadata

        decision_config = dict(config)
        if normalized_choice == "always_approve":
            decision_config["options"] = [
                *[str(item) for item in config.get("options") or []],
                "always_approve",
            ]
        approval_metadata = workflow_approval_decision_metadata(
            decision_config,
            decision=normalized_choice,
            actor_id=user_id,
            decided_at=datetime.now(timezone.utc),
        )

    from packages.core.workspace_chat import resolve_pending_action

    await resolve_pending_action(
        db,
        message_id=message.id,
        user_id=user_id,
        resolution={
            "choice": normalized_choice,
            **(
                {"payload": {"review": deepcopy(submitted_review)}}
                if submitted_review is not None
                else {}
            ),
        },
        publish_resolution=False,
    )
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    await mark_hitl_request_resolved(
        db,
        conversation_id=conversation_id,
        hitl_id=hitl_id,
        choice=normalized_choice,
    )

    if normalized_choice in cancel_choices:
        from packages.core.constants.task import CancelSource
        from packages.core.services.workflow_run_control import cancel_run

        nested = context.get("nested") is True
        # The run is paused at this wait (checked above), so it is cancellable.
        cancel_run(run, actor_id=user_id, source=CancelSource.USER)
        if nested:
            from packages.core.ai.workflow_runner import (
                mark_workflow_terminal_effects_pending,
            )

            trigger_data = dict(run.trigger_data or {})
            trigger_data["_workflow_chat_cancel_ancestors"] = {
                "host_run_id": str(context.get("host_run_id") or ""),
                "cancelled_by": user_id,
            }
            run.trigger_data = trigger_data
            mark_workflow_terminal_effects_pending(run)
        from packages.core.services.workflow_chat_projection import project_workflow_run_status

        await project_workflow_run_status(db, run=run)
        if nested:
            _enqueue_after_commit(db, run.id)
        return "The Workflow was cancelled from its originating Chat."

    if normalized_choice == "always_approve":
        await grant_workflow_wait_standing_approval(
            db,
            run=run,
            user_id=user_id,
            config=config,
            authority_prechecked=True,
        )

    response_variable = str(pending.get("response_variable") or f"{step_id}_response")
    response_value = {
        "choice": normalized_choice,
        **({"note": note} if note else {}),
        **({"review_edited": True} if review_edited else {}),
        **({"standing": True} if normalized_choice == "always_approve" else {}),
    }
    run.variables = {**dict(run.variables or {}), response_variable: response_value}
    if isinstance(step, dict) and step.get("type") == "stage":
        from packages.core.ai.workflow_runner import (
            complete_workflow_stage_wait,
            workflow_stage_wait_context,
        )

        stage_wait = workflow_stage_wait_context(run, step)
        if stage_wait is None:
            return "This Workflow step is not a human wait."
        complete_workflow_stage_wait(
            run,
            step,
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
        previous = dict((run.step_results or {}).get(step_id) or {})
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
        run.step_results = {**dict(run.step_results or {}), step_id: previous}
    run.status = "running"
    run.error = None

    from packages.core.services.workflow_chat_projection import project_workflow_run_status

    await project_workflow_run_status(db, run=run)
    _enqueue_after_commit(db, run.id)
    return (
        "Always approval was saved and the Workflow is continuing. Future matching "
        "operations will not ask again until you change the approval setting."
        if normalized_choice == "always_approve"
        else "The Workflow approval was recorded and the Workflow is continuing."
    )
