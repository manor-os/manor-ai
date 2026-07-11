"""Global search endpoint — searches across tasks, documents, agents, conversations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.search_service import global_search
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query("", description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    results = await global_search(
        db,
        user.entity_id,
        q,
        limit=limit,
        user_id=user.id,
        role=user.role,
    )
    return results
