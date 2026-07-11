"""Production callback handlers for workspace HITL pending actions.

When ``channel_reply_approvals.maybe_hold_external_reply_for_approval`` pauses an
outbound reply for operator review, it now ALSO fans out a notification
to the configured approvers with ``actions=[approve, always_approve, reject]`` and
``callback_kind="workspace.hitl.resolve_message"``. Whichever surface
the operator chooses to use — web chat button or channel reply — the
same ``chat_service.resolve_pending_action`` runs against the same
underlying ``Message`` row.

This module owns:

  1. The callback registration ("workspace.hitl.resolve_message") that
     turns a channel reply into a workspace_chat resolution.

  2. A producer helper ``notify_workspace_hitl_approvers`` that channel
     gateway calls right after writing the pending_action card. It looks
     up the configured approver user IDs (from workspace settings, falling
     back to entity owners) and sends each one a notification with the
     matching ``callback_payload``.

Resolution mapping (channel reply → ``resolution`` shape consumed by the
existing UI route):

  - ``approve``  → ``{"choice": "approve"}``     (mirrors the Approve button)
  - ``reject``   → ``{"choice": "reject"}``      (mirrors the Reject button)
  - anything else (numeric pick, synonym) → ``{"choice": <key>}``

The HITL approval flow then either:
  - approve → fires ``deliver_approved_external_reply`` to ship the
    paused text via the channel adapter
  - reject  → records the rejection; the original recipient gets nothing
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.services.hitl_options import approval_notification_actions
from packages.core.services.notification_callbacks import register_callback

logger = logging.getLogger(__name__)


CALLBACK_KIND = "workspace.hitl.resolve_message"


async def _resolve_message_via_chat_service(
    payload: dict[str, Any],
    action_key: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Channel-reply HITL handler.

    Mirrors the existing ``POST /api/v1/workspace-chat/{message_id}/resolve``
    endpoint: load the message, validate it's still open, hand it to
    ``resolve_pending_action`` with a resolution shaped like the web UI's
    button click. Then run the kind-specific follow-up (currently:
    ``external_message_approval`` → deliver the queued channel reply).

    Returns ``{"ok": bool, "message": str, "details"?: ...}`` so the
    inbound flow can ack the user. The detailed follow-up (e.g. "sent
    external reply OK") goes into the ``message`` field so the operator
    sees what happened right inside their chat.
    """
    chat_message_id = str(payload.get("chat_message_id") or "")
    if not chat_message_id:
        return {"ok": False, "error": "missing_chat_message_id"}

    responder = context.get("responder") or {}
    responder_user_id = responder.get("user_id")
    if not responder_user_id:
        # Channel binding hasn't been claimed by a Manor user — refuse
        # the resolution rather than approving with a phantom identity.
        return {
            "ok": False,
            "error": "responder_unverified",
            "message": (
                "Couldn't apply your response — this channel isn't linked "
                "to a verified Manor user yet."
            ),
        }

    from packages.core.models.task import Message
    from packages.core.workspace_chat import service as chat_service

    resolution = {"choice": action_key}
    async with async_session() as db:
        msg = (await db.execute(
            select(Message).where(Message.id == chat_message_id)
        )).scalar_one_or_none()
        if msg is None:
            return {"ok": False, "error": "chat_message_not_found"}
        if msg.resolved_at is not None:
            return {
                "ok": False,
                "error": "already_resolved",
                "message": "That approval was already handled.",
            }

        await chat_service.resolve_pending_action(
            db,
            message_id=chat_message_id,
            user_id=responder_user_id,
            resolution=resolution,
        )

        kind = (msg.pending_action or {}).get("kind") if isinstance(msg.pending_action, dict) else None
        ack_message = "Recorded your response."
        if kind == "external_message_approval":
            ack_message = await _resolve_external_message_action(
                db, msg=msg, action_key=action_key, responder_user_id=responder_user_id,
            )
        await db.commit()

    return {"ok": True, "message": ack_message}


async def _resolve_external_message_action(
    db,
    *,
    msg,
    action_key: str,
    responder_user_id: str,
) -> str:
    """Run the side effects for an external_message_approval card.

    Mirrors the matching branch in
    ``apps/api/routers/workspace_chat.py::resolve`` — approve → deliver
    the queued reply via the channel adapter; reject → leave the original
    sender hanging (no automatic apology message; producers can layer one
    on later).
    """
    pa = msg.pending_action or {}
    approved_choices = {"approve", "approved", "yes", "accept", "confirm"}
    if action_key.lower() in approved_choices:
        from packages.core.models.task import Conversation, Message as ChatMessage
        from packages.core.services.channel_outbound_delivery import (
            deliver_approved_external_reply,
        )

        entity_id_value = pa.get("entity_id")
        if not entity_id_value:
            conv = (await db.execute(
                select(Conversation).where(Conversation.id == msg.conversation_id)
            )).scalar_one_or_none()
            entity_id_value = conv.entity_id if conv else ""

        result = await deliver_approved_external_reply(
            db,
            entity_id=str(entity_id_value or ""),
            channel_config_id=str(pa.get("channel_config_id") or ""),
            channel_type=str(pa.get("channel_type") or ""),
            channel_conversation_id=str(pa.get("channel_conversation_id") or ""),
            chat_id=str(pa.get("chat_id") or pa.get("sender_id") or ""),
            text=str(pa.get("reply_text") or ""),
            agent_subscription_id=pa.get("agent_subscription_id"),
        )
        if result.get("sent"):
            body = "Approved — the external reply was just sent."
        else:
            body = (
                "Approved, but the channel adapter did not ship the reply: "
                f"{result.get('reason') or result.get('error') or 'unknown'}"
            )
        db.add(ChatMessage(
            conversation_id=msg.conversation_id,
            role="system",
            content=body,
            author_kind="system",
            message_kind="system",
            refs=[
                {"type": "message", "id": msg.id},
                {"type": "channel_conversation", "id": pa.get("channel_conversation_id")},
                {"type": "message_log", "id": result.get("message_log_id")},
            ],
        ))
        await db.flush()
        return body

    return "Rejected — the draft external reply was not sent."


register_callback(CALLBACK_KIND, _resolve_message_via_chat_service)


# ── Producer helper ──────────────────────────────────────────────────────


async def notify_workspace_hitl_approvers(
    *,
    entity_id: str,
    workspace_id: str | None,
    chat_message_id: str,
    title: str,
    body: str,
    recipient_user_ids: list[str] | None = None,
) -> int:
    """Send the HITL approval card out as an actionable notification.

    ``recipient_user_ids`` overrides the resolution if the producer knows
    exactly whom to ping. Otherwise the helper walks workspace settings
    (``settings.notification_policy.hitl_notify_user_ids``) and falls
    back to entity owners + admins so something always reaches a human.

    Returns the number of users notified. Failures per-user are logged
    and swallowed — one bad address never holds up the rest.
    """
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace
    from packages.core.services.notify import notify

    user_ids: list[str] = list(recipient_user_ids or [])
    # Empty list passed in by the caller is an explicit "no recipients"
    # — honour it and skip the workspace/entity fallback. ``None`` means
    # "no preference, please resolve".
    explicit_empty = recipient_user_ids is not None and not user_ids

    if not user_ids and not explicit_empty and workspace_id:
        async with async_session() as db:
            ws = (await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                )
            )).scalar_one_or_none()
            if ws and isinstance(ws.settings, dict):
                policy = ws.settings.get("notification_policy") or {}
                configured = policy.get("hitl_notify_user_ids")
                if isinstance(configured, list):
                    # Same semantics at the workspace tier: an empty
                    # list opt-outs the workspace; absence falls through.
                    user_ids = [str(u) for u in configured if isinstance(u, str) and u]
                    if not user_ids:
                        explicit_empty = True

    if not user_ids and not explicit_empty:
        async with async_session() as db:
            rows = (await db.execute(
                select(User).where(
                    User.entity_id == entity_id,
                    User.status == "active",
                    User.role.in_(("owner", "admin")),
                )
            )).scalars().all()
            user_ids = [u.id for u in rows]

    if not user_ids:
        logger.info(
            "workspace.hitl notify: no recipients resolvable for entity=%s ws=%s",
            entity_id, workspace_id,
        )
        return 0

    actions = approval_notification_actions()

    delivered = 0
    for user_id in user_ids:
        try:
            await notify(
                entity_id=entity_id,
                user_id=user_id,
                type="task_hitl_requested",
                title=title,
                body=body,
                severity="warn",
                workspace_id=workspace_id,
                actions=actions,
                callback_kind=CALLBACK_KIND,
                callback_payload={
                    "chat_message_id": chat_message_id,
                    "workspace_id": workspace_id,
                    "entity_id": entity_id,
                },
                # Approval prompts shouldn't linger forever — a stale one
                # could cause an "approve" reply meant for something else
                # to release an unrelated message.
                expires_in_seconds=24 * 60 * 60,
            )
            delivered += 1
        except Exception:
            logger.warning(
                "workspace.hitl notify: failed for user=%s", user_id,
                exc_info=True,
            )
    return delivered
