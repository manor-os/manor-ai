"""Favorite / pin / bookmark endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.favorite_service import (
    toggle_favorite,
    list_favorites,
    is_favorited,
    get_favorite_counts,
    get_pinned_items,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/favorites", tags=["favorites"])


# ── Schemas ──

class ToggleRequest(BaseModel):
    resource_type: str
    resource_id: str
    favorite_type: str = "star"
    note: str | None = None


class FavoriteResponse(BaseModel):
    id: str
    entity_id: str
    user_id: str
    resource_type: str
    resource_id: str
    favorite_type: str
    note: str | None = None
    created_at: str | None = None


class ToggleResponse(BaseModel):
    is_favorited: bool
    favorite: FavoriteResponse | None = None


class CheckResponse(BaseModel):
    is_favorited: bool


class CountsResponse(BaseModel):
    star: int
    pin: int
    bookmark: int


class PinnedItemResponse(BaseModel):
    resource_type: str
    resource_id: str
    note: str | None = None
    created_at: str | None = None


# ── Endpoints ──

@router.post("/toggle", response_model=ToggleResponse)
async def toggle(
    body: ToggleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a favorite/pin/bookmark on a resource."""
    is_fav, fav = await toggle_favorite(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        favorite_type=body.favorite_type,
        note=body.note,
    )
    return {
        "is_favorited": is_fav,
        "favorite": _to_response(fav) if fav else None,
    }


@router.get("", response_model=list[FavoriteResponse])
async def list_my_favorites(
    resource_type: str | None = Query(None),
    favorite_type: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List current user's favorites."""
    favs = await list_favorites(
        db, user.entity_id, user.id,
        resource_type=resource_type,
        favorite_type=favorite_type,
    )
    return [_to_response(f) for f in favs]


@router.get("/check", response_model=CheckResponse)
async def check_favorite(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    favorite_type: str = Query("star"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if a resource is favorited by the current user."""
    result = await is_favorited(db, user.id, resource_type, resource_id, favorite_type)
    return {"is_favorited": result}


@router.get("/pinned", response_model=list[PinnedItemResponse])
async def pinned_items(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get pinned items for quick access sidebar."""
    return await get_pinned_items(db, user.entity_id, user.id)


@router.get("/counts", response_model=CountsResponse)
async def favorite_counts(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get favorite counts by type for a resource."""
    return await get_favorite_counts(db, user.entity_id, resource_type, resource_id)


# ── Helpers ──

def _to_response(f) -> dict:
    return {
        "id": f.id,
        "entity_id": f.entity_id,
        "user_id": f.user_id,
        "resource_type": f.resource_type,
        "resource_id": f.resource_id,
        "favorite_type": f.favorite_type,
        "note": f.note,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }
