"""Comment endpoints — threaded comments on tasks, documents, etc."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.comment import Comment
from packages.core.models.permission import Capability, ResourceType
from packages.core.models.user import User
from packages.core.services.document_access import (
    effective_document_capabilities_for_user,
    get_visible_document,
)
from packages.core.services.comment_service import (
    create_comment,
    list_comments,
    update_comment,
    delete_comment,
    add_reaction,
    get_comment_count,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/comments", tags=["comments"])


# ── Schemas ──

class CommentResponse(BaseModel):
    id: str
    entity_id: str
    resource_type: str
    resource_id: str
    parent_id: str | None = None
    user_id: str
    user_email: str | None = None
    display_name: str | None = None
    user_display_name: str | None = None
    user_avatar_url: str | None = None
    content: str
    mentions: list = []
    anchor: dict = {}
    reactions: dict = {}
    is_edited: bool = False
    status: str = "active"
    created_at: str | None = None
    updated_at: str | None = None
    replies: list = []


class CommentCreateRequest(BaseModel):
    resource_type: str
    resource_id: str
    content: str
    parent_id: str | None = None
    mentions: list | None = None
    anchor: dict | None = None


class CommentUpdateRequest(BaseModel):
    content: str


class ReactionRequest(BaseModel):
    reaction: str


class CommentCountResponse(BaseModel):
    count: int


# ── Endpoints ──

@router.get("", response_model=list[CommentResponse])
async def list_resource_comments(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List threaded comments for a resource."""
    await _require_resource_access(
        db,
        user,
        resource_type=resource_type,
        resource_id=resource_id,
        require_comment=False,
    )
    return await list_comments(db, user.entity_id, resource_type, resource_id)


@router.post("", response_model=CommentResponse, status_code=201)
async def create_new_comment(
    body: CommentCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a comment on a resource."""
    await _require_resource_access(
        db,
        user,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        require_comment=True,
    )
    try:
        comment = await create_comment(
            db,
            entity_id=user.entity_id,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            user_id=user.id,
            content=body.content,
            user_email=user.email,
            parent_id=body.parent_id,
            mentions=body.mentions,
            anchor=body.anchor,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_response(comment, user)


@router.put("/{comment_id}", response_model=CommentResponse)
async def edit_comment(
    comment_id: str,
    body: CommentUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a comment (only by author)."""
    await _require_comment_resource_access(
        db,
        user,
        comment_id=comment_id,
        require_comment=True,
    )
    try:
        comment = await update_comment(db, comment_id, user.entity_id, user.id, body.content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return _to_response(comment, user)


@router.delete("/{comment_id}", status_code=204)
async def remove_comment(
    comment_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a comment (only by author)."""
    await _require_comment_resource_access(
        db,
        user,
        comment_id=comment_id,
        require_comment=False,
    )
    try:
        ok = await delete_comment(db, comment_id, user.entity_id, user.id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Comment not found")


@router.post("/{comment_id}/reactions")
async def toggle_reaction(
    comment_id: str,
    body: ReactionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a reaction on a comment."""
    await _require_comment_resource_access(
        db,
        user,
        comment_id=comment_id,
        require_comment=False,
    )
    reactions = await add_reaction(db, comment_id, user.entity_id, user.id, body.reaction)
    if reactions is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"reactions": reactions}


@router.get("/count", response_model=CommentCountResponse)
async def count_comments(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get comment count for a resource."""
    await _require_resource_access(
        db,
        user,
        resource_type=resource_type,
        resource_id=resource_id,
        require_comment=False,
    )
    count = await get_comment_count(db, user.entity_id, resource_type, resource_id)
    return {"count": count}


# ── Helpers ──

def _is_document_resource(resource_type: str) -> bool:
    return resource_type in {ResourceType.DOCUMENT, "documents"}


def _user_is_document_manager(user: User, doc) -> bool:
    if user.role in {"owner", "admin"}:
        return True
    if getattr(doc, "owner_id", None) == user.id:
        return True
    created_by = getattr(doc, "created_by", None)
    return bool(created_by and created_by in {user.id, user.email, user.display_name})


async def _require_resource_access(
    db: AsyncSession,
    user: User,
    *,
    resource_type: str,
    resource_id: str,
    require_comment: bool,
) -> None:
    """Enforce document ACLs for generic comments while leaving other
    resource types on their existing behavior.
    """
    if not _is_document_resource(resource_type):
        return

    doc = await get_visible_document(
        db,
        resource_id,
        user.entity_id,
        user_id=user.id,
        role=user.role,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not require_comment:
        return
    if _user_is_document_manager(user, doc):
        return

    capabilities = await effective_document_capabilities_for_user(
        db,
        document=doc,
        user_id=user.id,
        role=user.role,
    )
    if Capability.COMMENT not in capabilities:
        raise HTTPException(
            status_code=403,
            detail="Only users with comment access can comment on this document",
        )


async def _require_comment_resource_access(
    db: AsyncSession,
    user: User,
    *,
    comment_id: str,
    require_comment: bool,
) -> None:
    result = await db.execute(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.entity_id == user.entity_id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    await _require_resource_access(
        db,
        user,
        resource_type=comment.resource_type,
        resource_id=comment.resource_id,
        require_comment=require_comment,
    )


def _user_display_name(user: User | None) -> str | None:
    if not user:
        return None
    full_name = " ".join(
        part.strip()
        for part in (user.first_name or "", user.last_name or "")
        if part and part.strip()
    )
    return user.display_name or full_name or None


def _to_response(c, author: User | None = None) -> dict:
    display_name = _user_display_name(author)
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
        "content": c.content,
        "mentions": c.mentions,
        "anchor": c.anchor or {},
        "reactions": c.reactions,
        "is_edited": c.is_edited,
        "status": c.status,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "replies": [],
    }
