"""Create and validate durable, narrowly scoped workflow action grants."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.workflow import (
    WorkflowActionGrant,
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowRun,
)
from packages.core.proposals.constants import WORKFLOW_RUN_EXTERNAL_ACTION_KEY


YOUTUBE_PUBLICATION_GRANT_TYPE = "youtube_publication_v1"
_YOUTUBE_PUBLIC_LABEL = re.compile(r"^(?:publish|发布)$", re.IGNORECASE)
_YOUTUBE_UPLOAD_ENTRY_LABEL = re.compile(
    r"^(?:upload videos|上传视频)$",
    re.IGNORECASE,
)


class WorkflowActionGrantDenied(PermissionError):
    """Raised for absent, expired, revoked, or out-of-scope grants."""


def _denied() -> WorkflowActionGrantDenied:
    return WorkflowActionGrantDenied("Workflow action grant is unavailable or out of scope")


def proposal_workflow_authorization_for_inputs(
    declaration: Any,
    inputs: Any,
    *,
    workflow_steps: Any = None,
) -> dict[str, Any] | None:
    """Return one strict, normalized authorization only when its input condition matches."""

    if not isinstance(declaration, dict) or not isinstance(inputs, dict):
        return None
    allowed_fields = {
        "kind",
        "action_key",
        "when",
        "destination",
        "upload_step_id",
        "publish_step_id",
        "ttl_seconds",
    }
    if set(declaration) - allowed_fields:
        return None
    condition = declaration.get("when")
    if not isinstance(condition, dict) or set(condition) != {"input_key", "equals"}:
        return None
    input_key = str(condition.get("input_key") or "").strip()
    upload_step_id = str(declaration.get("upload_step_id") or "").strip()
    publish_step_id = str(declaration.get("publish_step_id") or "").strip()
    ttl_seconds = declaration.get("ttl_seconds", 86400)
    if (
        declaration.get("kind") != YOUTUBE_PUBLICATION_GRANT_TYPE
        or declaration.get("action_key") != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or declaration.get("destination") != "studio.youtube.com"
        or condition.get("equals") != "public"
        or not input_key
        or inputs.get(input_key) != "public"
        or not upload_step_id
        or not publish_step_id
        or upload_step_id == publish_step_id
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 86400
    ):
        return None
    if workflow_steps is not None:
        steps_by_id = {
            str(step.get("id") or "").strip(): step
            for step in workflow_steps
            if isinstance(step, dict)
        } if isinstance(workflow_steps, list) else {}
        for step_id in (upload_step_id, publish_step_id):
            step = steps_by_id.get(step_id)
            if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "agent":
                return None
    return {
        "kind": YOUTUBE_PUBLICATION_GRANT_TYPE,
        "action_key": WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        "when": {"input_key": input_key, "equals": "public"},
        "destination": "studio.youtube.com",
        "upload_step_id": upload_step_id,
        "publish_step_id": publish_step_id,
        "ttl_seconds": ttl_seconds,
    }


def youtube_publication_provider_request_kind(provider_request: Any) -> str | None:
    """Classify only the three allowlisted YouTube Studio approval requests."""

    if not isinstance(provider_request, dict) or provider_request.get("provider") != "chrome":
        return None
    try:
        host = str(urlparse(str(provider_request.get("url") or "")).hostname or "").lower()
    except ValueError:
        return None
    if host != "studio.youtube.com":
        return None
    retry_tool = str(provider_request.get("retry_tool") or "")
    if (
        provider_request.get("confirmation_mode") == "preapproval_allowed"
        and provider_request.get("policy_category") == "file_upload"
        and retry_tool == "mcp__chrome__upload"
    ):
        retry_arguments = provider_request.get("retry_arguments")
        files = retry_arguments.get("files") if isinstance(retry_arguments, dict) else None
        if (
            isinstance(files, list)
            and len(files) == 1
            and str(files[0]).lower().split("?", 1)[0].endswith(".mp4")
        ):
            return "upload_transfer"
        return None
    if (
        provider_request.get("confirmation_mode") != "always_action_time"
        or provider_request.get("policy_category") != "representational_communication"
        or retry_tool != "mcp__chrome__click_element"
    ):
        return None
    label = str(provider_request.get("target_label") or "").strip()
    if _YOUTUBE_UPLOAD_ENTRY_LABEL.fullmatch(label):
        return "upload_entry"
    if _YOUTUBE_PUBLIC_LABEL.fullmatch(label):
        return "publish"
    return None


async def create_proposal_workflow_action_grant(
    db: AsyncSession,
    *,
    item: ProposalItemRecord,
    run: WorkflowRun,
    binding: WorkflowBinding,
    declaration: dict[str, Any],
    granted_by: str,
) -> WorkflowActionGrant:
    """Create the single runtime grant covered by one approved Proposal item."""

    workflow = await db.get(WorkflowDefinition, binding.workflow_id)
    normalized = proposal_workflow_authorization_for_inputs(
        declaration,
        (item.payload or {}).get("inputs"),
        workflow_steps=workflow.steps if workflow is not None else None,
    )
    snapshot = (
        (item.payload or {}).get("_proposal_authorization_binding")
        if isinstance(item.payload, dict)
        else None
    )
    root_run_id = str(run.lineage_root_run_id or run.id)
    if (
        normalized is None
        or workflow is None
        or not isinstance(snapshot, dict)
        or snapshot.get("binding_id") != binding.id
        or snapshot.get("workflow_id") != binding.workflow_id
        or snapshot.get("revision") != binding.revision
        or snapshot.get("declaration") != normalized
        or item.entity_id != run.entity_id
        or item.workspace_id != run.workspace_id
        or item.action_key != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or item.risk_level != "high"
        or str(item.status) != "approved"
        or run.binding_id != binding.id
        or run.trigger_source != "proposal"
        or run.lineage_is_legacy is not False
        or root_run_id != run.id
        or str(((run.trigger_data or {}).get("_proposal_context") or {}).get("proposal_item_id") or "") != item.id
    ):
        raise _denied()
    existing = (await db.execute(
        select(WorkflowActionGrant).where(
            WorkflowActionGrant.proposal_item_id == item.id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        if (
            existing.workflow_run_id == run.id
            and existing.workflow_lineage_root_run_id == root_run_id
            and existing.scope.get("declaration") == normalized
        ):
            return existing
        raise _denied()
    now = datetime.now(timezone.utc)
    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=item.entity_id,
        workspace_id=item.workspace_id,
        workflow_run_id=run.id,
        project_id=None,
        proposal_item_id=item.id,
        workflow_lineage_root_run_id=root_run_id,
        action_key=WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        grant_type=YOUTUBE_PUBLICATION_GRANT_TYPE,
        scope={
            "source": "proposal",
            "binding_id": binding.id,
            "binding_revision": binding.revision,
            "declaration": normalized,
        },
        granted_by=granted_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=normalized["ttl_seconds"]),
    )
    db.add(grant)
    await db.flush()
    return grant


async def _validate_proposal_youtube_publication_scope(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    workflow_run_id: str,
    workflow_lineage_root_run_id: str,
    workflow_step_id: str,
    for_update: bool = False,
) -> tuple[WorkflowActionGrant, dict[str, Any]]:
    statement = select(WorkflowActionGrant).where(
        WorkflowActionGrant.id == grant_id,
        WorkflowActionGrant.entity_id == entity_id,
        WorkflowActionGrant.workspace_id == workspace_id,
        WorkflowActionGrant.granted_by == user_id,
        WorkflowActionGrant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE,
        WorkflowActionGrant.action_key == WORKFLOW_RUN_EXTERNAL_ACTION_KEY,
        WorkflowActionGrant.workflow_lineage_root_run_id == workflow_lineage_root_run_id,
    )
    if for_update:
        statement = statement.with_for_update()
    grant = (await db.execute(statement)).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None or grant.consumed_at is not None:
        raise _denied()
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    declaration = scope.get("declaration")
    run = (await db.execute(select(WorkflowRun).where(
        WorkflowRun.id == workflow_run_id,
        WorkflowRun.entity_id == entity_id,
        WorkflowRun.workspace_id == workspace_id,
        WorkflowRun.lineage_root_run_id == workflow_lineage_root_run_id,
        WorkflowRun.binding_id == scope.get("binding_id"),
        WorkflowRun.trigger_source == "proposal",
    ))).scalar_one_or_none()
    item = await db.get(ProposalItemRecord, grant.proposal_item_id)
    binding = await db.get(WorkflowBinding, scope.get("binding_id"))
    workflow = await db.get(WorkflowDefinition, binding.workflow_id) if binding is not None else None
    current_declaration = proposal_workflow_authorization_for_inputs(
        (binding.config or {}).get("proposal_authorization") if binding is not None else None,
        (item.payload or {}).get("inputs") if item is not None else None,
        workflow_steps=workflow.steps if workflow is not None else None,
    )
    proposal_context = (
        (run.trigger_data or {}).get("_proposal_context")
        if run is not None and isinstance(run.trigger_data, dict)
        else None
    )
    if (
        scope.get("source") != "proposal"
        or not isinstance(declaration, dict)
        or run is None
        or run.lineage_is_legacy is not False
        or item is None
        or item.entity_id != entity_id
        or item.workspace_id != workspace_id
        or item.execution_root_id != workflow_lineage_root_run_id
        or str(item.status) != "executing"
        or item.action_key != WORKFLOW_RUN_EXTERNAL_ACTION_KEY
        or not isinstance(proposal_context, dict)
        or proposal_context.get("proposal_item_id") != item.id
        or binding is None
        or not binding.enabled
        or binding.status != "active"
        or binding.revision != scope.get("binding_revision")
        or current_declaration != declaration
    ):
        raise _denied()
    return grant, declaration


async def validate_proposal_youtube_publication_step(
    db: AsyncSession,
    *,
    browser_action: str,
    **kwargs: Any,
) -> WorkflowActionGrant:
    """Authorize only browser side effects owned by the declared Workflow step."""

    grant, declaration = await _validate_proposal_youtube_publication_scope(
        db,
        **kwargs,
    )
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    allowed_actions = (
        {"click_element", "upload"}
        if workflow_step_id == declaration.get("upload_step_id")
        else {"click_element", "fill_or_select"}
        if workflow_step_id == declaration.get("publish_step_id")
        else set()
    )
    if browser_action not in allowed_actions:
        raise _denied()
    return grant


async def validate_proposal_youtube_publication_grant(
    db: AsyncSession,
    *,
    provider_request: dict[str, Any],
    for_update: bool = False,
    **kwargs: Any,
) -> tuple[WorkflowActionGrant, str]:
    grant, declaration = await _validate_proposal_youtube_publication_scope(
        db,
        for_update=for_update,
        **kwargs,
    )
    request_kind = youtube_publication_provider_request_kind(provider_request)
    workflow_step_id = str(kwargs.get("workflow_step_id") or "")
    expected_step_id = (
        declaration.get("publish_step_id")
        if request_kind == "publish"
        else declaration.get("upload_step_id")
        if request_kind in {"upload_entry", "upload_transfer"}
        else None
    )
    if not request_kind or expected_step_id != workflow_step_id:
        raise _denied()
    return grant, request_kind


async def consume_proposal_youtube_publication_grant(
    db: AsyncSession,
    **kwargs: Any,
) -> WorkflowActionGrant:
    grant, request_kind = await validate_proposal_youtube_publication_grant(
        db,
        for_update=True,
        **kwargs,
    )
    if request_kind != "publish":
        raise _denied()
    grant.consumed_at = datetime.now(timezone.utc)
    grant.consumed_by_run_id = str(kwargs.get("workflow_run_id") or "")
    await db.flush()
    return grant


async def create_workflow_action_grant(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    workflow_run_id: str,
    project_id: str,
    grant_type: str,
    scope: dict,
    granted_by: str,
    ttl_seconds: int = 86400,
) -> WorkflowActionGrant:
    now = datetime.now(timezone.utc)
    lifetime_seconds = max(1, min(int(ttl_seconds), 86400))
    grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        workflow_run_id=workflow_run_id,
        project_id=project_id,
        grant_type=grant_type,
        scope=dict(scope),
        granted_by=granted_by,
        granted_at=now,
        expires_at=now + timedelta(seconds=lifetime_seconds),
    )
    db.add(grant)
    await db.flush()
    return grant


async def validate_workflow_action_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    project_id: str,
    grant_type: str,
    plan_version: int,
    scene_id: str | None,
    action: str,
    allow_batch: bool = False,
) -> WorkflowActionGrant:
    grant = (await db.execute(
        select(WorkflowActionGrant).where(
            WorkflowActionGrant.id == grant_id,
            WorkflowActionGrant.entity_id == entity_id,
            WorkflowActionGrant.granted_by == user_id,
            WorkflowActionGrant.workspace_id == workspace_id,
            WorkflowActionGrant.project_id == project_id,
            WorkflowActionGrant.grant_type == grant_type,
        )
    )).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        raise _denied()

    now = datetime.now(timezone.utc)
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise _denied()

    scope = grant.scope if isinstance(grant.scope, dict) else {}
    if scope.get("approved_plan_version") != plan_version:
        raise _denied()
    scene_ids = scope.get("scene_ids")
    if not isinstance(scene_ids, list) or not scene_ids:
        raise _denied()
    if scene_id:
        if scene_id not in scene_ids:
            raise _denied()
    elif not allow_batch:
        raise _denied()
    allowed_actions = scope.get("allowed_actions")
    if not isinstance(allowed_actions, list) or action not in allowed_actions:
        raise _denied()
    return grant


async def revoke_workflow_action_grant(
    db: AsyncSession,
    *,
    grant_id: str,
    entity_id: str,
    workspace_id: str,
) -> WorkflowActionGrant:
    grant = (await db.execute(
        select(WorkflowActionGrant)
        .where(
            WorkflowActionGrant.id == grant_id,
            WorkflowActionGrant.entity_id == entity_id,
            WorkflowActionGrant.workspace_id == workspace_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if grant is None:
        raise _denied()
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    return grant
