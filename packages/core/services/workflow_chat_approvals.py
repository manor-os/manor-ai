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


def workflow_wait_approval_scope(config: dict[str, Any]) -> tuple[str | None, str | None]:
    action_key = str(
        config.get("approval_action_key") or config.get("action_key") or ""
    ).strip() or None
    capability_id = str(
        config.get("approval_capability_id") or config.get("capability_id") or ""
    ).strip() or None
    return action_key, capability_id


async def workflow_wait_has_standing_approval(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    config: dict[str, Any],
) -> bool:
    """Honor the same durable Always store used by runtime tool approvals."""
    if not config.get("allow_always"):
        return False
    action_key, capability_id = workflow_wait_approval_scope(config)
    if not action_key and not capability_id:
        return False
    if run.workspace_id:
        from packages.core.governance.service import workspace_policy_auto_approves

        return await workspace_policy_auto_approves(
            db,
            workspace_id=run.workspace_id,
            action_key=action_key,
            capability_id=capability_id,
        )
    from packages.core.ai.runtime.approval_preferences import (
        runtime_approval_preference_mode,
    )

    return await runtime_approval_preference_mode(
        db,
        user_id=run.started_by,
        action_key=action_key,
        capability_id=capability_id,
    ) == "always_approve"


async def grant_workflow_wait_standing_approval(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    user_id: str,
    config: dict[str, Any],
) -> None:
    if not config.get("allow_always"):
        raise ValueError("Always approve is not enabled for this Workflow approval")
    action_key, capability_id = workflow_wait_approval_scope(config)
    if not action_key and not capability_id:
        raise ValueError("Always approve requires a stable action or capability scope")
    if run.workspace_id:
        from packages.core.governance.service import (
            add_auto_approve_action,
            add_auto_approve_capability,
        )

        if action_key:
            await add_auto_approve_action(
                db,
                entity_id=run.entity_id,
                workspace_id=run.workspace_id,
                action_key=action_key,
                changed_by=user_id,
            )
        elif capability_id:
            await add_auto_approve_capability(
                db,
                entity_id=run.entity_id,
                workspace_id=run.workspace_id,
                capability_id=capability_id,
                changed_by=user_id,
            )
        return

    from packages.core.ai.runtime.approval_preferences import (
        set_runtime_approval_preference,
    )

    await set_runtime_approval_preference(
        db,
        user_id=user_id,
        mode="always_approve",
        action_key=action_key,
        capability_id=capability_id,
    )


def _review_variable(config: dict[str, Any]) -> str | None:
    match = _REVIEW_VARIABLE_PATTERN.match(str(config.get("review") or ""))
    return match.group(1) if match else None


def _review_matches_original(original: Any, submitted: Any) -> bool:
    if original is None:
        return isinstance(submitted, (dict, list, str))
    if isinstance(original, dict):
        return isinstance(submitted, dict) and set(submitted) == set(original)
    if isinstance(original, list):
        return isinstance(submitted, list)
    return isinstance(submitted, type(original))


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
    )

    if workflow_definition_changed(workflow, run):
        return DEFINITION_CHANGED_ERROR
    step = next(
        (
            candidate
            for candidate in (workflow.steps or [])
            if isinstance(candidate, dict)
            and str(candidate.get("id") or "") == step_id
        ),
        None,
    )
    config = step.get("config") if isinstance(step, dict) and isinstance(step.get("config"), dict) else {}

    review_edited = False
    review_variable = _review_variable(config)
    if submitted_review is not None and normalized_choice not in cancel_choices:
        if not review_variable:
            return "This approval material is not editable."
        original_review = (run.variables or {}).get(review_variable)
        if not _review_matches_original(original_review, submitted_review):
            return "The edited approval material no longer matches the Workflow packet shape."
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
        emit_followup=False,
    )
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    await mark_hitl_request_resolved(
        db,
        conversation_id=conversation_id,
        hitl_id=hitl_id,
        choice=normalized_choice,
    )

    if normalized_choice in cancel_choices:
        run.status = "cancelled"
        run.completed_at = datetime.now(timezone.utc)
        from packages.core.services.workflow_chat_projection import project_workflow_run_status

        await project_workflow_run_status(db, run=run)
        return "The Workflow was cancelled from its originating Chat."

    if normalized_choice == "always_approve":
        await grant_workflow_wait_standing_approval(
            db,
            run=run,
            user_id=user_id,
            config=config,
        )

    response_variable = str(pending.get("response_variable") or f"{step_id}_response")
    response_value = {
        "choice": normalized_choice,
        **({"review_edited": True} if review_edited else {}),
        **({"standing": True} if normalized_choice == "always_approve" else {}),
    }
    run.variables = {**dict(run.variables or {}), response_variable: response_value}
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
