"""Durable intake of one Telegram update, shared by the webhook and the poller.

A Telegram ``update_id`` is unique per bot, so it is the receipt key: a
webhook retry or a poller replay of the same update hits the unique receipt
index and is acknowledged without a second agent run. The receipt and the
dispatch enqueue commit together, so a queue failure leaves no receipt and the
same update can be delivered again. The receipt id rides with the dispatch,
which then takes the receipt-aware path (prepared reply, idempotent resend).
"""
from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.exc import IntegrityError

from packages.core.database import async_session
from packages.core.services.channel_service import handle_inbound_message

TELEGRAM_RECEIPT_PREFIX = "update:"


def telegram_receipt_key(parsed: dict[str, Any]) -> str | None:
    """The update's receipt key, or None when the update carries no id."""
    update_id = (parsed.get("raw") or {}).get("update_id")
    if update_id is None or str(update_id).strip() == "":
        return None
    return f"{TELEGRAM_RECEIPT_PREFIX}{update_id}"


async def enqueue_telegram_update(
    cc: Any, parsed: dict[str, Any], *, source: str,
) -> Literal["queued", "duplicate"]:
    """Record the update's receipt and enqueue its dispatch in one commit.

    Raises when the dispatch cannot be enqueued; nothing is recorded then, so
    the caller must let Telegram deliver the update again.
    """
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    async with async_session() as db:
        try:
            inbound_log = await handle_inbound_message(
                db,
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                payload={
                    "from": parsed["sender_id"],
                    "to": str(parsed.get("chat_id", "")),
                    "content": parsed["content"],
                    "external_id": telegram_receipt_key(parsed),
                    "channel_type": "telegram",
                    "metadata": {
                        "message_type": parsed["message_type"],
                        "sender_name": parsed.get("sender_name", ""),
                        "chat_id": parsed.get("chat_id"),
                        "message_id": parsed.get("msg_id"),
                        "source": source,
                    },
                },
            )
        except IntegrityError:
            await db.rollback()
            return "duplicate"
        dispatch_inbound_task.delay(
            entity_id=cc.entity_id,
            channel_config_id=cc.id,
            channel_type="telegram",
            sender_id=str(parsed["sender_id"]),
            sender_name=parsed.get("sender_name"),
            chat_id=str(parsed.get("chat_id") or parsed["sender_id"]),
            content=parsed["content"] or "",
            inbound_message_log_id=inbound_log.id,
        )
        await db.commit()
    return "queued"
