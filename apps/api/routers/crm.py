"""Authenticated HTTP surface for the shared company CRM service."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import crm as service
from packages.core.services.crm import Activity, Followup, Lifecycle, Opportunity, Profile, Stage
from typing import Literal

router = APIRouter(prefix="/api/v1", tags=["crm"])


@router.get("/crm/clients")
async def clients(
    search: str = "",
    lifecycle: Lifecycle | None = None,
    owner_id: str = "",
    status: Literal["active", "inactive"] = "active",
    tag: str = "",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.clients(
        search=search,
        lifecycle=lifecycle,
        owner_id=owner_id,
        status=status,
        tag=tag,
        limit=limit,
        offset=offset,
        user=user,
        db=db,
    )


@router.post("/crm/clients", status_code=201)
async def create(req: Profile, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.create(req=req, user=user, db=db)


@router.put("/clients/{client_id}/crm")
async def profile(
    client_id: str, req: Profile, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await service.profile(client_id=client_id, req=req, user=user, db=db)


@router.get("/crm/overview")
async def overview(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.overview(user=user, db=db)


@router.get("/clients/{client_id}/activities")
async def activities(
    client_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.activities(client_id=client_id, limit=limit, offset=offset, user=user, db=db)


@router.post("/clients/{client_id}/activities", status_code=201)
@router.put("/clients/{client_id}/activities/{record_id}")
async def activity(
    client_id: str,
    req: Activity,
    record_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.activity(client_id=client_id, req=req, record_id=record_id, user=user, db=db)


@router.get("/clients/{client_id}/opportunities")
async def opportunities(
    client_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.opportunities(client_id=client_id, limit=limit, offset=offset, user=user, db=db)


@router.post("/clients/{client_id}/opportunities", status_code=201)
@router.put("/clients/{client_id}/opportunities/{record_id}")
async def opportunity(
    client_id: str,
    req: Opportunity,
    record_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.opportunity(client_id=client_id, req=req, record_id=record_id, user=user, db=db)


@router.get("/clients/{client_id}/followups")
async def followups(
    client_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.followups(client_id=client_id, limit=limit, offset=offset, user=user, db=db)


@router.post("/clients/{client_id}/followups", status_code=201)
@router.put("/clients/{client_id}/followups/{record_id}")
async def followup(
    client_id: str,
    req: Followup,
    record_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.followup(client_id=client_id, req=req, record_id=record_id, user=user, db=db)


@router.delete("/clients/{client_id}/{kind}/{record_id}", status_code=204)
async def delete_record(
    client_id: str,
    kind: Literal["activities", "opportunities", "followups"],
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.delete_record(client_id=client_id, kind=kind, record_id=record_id, user=user, db=db)


@router.get("/crm/opportunities")
async def pipeline(
    stage: Stage | None = None,
    owner_id: str = "",
    search: str = "",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.pipeline(
        stage=stage, owner_id=owner_id, search=search, limit=limit, offset=offset, user=user, db=db
    )


@router.get("/crm/followups")
async def followup_queue(
    status: Literal["open", "done"] = "open",
    owner_id: str = "",
    overdue: bool = False,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.followup_queue(
        status=status, owner_id=owner_id, overdue=overdue, limit=limit, offset=offset, user=user, db=db
    )
