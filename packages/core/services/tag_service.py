"""Tag service — create, list, delete tags and tag/untag resources."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.tag import ResourceTag, Tag


async def create_tag(
    db: AsyncSession,
    entity_id: str,
    name: str,
    *,
    color: str | None = None,
    description: str | None = None,
) -> Tag:
    """Create a tag (or return existing if name already exists)."""
    result = await db.execute(
        select(Tag).where(Tag.entity_id == entity_id, Tag.name == name)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    tag = Tag(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        color=color,
        description=description,
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


async def list_tags(
    db: AsyncSession, entity_id: str, search: str | None = None
) -> list[Tag]:
    """List all tags for an entity, optionally filtered by search prefix (autocomplete)."""
    stmt = select(Tag).where(Tag.entity_id == entity_id)
    if search:
        stmt = stmt.where(Tag.name.ilike(f"{search}%"))
    stmt = stmt.order_by(Tag.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def delete_tag(db: AsyncSession, tag_id: str, entity_id: str) -> bool:
    """Delete a tag and all its associations."""
    result = await db.execute(
        select(Tag).where(Tag.id == tag_id, Tag.entity_id == entity_id)
    )
    tag = result.scalar_one_or_none()
    if not tag:
        return False

    await db.execute(delete(ResourceTag).where(ResourceTag.tag_id == tag_id))
    await db.delete(tag)
    await db.commit()
    return True


async def tag_resource(
    db: AsyncSession,
    entity_id: str,
    tag_name: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    """Add a tag to a resource. Creates the tag if it doesn't exist. Returns True if newly added."""
    tag = await create_tag(db, entity_id, tag_name)

    existing = await db.execute(
        select(ResourceTag).where(
            ResourceTag.tag_id == tag.id,
            ResourceTag.resource_type == resource_type,
            ResourceTag.resource_id == resource_id,
        )
    )
    if existing.scalar_one_or_none():
        return False

    rt = ResourceTag(
        tag_id=tag.id,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    db.add(rt)
    await db.commit()
    return True


async def untag_resource(
    db: AsyncSession,
    entity_id: str,
    tag_name: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    """Remove a tag from a resource."""
    result = await db.execute(
        select(Tag).where(Tag.entity_id == entity_id, Tag.name == tag_name)
    )
    tag = result.scalar_one_or_none()
    if not tag:
        return False

    del_result = await db.execute(
        delete(ResourceTag).where(
            ResourceTag.tag_id == tag.id,
            ResourceTag.resource_type == resource_type,
            ResourceTag.resource_id == resource_id,
        )
    )
    await db.commit()
    return del_result.rowcount > 0


async def get_resource_tags(
    db: AsyncSession, entity_id: str, resource_type: str, resource_id: str
) -> list[Tag]:
    """Get all tags for a specific resource."""
    stmt = (
        select(Tag)
        .join(ResourceTag, ResourceTag.tag_id == Tag.id)
        .where(
            Tag.entity_id == entity_id,
            ResourceTag.resource_type == resource_type,
            ResourceTag.resource_id == resource_id,
        )
        .order_by(Tag.name)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def find_by_tag(
    db: AsyncSession,
    entity_id: str,
    tag_name: str,
    resource_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Find all resources with a specific tag.
    Returns: [{resource_type, resource_id, tagged_at}, ...]
    """
    stmt = (
        select(
            ResourceTag.resource_type,
            ResourceTag.resource_id,
            ResourceTag.created_at,
        )
        .join(Tag, Tag.id == ResourceTag.tag_id)
        .where(Tag.entity_id == entity_id, Tag.name == tag_name)
    )
    if resource_type:
        stmt = stmt.where(ResourceTag.resource_type == resource_type)
    stmt = stmt.order_by(ResourceTag.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    return [
        {
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "tagged_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in result.all()
    ]


async def get_popular_tags(
    db: AsyncSession, entity_id: str, limit: int = 20
) -> list[dict]:
    """Get most-used tags. Returns: [{name, color, count}, ...]"""
    stmt = (
        select(Tag.name, Tag.color, func.count(ResourceTag.tag_id).label("count"))
        .join(ResourceTag, ResourceTag.tag_id == Tag.id)
        .where(Tag.entity_id == entity_id)
        .group_by(Tag.id, Tag.name, Tag.color)
        .order_by(func.count(ResourceTag.tag_id).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [
        {"name": row.name, "color": row.color, "count": row.count}
        for row in result.all()
    ]
