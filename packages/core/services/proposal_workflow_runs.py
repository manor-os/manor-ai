"""Proposal item dispatch and lifecycle synchronization for Workspace Flows."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.proposal import ProposalItemKind, ProposalItemStatus
from packages.core.constants.workflow import WorkflowRunStatus
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.task import Message
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun
from packages.core.proposals.constants import WORKFLOW_RUN_EXTERNAL_ACTION_KEY
from packages.core.models.workspace import Workspace


class ProposalWorkflowRunError(ValueError):
    pass


class ProposalWorkflowRunConflict(ProposalWorkflowRunError):
    """The approved item lost a final dispatch race to existing live work."""

    def __init__(self, run: WorkflowRun, *, workflow_slug: str):
        self.run_id = run.id
        self.run_status = run.status
        self.workflow_slug = workflow_slug
        super().__init__(
            f"Workspace Flow {workflow_slug!r} already has active "
            f"{run.status} run {run.id}"
        )


async def resolve_proposal_workflow_binding(
    db: AsyncSession,
    *,
    item: ProposalItemRecord,
    lock_binding: bool = False,
) -> tuple[Any, WorkflowBinding, WorkflowDefinition, Workspace]:
    from packages.core.services.workspace_flow_catalog import (
        WorkspaceFlowCatalogError,
        resolve_workspace_flow,
    )

    payload = item.payload if isinstance(item.payload, dict) else {}
    workflow_ref = payload.get("workflow_ref") if isinstance(payload.get("workflow_ref"), dict) else {}
    blueprint_slug = str(workflow_ref.get("blueprint_slug") or "").strip()
    workflow_slug = str(workflow_ref.get("workflow_slug") or "").strip()
    if not blueprint_slug or not workflow_slug:
        raise ProposalWorkflowRunError("workflow_ref requires blueprint_slug and workflow_slug")
    workspace = await db.get(Workspace, item.workspace_id)
    if (
        workspace is None
        or workspace.entity_id != item.entity_id
        or workspace.deleted_at is not None
    ):
        raise ProposalWorkflowRunError("Proposal Workspace is unavailable")
    try:
        flow = await resolve_workspace_flow(
            db,
            workspace=workspace,
            blueprint_slug=blueprint_slug,
            workflow_slug=workflow_slug,
            lock_binding=lock_binding,
        )
    except WorkspaceFlowCatalogError as exc:
        raise ProposalWorkflowRunError(str(exc)) from None
    entrypoint = flow.entrypoint
    declared_keys = {str(field.get("key") or "") for field in entrypoint.run_inputs}
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    unknown_keys = sorted(set(inputs) - declared_keys)
    if unknown_keys:
        raise ProposalWorkflowRunError(
            f"Unknown Flow input keys: {', '.join(unknown_keys)}"
        )
    return entrypoint, flow.binding, flow.workflow, workspace


async def _dispatch_user(
    db: AsyncSession,
    *,
    workspace: Workspace,
    actor_id: str | None,
) -> User:
    from packages.core.services.workspace_access import user_can_write_workspace_artifacts

    candidate_ids = [
        str(actor_id or "").strip(),
        str((workspace.settings or {}).get("created_by_user_id") or "").strip(),
    ]
    for user_id in dict.fromkeys(value for value in candidate_ids if value):
        user = await db.get(User, user_id)
        if (
            user is not None
            and user.entity_id == workspace.entity_id
            and await user_can_write_workspace_artifacts(
                db,
                workspace_id=workspace.id,
                user_id=user.id,
                entity_role=user.role,
            )
        ):
            return user
    user = (await db.execute(
        select(User)
        .where(
            User.entity_id == workspace.entity_id,
            User.status == "active",
            User.role.in_(("owner", "admin")),
            User.deleted_at.is_(None),
        )
        .order_by(User.created_at.asc(), User.id.asc())
        .limit(1)
    )).scalar_one_or_none()
    if user is None:
        raise ProposalWorkflowRunError("No authorized user can dispatch this Flow")
    return user


async def _proposal_origin_message(
    db: AsyncSession,
    *,
    item: ProposalItemRecord,
    workspace: Workspace,
    user: User,
    source_brief: str,
) -> tuple[Any, Message]:
    from packages.core.services.conversation_messages import add_message
    from packages.core.workspace_chat import service as chat_service

    conversation = await chat_service.ensure_main_conversation(
        db,
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
    )
    existing = (await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.meta["proposal_workflow_run_item_id"].astext == item.id,
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return conversation, existing
    message = await add_message(
        db,
        conversation.id,
        role="system",
        content=source_brief,
        message_kind="system",
        meta={
            "author_user_id": user.id,
            "proposal_workflow_run_item_id": item.id,
            "proposal_id": item.proposal_id,
        },
        refs=[
            {"type": "proposal", "id": item.proposal_id},
            {"type": "proposal_item", "id": item.id},
        ],
    )
    return conversation, message


async def dispatch_workflow_run_item(
    db: AsyncSession,
    *,
    item_id: str,
    actor_id: str | None = None,
) -> WorkflowRun:
    """Create exactly one Flow lineage for an approved Proposal item."""
    item = (await db.execute(
        select(ProposalItemRecord)
        .where(ProposalItemRecord.id == item_id)
        .with_for_update()
    )).scalar_one_or_none()
    if item is None or item.kind != ProposalItemKind.WORKFLOW_RUN:
        raise ProposalWorkflowRunError("Workflow-run Proposal item not found")
    if item.execution_root_id:
        existing = await db.get(WorkflowRun, item.execution_root_id)
        if existing is not None:
            return existing
        raise ProposalWorkflowRunError("Proposal item references a missing Workflow lineage")
    if item.status != ProposalItemStatus.APPROVED:
        raise ProposalWorkflowRunError("Workflow-run Proposal item is not approved")
    entrypoint, binding, workflow, workspace = await resolve_proposal_workflow_binding(
        db,
        item=item,
        lock_binding=True,
    )
    from packages.core.services.workspace_flow_catalog import (
        latest_open_flow_run,
        workspace_flow_slug,
    )

    conflict = await latest_open_flow_run(
        db,
        workspace=workspace,
        binding_id=binding.id,
    )
    if conflict is not None:
        raise ProposalWorkflowRunConflict(
            conflict,
            workflow_slug=workspace_flow_slug(binding),
        )
    user = await _dispatch_user(db, workspace=workspace, actor_id=actor_id)
    payload = dict(item.payload or {})
    from packages.core.services.workflow_action_grant_service import (
        create_proposal_workflow_action_grant,
        proposal_workflow_authorization_for_inputs,
    )

    declaration = proposal_workflow_authorization_for_inputs(
        (binding.config or {}).get("proposal_authorization"),
        payload.get("inputs"),
        workflow_steps=workflow.steps,
    )
    authorization_snapshot = payload.get("_proposal_authorization_binding")
    snapshot_matches = bool(
        isinstance(authorization_snapshot, dict)
        and authorization_snapshot.get("binding_id") == binding.id
        and authorization_snapshot.get("workflow_id") == workflow.id
        and authorization_snapshot.get("revision") == binding.revision
        and authorization_snapshot.get("declaration") == declaration
    )
    if declaration is not None and (
        item.action_key != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or item.risk_level != "high"
        or not snapshot_matches
    ):
        raise ProposalWorkflowRunError(
            "Workflow publication authorization changed after Proposal review"
        )
    if declaration is None and item.action_key == WORKFLOW_RUN_EXTERNAL_ACTION_KEY:
        raise ProposalWorkflowRunError(
            "Workflow publication authorization is no longer available"
        )
    source_brief = str(payload.get("source_brief") or "").strip()
    conversation, origin = await _proposal_origin_message(
        db,
        item=item,
        workspace=workspace,
        user=user,
        source_brief=source_brief,
    )
    from packages.core.services.workspace_flow_launcher import (
        enqueue_workspace_flow_launch,
        launch_workspace_flow,
    )

    launched = await launch_workspace_flow(
        db,
        source="proposal",
        entrypoint=entrypoint,
        binding=binding,
        entity_id=item.entity_id,
        user_id=user.id,
        workspace_id=item.workspace_id,
        conversation_id=conversation.id,
        origin_message_id=origin.id,
        source_brief=source_brief,
        input_values=(
            dict(payload["inputs"])
            if isinstance(payload.get("inputs"), dict)
            else None
        ),
        starter_policy="only_missing",
        proposal_context={
            "proposal_id": item.proposal_id,
            "proposal_item_id": item.id,
            "run_key": payload.get("run_key"),
        },
        transaction_owner="caller",
    )
    run = launched.run
    if declaration is not None:
        grant = await create_proposal_workflow_action_grant(
            db,
            item=item,
            run=run,
            binding=binding,
            declaration=declaration,
            granted_by=user.id,
        )
        trigger_data = dict(run.trigger_data or {})
        runtime_context = dict(trigger_data.get("_workflow_runtime_context") or {})
        runtime_context["workflow_action_grant_id"] = grant.id
        trigger_data["_workflow_runtime_context"] = runtime_context
        run.trigger_data = trigger_data
    item.status = ProposalItemStatus.EXECUTING
    item.execution_root_id = run.lineage_root_run_id or run.id
    decision = dict(item.decision or {})
    decision.update({
        "latest_workflow_run_id": run.id,
        "dispatch_actor_id": user.id,
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    })
    item.decision = decision
    await db.commit()
    queued = await enqueue_workspace_flow_launch(db, launched)
    if not queued:
        await sync_proposal_workflow_run_item(db, run)
        await db.commit()
    return run


async def proposal_item_for_workflow_run(
    db: AsyncSession,
    run: WorkflowRun,
) -> ProposalItemRecord | None:
    context = (
        (run.trigger_data or {}).get("_proposal_context")
        if isinstance(run.trigger_data, dict)
        else None
    )
    item_id = str((context or {}).get("proposal_item_id") or "")
    if not item_id:
        return None
    return (await db.execute(
        select(ProposalItemRecord).where(
            ProposalItemRecord.id == item_id,
            ProposalItemRecord.entity_id == run.entity_id,
            ProposalItemRecord.workspace_id == run.workspace_id,
            ProposalItemRecord.kind == ProposalItemKind.WORKFLOW_RUN,
        ).with_for_update()
    )).scalar_one_or_none()


def _workflow_business_state(run: WorkflowRun) -> tuple[str, dict[str, Any]]:
    variables = run.variables if isinstance(run.variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    outcome = str(state.get("business_outcome") or "in_progress").strip().lower()
    retry_state = state.get("retry_state")
    return outcome, retry_state if isinstance(retry_state, dict) else {}


async def sync_proposal_workflow_run_item(
    db: AsyncSession,
    run: WorkflowRun,
) -> ProposalItemRecord | None:
    """Mirror the latest Workflow attempt onto its Proposal item."""
    item = await proposal_item_for_workflow_run(db, run)
    if item is None:
        return None

    lineage_root_id = str(run.lineage_root_run_id or run.id)
    if item.execution_root_id and str(item.execution_root_id) != lineage_root_id:
        raise ProposalWorkflowRunError(
            "Workflow Run does not belong to the Proposal item's execution lineage"
        )
    item.execution_root_id = lineage_root_id

    attempt_number = int(run.effective_attempt_number or 1)
    decision = dict(item.decision or {})
    previous_attempt = int(decision.get("latest_workflow_attempt_number") or 0)
    if previous_attempt > attempt_number:
        return item

    business_outcome, retry_state = _workflow_business_state(run)
    retry_from_step_id = str(
        retry_state.get("retry_from_step_id")
        or run.effective_retry_from_step_id
        or run.current_step_id
        or ""
    ).strip()
    actionable_completed = (
        run.status == WorkflowRunStatus.COMPLETED
        and business_outcome in {"needs_input", "revision_required"}
    )
    retryable_failed = run.status == WorkflowRunStatus.FAILED and bool(
        retry_from_step_id
    )

    now = datetime.now(timezone.utc)
    if run.status == WorkflowRunStatus.CANCELLED:
        item.status = ProposalItemStatus.CANCELLED
        item.finished_at = run.completed_at or now
    elif run.status == WorkflowRunStatus.COMPLETED and not actionable_completed:
        item.status = ProposalItemStatus.SUCCEEDED
        item.finished_at = run.completed_at or now
    elif run.status == WorkflowRunStatus.FAILED and not retryable_failed:
        item.status = ProposalItemStatus.FAILED
        item.finished_at = run.completed_at or now
    else:
        item.status = ProposalItemStatus.EXECUTING
        item.finished_at = None

    decision.update({
        "latest_workflow_run_id": run.id,
        "latest_workflow_attempt_number": attempt_number,
        "workflow_status": run.status,
        "workflow_business_outcome": business_outcome,
        "workflow_synced_at": now.isoformat(),
    })
    item.decision = decision
    await db.flush()
    return item


def mark_workflow_run_item_conflict(
    item: ProposalItemRecord,
    conflict: ProposalWorkflowRunConflict,
) -> None:
    """Settle a raced duplicate as a deterministic cancellation, not failure."""
    now = datetime.now(timezone.utc)
    item.status = ProposalItemStatus.CANCELLED
    item.finished_at = now
    decision = dict(item.decision or {})
    decision.update({
        "reason_code": "DUPLICATE",
        "dispatch_error": str(conflict),
        "conflicting_workflow_run_id": conflict.run_id,
        "conflicting_workflow_run_status": conflict.run_status,
        "finished_at": now.isoformat(),
    })
    item.decision = decision
