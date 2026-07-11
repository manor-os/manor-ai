"""Entity endpoints — get and update current entity."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.entity_service import get_entity, update_entity
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


class EntityResponse(BaseModel):
    id: str
    name: str
    slug: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    logo_url: str | None = None
    llm_model: str | None = None
    settings: dict = {}


class EntityUpdateRequest(BaseModel):
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    logo_url: str | None = None
    llm_model: str | None = None


def _entity_response(entity) -> EntityResponse:
    if isinstance(entity, dict):
        return EntityResponse(**{k: entity.get(k) for k in EntityResponse.model_fields if k in entity})
    return EntityResponse(
        id=entity.id, name=entity.name, slug=entity.slug,
        address=entity.address, phone=entity.phone, email=entity.email,
        logo_url=entity.logo_url, llm_model=entity.llm_model,
        settings=entity.settings or {},
    )


@router.get("/me", response_model=EntityResponse)
async def get_my_entity(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current user's entity."""
    entity = await get_entity(db, user.entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found")
    return _entity_response(entity)


@router.put("/me", response_model=EntityResponse)
async def update_my_entity(
    req: EntityUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current user's entity."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only owner/admin can update entity")
    entity = await update_entity(db, user.entity_id, **req.model_dump(exclude_none=True))
    if not entity:
        raise HTTPException(404, "Entity not found")
    return _entity_response(entity)
