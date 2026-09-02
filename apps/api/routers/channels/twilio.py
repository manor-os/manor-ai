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
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_credentials import lease_channel_credentials
from packages.core.services.channel_bindings import (
    resolve_unique_channel_binding_scope,
)
from packages.core.services.channels.twilio_adapter import TwilioAdapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.channel_inbound_receipts import (
    InboundDispatchClaim,
    InboundDispatchClaimOutcome,
    claim_inbound_dispatch,
    mark_inbound_dispatch_published,
    release_inbound_dispatch_claim,
)
from packages.core.tasks.channel_tasks import dispatch_inbound_task
from packages.core.services.voice.call_sessions import (
    create_call_session,
    finish_call_session,
    get_call_session_for_update,
    get_call_session_by_sid,
    get_call_session_by_token,
    mark_outbound_call_connecting,
    rotate_pending_call_session_token,
)
from packages.core.services.voice.twiml import build_stream_twiml

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

_TWILIO_CALL_SESSION_STATUS_MAP = {
    "queued": "connecting",
    "initiated": "connecting",
    "ringing": "connecting",
    "in-progress": "in_progress",
    "answered": "in_progress",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "canceled",
    "cancelled": "canceled",
}


def _map_twilio_status(raw_status: str, *, is_call: bool) -> str:
    status = (raw_status or "").strip().lower()
    table = _TWILIO_CALL_STATUS_MAP if is_call else _TWILIO_SMS_STATUS_MAP
    return table.get(status, "sent" if status else "queued")


async def _claim_received(config_id: str, message_sid: str) -> tuple[MessageLog | None, bool]:
    """Compatibility wrapper around the shared durable dispatch claim."""
    claim = await _claim_dispatch(config_id, message_sid)
    return claim.receipt, claim.acquired


async def _claim_dispatch(config_id: str, message_sid: str) -> InboundDispatchClaim:
    return await claim_inbound_dispatch(
        config_id=config_id,
        channel_type="twilio_sms",
        external_id=message_sid,
        session_factory=async_session,
    )


async def _release_claim(log_id: str, claim_id: str) -> None:
    await release_inbound_dispatch_claim(
        message_log_id=log_id,
        claim_id=claim_id,
        session_factory=async_session,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
    *,
    allowed_channel_types: tuple[str, ...] | None = None,
) -> tuple[TwilioAdapter, ChannelConfig]:
    """Load an active source-linked config and lease its Twilio credentials."""
    channel_types = allowed_channel_types or (
        "sms",
        "twilio_sms",
        "voice",
        "twilio_voice",
    )
    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == config_id,
                ChannelConfig.status == "active",
                ChannelConfig.channel_type.in_(channel_types),
            )
        )
        cc = result.scalar_one_or_none()

        if not cc:
            raise HTTPException(404, "Channel config not found")
        try:
            creds = await lease_channel_credentials(
                db,
                cc,
                reason="channel.twilio.webhook",
            )
        except ValueError as exc:
            raise HTTPException(410, "Twilio channel credential source is unavailable") from exc

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
    """Validate the X-Twilio-Signature header before trusting the payload.

    Unsigned requests are accepted only in an explicitly local runtime. This
    keeps existing local fixtures usable while staging and production always
    fail closed when Twilio's signature is missing or invalid.
    """
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        environment = os.getenv("MANOR_ENV", "production").strip().lower()
        local_override = os.getenv("TWILIO_ALLOW_UNSIGNED_LOCAL", "false").lower() in {
            "1", "true", "yes", "on",
        }
        if environment in {"local", "dev", "development", "test"} and local_override:
            logger.warning("Twilio webhook received without X-Twilio-Signature in local mode")
            return
        raise HTTPException(403, "Twilio signature is required")

    # Reconstruct the full URL that Twilio signed
    url = str(request.url)
    valid = await adapter.validate_signature(url, form_data, signature)
    if not valid:
        raise HTTPException(403, "Twilio signature validation failed")


async def _resolve_voice_channel_config(
    source: ChannelConfig,
    *,
    to_number: str,
) -> ChannelConfig:
    """Resolve only the voice config owned by the signed source connection."""
    if source.channel_type in {"voice", "twilio_voice"}:
        return source
    if not (
        source.owner_user_id
        and source.credential_source_kind
        and source.credential_source_id
    ):
        # The signed SMS config is itself accepted by the voice stream. Legacy
        # rows without an explicit owner/source must never search Entity-wide.
        return source

    async with async_session() as db:
        candidates = list((await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == source.entity_id,
                ChannelConfig.channel_type == "twilio_voice",
                ChannelConfig.status == "active",
                ChannelConfig.owner_user_id == source.owner_user_id,
                ChannelConfig.credential_source_kind == source.credential_source_kind,
                ChannelConfig.credential_source_id == source.credential_source_id,
            )
        )).scalars().all())

    normalized_to = str(to_number or "").strip()
    if normalized_to:
        number_matches = [
            candidate
            for candidate in candidates
            if not str((candidate.config or {}).get("phone_number") or "").strip()
            or str((candidate.config or {}).get("phone_number") or "").strip()
            == normalized_to
        ]
        if len(number_matches) == 1:
            return number_matches[0]
        if len(number_matches) > 1:
            raise HTTPException(409, "Ambiguous Twilio voice channel config")
        # A configured mismatched number must not silently bind this call to
        # another endpoint. Source-linked integration rows may omit the phone
        # from non-secret config because it is leased with the credentials.
        return source

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise HTTPException(409, "Ambiguous Twilio voice channel config")
    return source


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
    adapter, cc = await _get_adapter_and_config(
        config_id,
        allowed_channel_types=("sms", "twilio_sms"),
    )

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

    # Twilio retries deliveries when the response is delayed or unavailable.
    # A previously persisted receipt is safe to acknowledge once it has been
    # queued/processed, but a receipt left in ``received`` means an earlier
    # broker attempt failed and must be retried.
    message_sid = str(parsed.get("msg_id") or "").strip()
    inbound_log: MessageLog | None = None
    if message_sid:
        async with async_session() as db:
            existing = (await db.execute(
                select(MessageLog).where(
                    MessageLog.channel_config_id == cc.id,
                    MessageLog.direction == "inbound",
                    MessageLog.external_id == message_sid,
                ).limit(1)
            )).scalar_one_or_none()
        if existing is not None:
            inbound_log = existing

    # Log the inbound message
    if inbound_log is None:
        try:
            async with async_session() as db:
                inbound_log = await handle_inbound_message(
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
        except IntegrityError as exc:
            # Another delivery can win the provider-specific unique receipt
            # race between the lookup above and this insert. Re-read that
            # committed receipt and let the atomic claim decide dispatch.
            async with async_session() as db:
                inbound_log = (await db.execute(
                    select(MessageLog).where(
                        MessageLog.channel_config_id == cc.id,
                        MessageLog.direction == "inbound",
                        MessageLog.external_id == message_sid,
                    ).limit(1)
                )).scalar_one_or_none()
            if inbound_log is None:
                logger.exception("Failed to log inbound Twilio SMS message")
                raise HTTPException(503, "Unable to persist inbound Twilio message") from exc
        except Exception:
            logger.exception("Failed to log inbound Twilio SMS message")
            raise HTTPException(503, "Unable to persist inbound Twilio message")

    claim: InboundDispatchClaim | None = None
    if message_sid:
        claim = await _claim_dispatch(cc.id, message_sid)
        inbound_log = claim.receipt
        if claim.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING:
            raise HTTPException(503, "Inbound message dispatch is pending")
        if not claim.acquired or inbound_log is None or not claim.claim_id:
            return PlainTextResponse(
                '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )

    # Enqueue agent dispatch — Twilio's 15-second webhook ack budget is
    # plenty for the broker round-trip; the LLM reply goes out-of-band
    # via TwilioAdapter.send_sms.
    try:
        dispatch_inbound_task.delay(
            entity_id=cc.entity_id,
            channel_config_id=cc.id,
            channel_type="twilio_sms",
            sender_id=str(parsed["sender_id"]),
            sender_name=None,
            chat_id=str(parsed["sender_id"]),
            content=parsed.get("content", "") or "",
            inbound_message_log_id=inbound_log.id if inbound_log else None,
            dispatch_claim_id=claim.claim_id if claim else None,
        )
        if claim and inbound_log and claim.claim_id:
            published = await mark_inbound_dispatch_published(
                message_log_id=inbound_log.id,
                claim_id=claim.claim_id,
                session_factory=async_session,
            )
            if not published:
                raise RuntimeError("Inbound dispatch publish could not be confirmed")
    except Exception as exc:
        logger.exception("Failed to enqueue inbound Twilio SMS message")
        if claim and claim.acquired and inbound_log is not None and claim.claim_id:
            await _release_claim(inbound_log.id, claim.claim_id)
        raise HTTPException(503, "Inbound message queue unavailable") from exc

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

    # A single Twilio number can serve SMS and voice, but the fallback must
    # stay inside the signed config's owner and credential-source boundary.
    voice_cc = await _resolve_voice_channel_config(
        cc,
        to_number=form_data.get("To", ""),
    )

    call_sid = form_data.get("CallSid", "").strip() or None
    session = None
    stream_token = None
    created = False
    try:
        async with async_session() as db:
            if call_sid:
                session = await get_call_session_by_sid(
                    db,
                    channel_config_id=voice_cc.id,
                    call_sid=call_sid,
                )
            if session is not None:
                if session.token_used_at is None:
                    stream_token = await rotate_pending_call_session_token(db, session)
            else:
                binding_scope = await resolve_unique_channel_binding_scope(
                    db,
                    voice_cc,
                )
                if binding_scope is None:
                    raise ValueError("Twilio Voice channel has no active Agent binding")
                try:
                    session, stream_token = await create_call_session(
                        db,
                        entity_id=voice_cc.entity_id,
                        channel_config_id=voice_cc.id,
                        owner_user_id=voice_cc.owner_user_id,
                        workspace_id=binding_scope.workspace_id,
                        direction="inbound",
                        call_sid=call_sid,
                        from_number=form_data.get("From", ""),
                        to_number=form_data.get("To", ""),
                        agent_id=binding_scope.agent_id,
                        metadata={
                            "call_status": form_data.get("CallStatus", ""),
                            "direction": form_data.get("Direction", ""),
                            "channel_binding_id": binding_scope.binding.id,
                            "agent_subscription_id": (
                                binding_scope.agent_subscription_id
                            ),
                        },
                    )
                    created = True
                except IntegrityError:
                    # Two webhook deliveries can pass the initial lookup at
                    # the same time. The partial unique CallSid index makes
                    # one insert win; the loser re-reads that committed row
                    # and returns a fresh token without duplicating history.
                    await db.rollback()
                    if not call_sid:
                        raise
                    session = await get_call_session_by_sid(
                        db,
                        channel_config_id=voice_cc.id,
                        call_sid=call_sid,
                    )
                    if session is None:
                        raise
                    if session.token_used_at is None:
                        stream_token = await rotate_pending_call_session_token(db, session)

            # Log only the first delivery for a CallSid. Twilio retries must
            # return the same functional response without duplicate history.
            if created:
                await handle_inbound_message(
                    db,
                    entity_id=voice_cc.entity_id,
                    channel_config_id=voice_cc.id,
                    payload={
                        "from": form_data.get("From", ""),
                        "to": form_data.get("To", ""),
                        "content": f"Incoming call from {form_data.get('From', 'unknown')}",
                        "MessageSid": call_sid or "",
                        "channel_type": "twilio_voice",
                        "metadata": {
                            "call_status": form_data.get("CallStatus", ""),
                            "direction": form_data.get("Direction", ""),
                            "call_session_id": session.id,
                        },
                    },
                )
            await db.commit()
    except Exception:
        logger.exception("Failed to persist inbound Twilio voice session")
        raise HTTPException(503, "Unable to persist inbound Twilio voice session")

    if not stream_token:
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Say voice="alice">This call is no longer available. Goodbye.</Say>'
                '</Response>'
            ),
            media_type="application/xml",
        )

    # Build the wss:// URL for the Media Streams socket
    from packages.core.config import get_settings
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    wss_base = base.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{wss_base}/api/v1/channels/twilio_voice/stream/{stream_token}"
    twiml = build_stream_twiml(
        stream_url=stream_url,
        from_number=form_data.get("From", ""),
        to_number=form_data.get("To", ""),
        direction="inbound",
    )
    return Response(content=twiml, media_type="application/xml")


@router.api_route("/voice/outbound/{session_token}", methods=["GET", "POST"])
async def twilio_outbound_voice_webhook(
    request: Request,
    session_token: str,
):
    """Return Manor-owned TwiML for a previously created outbound call."""
    async with async_session() as db:
        session_snapshot = await get_call_session_by_token(db, session_token)
        if session_snapshot is None or session_snapshot.direction != "outbound":
            raise HTTPException(404, "Voice call session not found")
        cc = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == session_snapshot.channel_config_id,
                ChannelConfig.status == "active",
                ChannelConfig.channel_type == "twilio_voice",
            )
        )).scalar_one_or_none()
        if cc is None:
            raise HTTPException(404, "Voice channel config not found")
        adapter, _ = await _get_adapter_and_config(session_snapshot.channel_config_id)
        form_data = {}
        if request.method == "POST":
            form = await request.form()
            form_data = {key: str(value) for key, value in form.items()}
        else:
            form_data = {key: str(value) for key, value in request.query_params.items()}
        await _validate_twilio_signature(adapter, request, form_data)
        session = await mark_outbound_call_connecting(db, session_token)
        if session is None:
            raise HTTPException(404, "Voice call session not found")
        from_number = session.from_number or ""
        to_number = session.to_number or ""
        await db.commit()

    from packages.core.config import get_settings
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    wss_base = base.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{wss_base}/api/v1/channels/twilio_voice/stream/{session_token}"
    return Response(
        content=build_stream_twiml(
            stream_url=stream_url,
            from_number=from_number,
            to_number=to_number,
            direction="outbound",
        ),
        media_type="application/xml",
    )


@router.post("/status", response_class=PlainTextResponse)
async def twilio_status_callback(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this Twilio account"),
    session_id: str | None = Query(
        None,
        description="Durable outbound Voice session ID",
    ),
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
                call_session = None
                if is_call:
                    if session_id:
                        call_session = await get_call_session_for_update(db, session_id)
                        if (
                            call_session is None
                            or call_session.channel_config_id != cc.id
                            or call_session.direction != "outbound"
                        ):
                            raise ValueError(
                                "Twilio callback does not match the outbound Voice session"
                            )
                        if call_session.call_sid and call_session.call_sid != message_sid:
                            raise ValueError(
                                "Twilio callback CallSid conflicts with the Voice session"
                            )
                        call_session.call_sid = message_sid
                    else:
                        call_session = await get_call_session_by_sid(
                            db,
                            channel_config_id=cc.id,
                            call_sid=message_sid,
                        )
                    session_status = _TWILIO_CALL_SESSION_STATUS_MAP.get(
                        status.strip().lower(),
                    )
                    if (
                        call_session is not None
                        and session_status == "in_progress"
                        and call_session.status not in {
                            "completed", "failed", "busy", "no_answer", "canceled", "expired",
                        }
                    ):
                        call_session.status = "in_progress"
                        call_session.connected_at = call_session.connected_at or datetime.now(timezone.utc)
                    elif call_session is not None and session_status in {
                        "completed", "busy", "no_answer", "failed", "canceled",
                    }:
                        await finish_call_session(
                            db,
                            call_session,
                            status=session_status,
                            duration_seconds=(
                                int(form_data["CallDuration"])
                                if str(form_data.get("CallDuration", "")).isdigit()
                                else None
                            ),
                            error_message=form_data.get("ErrorMessage"),
                        )
                row = (await db.execute(
                    select(MessageLog).where(
                        MessageLog.channel_config_id == cc.id,
                        MessageLog.direction == "outbound",
                        MessageLog.external_id == message_sid,
                    ).order_by(MessageLog.created_at.desc()).limit(1)
                )).scalar_one_or_none()
                if row is None and call_session is not None:
                    log_id = str(
                        (call_session.metadata_json or {}).get("message_log_id") or ""
                    ).strip()
                    if log_id:
                        row = (await db.execute(
                            select(MessageLog).where(
                                MessageLog.id == log_id,
                                MessageLog.channel_config_id == cc.id,
                                MessageLog.direction == "outbound",
                            ).with_for_update()
                        )).scalar_one_or_none()
                        if row is not None:
                            if row.external_id and row.external_id != message_sid:
                                raise ValueError(
                                    "Twilio callback CallSid conflicts with the message log"
                                )
                            row.external_id = message_sid

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
                else:
                    logger.info(
                        "Twilio status callback sid=%s had no outbound MessageLog match (config=%s)",
                        message_sid,
                        cc.id,
                    )
                await db.commit()
        except Exception as exc:
            logger.exception(
                "Failed to persist Twilio status callback sid=%s config=%s",
                message_sid,
                cc.id,
            )
            raise HTTPException(503, "Unable to persist Twilio status callback") from exc

    return PlainTextResponse("ok")
