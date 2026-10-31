"""Customer-controlled external collaboration; ordinary company routes stay private."""

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import client_trial_access
from packages.core.services import workspace_collaboration as service
from packages.core.services.auth_service import create_collaborator_session_token
from packages.core.services.external_collaboration_access import (
    COLLABORATOR_MEMBERSHIP_ROLE,
    WORKSPACE_ACCESS_ROLES,
    collaborator_workspace_ids,
    sync_external_access,
)

router = APIRouter(prefix="/api/v1", tags=["workspace-collaboration"])


class PhaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["request_acceptance", "accept"]
    note: str = Field(default="", max_length=2000)
    # Required when accepting a delivery whose provider is still co-working in it.
    provider: Literal["keep", "remove"] | None = None


@router.get("/workspaces/{workspace_id}/collaboration")
async def management(workspace_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.management(db, user, workspace_id)


@router.post("/workspaces/{workspace_id}/collaboration", status_code=201)
async def invite(
    workspace_id: str,
    req: service.InviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.invite(db, user, workspace_id, req)


@router.post("/workspaces/{workspace_id}/collaboration/{grant_id}/{action}")
async def change(
    workspace_id: str,
    grant_id: str,
    action: Literal["authorize", "revoke", "update"],
    req: service.InviteRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if action != "revoke" and req is None:
        raise HTTPException(422, "Confirm the requested person, role and duration")
    return await service.change_grant(db, user, workspace_id, grant_id, action, req)


@router.post("/clients/{client_id}/deliveries/{delivery_id}/maintenance/request")
async def maintenance(
    client_id: str, delivery_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await service.request_maintenance(db, user, client_id, delivery_id)


@router.post("/workspaces/{workspace_id}/delivery-phase")
async def phase(
    workspace_id: str, req: PhaseRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await service.phase(db, user, workspace_id, req.action, req.note, provider=req.provider)


@router.get("/workspace-collaborations/{grant_id}")
async def portal(grant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.portal(db, user, grant_id)


@router.post("/workspace-collaborations/{grant_id}/accept")
async def accept(grant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.accept(db, user, grant_id)


def _home_session_only(request: Request):
    """A scoped token or a support session never opens another company's Workspace."""
    claims = getattr(request.state, "auth_claims", None) or {}
    if getattr(request.state, "impersonation", None) or claims.get("typ"):
        raise HTTPException(403, "Open the customer Workspace from your own sign-in")
    return claims


def _session(token, expires_at, ws, *, phase=None, access, customer_name=None):
    return {
        "access_token": token,
        "expires_at": expires_at,
        "entity_id": ws.entity_id,
        "workspace_id": ws.id,
        "role": COLLABORATOR_MEMBERSHIP_ROLE,
        "customer_name": customer_name,
        "phase": phase,
        "access": access,
    }


@router.get("/workspaces/{workspace_id}/client-trials")
async def client_trials(workspace_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await client_trial_access.list_client_trials(db, user, workspace_id)


@router.post("/workspaces/{workspace_id}/client-trials/{delivery_id}/session")
async def client_trial_session(
    workspace_id: str,
    delivery_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    claims = _home_session_only(request)
    delivery, ws, grant = await client_trial_access.open_client_trial(db, user, workspace_id, delivery_id)
    customer_name = await db.scalar(select(Entity.name).where(Entity.id == ws.entity_id))
    phase, access = delivery.phase, client_trial_access.access_label(grant.role)
    token_version = user.token_version
    await db.commit()
    # Never activate_user_membership: the person stays in their own company.
    token, expires_at = create_collaborator_session_token(
        user_id=user.id,
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        kind="client_trial",
        ref=delivery.id,
        token_version=token_version,
        mfa_authenticated="mfa" in (claims.get("amr") or []),
    )
    return _session(token, expires_at, ws, phase=phase, access=access, customer_name=customer_name)


@router.post("/workspace-collaborations/{grant_id}/session")
async def collaboration_session(
    grant_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    claims = _home_session_only(request)
    # guest_grant requires a verified email, unlike the client-trial mint: an
    # invitation is bound to an email address, so the guest must prove it; a
    # trial is bound to the caller's own company role on the source Workspace.
    ws, row = await service.guest_grant(db, user, grant_id, active=True, for_update=True)
    if row.role not in WORKSPACE_ACCESS_ROLES or row.user_id != user.id or user.entity_id == row.entity_id:
        raise HTTPException(409, "This collaboration does not open the Workspace")
    customer_role = await db.scalar(
        select(UserMembership.role).where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == row.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )
    if customer_role is not None and customer_role != COLLABORATOR_MEMBERSHIP_ROLE:
        # A real member of the customer company works there, not as a collaborator.
        raise HTTPException(409, client_trial_access.CUSTOMER_MEMBER)
    await sync_external_access(db, row)
    if ws.id not in await collaborator_workspace_ids(db, user_id=user.id, entity_id=ws.entity_id):
        raise HTTPException(410, "Collaboration expired or revoked")
    customer_name = await db.scalar(select(Entity.name).where(Entity.id == ws.entity_id))
    access, token_version = row.role, user.token_version
    await db.commit()
    token, expires_at = create_collaborator_session_token(
        user_id=user.id,
        entity_id=ws.entity_id,
        workspace_id=ws.id,
        kind="grant",
        ref=row.id,
        token_version=token_version,
        mfa_authenticated="mfa" in (claims.get("amr") or []),
    )
    return _session(token, expires_at, ws, access=client_trial_access.access_label(access), customer_name=customer_name)


@router.put("/workspaces/{workspace_id}/collaboration-group")
async def link_group(
    workspace_id: str,
    req: service.GroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.link_group(db, user, workspace_id, req)
