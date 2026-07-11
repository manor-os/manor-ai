"""Webhook management endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import webhook_service
from apps.api.deps import get_current_user, require_permission
from packages.core.permissions import Permission

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


# ── Schemas ──


class WebhookEndpointCreate(BaseModel):
    url: str
    events: list[str] = []
    secret: str | None = None
    headers: dict = {}
    description: str | None = None


class WebhookEndpointUpdate(BaseModel):
    url: str | None = None
    events: list[str] | None = None
    secret: str | None = None
    headers: dict | None = None
    description: str | None = None
    enabled: bool | None = None


class WebhookEndpointResponse(BaseModel):
    id: str
    entity_id: str
    url: str
    secret: str | None = None
    events: list[str] = []
    headers: dict = {}
    enabled: bool = True
    description: str | None = None
    last_triggered_at: datetime | None = None
    last_status: str | None = None
    consecutive_failures: int = 0
    created_at: datetime
    updated_at: datetime | None = None


class WebhookDeliveryResponse(BaseModel):
    id: str
    endpoint_id: str
    event_type: str
    payload: dict
    status: str
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None
    attempt: int = 1
    duration_ms: float | None = None
    created_at: datetime


class TestResultResponse(BaseModel):
    success: bool
    delivery_id: str | None = None
    status_code: int | None = None
    duration_ms: float | None = None
    error: str | None = None


# ── Helpers ──


def _endpoint_to_response(ep) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        id=ep.id,
        entity_id=ep.entity_id,
        url=ep.url,
        secret=ep.secret,
        events=ep.events or [],
        headers=ep.headers or {},
        enabled=ep.enabled,
        description=ep.description,
        last_triggered_at=ep.last_triggered_at,
        last_status=ep.last_status,
        consecutive_failures=ep.consecutive_failures,
        created_at=ep.created_at,
        updated_at=ep.updated_at,
    )


def _delivery_to_response(d) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        id=d.id,
        endpoint_id=d.endpoint_id,
        event_type=d.event_type,
        payload=d.payload,
        status=d.status,
        status_code=d.status_code,
        response_body=d.response_body,
        error=d.error,
        attempt=d.attempt,
        duration_ms=d.duration_ms,
        created_at=d.created_at,
    )


# ── Endpoints ──


@router.get("", response_model=list[WebhookEndpointResponse])
async def list_endpoints(
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """List all webhook endpoints for the current entity."""
    endpoints = await webhook_service.list_endpoints(db, user.entity_id)
    return [_endpoint_to_response(ep) for ep in endpoints]


@router.post("", response_model=WebhookEndpointResponse, status_code=201)
async def create_endpoint(
    req: WebhookEndpointCreate,
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """Register a new webhook endpoint."""
    endpoint = await webhook_service.create_endpoint(
        db,
        entity_id=user.entity_id,
        url=req.url,
        events=req.events,
        secret=req.secret,
        headers=req.headers,
        description=req.description,
    )
    return _endpoint_to_response(endpoint)


@router.get("/{endpoint_id}", response_model=WebhookEndpointResponse)
async def get_endpoint(
    endpoint_id: str,
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """Get a single webhook endpoint (includes secret for owner)."""
    endpoint = await webhook_service.get_endpoint(db, endpoint_id, user.entity_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return _endpoint_to_response(endpoint)


@router.put("/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_endpoint(
    endpoint_id: str,
    req: WebhookEndpointUpdate,
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """Update a webhook endpoint."""
    updates = req.model_dump(exclude_unset=True)
    endpoint = await webhook_service.update_endpoint(
        db, endpoint_id, user.entity_id, **updates
    )
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return _endpoint_to_response(endpoint)


@router.delete("/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: str,
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """Delete a webhook endpoint."""
    deleted = await webhook_service.delete_endpoint(db, endpoint_id, user.entity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")


@router.post("/{endpoint_id}/test", response_model=TestResultResponse)
async def test_endpoint(
    endpoint_id: str,
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """Send a test ping event to a webhook endpoint."""
    result = await webhook_service.test_endpoint(db, endpoint_id, user.entity_id)
    if result.get("error") == "Endpoint not found":
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return TestResultResponse(**result)


@router.get("/{endpoint_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_deliveries(
    endpoint_id: str,
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(require_permission(Permission.ADMIN_WEBHOOKS)),
    db: AsyncSession = Depends(get_db),
):
    """List delivery history for a webhook endpoint."""
    # Verify endpoint exists and belongs to user
    endpoint = await webhook_service.get_endpoint(db, endpoint_id, user.entity_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    deliveries = await webhook_service.list_deliveries(
        db, endpoint_id, user.entity_id, limit=limit
    )
    return [_delivery_to_response(d) for d in deliveries]
