"""Memory endpoints -- CRUD, context retrieval, LLM extraction."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.memory_service import (
    add_memory,
    archive_memory,
    delete_memory,
    extract_memories_from_conversation,
    get_context_memories,
    list_memories,
    update_memory,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


# ── Schemas ──

class MemoryResponse(BaseModel):
    id: str
    entity_id: str
    agent_id: str | None = None
    user_id: str | None = None
    memory_type: str
    content: str
    importance: int = 5
    source: str | None = None
    metadata: dict = {}
    expires_at: str | None = None
    status: str = "active"


class MemoryCreateRequest(BaseModel):
    content: str
    memory_type: str = "fact"
    agent_id: str | None = None
    user_id: str | None = None
    importance: int = 5
    source: str | None = None
    metadata: dict = {}
    expires_at: datetime | None = None


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    memory_type: str | None = None
    importance: int | None = None
    source: str | None = None
    metadata: dict | None = None
    expires_at: datetime | None = None


class ExtractRequest(BaseModel):
    conversation_id: str
    agent_id: str | None = None
    user_id: str | None = None


class ContextResponse(BaseModel):
    context: str


def _to_response(mem) -> MemoryResponse:
    return MemoryResponse(
        id=mem.id,
        entity_id=mem.entity_id,
        agent_id=mem.agent_id,
        user_id=mem.user_id,
        memory_type=mem.memory_type,
        content=mem.content,
        importance=mem.importance,
        source=mem.source,
        metadata=mem.metadata_ or {},
        expires_at=mem.expires_at.isoformat() if mem.expires_at else None,
        status=mem.status,
    )


# ── Endpoints ──

@router.get("", response_model=list[MemoryResponse])
async def list_memories_endpoint(
    agent_id: str | None = Query(None),
    user_id: str | None = Query(None),
    type: str | None = Query(None, alias="type"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List memories for the current entity."""
    mems = await list_memories(
        db, user.entity_id,
        agent_id=agent_id,
        user_id=user_id,
        memory_type=type,
    )
    return [_to_response(m) for m in mems]


@router.post("", response_model=MemoryResponse, status_code=201)
async def create_memory_endpoint(
    body: MemoryCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a memory manually."""
    mem = await add_memory(
        db, user.entity_id,
        content=body.content,
        memory_type=body.memory_type,
        agent_id=body.agent_id,
        user_id=body.user_id,
        importance=body.importance,
        source=body.source or "manual",
        metadata=body.metadata,
        expires_at=body.expires_at,
    )
    await db.commit()
    return _to_response(mem)


@router.put("/{memory_id}", response_model=MemoryResponse)
async def update_memory_endpoint(
    memory_id: str,
    body: MemoryUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a memory entry."""
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if "metadata" in kwargs:
        kwargs["metadata_"] = kwargs.pop("metadata")
    mem = await update_memory(db, memory_id, user.entity_id, **kwargs)
    if not mem:
        raise HTTPException(404, "Memory not found")
    await db.commit()
    return _to_response(mem)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory_endpoint(
    memory_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a memory entry."""
    ok = await delete_memory(db, memory_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    await db.commit()


@router.post("/{memory_id}/archive", status_code=200)
async def archive_memory_endpoint(
    memory_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Archive a memory (soft remove from context)."""
    ok = await archive_memory(db, memory_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Memory not found")
    await db.commit()
    return {"status": "archived"}


@router.get("/context", response_model=ContextResponse)
async def get_context_endpoint(
    agent_id: str | None = Query(None),
    user_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get formatted memory context string (for debugging)."""
    ctx = await get_context_memories(
        db, user.entity_id, agent_id=agent_id, user_id=user_id
    )
    return ContextResponse(context=ctx)


@router.post("/extract", response_model=list[MemoryResponse])
async def extract_memories_endpoint(
    body: ExtractRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract memories from a conversation using LLM."""
    mems = await extract_memories_from_conversation(
        db, user.entity_id, body.conversation_id,
        agent_id=body.agent_id,
        user_id=body.user_id,
    )
    await db.commit()
    return [_to_response(m) for m in mems]
