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
    remove_auto_approve_action,
    remove_auto_approve_capability,
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


class StandingGrantsResponse(BaseModel):
    """Active always-approve grants — the workspace's standing approvals."""

    actions: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class ApprovalMatrixRow(BaseModel):
    """One Strategist proposal type + whether it currently auto-approves."""

    action_key: str
    kind: str
    operation: str = ""
    label: str
    risk_level: str
    auto_approved: bool


class OtherGrant(BaseModel):
    kind: str  # "action" | "capability"
    value: str


class ApprovalMatrixResponse(BaseModel):
    rows: list[ApprovalMatrixRow] = Field(default_factory=list)
    other_grants: list[OtherGrant] = Field(default_factory=list)


class ApprovalMatrixUpdate(BaseModel):
    action_key: str
    auto_approved: bool


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


@router.get("/standing-grants", response_model=StandingGrantsResponse)
async def list_standing_grants(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Active always-approve grants (actions + capabilities).

    Any workspace member may read; revoking requires owner/admin.
    """
    await _require_workspace(db, workspace_id, user.entity_id)
    policy = await get_policy(db, workspace_id)
    return StandingGrantsResponse(
        actions=list(policy.auto_approve_actions),
        capabilities=list(policy.auto_approve_capabilities),
    )


@router.delete("/standing-grants/{kind}/{value}", response_model=StandingGrantsResponse)
async def revoke_standing_grant(
    workspace_id: str,
    kind: str,
    value: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one standing grant (``kind`` = ``action`` | ``capability``).

    Owner/admin only — removal writes a GovernanceRevision audit row.
    Returns the remaining grants after the revoke.
    """
    await _require_workspace(db, workspace_id, user.entity_id)
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only entity owners or admins can revoke standing grants")
    if kind == "action":
        removed = await remove_auto_approve_action(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            action_key=value,
            changed_by=user.id,
        )
    elif kind == "capability":
        removed = await remove_auto_approve_capability(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            capability_id=value,
            changed_by=user.id,
        )
    else:
        raise HTTPException(400, "kind must be 'action' or 'capability'")
    if not removed:
        raise HTTPException(404, f"standing grant not found: {value}")
    await db.commit()
    policy = await get_policy(db, workspace_id)
    return StandingGrantsResponse(
        actions=list(policy.auto_approve_actions),
        capabilities=list(policy.auto_approve_capabilities),
    )


async def _approval_matrix(db: AsyncSession, workspace_id: str) -> ApprovalMatrixResponse:
    """Catalog rows + the non-Strategist grants, from the current policy."""
    from packages.core.proposals.constants import strategist_approval_catalog

    policy = await get_policy(db, workspace_id)
    auto_actions = set(policy.auto_approve_actions)
    catalog = strategist_approval_catalog()
    strategist_keys = {row["action_key"] for row in catalog}
    return ApprovalMatrixResponse(
        rows=[
            ApprovalMatrixRow(
                action_key=row["action_key"],
                kind=row["kind"],
                operation=row["operation"],
                label=row["label"],
                risk_level=row["risk_level"],
                auto_approved=row["action_key"] in auto_actions,
            )
            for row in catalog
        ],
        other_grants=[
            OtherGrant(kind="action", value=value)
            for value in policy.auto_approve_actions
            if value not in strategist_keys
        ] + [
            OtherGrant(kind="capability", value=value)
            for value in policy.auto_approve_capabilities
        ],
    )


@router.get("/approval-matrix", response_model=ApprovalMatrixResponse)
async def get_approval_matrix(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-proposal-type approval automation (Settings → Approval automation).

    One row per distinct ``workspace.proposal.*`` action_key in the M8
    catalog — a type is either auto-approved by a standing grant or still
    routed to a human. ``other_grants`` carries every standing grant that is
    not a Strategist proposal type (e.g. ``file.write``) so nothing the
    workspace already granted disappears from the settings UI.

    Any workspace member may read; toggling requires owner/admin.
    """
    await _require_workspace(db, workspace_id, user.entity_id)
    return await _approval_matrix(db, workspace_id)


@router.put("/approval-matrix", response_model=ApprovalMatrixResponse)
async def put_approval_matrix(
    workspace_id: str,
    req: ApprovalMatrixUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle one proposal type between auto-approve and human review.

    Owner/admin only (mirrors the standing-grant revoke endpoint); each
    toggle writes a GovernanceRevision audit row.
    """
    from packages.core.governance import add_auto_approve_action
    from packages.core.proposals.constants import STRATEGIST_ACTION_KEYS

    await _require_workspace(db, workspace_id, user.entity_id)
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only entity owners or admins can change approval automation")
    if req.action_key not in STRATEGIST_ACTION_KEYS:
        raise HTTPException(400, f"unknown proposal action_key: {req.action_key}")
    if req.auto_approved:
        await add_auto_approve_action(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            action_key=req.action_key,
            changed_by=user.id,
        )
    else:
        await remove_auto_approve_action(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            action_key=req.action_key,
            changed_by=user.id,
        )
    await db.commit()
    return await _approval_matrix(db, workspace_id)


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
