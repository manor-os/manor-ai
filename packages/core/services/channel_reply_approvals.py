from __future__ import annotations

import logging
from typing import Any

from packages.core.database import async_session
from packages.core.services.hitl_options import approval_options

logger = logging.getLogger(__name__)


async def maybe_hold_external_reply_for_approval(
    *,
    entity_id: str,
    workspace_id: str | None,
    channel_type: str,
    channel_config_id: str,
    conversation_id: str,
    agent_subscription_id: str | None,
    sender_id: str,
    sender_name: str | None,
    chat_id: str,
    reply_text: str,
    customer_message: str | None = None,
) -> dict[str, Any] | None:
    """Pause a channel text reply when workspace governance requires review."""
    if not workspace_id or not reply_text.strip():
        return None
    if channel_type == "webchat":
        return None

    async with async_session() as db:
        from packages.core.governance import check_step_policy

        decision = await check_step_policy(
            db,
            workspace_id=workspace_id,
            kind="action",
            action_key="external_message.send",
            risk_level="high",
        )
        if decision.allowed:
            return None

        from packages.core.workspace_chat import service as chat_service

        customer_message = (customer_message or "").strip()
        if customer_message:
            await chat_service.post_message(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                body=customer_message,
                message_kind="external_message",
                author_kind="external",
                meta={
                    "channel_type": channel_type,
                    "channel_config_id": channel_config_id,
                    "channel_conversation_id": conversation_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "chat_id": chat_id,
                },
                refs=[
                    {"type": "conversation", "id": conversation_id},
                    {"type": "channel_config", "id": channel_config_id},
                ],
            )

        if decision.pause_for_hitl:
            preview = reply_text.strip()
            if len(preview) > 1400:
                preview = preview[:1400].rstrip() + "..."
            body = (
                "Approval needed before sending an external message.\n\n"
                f"Channel: `{channel_type}`\n"
                f"Recipient: `{sender_name or sender_id}`\n"
                f"Rule: `{decision.matched_rule or 'external_message.send'}`\n\n"
                f"Draft reply:\n{preview}"
            )
            msg = await chat_service.post_message(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                body=body,
                message_kind="hitl_prompt",
                author_kind="system",
                pending_action={
                    "kind": "external_message_approval",
                    "channel_type": channel_type,
                    "channel_config_id": channel_config_id,
                    "channel_conversation_id": conversation_id,
                    "agent_subscription_id": agent_subscription_id,
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "reply_text": reply_text,
                    "action_key": "external_message.send",
                    "matched_rule": decision.matched_rule,
                    "options": approval_options(),
                },
                refs=[
                    {"type": "conversation", "id": conversation_id},
                    {"type": "channel_config", "id": channel_config_id},
                ],
            )
            await db.commit()

            try:
                from packages.core.services.notification_workspace_callbacks import (
                    notify_workspace_hitl_approvers,
                )

                hitl_title = "Approve external reply?"
                hitl_body = (
                    f"Recipient: {sender_name or sender_id}\n\n"
                    f"Draft: {preview}"
                )
                await notify_workspace_hitl_approvers(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    chat_message_id=msg.id,
                    title=hitl_title,
                    body=hitl_body,
                )
            except Exception:
                logger.warning(
                    "Channel HITL approver notification failed for ws=%s msg=%s",
                    workspace_id,
                    msg.id,
                    exc_info=True,
                )

            return {
                "status": "approval_required",
                "approval_message_id": msg.id,
                "matched_rule": decision.matched_rule,
                "sent": False,
            }

        body = (
            "External message blocked by workspace governance.\n\n"
            f"Channel: `{channel_type}`\n"
            f"Recipient: `{sender_name or sender_id}`\n"
            f"Reason: {decision.reason or 'blocked'}"
        )
        msg = await chat_service.post_message(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            body=body,
            message_kind="goal_alert",
            author_kind="system",
            refs=[
                {"type": "conversation", "id": conversation_id},
                {"type": "channel_config", "id": channel_config_id},
            ],
        )
        await db.commit()
        return {
            "status": "blocked_by_governance",
            "blocked_message_id": msg.id,
            "matched_rule": decision.matched_rule,
            "sent": False,
        }
