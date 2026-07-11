"""Twilio SMS + Voice webhook endpoints.

POST /api/v1/channels/twilio/sms     — incoming SMS webhook
POST /api/v1/channels/twilio/voice   — incoming voice webhook
POST /api/v1/channels/twilio/status  — delivery status callback

Twilio sends form-encoded POST requests to these endpoints.
The channel_config_id is passed as a query parameter so multiple Twilio
accounts can share the same endpoint.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channels.twilio_adapter import TwilioAdapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/twilio", tags=["channels"])


# Twilio provider statuses -> our MessageLog.status enum.
_TWILIO_SMS_STATUS_MAP = {
    "accepted": "queued",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "undelivered": "failed",
    "failed": "failed",
    "canceled": "failed",
    "cancelled": "failed",
}

_TWILIO_CALL_STATUS_MAP = {
    "queued": "queued",
    "ringing": "queued",
    "in-progress": "sent",
    "completed": "delivered",
    "busy": "failed",
    "no-answer": "failed",
    "failed": "failed",
    "canceled": "failed",
    "cancelled": "failed",
}


def _map_twilio_status(raw_status: str, *, is_call: bool) -> str:
    status = (raw_status or "").strip().lower()
    table = _TWILIO_CALL_STATUS_MAP if is_call else _TWILIO_SMS_STATUS_MAP
    return table.get(status, "sent" if status else "queued")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
) -> tuple[TwilioAdapter, ChannelConfig]:
    """Load ChannelConfig and build a TwilioAdapter from its credentials."""
    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )
        cc = result.scalar_one_or_none()

    if not cc:
        raise HTTPException(404, "Channel config not found")

    creds = cc.credentials or {}
    account_sid = creds.get("account_sid", "")
    auth_token = creds.get("auth_token", "")
    from_number = (
        (cc.config or {}).get("phone_number", "")
        or creds.get("phone_number", "")
        or creds.get("from_number", "")
    )

    if not account_sid or not auth_token:
        raise HTTPException(500, "Twilio channel config is missing required credentials (account_sid, auth_token)")

    adapter = TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
    )
    return adapter, cc


async def _validate_twilio_signature(
    adapter: TwilioAdapter,
    request: Request,
    form_data: dict[str, str],
) -> None:
    """Validate the X-Twilio-Signature header if present."""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("Twilio webhook received without X-Twilio-Signature header")
        return

    # Reconstruct the full URL that Twilio signed
    url = str(request.url)
    valid = await adapter.validate_signature(url, form_data, signature)
    if not valid:
        raise HTTPException(403, "Twilio signature validation failed")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/sms", response_class=PlainTextResponse)
async def twilio_sms_webhook(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this Twilio account"),
):
    """Receive inbound SMS messages from Twilio.

    Twilio POSTs form-encoded data with From, To, Body, MessageSid, etc.
    Returns empty TwiML response to acknowledge receipt.
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    # Parse form data
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}

    # Validate signature
    await _validate_twilio_signature(adapter, request, form_data)

    # Parse the message
    try:
        parsed = await adapter.handle_sms_webhook(form_data)
    except Exception:
        logger.exception("Failed to parse Twilio SMS webhook")
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )

    # Log the inbound message
    try:
        async with async_session() as db:
            await handle_inbound_message(
                db,
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                payload={
                    "from": parsed["sender_id"],
                    "to": parsed.get("recipient_id", ""),
                    "content": parsed["content"],
                    "message_id": parsed.get("msg_id"),
                    "MessageSid": parsed.get("msg_id"),
                    "channel_type": "twilio_sms",
                    "metadata": {
                        "message_type": parsed["message_type"],
                        "media_urls": parsed.get("media_urls", []),
                    },
                },
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to log inbound Twilio SMS message")

    # Enqueue agent dispatch — Twilio's 15-second webhook ack budget is
    # plenty for the broker round-trip; the LLM reply goes out-of-band
    # via TwilioAdapter.send_sms.
    dispatch_inbound_task.delay(
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        channel_type="twilio_sms",
        sender_id=str(parsed["sender_id"]),
        sender_name=None,
        chat_id=str(parsed["sender_id"]),
        content=parsed.get("content", "") or "",
    )

    # Return empty TwiML to acknowledge — reply is sent out-of-band
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


@router.post("/voice")
async def twilio_voice_webhook(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this Twilio account"),
):
    """Handle incoming voice calls from Twilio.

    Returns TwiML that hands the call over to our Media Streams
    websocket, which runs the real-time STT → agent → TTS loop. The
    websocket URL is derived from PUBLIC_BASE_URL; switch its scheme
    from https:// to wss:// for Twilio to open a streaming connection.
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    # Parse form data
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}

    # Validate signature
    await _validate_twilio_signature(adapter, request, form_data)

    # Resolve ChannelConfig: if this is a voice-flavoured entry, use it;
    # otherwise fall back to the same entity's twilio_voice config so
    # one Twilio number can serve both SMS and voice.
    from sqlalchemy import select
    voice_cc = cc
    if cc.channel_type != "twilio_voice":
        async with async_session() as db:
            voice_cc = (await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.entity_id == cc.entity_id,
                    ChannelConfig.channel_type == "twilio_voice",
                    ChannelConfig.status == "active",
                ).limit(1)
            )).scalar_one_or_none() or cc

    # Log the inbound call
    try:
        async with async_session() as db:
            await handle_inbound_message(
                db,
                entity_id=voice_cc.entity_id,
                channel_config_id=voice_cc.id,
                payload={
                    "from": form_data.get("From", ""),
                    "to": form_data.get("To", ""),
                    "content": f"Incoming call from {form_data.get('From', 'unknown')}",
                    "MessageSid": form_data.get("CallSid", ""),
                    "channel_type": "twilio_voice",
                    "metadata": {
                        "call_status": form_data.get("CallStatus", ""),
                        "direction": form_data.get("Direction", ""),
                    },
                },
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to log inbound Twilio voice call")

    # Build the wss:// URL for the Media Streams socket
    from packages.core.config import get_settings
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    wss_base = base.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{wss_base}/api/v1/channels/twilio_voice/stream/{voice_cc.id}"

    caller = form_data.get("From", "")
    to_number = form_data.get("To", "")

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Connect><Stream url="{stream_url}">'
        f'<Parameter name="from" value="{caller}"/>'
        f'<Parameter name="to" value="{to_number}"/>'
        '</Stream></Connect>'
        '</Response>'
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/status", response_class=PlainTextResponse)
async def twilio_status_callback(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this Twilio account"),
):
    """Handle delivery status callbacks from Twilio.

    Twilio POSTs form-encoded status updates:
        MessageSid, MessageStatus (queued, sent, delivered, undelivered, failed)
        or CallSid, CallStatus, CallDuration, etc.
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}

    # Validate signature
    await _validate_twilio_signature(adapter, request, form_data)

    # Log the status update
    is_call = bool(form_data.get("CallSid"))
    message_sid = form_data.get("MessageSid") or form_data.get("CallSid", "")
    status = form_data.get("MessageStatus") or form_data.get("CallStatus", "")
    logger.info(
        "Twilio status callback: sid=%s status=%s config=%s",
        message_sid, status, config_id,
    )

    # Update the most recent outbound message log for this sid.
    if message_sid:
        mapped_status = _map_twilio_status(status, is_call=is_call)
        try:
            async with async_session() as db:
                row = (await db.execute(
                    select(MessageLog).where(
                        MessageLog.channel_config_id == cc.id,
                        MessageLog.direction == "outbound",
                        MessageLog.external_id == message_sid,
                    ).order_by(MessageLog.created_at.desc()).limit(1)
                )).scalar_one_or_none()

                if row:
                    row.status = mapped_status
                    if mapped_status == "failed":
                        row.error_message = (
                            form_data.get("ErrorMessage")
                            or form_data.get("SmsStatus")
                            or form_data.get("CallStatus")
                            or row.error_message
                        )
                    if is_call:
                        duration = form_data.get("CallDuration")
                        if duration and str(duration).isdigit():
                            row.duration_seconds = int(duration)
                    await db.commit()
                else:
                    logger.info(
                        "Twilio status callback sid=%s had no outbound MessageLog match (config=%s)",
                        message_sid,
                        cc.id,
                    )
        except Exception:
            logger.exception(
                "Failed to persist Twilio status callback sid=%s config=%s",
                message_sid,
                cc.id,
            )

    return PlainTextResponse("ok")
