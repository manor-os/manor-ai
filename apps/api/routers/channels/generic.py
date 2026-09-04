"""Generic channel inbound router.

One endpoint for every registered ``ChannelAdapter``:

    POST /api/v1/channels/{channel_type}/callback?config_id=<cc_id>

The router finds the adapter for ``channel_type`` in the registry, asks
it to verify + parse the request, logs the inbound message, and kicks
off ``dispatch_inbound`` in a FastAPI ``BackgroundTasks`` so the 200 OK
comes back inside any provider's ack window.

Channel-specific quirks that don't fit the plain POST shape (Telegram's
bot_token_hash path, WeChat's XML handshake, WhatsApp's hub.verify_token
GET) keep their dedicated routers. This generic endpoint handles the
rest (Discord, In-App, SMS, Voice, email inbound). Slack uses its fixed,
installation-routed Events endpoint.

Discord interaction PING (``type == 1``) is handled inline because it is a
first-packet handshake rather than a real message.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channels import get_adapter
from packages.core.services.channel_credentials import (
    channel_credential_source_is_available,
)
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# ── Handshake sniffers ──────────────────────────────────────────────────────

def _try_discord_ping(body: bytes) -> Dict[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return None
    if payload.get("type") == 1:
        return {"type": 1}
    return None


# ── Main endpoint ───────────────────────────────────────────────────────────

@router.post("/{channel_type}/callback")
async def channel_callback(
    channel_type: str,
    request: Request,
    config_id: str = Query(..., description="ChannelConfig ID for this channel instance"),
):
    if channel_type in {"slack", "discord"}:
        fixed_endpoint = (
            "/api/v1/channels/slack/events"
            if channel_type == "slack"
            else "/api/v1/channels/discord/interactions"
        )
        raise HTTPException(
            410,
            f"{channel_type.title()} events must use {fixed_endpoint}",
        )

    adapter = get_adapter(channel_type)
    if adapter is None:
        raise HTTPException(404, f"No adapter registered for '{channel_type}'")

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    query = dict(request.query_params)

    # ── Load the channel config ──
    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )).scalar_one_or_none()
        source_available = bool(
            cc and await channel_credential_source_is_available(db, cc)
        )
    if not cc or not source_available:
        raise HTTPException(404, "Channel config not found")
    if cc.channel_type != channel_type:
        raise HTTPException(400, f"config_id belongs to {cc.channel_type}, not {channel_type}")

    # ── Adapter verify + parse ──
    if not await adapter.verify_inbound(cc, headers=headers, query=query, body=body):
        raise HTTPException(403, "Signature verification failed")

    # Discord handshakes must use the same authenticated Config as ordinary
    # inbound events. Returning before verification would accept forged PINGs.
    if channel_type == "discord":
        ping = _try_discord_ping(body)
        if ping is not None:
            return JSONResponse(ping)

    parsed = await adapter.parse_inbound(cc, headers=headers, query=query, body=body)
    if not parsed:
        # Provider may send delivery receipts or unsupported events here
        return JSONResponse({"ok": True, "noop": True})

    # ── Log + hand off ──
    # Only WeChat Personal has a durable receipt contract on this generic
    # route. Its worker uses the receipt ID to make provider retries
    # idempotent, so the broker publish must happen before the commit. Other
    # generic channels keep the established commit-before-publish ordering.
    durable_receipt_channel = channel_type == "wechat_personal"
    dispatch_kwargs = {
        "entity_id": cc.entity_id,
        "channel_config_id": cc.id,
        "channel_type": channel_type,
        "sender_id": parsed.source_id,
        "sender_name": parsed.sender_name,
        "chat_id": parsed.reply_to,
        "content": parsed.content,
    }

    def enqueue_or_raise() -> None:
        try:
            dispatch_inbound_task.delay(**dispatch_kwargs)
        except Exception as exc:
            logger.exception(
                "Could not enqueue inbound %s message", channel_type,
            )
            raise HTTPException(
                503, f"{channel_type.title()} event queue unavailable",
            ) from exc

    try:
        async with async_session() as db:
            inbound_log = await handle_inbound_message(
                db,
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                payload={
                    "from": parsed.source_id,
                    "to": parsed.reply_to,
                    "content": parsed.content,
                    "message_id": parsed.external_message_id,
                    "channel_type": channel_type,
                    "metadata": {
                        "message_type": parsed.message_type,
                        "sender_name": parsed.sender_name,
                    },
                },
            )
            dispatch_kwargs["inbound_message_log_id"] = inbound_log.id
            if durable_receipt_channel:
                try:
                    enqueue_or_raise()
                except HTTPException:
                    await db.rollback()
                    raise
                await db.commit()
            else:
                await db.commit()
                enqueue_or_raise()
    except IntegrityError:
        if not durable_receipt_channel or not parsed.external_message_id:
            raise
        # WeChat Personal's runner retries the same msg_id when its callback
        # acknowledgement is delayed. The partial unique index makes that
        # replay a harmless acknowledgement instead of a second agent turn.
        async with async_session() as db:
            duplicate = await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.channel_config_id == cc.id,
                    MessageLog.channel_type == channel_type,
                    MessageLog.direction == "inbound",
                    MessageLog.external_id == parsed.external_message_id,
                )
            )
        if duplicate is None:
            raise
        return JSONResponse({"ok": True, "noop": True, "reason": "duplicate_event"})

    return JSONResponse({"ok": True})
