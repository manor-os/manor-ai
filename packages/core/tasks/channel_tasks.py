"""Channel gateway Celery tasks.

Channel routers (Telegram / WeChat / WhatsApp / Slack / …) enqueue an
inbound message via ``dispatch_inbound_task.delay(…)`` so the webhook
can ack in <100 ms while the LLM run happens on a worker.

Durability beyond ``FastAPI.BackgroundTasks``:
  - broker is Redis-backed (CELERY_BROKER_URL)
  - ``task_acks_late=True`` + ``task_reject_on_worker_lost=True`` mean a
    crashed worker hands the task back to the queue
  - retries with exponential backoff (30 s / 60 s / 120 s) for transient
    errors like an LLM 429 or provider API hiccup
  - ``task_soft_time_limit=120 s`` / hard 180 s caps a stuck run so the
    queue drains even when one message pathologically loops
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from celery.exceptions import SoftTimeLimitExceeded

from packages.core.celery_app import celery_app
from packages.core.constants.channels import uses_durable_inbound_receipt
from packages.core.tasks._runtime import run_in_worker as _run_async

logger = logging.getLogger(__name__)

_TEAMS_RENEWAL_WINDOW = timedelta(hours=12)
_CHANNEL_DISPATCH_AGENT_RETRY_LIMIT = 3

_DISCORD_TERMINAL_MESSAGES = {
    "unbound": "This Discord connection is not bound to a Manor Agent.",
    "error": "Manor could not complete this request. Please try again.",
    "timeout": "Manor took too long to respond. Please try again.",
}


async def _dispatch_slack_inbound_once(
    *,
    inbound_message_log_id: str,
    dispatch_claim_id: str | None = None,
    **dispatch_kwargs: Any,
) -> Dict[str, Any]:
    """Fence one physical worker around a durable logical claim."""
    from packages.core.constants.advisory_locks import AdvisoryLockNamespace
    from packages.core.database import async_session
    from packages.core.services.advisory_locks import TransactionAdvisoryLock

    async with async_session() as lock_db:
        lease = await TransactionAdvisoryLock.try_acquire(
            lock_db,
            namespace=int(AdvisoryLockNamespace.CHANNEL_INBOUND_DISPATCH),
            key=inbound_message_log_id,
        )
    if lease is None:
        return {"status": "duplicate_inflight"}
    try:
        return await _dispatch_slack_inbound_with_lease(
            inbound_message_log_id=inbound_message_log_id,
            dispatch_claim_id=dispatch_claim_id,
            **dispatch_kwargs,
        )
    finally:
        await lease.release()


async def _dispatch_slack_inbound_with_lease(
    *,
    inbound_message_log_id: str,
    dispatch_claim_id: str | None = None,
    **dispatch_kwargs: Any,
) -> Dict[str, Any]:
    """Claim one durable inbound receipt across Celery redelivery."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import MessageLog
    from packages.core.services.channel_inbound_receipts import (
        DISPATCH_CLAIM_STALE_AFTER,
        PREPARED_REPLY_RUNTIME_SOURCE,
        WEBHOOK_ROUTER_SOURCE,
        dispatch_time_is_stale,
        read_dispatch_metadata,
        write_dispatch_metadata,
    )
    from packages.core.services.channel_gateway import dispatch_inbound

    dispatch_claimed = False
    async with async_session() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == inbound_message_log_id)
            .with_for_update()
        )
        if receipt is None:
            # The broker may deliver before the HTTP transaction commits.
            return {"status": "retry", "reason": "receipt_not_visible"}
        metadata = read_dispatch_metadata(receipt.attachments)
        if (
            receipt.entity_id != dispatch_kwargs["entity_id"]
            or receipt.channel_type != dispatch_kwargs["channel_type"]
            or receipt.direction != "inbound"
            or receipt.channel_config_id != dispatch_kwargs["channel_config_id"]
        ):
            if not (
                dispatch_claim_id
                and metadata
                and metadata.get("claim_id") == dispatch_claim_id
            ):
                return {"status": "skipped", "reason": "stale_dispatch_claim"}
            receipt.status = "failed"
            receipt.error_message = "Inbound dispatch task does not match receipt scope"
            receipt.attachments = write_dispatch_metadata(
                receipt.attachments,
                claim_id=None,
                claimed_at=None,
                failed_claim_id=dispatch_claim_id,
            )
            await db.commit()
            return {"status": "skipped", "reason": "receipt_mismatch"}
        if receipt.status == "processed":
            return {"status": "duplicate_event"}
        if not dispatch_claim_id:
            return {"status": "retry", "reason": "dispatch_claim_missing"}

        is_webhook_router_claim = bool(
            metadata and metadata.get("source") == WEBHOOK_ROUTER_SOURCE
        )
        if (
            is_webhook_router_claim
            and metadata
            and metadata.get("claim_id") != dispatch_claim_id
        ):
            return {"status": "skipped", "reason": "stale_dispatch_claim"}
        now = datetime.now(timezone.utc)
        if receipt.status == "processing":
            owns_existing_claim = bool(
                metadata and metadata.get("claim_id") == dispatch_claim_id
            )
            stale_claim = dispatch_time_is_stale(
                (metadata or {}).get("claimed_at"),
                now=now,
                after=DISPATCH_CLAIM_STALE_AFTER,
            )
            if not stale_claim and not owns_existing_claim:
                return {"status": "duplicate_inflight"}
        elif receipt.status not in {"received", "queued"}:
            return {"status": "skipped", "reason": "receipt_not_dispatchable"}
        receipt.status = "processing"
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            claim_id=dispatch_claim_id,
            claimed_at=now.isoformat(),
        )
        await db.commit()
        dispatch_claimed = True

    try:
        prepared_reply = (
            metadata.get("prepared_reply")
            if metadata
            and metadata.get("prepared_reply_source") == PREPARED_REPLY_RUNTIME_SOURCE
            else None
        )
        if isinstance(prepared_reply, str) and prepared_reply:
            from packages.core.services.channel_outbound_delivery import (
                channel_reply_route_is_active,
                send_channel_text_reply,
            )

            route_is_active = await channel_reply_route_is_active(
                entity_id=dispatch_kwargs["entity_id"],
                cc_id=dispatch_kwargs["channel_config_id"],
                channel_type=dispatch_kwargs["channel_type"],
                channel_binding_id=str(metadata.get("channel_binding_id") or "") or None,
                channel_contact_id=str(metadata.get("channel_contact_id") or "") or None,
                agent_id=str(metadata.get("agent_id") or "") or None,
                agent_subscription_id=(
                    str(metadata.get("agent_subscription_id") or "") or None
                ),
                route_snapshot=metadata.get("route_snapshot"),
                workspace_id=str(metadata.get("workspace_id") or "") or None,
            )
            if not route_is_active:
                result = {
                    "status": "skipped",
                    "reason": "reply_route_unavailable",
                }
            else:
                send_kwargs = {
                    "cc_id": dispatch_kwargs["channel_config_id"],
                    "channel_type": dispatch_kwargs["channel_type"],
                    "chat_id": dispatch_kwargs["chat_id"] or dispatch_kwargs["sender_id"],
                    "text": prepared_reply,
                    "idempotency_key": inbound_message_log_id,
                    "channel_binding_id": (
                        str(metadata.get("channel_binding_id") or "") or None
                    ),
                    "require_active_binding": True,
                }
                if dispatch_kwargs.get("thread_ts"):
                    send_kwargs["thread_ts"] = dispatch_kwargs["thread_ts"]
                if dispatch_kwargs.get("reply_context"):
                    send_kwargs["reply_context"] = dispatch_kwargs["reply_context"]
                sent = await send_channel_text_reply(**send_kwargs)
                result = {
                    "status": "ok" if sent else "error",
                    "sent": sent,
                }
        else:
            gateway_kwargs = dict(dispatch_kwargs)
            gateway_kwargs["inbound_message_log_id"] = inbound_message_log_id
            result = await dispatch_inbound(**gateway_kwargs)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        if dispatch_claimed:
            await _release_inbound_dispatch_claim(
                inbound_message_log_id=inbound_message_log_id,
                dispatch_claim_id=dispatch_claim_id,
                preserve_claim=True,
            )
        raise
    if result.get("status") in {"error", "retry"}:
        if dispatch_claimed:
            await _release_inbound_dispatch_claim(
                inbound_message_log_id=inbound_message_log_id,
                dispatch_claim_id=dispatch_claim_id,
                preserve_claim=True,
            )
        return result

    async with async_session() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == inbound_message_log_id)
            .with_for_update()
        )
        metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
        owns_claim = bool(
            metadata and metadata.get("claim_id") == dispatch_claim_id
        )
        if receipt is not None and receipt.status != "processed" and owns_claim:
            receipt.status = "processed"
            receipt.attachments = write_dispatch_metadata(
                receipt.attachments,
                claim_id=None,
                claimed_at=None,
            )
            await db.commit()
    return result


async def _release_inbound_dispatch_claim(
    *,
    inbound_message_log_id: str,
    dispatch_claim_id: str | None,
    preserve_claim: bool = False,
) -> None:
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import MessageLog
    from packages.core.services.channel_inbound_receipts import (
        read_dispatch_metadata,
        write_dispatch_metadata,
    )

    async with async_session() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == inbound_message_log_id)
            .with_for_update()
        )
        metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
        if (
            receipt is None
            or receipt.status != "processing"
            or not metadata
            or metadata.get("claim_id") != dispatch_claim_id
        ):
            return
        receipt.status = "queued"
        if preserve_claim:
            receipt.attachments = write_dispatch_metadata(
                receipt.attachments,
                claimed_at=None,
            )
        else:
            receipt.attachments = write_dispatch_metadata(
                receipt.attachments,
                claim_id=None,
                claimed_at=None,
            )
        await db.commit()


async def _mark_inbound_dispatch_failed(
    *,
    inbound_message_log_id: str,
    dispatch_claim_id: str,
    error: str,
) -> Any:
    from packages.core.services.channel_inbound_receipts import (
        mark_inbound_dispatch_failed,
    )

    return await mark_inbound_dispatch_failed(
        message_log_id=inbound_message_log_id,
        claim_id=dispatch_claim_id,
        error=error,
    )


async def _send_discord_terminal_reply(
    *,
    channel_config_id: str,
    chat_id: str,
    reply_context: dict[str, str],
    status: str,
) -> None:
    message = _DISCORD_TERMINAL_MESSAGES.get(status)
    if not message:
        return
    from packages.core.services.channel_outbound_delivery import (
        send_channel_text_reply,
    )

    await send_channel_text_reply(
        cc_id=channel_config_id,
        channel_type="discord",
        chat_id=chat_id,
        text=message,
        reply_context=reply_context,
        require_active_binding=status != "unbound",
    )


@celery_app.task(
    name="channel.dispatch_inbound",
    bind=True,
    max_retries=None,
    soft_time_limit=120,
    time_limit=180,
)
def dispatch_inbound_task(
    self,
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    sender_id: str,
    sender_name: Optional[str] = None,
    chat_id: Optional[str] = None,
    content: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
    thread_ts: Optional[str] = None,
    inbound_message_log_id: Optional[str] = None,
    dispatch_claim_id: Optional[str] = None,
    reply_context: Optional[Dict[str, str]] = None,
    terminal_failure_error: Optional[str] = None,
    terminal_failure_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Celery wrapper around ``channel_gateway.dispatch_inbound``.

    The gateway function itself is queue-agnostic (no FastAPI / Celery
    types) — this wrapper just adapts it to Celery's sync worker model.
    """
    logger.info(
        "Channel dispatch: %s/%s sender=%s (attempt %d)",
        channel_type, channel_config_id, sender_id, self.request.retries + 1,
    )
    request_id = str(getattr(self.request, "id", "") or "")
    effective_claim_id = dispatch_claim_id or request_id or None
    durable_receipt = bool(
        uses_durable_inbound_receipt(channel_type)
        and inbound_message_log_id
        and effective_claim_id
    )

    def persist_terminal_failure(error: str, status: str) -> str:
        """Do not let Celery ACK until the owned receipt is terminal."""

        if not durable_receipt:
            raise RuntimeError("durable inbound receipt is unavailable")
        try:
            outcome = _run_async(_mark_inbound_dispatch_failed(
                inbound_message_log_id=inbound_message_log_id,
                dispatch_claim_id=effective_claim_id,
                error=error,
            ))
            if outcome is None:
                raise RuntimeError(
                    "durable inbound receipt rejected the terminal transition"
                )
            if outcome is True:
                return "failed"
            return str(getattr(outcome, "value", outcome))
        except Exception as persistence_exc:
            logger.exception(
                "Could not durably terminate channel receipt %s; retrying persistence",
                inbound_message_log_id,
            )
            retry_kwargs = {
                "entity_id": entity_id,
                "channel_config_id": channel_config_id,
                "channel_type": channel_type,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "chat_id": chat_id,
                "content": content,
                "attachments": attachments,
                "thread_ts": thread_ts,
                "inbound_message_log_id": inbound_message_log_id,
                "dispatch_claim_id": effective_claim_id,
                "reply_context": reply_context,
                "terminal_failure_error": error,
                "terminal_failure_status": status,
            }
            raise self.retry(
                exc=persistence_exc,
                countdown=30,
                kwargs=retry_kwargs,
            )

    if terminal_failure_error:
        final_status = terminal_failure_status or "error"
        outcome = persist_terminal_failure(terminal_failure_error, final_status)
        if outcome == "failed" and channel_type == "discord" and reply_context:
            _run_async(_send_discord_terminal_reply(
                channel_config_id=channel_config_id,
                chat_id=chat_id or sender_id,
                reply_context=reply_context,
                status=final_status,
            ))
        return {"status": final_status}

    try:
        dispatch_kwargs = dict(
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            channel_type=channel_type,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            content=content,
            attachments=attachments,
            thread_ts=thread_ts,
            reply_context=reply_context,
        )
        if (
            uses_durable_inbound_receipt(channel_type)
            or str(channel_type or "").strip() == "wechat"
        ) and inbound_message_log_id:
            dispatch_kwargs["reply_idempotency_key"] = inbound_message_log_id
            result = _run_async(_dispatch_slack_inbound_once(
                inbound_message_log_id=inbound_message_log_id,
                dispatch_claim_id=effective_claim_id,
                **dispatch_kwargs,
            ))
        else:
            from packages.core.services.channel_gateway import dispatch_inbound
            result = _run_async(dispatch_inbound(**dispatch_kwargs))
        result_status = str((result or {}).get("status") or "")
        if channel_type == "discord" and reply_context and result_status == "unbound":
            _run_async(_send_discord_terminal_reply(
                channel_config_id=channel_config_id,
                chat_id=chat_id or sender_id,
                reply_context=reply_context,
                status="unbound",
            ))
        if result.get("status") in {"error", "retry"}:
            raise RuntimeError(result.get("reason") or result["status"])
    except SoftTimeLimitExceeded as exc:
        from packages.core.services.channel_outbound_delivery import (
            ApprovedExternalReplyRetryableTimeout,
        )

        if (
            isinstance(exc, ApprovedExternalReplyRetryableTimeout)
            and self.request.retries < _CHANNEL_DISPATCH_AGENT_RETRY_LIMIT
        ):
            countdown = 30 * (2 ** self.request.retries)
            logger.warning(
                "Approved channel reply timed out — retrying the same provider "
                "key in %ds (attempt %d)",
                countdown,
                self.request.retries + 1,
            )
            raise self.retry(exc=exc, countdown=countdown)
        logger.error(
            "Channel dispatch exceeded soft time limit: %s/%s sender=%s",
            channel_type, channel_config_id, sender_id,
        )
        if durable_receipt:
            outcome = persist_terminal_failure(
                "channel dispatch soft time limit exceeded",
                "timeout",
            )
        else:
            outcome = "failed"
        if (
            outcome == "failed"
            and channel_type == "discord"
            and reply_context
        ):
            _run_async(_send_discord_terminal_reply(
                channel_config_id=channel_config_id,
                chat_id=chat_id or sender_id,
                reply_context=reply_context,
                status="timeout",
            ))
        return {"status": "timeout"}
    except Exception as exc:
        # 30 s, 60 s, 120 s backoff
        countdown = 30 * (2 ** self.request.retries)
        terminal_retry = (
            self.request.retries >= _CHANNEL_DISPATCH_AGENT_RETRY_LIMIT
        )
        if terminal_retry and durable_receipt:
            outcome = persist_terminal_failure(
                str(exc) or "channel dispatch failed",
                "error",
            )
            if outcome == "failed" and channel_type == "discord" and reply_context:
                _run_async(_send_discord_terminal_reply(
                    channel_config_id=channel_config_id,
                    chat_id=chat_id or sender_id,
                    reply_context=reply_context,
                    status="error",
                ))
            return {"status": "error", "reason": str(exc) or "channel dispatch failed"}
        if terminal_retry:
            raise
        logger.warning(
            "Channel dispatch %s/%s failed — retrying in %ds (attempt %d): %s",
            channel_type, channel_config_id, countdown,
            self.request.retries + 1, exc,
        )
        raise self.retry(exc=exc, countdown=countdown)

    logger.info(
        "Channel dispatch done: %s/%s status=%s",
        channel_type, channel_config_id, (result or {}).get("status"),
    )
    return result or {"status": "unknown"}


@celery_app.task(
    name="channel.ms_teams_subscription_tick",
    soft_time_limit=120,
    time_limit=180,
)
def ms_teams_subscription_tick() -> Dict[str, int]:
    """Renew Microsoft Graph subscriptions that expire within 12 hours.

    Each ChannelConfig is handled independently so one revoked or stale
    connection cannot prevent other Teams accounts from being renewed.
    """
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import ChannelConfig
    from packages.core.services.channels.ms_teams_adapter import TeamsChannelAdapter

    async def _run() -> Dict[str, int]:
        now = datetime.now(timezone.utc)
        checked = renewed = failed = skipped = 0
        async with async_session() as db:
            config_ids = list((await db.execute(
                select(ChannelConfig.id).where(
                    ChannelConfig.channel_type == "ms_teams",
                    ChannelConfig.status == "active",
                )
            )).scalars().all())

            adapter = TeamsChannelAdapter()
            for config_id in config_ids:
                cc = await db.get(ChannelConfig, config_id)
                if cc is None:
                    continue
                config = dict(cc.config or {})
                if not str(config.get("teams_subscription_id") or "").strip():
                    if not bool(config.get("teams_registration_pending")):
                        skipped += 1
                        continue

                expiration_raw = str(
                    config.get("teams_subscription_expiration") or ""
                ).strip()
                if expiration_raw:
                    try:
                        expiration = datetime.fromisoformat(
                            expiration_raw.replace("Z", "+00:00")
                        )
                        if expiration.tzinfo is None:
                            expiration = expiration.replace(tzinfo=timezone.utc)
                    except ValueError:
                        logger.warning(
                            "Invalid Teams subscription expiration for config=%s; renewing",
                            cc.id,
                        )
                    else:
                        if expiration > now + _TEAMS_RENEWAL_WINDOW:
                            skipped += 1
                            continue

                checked += 1
                try:
                    result = await adapter.register_webhook(cc)
                    if result.get("registered"):
                        await db.commit()
                        renewed += 1
                    else:
                        await db.rollback()
                        failed += 1
                        logger.warning(
                            "Teams subscription renewal was not confirmed for config=%s: %s",
                            cc.id,
                            result,
                        )
                except Exception:
                    await db.rollback()
                    failed += 1
                    logger.exception(
                        "Teams subscription renewal failed for config=%s",
                        cc.id,
                    )

        return {
            "checked": checked,
            "renewed": renewed,
            "failed": failed,
            "skipped": skipped,
        }

    return _run_async(_run())


@celery_app.task(
    name="channel.outlook_subscription_tick",
    soft_time_limit=120,
    time_limit=180,
)
def outlook_subscription_tick() -> Dict[str, int]:
    """Renew Outlook Graph mail subscriptions before they expire."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import ChannelConfig
    from packages.core.services.channels.outlook_adapter import OutlookChannelAdapter

    async def _run() -> Dict[str, int]:
        now = datetime.now(timezone.utc)
        checked = renewed = failed = skipped = 0
        async with async_session() as db:
            config_ids = list((await db.execute(
                select(ChannelConfig.id).where(
                    ChannelConfig.channel_type == "outlook",
                    ChannelConfig.status == "active",
                )
            )).scalars().all())
            adapter = OutlookChannelAdapter()
            for config_id in config_ids:
                cc = await db.get(ChannelConfig, config_id)
                if cc is None:
                    continue
                config = dict(cc.config or {})
                if not str(config.get("outlook_subscription_id") or "").strip():
                    if not bool(config.get("outlook_registration_pending")):
                        skipped += 1
                        continue
                expiration_raw = str(
                    config.get("outlook_subscription_expiration") or ""
                ).strip()
                if expiration_raw:
                    try:
                        expiration = datetime.fromisoformat(
                            expiration_raw.replace("Z", "+00:00")
                        )
                        if expiration.tzinfo is None:
                            expiration = expiration.replace(tzinfo=timezone.utc)
                    except ValueError:
                        logger.warning(
                            "Invalid Outlook subscription expiration for config=%s; renewing",
                            cc.id,
                        )
                    else:
                        if expiration > now + _TEAMS_RENEWAL_WINDOW:
                            skipped += 1
                            continue
                checked += 1
                try:
                    result = await adapter.register_webhook(cc)
                    if result.get("registered"):
                        await db.commit()
                        renewed += 1
                    else:
                        await db.rollback()
                        failed += 1
                        logger.warning(
                            "Outlook subscription renewal was not confirmed for config=%s: %s",
                            cc.id,
                            result,
                        )
                except Exception:
                    await db.rollback()
                    failed += 1
                    logger.exception(
                        "Outlook subscription renewal failed for config=%s",
                        cc.id,
                    )
        return {
            "checked": checked,
            "renewed": renewed,
            "failed": failed,
            "skipped": skipped,
        }

    return _run_async(_run())


@celery_app.task(
    name="channel.register_integration_webhooks",
    soft_time_limit=120,
    time_limit=180,
    bind=True,
    max_retries=3,
)
def register_integration_webhooks_task(
    self,
    *,
    entity_id: str,
    integration_id: str,
) -> Dict[str, Any]:
    """Register channel webhooks after an integration sync commits.

    Provider calls run in a worker with a fresh DB session, keeping Nango's
    user-facing sync response independent of Graph availability.
    """
    from packages.core.database import async_session
    from apps.api.routers.integrations import _register_integration_channel_webhooks

    async def _run() -> Dict[str, Any]:
        async with async_session() as db:
            failures = await _register_integration_channel_webhooks(
                db,
                entity_id=entity_id,
                integration_id=integration_id,
            )
        if failures:
            raise RuntimeError(
                "Channel webhook registration pending for configs: "
                + ", ".join(failures)
            )
        return {"ok": True, "entity_id": entity_id, "integration_id": integration_id}

    try:
        return _run_async(_run())
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "Integration channel webhook registration exhausted retries "
                "for integration=%s: %s",
                integration_id,
                exc,
            )
            raise
        countdown = 30 * (2 ** self.request.retries)
        logger.warning(
            "Integration channel webhook registration failed for integration=%s; "
            "retrying in %ss (attempt %s): %s",
            integration_id,
            countdown,
            self.request.retries + 1,
            exc,
        )
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(
    name="channel.disconnect_whatsapp_business",
    soft_time_limit=120,
    time_limit=180,
    bind=True,
    max_retries=3,
)
def disconnect_whatsapp_business_task(
    self,
    *,
    entity_id: str,
    integration_id: str,
) -> Dict[str, Any]:
    """Retry fail-closed WhatsApp offboarding without reopening its route."""
    from apps.api.routers.integrations import (
        WhatsAppDisconnectPending,
        _disconnect_whatsapp_integration_once,
    )
    from packages.core.database import async_session

    async def _run() -> Dict[str, Any]:
        async with async_session() as db:
            await _disconnect_whatsapp_integration_once(
                db,
                entity_id=entity_id,
                integration_id=integration_id,
            )
        return {"ok": True, "entity_id": entity_id, "integration_id": integration_id}

    try:
        return _run_async(_run())
    except WhatsAppDisconnectPending:
        error = RuntimeError("WhatsApp provider cleanup is pending")
        if self.request.retries >= self.max_retries:
            logger.error(
                "WhatsApp disconnect exhausted retries for integration=%s",
                integration_id,
            )
            raise error from None
        countdown = 30 * (2 ** self.request.retries)
        logger.warning(
            "WhatsApp disconnect retry scheduled for integration=%s in %ss",
            integration_id,
            countdown,
        )
        raise self.retry(exc=error, countdown=countdown)


@celery_app.task(
    name="channel.retire_nango_connection",
    soft_time_limit=60,
    time_limit=120,
    bind=True,
    max_retries=3,
)
def retire_nango_connection_task(
    self,
    *,
    entity_id: str,
    integration_id: str,
) -> Dict[str, Any]:
    """Retire the old delegated token after a successful account swap."""
    from sqlalchemy import select

    from apps.api.routers.integrations import _clear_whatsapp_retirement_state
    from packages.core.ai.mcp.nango import get_nango_secret
    from packages.core.database import async_session
    from packages.core.models.document import Integration
    from packages.core.services.whatsapp_business_provisioning import (
        delete_nango_connection,
    )

    async def _run() -> Dict[str, Any]:
        async with async_session() as db:
            integration = (await db.execute(
                select(Integration).where(
                    Integration.id == integration_id,
                    Integration.entity_id == entity_id,
                ).with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if integration is None:
                return {"ok": True, "already_complete": True}
            config = dict(integration.config or {})
            retirement = config.get("whatsapp_retirement")
            if not isinstance(retirement, dict):
                return {"ok": True, "already_complete": True}
            connection_id = str(retirement.get("connection_id") or "").strip()
            provider_config_key = str(
                retirement.get("provider_config_key") or "whatsapp"
            ).strip()
            if not connection_id or not provider_config_key:
                raise RuntimeError("WhatsApp retirement metadata is incomplete")
            config["whatsapp_retirement"] = {
                **retirement,
                "status": "retiring",
                "attempt_count": int(retirement.get("attempt_count") or 0) + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            integration.config = config
            await db.commit()
            nango_secret = await get_nango_secret(db, entity_id)
            if not nango_secret:
                raise RuntimeError("Nango is not configured")
            await db.commit()
            await delete_nango_connection(
                nango_secret=nango_secret,
                provider_config_key=provider_config_key,
                connection_id=connection_id,
            )
            await _clear_whatsapp_retirement_state(
                db,
                entity_id=entity_id,
                integration_id=integration_id,
                connection_id=connection_id,
            )
            await db.commit()
            return {"ok": True, "entity_id": entity_id, "integration_id": integration_id}

    try:
        return _run_async(_run())
    except Exception:
        error = RuntimeError("Nango connection retirement is pending")
        if self.request.retries >= self.max_retries:
            logger.error(
                "Nango retirement exhausted retries for integration=%s",
                integration_id,
            )
            raise error from None
        countdown = 30 * (2 ** self.request.retries)
        logger.warning(
            "Nango retirement retry scheduled for integration=%s in %ss",
            integration_id,
            countdown,
        )
        raise self.retry(exc=error, countdown=countdown)


@celery_app.task(
    name="notification.dispatch_due",
    soft_time_limit=60,
    time_limit=120,
)
def dispatch_due_notifications_task() -> Dict[str, int]:
    """Periodic sweeper for scheduled notifications.

    Wakes up on the Celery beat cadence (configured in ``celery_app``)
    and runs ``notification_scheduler.dispatch_due_notifications``. Due or
    stale outbox leases are claimed from PostgreSQL and dispatched without
    creating another parent Notification row.
    """
    from packages.core.database import async_session
    from packages.core.services.notification_scheduler import (
        dispatch_due_notifications,
    )

    async def _run() -> Dict[str, int]:
        async with async_session() as db:
            return await dispatch_due_notifications(db)

    return _run_async(_run())


@celery_app.task(
    name="integrations.health_check",
    bind=True, max_retries=1, soft_time_limit=30, time_limit=60,
)
def health_check_task(
    self,
    *,
    integration_id: str | None = None,
    oauth_account_id: str | None = None,
) -> Dict[str, Any]:
    """Background runner for a single provider health probe. Fired
    after a save (from the API) and from the daily sweep.
    """
    from packages.core.database import async_session
    from packages.core.services.integration_health import (
        run_and_persist_integration, run_and_persist_oauth,
    )

    async def _run() -> Dict[str, Any]:
        async with async_session() as db:
            if integration_id:
                result = await run_and_persist_integration(db, integration_id)
            elif oauth_account_id:
                result = await run_and_persist_oauth(db, oauth_account_id)
            else:
                return {"ok": False, "detail": "no id provided"}
            await db.commit()
            return result

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.warning("Health check task failed: %s", exc)
        return {"ok": False, "detail": str(exc)}


@celery_app.task(name="integrations.health_tick")
def health_tick() -> Dict[str, Any]:
    """Daily sweep — test every active Integration + every OAuthAccount.

    Small batches (~20 providers × N entities) so we just run serially;
    can shard later if needed.
    """
    from sqlalchemy import select
    from packages.core.database import async_session
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount
    from packages.core.services.integration_health import (
        run_and_persist_integration, run_and_persist_oauth,
    )

    async def _run() -> Dict[str, Any]:
        total = 0
        failed = 0
        async with async_session() as db:
            integrations = (await db.execute(
                select(Integration).where(Integration.status == "active")
            )).scalars().all()
            for row in integrations:
                total += 1
                result = await run_and_persist_integration(db, row.id)
                if not result.get("ok"):
                    failed += 1

            oauth_rows = (await db.execute(
                select(OAuthAccount)
            )).scalars().all()
            for row in oauth_rows:
                total += 1
                result = await run_and_persist_oauth(db, row.id)
                if not result.get("ok"):
                    failed += 1

            await db.commit()
        logger.info("Health tick: %d checked, %d failed", total, failed)
        return {"checked": total, "failed": failed}

    return _run_async(_run())
