"""Microsoft Graph Outlook Mail webhook receiver."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter
from packages.core.services.channel_inbound_receipts import (
    InboundDispatchClaim,
    InboundDispatchClaimOutcome,
    claim_inbound_dispatch,
    mark_inbound_dispatch_published,
    release_inbound_dispatch_claim,
)
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/channels/outlook", tags=["channels"])


def _ack() -> PlainTextResponse:
    # Graph accepts any 2xx response for a notification after the payload has
    # been persisted; an empty body avoids leaking message content.
    return PlainTextResponse("", status_code=202)


async def _load_config(config_id: str) -> ChannelConfig:
    async with async_session() as db:
        config = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == config_id,
                ChannelConfig.channel_type == "outlook",
                ChannelConfig.status == "active",
            )
        )).scalar_one_or_none()
    if config is None:
        raise HTTPException(404, "Outlook channel config not found")
    return config


async def _existing_receipt(config_id: str, message_id: str) -> MessageLog | None:
    async with async_session() as db:
        return (await db.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config_id,
                MessageLog.direction == "inbound",
                MessageLog.external_id == message_id,
            ).limit(1)
        )).scalar_one_or_none()


async def _mark_queued(log_id: str) -> None:
    async with async_session() as db:
        row = await db.get(MessageLog, log_id)
        if row is not None and row.status == "received":
            row.status = "queued"
            await db.commit()


async def _claim_received(config_id: str, message_id: str) -> tuple[MessageLog | None, bool]:
    """Compatibility wrapper around the shared durable dispatch claim."""
    claim = await _claim_dispatch(config_id, message_id)
    return claim.receipt, claim.acquired


async def _claim_dispatch(config_id: str, message_id: str) -> InboundDispatchClaim:
    return await claim_inbound_dispatch(
        config_id=config_id,
        channel_type="outlook",
        external_id=message_id,
        session_factory=async_session,
    )


async def _release_claim(log_id: str, claim_id: str) -> None:
    await release_inbound_dispatch_claim(
        message_log_id=log_id,
        claim_id=claim_id,
        session_factory=async_session,
    )


@router.post("/notifications")
async def outlook_notifications(
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this Outlook account"),
    validationToken: str | None = Query(None),
):
    """Receive Graph mail notifications and enqueue the agent turn.

    Microsoft Graph sends ``validationToken`` during subscription creation;
    that request must be echoed as plain text before any authentication or
    body parsing. Normal notifications are authenticated by the persisted
    subscription id/client state in the ChannelConfig.
    """
    if validationToken is not None:
        return PlainTextResponse(validationToken)

    config = await _load_config(config_id)
    adapter = OutlookChannelAdapter()
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Outlook webhook body is not valid JSON") from exc

    values: list[dict[str, Any]]
    if isinstance(payload, dict) and isinstance(payload.get("value"), list):
        values = [item for item in payload["value"] if isinstance(item, dict)]
    elif isinstance(payload, dict):
        values = [payload]
    else:
        return _ack()

    for notification in values:
        # Reject forged routing metadata before hydrate_notification can spend
        # the owner's Graph token on an attacker-controlled resource path.
        if not adapter.notification_matches_subscription(config, notification):
            continue
        try:
            hydrated = await adapter.hydrate_notification(config, notification)
            parsed = adapter.parse_notification(config, hydrated)
        except Exception:
            logger.exception("Failed to process Outlook notification config=%s", config.id)
            continue
        if parsed is None:
            continue

        message_id = str(parsed.external_message_id or "").strip()
        inbound_log: MessageLog | None = None
        if message_id:
            inbound_log = await _existing_receipt(config.id, message_id)
        resource_data = parsed.raw.get("resourceData") if isinstance(parsed.raw, dict) else {}
        resource_data = resource_data if isinstance(resource_data, dict) else {}
        if inbound_log is None:
            try:
                async with async_session() as db:
                    inbound_log = await handle_inbound_message(
                        db,
                        entity_id=config.entity_id,
                        channel_config_id=config.id,
                        payload={
                            "from": parsed.source_id,
                            "to": str(config.config.get("outlook_user_email") or "")
                            if isinstance(config.config, dict)
                            else "",
                            "content": parsed.content,
                            "message_id": message_id,
                            "conversation_id": resource_data.get("conversationId"),
                            "channel_type": "outlook",
                            "metadata": {
                                "message_type": parsed.message_type,
                                "sender_name": parsed.sender_name,
                                "subject": resource_data.get("subject"),
                                "via": "microsoft_graph",
                            },
                        },
                    )
                    await db.commit()
            except IntegrityError as exc:
                # Graph may replay the same message concurrently. If another
                # request won the provider-specific receipt insert, reuse its
                # committed row and let the atomic claim gate dispatch.
                inbound_log = await _existing_receipt(config.id, message_id)
                if inbound_log is None:
                    logger.exception("Failed to persist Outlook inbound message")
                    raise HTTPException(503, "Unable to persist inbound Outlook message") from exc
            except Exception as exc:
                logger.exception("Failed to persist Outlook inbound message")
                raise HTTPException(503, "Unable to persist inbound Outlook message") from exc

        claim: InboundDispatchClaim | None = None
        if message_id:
            claim = await _claim_dispatch(config.id, message_id)
            inbound_log = claim.receipt
            if claim.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING:
                raise HTTPException(503, "Inbound message dispatch is pending")
            if not claim.acquired or inbound_log is None or not claim.claim_id:
                continue

        try:
            dispatch_inbound_task.delay(
                entity_id=config.entity_id,
                channel_config_id=config.id,
                channel_type="outlook",
                sender_id=parsed.source_id,
                sender_name=parsed.sender_name,
                chat_id=parsed.reply_to,
                content=parsed.content,
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
            logger.exception("Failed to enqueue Outlook inbound message")
            if claim and claim.acquired and inbound_log is not None and claim.claim_id:
                await _release_claim(inbound_log.id, claim.claim_id)
            raise HTTPException(503, "Inbound message queue unavailable") from exc
        if inbound_log is not None:
            # Kept for callers/tests that patch this legacy marker; the
            # atomic claim above already moved the row to ``queued``.
            await _mark_queued(inbound_log.id)

    return _ack()


__all__ = ["outlook_notifications", "router"]
