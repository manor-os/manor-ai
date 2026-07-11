"""Activity feed and event log endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.event_service import list_events, get_activity_feed, log_event
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])


# ── Schemas ──

class EventLogResponse(BaseModel):
    id: str
    entity_id: str | None = None
    event_type: str
    source: str | None = None
    payload: dict = {}
    created_at: datetime


class EventLogListResponse(BaseModel):
    items: list[EventLogResponse]
    total: int


class EventLogCreateRequest(BaseModel):
    event_type: str
    source: str | None = None
    payload: dict = {}


class FeedItem(BaseModel):
    id: str
    event_type: str
    source: str | None = None
    description: str
    timestamp: str
    icon: str
    link: str | None = None


# ── Endpoints ──

@router.get("/events", response_model=EventLogListResponse)
async def get_events(
    event_type: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List raw event logs for the current entity."""
    items, total = await list_events(
        db, user.entity_id,
        event_type=event_type, source=source,
        limit=limit, offset=offset,
    )
    return EventLogListResponse(
        items=[
            EventLogResponse(
                id=e.id, entity_id=e.entity_id,
                event_type=e.event_type, source=e.source,
                payload=e.payload or {}, created_at=e.created_at,
            )
            for e in items
        ],
        total=total,
    )


@router.get("/feed", response_model=list[FeedItem])
async def get_feed(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Human-readable activity feed."""
    feed = await get_activity_feed(db, user.entity_id, limit=limit)
    return feed


@router.post("/events", response_model=EventLogResponse, status_code=201)
async def create_event(
    req: EventLogCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually log an event (for testing/internal use)."""
    entry = await log_event(
        db, user.entity_id,
        event_type=req.event_type,
        source=req.source,
        payload=req.payload,
    )
    return EventLogResponse(
        id=entry.id, entity_id=entry.entity_id,
        event_type=entry.event_type, source=entry.source,
        payload=entry.payload or {}, created_at=entry.created_at,
    )
