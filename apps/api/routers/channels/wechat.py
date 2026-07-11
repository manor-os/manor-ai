"""WeChat Official Account webhook endpoints.

GET  /api/v1/channels/wechat/callback  — WeChat server verification
POST /api/v1/channels/wechat/callback  — Receive messages from WeChat

WeChat requires a fixed callback URL during Official Account configuration.
The channel_config_id is passed as a query parameter so multiple OA accounts
can share the same endpoint.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.wechat_adapter import WeChatAdapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/wechat", tags=["channels"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
) -> tuple[WeChatAdapter, ChannelConfig]:
    """Load ChannelConfig and build a WeChatAdapter from its credentials."""
    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )
        cc = result.scalar_one_or_none()

    if not cc:
        raise HTTPException(404, "Channel config not found")

    creds = cc.credentials or {}
    app_id = creds.get("app_id", "")
    app_secret = creds.get("app_secret", "")
    token = creds.get("token", "")

    if not app_id or not app_secret or not token:
        raise HTTPException(500, "WeChat channel config is missing required credentials (app_id, app_secret, token)")

    adapter = WeChatAdapter(
        app_id=app_id,
        app_secret=app_secret,
        token=token,
        encoding_aes_key=creds.get("encoding_aes_key"),
    )
    return adapter, cc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/callback", response_class=PlainTextResponse)
async def wechat_verify(
    signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    config_id: str = Query(..., description="ChannelConfig ID for this WeChat OA"),
):
    """WeChat server verification (GET).

    When configuring the callback URL in the WeChat OA admin panel,
    WeChat sends a GET request with signature, timestamp, nonce, and echostr.
    We must return echostr if the signature is valid.
    """
    adapter, _cc = await _get_adapter_and_config(config_id)

    try:
        echo = await adapter.verify_callback(signature, timestamp, nonce, echostr)
        return PlainTextResponse(echo)
    except ValueError:
        raise HTTPException(403, "Signature verification failed")


@router.post("/callback", response_class=PlainTextResponse)
async def wechat_receive(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this WeChat OA"),
    signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    msg_signature: str = Query(
        "", description="Signature used in encrypted mode (not yet supported)."
    ),
):
    """Receive inbound messages from WeChat (POST).

    WeChat POSTs XML-formatted messages to this endpoint. We parse the
    message, log it via handle_inbound_message, and optionally reply.

    Returns "success" to acknowledge receipt (WeChat requires a response
    within 5 seconds; async processing should happen in background).
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    # Reject encrypted-mode callbacks loudly — we don't decrypt yet.
    # Admins who set an encoding_aes_key should turn it off in the OA
    # panel until we ship AES-CBC + PKCS7 decryption.
    if msg_signature or adapter.encoding_aes_key:
        logger.warning(
            "WeChat encrypted-mode callback received for config %s — "
            "not supported. Switch the OA callback to 'plain text' mode.",
            config_id,
        )
        raise HTTPException(
            501,
            "WeChat encrypted-mode callbacks are not supported. "
            "Switch to 'plain text' in the Official Account admin panel.",
        )

    # Plain-mode signature is mandatory — without it the endpoint is open.
    if not (signature and timestamp and nonce):
        raise HTTPException(403, "Missing signature/timestamp/nonce query params.")
    expected = adapter._sign(adapter.token, timestamp, nonce)
    if expected != signature:
        raise HTTPException(403, "Signature verification failed")

    # Parse XML body
    body = await request.body()
    xml_body = body.decode("utf-8")

    try:
        parsed = await adapter.handle_message(xml_body)
    except Exception:
        logger.exception("Failed to parse WeChat message")
        return PlainTextResponse("success")

    # Log the inbound message
    try:
        async with async_session() as db:
            await handle_inbound_message(
                db,
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                payload={
                    "from": parsed["sender_id"],
                    "to": parsed["recipient_id"],
                    "content": parsed["content"],
                    "message_id": parsed.get("msg_id"),
                    "channel_type": "wechat",
                    "metadata": {
                        "message_type": parsed["message_type"],
                        "media_id": parsed.get("media_id"),
                    },
                },
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to log inbound WeChat message")

    # Enqueue agent dispatch on Celery — WeChat's 5-second ack is hard,
    # and the broker keeps the task alive across worker restarts.
    dispatch_inbound_task.delay(
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        channel_type="wechat",
        sender_id=parsed["sender_id"],
        sender_name=parsed.get("sender_name"),
        chat_id=parsed["sender_id"],  # WeChat replies go to the OpenID
        content=parsed.get("content", "") or "",
    )

    return PlainTextResponse("success")
