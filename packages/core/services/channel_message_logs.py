from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import MessageLog


async def create_channel_outbound_log(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    to_address: str,
    content: str,
    conversation_id: str | None = None,
    from_address: str | None = None,
    status: str = "queued",
) -> MessageLog:
    """Create the queued outbound log shared by channel delivery paths."""
    log = MessageLog(
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        conversation_id=conversation_id,
        direction="outbound",
        channel_type=channel_type,
        from_address=from_address,
        to_address=to_address,
        content=content,
        status=status,
    )
    db.add(log)
    await db.flush()
    return log


def normalize_channel_outbound_status(raw: str) -> str:
    status = (raw or "").strip().lower()
    if status in {"queued", "sent", "delivered", "failed"}:
        return status
    if status == "deferred":
        # Voice adapters may defer transport to a live websocket path.
        return "sent"
    return "sent"


async def mark_last_channel_outbound_failed(cc_id: str, chat_id: str) -> None:
    async with async_session() as db:
        row = (await db.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == cc_id,
                MessageLog.direction == "outbound",
                MessageLog.to_address == str(chat_id),
            ).order_by(desc(MessageLog.created_at)).limit(1)
        )).scalar_one_or_none()
        if row and row.status == "queued":
            row.status = "failed"
            row.error_message = "adapter send failed"
            await db.commit()


async def mark_last_channel_outbound_sent(
    cc_id: str,
    chat_id: str,
    result: Any,
) -> None:
    if not isinstance(result, dict):
        return

    async with async_session() as db:
        row = (await db.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == cc_id,
                MessageLog.direction == "outbound",
                MessageLog.to_address == str(chat_id),
            ).order_by(desc(MessageLog.created_at)).limit(1)
        )).scalar_one_or_none()
        if not row:
            return

        row.status = normalize_channel_outbound_status(str(result.get("status", "")))
        external_id = result.get("external_id")
        if external_id:
            row.external_id = str(external_id)
        from_address = result.get("from_address")
        if from_address and not row.from_address:
            row.from_address = str(from_address)
        await db.commit()
