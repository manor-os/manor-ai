"""Favorite / pin / bookmark service."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.favorite import Favorite


async def toggle_favorite(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    resource_type: str,
    resource_id: str,
    *,
    favorite_type: str = "star",
    note: Optional[str] = None,
) -> tuple[bool, Favorite | None]:
    """Toggle a favorite. Returns (is_favorited, favorite_or_None)."""
    stmt = select(Favorite).where(
        Favorite.user_id == user_id,
        Favorite.resource_type == resource_type,
        Favorite.resource_id == resource_id,
        Favorite.favorite_type == favorite_type,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing:
        await db.delete(existing)
        await db.commit()
        return False, None

    fav = Favorite(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        favorite_type=favorite_type,
        note=note,
    )
    db.add(fav)
    await db.commit()
    await db.refresh(fav)
    return True, fav


async def list_favorites(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    *,
    resource_type: Optional[str] = None,
    favorite_type: Optional[str] = None,
    limit: int = 50,
) -> list[Favorite]:
    """List user's favorites, optionally filtered by type."""
    stmt = (
        select(Favorite)
        .where(Favorite.entity_id == entity_id, Favorite.user_id == user_id)
        .order_by(Favorite.created_at.desc())
        .limit(limit)
    )
    if resource_type:
        stmt = stmt.where(Favorite.resource_type == resource_type)
    if favorite_type:
        stmt = stmt.where(Favorite.favorite_type == favorite_type)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


async def is_favorited(
    db: AsyncSession,
    user_id: str,
    resource_type: str,
    resource_id: str,
    favorite_type: str = "star",
) -> bool:
    """Check if a resource is favorited by user."""
    stmt = select(Favorite.id).where(
        Favorite.user_id == user_id,
        Favorite.resource_type == resource_type,
        Favorite.resource_id == resource_id,
        Favorite.favorite_type == favorite_type,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return row is not None


async def get_favorite_counts(
    db: AsyncSession,
    entity_id: str,
    resource_type: str,
    resource_id: str,
) -> dict:
    """Count favorites by type for a resource.

    Returns: {"star": N, "pin": N, "bookmark": N}
    """
    stmt = (
        select(Favorite.favorite_type, func.count())
        .where(
            Favorite.entity_id == entity_id,
            Favorite.resource_type == resource_type,
            Favorite.resource_id == resource_id,
        )
        .group_by(Favorite.favorite_type)
    )
    rows = await db.execute(stmt)
    counts = {r[0]: r[1] for r in rows.all()}
    return {
        "star": counts.get("star", 0),
        "pin": counts.get("pin", 0),
        "bookmark": counts.get("bookmark", 0),
    }


async def get_pinned_items(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
) -> list[dict]:
    """Get all pinned items for quick access sidebar."""
    stmt = (
        select(Favorite)
        .where(
            Favorite.entity_id == entity_id,
            Favorite.user_id == user_id,
            Favorite.favorite_type == "pin",
        )
        .order_by(Favorite.created_at.desc())
    )
    rows = await db.execute(stmt)
    return [
        {
            "resource_type": f.resource_type,
            "resource_id": f.resource_id,
            "note": f.note,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in rows.scalars().all()
    ]
