"""Webhook delivery service — register endpoints, sign & deliver events."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.webhook import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 10
DELIVERY_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BODY_LENGTH = 1000


# ── Endpoint CRUD ──


async def create_endpoint(
    db: AsyncSession,
    entity_id: str,
    url: str,
    events: list[str],
    *,
    secret: Optional[str] = None,
    headers: Optional[dict] = None,
    description: Optional[str] = None,
) -> WebhookEndpoint:
    """Register a new webhook endpoint."""
    secret = secret or secrets.token_hex(32)
    endpoint = WebhookEndpoint(
        id=generate_ulid(),
        entity_id=entity_id,
        url=url,
        secret=secret,
        events=events or [],
        headers=headers or {},
        description=description,
        enabled=True,
        consecutive_failures=0,
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


async def list_endpoints(
    db: AsyncSession, entity_id: str
) -> list[WebhookEndpoint]:
    """List all webhook endpoints for an entity."""
    result = await db.execute(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.entity_id == entity_id)
        .order_by(WebhookEndpoint.created_at.desc())
    )
    return list(result.scalars().all())


async def get_endpoint(
    db: AsyncSession, endpoint_id: str, entity_id: str
) -> WebhookEndpoint | None:
    """Get a single webhook endpoint, scoped to entity."""
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()


async def update_endpoint(
    db: AsyncSession, endpoint_id: str, entity_id: str, **kwargs
) -> WebhookEndpoint | None:
    """Update a webhook endpoint. Returns None if not found."""
    endpoint = await get_endpoint(db, endpoint_id, entity_id)
    if not endpoint:
        return None
    for key, value in kwargs.items():
        if hasattr(endpoint, key):
            setattr(endpoint, key, value)
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


async def delete_endpoint(
    db: AsyncSession, endpoint_id: str, entity_id: str
) -> bool:
    """Delete a webhook endpoint. Returns True if deleted."""
    endpoint = await get_endpoint(db, endpoint_id, entity_id)
    if not endpoint:
        return False
    await db.delete(endpoint)
    await db.flush()
    return True


# ── Test & Delivery ──


async def test_endpoint(
    db: AsyncSession, endpoint_id: str, entity_id: str
) -> dict:
    """Send a test ping event to a webhook endpoint."""
    endpoint = await get_endpoint(db, endpoint_id, entity_id)
    if not endpoint:
        return {"success": False, "error": "Endpoint not found"}

    test_payload = {"event": "webhook.test", "data": {"message": "Hello from Manor AI"}}
    delivery_id = generate_ulid()
    test_payload["webhook_id"] = delivery_id
    test_payload["timestamp"] = datetime.now(timezone.utc).isoformat()

    payload_bytes = json.dumps(test_payload).encode()
    signature = _sign_payload(payload_bytes, endpoint.secret or "")

    request_headers = {
        "Content-Type": "application/json",
        "X-Manor-Signature": signature,
        "X-Manor-Event": "webhook.test",
        "X-Manor-Delivery": delivery_id,
        **(endpoint.headers or {}),
    }

    delivery = WebhookDelivery(
        id=delivery_id,
        endpoint_id=endpoint.id,
        event_type="webhook.test",
        payload=test_payload,
        status="pending",
        attempt=1,
    )

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            resp = await client.post(endpoint.url, content=payload_bytes, headers=request_headers)
        duration = (time.monotonic() - start) * 1000

        delivery.status_code = resp.status_code
        delivery.response_body = resp.text[:MAX_RESPONSE_BODY_LENGTH] if resp.text else None
        delivery.duration_ms = duration

        if 200 <= resp.status_code < 300:
            delivery.status = "success"
            endpoint.last_status = "success"
            endpoint.consecutive_failures = 0
        else:
            delivery.status = "failed"
            delivery.error = f"HTTP {resp.status_code}"
            endpoint.last_status = "failed"
            endpoint.consecutive_failures += 1
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        delivery.status = "failed"
        delivery.error = str(exc)[:MAX_RESPONSE_BODY_LENGTH]
        delivery.duration_ms = duration
        endpoint.last_status = "failed"
        endpoint.consecutive_failures += 1

    endpoint.last_triggered_at = datetime.now(timezone.utc)
    db.add(delivery)
    await db.flush()

    return {
        "success": delivery.status == "success",
        "delivery_id": delivery.id,
        "status_code": delivery.status_code,
        "duration_ms": delivery.duration_ms,
        "error": delivery.error,
    }


async def deliver_event(entity_id: str, event_type: str, payload: dict):
    """Deliver an event to all subscribed webhook endpoints for this entity.

    Runs in its own session (fire-and-forget from event emitter).
    """
    try:
        from packages.core.database import async_session
        async with async_session() as db:
            result = await db.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.entity_id == entity_id,
                    WebhookEndpoint.enabled.is_(True),
                )
            )
            endpoints = result.scalars().all()

            for endpoint in endpoints:
                # If endpoint subscribes to specific events, check membership
                if endpoint.events and event_type not in endpoint.events:
                    continue
                await _deliver_to_endpoint(db, endpoint, event_type, payload)

            await db.commit()
    except Exception as e:
        logger.debug("Webhook delivery failed: %s", e)


async def _deliver_to_endpoint(
    db: AsyncSession,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: dict,
):
    """Deliver a single event to a single endpoint."""
    delivery_id = generate_ulid()
    full_payload = {
        "event": event_type,
        "data": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "webhook_id": delivery_id,
    }

    payload_bytes = json.dumps(full_payload).encode()
    signature = _sign_payload(payload_bytes, endpoint.secret or "")

    request_headers = {
        "Content-Type": "application/json",
        "X-Manor-Signature": signature,
        "X-Manor-Event": event_type,
        "X-Manor-Delivery": delivery_id,
        **(endpoint.headers or {}),
    }

    delivery = WebhookDelivery(
        id=delivery_id,
        endpoint_id=endpoint.id,
        event_type=event_type,
        payload=full_payload,
        status="pending",
        attempt=1,
    )

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            resp = await client.post(endpoint.url, content=payload_bytes, headers=request_headers)
        duration = (time.monotonic() - start) * 1000

        delivery.status_code = resp.status_code
        delivery.response_body = resp.text[:MAX_RESPONSE_BODY_LENGTH] if resp.text else None
        delivery.duration_ms = duration

        if 200 <= resp.status_code < 300:
            delivery.status = "success"
            endpoint.last_status = "success"
            endpoint.consecutive_failures = 0
        else:
            delivery.status = "failed"
            delivery.error = f"HTTP {resp.status_code}"
            endpoint.last_status = "failed"
            endpoint.consecutive_failures += 1
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        delivery.status = "failed"
        delivery.error = str(exc)[:MAX_RESPONSE_BODY_LENGTH]
        delivery.duration_ms = duration
        endpoint.last_status = "failed"
        endpoint.consecutive_failures += 1

    endpoint.last_triggered_at = datetime.now(timezone.utc)

    # Auto-disable after too many consecutive failures
    if endpoint.consecutive_failures > MAX_CONSECUTIVE_FAILURES:
        endpoint.enabled = False
        logger.warning(
            "Webhook endpoint %s auto-disabled after %d consecutive failures",
            endpoint.id, endpoint.consecutive_failures,
        )

    db.add(delivery)


async def list_deliveries(
    db: AsyncSession, endpoint_id: str, entity_id: str, limit: int = 50
) -> list[WebhookDelivery]:
    """List delivery attempts for an endpoint (after verifying ownership)."""
    # Verify the endpoint belongs to the entity
    endpoint = await get_endpoint(db, endpoint_id, entity_id)
    if not endpoint:
        return []
    result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """HMAC-SHA256 signature."""
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
