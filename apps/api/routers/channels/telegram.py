"""Telegram Bot API webhook endpoint.

POST /api/v1/channels/telegram/webhook/{bot_token_hash}  — incoming updates

Telegram sends JSON updates to a webhook URL that includes a hash of the
bot token (not the token itself) to prevent unauthorised access.
The channel_config_id is passed as a query parameter so multiple bots
can share the same endpoint pattern.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.telegram_adapter import TelegramAdapter
from packages.core.services.channel_service import handle_inbound_message
from packages.core.tasks.channel_tasks import dispatch_inbound_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/telegram", tags=["channels"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
) -> tuple[TelegramAdapter, ChannelConfig]:
    """Load ChannelConfig and build a TelegramAdapter from its credentials."""
    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )
        cc = result.scalar_one_or_none()

    if not cc:
        raise HTTPException(404, "Channel config not found")

    creds = cc.credentials or {}
    bot_token = creds.get("bot_token", "")

    if not bot_token:
        raise HTTPException(500, "Telegram channel config is missing required credentials (bot_token)")

    adapter = TelegramAdapter(bot_token=bot_token)
    return adapter, cc


def _hash_bot_token(bot_token: str) -> str:
    """Generate a SHA-256 hash of the bot token for URL matching.

    The webhook URL uses a hash instead of the raw token so that
    the token is never exposed in logs or HTTP traffic.
    """
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/webhook/{bot_token_hash}")
async def telegram_webhook(
    request: Request,
    bot_token_hash: str,
    config_id: str = Query(..., description="ChannelConfig ID for this Telegram bot"),
):
    """Receive incoming updates from Telegram.

    Telegram POSTs JSON updates to the registered webhook URL.
    The URL includes a hash of the bot token for verification.
    Additionally, the optional X-Telegram-Bot-Api-Secret-Token header
    is checked if a secret_token was configured.
    """
    adapter, cc = await _get_adapter_and_config(config_id)

    # Verify the bot token hash matches
    expected_hash = _hash_bot_token(adapter.bot_token)
    if bot_token_hash != expected_hash:
        logger.warning(
            "Telegram webhook token hash mismatch: expected=%s got=%s config=%s",
            expected_hash[:8], bot_token_hash[:8], config_id,
        )
        raise HTTPException(403, "Invalid bot token hash")

    # Optionally verify the secret token header
    secret_token = (cc.credentials or {}).get("secret_token", "")
    if secret_token:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_secret != secret_token:
            raise HTTPException(403, "Invalid secret token")

    # Parse JSON body
    try:
        update = await request.json()
    except Exception:
        logger.exception("Failed to parse Telegram webhook JSON body")
        return {"ok": True}

    # Parse the update
    try:
        parsed = await adapter.handle_update(update)
    except Exception:
        logger.exception("Failed to parse Telegram update")
        return {"ok": True}

    if not parsed:
        # Unsupported update type — acknowledge silently
        return {"ok": True}

    # Log the inbound message (synchronous — cheap, keeps an audit trail
    # even if the agent dispatch fails downstream)
    try:
        async with async_session() as db:
            await handle_inbound_message(
                db,
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                payload={
                    "from": parsed["sender_id"],
                    "to": str(parsed.get("chat_id", "")),
                    "content": parsed["content"],
                    "message_id": parsed.get("msg_id"),
                    "channel_type": "telegram",
                    "metadata": {
                        "message_type": parsed["message_type"],
                        "sender_name": parsed.get("sender_name", ""),
                        "chat_id": parsed.get("chat_id"),
                    },
                },
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to log inbound Telegram message from %s", parsed.get("sender_id"))

    # Enqueue agent dispatch on Celery so the webhook acks in <100 ms
    # and the LLM run survives worker restarts via broker persistence.
    dispatch_inbound_task.delay(
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=str(parsed["sender_id"]),
        sender_name=parsed.get("sender_name"),
        chat_id=str(parsed.get("chat_id") or parsed["sender_id"]),
        content=parsed["content"] or "",
    )

    # Telegram requires 200 OK to acknowledge receipt
    return {"ok": True}
