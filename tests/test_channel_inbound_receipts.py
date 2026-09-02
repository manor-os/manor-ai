"""Regression coverage for durable webhook-to-Celery handoff claims."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.channel import MessageLog
from packages.core.services.channel_inbound_receipts import (
    DISCORD_GATEWAY_SOURCE,
    InboundDispatchClaimOutcome,
    InboundDispatchFailureOutcome,
    WEBHOOK_ROUTER_SOURCE,
    claim_inbound_dispatch,
    mark_inbound_dispatch_failed,
    mark_inbound_dispatch_published,
    read_dispatch_metadata,
)


@pytest.mark.asyncio
async def test_unpublished_claim_stays_provider_visible_until_publish_is_confirmed(
    db_session,
) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="durable-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="twilio_sms",
        external_id="SM-durable-claim",
        status="received",
        content="deliver me",
    )
    db_session.add(receipt)
    await db_session.commit()

    first = await claim_inbound_dispatch(
        config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        external_id=receipt.external_id,
    )
    pending_retry = await claim_inbound_dispatch(
        config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        external_id=receipt.external_id,
    )

    assert first.outcome is InboundDispatchClaimOutcome.ACQUIRED
    assert first.claim_id
    assert pending_retry.outcome is InboundDispatchClaimOutcome.PUBLISH_PENDING

    assert await mark_inbound_dispatch_published(
        message_log_id=receipt.id,
        claim_id=first.claim_id,
    )
    published_retry = await claim_inbound_dispatch(
        config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        external_id=receipt.external_id,
    )

    assert published_retry.outcome is InboundDispatchClaimOutcome.ALREADY_PUBLISHED
    assert published_retry.claim_id == first.claim_id


@pytest.mark.asyncio
async def test_stale_unpublished_claim_can_be_replaced(db_session) -> None:
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="stale-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-stale-claim",
        status="queued",
        content="retry me",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "dead-publisher",
                "claimed_at": stale_time.isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    replacement = await claim_inbound_dispatch(
        config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        external_id=receipt.external_id,
    )

    assert replacement.outcome is InboundDispatchClaimOutcome.ACQUIRED
    assert replacement.claim_id and replacement.claim_id != "dead-publisher"


@pytest.mark.asyncio
async def test_worker_rejects_superseded_router_claim(db_session, monkeypatch) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="fenced-worker-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-fenced-worker",
        status="queued",
        content="do not duplicate",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "current-claim",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    async def unexpected_dispatch(**_kwargs):
        raise AssertionError("a superseded claim must not reach the agent")

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", unexpected_dispatch)

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="superseded-claim",
        entity_id=receipt.entity_id,
        channel_config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        sender_id="sender",
        chat_id="thread",
        content=receipt.content,
    )

    assert result == {"status": "skipped", "reason": "stale_dispatch_claim"}


@pytest.mark.asyncio
async def test_same_worker_claim_resumes_after_worker_redelivery(
    db_session,
    monkeypatch,
) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="redelivered-worker-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-redelivered-worker",
        status="processing",
        content="resume this claim",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "same-celery-delivery",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    calls = 0

    async def dispatch(**_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", dispatch)

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="same-celery-delivery",
        entity_id=receipt.entity_id,
        channel_config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        sender_id="sender",
        chat_id="thread",
        content=receipt.content,
    )

    await db_session.refresh(receipt)
    assert result == {"status": "ok"}
    assert calls == 1
    assert receipt.status == "processed"


@pytest.mark.asyncio
async def test_worker_exception_releases_execution_fence_for_redelivery(
    db_session,
    monkeypatch,
) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-exception-redelivery",
        status="queued",
        content="retry after worker exception",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "exception-redelivery-claim",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    calls = 0

    async def dispatch(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("worker interrupted")
        return {"status": "ok"}

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", dispatch)
    kwargs = {
        "inbound_message_log_id": receipt.id,
        "dispatch_claim_id": "exception-redelivery-claim",
        "entity_id": receipt.entity_id,
        "channel_config_id": receipt.channel_config_id,
        "channel_type": receipt.channel_type,
        "sender_id": "sender",
        "chat_id": "thread",
        "content": receipt.content,
    }

    with pytest.raises(RuntimeError, match="worker interrupted"):
        await _dispatch_slack_inbound_once(**kwargs)
    result = await _dispatch_slack_inbound_once(**kwargs)

    await db_session.refresh(receipt)
    assert result == {"status": "ok"}
    assert calls == 2
    assert receipt.status == "processed"


@pytest.mark.asyncio
async def test_worker_rejects_receipt_from_another_entity(
    db_session,
    monkeypatch,
) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="receipt-owner-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-wrong-entity",
        status="queued",
        content="do not cross entities",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "entity-fenced-claim",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    from packages.core.services import channel_gateway
    from packages.core.tasks.channel_tasks import _dispatch_slack_inbound_once

    async def unexpected_dispatch(**_kwargs):
        raise AssertionError("a cross-entity receipt must not reach the agent")

    monkeypatch.setattr(channel_gateway, "dispatch_inbound", unexpected_dispatch)

    result = await _dispatch_slack_inbound_once(
        inbound_message_log_id=receipt.id,
        dispatch_claim_id="entity-fenced-claim",
        entity_id="task-supplied-other-entity",
        channel_config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        sender_id="sender",
        chat_id="thread",
        content=receipt.content,
    )

    await db_session.refresh(receipt)
    assert result == {"status": "skipped", "reason": "receipt_mismatch"}
    assert receipt.status == "failed"
    assert receipt.error_message == "Inbound dispatch task does not match receipt scope"
    metadata = receipt.attachments["_manor_inbound_dispatch"]
    assert metadata["failed_claim_id"] == "entity-fenced-claim"
    assert "claim_id" not in metadata


@pytest.mark.asyncio
async def test_failed_worker_claim_is_terminal_and_inspectable(db_session) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id="failed-claim-entity",
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="outlook",
        external_id="outlook-failed-claim",
        status="processing",
        content="time out",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "timed-out-claim",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    assert await mark_inbound_dispatch_failed(
        message_log_id=receipt.id,
        claim_id="timed-out-claim",
        error="channel dispatch soft time limit exceeded",
    ) is InboundDispatchFailureOutcome.FAILED
    assert await mark_inbound_dispatch_failed(
        message_log_id=receipt.id,
        claim_id="timed-out-claim",
        error="channel dispatch soft time limit exceeded",
    ) is InboundDispatchFailureOutcome.FAILED
    await db_session.refresh(receipt)
    metadata = read_dispatch_metadata(receipt.attachments)
    retry = await claim_inbound_dispatch(
        config_id=receipt.channel_config_id,
        channel_type=receipt.channel_type,
        external_id=receipt.external_id,
    )

    assert receipt.status == "failed"
    assert receipt.error_message == "channel dispatch soft time limit exceeded"
    assert metadata and "claim_id" not in metadata and "claimed_at" not in metadata
    assert retry.outcome is InboundDispatchClaimOutcome.FAILED


@pytest.mark.asyncio
async def test_exhausted_published_worker_claim_can_fail_after_release(db_session) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="discord",
        external_id="discord-failed-released-claim",
        status="queued",
        content="retry exhausted",
        attachments={
            "_manor_inbound_dispatch": {
                "source": WEBHOOK_ROUTER_SOURCE,
                "claim_id": "released-terminal-claim",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    assert await mark_inbound_dispatch_failed(
        message_log_id=receipt.id,
        claim_id="released-terminal-claim",
        error="reply_not_sent",
    )
    await db_session.refresh(receipt)

    assert receipt.status == "failed"
    assert receipt.error_message == "reply_not_sent"


@pytest.mark.asyncio
async def test_exhausted_gateway_worker_claim_can_fail_after_release(db_session) -> None:
    receipt = MessageLog(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        channel_config_id=generate_ulid(),
        direction="inbound",
        channel_type="discord",
        external_id="discord-failed-gateway-claim",
        status="queued",
        content="retry exhausted",
        attachments={
            "_manor_inbound_dispatch": {
                "source": DISCORD_GATEWAY_SOURCE,
                "claim_id": "gateway-terminal-claim",
            }
        },
    )
    db_session.add(receipt)
    await db_session.commit()

    assert await mark_inbound_dispatch_failed(
        message_log_id=receipt.id,
        claim_id="gateway-terminal-claim",
        error="reply_not_sent",
    )
    await db_session.refresh(receipt)

    assert receipt.status == "failed"
    assert receipt.error_message == "reply_not_sent"
