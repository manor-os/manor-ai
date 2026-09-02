"""Internal metadata helpers for durable inbound channel receipts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


DISPATCH_METADATA_KEY = "_manor_inbound_dispatch"
DISCORD_GATEWAY_SOURCE = "discord_gateway"
WEBHOOK_ROUTER_SOURCE = "webhook_router"
PREPARED_REPLY_RUNTIME_SOURCE = "channel_gateway"
DISPATCH_CLAIM_STALE_AFTER = timedelta(minutes=10)
DISPATCH_PUBLISH_STALE_AFTER = timedelta(minutes=15)
DISPATCH_RETRY_AFTER = timedelta(seconds=30)


class InboundDispatchClaimOutcome(str, Enum):
    """Durable webhook-to-broker handoff states."""

    ACQUIRED = "acquired"
    ALREADY_PUBLISHED = "already_published"
    PUBLISH_PENDING = "publish_pending"
    PROCESSED = "processed"
    FAILED = "failed"
    MISSING = "missing"


class InboundDispatchFailureOutcome(str, Enum):
    """Durable evidence produced while terminally failing a worker claim."""

    FAILED = "failed"
    PROCESSED = "processed"


@dataclass(frozen=True)
class InboundDispatchClaim:
    receipt: Any | None
    outcome: InboundDispatchClaimOutcome
    claim_id: str | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome is InboundDispatchClaimOutcome.ACQUIRED


def read_dispatch_metadata(attachments: object) -> dict[str, Any] | None:
    if not isinstance(attachments, dict):
        return None
    metadata = attachments.get(DISPATCH_METADATA_KEY)
    return dict(metadata) if isinstance(metadata, dict) else None


def write_dispatch_metadata(
    attachments: object,
    **updates: Any,
) -> dict[str, Any]:
    result = dict(attachments) if isinstance(attachments, dict) else {}
    metadata = read_dispatch_metadata(result) or {}
    for key, value in updates.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    result[DISPATCH_METADATA_KEY] = metadata
    return result


async def set_inbound_dispatch_prepared_reply(
    db: Any,
    *,
    message_log_id: str,
    prepared_reply: str,
    channel_binding_id: str | None = None,
    channel_contact_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    route_snapshot: dict[str, object] | None = None,
    workspace_id: str | None = None,
) -> bool:
    """Persist a prepared reply on a locked inbound channel receipt."""

    from sqlalchemy import select

    from packages.core.models.channel import MessageLog

    receipt = await db.scalar(
        select(MessageLog)
        .where(MessageLog.id == message_log_id)
        .with_for_update()
    )
    if receipt is None:
        return False
    receipt.attachments = write_dispatch_metadata(
        receipt.attachments,
        prepared_reply=prepared_reply,
        prepared_reply_source=PREPARED_REPLY_RUNTIME_SOURCE,
        channel_binding_id=channel_binding_id,
        channel_contact_id=channel_contact_id,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        route_snapshot=route_snapshot,
        resolved_route_version=(
            route_snapshot.get("version") if route_snapshot else 1
        ),
        workspace_id=workspace_id,
    )
    return True


def parse_dispatch_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dispatch_time_is_stale(
    value: object,
    *,
    now: datetime,
    after: timedelta,
) -> bool:
    timestamp = parse_dispatch_time(value)
    return timestamp is None or timestamp <= now - after


async def claim_inbound_dispatch(
    *,
    config_id: str,
    channel_type: str,
    external_id: str,
    now: datetime | None = None,
    session_factory: Any | None = None,
) -> InboundDispatchClaim:
    """Claim one persisted webhook receipt before publishing to Celery.

    A non-stale claim without ``published_at`` is deliberately reported as
    pending. The provider must retry instead of receiving a false-success ACK
    during the database-commit/broker-publish window.
    """

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import MessageLog

    current_time = now or datetime.now(timezone.utc)
    session_factory = session_factory or async_session
    async with session_factory() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(
                MessageLog.channel_config_id == config_id,
                MessageLog.direction == "inbound",
                MessageLog.channel_type == channel_type,
                MessageLog.external_id == external_id,
            )
            .with_for_update()
        )
        if receipt is None:
            return InboundDispatchClaim(None, InboundDispatchClaimOutcome.MISSING)
        if receipt.status == "processed":
            return InboundDispatchClaim(
                receipt,
                InboundDispatchClaimOutcome.PROCESSED,
            )
        if receipt.status == "failed":
            return InboundDispatchClaim(
                receipt,
                InboundDispatchClaimOutcome.FAILED,
            )

        metadata = read_dispatch_metadata(receipt.attachments)
        if metadata and metadata.get("source") == WEBHOOK_ROUTER_SOURCE:
            existing_claim_id = str(metadata.get("claim_id") or "").strip() or None
            if receipt.status == "processing":
                return InboundDispatchClaim(
                    receipt,
                    InboundDispatchClaimOutcome.ALREADY_PUBLISHED,
                    existing_claim_id,
                )
            if receipt.status == "queued" and metadata.get("published_at"):
                return InboundDispatchClaim(
                    receipt,
                    InboundDispatchClaimOutcome.ALREADY_PUBLISHED,
                    existing_claim_id,
                )
            if existing_claim_id and receipt.status == "queued":
                if not dispatch_time_is_stale(
                    metadata.get("claimed_at"),
                    now=current_time,
                    after=DISPATCH_PUBLISH_STALE_AFTER,
                ):
                    return InboundDispatchClaim(
                        receipt,
                        InboundDispatchClaimOutcome.PUBLISH_PENDING,
                        existing_claim_id,
                    )
        elif receipt.status in {"queued", "processing"}:
            # Legacy receipts may predate dispatch metadata. Their queued state
            # already represented a successful publish, so preserve that
            # interpretation during rolling upgrades.
            return InboundDispatchClaim(
                receipt,
                InboundDispatchClaimOutcome.ALREADY_PUBLISHED,
            )

        claim_id = generate_ulid()
        receipt.status = "queued"
        receipt.error_message = None
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            source=WEBHOOK_ROUTER_SOURCE,
            claim_id=claim_id,
            claimed_at=current_time.isoformat(),
            published_at=None,
        )
        await db.commit()
        return InboundDispatchClaim(
            receipt,
            InboundDispatchClaimOutcome.ACQUIRED,
            claim_id,
        )


async def mark_inbound_dispatch_published(
    *,
    message_log_id: str,
    claim_id: str,
    now: datetime | None = None,
    session_factory: Any | None = None,
) -> bool:
    """Fence and record a successful broker publish for one claim."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import MessageLog

    session_factory = session_factory or async_session
    async with session_factory() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == message_log_id)
            .with_for_update()
        )
        if receipt is not None and receipt.status == "processed":
            # The worker can finish between broker publish and this marker.
            # A terminal receipt is stronger evidence than ``published_at``.
            return True
        metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
        if (
            receipt is None
            or receipt.status not in {"queued", "processing"}
            or not metadata
            or metadata.get("source") != WEBHOOK_ROUTER_SOURCE
            or metadata.get("claim_id") != claim_id
        ):
            return False
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            published_at=(now or datetime.now(timezone.utc)).isoformat(),
        )
        await db.commit()
        return True


async def release_inbound_dispatch_claim(
    *,
    message_log_id: str,
    claim_id: str,
    session_factory: Any | None = None,
) -> bool:
    """Release only the matching unpublished claim after a broker failure."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import MessageLog

    session_factory = session_factory or async_session
    async with session_factory() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == message_log_id)
            .with_for_update()
        )
        metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
        if (
            receipt is None
            or receipt.status != "queued"
            or not metadata
            or metadata.get("source") != WEBHOOK_ROUTER_SOURCE
            or metadata.get("claim_id") != claim_id
        ):
            return False
        receipt.status = "received"
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            claim_id=None,
            claimed_at=None,
            published_at=None,
        )
        await db.commit()
        return True


async def mark_inbound_dispatch_failed(
    *,
    message_log_id: str,
    claim_id: str,
    error: str,
    session_factory: Any | None = None,
) -> InboundDispatchFailureOutcome | None:
    """Fence and terminally fail the currently owned worker claim."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.channel import MessageLog

    session_factory = session_factory or async_session
    async with session_factory() as db:
        receipt = await db.scalar(
            select(MessageLog)
            .where(MessageLog.id == message_log_id)
            .with_for_update()
        )
        metadata = read_dispatch_metadata(receipt.attachments) if receipt else None
        if receipt is not None and receipt.status == "processed":
            return InboundDispatchFailureOutcome.PROCESSED
        if (
            receipt is not None
            and receipt.status == "failed"
            and metadata
            and metadata.get("failed_claim_id") == claim_id
        ):
            return InboundDispatchFailureOutcome.FAILED
        queued_owned_claim = bool(
            receipt is not None
            and receipt.status == "queued"
            and metadata
            and metadata.get("claim_id") == claim_id
        )
        if (
            receipt is None
            or (receipt.status != "processing" and not queued_owned_claim)
            or not metadata
            or metadata.get("claim_id") != claim_id
        ):
            return None
        receipt.status = "failed"
        receipt.error_message = str(error or "channel dispatch failed")[:500]
        receipt.attachments = write_dispatch_metadata(
            receipt.attachments,
            claim_id=None,
            claimed_at=None,
            failed_claim_id=claim_id,
        )
        await db.commit()
        return InboundDispatchFailureOutcome.FAILED
