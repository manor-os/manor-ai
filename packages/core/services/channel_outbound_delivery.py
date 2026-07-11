from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.channel import ChannelContact
from packages.core.services import channels as _channels_pkg  # noqa: F401
from packages.core.services.channel_bindings import load_channel_config
from packages.core.services.channel_conversations import add_channel_assistant_message
from packages.core.services.channel_message_logs import (
    create_channel_outbound_log,
    mark_last_channel_outbound_failed,
    mark_last_channel_outbound_sent,
    normalize_channel_outbound_status,
)
from packages.core.services.channels import ADAPTERS

logger = logging.getLogger(__name__)


async def send_channel_text_reply(
    *,
    cc_id: str,
    channel_type: str,
    chat_id: str,
    text: str,
) -> bool:
    """Route a text reply back to the channel user via the registered adapter."""
    adapter = ADAPTERS.get(channel_type)
    if adapter is None:
        logger.info(
            "Gateway: no adapter registered for channel_type=%s; reply logged only",
            channel_type,
        )
        return False

    async with async_session() as db:
        cc = await load_channel_config(db, cc_id)
    if not cc:
        return False

    try:
        result = await adapter.send_text(cc, chat_id, text)
        await mark_last_channel_outbound_sent(cc_id, chat_id, result)
        if isinstance(result, dict) and str(result.get("status", "")).lower() == "deferred":
            return False
        return True
    except NotImplementedError as exc:
        logger.info("Gateway: %s send_text not implemented yet: %s", channel_type, exc)
        return False
    except Exception:
        logger.exception(
            "Gateway send-back failed for %s chat=%s",
            channel_type,
            chat_id,
        )
        await mark_last_channel_outbound_failed(cc_id, chat_id)
        return False


async def send_actionable_outbound_to_contact(
    db: AsyncSession,
    *,
    contact: ChannelContact,
    text: str,
    actions: list[dict[str, Any]],
    notification_id: str | None = None,
) -> dict[str, Any]:
    """System-initiated actionable outbound with channel-native CTAs."""
    cc = await load_channel_config(db, contact.channel_config_id)
    if not cc:
        return {"sent": False, "error": "channel_config_not_found"}

    log = await create_channel_outbound_log(
        db,
        entity_id=contact.entity_id,
        channel_config_id=cc.id,
        channel_type=contact.channel_type,
        to_address=str(contact.source_id),
        content=text,
    )

    adapter = ADAPTERS.get(contact.channel_type)
    if adapter is None:
        log.status = "failed"
        log.error_message = "no_adapter"
        await db.flush()
        return {"sent": False, "error": "no_adapter", "message_log_id": log.id}

    if notification_id:
        logger.debug(
            "Notification (actionable) dispatch: notif=%s contact=%s channel=%s actions=%d",
            notification_id,
            contact.id,
            contact.channel_type,
            len(actions),
        )

    try:
        result = await adapter.send_actionable_message(
            cc,
            contact.source_id,
            text,
            actions=actions,
        )
    except NotImplementedError as exc:
        log.status = "failed"
        log.error_message = f"not_implemented: {exc}"
        await db.flush()
        return {"sent": False, "error": "not_implemented", "message_log_id": log.id}
    except Exception as exc:
        log.status = "failed"
        log.error_message = str(exc)[:500]
        await db.flush()
        logger.exception(
            "Actionable notification dispatch failed via %s for contact=%s",
            contact.channel_type,
            contact.id,
        )
        return {"sent": False, "error": str(exc), "message_log_id": log.id}

    log.status = normalize_channel_outbound_status(
        str(result.get("status", "")) if isinstance(result, dict) else ""
    )
    if isinstance(result, dict):
        external_id = result.get("external_id") or result.get("message_id")
        if external_id:
            log.external_id = str(external_id)
    await db.flush()
    return {
        "sent": True,
        "message_log_id": log.id,
        "external_id": log.external_id,
        "adapter_result": result if isinstance(result, dict) else None,
    }


async def send_outbound_to_contact(
    db: AsyncSession,
    *,
    contact: ChannelContact,
    text: str,
    notification_id: str | None = None,
) -> dict[str, Any]:
    """System-initiated text outbound to a linked channel contact."""
    cc = await load_channel_config(db, contact.channel_config_id)
    if not cc:
        return {"sent": False, "error": "channel_config_not_found"}

    log = await create_channel_outbound_log(
        db,
        entity_id=contact.entity_id,
        channel_config_id=cc.id,
        channel_type=contact.channel_type,
        to_address=str(contact.source_id),
        content=text,
    )

    adapter = ADAPTERS.get(contact.channel_type)
    if adapter is None:
        log.status = "failed"
        log.error_message = "no_adapter"
        await db.flush()
        return {"sent": False, "error": "no_adapter", "message_log_id": log.id}

    if notification_id:
        logger.debug(
            "Notification dispatch: notif=%s contact=%s channel=%s",
            notification_id,
            contact.id,
            contact.channel_type,
        )

    try:
        result = await adapter.send_text(cc, contact.source_id, text)
    except NotImplementedError as exc:
        log.status = "failed"
        log.error_message = f"not_implemented: {exc}"
        await db.flush()
        return {"sent": False, "error": "not_implemented", "message_log_id": log.id}
    except Exception as exc:
        log.status = "failed"
        log.error_message = str(exc)[:500]
        await db.flush()
        logger.exception(
            "Notification dispatch failed via %s for contact=%s",
            contact.channel_type,
            contact.id,
        )
        return {"sent": False, "error": str(exc), "message_log_id": log.id}

    log.status = normalize_channel_outbound_status(
        str(result.get("status", "")) if isinstance(result, dict) else ""
    )
    if isinstance(result, dict):
        external_id = result.get("external_id") or result.get("message_id")
        if external_id:
            log.external_id = str(external_id)
        from_address = result.get("from_address")
        if from_address:
            log.from_address = str(from_address)
    await db.flush()
    return {
        "sent": True,
        "message_log_id": log.id,
        "external_id": log.external_id,
        "adapter_result": result if isinstance(result, dict) else None,
    }


async def deliver_approved_external_reply(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    channel_conversation_id: str,
    chat_id: str,
    text: str,
    agent_subscription_id: str | None = None,
) -> dict[str, Any]:
    """Persist and send a governance-approved channel reply."""
    cc = await load_channel_config(db, channel_config_id)
    if not cc or cc.entity_id != entity_id:
        return {"ok": False, "error": "channel_config_not_found"}

    await add_channel_assistant_message(
        db,
        conversation_id=channel_conversation_id,
        channel_type=channel_type,
        chat_id=chat_id,
        content=text,
        author_kind="agent" if agent_subscription_id else "system",
        author_subscription_id=agent_subscription_id,
        approved_external_message=True,
    )
    log = await create_channel_outbound_log(
        db,
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        conversation_id=channel_conversation_id,
        channel_type=channel_type,
        to_address=str(chat_id),
        content=text,
    )

    adapter = ADAPTERS.get(channel_type)
    if adapter is None:
        return {"ok": True, "sent": False, "reason": "no_adapter", "message_log_id": log.id}
    try:
        result = await adapter.send_text(cc, chat_id, text)
        log.status = "sent"
        if isinstance(result, dict):
            external_id = result.get("external_id") or result.get("message_id")
            if external_id:
                log.external_id = str(external_id)
        await db.flush()
        return {"ok": True, "sent": True, "message_log_id": log.id, "adapter_result": result}
    except Exception as exc:
        log.status = "failed"
        log.error_message = str(exc)
        await db.flush()
        return {"ok": False, "sent": False, "error": str(exc), "message_log_id": log.id}
