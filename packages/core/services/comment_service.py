"""Comment service — threaded comments on any resource."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.comment import Comment
from packages.core.models.user import User


_DOCUMENT_COMMENT_RESOURCE_TYPES = ("document", "documents")


def normalize_comment_resource_type(resource_type: str) -> str:
    """Return the canonical stored name for known comment resource aliases."""
    normalized = resource_type.strip()
    if normalized.lower() in _DOCUMENT_COMMENT_RESOURCE_TYPES:
        return "document"
    return normalized


def _comment_resource_type_clause(resource_type: str):
    normalized = normalize_comment_resource_type(resource_type)
    if normalized == "document":
        return func.lower(func.trim(Comment.resource_type)).in_(
            _DOCUMENT_COMMENT_RESOURCE_TYPES
        )
    return Comment.resource_type == normalized


async def create_comment(
    db: AsyncSession,
    entity_id: str,
    resource_type: str,
    resource_id: str,
    user_id: str,
    content: str,
    *,
    user_email: str | None = None,
    parent_id: str | None = None,
    mentions: list | None = None,
    anchor: dict | None = None,
) -> Comment:
    """Create a comment on a resource."""
    resource_type = normalize_comment_resource_type(resource_type)
    if parent_id:
        result = await db.execute(
            select(Comment).where(
                Comment.id == parent_id,
                Comment.entity_id == entity_id,
                _comment_resource_type_clause(resource_type),
                Comment.resource_id == resource_id,
                Comment.status != "deleted",
            )
            .with_for_update()
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise ValueError("Parent comment not found")

    comment = Comment(
        id=generate_ulid(),
        entity_id=entity_id,
        resource_type=resource_type,
        resource_id=resource_id,
        parent_id=parent_id,
        user_id=user_id,
        user_email=user_email,
        content=content,
        mentions=mentions or [],
        anchor=anchor or {},
        reactions={},
        is_edited=False,
        status="active",
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


def _author_display_name(user: User | None, fallback: str | None = None) -> str:
    if user:
        full_name = " ".join(
            part.strip()
            for part in (user.first_name or "", user.last_name or "")
            if part and part.strip()
        )
        return user.display_name or full_name or fallback or "User"
    return fallback or "User"


def _comment_to_dict(
    c: Comment,
    author: User | None = None,
    *,
    redact_deleted: bool = False,
) -> dict:
    display_name = _author_display_name(author)
    return {
        "id": c.id,
        "entity_id": c.entity_id,
        "resource_type": c.resource_type,
        "resource_id": c.resource_id,
        "parent_id": c.parent_id,
        "user_id": c.user_id,
        "user_email": c.user_email,
        "display_name": display_name,
        "user_display_name": display_name,
        "user_avatar_url": author.avatar_url if author else None,
        "content": "" if redact_deleted else c.content,
        "mentions": [] if redact_deleted else c.mentions,
        "anchor": c.anchor or {},
        "reactions": {} if redact_deleted else c.reactions,
        "is_edited": False if redact_deleted else c.is_edited,
        "status": c.status,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "replies": [],
    }


async def list_comments(
    db: AsyncSession,
    entity_id: str,
    resource_type: str,
    resource_id: str,
    *,
    include_deleted: bool = False,
) -> list[dict]:
    """List comments as a threaded tree.

    Returns top-level comments with nested `replies`.
    """
    q = select(Comment).where(
        Comment.entity_id == entity_id,
        _comment_resource_type_clause(resource_type),
        Comment.resource_id == resource_id,
    )
    q = q.order_by(Comment.created_at.asc())

    result = await db.execute(q)
    comments = list(result.scalars().all())
    if not include_deleted:
        comments_by_id = {comment.id: comment for comment in comments}
        visible_ids = {
            comment.id for comment in comments if comment.status != "deleted"
        }
        for comment in comments:
            if comment.status == "deleted":
                continue
            parent_id = comment.parent_id
            visited: set[str] = set()
            while parent_id and parent_id in comments_by_id and parent_id not in visited:
                visited.add(parent_id)
                visible_ids.add(parent_id)
                parent_id = comments_by_id[parent_id].parent_id
        comments = [comment for comment in comments if comment.id in visible_ids]
    user_ids = {c.user_id for c in comments if c.user_id}
    authors: dict[str, User] = {}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        authors = {u.id: u for u in users_result.scalars().all()}

    # Build tree
    by_id: dict[str, dict] = {}
    top_level: list[dict] = []

    for c in comments:
        d = _comment_to_dict(
            c,
            authors.get(c.user_id),
            redact_deleted=not include_deleted and c.status == "deleted",
        )
        by_id[c.id] = d

    for c in comments:
        d = by_id[c.id]
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id]["replies"].append(d)
        else:
            top_level.append(d)

    return top_level


async def update_comment(
    db: AsyncSession,
    comment_id: str,
    entity_id: str,
    user_id: str,
    content: str,
) -> Optional[Comment]:
    """Edit a comment (only by author). Sets is_edited=True."""
    result = await db.execute(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.entity_id == entity_id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        return None
    if comment.user_id != user_id:
        raise PermissionError("Only the author can edit this comment")
    if comment.status == "deleted":
        return None

    comment.content = content
    comment.is_edited = True
    await db.commit()
    await db.refresh(comment)
    return comment


async def delete_comment(
    db: AsyncSession,
    comment_id: str,
    entity_id: str,
    user_id: str,
) -> bool:
    """Soft-delete a comment (only by author)."""
    result = await db.execute(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.entity_id == entity_id,
        )
        .with_for_update()
    )
    comment = result.scalar_one_or_none()
    if not comment:
        return False
    if comment.user_id != user_id:
        raise PermissionError("Only the author can delete this comment")

    comment.status = "deleted"
    await db.commit()
    return True


async def add_reaction(
    db: AsyncSession,
    comment_id: str,
    entity_id: str,
    user_id: str,
    reaction: str,
) -> dict | None:
    """Toggle a reaction on a comment. Returns updated reactions dict."""
    result = await db.execute(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.entity_id == entity_id,
            Comment.status != "deleted",
        )
        .with_for_update()
    )
    comment = result.scalar_one_or_none()
    if not comment:
        return None

    reactions = dict(comment.reactions) if comment.reactions else {}
    users = list(reactions.get(reaction, []))

    if user_id in users:
        users.remove(user_id)
    else:
        users.append(user_id)

    if users:
        reactions[reaction] = users
    else:
        reactions.pop(reaction, None)

    comment.reactions = reactions
    await db.commit()
    await db.refresh(comment)
    return comment.reactions


async def delete_resource_comments(
    db: AsyncSession,
    entity_id: str,
    resource_type: str,
    resource_ids: list[str] | set[str] | tuple[str, ...],
) -> int:
    """Delete polymorphic comments alongside permanently deleted resources."""
    ids = {str(resource_id).strip() for resource_id in resource_ids if str(resource_id).strip()}
    if not ids:
        return 0
    result = await db.execute(
        delete(Comment).where(
            Comment.entity_id == entity_id,
            _comment_resource_type_clause(resource_type),
            Comment.resource_id.in_(ids),
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


async def get_comment_count(
    db: AsyncSession,
    entity_id: str,
    resource_type: str,
    resource_id: str,
) -> int:
    """Count active comments on a resource."""
    result = await db.execute(
        select(func.count()).select_from(Comment).where(
            Comment.entity_id == entity_id,
            _comment_resource_type_clause(resource_type),
            Comment.resource_id == resource_id,
            Comment.status == "active",
        )
    )
    return result.scalar_one()
