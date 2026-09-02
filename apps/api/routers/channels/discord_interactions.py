"""Fixed Discord Interactions endpoint for user-owned Guild installations."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_credentials import (
    channel_credential_source_is_available,
)
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.channels.discord_adapter import verify_discord_signature
from packages.core.services.discord_app_config import resolve_discord_app_config
from packages.core.tasks.channel_tasks import dispatch_inbound_task


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channels/discord", tags=["channels"])

_NOT_CONNECTED = {
    "type": 4,
    "data": {
        "content": "This Discord Server is not connected to Manor.",
        "flags": 64,
    },
}
_UNSUPPORTED_COMMAND = {
    "type": 4,
    "data": {
        "content": "Use /manor to send a message to Manor.",
        "flags": 64,
    },
}


async def _resolve_channel_config(
    db: AsyncSession,
    *,
    application_id: str,
    guild_id: str,
) -> ChannelConfig | None:
    config = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == "discord",
                ChannelConfig.status == "active",
                ChannelConfig.owner_user_id.is_not(None),
                ChannelConfig.discord_application_id == application_id,
                ChannelConfig.discord_guild_id == guild_id,
            )
        )
    ).scalar_one_or_none()
    if config is None:
        return None
    if not await channel_credential_source_is_available(db, config):
        return None
    return config


def _command_message(payload: dict) -> str | None:
    if payload.get("type") != 2:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if data.get("name") != "manor":
        return None
    options = data.get("options") if isinstance(data.get("options"), list) else []
    for option in options:
        if (
            isinstance(option, dict)
            and option.get("name") == "message"
            and option.get("value") is not None
        ):
            message = str(option["value"]).strip()
            return message or None
    return None


@router.post("/interactions")
async def discord_interactions(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")

    async with async_session() as db:
        app = await resolve_discord_app_config(db)
        if app is None:
            raise HTTPException(503, "Discord App runtime configuration is invalid")

        if not verify_discord_signature(
            public_key=app.public_key,
            signature=signature,
            timestamp=timestamp,
            body=body,
        ):
            raise HTTPException(401, "Signature verification failed")

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "Invalid Discord Interaction payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Invalid Discord Interaction payload")
        application_id = str(payload.get("application_id") or "").strip()
        if application_id != app.application_id:
            raise HTTPException(401, "Discord application mismatch")
        if payload.get("type") == 1:
            return JSONResponse({"type": 1})

        content = _command_message(payload)
        if content is None:
            return JSONResponse(_UNSUPPORTED_COMMAND)

        guild_id = str(payload.get("guild_id") or "").strip()
        channel_id = str(payload.get("channel_id") or "").strip()
        interaction_id = str(payload.get("id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        user = (
            (payload.get("member") or {}).get("user")
            if isinstance(payload.get("member"), dict)
            else None
        )
        if not isinstance(user, dict):
            user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        sender_id = str(user.get("id") or "").strip()
        sender_name = str(
            user.get("global_name") or user.get("username") or ""
        ).strip() or None
        if not all((guild_id, channel_id, interaction_id, interaction_token, sender_id)):
            return JSONResponse(_UNSUPPORTED_COMMAND)

        config = await _resolve_channel_config(
            db,
            application_id=application_id,
            guild_id=guild_id,
        )
        if config is None:
            return JSONResponse(_NOT_CONNECTED)

        entity_id = config.entity_id
        config_id = config.id
        try:
            inbound_log = await handle_inbound_message(
                db,
                entity_id=entity_id,
                channel_config_id=config_id,
                payload={
                    "from": sender_id,
                    "to": channel_id,
                    "content": content,
                    "external_id": interaction_id,
                    "channel_type": "discord",
                },
            )
        except IntegrityError:
            await db.rollback()
            duplicate = await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.channel_config_id == config_id,
                    MessageLog.direction == "inbound",
                    MessageLog.channel_type == "discord",
                    MessageLog.external_id == interaction_id,
                )
            )
            if duplicate is None:
                raise
            return JSONResponse({"type": 5})

        try:
            dispatch_inbound_task.delay(
                entity_id=entity_id,
                channel_config_id=config_id,
                channel_type="discord",
                sender_id=sender_id,
                sender_name=sender_name,
                chat_id=channel_id,
                content=content,
                inbound_message_log_id=inbound_log.id,
                reply_context={
                    "application_id": application_id,
                    "interaction_token": interaction_token,
                },
            )
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "Could not enqueue Discord Interaction %s",
                interaction_id,
            )
            raise HTTPException(503, "Discord Interaction queue unavailable") from exc
        await db.commit()

    return JSONResponse({"type": 5})
