"""Integration-session endpoints — operator-side capture + admin.

Used by the M7 browser adapter. The capture flow is HITL: the operator
runs a Manor-spawned Playwright window, signs in manually, then dumps
the storage_state. This router exposes:

  GET    /api/v1/integration-sessions                   list sessions for the entity
  POST   /api/v1/integration-sessions/start             begin capture (returns session_id)
  POST   /api/v1/integration-sessions/{id}/finalize     persist the storage_state
  POST   /api/v1/integration-sessions/{id}/revoke       permanent revoke
  POST   /api/v1/integration-sessions/{id}/expire       mark expired (re-pair flow)

The decrypt endpoint is intentionally absent: the storage_state is
only ever read by workers via ``CredentialService.lease_browser_session``,
never returned over the operator-facing API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.integrations.sessions import (
    SessionNotFound,
    expire_session,
    finalize_capture,
    list_sessions,
    revoke_session,
    start_capture,
)
from packages.core.models.user import User

router = APIRouter(prefix="/api/v1/integration-sessions", tags=["integration-sessions"])


# ── Models ────────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    id: str
    provider: str
    label: Optional[str] = None
    status: str
    last_validated_at: Optional[datetime] = None
    validated_steps: int
    expired_at: Optional[datetime] = None
    expired_reason: Optional[str] = None
    created_at: datetime


class StartCaptureRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=50)
    label: Optional[str] = Field(None, max_length=100)
    expected_login_url: Optional[str] = None
    health_check: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class StartCaptureResponse(BaseModel):
    session_id: str
    provider: str
    label: Optional[str] = None
    expected_login_url: Optional[str] = None


class FinalizeCaptureRequest(BaseModel):
    storage_state: dict[str, Any]
    user_agent: Optional[str] = None
    viewport: Optional[dict[str, int]] = None


class ExpireRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=200)
    notify_chat: bool = True


class RevokeRequest(BaseModel):
    reason: str = "manual_revoke"


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("", response_model=list[SessionSummary])
async def list_entity_sessions(
    provider: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_sessions(db, entity_id=user.entity_id, provider=provider)
    return [
        SessionSummary(
            id=r.id,
            provider=r.provider,
            label=r.label,
            status=r.status,
            last_validated_at=r.last_validated_at,
            validated_steps=r.validated_steps,
            expired_at=r.expired_at,
            expired_reason=r.expired_reason,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/start", response_model=StartCaptureResponse, status_code=201)
async def start(
    req: StartCaptureRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cap = await start_capture(
        db,
        entity_id=user.entity_id,
        provider=req.provider,
        label=req.label,
        expected_login_url=req.expected_login_url,
        health_check=req.health_check,
        metadata=req.metadata,
    )
    await db.commit()
    return StartCaptureResponse(
        session_id=cap.session_id,
        provider=cap.provider,
        label=cap.label,
        expected_login_url=cap.expected_login_url,
    )


@router.post("/{session_id}/finalize", response_model=SessionSummary)
async def finalize(
    session_id: str,
    req: FinalizeCaptureRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await finalize_capture(
            db,
            session_id=session_id,
            storage_state=req.storage_state,
            user_agent=req.user_agent,
            viewport=req.viewport,
        )
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc))
    await db.commit()
    return await _summary(db, session_id, user.entity_id)


@router.post("/{session_id}/expire", response_model=SessionSummary)
async def expire(
    session_id: str,
    req: ExpireRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await expire_session(
            db, session_id=session_id, reason=req.reason,
            notify_chat=req.notify_chat,
        )
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc))
    await db.commit()
    return await _summary(db, session_id, user.entity_id)


@router.post("/{session_id}/revoke", response_model=SessionSummary)
async def revoke(
    session_id: str,
    req: RevokeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await revoke_session(db, session_id=session_id, reason=req.reason)
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc))
    await db.commit()
    return await _summary(db, session_id, user.entity_id)


# ── Helpers ───────────────────────────────────────────────────────────

async def _summary(
    db: AsyncSession, session_id: str, entity_id: str,
) -> SessionSummary:
    """Tenant-scoped re-fetch — guards against cross-entity ID guessing."""
    from sqlalchemy import select

    from packages.core.models.integration_session import IntegrationSession

    row = (await db.execute(
        select(IntegrationSession).where(
            IntegrationSession.id == session_id,
            IntegrationSession.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "session not found")
    return SessionSummary(
        id=row.id,
        provider=row.provider,
        label=row.label,
        status=row.status,
        last_validated_at=row.last_validated_at,
        validated_steps=row.validated_steps,
        expired_at=row.expired_at,
        expired_reason=row.expired_reason,
        created_at=row.created_at,
    )
