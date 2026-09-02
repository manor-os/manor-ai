"""Microsoft Graph change-notification endpoint for Teams chats."""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_credentials import channel_credential_source_is_available
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.channels.ms_teams_adapter import TeamsChannelAdapter
from packages.core.services.channel_inbound_receipts import (
    InboundDispatchClaim,
    InboundDispatchClaimOutcome,
    claim_inbound_dispatch,
    mark_inbound_dispatch_published,
    release_inbound_dispatch_claim,
)
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/channels/ms_teams", tags=["channels"])
_adapter = TeamsChannelAdapter()


async def _existing_receipt(config_id: str, message_id: str) -> MessageLog | None:
    async with async_session() as db:
        return (await db.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == config_id,
                MessageLog.direction == "inbound",
                MessageLog.channel_type == "ms_teams",
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
        channel_type="ms_teams",
        external_id=message_id,
        session_factory=async_session,
    )


async def _release_claim(log_id: str, claim_id: str) -> None:
    await release_inbound_dispatch_claim(
        message_log_id=log_id,
        claim_id=claim_id,
        session_factory=async_session,
    )


async def _resolve_channel_config(
    db: AsyncSession,
    notification: dict,
) -> ChannelConfig | None:
    subscription_id = str(notification.get("subscriptionId") or "").strip()
    client_state = str(notification.get("clientState") or "").strip()
    if not subscription_id and not client_state:
        return None
    rows = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.channel_type == "ms_teams",
            ChannelConfig.status == "active",
            ChannelConfig.owner_user_id.is_not(None),
        )
    )).scalars().all()
    matches: list[ChannelConfig] = []
    for row in rows:
        config = row.config or {}
        configured_subscription = str(config.get("teams_subscription_id") or "")
        configured_state = str(config.get("teams_client_state") or "")
        if not configured_subscription or not configured_state:
            continue
        if configured_subscription != subscription_id:
            continue
        if not hmac.compare_digest(configured_state, client_state):
            continue
        if not await channel_credential_source_is_available(db, row):
            continue
        matches.append(row)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning("Ambiguous Teams notification subscription=%s", subscription_id)
    return None


async def _receive(request: Request):
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        # Graph requires the token as plain text, not a JSON envelope.
        return PlainTextResponse(validation_token)

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid Microsoft Graph notification payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Microsoft Graph notification payload")
    notifications = payload.get("value")
    if not isinstance(notifications, list):
        return JSONResponse({"ok": True, "noop": True})

    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        async with async_session() as db:
            config = await _resolve_channel_config(db, notification)
            if config is None:
                continue
            try:
                hydrated = await _adapter.hydrate_notification(config, notification)
                parsed = _adapter.parse_notification(config, hydrated)
            except Exception:
                logger.exception("Failed to parse Teams notification")
                continue
            if parsed is None:
                continue
            inbound_log = await _existing_receipt(
                config.id,
                parsed.external_message_id,
            )
            if inbound_log is None:
                try:
                    inbound_log = await handle_inbound_message(
                        db,
                        entity_id=config.entity_id,
                        channel_config_id=config.id,
                        payload={
                            "from": parsed.source_id,
                            "to": parsed.reply_to,
                            "content": parsed.content,
                            "external_id": parsed.external_message_id,
                            "channel_type": "ms_teams",
                            "metadata": {"message_type": parsed.message_type},
                        },
                    )
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    inbound_log = await _existing_receipt(
                        config.id,
                        parsed.external_message_id,
                    )
                    if inbound_log is None:
                        raise

            claim = await _claim_dispatch(
                config.id,
                parsed.external_message_id,
            )
            inbound_log = claim.receipt
            if claim.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING:
                raise HTTPException(503, "Microsoft Teams event dispatch is pending")
            if not claim.acquired or inbound_log is None or not claim.claim_id:
                continue
            try:
                dispatch_inbound_task.delay(
                    entity_id=config.entity_id,
                    channel_config_id=config.id,
                    channel_type="ms_teams",
                    sender_id=parsed.source_id,
                    sender_name=parsed.sender_name,
                    chat_id=parsed.reply_to,
                    content=parsed.content,
                    inbound_message_log_id=inbound_log.id,
                    dispatch_claim_id=claim.claim_id,
                )
                published = await mark_inbound_dispatch_published(
                    message_log_id=inbound_log.id,
                    claim_id=claim.claim_id,
                    session_factory=async_session,
                )
                if not published:
                    raise RuntimeError("Inbound dispatch publish could not be confirmed")
            except Exception:
                await _release_claim(inbound_log.id, claim.claim_id)
                logger.exception("Could not enqueue Teams notification %s", parsed.external_message_id)
                raise HTTPException(503, "Microsoft Teams event queue unavailable")

    return JSONResponse({"ok": True})


@router.post("/notifications")
async def teams_notifications(request: Request):
    return await _receive(request)


@router.get("/notifications")
async def teams_notification_validation(
    validationToken: str | None = Query(None),
):
    if validationToken is None:
        raise HTTPException(400, "validationToken is required")
    return PlainTextResponse(validationToken)
