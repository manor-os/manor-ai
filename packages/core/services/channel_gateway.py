"""Channel gateway — generic inbound→agent dispatch + outbound send-back.

One entry point every channel router funnels into:

    dispatch_inbound(db, channel_config, normalized_inbound)

What it does, in order:
  1. Find the `Channel` binding for the inbound's channel_config (matches
     by `entity_id` + `type` + `config.integration_id` or
     `config.channel_config_id`). Without a binding, just logs the
     inbound and returns.
  2. Resolve the bound agent — ``Channel.agent_id``; falls back to the
     entity's master agent.
  3. Resolve or create a `Conversation` keyed by
     `(entity_id, channel=type, meta.channel_config_id, meta.sender_id)`.
     Loads the last N messages as history.
  4. Runs the agentic loop with the agent's system prompt + tools.
  5. Persists the user + assistant turns to `messages`, writes an
     outbound `message_logs` row, and hands the reply to the channel
     adapter to actually send it back to the user (Telegram/WeChat/
     WhatsApp/…).

The webhook routers invoke this in a FastAPI ``BackgroundTasks``, so the
caller's 200 OK lands inside Telegram's ~5 s window even when the LLM
takes a while.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from celery.exceptions import SoftTimeLimitExceeded

from packages.core.ai.runtime import (
    runtime_persist_channel_hold_runtime_events,
    runtime_persist_channel_runtime_events,
)
from packages.core.constants.channels import uses_durable_inbound_receipt
from packages.core.database import async_session
from packages.core.models.document import Integration
# Force-load every channel adapter so ADAPTERS is populated
from packages.core.services import channels as _channels_pkg  # noqa: F401
from packages.core.services.channels import ADAPTERS
# Side-effect import: registers the workspace.hitl.resolve_message callback
# so channel-reply HITL ack can resolve workspace_chat pending actions.
from packages.core.services import notification_workspace_callbacks as _hitl_cb  # noqa: F401
from packages.core.services.agent_subscription_service import (
    resolve_subscription,
)
from packages.core.services.channel_agent_runtime import (
    ChannelAgentRunResult,
    run_channel_agent_turn,
)
from packages.core.services.channel_bindings import (
    channel_workspace_is_routable,
    channel_runtime_config,
    load_channel_binding_by_id_for_config,
    load_channel_binding_for_config,
    load_channel_config,
    load_slack_channel_bindings_for_config,
    resolve_unique_channel_binding_scope,
)
from packages.core.services.channel_conversations import (
    add_channel_assistant_message,
    add_channel_inbound_message,
    get_or_create_channel_conversation,
    load_recent_channel_messages,
)
from packages.core.services.channel_contacts import upsert_channel_contact
from packages.core.services.channel_inbound_actions import (
    ChannelInboundAction,
    maybe_claim_channel_link_token,
    maybe_handle_pending_channel_delivery,
)
from packages.core.services.channel_message_logs import (
    create_channel_outbound_log,
)
from packages.core.services.channel_outbound_delivery import (
    build_channel_reply_route_snapshot,
    channel_reply_route_is_active,
    send_channel_text_reply,
)
from packages.core.services.channel_reply_approvals import (
    maybe_hold_external_reply_for_approval,
)

logger = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────

async def dispatch_inbound(
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    content: str,
    channel_binding_id: str | None = None,
    channel_agent_subscription_id: str | None = None,
    channel_agent_id: str | None = None,
    channel_workspace_id: str | None = None,
    attachments: Optional[List[dict]] = None,
    runtime_metadata: dict[str, Any] | None = None,
    thread_ts: Optional[str] = None,
    reply_context: dict[str, str] | None = None,
    inbound_message_log_id: str | None = None,
    reply_idempotency_key: str | None = None,
    deliver_reply: bool = True,
) -> Dict[str, Any]:
    """Entry point for channel routers (Telegram, WeChat, WhatsApp, …).

    Returns a dict describing what happened — useful for observability /
    tests. Never raises: channel dispatch failures are logged and
    swallowed so a slow LLM doesn't take down the webhook.
    """
    try:
        async with async_session() as db:
            cc = await load_channel_config(db, channel_config_id)
            if not cc:
                return {"status": "skipped", "reason": "no channel config"}
            if cc.entity_id != entity_id:
                return {"status": "skipped", "reason": "entity mismatch"}
            if cc.channel_type != channel_type:
                return {"status": "skipped", "reason": "channel type mismatch"}
            if cc.status != "active":
                return {"status": "skipped", "reason": "channel_config_inactive"}
            from packages.core.services.channel_credentials import (
                channel_credential_source_is_available,
            )

            if not await channel_credential_source_is_available(db, cc):
                return {
                    "status": "skipped",
                    "reason": "channel credential source unavailable",
                }
            if not await channel_workspace_is_routable(
                db,
                cc.workspace_id,
                entity_id=entity_id,
            ):
                return {"status": "skipped", "reason": "workspace unavailable"}

            resolved_channel_sub = None
            if channel_binding_id:
                binding = await load_channel_binding_by_id_for_config(
                    db,
                    cc,
                    channel_binding_id,
                )
            elif channel_type == "slack":
                slack_bindings = await load_slack_channel_bindings_for_config(db, cc)
                if len(slack_bindings) > 1:
                    return {
                        "status": "unbound",
                        "channel_config_id": channel_config_id,
                        "reason": "ambiguous_bindings",
                    }
                binding = slack_bindings[0] if slack_bindings else None
            elif channel_type == "whatsapp":
                try:
                    binding_scope = await resolve_unique_channel_binding_scope(db, cc)
                except ValueError:
                    return {
                        "status": "unbound",
                        "channel_config_id": channel_config_id,
                        "reason": "ambiguous_bindings",
                    }
                if binding_scope and not (
                    binding_scope.agent_id
                    and binding_scope.workspace_id
                    and binding_scope.agent_subscription_id
                ):
                    return {
                        "status": "unbound",
                        "channel_config_id": channel_config_id,
                        "reason": "agent_not_bound",
                    }
                binding = binding_scope.binding if binding_scope else None
            else:
                binding = await load_channel_binding_for_config(db, cc)
            if not binding:
                # Received a message we have nowhere to route. Log it but
                # don't run the agent — users may not have finished setup.
                logger.info(
                    "Gateway: no Channel binding for %s config=%s — logging only",
                    channel_type, channel_config_id,
                )
                return {"status": "unbound", "channel_config_id": channel_config_id}

            if binding.status != "active":
                return {"status": "binding_inactive"}
            if not await channel_workspace_is_routable(
                db,
                binding.workspace_id,
                entity_id=entity_id,
            ):
                return {"status": "skipped", "reason": "workspace unavailable"}
            if channel_type in {
                "slack",
                "discord",
                "twilio_voice",
                "whatsapp",
            } and (
                binding.user_id != cc.owner_user_id
            ):
                return {
                    "status": "unbound",
                    "channel_config_id": channel_config_id,
                    "reason": "binding_owner_mismatch",
                }

            if channel_type == "twilio_voice" and channel_binding_id and channel_agent_id:
                resolved_channel_sub = await resolve_subscription(
                    db,
                    binding=binding,
                    contact=None,
                )
                if (
                    resolved_channel_sub.agent_id != channel_agent_id
                    or resolved_channel_sub.workspace_id != channel_workspace_id
                    or (
                        channel_agent_subscription_id is not None
                        and resolved_channel_sub.id != channel_agent_subscription_id
                    )
                ):
                    return {
                        "status": "unbound",
                        "channel_config_id": channel_config_id,
                        "reason": "binding_scope_changed",
                    }

            # Upsert the per-channel contact identity. Unique index on
            # (channel_config_id, source_id) makes this idempotent — a
            # returning sender always lands on the same row.
            contact = await upsert_channel_contact(
                db, entity_id=entity_id, channel_type=channel_type,
                channel_config_id=channel_config_id,
                source_id=sender_id, sender_name=sender_name,
            )
            if contact.status == "blocked":
                return {"status": "sender_blocked", "channel_contact_id": contact.id}

            # Pull identity fields from the contact early — used for both
            # the Conversation.user_id attribution and the Runtime sender
            # context later. getattr keeps us compatible with workers that
            # haven't reloaded the new user_id/role columns yet.
            contact_user_id = getattr(contact, "user_id", None)
            contact_role = getattr(contact, "role", None) or "external"
            if channel_type == "whatsapp":
                # WhatsApp Business senders are customers. Historical personal-
                # notification links must never grant web-session authority.
                contact_user_id = None
                contact_role = "external"

            # Resolve the subscription: per-contact pin > channel default
            # > legacy agent_id synth. ``sub.id`` is None for the legacy
            # path — everything else works regardless.
            sub = resolved_channel_sub or await resolve_subscription(
                db,
                binding=binding,
                # A Slack installation is bound to exactly one Agent. Contact
                # pins remain available to other shared-channel products.
                contact=(
                    None
                    if channel_type in {
                        "slack",
                        "discord",
                        "twilio_voice",
                        "whatsapp",
                    }
                    else contact
                ),
            )
            if sub.source == "invalid":
                return {
                    "status": "unbound",
                    "channel_config_id": channel_config_id,
                    "reason": "invalid_agent_subscription",
                }
            agent_id = sub.agent_id
            workspace_id = sub.workspace_id
            route_snapshot = build_channel_reply_route_snapshot(
                config_workspace_id=cc.workspace_id,
                binding_workspace_id=binding.workspace_id,
                runtime_workspace_id=workspace_id,
            )
            if not await channel_workspace_is_routable(
                db,
                workspace_id,
                entity_id=entity_id,
            ):
                return {"status": "skipped", "reason": "workspace unavailable"}
            if channel_type in {"slack", "discord", "twilio_voice"} and not agent_id:
                return {
                    "status": "unbound",
                    "channel_config_id": channel_config_id,
                    "reason": "agent_not_bound",
                }

            conversation_key = None
            if channel_type == "slack" and chat_id:
                conversation_key = (
                    f"slack:{chat_id}:thread:{thread_ts}"
                    if thread_ts
                    else f"slack:{chat_id}:direct"
                )

            # Attribute the Conversation to the person who bound the
            # channel so it shows up in their web chat history. Falls
            # back to the sender's claimed user_id when that exists.
            owner_user_id = contact_user_id or binding.user_id
            conv = await get_or_create_channel_conversation(
                db, entity_id=entity_id, channel_type=channel_type,
                channel_config_id=channel_config_id,
                channel_contact_id=contact.id,
                sender_id=sender_id, sender_name=sender_name,
                chat_id=chat_id, agent_id=agent_id,
                user_id=owner_user_id,
                workspace_id=workspace_id,
                agent_subscription_id=sub.id,
                conversation_key=conversation_key,
            )

            await add_channel_inbound_message(
                db,
                conversation_id=conv.id,
                channel_type=channel_type,
                sender_id=sender_id,
                sender_name=sender_name,
                chat_id=chat_id,
                content=content,
                attachments=attachments,
            )
            history = await load_recent_channel_messages(db, conv.id)
            await db.commit()

        # Short-circuit: ``/start <token>`` is the end-user channel-link
        # handshake — bind the contact to the user that minted the
        # token, ack with a confirmation, and don't run the agent. Most
        # inbound text isn't a /start, so the regex check is cheap.
        link_action = await maybe_claim_channel_link_token(
            channel_contact_id=contact.id,
            channel_type=channel_type,
            content=content,
        )
        if link_action is not None:
            ack_message = await _send_inbound_action_ack(
                link_action,
                channel_config_id=channel_config_id,
                channel_binding_id=binding.id,
                channel_type=channel_type,
                chat_id=chat_id or sender_id,
                thread_ts=thread_ts,
                reply_context=reply_context,
                log_label="claim-token",
                deliver_reply=deliver_reply,
            )
            if not deliver_reply:
                return {
                    **link_action.result,
                    "conversation_id": conv.id,
                    "agent_id": agent_id,
                    "ack_message": ack_message,
                }
            return link_action.result

        # Short-circuit: if there's an open notification delivery awaiting
        # a reply on this conversation and the user's text matches one of
        # its action keys, fire the callback + ack instead of running the
        # agent. This is how HITL approvals (and any future actionable
        # notification) close the loop without round-tripping to the web UI.
        pending_action = await maybe_handle_pending_channel_delivery(
            entity_id=entity_id,
            channel_type=channel_type,
            conversation_id=conv.id,
            channel_contact_id=contact.id,
            sender_id=sender_id,
            content=content,
            sender_name=sender_name,
            contact_user_id=contact_user_id,
            contact_role=contact_role,
        )
        if pending_action is not None:
            ack_message = await _send_inbound_action_ack(
                pending_action,
                channel_config_id=channel_config_id,
                channel_binding_id=binding.id,
                channel_type=channel_type,
                chat_id=chat_id or sender_id,
                thread_ts=thread_ts,
                reply_context=reply_context,
                log_label="pending-delivery",
                deliver_reply=deliver_reply,
            )
            if not deliver_reply:
                return {
                    **pending_action.result,
                    "conversation_id": conv.id,
                    "agent_id": agent_id,
                    "ack_message": ack_message,
                }
            return pending_action.result

        # Heads-up for staff: an external contact just pushed something
        # new through the channel. Throttled per (recipient, conversation)
        # so a chatty customer doesn't spam the operator's Telegram. Runs
        # only for ``external`` contacts — internal staff replying via
        # their own bound channel shouldn't notify themselves.
        if (contact_role or "external") == "external":
            try:
                from packages.core.services.notification_channel_inbound import (
                    notify_channel_inbound_recipients,
                )

                await notify_channel_inbound_recipients(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    owner_user_id=getattr(cc, "owner_user_id", None),
                    channel_type=channel_type,
                    channel_contact_id=contact.id,
                    conversation_id=conv.id,
                    sender_name=sender_name,
                    sender_source_id=sender_id,
                    content_preview=content,
                )
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                logger.warning(
                    "Channel inbound notification failed for conv=%s",
                    conv.id, exc_info=True,
                )

        # Sender context — who's on the other end. Runtime context blocks
        # turn this into channel-safe guidance, and linked contacts can
        # still use their user_id for scoped tool execution.
        sender_ctx = {
            "channel_type": channel_type,
            "source_id": sender_id,
            "display_name": sender_name or contact.display_name,
            "user_id": contact_user_id,
            "role": contact_role,
            "is_verified": bool(contact_user_id),
            "conversation_id": conv.id,
            "channel_contact_id": contact.id,
            "channel_language": channel_runtime_config(cc, binding).get("language"),
            # Extras the agent tool injector needs — not part of the
            # Runtime context block payload.
            "_cc_obj": cc,
            "reply_to": chat_id or sender_id,
        }
        reply_route = {
            "entity_id": entity_id,
            "cc_id": channel_config_id,
            "channel_type": channel_type,
            "channel_binding_id": binding.id,
            "channel_contact_id": contact.id,
            "agent_id": agent_id,
            "agent_subscription_id": sub.id,
            "route_snapshot": route_snapshot,
            "workspace_id": workspace_id,
        }

        # Run the agent outside the DB session above so we don't hold a
        # connection while the LLM streams. Wrap in the channel's
        # typing_indicator so the upstream chat shows "typing…" (or
        # whatever the channel's equivalent is) while the agent thinks.
        adapter = ADAPTERS.get(channel_type)
        target = chat_id or sender_id
        async def _do_run() -> Optional[ChannelAgentRunResult]:
            return await run_channel_agent_turn(
                entity_id=entity_id, agent_id=agent_id,
                user_id=binding.user_id,
                conversation_id=conv.id,
                current_message=content,
                history=history,
                sender_ctx=sender_ctx,
                subscription=sub,
                reply_route=reply_route,
                runtime_metadata=runtime_metadata,
            )
        if adapter is not None:
            async with adapter.typing_indicator(cc, target):
                run_result = await _do_run()
        else:
            run_result = await _do_run()
        if not run_result or not run_result.content:
            return {
                "status": "no_reply",
                "agent_id": agent_id,
                "agent_subscription_id": sub.id,
                "workspace_id": workspace_id,
                "conversation_id": conv.id,
            }
        reply_text = run_result.content

        approval_hold = await maybe_hold_external_reply_for_approval(
            entity_id=entity_id,
            workspace_id=workspace_id,
            channel_type=channel_type,
            channel_config_id=channel_config_id,
            channel_binding_id=binding.id,
            channel_contact_id=contact.id,
            conversation_id=conv.id,
            agent_id=agent_id,
            agent_subscription_id=sub.id,
            route_snapshot=route_snapshot,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id or sender_id,
            reply_text=reply_text,
            customer_message=content,
            thread_ts=thread_ts,
        )
        if approval_hold is not None:
            await runtime_persist_channel_hold_runtime_events(
                run_result.runtime_envelope,
            )
            return {
                "status": approval_hold.get("status", "approval_required"),
                "conversation_id": conv.id,
                "agent_id": agent_id,
                "reply_chars": len(reply_text),
                **approval_hold,
            }

        # Persist the outbound turn + ship the reply
        assistant_message_id: str | None = None
        async with async_session() as db:
            assistant_message = await add_channel_assistant_message(
                db,
                conversation_id=conv.id,
                channel_type=channel_type,
                chat_id=chat_id,
                content=reply_text,
                runtime_meta=run_result.runtime_meta,
            )
            await create_channel_outbound_log(
                db,
                entity_id=entity_id,
                channel_config_id=channel_config_id,
                conversation_id=conv.id,
                channel_type=channel_type,
                to_address=str(chat_id or sender_id),
                content=reply_text,
            )
            if inbound_message_log_id and uses_durable_inbound_receipt(channel_type):
                from packages.core.services.channel_inbound_receipts import (
                    set_inbound_dispatch_prepared_reply,
                )

                await set_inbound_dispatch_prepared_reply(
                    db,
                    message_log_id=inbound_message_log_id,
                    prepared_reply=reply_text,
                    channel_binding_id=binding.id,
                    channel_contact_id=contact.id,
                    agent_id=agent_id,
                    agent_subscription_id=sub.id,
                    route_snapshot=route_snapshot,
                    workspace_id=workspace_id,
                )
            assistant_message_id = assistant_message.id
            await db.commit()
        await runtime_persist_channel_runtime_events(
            run_result.runtime_envelope,
            message_id=assistant_message_id,
        )

        route_is_active = await channel_reply_route_is_active(**reply_route)
        if not route_is_active:
            return {
                "status": "skipped",
                "conversation_id": conv.id,
                "agent_id": agent_id,
                "reply_chars": len(reply_text),
                "sent": False,
                "reason": "reply_route_unavailable",
            }

        # Voice owns delivery on its Media Stream websocket. The Agent turn
        # and conversation history are still persisted above, but routing it
        # through the text adapter would create a deferred log and then mark
        # it sent even though no audio was delivered.
        sent = True
        if deliver_reply:
            sent = await send_channel_text_reply(
                cc_id=channel_config_id,
                channel_type=channel_type,
                chat_id=chat_id or sender_id,
                text=reply_text,
                thread_ts=thread_ts,
                reply_context=reply_context,
                idempotency_key=reply_idempotency_key,
                channel_binding_id=binding.id,
                require_active_binding=True,
            )

        return {
            "status": (
                "error"
                if uses_durable_inbound_receipt(channel_type) and not sent
                else "ok"
            ),
            "conversation_id": conv.id,
            "agent_id": agent_id,
            "agent_subscription_id": sub.id,
            "workspace_id": workspace_id,
            "reply": reply_text,
            "reply_chars": len(reply_text),
            "sent": sent,
            "reason": None if sent else "reply_not_sent",
        }
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception(
            "Channel gateway dispatch failed for %s/%s",
            channel_type, channel_config_id,
        )
        return {"status": "error"}


async def _send_inbound_action_ack(
    action: ChannelInboundAction,
    *,
    channel_config_id: str,
    channel_binding_id: str,
    channel_type: str,
    chat_id: str,
    thread_ts: str | None,
    reply_context: dict[str, str] | None,
    log_label: str,
    deliver_reply: bool = True,
) -> str | None:
    if not action.ack_message:
        return None
    if not deliver_reply:
        return action.ack_message

    try:
        await send_channel_text_reply(
            cc_id=channel_config_id,
            channel_type=channel_type,
            chat_id=chat_id,
            text=action.ack_message,
            thread_ts=thread_ts,
            reply_context=reply_context,
            channel_binding_id=channel_binding_id,
            require_active_binding=True,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception(
            "Gateway: %s ack send-back failed for %s chat=%s",
            log_label, channel_type, chat_id,
        )
    return action.ack_message

# Touch the Integration symbol so static checkers don't warn — it's
# imported here for future cross-linking but not yet used.
_ = Integration
