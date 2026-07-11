"""Tag endpoints — universal tagging system for any resource."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.tag_service import (
    create_tag,
    delete_tag,
    find_by_tag,
    get_popular_tags,
    get_resource_tags,
    list_tags,
    tag_resource,
    untag_resource,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])


# ── Schemas ──

class TagResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    color: str | None = None
    description: str | None = None
    created_at: str | None = None


class TagCreateRequest(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


class TagApplyRequest(BaseModel):
    tag_name: str
    resource_type: str
    resource_id: str


class TagRemoveRequest(BaseModel):
    tag_name: str
    resource_type: str
    resource_id: str


class ResourceResult(BaseModel):
    resource_type: str
    resource_id: str
    tagged_at: str | None = None


class PopularTagResponse(BaseModel):
    name: str
    color: str | None = None
    count: int


# ── Helpers ──

def _tag_to_response(tag) -> dict:
    return {
        "id": tag.id,
        "entity_id": tag.entity_id,
        "name": tag.name,
        "color": tag.color,
        "description": tag.description,
        "created_at": tag.created_at.isoformat() if tag.created_at else None,
    }


# ── Endpoints ──

@router.get("", response_model=list[TagResponse])
async def list_tags_endpoint(
    search: str | None = Query(None, description="Search prefix for autocomplete"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List/search tags for the current entity."""
    tags = await list_tags(db, user.entity_id, search=search)
    return [_tag_to_response(t) for t in tags]


@router.post("", response_model=TagResponse, status_code=201)
async def create_tag_endpoint(
    body: TagCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tag."""
    tag = await create_tag(
        db, user.entity_id, body.name, color=body.color, description=body.description
    )
    return _tag_to_response(tag)


@router.delete("/{tag_id}", status_code=204)
async def delete_tag_endpoint(
    tag_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tag and all its resource associations."""
    deleted = await delete_tag(db, tag_id, user.entity_id)
    if not deleted:
        raise HTTPException(404, "Tag not found")


@router.post("/apply", status_code=200)
async def apply_tag_endpoint(
    body: TagApplyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tag a resource. Creates the tag if it doesn't exist."""
    newly_added = await tag_resource(
        db, user.entity_id, body.tag_name, body.resource_type, body.resource_id
    )
    return {"applied": newly_added}


@router.post("/remove", status_code=200)
async def remove_tag_endpoint(
    body: TagRemoveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a tag from a resource."""
    removed = await untag_resource(
        db, user.entity_id, body.tag_name, body.resource_type, body.resource_id
    )
    return {"removed": removed}


@router.get("/resource", response_model=list[TagResponse])
async def get_resource_tags_endpoint(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all tags for a specific resource."""
    tags = await get_resource_tags(db, user.entity_id, resource_type, resource_id)
    return [_tag_to_response(t) for t in tags]


@router.get("/find/{tag_name}", response_model=list[ResourceResult])
async def find_by_tag_endpoint(
    tag_name: str,
    resource_type: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Find all resources with a specific tag."""
    results = await find_by_tag(
        db, user.entity_id, tag_name, resource_type=resource_type
    )
    return results


@router.get("/popular", response_model=list[PopularTagResponse])
async def popular_tags_endpoint(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get most-used tags."""
    return await get_popular_tags(db, user.entity_id, limit=limit)
