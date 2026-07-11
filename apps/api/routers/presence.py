"""Presence REST endpoints — who's online, viewing, typing."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from packages.core.models.user import User
from packages.core.services.presence_service import (
    get_presence_summary,
    get_viewers,
    get_typing_users,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/presence", tags=["presence"])


@router.get("")
async def online_users(user: User = Depends(get_current_user)):
    """Get online users summary for the current entity."""
    return get_presence_summary(user.entity_id)


@router.get("/viewers")
async def resource_viewers(
    resource: str = Query(..., description="Resource identifier, e.g. task:abc123"),
    user: User = Depends(get_current_user),
):
    """Get users currently viewing a specific resource."""
    return {"resource": resource, "viewers": get_viewers(user.entity_id, resource)}


@router.get("/typing")
async def typing_users(
    conversation_id: str = Query(..., description="Conversation ID"),
    user: User = Depends(get_current_user),
):
    """Get users currently typing in a conversation."""
    return {
        "conversation_id": conversation_id,
        "typing": get_typing_users(user.entity_id, conversation_id),
    }
