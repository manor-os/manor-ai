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
from types import SimpleNamespace
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from packages.core.constants.channels import ExternalMessageActionKey
from packages.core.constants.notification_types import (
    NotificationCallbackDisposition,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.database import async_session
from packages.core.services.hitl_options import (
    HumanDecisionIntent,
    approval_notification_actions,
    external_reply_decision_intent,
)
from packages.core.services.notification_callbacks import register_callback

logger = logging.getLogger(__name__)


CALLBACK_KIND = "workspace.hitl.resolve_message"


def _approval_claim_conflict_result(reason: str) -> dict[str, Any]:
    if reason == "approval_delivery_in_progress":
        return {
            "ok": False,
            "disposition": NotificationCallbackDisposition.RETRYABLE.value,
            "error": reason,
            "message": (
                "External reply delivery is already in progress; "
                "please retry shortly."
            ),
        }
    if reason == "approval_delivery_outcome_unknown":
        return {
            "ok": False,
            "disposition": NotificationCallbackDisposition.TERMINAL_FAILURE.value,
            "error": reason,
            "message": (
                "External reply delivery outcome is unknown and requires "
                "manual reconciliation; it will not retry automatically."
            ),
        }
    return {
        "ok": False,
        "disposition": NotificationCallbackDisposition.TERMINAL_FAILURE.value,
        "error": reason,
        "message": "That approval is no longer available.",
    }


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
    payload_entity_id = str(payload.get("entity_id") or "")
    workspace_id = str(payload.get("workspace_id") or "")
    context_entity_id = str(context.get("entity_id") or "")
    if not chat_message_id or not payload_entity_id or not workspace_id:
        return {"ok": False, "error": "invalid_callback_scope"}
    if context_entity_id != payload_entity_id:
        return {"ok": False, "error": "callback_scope_mismatch"}

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

    from packages.core.humans import participant_can
    from packages.core.humans.authority import ParticipantAuthority
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_access import (
        user_can_read_workspace_by_identity,
    )
    from packages.core.workspace_chat import service as chat_service

    resolution = {"choice": action_key}
    async with async_session() as db:
        user = (await db.execute(
            select(User).where(
                User.id == responder_user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if user is None:
            return {"ok": False, "error": "responder_inactive"}
        membership = (await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == responder_user_id,
                UserMembership.entity_id == payload_entity_id,
            )
        )).scalar_one_or_none()
        if membership is not None and (
            membership.status != "active" or membership.deleted_at is not None
        ):
            return {"ok": False, "error": "responder_membership_inactive"}
        if membership is None and user.entity_id != payload_entity_id:
            return {"ok": False, "error": "responder_membership_inactive"}
        entity_role = membership.role if membership is not None else user.role
        actor = SimpleNamespace(
            id=user.id,
            entity_id=payload_entity_id,
            role=entity_role,
        )
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == payload_entity_id,
                Workspace.deleted_at.is_(None),
                Workspace.status == "active",
            )
        )).scalar_one_or_none()
        if workspace is None:
            return {"ok": False, "error": "workspace_unavailable"}
        if not await user_can_read_workspace_by_identity(
            db,
            workspace=workspace,
            entity_id=payload_entity_id,
            user_id=user.id,
            role=entity_role,
        ):
            return {"ok": False, "error": "workspace_access_revoked"}
        if not await participant_can(
            db,
            user=actor,
            entity_id=payload_entity_id,
            workspace_id=workspace_id,
            permission_key=ParticipantAuthority.APPROVE_EXTERNAL_PUBLISH.value,
        ):
            return {"ok": False, "error": "external_publish_authority_required"}

        msg = (await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == chat_message_id,
                Conversation.entity_id == payload_entity_id,
                Conversation.workspace_id == workspace_id,
            )
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if msg is None:
            return {"ok": False, "error": "chat_message_not_found"}
        if msg.resolved_at is not None:
            return {
                "ok": False,
                "error": "already_resolved",
                "message": "That approval was already handled.",
            }
        kind = (msg.pending_action or {}).get("kind") if isinstance(msg.pending_action, dict) else None
        if kind != PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
            return {"ok": False, "error": "callback_action_kind_mismatch"}
        ack_message = "Recorded your response."
        decision_intent = external_reply_decision_intent(action_key)
        approved = decision_intent in {
            HumanDecisionIntent.APPROVE,
            HumanDecisionIntent.APPROVE_STANDING,
        }
        standing = decision_intent is HumanDecisionIntent.APPROVE_STANDING
        if decision_intent not in {
            HumanDecisionIntent.APPROVE,
            HumanDecisionIntent.APPROVE_STANDING,
            HumanDecisionIntent.DENY,
            HumanDecisionIntent.CANCEL,
        }:
            return {
                "ok": False,
                "disposition": (
                    NotificationCallbackDisposition.TERMINAL_FAILURE.value
                ),
                "error": "invalid_external_reply_decision",
                "message": "That approval response is not valid.",
            }
        if standing and not await participant_can(
            db,
            user=actor,
            entity_id=payload_entity_id,
            workspace_id=workspace_id,
            permission_key=ParticipantAuthority.MANAGE_STANDING_GRANTS.value,
        ):
            return {
                "ok": False,
                "error": "standing_grant_authority_required",
            }
        pending_action = (
            msg.pending_action if isinstance(msg.pending_action, dict) else {}
        )
        standing_resource_id = str(
            pending_action.get("channel_config_id") or ""
        ).strip()
        if standing and not standing_resource_id:
            return {
                "ok": False,
                "disposition": NotificationCallbackDisposition.TERMINAL_FAILURE.value,
                "error": "external_reply_account_scope_missing",
                "message": (
                    "The channel account scope is missing; reconnect the "
                    "channel before creating a standing grant."
                ),
            }
        if approved:
            from packages.core.services.channel_outbound_delivery import (
                ApprovedExternalReplyDeliveryDisposition,
                ApprovedExternalReplyDeliveryError,
                ApprovedExternalReplyOutcomeUnknownError,
                ApprovedExternalReplySameKeyRetryRequired,
                approved_external_reply_delivery_disposition,
                claim_external_reply_approval,
                external_reply_approval_claim_owner_conflict_reason,
                mark_external_reply_approval_same_key_retry_required,
                release_external_reply_approval_claim,
            )

            claim = await claim_external_reply_approval(
                db,
                message_id=msg.id,
                entity_id=payload_entity_id,
                workspace_id=workspace_id,
                user_id=responder_user_id,
            )
            if not claim["acquired"]:
                return _approval_claim_conflict_result(claim["reason"])
            claim_id = claim["claim_id"]
            assert claim_id is not None
            claimed_pending_action = claim.get("pending_action")
            if not isinstance(claimed_pending_action, dict):
                await release_external_reply_approval_claim(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                return {
                    "ok": False,
                    "disposition": NotificationCallbackDisposition.RETRYABLE.value,
                    "error": "external_reply_payload_unavailable",
                    "message": "The approval is still open; please retry.",
                }
            pending_action = claimed_pending_action
            standing_resource_id = str(
                pending_action.get("channel_config_id") or ""
            ).strip()
            if standing and not standing_resource_id:
                await release_external_reply_approval_claim(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                return {
                    "ok": False,
                    "disposition": (
                        NotificationCallbackDisposition.TERMINAL_FAILURE.value
                    ),
                    "error": "external_reply_account_scope_missing",
                    "message": (
                        "The channel account scope is missing; reconnect the "
                        "channel before creating a standing grant."
                    ),
                }
            try:
                delivery_result = await _resolve_external_message_action(
                    db,
                    msg=msg,
                    pending_action=pending_action,
                    approval_claim_id=claim_id,
                    retry_mode=claim.get("retry_mode"),
                )
                from packages.core.services.channel_outbound_delivery import (
                    approved_external_reply_outcome_message,
                )

                ack_message = approved_external_reply_outcome_message(delivery_result)
            except SoftTimeLimitExceeded:
                # Provider acceptance is ambiguous. Keep rejection blocked,
                # but allow another approval to reuse the same provider key.
                await mark_external_reply_approval_same_key_retry_required(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                    error="external reply delivery timed out",
                )
                raise
            except ApprovedExternalReplyOutcomeUnknownError:
                return _approval_claim_conflict_result(
                    "approval_delivery_outcome_unknown"
                )
            except ApprovedExternalReplySameKeyRetryRequired as exc:
                await mark_external_reply_approval_same_key_retry_required(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                    error=str(exc),
                )
                return {
                    "ok": False,
                    "disposition": NotificationCallbackDisposition.RETRYABLE.value,
                    "error": "external_reply_retryable",
                    "message": (
                        "The channel provider did not confirm the reply. "
                        "Retrying will reuse the same delivery key."
                    ),
                }
            except ApprovedExternalReplyDeliveryError as exc:
                await release_external_reply_approval_claim(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                if exc.reason_code == "whatsapp_template_required":
                    return {
                        "ok": False,
                        "disposition": NotificationCallbackDisposition.RETRYABLE.value,
                        "error": exc.reason_code,
                        "message": (
                            "The 24-hour WhatsApp customer-service window has "
                            "closed. Send an approved WhatsApp template instead; "
                            "the approval remains open."
                        ),
                    }
                return {
                    "ok": False,
                    "disposition": NotificationCallbackDisposition.RETRYABLE.value,
                    "error": "external_reply_retryable",
                    "message": (
                        "The channel provider did not accept the reply. "
                        "The approval is still open; please retry."
                    ),
                }
            delivery_disposition = approved_external_reply_delivery_disposition(
                delivery_result
            )
            if (
                delivery_disposition
                is ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
            ):
                await release_external_reply_approval_claim(
                    db,
                    message_id=chat_message_id,
                    entity_id=payload_entity_id,
                    workspace_id=workspace_id,
                    claim_id=claim_id,
                )
                return {
                    "ok": False,
                    "disposition": NotificationCallbackDisposition.RETRYABLE.value,
                    "error": "external_reply_retryable",
                    "message": (
                        "The external reply was not sent. The approval is still "
                        "open; please retry."
                    ),
                }
            if (
                delivery_disposition
                is ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN
            ):
                resolution.update({
                    "delivery_outcome": "unknown",
                    "message_log_id": delivery_result.get("message_log_id"),
                    "delivery_error": delivery_result.get("error"),
                })
        else:
            ack_message = "Rejected — the draft external reply was not sent."
        # Provider I/O above ran without a Message lock or staged approval
        # write. Lock and revalidate immediately before single consumption.
        msg = (await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == chat_message_id,
                Conversation.entity_id == payload_entity_id,
                Conversation.workspace_id == workspace_id,
            )
            .with_for_update(of=Message)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if msg is None:
            await db.rollback()
            return {"ok": False, "error": "chat_message_not_found"}
        if msg.resolved_at is not None:
            await db.rollback()
            return {
                "ok": False,
                "error": "already_resolved",
                "message": "That approval was already handled.",
            }
        if not approved:
            from packages.core.services.channel_outbound_delivery import (
                external_reply_approval_claim_conflict_reason,
            )

            conflict_reason = external_reply_approval_claim_conflict_reason(msg)
            if conflict_reason is not None:
                await db.rollback()
                return _approval_claim_conflict_result(conflict_reason)
        else:
            claim_conflict = external_reply_approval_claim_owner_conflict_reason(
                msg,
                claim_id,
            )
            if claim_conflict is not None:
                await db.rollback()
                return _approval_claim_conflict_result(claim_conflict)
        if standing:
            from packages.core.governance import add_auto_approve_action

            await add_auto_approve_action(
                db,
                entity_id=payload_entity_id,
                workspace_id=workspace_id,
                action_key=ExternalMessageActionKey.SEND.value,
                resource_id=standing_resource_id,
                changed_by=responder_user_id,
            )
        await chat_service.resolve_pending_action(
            db,
            message_id=chat_message_id,
            user_id=responder_user_id,
            resolution=resolution,
        )
        await db.commit()

    return {"ok": True, "message": ack_message}


async def _resolve_external_message_action(
    db,
    *,
    msg,
    pending_action: dict[str, Any],
    approval_claim_id: str,
    retry_mode=None,
) -> dict[str, Any]:
    """Run the side effects for an external_message_approval card.

    Mirrors the matching branch in
    ``apps/api/routers/workspace_chat.py::resolve`` — approve → deliver
    the queued reply via the channel adapter; reject → leave the original
    sender hanging (no automatic apology message; producers can layer one
    on later).
    """
    pa = pending_action
    from packages.core.models.task import Conversation, Message as ChatMessage
    from packages.core.services.channel_outbound_delivery import (
        ApprovedExternalReplyDeliveryDisposition,
        ApprovedExternalReplyDeliveryError,
        approved_external_reply_delivery_disposition,
        approved_external_reply_outcome_message,
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
        channel_binding_id=pa.get("channel_binding_id"),
        channel_contact_id=pa.get("channel_contact_id"),
        agent_id=pa.get("agent_id"),
        agent_subscription_id=pa.get("agent_subscription_id"),
        route_snapshot=pa.get("route_snapshot"),
        workspace_id=pa.get("workspace_id"),
        thread_ts=pa.get("thread_ts"),
        idempotency_key=msg.id,
        approval_claim_id=approval_claim_id,
        retry_mode=retry_mode,
    )
    if (
        approved_external_reply_delivery_disposition(result)
        is ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
    ):
        reason = str(result.get("reason") or result.get("error") or "unknown")
        raise ApprovedExternalReplyDeliveryError(reason)
    body = approved_external_reply_outcome_message(result)
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
    return result


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
                idempotency_key=f"workspace-hitl:{chat_message_id}",
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
