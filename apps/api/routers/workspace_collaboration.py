"""Customer-controlled external collaboration; ordinary company routes stay private."""

from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import workspace_collaboration as service

router = APIRouter(prefix="/api/v1", tags=["workspace-collaboration"])


class PhaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["request_acceptance", "accept"]
    note: str = Field(default="", max_length=2000)


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
    return await service.phase(db, user, workspace_id, req.action, req.note)


@router.get("/workspace-collaborations/{grant_id}")
async def portal(grant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.portal(db, user, grant_id)


@router.post("/workspace-collaborations/{grant_id}/accept")
async def accept(grant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.accept(db, user, grant_id)


@router.put("/workspaces/{workspace_id}/collaboration-group")
async def link_group(
    workspace_id: str,
    req: service.GroupRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.link_group(db, user, workspace_id, req)
