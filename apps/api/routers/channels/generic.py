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
rest (Slack, Discord, In-App, SMS, Voice, email inbound).

Two inline special cases are handled because they are first-packet
handshakes rather than real messages:

  - Slack ``url_verification`` → echo the challenge value.
  - Discord interaction PING (``type == 1``) → echo pong.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels import get_adapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


# ── Handshake sniffers ──────────────────────────────────────────────────────

def _try_slack_challenge(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return None
    if payload.get("type") == "url_verification":
        return str(payload.get("challenge") or "")
    return None


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
    adapter = get_adapter(channel_type)
    if adapter is None:
        raise HTTPException(404, f"No adapter registered for '{channel_type}'")

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    query = dict(request.query_params)

    # ── Handshakes that don't go through verify/parse ──
    if channel_type == "slack":
        challenge = _try_slack_challenge(body)
        if challenge is not None:
            return PlainTextResponse(challenge)
    elif channel_type == "discord":
        ping = _try_discord_ping(body)
        if ping is not None:
            return JSONResponse(ping)

    # ── Load the channel config ──
    async with async_session() as db:
        cc = (await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )).scalar_one_or_none()
    if not cc:
        raise HTTPException(404, "Channel config not found")
    if cc.channel_type != channel_type:
        raise HTTPException(400, f"config_id belongs to {cc.channel_type}, not {channel_type}")

    # ── Adapter verify + parse ──
    if not await adapter.verify_inbound(cc, headers=headers, query=query, body=body):
        raise HTTPException(403, "Signature verification failed")

    parsed = await adapter.parse_inbound(cc, headers=headers, query=query, body=body)
    if not parsed:
        # Provider may send delivery receipts or unsupported events here
        return JSONResponse({"ok": True, "noop": True})

    # ── Log + hand off ──
    try:
        async with async_session() as db:
            await handle_inbound_message(
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
            await db.commit()
    except Exception:
        logger.exception("Failed to log inbound %s message", channel_type)

    dispatch_inbound_task.delay(
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        channel_type=channel_type,
        sender_id=parsed.source_id,
        sender_name=parsed.sender_name,
        chat_id=parsed.reply_to,
        content=parsed.content,
    )

    return JSONResponse({"ok": True})
