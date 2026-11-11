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

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.telegram_adapter import TelegramAdapter
from packages.core.services.channels.telegram_inbound import enqueue_telegram_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/telegram", tags=["channels"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_adapter_and_config(
    config_id: str,
) -> tuple[TelegramAdapter, ChannelConfig, dict]:
    """Load a Telegram source-linked config and lease its current credentials."""
    from packages.core.services.channel_credentials import lease_channel_credentials

    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.id == config_id)
        )
        cc = result.scalar_one_or_none()
        if not cc:
            raise HTTPException(404, "Channel config not found")
        try:
            creds = await lease_channel_credentials(
                db, cc, reason="channel.telegram.inbound_webhook",
            )
        except ValueError as exc:
            raise HTTPException(410, "Telegram credential source is unavailable") from exc

    bot_token = creds.get("bot_token", "")

    if not bot_token:
        raise HTTPException(410, "Telegram credential source is missing bot_token")

    adapter = TelegramAdapter(bot_token=bot_token)
    return adapter, cc, creds


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
    The X-Telegram-Bot-Api-Secret-Token header is required and checked against
    the server-generated secret held by the source Integration.
    """
    adapter, cc, credentials = await _get_adapter_and_config(config_id)

    # Verify the bot token hash matches
    expected_hash = _hash_bot_token(adapter.bot_token)
    if bot_token_hash != expected_hash:
        logger.warning(
            "Telegram webhook token hash mismatch: expected=%s got=%s config=%s",
            expected_hash[:8], bot_token_hash[:8], config_id,
        )
        raise HTTPException(403, "Invalid bot token hash")

    secret_token = credentials.get("secret_token", "")
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret_token and cc.telegram_bot_id is None:
        logger.warning(
            "Accepting legacy Telegram webhook without secret config=%s; "
            "re-register the connection to enable secret verification",
            config_id,
        )
    elif not secret_token or header_secret != secret_token:
        raise HTTPException(403, "Invalid Telegram webhook secret")

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

    # Record the receipt and enqueue the agent dispatch on Celery in one
    # commit, so the webhook acks in <100 ms, a Telegram retry of the same
    # update is acknowledged once, and the run survives worker restarts.
    try:
        await enqueue_telegram_update(cc, parsed, source="webhook")
    except Exception:
        logger.exception("Could not enqueue Telegram update for config=%s", cc.id)
        # Telegram redelivers an update it did not get a 2xx for.
        raise HTTPException(503, "Telegram update queue unavailable")

    # Telegram requires 200 OK to acknowledge receipt
    return {"ok": True}
