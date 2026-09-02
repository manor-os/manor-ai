"""Discord Gateway consumer for mentions in connected Guilds.

Discord's HTTP Interactions endpoint receives slash commands only. Normal
Guild messages arrive over the Gateway, so this singleton runner normalises
bot mentions and hands them to the existing durable channel dispatch path.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.services.channel_bindings import channel_workspace_is_routable
from packages.core.services.channel_credentials import (
    channel_credential_source_is_available,
)
from packages.core.services.channel_inbound_receipts import (
    DISCORD_GATEWAY_SOURCE,
    DISPATCH_METADATA_KEY,
    DISPATCH_PUBLISH_STALE_AFTER,
    DISPATCH_RETRY_AFTER,
    dispatch_time_is_stale,
    read_dispatch_metadata,
    write_dispatch_metadata,
)
from packages.core.services.channel_service import handle_inbound_message
from packages.core.services.discord_app_config import resolve_discord_gateway_config
from packages.core.tasks.channel_tasks import dispatch_inbound_task


logger = logging.getLogger(__name__)

_READY_PATH = Path(os.getenv(
    "DISCORD_GATEWAY_READY_PATH",
    "/tmp/discord-gateway-ready",  # nosec B108 -- container-local K8s probe contract
))
_RECOVERY_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class DiscordGatewayMessage:
    message_id: str
    guild_id: str
    channel_id: str
    sender_id: str
    sender_name: str | None
    content: str


def build_gateway_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    # Discord includes content for messages that explicitly mention the app,
    # so the inbound @Manor path does not need the privileged intent. Avoid
    # requesting it because an unapproved intent makes the Gateway disconnect
    # with 4014 before it can receive any messages.
    return intents


def normalize_discord_message(
    payload: dict[str, Any],
    *,
    bot_user_id: str,
) -> DiscordGatewayMessage | None:
    """Accept human text mentions only; never consume all Guild traffic."""
    guild_id = str(payload.get("guild_id") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    message_id = str(payload.get("id") or "").strip()
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    sender_id = str(author.get("id") or "").strip()
    if not all((guild_id, channel_id, message_id, sender_id, bot_user_id)):
        return None
    if author.get("bot") is True or payload.get("webhook_id"):
        return None
    if payload.get("type") not in {0, 19, None}:
        return None

    mentions = payload.get("mentions") if isinstance(payload.get("mentions"), list) else []
    if not any(
        isinstance(mention, dict)
        and str(mention.get("id") or "") == bot_user_id
        for mention in mentions
    ):
        return None

    content = str(payload.get("content") or "")
    content = re.sub(rf"<@!?{re.escape(bot_user_id)}>", "", content).strip()
    if not content:
        return None
    sender_name = str(
        author.get("global_name") or author.get("username") or ""
    ).strip() or None
    return DiscordGatewayMessage(
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
    )


async def _resolve_channel_config(
    *,
    application_id: str,
    guild_id: str,
) -> ChannelConfig | None:
    async with async_session() as db:
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
        if not await channel_workspace_is_routable(
            db,
            config.workspace_id,
            entity_id=config.entity_id,
        ):
            return None
        if not await channel_credential_source_is_available(db, config):
            return None
        return config


async def ingest_discord_message(
    payload: dict[str, Any],
    *,
    application_id: str,
    bot_user_id: str,
) -> str:
    message = normalize_discord_message(payload, bot_user_id=bot_user_id)
    if message is None:
        return "ignored"
    config = await _resolve_channel_config(
        application_id=application_id,
        guild_id=message.guild_id,
    )
    if config is None:
        return "not_connected"

    async with async_session() as db:
        try:
            inbound_log = await handle_inbound_message(
                db,
                entity_id=config.entity_id,
                channel_config_id=config.id,
                payload={
                    "from": message.sender_id,
                    "to": message.channel_id,
                    "content": message.content,
                    "external_id": message.message_id,
                    "channel_type": "discord",
                },
            )
            inbound_log.attachments = write_dispatch_metadata(
                inbound_log.attachments,
                source=DISCORD_GATEWAY_SOURCE,
                sender_name=message.sender_name,
            )
        except IntegrityError:
            await db.rollback()
            duplicate = await db.scalar(
                select(MessageLog.id).where(
                    MessageLog.channel_config_id == config.id,
                    MessageLog.direction == "inbound",
                    MessageLog.channel_type == "discord",
                    MessageLog.external_id == message.message_id,
                )
            )
            if duplicate is None:
                raise
            return "duplicate"
        await db.commit()

    if await _publish_discord_gateway_receipt(inbound_log.id):
        return "queued"
    logger.error(
        "Stored Discord Gateway message %s for queue recovery",
        message.message_id,
    )
    return "stored_for_retry"


def _gateway_dispatch_kwargs(
    receipt: MessageLog,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    if not all(
        (
            receipt.entity_id,
            receipt.channel_config_id,
            receipt.from_address,
            receipt.to_address,
            receipt.content,
        )
    ):
        return None
    return {
        "entity_id": receipt.entity_id,
        "channel_config_id": receipt.channel_config_id,
        "channel_type": "discord",
        "sender_id": receipt.from_address,
        "sender_name": metadata.get("sender_name"),
        "chat_id": receipt.to_address,
        "content": receipt.content,
        "inbound_message_log_id": receipt.id,
    }


async def _publish_discord_gateway_receipt(
    receipt_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    published_at = now.isoformat()
    async with async_session() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == receipt_id)
            .with_for_update()
        )
        if receipt is None:
            return False
        metadata = read_dispatch_metadata(receipt.attachments)
        if not metadata or metadata.get("source") != DISCORD_GATEWAY_SOURCE:
            return False
        if receipt.status in {"processed", "failed"}:
            return False
        if receipt.status == "received":
            if metadata.get("published_at") and not dispatch_time_is_stale(
                metadata.get("published_at"),
                now=now,
                after=DISPATCH_RETRY_AFTER,
            ):
                return False
        elif receipt.status == "queued":
            if not dispatch_time_is_stale(
                metadata.get("published_at"),
                now=now,
                after=DISPATCH_PUBLISH_STALE_AFTER,
            ):
                return False
        elif receipt.status == "processing":
            if not dispatch_time_is_stale(
                metadata.get("claimed_at"),
                now=now,
                after=DISPATCH_PUBLISH_STALE_AFTER,
            ):
                return False
        else:
            return False

        config = await db.get(ChannelConfig, receipt.channel_config_id)
        if (
            config is None
            or config.entity_id != receipt.entity_id
            or config.channel_type != "discord"
            or config.status != "active"
            or not config.owner_user_id
            or not await channel_workspace_is_routable(
                db,
                config.workspace_id,
                entity_id=receipt.entity_id,
            )
            or not await channel_credential_source_is_available(db, config)
        ):
            receipt.status = "failed"
            receipt.error_message = "Discord connection is no longer available"
            await db.commit()
            return False

        dispatch_kwargs = _gateway_dispatch_kwargs(receipt, metadata)
        if dispatch_kwargs is None:
            receipt.status = "failed"
            receipt.error_message = "Discord Gateway receipt is incomplete"
            await db.commit()
            return False
        receipt.status = "queued"
        receipt.error_message = None
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            published_at=published_at,
            claim_id=None,
            claimed_at=None,
        )
        await db.commit()

    try:
        dispatch_inbound_task.delay(**dispatch_kwargs)
    except Exception:
        logger.exception("Could not enqueue Discord Gateway receipt %s", receipt_id)
        async with async_session() as db:
            receipt = await db.scalar(
                select(MessageLog)
                .where(MessageLog.id == receipt_id)
                .with_for_update()
            )
            metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
            if (
                receipt is not None
                and receipt.status == "queued"
                and metadata
                and metadata.get("published_at") == published_at
            ):
                receipt.status = "received"
                receipt.error_message = "Discord Gateway queue unavailable"
                receipt.attachments = write_dispatch_metadata(
                    receipt.attachments,
                    published_at=None,
                    claim_id=None,
                    claimed_at=None,
                )
                await db.commit()
        return False
    return True


async def recover_discord_gateway_messages(
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> int:
    async with async_session() as db:
        receipts = list(
            (
                await db.execute(
                    select(MessageLog)
                    .where(
                        MessageLog.channel_type == "discord",
                        MessageLog.direction == "inbound",
                        MessageLog.status.in_({"received", "queued", "processing"}),
                        MessageLog.attachments[DISPATCH_METADATA_KEY]["source"].astext
                        == DISCORD_GATEWAY_SOURCE,
                    )
                    .order_by(MessageLog.created_at.asc())
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        receipt_ids = [receipt.id for receipt in receipts]

    recovered = 0
    for receipt_id in receipt_ids:
        if await _publish_discord_gateway_receipt(receipt_id, now=now):
            recovered += 1
    return recovered


async def _run_recovery_loop() -> None:
    while True:
        try:
            recovered = await recover_discord_gateway_messages()
            if recovered:
                logger.info("Recovered %d Discord Gateway receipts", recovered)
        except Exception:
            logger.exception("Discord Gateway receipt recovery failed")
        await asyncio.sleep(_RECOVERY_INTERVAL_SECONDS)


class ManorDiscordGatewayClient(discord.Client):
    def __init__(self, *, application_id: str) -> None:
        super().__init__(intents=build_gateway_intents())
        self.deployment_application_id = application_id
        self._application_verified = False
        self._recovery_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        connected_application_id = str(self.application_id or "")
        if connected_application_id != self.deployment_application_id:
            raise RuntimeError(
                "Discord Gateway application mismatch: "
                f"configured={self.deployment_application_id} "
                f"connected={connected_application_id or 'missing'}"
            )
        self._application_verified = True
        self._recovery_task = asyncio.create_task(_run_recovery_loop())

    async def on_ready(self) -> None:
        if self.user is None:
            return
        _READY_PATH.touch()
        logger.info(
            "Discord Gateway ready application=%s bot_user=%s guilds=%d",
            self.deployment_application_id,
            self.user.id,
            len(self.guilds),
        )

    async def on_resumed(self) -> None:
        _READY_PATH.touch()
        logger.info(
            "Discord Gateway resumed application=%s",
            self.deployment_application_id,
        )

    async def on_disconnect(self) -> None:
        _READY_PATH.unlink(missing_ok=True)
        logger.warning("Discord Gateway disconnected")

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or not self._application_verified:
            return
        author = message.author
        payload = {
            "id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "channel_id": str(message.channel.id),
            "type": message.type.value,
            "content": message.content,
            "webhook_id": message.webhook_id,
            "author": {
                "id": str(author.id),
                "username": author.name,
                "global_name": getattr(author, "global_name", None),
                "bot": author.bot,
            },
            "mentions": [
                {"id": str(mention.id), "bot": mention.bot}
                for mention in message.mentions
            ],
        }
        result = await ingest_discord_message(
            payload,
            application_id=self.deployment_application_id,
            bot_user_id=str(self.user.id),
        )
        if result not in {"ignored", "duplicate"}:
            logger.info(
                "Discord Gateway message=%s guild=%s status=%s",
                message.id,
                message.guild.id if message.guild else None,
                result,
            )

    async def close(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        await super().close()


async def run_gateway() -> None:
    _READY_PATH.unlink(missing_ok=True)
    async with async_session() as db:
        app = await resolve_discord_gateway_config(db)
    if app is None:
        raise RuntimeError("Discord App runtime configuration is invalid")

    client = ManorDiscordGatewayClient(application_id=app.application_id)
    try:
        async with client:
            await client.start(app.bot_token, reconnect=True)
    finally:
        _READY_PATH.unlink(missing_ok=True)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    asyncio.run(run_gateway())


if __name__ == "__main__":
    main()
