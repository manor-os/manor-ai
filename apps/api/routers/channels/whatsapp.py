"""WhatsApp Cloud API webhook endpoints.

GET  /api/v1/channels/whatsapp/webhook  — webhook verification (Meta challenge)
POST /api/v1/channels/whatsapp/webhook  — incoming messages and status updates

Meta requires a fixed callback URL during app configuration.
The channel_config_id is passed as a query parameter so multiple WhatsApp
Business accounts can share the same endpoint.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.whatsapp_adapter import WhatsAppAdapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["channels"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
) -> tuple[WhatsAppAdapter, ChannelConfig]:
    """Load ChannelConfig and build a WhatsAppAdapter from its credentials."""
    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )
        cc = result.scalar_one_or_none()

    if not cc:
        raise HTTPException(404, "Channel config not found")

    creds = cc.credentials or {}
    phone_number_id = creds.get("phone_number_id", "")
    access_token = creds.get("access_token", creds.get("api_token", ""))
    verify_token = creds.get("verify_token", "")
    api_version = (cc.config or {}).get("api_version", "v18.0")

    if not phone_number_id or not access_token:
        raise HTTPException(500, "WhatsApp channel config is missing required credentials (phone_number_id, access_token)")

    adapter = WhatsAppAdapter(
        phone_number_id=phone_number_id,
        access_token=access_token,
        verify_token=verify_token,
        api_version=api_version,
    )
    return adapter, cc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/webhook", response_class=PlainTextResponse)
async def whatsapp_verify(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this WhatsApp Business account"),
):
    """WhatsApp webhook verification (GET).

    Meta sends a GET request with hub.mode, hub.verify_token, and hub.challenge
    when configuring the webhook URL. We must return hub.challenge if the token
    matches to confirm the endpoint.
    """
    # Meta uses "hub." prefixed query params
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    adapter, _cc = await _get_adapter_and_config(config_id)

    result = await adapter.verify_webhook(mode, token, challenge)
    if result is not None:
        return PlainTextResponse(result)

    raise HTTPException(403, "Webhook verification failed")


@router.post("/webhook")
async def whatsapp_receive(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this WhatsApp Business account"),
):
    """Receive inbound messages and status updates from WhatsApp.

    Meta POSTs JSON payloads with message and status events.
    Returns 200 OK immediately to acknowledge receipt (Meta requires
    acknowledgement within 20 seconds).
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        logger.exception("Failed to parse WhatsApp webhook JSON body")
        return {"status": "ok"}

    # Parse events
    try:
        events = await adapter.handle_webhook(body)
    except Exception:
        logger.exception("Failed to parse WhatsApp webhook events")
        return {"status": "ok"}

    # Process each event
    for event in events:
        # Skip status updates for now (delivery receipts)
        if event.get("message_type") == "status":
            logger.debug(
                "WhatsApp status update: id=%s status=%s",
                event.get("msg_id"), event.get("status"),
            )
            continue

        # Log the inbound message
        try:
            async with async_session() as db:
                await handle_inbound_message(
                    db,
                    entity_id=cc.entity_id,
                    channel_config_id=cc.id,
                    payload={
                        "from": event["sender_id"],
                        "to": cc.credentials.get("phone_number_id", ""),
                        "content": event["content"],
                        "message_id": event.get("msg_id"),
                        "wamid": event.get("msg_id"),
                        "channel_type": "whatsapp",
                        "metadata": {
                            "message_type": event["message_type"],
                            "profile_name": event.get("profile_name", ""),
                        },
                    },
                )
                await db.commit()

            # Mark the message as read (best-effort)
            try:
                if event.get("msg_id"):
                    await adapter.mark_as_read(event["msg_id"])
            except Exception:
                logger.debug("Failed to mark WhatsApp message as read: %s", event.get("msg_id"))

            # Enqueue agent dispatch — WhatsApp's ack budget is generous
            # (~20 s) but a slow LLM would still trip it.
            dispatch_inbound_task.delay(
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                channel_type="whatsapp",
                sender_id=str(event["sender_id"]),
                sender_name=event.get("profile_name"),
                chat_id=str(event["sender_id"]),
                content=event.get("content", "") or "",
            )

        except Exception:
            logger.exception("Failed to log inbound WhatsApp message from %s", event.get("sender_id"))

    return {"status": "ok"}
