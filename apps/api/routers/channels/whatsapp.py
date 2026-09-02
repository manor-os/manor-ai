"""WhatsApp Cloud API webhook endpoints.

GET  /api/v1/channels/whatsapp/webhook  — webhook verification (Meta challenge)
POST /api/v1/channels/whatsapp/webhook  — incoming messages and status updates

Meta requires a fixed callback URL during app configuration. New callbacks
route by ``metadata.phone_number_id``; the optional ``config_id`` query
parameter remains for backwards compatibility with older registrations.
"""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_credentials import lease_channel_credentials
from packages.core.services.channels.whatsapp_adapter import (
    DEFAULT_API_VERSION,
    WhatsAppAdapter,
)
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.whatsapp_business_config import (
    load_whatsapp_business_config,
)
from packages.core.services.channel_inbound_receipts import (
    InboundDispatchClaim,
    InboundDispatchClaimOutcome,
    claim_inbound_dispatch,
    mark_inbound_dispatch_published,
    release_inbound_dispatch_claim,
)
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["channels"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _existing_receipt(config_id: str, message_id: str) -> MessageLog | None:
    async with async_session() as db:
        return (await db.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config_id,
                MessageLog.direction == "inbound",
                MessageLog.channel_type == "whatsapp",
                MessageLog.external_id == message_id,
            ).limit(1)
        )).scalar_one_or_none()


async def _claim_received(config_id: str, message_id: str) -> tuple[MessageLog | None, bool]:
    """Compatibility wrapper around the shared durable dispatch claim."""
    claim = await _claim_dispatch(config_id, message_id)
    return claim.receipt, claim.acquired


async def _claim_dispatch(config_id: str, message_id: str) -> InboundDispatchClaim:
    return await claim_inbound_dispatch(
        config_id=config_id,
        channel_type="whatsapp",
        external_id=message_id,
        session_factory=async_session,
    )


async def _release_claim(log_id: str, claim_id: str) -> None:
    await release_inbound_dispatch_claim(
        message_log_id=log_id,
        claim_id=claim_id,
        session_factory=async_session,
    )

def _webhook_events_match_phone_number(
    body: dict,
    phone_number_id: str,
) -> bool:
    """Require every message/status change to target this channel number."""
    entries = body.get("entry", [])
    if not isinstance(entries, list):
        return False

    for entry in entries:
        if not isinstance(entry, dict):
            return False
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            return False
        for change in changes:
            if not isinstance(change, dict):
                return False
            value = change.get("value", {})
            if not isinstance(value, dict):
                return False
            if not (value.get("messages") or value.get("statuses")):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                return False
            if str(metadata.get("phone_number_id") or "") != phone_number_id:
                return False
    return True

async def _get_adapter_and_config(
    config_id: str | None = None,
    *,
    phone_number_id: str | None = None,
    verify_token: str | None = None,
    require_app_secret: bool = True,
) -> tuple[WhatsAppAdapter, ChannelConfig]:
    """Resolve one WhatsApp account for legacy or fixed Meta callbacks."""
    deployment = load_whatsapp_business_config()
    async with async_session() as db:
        query = select(ChannelConfig).where(
            ChannelConfig.channel_type == "whatsapp",
            ChannelConfig.status == "active",
        )
        if config_id:
            query = query.where(ChannelConfig.id == config_id)
        if phone_number_id:
            query = query.where(
                ChannelConfig.whatsapp_phone_number_id == phone_number_id,
            )
        elif not config_id:
            raise HTTPException(
                400,
                "WhatsApp fixed webhook payload is missing metadata.phone_number_id",
            )
        rows = (await db.execute(query)).scalars().all()
        matches: list[tuple[WhatsAppAdapter, ChannelConfig]] = []
        for cc in rows:
            try:
                creds = await lease_channel_credentials(
                    db,
                    cc,
                    reason="channel.whatsapp.webhook",
                    resolve_nango=True,
                )
            except ValueError:
                continue
            candidate_phone_id = str(creds.get("phone_number_id") or "").strip()
            candidate_verify_token = deployment.verify_token
            access_token = creds.get("access_token", creds.get("api_token", ""))
            if not candidate_phone_id or not access_token:
                continue
            if phone_number_id and candidate_phone_id != phone_number_id:
                continue
            if verify_token is not None and candidate_verify_token != verify_token:
                continue
            app_secret = deployment.app_secret
            if require_app_secret and not app_secret:
                raise HTTPException(
                    503,
                    "WhatsApp channel is not ready: app_secret is required for webhook verification",
                )
            matches.append((WhatsAppAdapter(
                phone_number_id=candidate_phone_id,
                access_token=access_token,
                verify_token=candidate_verify_token,
                app_secret=app_secret,
                api_version=(cc.config or {}).get("api_version", DEFAULT_API_VERSION),
            ), cc))

    if len(matches) != 1:
        if not matches:
            raise HTTPException(404, "WhatsApp channel config not found")
        raise HTTPException(409, "WhatsApp webhook matched multiple channel configs")
    return matches[0]


def _payload_phone_number_id(body: dict) -> str | None:
    """Extract the one Meta phone-number id addressed by this delivery."""
    ids: set[str] = set()
    for entry in body.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            value = change.get("value", {}) if isinstance(change, dict) else {}
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            phone_id = str(metadata.get("phone_number_id") or "").strip()
            if phone_id and (value.get("messages") or value.get("statuses")):
                ids.add(phone_id)
    return ids.pop() if len(ids) == 1 else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/webhook", response_class=PlainTextResponse)
async def whatsapp_verify(
    request: Request,
    config_id: str | None = Query(None, description="Legacy ChannelConfig ID"),
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

    # A fixed Meta app callback has no phone number in its verification GET.
    # Use one operator-owned token for that handshake so it never has to
    # enumerate and lease every user connection. ``config_id`` remains a
    # compatibility path for older per-config registrations.
    if not config_id:
        try:
            expected = load_whatsapp_business_config().verify_token
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if (
            mode == "subscribe"
            and expected
            and hmac.compare_digest(token, expected)
        ):
            return PlainTextResponse(challenge)
        raise HTTPException(403, "Webhook verification failed")

    adapter, _cc = await _get_adapter_and_config(
        config_id,
        verify_token=token if not config_id else None,
        require_app_secret=False,
    )

    result = await adapter.verify_webhook(mode, token, challenge)
    if result is not None:
        return PlainTextResponse(result)

    raise HTTPException(403, "Webhook verification failed")


@router.post("/webhook")
async def whatsapp_receive(
    request: Request,
    config_id: str | None = Query(None, description="Legacy ChannelConfig ID"),
):
    """Receive inbound messages and status updates from WhatsApp.

    Meta POSTs JSON payloads with message and status events.
    Returns 200 OK immediately to acknowledge receipt (Meta requires
    acknowledgement within 20 seconds).
    """
    raw_body = await request.body()
    try:
        preliminary_body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid WhatsApp webhook payload") from exc
    if not isinstance(preliminary_body, dict):
        raise HTTPException(400, "Invalid WhatsApp webhook payload")
    if config_id:
        # Preserve the legacy resolver call shape for explicitly addressed
        # configs; fixed Meta callbacks use phone_number_id-based routing.
        adapter, cc = await _get_adapter_and_config(config_id)
    else:
        adapter, cc = await _get_adapter_and_config(
            phone_number_id=_payload_phone_number_id(preliminary_body),
        )
    if not adapter.verify_inbound_signature(headers=request.headers, body=raw_body):
        raise HTTPException(403, "WhatsApp webhook signature verification failed")

    # Parse JSON only after the provider signature has been checked against
    # the original bytes; re-serialization changes the HMAC input.
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid WhatsApp webhook payload") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid WhatsApp webhook payload")
    if not _webhook_events_match_phone_number(body, adapter.phone_number_id):
        raise HTTPException(403, "WhatsApp webhook phone number does not match this channel")

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
            message_id = str(event.get("msg_id") or "")
            status = str(event.get("status") or "")
            if message_id and status:
                values: dict[str, str] = {"status": status}
                if status == "failed":
                    values["error_message"] = json.dumps(
                        event.get("raw", {}).get("errors", []),
                        ensure_ascii=False,
                    )
                async with async_session() as db:
                    await db.execute(
                        update(MessageLog)
                        .where(
                            MessageLog.channel_config_id == cc.id,
                            MessageLog.channel_type == "whatsapp",
                            MessageLog.direction == "outbound",
                            MessageLog.external_id == message_id,
                        )
                        .values(**values)
                    )
                    await db.commit()
            continue

        # Log the inbound message, then claim its durable receipt before
        # publishing the agent task. This closes the broker/DB race for
        # provider retries and concurrent duplicate deliveries.
        async with async_session() as db:
            message_id = str(event.get("msg_id") or "").strip()
            inbound_log = await _existing_receipt(cc.id, message_id) if message_id else None
            if inbound_log is None:
                try:
                    inbound_log = await handle_inbound_message(
                        db,
                        entity_id=cc.entity_id,
                        channel_config_id=cc.id,
                        payload={
                            "from": event["sender_id"],
                            "to": adapter.phone_number_id,
                            "content": event["content"],
                            "message_id": message_id,
                            "wamid": message_id,
                            "channel_type": "whatsapp",
                            "metadata": {
                                "message_type": event["message_type"],
                                "profile_name": event.get("profile_name", ""),
                            },
                        },
                    )
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    inbound_log = await _existing_receipt(cc.id, message_id) if message_id else None
                    if inbound_log is None:
                        raise

            claim: InboundDispatchClaim | None = None
            if message_id:
                claim = await _claim_dispatch(cc.id, message_id)
                inbound_log = claim.receipt
                if claim.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING:
                    raise HTTPException(503, "WhatsApp event dispatch is pending")
                if not claim.acquired or inbound_log is None or not claim.claim_id:
                    continue

            try:
                dispatch_inbound_task.delay(
                    entity_id=cc.entity_id,
                    channel_config_id=cc.id,
                    channel_type="whatsapp",
                    sender_id=str(event["sender_id"]),
                    sender_name=event.get("profile_name"),
                    chat_id=str(event["sender_id"]),
                    content=event.get("content", "") or "",
                    inbound_message_log_id=inbound_log.id,
                    dispatch_claim_id=claim.claim_id if claim else None,
                )
                if claim and claim.claim_id:
                    published = await mark_inbound_dispatch_published(
                        message_log_id=inbound_log.id,
                        claim_id=claim.claim_id,
                        session_factory=async_session,
                    )
                    if not published:
                        raise RuntimeError("Inbound dispatch publish could not be confirmed")
            except Exception:
                if claim and claim.acquired and inbound_log is not None and claim.claim_id:
                    await _release_claim(inbound_log.id, claim.claim_id)
                else:
                    await db.rollback()
                logger.exception("Could not enqueue WhatsApp event %s", event.get("msg_id"))
                raise HTTPException(503, "WhatsApp event queue unavailable")
            await db.commit()

        # Mark the message as read only after the durable receipt and dispatch
        # intent have committed. Failure is provider-visible but non-blocking.
        try:
            if event.get("msg_id"):
                await adapter.mark_as_read(event["msg_id"])
        except Exception:
            logger.debug("Failed to mark WhatsApp message as read: %s", event.get("msg_id"))

    return {"status": "ok"}
