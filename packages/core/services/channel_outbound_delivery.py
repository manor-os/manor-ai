from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from typing import Any, NotRequired, TypedDict

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.advisory_locks import AdvisoryLockNamespace
from packages.core.constants.channels import ExternalMessageActionKey
from packages.core.database import async_session
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact, MessageLog
from packages.core.models.document import Channel, Integration
from packages.core.models.task import Conversation, Message
from packages.core.models.user import OAuthAccount
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services import channels as _channels_pkg  # noqa: F401
from packages.core.services.agent_subscription_service import resolve_subscription
from packages.core.services.advisory_locks import TransactionAdvisoryLock
from packages.core.services.channel_bindings import (
    channel_workspace_is_routable,
    load_channel_binding_for_config,
    load_channel_config,
    load_slack_channel_bindings_for_config,
)
from packages.core.services.channel_conversations import add_channel_assistant_message
from packages.core.services.channel_credentials import (
    channel_credential_source_is_available,
)
from packages.core.services.channel_message_logs import (
    create_channel_outbound_log,
    mark_last_channel_outbound_failed,
    mark_last_channel_outbound_sent,
    normalize_channel_outbound_status,
)
from packages.core.services.integration_access import (
    lock_connection_owner_authorization,
    resolve_integration_access,
)
from packages.core.services.channels import (
    ADAPTERS,
    ChannelTextSendError,
    ChannelTextSendFailureDisposition,
    ChannelTextSendResultStatus,
    ChannelTextSendRetryMode,
)

logger = logging.getLogger(__name__)


class ApprovedExternalReplyDeliveryError(RuntimeError):
    """Retryable provider failure; the approval transaction must roll back."""

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ApprovedExternalReplySameKeyRetryRequired(RuntimeError):
    """Ambiguous provider-idempotent attempt that may only retry the same key."""


class ApprovedExternalReplyOutcomeUnknownError(RuntimeError):
    """Ambiguous provider outcome whose durable quarantine could not be written."""


class ApprovedExternalReplyRetryableTimeout(SoftTimeLimitExceeded):
    """Provider-idempotent timeout that must retry the same approved reply key."""


class ApprovedExternalReplyDeliveryDisposition(str, Enum):
    """Whether a delivery result is valid terminal approval evidence."""

    ACCEPTED = "accepted"
    TERMINAL_ROUTE_INVALID = "terminal_route_invalid"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RETRYABLE_FAILURE = "retryable_failure"


class ApprovedExternalReplyAdapterDisposition(str, Enum):
    """Strict meaning of one adapter return on the approved-send path."""

    ACCEPTED = "accepted"
    DETERMINATE_FAILURE = "determinate_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ApprovedExternalReplyDeliveryResult(TypedDict):
    """Stable result shape consumed by approval resolution entry points."""

    disposition: str
    ok: bool
    sent: bool
    error: NotRequired[str]
    reason: NotRequired[str]
    message_log_id: NotRequired[str]
    external_id: NotRequired[str | None]
    replayed: NotRequired[bool]
    quarantine_persisted: NotRequired[bool]
    quarantine_source: NotRequired[str]
    adapter_result: NotRequired[dict[str, Any]]
    transcript_projected: NotRequired[bool]


_TERMINAL_ROUTE_ERRORS = frozenset({
    "invalid_reply_route",
    "reply_route_unavailable",
})


def _approved_external_reply_delivery_result(
    disposition: ApprovedExternalReplyDeliveryDisposition,
    *,
    error: str | None = None,
    reason: str | None = None,
    **evidence: Any,
) -> ApprovedExternalReplyDeliveryResult:
    """Build the only result shape trusted to consume an approval."""

    reserved = {"disposition", "ok", "sent"}.intersection(evidence)
    if reserved:
        raise ValueError(
            "approved delivery evidence cannot override reserved result fields"
        )
    accepted = disposition is ApprovedExternalReplyDeliveryDisposition.ACCEPTED
    result: ApprovedExternalReplyDeliveryResult = {
        "disposition": disposition.value,
        "ok": accepted,
        "sent": accepted,
    }
    if error:
        result["error"] = error
    if reason:
        result["reason"] = reason
    result.update(evidence)
    return result


def _approved_external_reply_adapter_disposition(
    result: object,
) -> tuple[ApprovedExternalReplyAdapterDisposition, str, str]:
    """Classify only explicit adapter statuses; unknown shapes fail closed."""

    if not isinstance(result, dict):
        return (
            ApprovedExternalReplyAdapterDisposition.OUTCOME_UNKNOWN,
            ApprovedReplyAttemptStatus.UNKNOWN.value,
            "adapter returned a non-object provider result",
        )
    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in {
        ChannelTextSendResultStatus.SENT.value,
        ChannelTextSendResultStatus.DELIVERED.value,
        ChannelTextSendResultStatus.QUEUED.value,
    }:
        durable_status = (
            ApprovedReplyAttemptStatus.SENT.value
            if raw_status == ChannelTextSendResultStatus.QUEUED.value
            else raw_status
        )
        return (
            ApprovedExternalReplyAdapterDisposition.ACCEPTED,
            durable_status,
            "",
        )
    error = str(
        result.get("error")
        or result.get("reason")
        or (
            "adapter deferred the provider send"
            if raw_status == "deferred"
            else "adapter did not report an explicit provider outcome"
        )
    )
    if raw_status in {
        ChannelTextSendResultStatus.FAILED.value,
        ChannelTextSendResultStatus.DEFERRED.value,
    }:
        return (
            ApprovedExternalReplyAdapterDisposition.DETERMINATE_FAILURE,
            ApprovedReplyAttemptStatus.FAILED.value,
            error,
        )
    return (
        ApprovedExternalReplyAdapterDisposition.OUTCOME_UNKNOWN,
        ApprovedReplyAttemptStatus.UNKNOWN.value,
        error,
    )


def approved_external_reply_delivery_disposition(
    result: dict[str, Any],
) -> ApprovedExternalReplyDeliveryDisposition:
    """Classify delivery evidence, failing closed on unknown result shapes."""

    try:
        disposition = ApprovedExternalReplyDeliveryDisposition(
            result.get("disposition")
        )
    except (TypeError, ValueError):
        return ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
    reason = str(result.get("reason") or result.get("error") or "").strip()
    message_log_id = str(result.get("message_log_id") or "").strip()
    if disposition is ApprovedExternalReplyDeliveryDisposition.ACCEPTED:
        if (
            result.get("ok") is True
            and result.get("sent") is True
            and message_log_id
        ):
            return disposition
    elif disposition is ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN:
        if (
            result.get("sent") is False
            and reason == "provider_outcome_unknown"
            and message_log_id
            and (
                result.get("replayed") is True
                or result.get("quarantine_persisted") is True
            )
        ):
            return disposition
    elif disposition is ApprovedExternalReplyDeliveryDisposition.TERMINAL_ROUTE_INVALID:
        if result.get("sent") is False and reason in _TERMINAL_ROUTE_ERRORS:
            return disposition
    return ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE


def approved_external_reply_outcome_message(result: dict[str, Any]) -> str:
    """Return honest operator copy for one terminal delivery projection."""

    disposition = approved_external_reply_delivery_disposition(result)
    if disposition is ApprovedExternalReplyDeliveryDisposition.ACCEPTED:
        return "Approved — the external reply was sent."
    reason = str(result.get("reason") or result.get("error") or "unknown")
    if disposition is ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN:
        return (
            "Approved — delivery outcome is unknown and requires manual "
            "reconciliation; it will not retry automatically."
        )
    return f"Approved — the external reply was not sent: {reason}."


class ChannelReplyRouteVersion(IntEnum):
    """Schema versions for durable, exact channel reply routes."""

    EXACT_SCOPE_V1 = 1


class ExternalReplyApprovalClaimStatus(str, Enum):
    """Transient state that serializes approval decisions around provider I/O."""

    DELIVERY_IN_PROGRESS = "external_delivery_in_progress"
    DELIVERY_RETRY_REQUIRED = "external_delivery_retry_required"
    DELIVERY_OUTCOME_UNKNOWN = "external_delivery_outcome_unknown"


class ExternalReplyApprovalClaimDisposition(str, Enum):
    """Decision-surface meaning of the currently persisted approval claim."""

    AVAILABLE = "available"
    IN_PROGRESS = "approval_delivery_in_progress"
    SAME_KEY_RETRY_REQUIRED = "approval_delivery_same_key_retry_required"
    STALE_RETRYABLE = "approval_delivery_stale_retryable"
    OUTCOME_UNKNOWN = "approval_delivery_outcome_unknown"
    CLAIM_LOST = "approval_delivery_claim_lost"
    PAYLOAD_CHANGED = "approval_payload_changed"


class ApprovedReplyAttemptStatus(str, Enum):
    """Durable lifecycle for one approved provider send attempt."""

    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


_ACCEPTED_ATTEMPT_STATUSES = frozenset({
    ApprovedReplyAttemptStatus.SENT.value,
    ApprovedReplyAttemptStatus.DELIVERED.value,
})
_IN_FLIGHT_ATTEMPT_STATUSES = frozenset({
    ApprovedReplyAttemptStatus.QUEUED.value,
    ApprovedReplyAttemptStatus.PROCESSING.value,
})
_EXTERNAL_REPLY_CLAIM_TTL = timedelta(minutes=5)
_APPROVED_REPLY_ATTEMPT_LEASE_TTL = timedelta(minutes=5)


class ChannelReplyRouteSnapshot(TypedDict):
    version: int
    config_workspace_id: str | None
    binding_workspace_id: str | None
    runtime_workspace_id: str | None


class ApprovedReplyAttemptSnapshot(TypedDict):
    id: str
    status: str
    external_id: str | None
    error_message: str | None
    created: bool
    created_at: datetime | None
    updated_at: datetime | None
    retry_mode: NotRequired[ChannelTextSendRetryMode]


class ExternalReplyApprovalClaimResult(TypedDict):
    acquired: bool
    reason: str
    claim_id: str | None
    retry_mode: NotRequired[ChannelTextSendRetryMode]
    pending_action: NotRequired[dict[str, Any]]


_APPROVED_REPLY_METADATA_KEY = "approved_external_reply"
_APPROVED_REPLY_METADATA_VERSION = 1


def _normalize_text_send_retry_mode(
    value: object,
) -> ChannelTextSendRetryMode:
    """Fail closed when persisted or adapter retry metadata is unknown."""

    if isinstance(value, ChannelTextSendRetryMode):
        return value
    try:
        return ChannelTextSendRetryMode(value)
    except (TypeError, ValueError):
        return ChannelTextSendRetryMode.AT_LEAST_ONCE


def _configured_text_send_retry_mode(
    channel_type: str,
) -> ChannelTextSendRetryMode:
    adapter = ADAPTERS.get(channel_type)
    return _normalize_text_send_retry_mode(
        getattr(
            adapter,
            "text_send_retry_mode",
            ChannelTextSendRetryMode.AT_LEAST_ONCE,
        )
    )


def _external_reply_pending_action_snapshot(
    pending_action: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """Return one canonical, hash-bound external-reply approval payload."""

    action_key = str(pending_action.get("action_key") or "").strip()
    if action_key != ExternalMessageActionKey.SEND.value:
        return None
    canonical = dict(pending_action)
    try:
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        snapshot = json.loads(serialized)
    except (TypeError, ValueError):
        return None
    return snapshot, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _external_reply_claim_retry_mode(
    message: Message,
) -> ChannelTextSendRetryMode:
    resolution = message.resolution if isinstance(message.resolution, dict) else {}
    return _normalize_text_send_retry_mode(resolution.get("retry_mode"))


def _external_reply_claim_payload_conflict(message: Message) -> str | None:
    """Return a fail-closed reason when a live claim no longer matches its card."""

    if not external_reply_approval_claim_is_active(message):
        return None
    resolution = message.resolution if isinstance(message.resolution, dict) else {}
    expected_hash = str(resolution.get("payload_hash") or "").strip()
    if not expected_hash:
        return None
    pending_action = (
        message.pending_action if isinstance(message.pending_action, dict) else {}
    )
    snapshot = _external_reply_pending_action_snapshot(pending_action)
    if snapshot is None or snapshot[1] != expected_hash:
        return ExternalReplyApprovalClaimDisposition.PAYLOAD_CHANGED.value
    return None


def _approved_reply_attempt_retry_mode(
    log: MessageLog,
) -> ChannelTextSendRetryMode:
    attachments = log.attachments if isinstance(log.attachments, dict) else {}
    metadata = attachments.get(_APPROVED_REPLY_METADATA_KEY)
    if not isinstance(metadata, dict):
        return ChannelTextSendRetryMode.AT_LEAST_ONCE
    if metadata.get("version") != _APPROVED_REPLY_METADATA_VERSION:
        return ChannelTextSendRetryMode.AT_LEAST_ONCE
    return _normalize_text_send_retry_mode(metadata.get("retry_mode"))


def _approved_reply_attempt_metadata(
    retry_mode: ChannelTextSendRetryMode,
) -> dict[str, object]:
    return {
        _APPROVED_REPLY_METADATA_KEY: {
            "version": _APPROVED_REPLY_METADATA_VERSION,
            "retry_mode": retry_mode.value,
        }
    }


def external_reply_approval_claim_is_active(message: Message) -> bool:
    resolution = message.resolution
    return bool(
        isinstance(resolution, dict)
        and resolution.get("status") in {
            ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value,
            ExternalReplyApprovalClaimStatus.DELIVERY_RETRY_REQUIRED.value,
            ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value,
        }
        and resolution.get("attempt_id") == message.id
    )


def _external_reply_claimed_at(message: Message) -> datetime | None:
    if not external_reply_approval_claim_is_active(message):
        return None
    raw = message.resolution.get("claimed_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        claimed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return claimed_at.astimezone(timezone.utc)


def external_reply_approval_claim_disposition(
    message: Message,
) -> ExternalReplyApprovalClaimDisposition:
    """Classify whether another decision may safely replace the current claim."""
    if not external_reply_approval_claim_is_active(message):
        return ExternalReplyApprovalClaimDisposition.AVAILABLE
    if (
        message.resolution.get("status")
        == ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value
    ):
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN
    if (
        message.resolution.get("status")
        == ExternalReplyApprovalClaimStatus.DELIVERY_RETRY_REQUIRED.value
    ):
        if (
            _external_reply_claim_retry_mode(message)
            == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
        ):
            return ExternalReplyApprovalClaimDisposition.SAME_KEY_RETRY_REQUIRED
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN
    claimed_at = _external_reply_claimed_at(message)
    if claimed_at is None:
        return ExternalReplyApprovalClaimDisposition.IN_PROGRESS
    if datetime.now(timezone.utc) - claimed_at < _EXTERNAL_REPLY_CLAIM_TTL:
        return ExternalReplyApprovalClaimDisposition.IN_PROGRESS

    if (
        _external_reply_claim_retry_mode(message)
        != ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
    ):
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN
    return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE


def external_reply_approval_claim_conflict_reason(message: Message) -> str | None:
    """Return why an opposing decision must not consume this approval."""

    payload_conflict = _external_reply_claim_payload_conflict(message)
    if payload_conflict is not None:
        return payload_conflict
    disposition = external_reply_approval_claim_disposition(message)
    if disposition is ExternalReplyApprovalClaimDisposition.AVAILABLE:
        return None
    if disposition in {
        ExternalReplyApprovalClaimDisposition.SAME_KEY_RETRY_REQUIRED,
        ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE,
    }:
        # A same-key approval may safely take over, but rejection cannot prove
        # that the prior provider call did not accept the message.
        return ExternalReplyApprovalClaimDisposition.IN_PROGRESS.value
    return disposition.value


def external_reply_approval_claim_owner_conflict_reason(
    message: Message,
    claim_id: str,
) -> str | None:
    """Return ``None`` only while ``claim_id`` owns the live approval claim."""

    payload_conflict = _external_reply_claim_payload_conflict(message)
    if payload_conflict is not None:
        return payload_conflict
    if (
        message.resolved_at is None
        and external_reply_approval_claim_is_active(message)
        and message.resolution.get("status") in {
            ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value,
            ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value,
        }
        and message.resolution.get("claim_id") == claim_id
    ):
        return None
    conflict_reason = external_reply_approval_claim_conflict_reason(message)
    return conflict_reason or ExternalReplyApprovalClaimDisposition.CLAIM_LOST.value


async def claim_external_reply_approval(
    db: AsyncSession,
    *,
    message_id: str,
    entity_id: str,
    workspace_id: str,
    user_id: str,
) -> ExternalReplyApprovalClaimResult:
    """Commit a short-lived decision claim before external provider I/O."""

    message = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
        )
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if message is None:
        await db.rollback()
        return {
            "acquired": False,
            "reason": "approval_not_found",
            "claim_id": None,
        }
    pending_action = (
        message.pending_action if isinstance(message.pending_action, dict) else {}
    )
    if pending_action.get("kind") != PendingActionKind.EXTERNAL_MESSAGE_APPROVAL:
        await db.rollback()
        return {
            "acquired": False,
            "reason": "approval_kind_changed",
            "claim_id": None,
        }
    action_key = str(pending_action.get("action_key") or "").strip()
    if action_key != ExternalMessageActionKey.SEND.value:
        await db.rollback()
        return {
            "acquired": False,
            "reason": "approval_action_mismatch",
            "claim_id": None,
        }
    pending_action_snapshot = _external_reply_pending_action_snapshot(pending_action)
    if pending_action_snapshot is None:
        await db.rollback()
        return {
            "acquired": False,
            "reason": "approval_payload_invalid",
            "claim_id": None,
        }
    frozen_pending_action, payload_hash = pending_action_snapshot
    if message.resolved_at is not None:
        await db.rollback()
        return {
            "acquired": False,
            "reason": "approval_already_resolved",
            "claim_id": None,
        }
    resolution = message.resolution if isinstance(message.resolution, dict) else {}
    existing_payload_hash = str(resolution.get("payload_hash") or "").strip()
    if existing_payload_hash and existing_payload_hash != payload_hash:
        await db.rollback()
        return {
            "acquired": False,
            "reason": ExternalReplyApprovalClaimDisposition.PAYLOAD_CHANGED.value,
            "claim_id": None,
        }

    claim_disposition = await _attempt_aware_external_reply_claim_disposition(
        message,
        entity_id=entity_id,
        pending_action=frozen_pending_action,
    )
    if claim_disposition not in {
        ExternalReplyApprovalClaimDisposition.AVAILABLE,
        ExternalReplyApprovalClaimDisposition.SAME_KEY_RETRY_REQUIRED,
        ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE,
    }:
        await db.rollback()
        return {
            "acquired": False,
            "reason": claim_disposition.value,
            "claim_id": None,
        }

    now = datetime.now(timezone.utc)
    claim_id = generate_ulid()
    if claim_disposition is ExternalReplyApprovalClaimDisposition.AVAILABLE:
        retry_mode = _configured_text_send_retry_mode(
            str(pending_action.get("channel_type") or "")
        )
    else:
        retry_mode = _external_reply_claim_retry_mode(message)
    message.resolution = {
        "status": ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value,
        "choice": "approve",
        "attempt_id": message.id,
        "claim_id": claim_id,
        "claimed_by_user_id": user_id,
        "claimed_at": now.isoformat(),
        "retry_mode": retry_mode.value,
        "payload_hash": payload_hash,
    }
    await db.commit()
    return {
        "acquired": True,
        "reason": "claimed",
        "claim_id": claim_id,
        "retry_mode": retry_mode,
        "pending_action": frozen_pending_action,
    }


async def release_external_reply_approval_claim(
    db: AsyncSession,
    *,
    message_id: str,
    entity_id: str,
    workspace_id: str,
    claim_id: str,
) -> None:
    """Reopen a claimed approval after a determinate retryable failure."""

    message = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
        )
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        message is not None
        and message.resolved_at is None
        and external_reply_approval_claim_is_active(message)
        and message.resolution.get("status")
        == ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
        and message.resolution.get("claim_id") == claim_id
    ):
        message.resolution = None
    await db.commit()


async def mark_external_reply_approval_same_key_retry_required(
    db: AsyncSession,
    *,
    message_id: str,
    entity_id: str,
    workspace_id: str,
    claim_id: str,
    error: str,
) -> bool:
    """Keep an ambiguous approval claimed while allowing same-key approval retry."""

    message = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
        )
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        message is None
        or message.resolved_at is not None
        or not external_reply_approval_claim_is_active(message)
        or message.resolution.get("status")
        != ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
        or message.resolution.get("claim_id") != claim_id
    ):
        await db.rollback()
        return False

    resolution = dict(message.resolution)
    resolution.update({
        "status": ExternalReplyApprovalClaimStatus.DELIVERY_RETRY_REQUIRED.value,
        "retry_error": error[:500],
        "retry_recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    message.resolution = resolution
    await db.commit()
    return True


async def _persist_external_reply_approval_outcome_unknown(
    db: AsyncSession,
    *,
    message_id: str,
    entity_id: str,
    workspace_id: str | None,
    claim_id: str | None,
    error: str,
) -> bool:
    """Use the approval claim as a durable quarantine fallback."""

    message = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
        )
        .with_for_update(of=Message)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        message is None
        or message.resolved_at is not None
        or not external_reply_approval_claim_is_active(message)
        or not claim_id
        or message.resolution.get("claim_id") != claim_id
    ):
        await db.rollback()
        return False

    resolution = dict(message.resolution or {})
    resolution.update({
        "status": ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value,
        "outcome_error": error[:500],
        "outcome_recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    message.resolution = resolution
    await db.commit()
    return True


def _approved_reply_attempt_matches(
    log: MessageLog,
    *,
    entity_id: str,
    channel_config_id: str,
    conversation_id: str,
    channel_type: str,
    to_address: str,
    content: str,
) -> bool:
    return (
        log.direction == "outbound"
        and log.entity_id == entity_id
        and log.channel_config_id == channel_config_id
        and log.conversation_id == conversation_id
        and log.channel_type == channel_type
        and log.to_address == to_address
        and log.content == content
    )


async def _attempt_aware_external_reply_claim_disposition(
    message: Message,
    *,
    entity_id: str,
    pending_action: dict[str, Any],
) -> ExternalReplyApprovalClaimDisposition:
    """Recover stale claims only when their durable attempt proves it safe."""

    claim_disposition = external_reply_approval_claim_disposition(message)
    if claim_disposition is ExternalReplyApprovalClaimDisposition.AVAILABLE:
        return claim_disposition
    resolution = message.resolution if isinstance(message.resolution, dict) else {}
    if (
        resolution.get("status")
        == ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value
    ):
        # This can be the only durable quarantine when MessageLog persistence
        # failed, so absence of an attempt must never reopen it.
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN

    async with async_session() as attempt_db:
        attempt = await attempt_db.get(MessageLog, message.id)
    if attempt is None:
        if claim_disposition in {
            ExternalReplyApprovalClaimDisposition.SAME_KEY_RETRY_REQUIRED,
            ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE,
            ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN,
        }:
            # No durable intent means the provider call was never entered.
            return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
        return claim_disposition

    if not _approved_reply_attempt_matches(
        attempt,
        entity_id=entity_id,
        channel_config_id=str(pending_action.get("channel_config_id") or ""),
        conversation_id=str(
            pending_action.get("channel_conversation_id") or ""
        ),
        channel_type=str(pending_action.get("channel_type") or ""),
        to_address=str(
            pending_action.get("chat_id")
            or pending_action.get("sender_id")
            or ""
        ),
        content=str(pending_action.get("reply_text") or ""),
    ):
        return ExternalReplyApprovalClaimDisposition.PAYLOAD_CHANGED

    status = str(attempt.status or "")
    if status in _ACCEPTED_ATTEMPT_STATUSES:
        # The next owner will replay the durable receipt, never call again.
        return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
    if status == ApprovedReplyAttemptStatus.UNKNOWN.value:
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN
    if status == ApprovedReplyAttemptStatus.FAILED.value:
        return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
    if claim_disposition is ExternalReplyApprovalClaimDisposition.IN_PROGRESS:
        # QUEUED may be about to transition to PROCESSING in the live owner.
        return claim_disposition
    if status == ApprovedReplyAttemptStatus.QUEUED.value:
        return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
    if status == ApprovedReplyAttemptStatus.PROCESSING.value:
        if (
            _external_reply_claim_retry_mode(message)
            is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
        ):
            return ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
        return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN
    return ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN


def _approved_reply_attempt_snapshot(
    log: MessageLog,
    *,
    created: bool,
) -> ApprovedReplyAttemptSnapshot:
    return {
        "id": log.id,
        "status": str(log.status or ""),
        "external_id": log.external_id,
        "error_message": log.error_message,
        "created": created,
        "created_at": log.created_at,
        "updated_at": log.updated_at,
        "retry_mode": _approved_reply_attempt_retry_mode(log),
    }


def _approved_reply_attempt_is_recent(
    attempt: ApprovedReplyAttemptSnapshot,
) -> bool:
    timestamp = attempt.get("updated_at") or attempt.get("created_at")
    if not isinstance(timestamp, datetime):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        < _APPROVED_REPLY_ATTEMPT_LEASE_TTL
    )


async def _ensure_approved_reply_attempt(
    *,
    attempt_id: str,
    entity_id: str,
    channel_config_id: str,
    conversation_id: str,
    channel_type: str,
    to_address: str,
    content: str,
    retry_mode: ChannelTextSendRetryMode,
) -> ApprovedReplyAttemptSnapshot | None:
    """Commit the immutable outbound intent before provider I/O.

    The approval Message ULID is also the MessageLog ULID, which gives this
    cross-table operation a stable, schema-free idempotency key. A committed
    queued/processing attempt on an at-least-once adapter is conservatively
    treated as ambiguous after interruption; it is never sent a second time.
    """

    async with async_session() as attempt_db:
        log = await attempt_db.get(MessageLog, attempt_id)
        created = False
        if log is None:
            log = MessageLog(
                id=attempt_id,
                entity_id=entity_id,
                channel_config_id=channel_config_id,
                conversation_id=conversation_id,
                direction="outbound",
                channel_type=channel_type,
                to_address=to_address,
                content=content,
                attachments=_approved_reply_attempt_metadata(retry_mode),
                status=ApprovedReplyAttemptStatus.QUEUED.value,
            )
            try:
                async with attempt_db.begin_nested():
                    attempt_db.add(log)
                    await attempt_db.flush()
                created = True
            except IntegrityError:
                log = await attempt_db.get(MessageLog, attempt_id)
        if log is None or not _approved_reply_attempt_matches(
            log,
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            conversation_id=conversation_id,
            channel_type=channel_type,
            to_address=to_address,
            content=content,
        ):
            await attempt_db.rollback()
            return None
        await attempt_db.commit()
        return _approved_reply_attempt_snapshot(log, created=created)


async def _load_approved_reply_attempt(
    *,
    attempt_id: str,
    entity_id: str,
    channel_config_id: str,
    conversation_id: str,
    channel_type: str,
    to_address: str,
    content: str,
) -> ApprovedReplyAttemptSnapshot | None:
    async with async_session() as attempt_db:
        log = await attempt_db.get(MessageLog, attempt_id)
        if log is None or not _approved_reply_attempt_matches(
            log,
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            conversation_id=conversation_id,
            channel_type=channel_type,
            to_address=to_address,
            content=content,
        ):
            return None
        return _approved_reply_attempt_snapshot(log, created=False)


async def _update_approved_reply_attempt(
    attempt_id: str,
    *,
    expected_status: str,
    expected_updated_at: datetime | None,
    status: str,
    error_message: str | None = None,
    external_id: str | None = None,
) -> ApprovedReplyAttemptSnapshot | None:
    async with async_session() as attempt_db:
        log = (await attempt_db.execute(
            select(MessageLog)
            .where(MessageLog.id == attempt_id)
            .with_for_update()
        )).scalar_one_or_none()
        if log is None:
            await attempt_db.rollback()
            return None
        if (
            str(log.status or "") != expected_status
            or log.updated_at != expected_updated_at
        ):
            await attempt_db.rollback()
            return None
        log.status = status
        log.error_message = error_message[:500] if error_message else None
        log.updated_at = datetime.now(timezone.utc)
        if external_id:
            log.external_id = external_id
        await attempt_db.commit()
        return _approved_reply_attempt_snapshot(log, created=False)


async def _project_approved_reply_transcript(
    db: AsyncSession,
    *,
    conversation_id: str,
    channel_type: str,
    chat_id: str,
    content: str,
    agent_subscription_id: str | None,
) -> bool:
    """Best-effort internal projection after provider acceptance is durable."""

    try:
        await add_channel_assistant_message(
            db,
            conversation_id=conversation_id,
            channel_type=channel_type,
            chat_id=chat_id,
            content=content,
            author_kind="agent" if agent_subscription_id else "system",
            author_subscription_id=agent_subscription_id,
            approved_external_message=True,
        )
        return True
    except Exception:
        logger.exception(
            "Approved external reply was accepted but transcript projection "
            "failed conversation=%s",
            conversation_id,
        )
        try:
            await db.rollback()
        except Exception:
            logger.exception(
                "Could not restore the approval session after transcript "
                "projection failure conversation=%s",
                conversation_id,
            )
        return False


def build_channel_reply_route_snapshot(
    *,
    config_workspace_id: str | None,
    binding_workspace_id: str | None,
    runtime_workspace_id: str | None,
) -> ChannelReplyRouteSnapshot:
    """Create the JSON-safe route scope persisted before provider I/O."""

    return {
        "version": int(ChannelReplyRouteVersion.EXACT_SCOPE_V1),
        "config_workspace_id": config_workspace_id,
        "binding_workspace_id": binding_workspace_id,
        "runtime_workspace_id": runtime_workspace_id,
    }


def _normalize_channel_reply_route_snapshot(
    value: object,
) -> ChannelReplyRouteSnapshot | None:
    if not isinstance(value, dict):
        return None
    required_keys = {
        "version",
        "config_workspace_id",
        "binding_workspace_id",
        "runtime_workspace_id",
    }
    if set(value) != required_keys:
        return None
    if value.get("version") != int(ChannelReplyRouteVersion.EXACT_SCOPE_V1):
        return None

    normalized_scopes: dict[str, str | None] = {}
    for key in (
        "config_workspace_id",
        "binding_workspace_id",
        "runtime_workspace_id",
    ):
        scope = value.get(key)
        if scope is not None and (not isinstance(scope, str) or not scope):
            return None
        normalized_scopes[key] = scope
    return {
        "version": int(ChannelReplyRouteVersion.EXACT_SCOPE_V1),
        "config_workspace_id": normalized_scopes["config_workspace_id"],
        "binding_workspace_id": normalized_scopes["binding_workspace_id"],
        "runtime_workspace_id": normalized_scopes["runtime_workspace_id"],
    }


async def _load_routable_channel_config(
    db: AsyncSession,
    *,
    cc_id: str,
    channel_type: str,
    entity_id: str | None = None,
) -> ChannelConfig | None:
    """Load the live config gate shared by every provider text send."""

    cc = await load_channel_config(db, cc_id)
    if (
        cc is None
        or (entity_id is not None and cc.entity_id != entity_id)
        or cc.channel_type != channel_type
        or cc.status != "active"
        or not await channel_credential_source_is_available(db, cc)
        or not await channel_workspace_is_routable(
            db,
            cc.workspace_id,
            entity_id=cc.entity_id,
        )
    ):
        return None
    return cc


async def _load_current_notification_route(
    db: AsyncSession,
    *,
    contact: ChannelContact,
) -> tuple[ChannelContact, ChannelConfig] | None:
    """Revalidate a notification contact and provider route before I/O."""

    expected = {
        "entity_id": contact.entity_id,
        "channel_config_id": contact.channel_config_id,
        "channel_type": contact.channel_type,
        "source_id": contact.source_id,
        "user_id": contact.user_id,
    }
    current = (await db.execute(
        select(ChannelContact)
        .where(ChannelContact.id == contact.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        current is None
        or current.status != "active"
        or any(getattr(current, key) != value for key, value in expected.items())
    ):
        return None
    cc = await _load_routable_channel_config(
        db,
        cc_id=expected["channel_config_id"],
        channel_type=expected["channel_type"],
        entity_id=expected["entity_id"],
    )
    if cc is None:
        return None
    return current, cc


async def _load_active_delivery_binding(
    db: AsyncSession,
    *,
    cc: ChannelConfig,
    channel_type: str,
) -> Any | None:
    if channel_type == "slack":
        bindings = await load_slack_channel_bindings_for_config(db, cc)
        if len(bindings) != 1:
            return None
        binding = bindings[0]
    else:
        binding = await load_channel_binding_for_config(db, cc)
    if (
        binding is None
        or (
            channel_type in {"slack", "discord"}
            and binding.user_id != cc.owner_user_id
        )
        or not await channel_workspace_is_routable(
            db,
            binding.workspace_id,
            entity_id=cc.entity_id,
        )
    ):
        return None
    return binding


async def channel_reply_route_is_active(
    *,
    entity_id: str,
    cc_id: str,
    channel_type: str,
    channel_binding_id: str | None = None,
    channel_contact_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    route_snapshot: object = None,
    workspace_id: str | None = None,
) -> bool:
    """Revalidate the durable reply route immediately before provider I/O."""

    snapshot = _normalize_channel_reply_route_snapshot(route_snapshot)
    if not channel_binding_id or not channel_contact_id or snapshot is None:
        return False

    async with async_session() as db:
        cc = await _load_routable_channel_config(
            db,
            cc_id=cc_id,
            channel_type=channel_type,
            entity_id=entity_id,
        )
        if cc is None:
            return False
        binding = await _load_active_delivery_binding(
            db,
            cc=cc,
            channel_type=channel_type,
        )
        if binding is None:
            return False
        if binding.id != channel_binding_id:
            return False
        if (
            cc.workspace_id != snapshot["config_workspace_id"]
            or binding.workspace_id != snapshot["binding_workspace_id"]
            or workspace_id != snapshot["runtime_workspace_id"]
        ):
            return False
        contact = await db.get(ChannelContact, channel_contact_id)
        if (
            contact is None
            or contact.entity_id != entity_id
            or contact.channel_config_id != cc_id
            or contact.channel_type != channel_type
            or contact.status != "active"
        ):
            return False
        resolved = await resolve_subscription(
            db,
            binding=binding,
            contact=(
                None
                if channel_type in {"slack", "discord", "twilio_voice"}
                else contact
            ),
        )
        if (
            resolved.agent_id != agent_id
            or resolved.id != agent_subscription_id
            or resolved.workspace_id != workspace_id
        ):
            return False
        return bool(
            await channel_workspace_is_routable(
                db,
                binding.workspace_id,
                entity_id=entity_id,
            )
            and await channel_workspace_is_routable(
                db,
                workspace_id,
                entity_id=entity_id,
            )
        )


@dataclass
class LockedChannelReplyRoute:
    """Live route rows held until one provider call has returned."""

    db: AsyncSession
    channel_config: ChannelConfig
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            await self.db.rollback()
        finally:
            await self.db.close()


async def _acquire_locked_channel_reply_route(
    *,
    entity_id: str,
    cc_id: str,
    channel_type: str,
    channel_binding_id: str | None,
    channel_contact_id: str | None,
    agent_id: str | None,
    agent_subscription_id: str | None,
    route_snapshot: object,
    workspace_id: str | None,
) -> LockedChannelReplyRoute | None:
    """Lock every mutable route boundary across the provider call.

    The final active-route validation runs only after the rows are locked.
    Deletes, revocations, rebindings, subscription changes, and Workspace
    pauses therefore commit either before this validation or after the send.
    """

    snapshot = _normalize_channel_reply_route_snapshot(route_snapshot)
    if not channel_binding_id or not channel_contact_id or snapshot is None:
        return None

    route_db = async_session()
    try:
        workspace_ids = sorted({
            value
            for value in (
                snapshot["config_workspace_id"],
                snapshot["binding_workspace_id"],
                snapshot["runtime_workspace_id"],
                workspace_id,
            )
            if value
        })
        if workspace_ids:
            locked_workspace_ids = set((await route_db.execute(
                select(Workspace.id)
                .where(
                    Workspace.id.in_(workspace_ids),
                    Workspace.entity_id == entity_id,
                )
                .order_by(Workspace.id)
                .with_for_update()
            )).scalars().all())
            if locked_workspace_ids != set(workspace_ids):
                await route_db.rollback()
                await route_db.close()
                return None

        cc = (await route_db.execute(
            select(ChannelConfig)
            .where(
                ChannelConfig.id == cc_id,
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.channel_type == channel_type,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if cc is None:
            await route_db.rollback()
            await route_db.close()
            return None

        binding = (await route_db.execute(
            select(Channel)
            .where(
                Channel.id == channel_binding_id,
                Channel.entity_id == entity_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        contact = (await route_db.execute(
            select(ChannelContact)
            .where(
                ChannelContact.id == channel_contact_id,
                ChannelContact.entity_id == entity_id,
            )
            .with_for_update()
        )).scalar_one_or_none()
        if binding is None or contact is None:
            await route_db.rollback()
            await route_db.close()
            return None

        if agent_subscription_id:
            subscription = (await route_db.execute(
                select(AgentSubscription)
                .where(
                    AgentSubscription.id == agent_subscription_id,
                    AgentSubscription.entity_id == entity_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if subscription is None:
                await route_db.rollback()
                await route_db.close()
                return None

        source_kind = str(cc.credential_source_kind or "")
        source_id = str(cc.credential_source_id or "")
        source_owner_user_id = str(cc.owner_user_id or "")
        if bool(source_kind) != bool(source_id):
            await route_db.rollback()
            await route_db.close()
            return None
        if source_kind and (
            not source_owner_user_id
            or not await lock_connection_owner_authorization(
                route_db,
                entity_id=entity_id,
                owner_user_id=source_owner_user_id,
            )
        ):
            await route_db.rollback()
            await route_db.close()
            return None
        if source_kind == "integration" and source_id:
            source = (await route_db.execute(
                select(Integration)
                .where(
                    Integration.id == source_id,
                    Integration.entity_id == entity_id,
                    Integration.owner_user_id == source_owner_user_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if source is None:
                await route_db.rollback()
                await route_db.close()
                return None
        elif source_kind == "oauth_account" and source_id:
            source = (await route_db.execute(
                select(OAuthAccount)
                .where(
                    OAuthAccount.id == source_id,
                    OAuthAccount.user_id == source_owner_user_id,
                )
                .with_for_update()
            )).scalar_one_or_none()
            if source is None:
                await route_db.rollback()
                await route_db.close()
                return None
        elif source_kind:
            await route_db.rollback()
            await route_db.close()
            return None
        if source_kind:
            decision = await resolve_integration_access(
                route_db,
                kind=source_kind,
                connection_id=source_id,
                entity_id=entity_id,
                user_id=source_owner_user_id,
                action="bind_channel",
            )
            if (
                not decision.allowed
                or decision.owner_user_id != source_owner_user_id
            ):
                await route_db.rollback()
                await route_db.close()
                return None

        if not await channel_reply_route_is_active(
            entity_id=entity_id,
            cc_id=cc_id,
            channel_type=channel_type,
            channel_binding_id=channel_binding_id,
            channel_contact_id=channel_contact_id,
            agent_id=agent_id,
            agent_subscription_id=agent_subscription_id,
            route_snapshot=snapshot,
            workspace_id=workspace_id,
        ):
            await route_db.rollback()
            await route_db.close()
            return None
        return LockedChannelReplyRoute(db=route_db, channel_config=cc)
    except BaseException:
        await route_db.rollback()
        await route_db.close()
        raise


async def send_channel_text_reply(
    *,
    cc_id: str,
    channel_type: str,
    chat_id: str,
    text: str,
    thread_ts: str | None = None,
    reply_context: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    channel_binding_id: str | None = None,
    require_active_binding: bool = False,
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
        cc = await _load_routable_channel_config(
            db,
            cc_id=cc_id,
            channel_type=channel_type,
        )
        if cc is None:
            return False
        if require_active_binding or channel_binding_id:
            binding = await _load_active_delivery_binding(
                db,
                cc=cc,
                channel_type=channel_type,
            )
            if (
                binding is None
                or (channel_binding_id and binding.id != channel_binding_id)
            ):
                return False

    try:
        send_kwargs: dict[str, Any] = {}
        if thread_ts:
            send_kwargs["thread_ts"] = thread_ts
        if reply_context:
            send_kwargs["reply_context"] = dict(reply_context)
        if idempotency_key:
            send_kwargs["idempotency_key"] = idempotency_key
        result = await adapter.send_text(cc, chat_id, text, **send_kwargs)
    except SoftTimeLimitExceeded:
        raise
    except NotImplementedError as exc:
        logger.info("Gateway: %s send_text not implemented yet: %s", channel_type, exc)
        return False
    except Exception:
        logger.exception(
            "Gateway send-back failed for %s chat=%s",
            channel_type,
            chat_id,
        )
        try:
            await mark_last_channel_outbound_failed(cc_id, chat_id)
        except Exception:
            logger.exception(
                "Gateway failed to project provider send failure for %s chat=%s",
                channel_type,
                chat_id,
            )
        return False

    raw_status = (
        str(result.get("status", ""))
        if isinstance(result, dict)
        else ""
    )
    normalized_status = normalize_channel_outbound_status(raw_status)
    deferred = raw_status.strip().lower() == "deferred"
    try:
        await mark_last_channel_outbound_sent(cc_id, chat_id, result)
    except Exception:
        logger.exception(
            "Gateway provider accepted %s chat=%s but outbound projection failed",
            channel_type,
            chat_id,
        )
    return not deferred and normalized_status != "failed"


async def send_actionable_outbound_to_contact(
    db: AsyncSession,
    *,
    contact: ChannelContact,
    text: str,
    actions: list[dict[str, Any]],
    notification_id: str | None = None,
) -> dict[str, Any]:
    """System-initiated actionable outbound with channel-native CTAs."""
    route = await _load_current_notification_route(db, contact=contact)
    if route is None:
        return {"sent": False, "error": "channel_route_unavailable"}
    contact, cc = route

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
    send_ok = log.status != "failed"
    if not send_ok:
        log.error_message = str(
            result.get("error") or result.get("reason") or "adapter_status_failed"
        )[:500]
    if isinstance(result, dict):
        external_id = result.get("external_id") or result.get("message_id")
        if external_id:
            log.external_id = str(external_id)
    await db.flush()
    return {
        "sent": send_ok,
        "error": None if send_ok else log.error_message,
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
    route = await _load_current_notification_route(db, contact=contact)
    if route is None:
        return {"sent": False, "error": "channel_route_unavailable"}
    contact, cc = route

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
    send_ok = log.status != "failed"
    if not send_ok:
        log.error_message = str(
            result.get("error") or result.get("reason") or "adapter_status_failed"
        )[:500]
    if isinstance(result, dict):
        external_id = result.get("external_id") or result.get("message_id")
        if external_id:
            log.external_id = str(external_id)
        from_address = result.get("from_address")
        if from_address:
            log.from_address = str(from_address)
    await db.flush()
    return {
        "sent": send_ok,
        "error": None if send_ok else log.error_message,
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
    channel_binding_id: str | None = None,
    channel_contact_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    route_snapshot: object = None,
    workspace_id: str | None = None,
    thread_ts: str | None = None,
    idempotency_key: str | None = None,
    approval_claim_id: str | None = None,
    retry_mode: ChannelTextSendRetryMode | str | None = None,
) -> dict[str, Any]:
    """Fence one governance-approved provider attempt by its stable key."""
    if not idempotency_key:
        return await _deliver_approved_external_reply_once(
            db,
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            channel_type=channel_type,
            channel_conversation_id=channel_conversation_id,
            chat_id=chat_id,
            text=text,
            channel_binding_id=channel_binding_id,
            channel_contact_id=channel_contact_id,
            agent_id=agent_id,
            agent_subscription_id=agent_subscription_id,
            route_snapshot=route_snapshot,
            workspace_id=workspace_id,
            thread_ts=thread_ts,
            idempotency_key=idempotency_key,
            approval_claim_id=approval_claim_id,
            retry_mode=retry_mode,
        )
    try:
        lease = await TransactionAdvisoryLock.try_acquire(
            db,
            namespace=int(AdvisoryLockNamespace.APPROVED_EXTERNAL_REPLY),
            key=idempotency_key,
        )
    except Exception as exc:
        raise ApprovedExternalReplyDeliveryError(
            "approved reply delivery lock is unavailable"
        ) from exc
    if lease is None:
        raise ApprovedExternalReplySameKeyRetryRequired(
            "approved reply delivery is already in progress"
        )
    try:
        return await _deliver_approved_external_reply_once(
            db,
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            channel_type=channel_type,
            channel_conversation_id=channel_conversation_id,
            chat_id=chat_id,
            text=text,
            channel_binding_id=channel_binding_id,
            channel_contact_id=channel_contact_id,
            agent_id=agent_id,
            agent_subscription_id=agent_subscription_id,
            route_snapshot=route_snapshot,
            workspace_id=workspace_id,
            thread_ts=thread_ts,
            idempotency_key=idempotency_key,
            approval_claim_id=approval_claim_id,
            retry_mode=retry_mode,
        )
    finally:
        await lease.release()


async def _deliver_approved_external_reply_once(
    db: AsyncSession,
    *,
    entity_id: str,
    channel_config_id: str,
    channel_type: str,
    channel_conversation_id: str,
    chat_id: str,
    text: str,
    channel_binding_id: str | None = None,
    channel_contact_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    route_snapshot: object = None,
    workspace_id: str | None = None,
    thread_ts: str | None = None,
    idempotency_key: str | None = None,
    approval_claim_id: str | None = None,
    retry_mode: ChannelTextSendRetryMode | str | None = None,
) -> dict[str, Any]:
    """Persist and send a governance-approved channel reply.

    The provider call never owns the caller's approval transaction. A stable
    MessageLog intent is committed first and updated in short independent
    transactions around the call. Provider-idempotent adapters may safely
    reuse the same key; at-least-once adapters quarantine an interrupted or
    otherwise ambiguous attempt instead of automatically sending twice.
    """
    if not channel_binding_id or not channel_contact_id or route_snapshot is None:
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.TERMINAL_ROUTE_INVALID,
            error="invalid_reply_route",
        )
    adapter = ADAPTERS.get(channel_type)
    # Approved delivery must receive the mode frozen by its durable claim.
    # Missing/legacy metadata is intentionally at-least-once, never inferred
    # from the adapter registry loaded by this deployment.
    frozen_retry_mode = _normalize_text_send_retry_mode(retry_mode)
    if idempotency_key:
        existing_attempt = await _load_approved_reply_attempt(
            attempt_id=idempotency_key,
            entity_id=entity_id,
            channel_config_id=channel_config_id,
            conversation_id=channel_conversation_id,
            channel_type=channel_type,
            to_address=chat_id,
            content=text,
        )
        if existing_attempt is not None:
            frozen_retry_mode = existing_attempt.get(
                "retry_mode",
                ChannelTextSendRetryMode.AT_LEAST_ONCE,
            )
        if (
            existing_attempt is not None
            and existing_attempt["status"] in _ACCEPTED_ATTEMPT_STATUSES
        ):
            # Acceptance is already terminal evidence. A later route revocation
            # cannot rewrite that fact after a crash-before-approval-commit.
            transcript_projected = await _project_approved_reply_transcript(
                db,
                conversation_id=channel_conversation_id,
                channel_type=channel_type,
                chat_id=chat_id,
                content=text,
                agent_subscription_id=agent_subscription_id,
            )
            return _approved_external_reply_delivery_result(
                ApprovedExternalReplyDeliveryDisposition.ACCEPTED,
                message_log_id=existing_attempt["id"],
                external_id=existing_attempt["external_id"],
                replayed=True,
                transcript_projected=transcript_projected,
            )
        if (
            existing_attempt is not None
            and existing_attempt["status"]
            == ApprovedReplyAttemptStatus.UNKNOWN.value
        ):
            return _approved_external_reply_delivery_result(
                ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN,
                reason="provider_outcome_unknown",
                error=(
                    existing_attempt["error_message"]
                    or "provider outcome unknown"
                ),
                message_log_id=existing_attempt["id"],
                replayed=True,
            )
        if (
            existing_attempt is not None
            and existing_attempt["status"] in _IN_FLIGHT_ATTEMPT_STATUSES
        ):
            if _approved_reply_attempt_is_recent(existing_attempt):
                error = "approved reply delivery is already in progress"
                if frozen_retry_mode == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
                    raise ApprovedExternalReplySameKeyRetryRequired(error)
                raise ApprovedExternalReplyDeliveryError(error)
            if frozen_retry_mode == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
                # A stale provider-idempotent owner can be resumed with the
                # exact same key; the provider remains the final duplicate
                # fence.
                pass
            else:
                # Terminalize an interrupted non-idempotent attempt before any
                # live-route check. Route drift must not leave ambiguous
                # provider I/O indefinitely processing or make it retryable.
                quarantined = await _update_approved_reply_attempt(
                    existing_attempt["id"],
                    expected_status=existing_attempt["status"],
                    expected_updated_at=existing_attempt.get("updated_at"),
                    status=ApprovedReplyAttemptStatus.UNKNOWN.value,
                    error_message="interrupted at-least-once provider attempt",
                )
                if quarantined is None:
                    raise ApprovedExternalReplyDeliveryError(
                        "approved reply delivery ownership changed"
                    )
                return _approved_external_reply_delivery_result(
                    ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN,
                    reason="provider_outcome_unknown",
                    error="interrupted at-least-once provider attempt",
                    message_log_id=existing_attempt["id"],
                    replayed=True,
                )
    current_retry_mode = _normalize_text_send_retry_mode(
        getattr(
            adapter,
            "text_send_retry_mode",
            ChannelTextSendRetryMode.AT_LEAST_ONCE,
        )
    )
    if (
        frozen_retry_mode is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
        and current_retry_mode is not ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
    ):
        raise ApprovedExternalReplySameKeyRetryRequired(
            "approved reply provider idempotency contract changed"
        )
    if not await channel_reply_route_is_active(
        entity_id=entity_id,
        cc_id=channel_config_id,
        channel_type=channel_type,
        channel_binding_id=channel_binding_id,
        channel_contact_id=channel_contact_id,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        route_snapshot=route_snapshot,
        workspace_id=workspace_id,
    ):
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.TERMINAL_ROUTE_INVALID,
            error="reply_route_unavailable",
        )
    if not idempotency_key:
        raise ApprovedExternalReplyDeliveryError(
            "approved reply delivery is missing its idempotency key"
        )

    if adapter is None:
        raise ApprovedExternalReplyDeliveryError(
            f"no channel adapter is registered for {channel_type}"
        )
    attempt = await _ensure_approved_reply_attempt(
        attempt_id=idempotency_key,
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        conversation_id=channel_conversation_id,
        channel_type=channel_type,
        to_address=chat_id,
        content=text,
        retry_mode=frozen_retry_mode,
    )
    if attempt is None:
        raise ApprovedExternalReplyDeliveryError(
            "approved reply delivery attempt conflicts with its immutable intent"
        )
    attempt_id = attempt["id"]
    frozen_retry_mode = attempt.get(
        "retry_mode",
        ChannelTextSendRetryMode.AT_LEAST_ONCE,
    )
    if attempt["status"] in _ACCEPTED_ATTEMPT_STATUSES:
        transcript_projected = await _project_approved_reply_transcript(
            db,
            conversation_id=channel_conversation_id,
            channel_type=channel_type,
            chat_id=chat_id,
            content=text,
            agent_subscription_id=agent_subscription_id,
        )
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.ACCEPTED,
            message_log_id=attempt_id,
            external_id=attempt["external_id"],
            replayed=True,
            transcript_projected=transcript_projected,
        )
    if attempt["status"] == ApprovedReplyAttemptStatus.UNKNOWN.value:
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN,
            reason="provider_outcome_unknown",
            error=attempt["error_message"] or "provider outcome unknown",
            message_log_id=attempt_id,
            replayed=True,
        )
    if (
        not attempt["created"]
        and attempt["status"] in _IN_FLIGHT_ATTEMPT_STATUSES
    ):
        if _approved_reply_attempt_is_recent(attempt):
            error = "approved reply delivery is already in progress"
            if frozen_retry_mode == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
                raise ApprovedExternalReplySameKeyRetryRequired(error)
            raise ApprovedExternalReplyDeliveryError(error)
        if frozen_retry_mode == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
            # Resume a stale provider-idempotent owner below.
            pass
        else:
            # A prior worker committed the intent but did not reach a terminal
            # projection. Without provider idempotency there is no safe way to
            # distinguish crash-before-call from acceptance-before-crash.
            quarantined = await _update_approved_reply_attempt(
                attempt_id,
                expected_status=attempt["status"],
                expected_updated_at=attempt.get("updated_at"),
                status=ApprovedReplyAttemptStatus.UNKNOWN.value,
                error_message="interrupted at-least-once provider attempt",
            )
            if quarantined is None:
                raise ApprovedExternalReplyDeliveryError(
                    "approved reply delivery ownership changed"
                )
            return _approved_external_reply_delivery_result(
                ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN,
                reason="provider_outcome_unknown",
                error="interrupted at-least-once provider attempt",
                message_log_id=attempt_id,
                replayed=True,
            )

    processing = await _update_approved_reply_attempt(
        attempt_id,
        expected_status=attempt["status"],
        expected_updated_at=attempt.get("updated_at"),
        status=ApprovedReplyAttemptStatus.PROCESSING.value,
    )
    if processing is None:
        raise ApprovedExternalReplyDeliveryError(
            "durable delivery attempt disappeared"
        )

    async def quarantine_unknown(exc: BaseException) -> dict[str, Any]:
        error = str(exc) or type(exc).__name__
        quarantine = None
        try:
            quarantine = await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.UNKNOWN.value,
                error_message=error,
            )
            if quarantine is None:
                # Rebuild the stable intent if it vanished after provider
                # acceptance, then terminalize it before the caller can crash.
                repaired = await _ensure_approved_reply_attempt(
                    attempt_id=attempt_id,
                    entity_id=entity_id,
                    channel_config_id=channel_config_id,
                    conversation_id=channel_conversation_id,
                    channel_type=channel_type,
                    to_address=chat_id,
                    content=text,
                    retry_mode=frozen_retry_mode,
                )
                if repaired is not None and repaired["created"]:
                    quarantine = await _update_approved_reply_attempt(
                        attempt_id,
                        expected_status=repaired["status"],
                        expected_updated_at=repaired.get("updated_at"),
                        status=ApprovedReplyAttemptStatus.UNKNOWN.value,
                        error_message=error,
                    )
        except Exception:
            logger.exception(
                "Could not persist ambiguous approved reply attempt=%s",
                attempt_id,
            )
        quarantine_source = "message_log"
        if quarantine is None:
            try:
                fallback_persisted = (
                    await _persist_external_reply_approval_outcome_unknown(
                        db,
                        message_id=idempotency_key,
                        entity_id=entity_id,
                        workspace_id=workspace_id,
                        claim_id=approval_claim_id,
                        error=error,
                    )
                )
            except Exception as fallback_exc:
                logger.exception(
                    "Could not persist approval-level ambiguity attempt=%s",
                    attempt_id,
                )
                raise ApprovedExternalReplyOutcomeUnknownError(error) from fallback_exc
            if not fallback_persisted:
                raise ApprovedExternalReplyOutcomeUnknownError(error)
            quarantine_source = "approval_claim"
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN,
            reason="provider_outcome_unknown",
            error=error,
            message_log_id=attempt_id,
            quarantine_persisted=True,
            quarantine_source=quarantine_source,
        )

    try:
        try:
            route_lease = await _acquire_locked_channel_reply_route(
                entity_id=entity_id,
                cc_id=channel_config_id,
                channel_type=channel_type,
                channel_binding_id=channel_binding_id,
                channel_contact_id=channel_contact_id,
                agent_id=agent_id,
                agent_subscription_id=agent_subscription_id,
                route_snapshot=route_snapshot,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message="approved reply route lock is unavailable",
            )
            raise ApprovedExternalReplyDeliveryError(
                "approved reply route lock is unavailable"
            ) from exc
        if route_lease is None:
            failed = await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message="reply_route_unavailable",
            )
            if failed is None:
                raise ApprovedExternalReplyDeliveryError(
                    "approved reply route ownership changed"
                )
            return _approved_external_reply_delivery_result(
                ApprovedExternalReplyDeliveryDisposition.TERMINAL_ROUTE_INVALID,
                error="reply_route_unavailable",
                message_log_id=attempt_id,
            )

        send_kwargs = {"thread_ts": thread_ts} if thread_ts else {}
        if idempotency_key:
            send_kwargs["idempotency_key"] = idempotency_key
        try:
            result = await adapter.send_text(
                route_lease.channel_config,
                chat_id,
                text,
                **send_kwargs,
            )
        finally:
            await route_lease.release()
        adapter_disposition, status, error = (
            _approved_external_reply_adapter_disposition(result)
        )
        if (
            adapter_disposition
            is ApprovedExternalReplyAdapterDisposition.DETERMINATE_FAILURE
        ):
            await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message=error,
            )
            raise ApprovedExternalReplyDeliveryError(error)
        if (
            adapter_disposition
            is ApprovedExternalReplyAdapterDisposition.OUTCOME_UNKNOWN
        ):
            if frozen_retry_mode != ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
                return await quarantine_unknown(RuntimeError(error))
            await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message=error,
            )
            raise ApprovedExternalReplySameKeyRetryRequired(error)
        external_id = None
        if isinstance(result, dict):
            raw_external_id = result.get("external_id") or result.get("message_id")
            if raw_external_id:
                external_id = str(raw_external_id)
        persisted = await _update_approved_reply_attempt(
            attempt_id,
            expected_status=processing["status"],
            expected_updated_at=processing.get("updated_at"),
            status=status,
            external_id=external_id,
        )
        if persisted is None:
            if frozen_retry_mode != ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
                return await quarantine_unknown(RuntimeError(
                    "provider accepted the reply but its durable receipt "
                    "disappeared"
                ))
            raise ApprovedExternalReplySameKeyRetryRequired(
                "provider accepted the reply but its durable receipt disappeared"
            )
        transcript_projected = await _project_approved_reply_transcript(
            db,
            conversation_id=channel_conversation_id,
            channel_type=channel_type,
            chat_id=chat_id,
            content=text,
            agent_subscription_id=agent_subscription_id,
        )
        return _approved_external_reply_delivery_result(
            ApprovedExternalReplyDeliveryDisposition.ACCEPTED,
            message_log_id=attempt_id,
            external_id=external_id,
            adapter_result=result,
            transcript_projected=transcript_projected,
        )
    except SoftTimeLimitExceeded as exc:
        if frozen_retry_mode == ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
            error = "approved external reply provider soft time limit exceeded"
            await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message=error,
            )
            raise ApprovedExternalReplyRetryableTimeout() from exc
        return await quarantine_unknown(exc)
    except ApprovedExternalReplyDeliveryError:
        raise
    except ApprovedExternalReplySameKeyRetryRequired:
        raise
    except ChannelTextSendError as exc:
        if exc.disposition is ChannelTextSendFailureDisposition.DETERMINATE:
            error = str(exc)
            if exc.reason_code:
                error = f"{exc.reason_code}: {error}"
            await _update_approved_reply_attempt(
                attempt_id,
                expected_status=processing["status"],
                expected_updated_at=processing.get("updated_at"),
                status=ApprovedReplyAttemptStatus.FAILED.value,
                error_message=error,
            )
            raise ApprovedExternalReplyDeliveryError(
                error,
                reason_code=exc.reason_code,
            ) from exc
        if frozen_retry_mode != ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
            return await quarantine_unknown(exc)
        await _update_approved_reply_attempt(
            attempt_id,
            expected_status=processing["status"],
            expected_updated_at=processing.get("updated_at"),
            status=ApprovedReplyAttemptStatus.FAILED.value,
            error_message=str(exc),
        )
        raise ApprovedExternalReplySameKeyRetryRequired(str(exc)) from exc
    except Exception as exc:
        if frozen_retry_mode != ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT:
            return await quarantine_unknown(exc)
        await _update_approved_reply_attempt(
            attempt_id,
            expected_status=processing["status"],
            expected_updated_at=processing.get("updated_at"),
            status=ApprovedReplyAttemptStatus.FAILED.value,
            error_message=str(exc),
        )
        raise ApprovedExternalReplySameKeyRetryRequired(str(exc)) from exc
