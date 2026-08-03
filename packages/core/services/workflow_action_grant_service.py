"""Create and validate durable, narrowly scoped workflow action grants."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowActionGrant


class WorkflowActionGrantDenied(PermissionError):
    """Raised for absent, expired, revoked, or out-of-scope grants."""


def _denied() -> WorkflowActionGrantDenied:
    return WorkflowActionGrantDenied("Workflow action grant is unavailable or out of scope")


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
