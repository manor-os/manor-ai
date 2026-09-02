from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from packages.core.services import channel_outbound_delivery as delivery
from packages.core.services.channels.base import (
    ChannelTextSendError,
    ChannelTextSendRetryMode,
)
from packages.core.database import async_session
from packages.core.models.base import generate_ulid


pytestmark = pytest.mark.asyncio


def _stub_active_route_lease(monkeypatch, *, state: dict | None = None) -> None:
    route_state = state if state is not None else {}

    class Lease:
        channel_config = SimpleNamespace(id="config-1")

        async def release(self) -> None:
            route_state["locked"] = False

    async def acquire(**_kwargs):
        route_state["locked"] = True
        return Lease()

    monkeypatch.setattr(delivery, "_acquire_locked_channel_reply_route", acquire)


def _stub_durable_attempt(monkeypatch, log) -> list[str]:
    updates: list[str] = []
    created_at = datetime.now(timezone.utc)
    revision = created_at
    frozen_retry_mode = ChannelTextSendRetryMode.AT_LEAST_ONCE

    async def ensure_attempt(**_kwargs):
        nonlocal frozen_retry_mode
        frozen_retry_mode = _kwargs["retry_mode"]
        return {
            "id": log.id,
            "status": log.status,
            "external_id": getattr(log, "external_id", None),
            "error_message": getattr(log, "error_message", None),
            "created": True,
            "created_at": created_at,
            "updated_at": revision,
            "retry_mode": frozen_retry_mode,
        }

    async def update_attempt(
        _attempt_id,
        *,
        expected_status,
        expected_updated_at,
        status,
        error_message=None,
        external_id=None,
    ):
        nonlocal revision
        if log.status != expected_status or revision != expected_updated_at:
            return None
        updates.append(status)
        log.status = status
        log.error_message = error_message
        if external_id:
            log.external_id = external_id
        revision += timedelta(microseconds=1)
        return {
            "id": log.id,
            "status": log.status,
            "external_id": getattr(log, "external_id", None),
            "error_message": getattr(log, "error_message", None),
            "created": False,
            "created_at": created_at,
            "updated_at": revision,
            "retry_mode": frozen_retry_mode,
        }

    monkeypatch.setattr(delivery, "_ensure_approved_reply_attempt", ensure_attempt)
    monkeypatch.setattr(delivery, "_update_approved_reply_attempt", update_attempt)

    async def no_existing_attempt(**_kwargs):
        return None

    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", no_existing_attempt)
    _stub_active_route_lease(monkeypatch)
    return updates


async def test_stale_approved_reply_owner_cannot_overwrite_successor_attempt(
    db_session,
) -> None:
    from packages.core.models.channel import MessageLog

    attempt = MessageLog(
        id=generate_ulid(),
        entity_id="entity-attempt-cas",
        channel_config_id="config-attempt-cas",
        conversation_id="conversation-attempt-cas",
        direction="outbound",
        channel_type="telegram",
        to_address="recipient-attempt-cas",
        content="approved reply",
        status=delivery.ApprovedReplyAttemptStatus.QUEUED.value,
    )
    async with async_session() as db:
        db.add(attempt)
        await db.commit()
        await db.refresh(attempt)
        queued_revision = attempt.updated_at

    first_owner = await delivery._update_approved_reply_attempt(
        attempt.id,
        expected_status=delivery.ApprovedReplyAttemptStatus.QUEUED.value,
        expected_updated_at=queued_revision,
        status=delivery.ApprovedReplyAttemptStatus.PROCESSING.value,
    )
    assert first_owner is not None
    successor = await delivery._update_approved_reply_attempt(
        attempt.id,
        expected_status=first_owner["status"],
        expected_updated_at=first_owner["updated_at"],
        status=delivery.ApprovedReplyAttemptStatus.PROCESSING.value,
    )
    assert successor is not None

    stale_write = await delivery._update_approved_reply_attempt(
        attempt.id,
        expected_status=first_owner["status"],
        expected_updated_at=first_owner["updated_at"],
        status=delivery.ApprovedReplyAttemptStatus.SENT.value,
    )
    assert stale_write is None

    current = await delivery._load_approved_reply_attempt(
        attempt_id=attempt.id,
        entity_id=attempt.entity_id,
        channel_config_id=attempt.channel_config_id,
        conversation_id=attempt.conversation_id,
        channel_type=attempt.channel_type,
        to_address=attempt.to_address,
        content=attempt.content,
    )
    assert current is not None
    assert current["status"] == delivery.ApprovedReplyAttemptStatus.PROCESSING.value
    assert current["updated_at"] == successor["updated_at"]

    await delivery._update_approved_reply_attempt(
        attempt.id,
        expected_status=successor["status"],
        expected_updated_at=successor["updated_at"],
        status=delivery.ApprovedReplyAttemptStatus.SENT.value,
    )
    async with async_session() as db:
        persisted = await db.get(MessageLog, attempt.id)
        assert persisted is not None
        await db.delete(persisted)
        await db.commit()


async def test_concurrent_approved_reply_delivery_has_one_provider_owner(
    monkeypatch,
    db_session,
) -> None:
    """A stable approval key must fence overlapping provider calls."""

    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class BlockingAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return {"status": "sent", "external_id": "provider-message-1"}

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    _stub_active_route_lease(monkeypatch)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", BlockingAdapter())

    attempt_id = generate_ulid()
    kwargs = {
        "entity_id": "entity-1",
        "channel_config_id": "config-1",
        "channel_type": "telegram",
        "channel_binding_id": "binding-1",
        "channel_contact_id": "contact-1",
        "channel_conversation_id": "conversation-1",
        "chat_id": "recipient-1",
        "text": "approved reply",
        "route_snapshot": {
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        "workspace_id": "workspace-1",
        "idempotency_key": attempt_id,
    }

    async def deliver_once():
        async with async_session() as db:
            return await delivery.deliver_approved_external_reply(db, **kwargs)

    first = asyncio.create_task(deliver_once())
    await entered.wait()
    try:
        with pytest.raises(
            delivery.ApprovedExternalReplySameKeyRetryRequired,
            match="already in progress",
        ):
            await deliver_once()
    finally:
        release.set()

    assert (await first)["sent"] is True
    assert calls == 1


async def test_external_reply_claim_serializes_decisions_and_can_reopen(
    monkeypatch,
    db_session,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    class IdempotentAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    monkeypatch.setitem(delivery.ADAPTERS, "claim_test_channel", IdempotentAdapter())

    entity_id = "entity-claim-test"
    workspace_id = "workspace-claim-test"
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            "channel_type": "claim_test_channel",
            "action_key": "external_message.send",
        },
    )
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()
        first = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-1",
        )
        second = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )

        assert first["acquired"] is True
        assert first["reason"] == "claimed"
        assert first["claim_id"]
        assert first["retry_mode"] is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT
        assert first["pending_action"]["action_key"] == "external_message.send"
        assert second == {
            "acquired": False,
            "reason": "approval_delivery_in_progress",
            "claim_id": None,
        }
        await db.refresh(message)
        assert message.resolution["payload_hash"]
        assert delivery.external_reply_approval_claim_is_active(message)

        await delivery.release_external_reply_approval_claim(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            claim_id="stale-claim",
        )
        await db.refresh(message)
        assert delivery.external_reply_approval_claim_is_active(message)

        await delivery.release_external_reply_approval_claim(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            claim_id=str(first["claim_id"]),
        )
        await db.refresh(message)
        assert message.resolution is None
        reopened = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert reopened["acquired"] is True
        assert reopened["reason"] == "claimed"
        assert reopened["claim_id"] != first["claim_id"]

        marked = (
            await delivery.mark_external_reply_approval_same_key_retry_required(
                db,
                message_id=message.id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                claim_id=str(reopened["claim_id"]),
                error="provider response was ambiguous",
            )
        )
        assert marked is True
        await db.refresh(message)
        assert (
            delivery.external_reply_approval_claim_disposition(message)
            is delivery.ExternalReplyApprovalClaimDisposition.SAME_KEY_RETRY_REQUIRED
        )
        assert delivery.external_reply_approval_claim_conflict_reason(message) == (
            delivery.ExternalReplyApprovalClaimDisposition.IN_PROGRESS.value
        )

        same_key_retry = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-3",
        )
        assert same_key_retry["acquired"] is True
        assert same_key_retry["claim_id"] != reopened["claim_id"]

        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


async def test_external_reply_claim_rejects_changed_payload_and_action(
    monkeypatch,
    db_session,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    class IdempotentAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    monkeypatch.setitem(delivery.ADAPTERS, "claim_payload_channel", IdempotentAdapter())
    entity_id = "entity-claim-payload"
    workspace_id = "workspace-claim-payload"
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            "channel_type": "claim_payload_channel",
            "reply_text": "original reply",
            "action_key": "external_message.send",
        },
    )
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()

        first = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-1",
        )
        assert first["acquired"] is True
        await delivery.mark_external_reply_approval_same_key_retry_required(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            claim_id=str(first["claim_id"]),
            error="ambiguous provider response",
        )

        await db.refresh(message)
        changed_action = dict(message.pending_action)
        changed_action["reply_text"] = "changed reply"
        message.pending_action = changed_action
        await db.commit()

        changed = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert changed == {
            "acquired": False,
            "reason": "approval_payload_changed",
            "claim_id": None,
        }

        await db.refresh(message)
        message.resolution = None
        invalid_action = dict(message.pending_action)
        invalid_action["action_key"] = "workspace.file.delete"
        message.pending_action = invalid_action
        await db.commit()
        invalid = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert invalid == {
            "acquired": False,
            "reason": "approval_action_mismatch",
            "claim_id": None,
        }

        await db.refresh(message)
        message.resolution = None
        missing_action = dict(message.pending_action)
        missing_action.pop("action_key", None)
        message.pending_action = missing_action
        await db.commit()
        missing = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert missing == {
            "acquired": False,
            "reason": "approval_action_mismatch",
            "claim_id": None,
        }

        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


async def test_approval_quarantine_cannot_overwrite_successor_claim(
    db_session,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    entity_id = "entity-quarantine-fence"
    workspace_id = "workspace-quarantine-fence"
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            "action_key": "external_message.send",
        },
        resolution={
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
            ),
            "attempt_id": "placeholder",
            "claim_id": "claim-2",
        },
    )
    message.resolution = {
        **message.resolution,
        "attempt_id": message.id,
    }
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()

        stale_persisted = (
            await delivery._persist_external_reply_approval_outcome_unknown(
                db,
                message_id=message.id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                claim_id="claim-1",
                error="stale provider result",
            )
        )
        assert stale_persisted is False
        await db.refresh(message)
        assert message.resolution["claim_id"] == "claim-2"
        assert message.resolution["status"] == (
            delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
        )

        owner_persisted = (
            await delivery._persist_external_reply_approval_outcome_unknown(
                db,
                message_id=message.id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                claim_id="claim-2",
                error="provider result unknown",
            )
        )
        assert owner_persisted is True
        await db.refresh(message)
        assert message.resolution["status"] == (
            delivery.ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value
        )
        assert delivery.external_reply_approval_claim_owner_conflict_reason(
            message,
            "claim-2",
        ) is None
        assert delivery.external_reply_approval_claim_owner_conflict_reason(
            message,
            "claim-1",
        ) == delivery.ExternalReplyApprovalClaimDisposition.OUTCOME_UNKNOWN.value

        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


async def test_stale_at_least_once_claim_without_attempt_can_recover(
    monkeypatch,
    db_session,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    entity_id = "entity-stale-claim-test"
    workspace_id = "workspace-stale-claim-test"
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            "channel_type": "non_idempotent_test_channel",
            "action_key": "external_message.send",
        },
    )
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()

        first = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-1",
        )
        assert first["acquired"] is True
        assert first["retry_mode"] is ChannelTextSendRetryMode.AT_LEAST_ONCE

        class UpgradedAdapter:
            text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        monkeypatch.setitem(
            delivery.ADAPTERS,
            "non_idempotent_test_channel",
            UpgradedAdapter(),
        )

        await db.refresh(message)
        stale_resolution = dict(message.resolution)
        stale_resolution["claimed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        message.resolution = stale_resolution
        await db.commit()

        second = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert second["acquired"] is True
        assert second["claim_id"] != first["claim_id"]
        assert second["retry_mode"] is ChannelTextSendRetryMode.AT_LEAST_ONCE

        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


@pytest.mark.parametrize(
    ("attempt_status", "expected_acquired", "expected_reason"),
    [
        ("sent", True, "claimed"),
        ("processing", False, "approval_delivery_outcome_unknown"),
    ],
)
async def test_stale_at_least_once_claim_uses_durable_attempt_evidence(
    db_session,
    attempt_status: str,
    expected_acquired: bool,
    expected_reason: str,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.channel import MessageLog
    from packages.core.models.task import Conversation, Message

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    pending_action = {
        "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
        "action_key": "external_message.send",
        "channel_type": "attempt_evidence_channel",
        "channel_config_id": generate_ulid(),
        "channel_conversation_id": generate_ulid(),
        "chat_id": "recipient-attempt-evidence",
        "reply_text": "approved reply",
    }
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action=pending_action,
    )
    message_id = message.id
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()
        first = await delivery.claim_external_reply_approval(
            db,
            message_id=message_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-1",
        )
        assert first["acquired"] is True

        db.add(MessageLog(
            id=message_id,
            entity_id=entity_id,
            channel_config_id=pending_action["channel_config_id"],
            conversation_id=pending_action["channel_conversation_id"],
            direction="outbound",
            channel_type=pending_action["channel_type"],
            to_address=pending_action["chat_id"],
            content=pending_action["reply_text"],
            status=attempt_status,
        ))
        await db.refresh(message)
        stale_resolution = dict(message.resolution)
        stale_resolution["claimed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        message.resolution = stale_resolution
        await db.commit()

        second = await delivery.claim_external_reply_approval(
            db,
            message_id=message_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert second["acquired"] is expected_acquired
        assert second["reason"] == expected_reason

        persisted_attempt = await db.get(MessageLog, message_id)
        if persisted_attempt is not None:
            await db.delete(persisted_attempt)
        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


async def test_stale_idempotent_claim_allows_approve_takeover_but_blocks_reject(
    monkeypatch,
    db_session,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    class IdempotentAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    monkeypatch.setitem(delivery.ADAPTERS, "idempotent_test_channel", IdempotentAdapter())
    entity_id = "entity-stale-idem-test"
    workspace_id = "workspace-stale-idem-test"
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Approve this external reply?",
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            "channel_type": "idempotent_test_channel",
            "action_key": "external_message.send",
        },
    )
    async with async_session() as db:
        db.add_all((conversation, message))
        await db.commit()

        first = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-1",
        )
        assert first["acquired"] is True
        assert first["retry_mode"] is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        class DowngradedAdapter:
            text_send_retry_mode = ChannelTextSendRetryMode.AT_LEAST_ONCE

        monkeypatch.setitem(
            delivery.ADAPTERS,
            "idempotent_test_channel",
            DowngradedAdapter(),
        )

        await db.refresh(message)
        stale_resolution = dict(message.resolution)
        stale_resolution["claimed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        message.resolution = stale_resolution
        await db.commit()
        await db.refresh(message)

        assert (
            delivery.external_reply_approval_claim_disposition(message)
            is delivery.ExternalReplyApprovalClaimDisposition.STALE_RETRYABLE
        )
        assert delivery.external_reply_approval_claim_conflict_reason(message) == (
            "approval_delivery_in_progress"
        )

        second = await delivery.claim_external_reply_approval(
            db,
            message_id=message.id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id="approver-2",
        )
        assert second["acquired"] is True
        assert second["claim_id"] != first["claim_id"]

        await db.delete(message)
        await db.delete(conversation)
        await db.commit()


async def test_unknown_delivery_claim_is_terminal_for_channel_callback() -> None:
    from packages.core.services import notification_workspace_callbacks as callbacks

    result = callbacks._approval_claim_conflict_result(
        "approval_delivery_outcome_unknown"
    )

    assert result["ok"] is False
    assert result["disposition"] == "terminal_failure"
    assert result["error"] == "approval_delivery_outcome_unknown"
    assert "manual reconciliation" in result["message"]


async def test_approved_reply_fails_before_persistence_when_route_was_revoked(
    monkeypatch,
) -> None:
    async def route_is_inactive(**_kwargs) -> bool:
        return False

    class UnexpectedAdapter:
        async def send_text(self, *_args, **_kwargs):
            raise AssertionError("A revoked approval route must not send")

    monkeypatch.setattr(
        delivery,
        "channel_reply_route_is_active",
        route_is_inactive,
    )
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", UnexpectedAdapter())

    result = await delivery.deliver_approved_external_reply(
        object(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="stale approved reply",
        agent_id="agent-1",
        agent_subscription_id="subscription-1",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
    )

    assert result == {
        "disposition": "terminal_route_invalid",
        "ok": False,
        "sent": False,
        "error": "reply_route_unavailable",
    }


async def test_approved_reply_delivery_disposition_fails_closed() -> None:
    assert (
        delivery.approved_external_reply_delivery_disposition({
            "disposition": "accepted",
            "ok": True,
            "sent": True,
            "message_log_id": "log-1",
        })
        is delivery.ApprovedExternalReplyDeliveryDisposition.ACCEPTED
    )
    assert (
        delivery.approved_external_reply_delivery_disposition({
            "ok": True,
            "sent": True,
        })
        is delivery.ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
    )
    assert (
        delivery.approved_external_reply_delivery_disposition({
            "disposition": "terminal_route_invalid",
            "ok": False,
            "sent": False,
            "error": "reply_route_unavailable",
        })
        is delivery.ApprovedExternalReplyDeliveryDisposition.TERMINAL_ROUTE_INVALID
    )
    assert (
        delivery.approved_external_reply_delivery_disposition({
            "disposition": "outcome_unknown",
            "ok": False,
            "sent": False,
            "reason": "provider_outcome_unknown",
            "message_log_id": "log-1",
            "replayed": True,
        })
        is delivery.ApprovedExternalReplyDeliveryDisposition.OUTCOME_UNKNOWN
    )
    for result in (
        {"ok": False, "sent": False, "error": "delivery_attempt_conflict"},
        {"ok": True, "sent": False, "reason": "no_adapter"},
        {},
    ):
        assert (
            delivery.approved_external_reply_delivery_disposition(result)
            is delivery.ApprovedExternalReplyDeliveryDisposition.RETRYABLE_FAILURE
        )


async def test_approved_reply_lock_failure_is_determinate(
    monkeypatch,
) -> None:
    async def fail_lock(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        delivery.TransactionAdvisoryLock,
        "try_acquire",
        fail_lock,
    )

    with pytest.raises(
        delivery.ApprovedExternalReplyDeliveryError,
        match="lock is unavailable",
    ):
        await delivery.deliver_approved_external_reply(
            object(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            idempotency_key="approval-message-1",
        )


async def test_approved_reply_refuses_downgraded_idempotency_contract(
    monkeypatch,
) -> None:
    calls = 0

    class DowngradedAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.AT_LEAST_ONCE

        async def send_text(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1

    async def existing_attempt(**_kwargs):
        return {
            "id": "approval-message-1",
            "status": "failed",
            "external_id": None,
            "error_message": "ambiguous provider response",
            "created": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "retry_mode": ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
        }

    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", existing_attempt)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", DowngradedAdapter())

    with pytest.raises(
        delivery.ApprovedExternalReplySameKeyRetryRequired,
        match="idempotency contract changed",
    ):
        await delivery.deliver_approved_external_reply(
            object(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
            retry_mode=ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
        )

    assert calls == 0


async def test_approved_reply_attempt_conflict_stays_retryable(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def no_existing_attempt(**_kwargs):
        return None

    async def conflicting_attempt(**_kwargs):
        return None

    class Adapter:
        text_send_retry_mode = ChannelTextSendRetryMode.AT_LEAST_ONCE

        async def send_text(self, *_args, **_kwargs):
            raise AssertionError("conflicting immutable intent must not send")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", no_existing_attempt)
    monkeypatch.setattr(delivery, "_ensure_approved_reply_attempt", conflicting_attempt)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", Adapter())

    with pytest.raises(
        delivery.ApprovedExternalReplyDeliveryError,
        match="delivery attempt conflicts",
    ):
        await delivery.deliver_approved_external_reply(
            object(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )


async def test_approved_reply_missing_adapter_stays_retryable(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def no_existing_attempt(**_kwargs):
        return None

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", no_existing_attempt)
    monkeypatch.delitem(delivery.ADAPTERS, "missing-adapter", raising=False)

    with pytest.raises(
        delivery.ApprovedExternalReplyDeliveryError,
        match="no channel adapter",
    ):
        await delivery.deliver_approved_external_reply(
            object(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="missing-adapter",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )


async def test_committed_provider_acceptance_survives_later_route_revocation(
    monkeypatch,
) -> None:
    async def existing_attempt(**_kwargs):
        return {
            "id": "approval-message-accepted",
            "status": "sent",
            "external_id": "provider-message-1",
            "error_message": None,
            "created": False,
        }

    async def unexpected_route_check(**_kwargs):
        raise AssertionError("terminal provider evidence must win over live route drift")

    projected: list[str] = []

    async def persist_message(*_args, **_kwargs):
        projected.append(_kwargs["content"])

    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", existing_attempt)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", unexpected_route_check)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)

    result = await delivery.deliver_approved_external_reply(
        object(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="already accepted reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-accepted",
    )

    assert result["sent"] is True
    assert result["replayed"] is True
    assert result["external_id"] == "provider-message-1"
    assert projected == ["already accepted reply"]


async def test_approved_reply_passes_stable_idempotency_key_to_adapter(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    route_state: dict[str, bool] = {}

    class RecordingAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        def __init__(self) -> None:
            self.kwargs = None

        async def send_text(self, *_args, **kwargs):
            assert route_state.get("locked") is True
            self.kwargs = kwargs
            return {"status": "sent", "external_id": "provider-1"}

    adapter = RecordingAdapter()
    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )
    _stub_durable_attempt(monkeypatch, log)
    _stub_active_route_lease(monkeypatch, state=route_state)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", adapter)

    result = await delivery.deliver_approved_external_reply(
        SimpleNamespace(flush=persist_message),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
    )

    assert result["sent"] is True
    assert adapter.kwargs == {"idempotency_key": "approval-message-1"}
    assert route_state["locked"] is False


async def test_approved_reply_provider_failure_remains_retryable(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    log = SimpleNamespace(id="log-1", status="queued", error_message=None)

    class FakeDb:
        pass

    class FailingAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **kwargs):
            assert kwargs["idempotency_key"] == "approval-message-1"
            raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", FailingAdapter())

    db = FakeDb()
    with pytest.raises(
        delivery.ApprovedExternalReplySameKeyRetryRequired,
        match="temporary provider outage",
    ):
        await delivery.deliver_approved_external_reply(
            db,
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
            retry_mode=ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
        )

    assert log.status == "failed"
    assert log.error_message == "temporary provider outage"
    assert updates == ["processing", "failed"]


async def test_idempotent_soft_timeout_is_durable_and_immediately_retryable(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    log = SimpleNamespace(id="log-1", status="queued", error_message=None)

    class TimeoutAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **_kwargs):
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", TimeoutAdapter())

    with pytest.raises(delivery.ApprovedExternalReplyRetryableTimeout):
        await delivery.deliver_approved_external_reply(
            SimpleNamespace(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
            retry_mode=ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
        )

    assert log.status == "failed"
    assert "soft time limit" in log.error_message
    assert updates == ["processing", "failed"]


async def test_approved_reply_quarantines_ambiguous_non_idempotent_failure(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )

    class FakeDb:
        pass

    class AtLeastOnceAdapter:
        async def send_text(self, *_args, **kwargs):
            assert kwargs["idempotency_key"] == "approval-message-1"
            raise RuntimeError("provider outcome unknown")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", AtLeastOnceAdapter())

    db = FakeDb()
    result = await delivery.deliver_approved_external_reply(
        db,
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
    )

    assert result == {
        "disposition": "outcome_unknown",
        "ok": False,
        "sent": False,
        "reason": "provider_outcome_unknown",
        "error": "provider outcome unknown",
        "message_log_id": "log-1",
        "quarantine_persisted": True,
        "quarantine_source": "message_log",
    }
    assert log.status == "unknown"
    assert log.error_message == "provider outcome unknown"
    assert updates == ["processing", "unknown"]


async def test_approved_reply_quarantines_unknown_adapter_result(
    monkeypatch,
) -> None:
    projected: list[str] = []

    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **kwargs) -> None:
        projected.append(kwargs["content"])

    class UnknownAdapter:
        async def send_text(self, *_args, **_kwargs):
            return {"reason": "provider timed out"}

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", UnknownAdapter())

    result = await delivery.deliver_approved_external_reply(
        SimpleNamespace(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
        retry_mode=ChannelTextSendRetryMode.AT_LEAST_ONCE,
    )

    assert result["sent"] is False
    assert result["reason"] == "provider_outcome_unknown"
    assert result["error"] == "provider timed out"
    assert log.status == "unknown"
    assert updates == ["processing", "unknown"]
    assert projected == []


async def test_idempotent_unknown_adapter_result_retries_same_key(
    monkeypatch,
) -> None:
    projected: list[str] = []

    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **kwargs) -> None:
        projected.append(kwargs["content"])

    class UnknownAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **_kwargs):
            return {"status": "unknown", "reason": "provider timed out"}

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", UnknownAdapter())

    with pytest.raises(
        delivery.ApprovedExternalReplySameKeyRetryRequired,
        match="provider timed out",
    ):
        await delivery.deliver_approved_external_reply(
            SimpleNamespace(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
            retry_mode=ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
        )

    assert log.status == "failed"
    assert updates == ["processing", "failed"]
    assert projected == []


async def test_approved_reply_reopens_after_determinate_provider_rejection(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        return None

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )

    class RejectingAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **_kwargs):
            raise ChannelTextSendError.determinate("invalid recipient")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", RejectingAdapter())

    with pytest.raises(
        delivery.ApprovedExternalReplyDeliveryError,
        match="invalid recipient",
    ):
        await delivery.deliver_approved_external_reply(
            SimpleNamespace(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )

    assert log.status == "failed"
    assert log.error_message == "invalid recipient"
    assert updates == ["processing", "failed"]


async def test_approved_whatsapp_reply_preserves_template_required_failure(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def persist_message(*_args, **_kwargs) -> None:
        raise AssertionError("a rejected free-form reply must not be projected as sent")

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )

    class RejectingAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.AT_LEAST_ONCE

        async def send_text(self, *_args, **_kwargs):
            raise ChannelTextSendError.determinate(
                "An approved WhatsApp template is required outside the "
                "24-hour customer-service window.",
                reason_code="whatsapp_template_required",
            )

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setitem(delivery.ADAPTERS, "whatsapp", RejectingAdapter())

    with pytest.raises(delivery.ApprovedExternalReplyDeliveryError) as raised:
        await delivery.deliver_approved_external_reply(
            SimpleNamespace(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="whatsapp",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="late approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )

    assert raised.value.reason_code == "whatsapp_template_required"
    assert log.status == "failed"
    assert log.external_id is None
    assert log.error_message.startswith("whatsapp_template_required:")
    assert updates == ["processing", "failed"]


async def test_approved_reply_deferred_status_is_determinate_not_sent(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def persist_message(*_args, **_kwargs) -> None:
        raise AssertionError("a deferred adapter result must not project sent")

    class DeferredAdapter:
        async def send_text(self, *_args, **_kwargs):
            return {"status": "deferred", "reason": "provider call not started"}

    log = SimpleNamespace(
        id="log-deferred",
        status="queued",
        external_id=None,
        error_message=None,
    )
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", persist_message)
    monkeypatch.setitem(delivery.ADAPTERS, "twilio_voice", DeferredAdapter())

    with pytest.raises(
        delivery.ApprovedExternalReplyDeliveryError,
        match="provider call not started",
    ):
        await delivery.deliver_approved_external_reply(
            SimpleNamespace(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="twilio_voice",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )

    assert log.status == "failed"
    assert updates == ["processing", "failed"]


async def test_email_missing_smtp_config_is_typed_determinate(
    monkeypatch,
) -> None:
    from packages.core.services.channels.email_adapter import EmailChannelAdapter

    adapter = EmailChannelAdapter()

    async def incomplete_credentials(*_args, **_kwargs):
        return {"username": "sender@example.com"}

    monkeypatch.setattr(adapter, "credentials", incomplete_credentials)
    with pytest.raises(ChannelTextSendError) as raised:
        await adapter.send_text(
            SimpleNamespace(),
            "recipient@example.com",
            "approved reply",
        )
    assert raised.value.disposition.value == "determinate"


async def test_approved_reply_attempt_persists_frozen_retry_mode(
    db_session,
) -> None:
    from packages.core.models.channel import MessageLog

    attempt_id = generate_ulid()
    attempt = await delivery._ensure_approved_reply_attempt(
        attempt_id=attempt_id,
        entity_id="entity-frozen-mode",
        channel_config_id="config-frozen-mode",
        conversation_id="conversation-frozen-mode",
        channel_type="slack",
        to_address="recipient-frozen-mode",
        content="approved reply",
        retry_mode=ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT,
    )
    assert attempt is not None
    assert attempt["retry_mode"] is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    loaded = await delivery._load_approved_reply_attempt(
        attempt_id=attempt_id,
        entity_id="entity-frozen-mode",
        channel_config_id="config-frozen-mode",
        conversation_id="conversation-frozen-mode",
        channel_type="slack",
        to_address="recipient-frozen-mode",
        content="approved reply",
    )
    assert loaded is not None
    assert loaded["retry_mode"] is ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    async with async_session() as db:
        persisted = await db.get(MessageLog, attempt_id)
        assert persisted is not None
        assert persisted.attachments == {
            "approved_external_reply": {
                "version": 1,
                "retry_mode": "provider_idempotent",
            }
        }
        await db.delete(persisted)
        await db.commit()


async def test_transcript_failure_cannot_overwrite_provider_acceptance(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def fail_projection(*_args, **_kwargs) -> None:
        raise RuntimeError("chat projection unavailable")

    class IdempotentAdapter:
        text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

        async def send_text(self, *_args, **_kwargs):
            return {"status": "sent", "external_id": "provider-message-1"}

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )
    updates = _stub_durable_attempt(monkeypatch, log)
    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", fail_projection)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", IdempotentAdapter())

    result = await delivery.deliver_approved_external_reply(
        object(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
    )

    assert result["sent"] is True
    assert result["transcript_projected"] is False
    assert log.status == "sent"
    assert updates == ["processing", "sent"]


async def test_at_least_once_acceptance_with_missing_receipt_is_quarantined(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def unexpected_message(*_args, **_kwargs) -> None:
        raise AssertionError("a missing durable receipt must not project sent")

    calls = 0

    class AtLeastOnceAdapter:
        async def send_text(self, *_args, **kwargs):
            nonlocal calls
            calls += 1
            assert kwargs["idempotency_key"] == "approval-message-1"
            return {"status": "sent", "external_id": "provider-message-1"}

    log = SimpleNamespace(
        id="log-1",
        status="queued",
        external_id=None,
        error_message=None,
    )

    async def no_existing_attempt(**_kwargs):
        return None

    attempt_exists = True
    ensure_calls = 0

    async def ensure_attempt(**_kwargs):
        nonlocal attempt_exists, ensure_calls
        ensure_calls += 1
        attempt_exists = True
        return {
            "id": log.id,
            "status": log.status,
            "external_id": log.external_id,
            "error_message": log.error_message,
            "created": True,
        }

    updates: list[str] = []

    async def update_attempt(
        _attempt_id,
        *,
        status,
        error_message=None,
        external_id=None,
        **_kwargs,
    ):
        nonlocal attempt_exists
        updates.append(status)
        if status == "sent":
            attempt_exists = False
            return None
        if not attempt_exists:
            return None
        log.status = status
        log.error_message = error_message
        log.external_id = external_id
        return {
            "id": log.id,
            "status": log.status,
            "external_id": log.external_id,
            "error_message": log.error_message,
            "created": False,
        }

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "add_channel_assistant_message", unexpected_message)
    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", no_existing_attempt)
    monkeypatch.setattr(delivery, "_ensure_approved_reply_attempt", ensure_attempt)
    monkeypatch.setattr(delivery, "_update_approved_reply_attempt", update_attempt)
    _stub_active_route_lease(monkeypatch)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", AtLeastOnceAdapter())

    result = await delivery.deliver_approved_external_reply(
        object(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
    )

    assert result == {
        "disposition": "outcome_unknown",
        "ok": False,
        "sent": False,
        "reason": "provider_outcome_unknown",
        "error": "provider accepted the reply but its durable receipt disappeared",
        "message_log_id": "log-1",
        "quarantine_persisted": True,
        "quarantine_source": "message_log",
    }
    assert calls == 1
    assert ensure_calls == 2
    assert updates == ["processing", "sent", "unknown", "unknown"]


async def test_ambiguous_send_fails_closed_when_no_quarantine_can_persist(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def no_existing_attempt(**_kwargs):
        return None

    async def ensure_attempt(**_kwargs):
        return {
            "id": "log-1",
            "status": "queued",
            "external_id": None,
            "error_message": None,
            "created": True,
        }

    async def update_attempt(_attempt_id, *, status, **_kwargs):
        if status == "processing":
            return {
                "id": "log-1",
                "status": status,
                "external_id": None,
                "error_message": None,
                "created": False,
            }
        return None

    async def cannot_persist_fallback(*_args, **_kwargs) -> bool:
        return False

    calls = 0

    class AmbiguousAdapter:
        async def send_text(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider outcome unknown")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", no_existing_attempt)
    monkeypatch.setattr(delivery, "_ensure_approved_reply_attempt", ensure_attempt)
    monkeypatch.setattr(delivery, "_update_approved_reply_attempt", update_attempt)
    _stub_active_route_lease(monkeypatch)
    monkeypatch.setattr(
        delivery,
        "_persist_external_reply_approval_outcome_unknown",
        cannot_persist_fallback,
    )
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", AmbiguousAdapter())

    with pytest.raises(delivery.ApprovedExternalReplyOutcomeUnknownError):
        await delivery.deliver_approved_external_reply(
            object(),
            entity_id="entity-1",
            channel_config_id="config-1",
            channel_type="telegram",
            channel_binding_id="binding-1",
            channel_contact_id="contact-1",
            channel_conversation_id="conversation-1",
            chat_id="recipient-1",
            text="approved reply",
            route_snapshot={
                "version": 1,
                "config_workspace_id": "workspace-1",
                "binding_workspace_id": "workspace-1",
                "runtime_workspace_id": "workspace-1",
            },
            workspace_id="workspace-1",
            idempotency_key="approval-message-1",
        )

    assert calls == 1


async def test_interrupted_at_least_once_attempt_is_not_sent_again(
    monkeypatch,
) -> None:
    async def route_is_active(**_kwargs) -> bool:
        return True

    async def load_config(*_args, **_kwargs):
        return SimpleNamespace(id="config-1")

    async def existing_attempt(**_kwargs):
        return {
            "id": "approval-message-1",
            "status": "processing",
            "external_id": None,
            "error_message": None,
            "created": False,
        }

    updates: list[str] = []

    async def update_attempt(_attempt_id, *, status, **_kwargs):
        updates.append(status)
        return {
            "id": "approval-message-1",
            "status": status,
            "external_id": None,
            "error_message": _kwargs.get("error_message"),
            "created": False,
        }

    class UnexpectedAdapter:
        async def send_text(self, *_args, **_kwargs):
            raise AssertionError("ambiguous at-least-once delivery must not retry")

    monkeypatch.setattr(delivery, "channel_reply_route_is_active", route_is_active)
    monkeypatch.setattr(delivery, "_load_routable_channel_config", load_config)
    monkeypatch.setattr(delivery, "_load_approved_reply_attempt", existing_attempt)
    monkeypatch.setattr(delivery, "_ensure_approved_reply_attempt", existing_attempt)
    monkeypatch.setattr(delivery, "_update_approved_reply_attempt", update_attempt)
    monkeypatch.setitem(delivery.ADAPTERS, "telegram", UnexpectedAdapter())

    result = await delivery.deliver_approved_external_reply(
        object(),
        entity_id="entity-1",
        channel_config_id="config-1",
        channel_type="telegram",
        channel_binding_id="binding-1",
        channel_contact_id="contact-1",
        channel_conversation_id="conversation-1",
        chat_id="recipient-1",
        text="approved reply",
        route_snapshot={
            "version": 1,
            "config_workspace_id": "workspace-1",
            "binding_workspace_id": "workspace-1",
            "runtime_workspace_id": "workspace-1",
        },
        workspace_id="workspace-1",
        idempotency_key="approval-message-1",
    )

    assert result["reason"] == "provider_outcome_unknown"
    assert result["replayed"] is True
    assert updates == ["unknown"]


async def test_channel_always_approve_sends_and_creates_standing_grant(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks
    from packages.core.workspace_chat import service as chat_service

    msg = SimpleNamespace(
        id="approval-message-standing",
        conversation_id="conversation-1",
        resolved_at=None,
        resolution=None,
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL,
            "action_key": "external_message.send",
            "channel_config_id": "config-1",
        },
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

        async def commit(self):
            return None

        async def rollback(self):
            return None

    db = FakeDb()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    async def claim_delivery(*_args, **_kwargs):
        msg.resolution = {
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
            ),
            "attempt_id": msg.id,
            "claim_id": "claim-1",
        }
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": dict(msg.pending_action),
        }

    async def deliver(*_args, **_kwargs):
        return {
            "disposition": "accepted",
            "ok": True,
            "sent": True,
            "message_log_id": "log-1",
        }

    grants: list[dict] = []

    async def add_grant(_db, **kwargs):
        grants.append(kwargs)

    resolutions: list[dict] = []

    async def resolve_pending_action(_db, *, resolution, **_kwargs):
        resolutions.append(resolution)
        msg.resolved_at = datetime.now(timezone.utc)
        return msg

    permission_checks: list[str] = []

    async def allow(*_args, **_kwargs):
        permission_key = _kwargs.get("permission_key")
        if permission_key:
            permission_checks.append(permission_key)
        return True

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", deliver)
    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim_delivery)
    monkeypatch.setattr(chat_service, "resolve_pending_action", resolve_pending_action)
    monkeypatch.setattr("packages.core.governance.add_auto_approve_action", add_grant)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", allow)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "always_approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "owner-1"},
        },
    )

    assert result["ok"] is True
    assert grants == [{
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "action_key": "external_message.send",
        "resource_id": "config-1",
        "changed_by": "owner-1",
    }]
    assert permission_checks == [
        "approve_external_publish",
        "manage_standing_grants",
    ]
    assert resolutions == [{"choice": "always_approve"}]


async def test_channel_approval_consumes_owner_quarantine_fallback(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks
    from packages.core.workspace_chat import service as chat_service

    msg = SimpleNamespace(
        id="approval-message-quarantined",
        conversation_id="conversation-1",
        resolved_at=None,
        resolution=None,
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL,
            "action_key": "external_message.send",
            "channel_config_id": "config-1",
        },
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

        async def commit(self):
            return None

        async def rollback(self):
            return None

    db = FakeDb()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    async def claim_delivery(*_args, **_kwargs):
        msg.resolution = {
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
            ),
            "attempt_id": msg.id,
            "claim_id": "claim-1",
        }
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": dict(msg.pending_action),
        }

    async def deliver(*_args, **_kwargs):
        msg.resolution = {
            **msg.resolution,
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_OUTCOME_UNKNOWN.value
            ),
            "outcome_error": "provider outcome unknown",
        }
        return {
            "disposition": "outcome_unknown",
            "ok": False,
            "sent": False,
            "reason": "provider_outcome_unknown",
            "error": "provider outcome unknown",
            "message_log_id": msg.id,
            "quarantine_persisted": True,
            "quarantine_source": "approval_claim",
        }

    resolutions: list[dict] = []

    async def resolve_pending_action(_db, *, resolution, **_kwargs):
        resolutions.append(resolution)
        msg.resolved_at = datetime.now(timezone.utc)
        return msg

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", deliver)
    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim_delivery)
    monkeypatch.setattr(chat_service, "resolve_pending_action", resolve_pending_action)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", allow)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "owner-1"},
        },
    )

    assert result["ok"] is True
    assert resolutions == [{
        "choice": "approve",
        "delivery_outcome": "unknown",
        "message_log_id": msg.id,
        "delivery_error": "provider outcome unknown",
    }]


async def test_channel_always_approve_requires_standing_grant_authority(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks

    msg = SimpleNamespace(
        id="approval-message-no-standing-authority",
        conversation_id="conversation-1",
        resolved_at=None,
        resolution=None,
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL,
            "action_key": "external_message.send",
            "channel_config_id": "config-1",
        },
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="editor-1",
                entity_id="entity-1",
                role="member",
                status="active",
            ),
            SimpleNamespace(role="editor", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    async def can_read(*_args, **_kwargs):
        return True

    permission_checks: list[str] = []

    async def can_participate(*_args, **kwargs):
        permission_key = kwargs["permission_key"]
        permission_checks.append(permission_key)
        return permission_key == "approve_external_publish"

    async def unexpected_claim(*_args, **_kwargs):
        raise AssertionError("standing authority must be checked before provider I/O")

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        can_read,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", can_participate)
    monkeypatch.setattr(
        delivery,
        "claim_external_reply_approval",
        unexpected_claim,
    )

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "always_approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "editor-1"},
        },
    )

    assert result == {
        "ok": False,
        "error": "standing_grant_authority_required",
    }
    assert permission_checks == [
        "approve_external_publish",
        "manage_standing_grants",
    ]


async def test_channel_approval_callback_cannot_consume_successor_claim(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks
    from packages.core.workspace_chat import service as chat_service

    msg = SimpleNamespace(
        id="approval-message-stale-owner",
        conversation_id="conversation-1",
        resolved_at=None,
        resolution=None,
        pending_action={
            "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL,
            "action_key": "external_message.send",
            "channel_config_id": "config-1",
        },
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    async def claim_delivery(*_args, **_kwargs):
        msg.resolution = {
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
            ),
            "attempt_id": msg.id,
            "claim_id": "claim-1",
        }
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": dict(msg.pending_action),
        }

    async def deliver(*_args, **_kwargs):
        msg.resolution = {
            "status": (
                delivery.ExternalReplyApprovalClaimStatus.DELIVERY_IN_PROGRESS.value
            ),
            "attempt_id": msg.id,
            "claim_id": "claim-2",
        }
        return {
            "disposition": "accepted",
            "ok": True,
            "sent": True,
            "message_log_id": "log-1",
        }

    async def allow(*_args, **_kwargs):
        return True

    resolved: list[str] = []

    async def resolve_pending_action(_db, *, message_id, **_kwargs):
        resolved.append(message_id)
        return msg

    grants: list[dict] = []

    async def add_grant(_db, **kwargs):
        grants.append(kwargs)

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", deliver)
    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim_delivery)
    monkeypatch.setattr(chat_service, "resolve_pending_action", resolve_pending_action)
    monkeypatch.setattr("packages.core.governance.add_auto_approve_action", add_grant)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", allow)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "always_approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "owner-1"},
        },
    )

    assert result["ok"] is False
    assert result["error"] == "approval_delivery_in_progress"
    assert grants == []
    assert resolved == []


async def test_channel_approval_callback_surfaces_template_required_and_releases_claim(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks
    from packages.core.workspace_chat import service as chat_service

    msg = SimpleNamespace(
        id="approval-message-1",
        conversation_id="conversation-1",
        resolved_at=None,
        pending_action={"kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL},
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        committed = False
        rolled_back = False
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    db = FakeDb()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    resolved: list[str] = []

    async def resolve_pending_action(_db, *, message_id, **_kwargs):
        resolved.append(message_id)
        return msg

    async def fail_delivery(*_args, **_kwargs):
        raise delivery.ApprovedExternalReplyDeliveryError(
            "whatsapp_template_required: approved template required",
            reason_code="whatsapp_template_required",
        )

    released: list[str] = []

    async def claim_delivery(*_args, **_kwargs):
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": dict(msg.pending_action),
        }

    async def release_delivery(_db, *, message_id, **_kwargs):
        released.append(message_id)
        await _db.commit()

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(chat_service, "resolve_pending_action", resolve_pending_action)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", fail_delivery)
    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim_delivery)
    monkeypatch.setattr(
        delivery,
        "release_external_reply_approval_claim",
        release_delivery,
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", allow)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "owner-1"},
        },
    )

    assert result["error"] == "whatsapp_template_required"
    assert result["disposition"] == "retryable"
    assert "approved WhatsApp template" in result["message"]
    assert resolved == []
    assert released == [msg.id]
    assert db.rolled_back is False
    assert db.committed is True


async def test_channel_approval_callback_marks_same_key_retry_and_propagates_timeout(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks

    msg = SimpleNamespace(
        id="approval-message-soft-timeout",
        conversation_id="conversation-1",
        resolved_at=None,
        pending_action={"kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL},
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
            msg,
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

        async def commit(self):
            return None

    db = FakeDb()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    async def claim_delivery(*_args, **_kwargs):
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": dict(msg.pending_action),
        }

    async def timeout_delivery(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    released: list[str] = []
    retry_required: list[tuple[str, str]] = []

    async def release_delivery(_db, *, message_id, **_kwargs):
        released.append(message_id)

    async def mark_retry_required(
        _db,
        *,
        message_id,
        error,
        **_kwargs,
    ):
        retry_required.append((message_id, error))
        return True

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", timeout_delivery)
    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim_delivery)
    monkeypatch.setattr(
        delivery,
        "release_external_reply_approval_claim",
        release_delivery,
    )
    monkeypatch.setattr(
        delivery,
        "mark_external_reply_approval_same_key_retry_required",
        mark_retry_required,
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", allow)

    with pytest.raises(SoftTimeLimitExceeded):
        await callbacks._resolve_message_via_chat_service(
            {
                "chat_message_id": msg.id,
                "entity_id": "entity-1",
                "workspace_id": "workspace-1",
            },
            "approve",
            {
                "entity_id": "entity-1",
                "responder": {"user_id": "owner-1"},
            },
        )

    assert released == []
    assert retry_required == [(msg.id, "external reply delivery timed out")]


async def test_channel_approval_callback_requires_current_external_authority(
    monkeypatch,
) -> None:
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.services import notification_workspace_callbacks as callbacks

    msg = SimpleNamespace(
        id="approval-message-2",
        conversation_id="conversation-2",
        resolved_at=None,
        pending_action={"kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL},
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="member-1",
                entity_id="entity-1",
                role="member",
                status="active",
            ),
            SimpleNamespace(role="member", status="active", deleted_at=None),
            SimpleNamespace(
                id="workspace-1",
                entity_id="entity-1",
                status="active",
            ),
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

    db = FakeDb()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    async def allow_read(*_args, **_kwargs):
        return True

    async def deny_authority(*_args, **_kwargs):
        return False

    async def unexpected_delivery(*_args, **_kwargs):
        raise AssertionError("unauthorized callback must not deliver")

    monkeypatch.setattr(callbacks, "async_session", SessionContext)
    monkeypatch.setattr(callbacks, "_resolve_external_message_action", unexpected_delivery)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_by_identity",
        allow_read,
    )
    monkeypatch.setattr("packages.core.humans.participant_can", deny_authority)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": msg.id,
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "member-1"},
        },
    )

    assert result == {
        "ok": False,
        "error": "external_publish_authority_required",
    }


async def test_channel_approval_callback_rejects_explicit_inactive_membership(
    monkeypatch,
) -> None:
    from packages.core.services import notification_workspace_callbacks as callbacks

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDb:
        results = iter((
            SimpleNamespace(
                id="owner-1",
                entity_id="entity-1",
                role="owner",
                status="active",
            ),
            SimpleNamespace(role="owner", status="inactive", deleted_at=None),
        ))

        async def execute(self, _statement):
            return Result(next(self.results))

    class SessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(callbacks, "async_session", SessionContext)

    result = await callbacks._resolve_message_via_chat_service(
        {
            "chat_message_id": "approval-message-3",
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
        },
        "approve",
        {
            "entity_id": "entity-1",
            "responder": {"user_id": "owner-1"},
        },
    )

    assert result == {
        "ok": False,
        "error": "responder_membership_inactive",
    }
