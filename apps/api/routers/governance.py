"""Workspace governance endpoints — read / write the policy + audit log.

Endpoints (all under ``/api/v1/workspaces/{workspace_id}/governance``):

  GET  /             current policy + revision number
  PUT  /             upsert policy (writes a revision row atomically)
  GET  /revisions    audit chain — newest first

The policy fields mirror ``WorkspacePolicy``; the router is just a
thin pydantic shell over the dataclass so the OpenAPI spec stays
self-documenting.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.governance import (
    PolicyError,
    WorkspacePolicy,
    get_policy,
    list_revisions,
    update_policy,
)
from packages.core.governance.policy import policy_to_dict
from packages.core.models.governance import GovernancePolicy
from packages.core.models.user import User
from packages.core.services.entity_service import get_workspace
from sqlalchemy import select

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/governance",
    tags=["governance"],
)


# ── Models ────────────────────────────────────────────────────────────

class PolicyBody(BaseModel):
    """Request + nested response shape — matches WorkspacePolicy 1:1."""

    never_allow_actions: list[str] = Field(default_factory=list)
    hitl_required_actions: list[str] = Field(default_factory=list)
    auto_approve_actions: list[str] = Field(default_factory=list)
    never_allow_capabilities: list[str] = Field(default_factory=list)
    hitl_required_capabilities: list[str] = Field(default_factory=list)
    auto_approve_capabilities: list[str] = Field(default_factory=list)
    max_risk_level: str = "high"
    budget_caps_per_kind: dict[str, int] = Field(default_factory=dict)


class PolicyResponse(BaseModel):
    workspace_id: str
    revision: int
    policy: PolicyBody
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class UpdatePolicyRequest(BaseModel):
    policy: PolicyBody
    change_summary: Optional[str] = None


class RevisionResponse(BaseModel):
    revision: int
    policy: PolicyBody
    change_summary: Optional[str] = None
    changed_by: Optional[str] = None
    created_at: datetime


# ── Helpers ───────────────────────────────────────────────────────────

async def _require_workspace(db: AsyncSession, workspace_id: str, entity_id: str):
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


async def _current_row(db: AsyncSession, workspace_id: str) -> Optional[GovernancePolicy]:
    return (await db.execute(
        select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace_id
        )
    )).scalar_one_or_none()


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("", response_model=PolicyResponse)
async def get_workspace_policy(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Current policy + revision number. Returns the default permissive
    policy at revision=0 if the operator hasn't customised one yet."""
    await _require_workspace(db, workspace_id, user.entity_id)
    policy = await get_policy(db, workspace_id)
    row = await _current_row(db, workspace_id)
    return PolicyResponse(
        workspace_id=workspace_id,
        revision=row.revision if row else 0,
        policy=PolicyBody(**policy_to_dict(policy)),
        updated_by=row.updated_by if row else None,
        updated_at=row.updated_at if row else None,
    )


@router.put("", response_model=PolicyResponse)
async def put_workspace_policy(
    workspace_id: str,
    req: UpdatePolicyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upsert the policy + write an audit revision atomically."""
    await _require_workspace(db, workspace_id, user.entity_id)
    try:
        new_policy = WorkspacePolicy(**req.policy.model_dump())
    except TypeError as exc:
        raise HTTPException(400, f"malformed policy body: {exc}")
    try:
        row = await update_policy(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            policy=new_policy,
            changed_by=user.id,
            change_summary=req.change_summary,
        )
    except PolicyError as exc:
        raise HTTPException(400, str(exc))
    await db.refresh(row)
    response = PolicyResponse(
        workspace_id=row.workspace_id,
        revision=row.revision,
        policy=PolicyBody(**row.policy),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )
    await db.commit()
    return response


@router.get("/revisions", response_model=list[RevisionResponse])
async def list_policy_revisions(
    workspace_id: str,
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Audit log — newest revision first."""
    await _require_workspace(db, workspace_id, user.entity_id)
    revs = await list_revisions(db, workspace_id, limit=limit)
    return [
        RevisionResponse(
            revision=r.revision,
            policy=PolicyBody(**r.policy),
            change_summary=r.change_summary,
            changed_by=r.changed_by,
            created_at=r.created_at,
        )
        for r in revs
    ]
