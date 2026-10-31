"""One transaction slot must retain lifecycle fencing and durable receipts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_action_transaction_boundaries import one_slot_db as one_slot_db
from packages.core import database
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact, MessageLog
from packages.core.models.event import EventLog
from packages.core.models.notification import Notification, NotificationDelivery, NotificationOutboxEvent
from packages.core.models.task import Task
from packages.core.models.user import Entity
from packages.core.models.webhook import WebhookDelivery, WebhookEndpoint
from packages.core.models.workspace import Workspace
from packages.core.services import channel_credentials, event_emitter
from tests.async_waits import LIVENESS_TIMEOUT_S, wait_for_signal


def install_runtime(monkeypatch, factory):
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(database, "engine", factory.kw["bind"])
    monkeypatch.setattr(channel_credentials, "async_session", factory)


async def queued_pause(observer_factory, workspace_id):
    started, acquired = asyncio.Event(), asyncio.Event()

    async def pause():
        async with observer_factory() as db:
            started.set()
            row = await db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
            acquired.set()
            row.status = "paused"
            await db.commit()

    task = asyncio.create_task(pause())
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.sleep(0.03)
    assert not acquired.is_set(), "Pause must wait until the active provider finishes"
    return task


def pending_event(workspace, event_type="document.uploaded", payload=None):
    return EventLog(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        event_type=event_type, payload=payload or {}, external_delivery_status="pending",
        external_delivery_available_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_workspace_webhooks_share_one_slot_and_recheck_pause(db_session, one_slot_db, monkeypatch, pause):
    from packages.core.services import webhook_service

    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Single-slot event", status="active")
    entry = pending_event(workspace)
    db_session.add_all([workspace, entry, *[
        WebhookEndpoint(entity_id=workspace.entity_id, url=f"https://test.invalid/{i}") for i in range(2)
    ]])
    await db_session.commit()
    observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
    calls, pauses = [], []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            assert runtime.checked_out == 1  # The required Workspace lock only.
            calls.append(url)
            if pause:
                pauses.append(await queued_pause(observer, workspace.id))
            return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(webhook_service.httpx, "AsyncClient", Client)
    result = await asyncio.wait_for(event_emitter.dispatch_due_external_events(event_ids=(entry.id,)), 3)
    await asyncio.gather(*pauses)
    assert result["canceled" if pause else "delivered"] == 1
    assert len(calls) == (1 if pause else 2)
    assert runtime.checked_out == 0
    async with factory() as db:
        receipts = (await db.scalars(select(WebhookDelivery).join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id).where(
            WebhookEndpoint.entity_id == workspace.entity_id,
        ))).all()
        assert len(receipts) == len(calls)
        assert all(receipt.status == "success" for receipt in receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped,pause", [(True, False), (True, True), (False, False)])
async def test_task_event_fanout_commits_each_receipt_with_one_slot(db_session, one_slot_db, monkeypatch, scoped, pause):
    from packages.core.services import email_service, task_event_notifications, task_external_notifications

    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    entity = Entity(id=generate_ulid(), name="Single-slot task event")
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Task event", status="active")
    task = Task(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id if scoped else None, title="Task", status="failed")
    channel = ChannelConfig(id=generate_ulid(), entity_id=entity.id, channel_type="slack", provider="slack",
                            config={"default_target": "synthetic-target"}, status="active")
    entity.settings = {"notification_policy": {"task_events": {
        "email": {"enabled": True},
        "external_chat": {"enabled": True, "channel_types": ["slack"], "channel_config_ids": [channel.id]},
    }}}
    entry = pending_event(workspace, "task.failed", {"task_id": task.id})
    if not scoped:
        entry.workspace_id = None
    db_session.add_all([entity, workspace, task, channel, entry])
    await db_session.commit()
    observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
    calls, pauses = [], []

    async def recipients(db, *args):
        await db.scalar(select(literal(1)))
        return task, [SimpleNamespace(id=str(n), email=f"slot-{n}@test.invalid") for n in range(2)]

    async def email(to, *args):
        assert runtime.checked_out == (1 if scoped else 0)
        calls.append(to)
        if pause:
            pauses.append(await queued_pause(observer, workspace.id))
        return True

    def lease(*args, **kwargs):
        assert runtime.checked_out <= 1
        return {"bot_token": "synthetic-token"}

    async def slack(*args):
        assert runtime.checked_out == (1 if scoped else 0)
        calls.append("slack")
        return True

    monkeypatch.setattr(task_event_notifications, "task_event_recipient_users", recipients)
    monkeypatch.setattr(email_service, "send_notification_email", email)
    monkeypatch.setattr(channel_credentials, "get_credential_service", lambda: SimpleNamespace(lease_channel_config=lease))
    monkeypatch.setattr(task_external_notifications, "_send_slack_bot_message", slack)
    result = await asyncio.wait_for(event_emitter.dispatch_due_external_events(event_ids=(entry.id,)), 3)
    await asyncio.gather(*pauses)
    assert result["canceled" if pause else "delivered"] == 1
    assert calls == (["slot-0@test.invalid"] if pause else ["slot-0@test.invalid", "slot-1@test.invalid", "slack"])
    async with factory() as db:
        saved = await db.get(EventLog, entry.id)
        assert saved.external_delivery_completed_sinks["webhook"] is True
        assert saved.external_delivery_completed_sinks["task_external.email:slot-0@test.invalid"] is True
        if not pause:
            assert saved.external_delivery_completed_sinks[f"task_external.external_chat:{channel.id}"] is True
            assert saved.external_delivery_completed_sinks["task_external"] is True


@pytest.mark.asyncio
async def test_cancel_during_borrowed_checkpoint_drains_before_session_close(db_session, one_slot_db, monkeypatch):
    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Cancel checkpoint", status="active")
    entry = pending_event(workspace, "task.failed")
    db_session.add_all([workspace, entry])
    await db_session.commit()
    checkpoint_started, checkpoint_release = asyncio.Event(), asyncio.Event()
    original = event_emitter._mark_external_sink_completed

    async def delayed_checkpoint(*args, **kwargs):
        checkpoint_started.set()
        await checkpoint_release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", AsyncMock())
    external = AsyncMock()
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", external)
    monkeypatch.setattr(event_emitter, "_mark_external_sink_completed", delayed_checkpoint)
    delivery = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(entry.id,)))
    await asyncio.wait_for(checkpoint_started.wait(), 2)
    delivery.cancel()
    await asyncio.sleep(0.02)
    assert not delivery.done()
    checkpoint_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(delivery, 2)
    external.assert_not_awaited()
    assert runtime.checked_out == 0
    async with factory() as db:
        saved = await db.get(EventLog, entry.id)
        assert saved.external_delivery_status == "processing"
        assert saved.external_delivery_completed_sinks == {"webhook": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["email", "telegram", "actionable"])
async def test_notification_authorization_adapter_and_receipt_share_one_slot(db_session, one_slot_db, monkeypatch, channel_type):
    from packages.core.services import email_service, notify, realtime
    from packages.core.services.auth_service import register_user
    from packages.core.services.channels import ADAPTERS

    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    user, entity = await register_user(db_session, email=f"slot-{generate_ulid().lower()}@test.invalid", password="StrongPassword123!")
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Notification", status="active")
    config = ChannelConfig(id=generate_ulid(), entity_id=entity.id, workspace_id=workspace.id,
                           channel_type="telegram", provider="telegram", status="active")
    contact = ChannelContact(entity_id=entity.id, channel_config_id=config.id, user_id=user.id,
                             channel_type="telegram", source_id="synthetic-chat", status="active")
    db_session.add_all([workspace, config, contact])
    await db_session.commit()
    sent = []

    def lease(*args, **kwargs):
        assert runtime.checked_out == 1
        return {"bot_token": "synthetic-token"}

    async def adapter(cc, to, text, **kwargs):
        creds = await channel_credentials.lease_channel_config_credentials(cc, reason="single-slot-test")
        assert creds["bot_token"] == "synthetic-token"
        assert runtime.checked_out == 1
        sent.append(to)
        return {"status": "sent", "external_id": "accepted"}

    async def email(to, *args):
        assert runtime.checked_out == 1
        sent.append(to)
        return True

    monkeypatch.setattr(channel_credentials, "get_credential_service", lambda: SimpleNamespace(lease_channel_config=lease))
    monkeypatch.setitem(ADAPTERS, "telegram", SimpleNamespace(send_text=adapter, send_actionable_message=adapter))
    monkeypatch.setattr(email_service, "send_notification_email", email)
    monkeypatch.setattr(realtime, "_redis_publish", AsyncMock())
    await asyncio.wait_for(notify.notify(
        entity_id=entity.id, user_id=user.id, workspace_id=workspace.id,
        type="task_failed", title="Test", body="Body",
        channels=["email" if channel_type == "email" else "telegram"],
        actions=[{"key": "approve", "label": "Approve"}] if channel_type == "actionable" else None,
    ), 3)
    assert sent == ([user.email] if channel_type == "email" else ["synthetic-chat"])
    async with factory() as db:
        event = await db.scalar(select(NotificationOutboxEvent).join(Notification).where(Notification.entity_id == entity.id))
        assert event.status == "delivered", event.last_error
        logs = (await db.scalars(select(MessageLog).where(MessageLog.entity_id == entity.id))).all()
        assert len(logs) == 1 and logs[0].status == "sent"
        if channel_type == "actionable":
            receipt = await db.scalar(select(NotificationDelivery).where(NotificationDelivery.entity_id == entity.id))
            assert receipt.status == "sent" and receipt.message_log_id == logs[0].id


@pytest.mark.asyncio
async def test_waiting_delivery_is_not_claimed_before_a_slot_is_available(db_session, one_slot_db, monkeypatch):
    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspaces = [Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Queued", status="active") for _ in range(2)]
    entries = [pending_event(workspace) for workspace in workspaces]
    db_session.add_all([*workspaces, *entries])
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    async def provider(entity_id, *args, **kwargs):
        assert runtime.checked_out == 1
        if entity_id == workspaces[0].entity_id:
            started.set()
            await release.wait()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    # The queued sweep gives up after its admission wait and leaves the row for
    # the next sweep. This test holds the slot through observer queries whose
    # duration depends on the runner, so its sweeps wait out the hold instead.
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_ADMISSION_WAIT_SECONDS", LIVENESS_TIMEOUT_S)
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_SWEEP_BUDGET_SECONDS", LIVENESS_TIMEOUT_S)
    first = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(entries[0].id,)))
    await wait_for_signal(started, first)
    second = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(entries[1].id,)))
    try:
        # Longer than admission's 100 ms timeout: the queued claim must not
        # compete for the connection or burn a delivery attempt during the wait.
        await asyncio.sleep(0.15)
        async with async_sessionmaker(db_session.bind, expire_on_commit=False)() as observer:
            saved = await observer.get(EventLog, entries[1].id)
            assert saved.external_delivery_status == "pending"
            assert saved.external_delivery_attempt_count == 0
            assert saved.external_delivery_claim_token is None
        assert not second.done()
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), LIVENESS_TIMEOUT_S)
    assert [result["delivered"] for result in results] == [1, 1]


@pytest.mark.asyncio
async def test_borrowed_credentials_refresh_a_previously_loaded_channel(db_session, one_slot_db, monkeypatch):
    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    config = ChannelConfig(entity_id=generate_ulid(), channel_type="telegram", provider="telegram", status="active",
                           credentials={"bot_token": "old-synthetic-token"})
    db_session.add(config)
    await db_session.commit()
    monkeypatch.setattr(channel_credentials, "get_credential_service", lambda: SimpleNamespace(
        lease_channel_config=lambda row, **kwargs: dict(row.credentials),
    ))
    async with factory() as db:
        cached = await db.get(ChannelConfig, config.id)
        await db.commit()
        config.credentials = {"bot_token": "new-synthetic-token"}
        await db_session.commit()
        with channel_credentials.use_channel_credential_session(db):
            creds = await channel_credentials.lease_channel_config_credentials(cached, reason="stale-channel-test")
        assert creds == {"bot_token": "new-synthetic-token"}
    assert runtime.checked_out == 0


@pytest.mark.asyncio
async def test_checkpoint_rejects_an_obsolete_claim_without_overwriting_receipts(db_session, one_slot_db, monkeypatch):
    _, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Fenced", status="active")
    entry = pending_event(workspace)
    entry.external_delivery_status = "processing"
    entry.external_delivery_claim_token = "current-claim"
    entry.external_delivery_completed_sinks = {"current": True}
    db_session.add_all([workspace, entry])
    await db_session.commit()
    stale = event_emitter._ExternalEventDeliveryClaim(
        event_id=entry.id, entity_id=entry.entity_id, workspace_id=workspace.id,
        event_type=entry.event_type, payload={}, completed_sinks={}, attempt_count=1, claim_token="old-claim",
    )
    async with factory() as db:
        with pytest.raises(event_emitter._ExternalEventDeliveryStale):
            await event_emitter._mark_external_sink_completed(stale, {}, "stale", db=db)
    async with factory() as db:
        assert (await db.get(EventLog, entry.id)).external_delivery_completed_sinks == {"current": True}


@pytest.mark.asyncio
async def test_stuck_checkpoint_has_a_bounded_cancellation_drain(db_session, one_slot_db, monkeypatch):
    runtime, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Stalled checkpoint", status="active")
    entry = pending_event(workspace)
    db_session.add_all([workspace, entry])
    await db_session.commit()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def blocked_checkpoint(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_CHECKPOINT_DRAIN_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(event_emitter, "_mark_external_sink_completed", blocked_checkpoint)
    monkeypatch.setattr(event_emitter, "deliver_webhook_event", AsyncMock())
    delivery = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(entry.id,)))
    await asyncio.wait_for(started.wait(), 2)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(delivery, 0.5)
    assert stopped.is_set() and runtime.checked_out == 0
    async with factory() as db:
        saved = await db.get(EventLog, entry.id)
        assert saved.external_delivery_status == "processing"
        assert not saved.external_delivery_completed_sinks


@pytest.mark.asyncio
async def test_future_sweep_attempts_each_event_only_once(db_session, one_slot_db, monkeypatch):
    _, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Retry sweep", status="active")
    entries = [pending_event(workspace, payload={"index": n}) for n in range(3)]
    db_session.add_all([workspace, *entries])
    await db_session.commit()
    attempted = []

    async def fail_provider(entity_id, event_type, payload, **kwargs):
        attempted.append(payload["index"])
        raise RuntimeError("synthetic provider outage")

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", fail_provider)
    result = await event_emitter.dispatch_due_external_events(
        event_ids=tuple(entry.id for entry in entries), batch_size=3,
        when=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert result["claimed"] == result["retried"] == 3
    assert sorted(attempted) == [0, 1, 2]
    async with factory() as db:
        for entry in entries:
            saved = await db.get(EventLog, entry.id)
            assert saved.external_delivery_attempt_count == 1
            assert saved.external_delivery_status == "pending"


@pytest.mark.asyncio
async def test_borrowed_credentials_recheck_a_cached_source_after_revocation(db_session, one_slot_db, monkeypatch):
    from packages.core.models.document import Integration
    from packages.core.services.auth_service import register_user

    _, factory = one_slot_db
    install_runtime(monkeypatch, factory)
    user, entity = await register_user(db_session, email=f"source-{generate_ulid().lower()}@test.invalid", password="StrongPassword123!")
    source = Integration(id=generate_ulid(), entity_id=entity.id, owner_user_id=user.id,
                         provider="slack", status="active", credentials={"token": "synthetic-token"})
    config = ChannelConfig(entity_id=entity.id, owner_user_id=user.id, channel_type="slack", provider="slack",
                           status="active", credential_source_kind="integration", credential_source_id=source.id)
    db_session.add_all([source, config])
    await db_session.commit()
    lease_calls = []

    def lease(*args, **kwargs):
        lease_calls.append(True)
        return {"token": "synthetic-token"}

    monkeypatch.setattr(channel_credentials, "get_credential_service", lambda: SimpleNamespace(lease_integration=lease))
    async with factory() as db:
        cached_source = await db.get(Integration, source.id)
        with channel_credentials.use_channel_credential_session(db):
            assert await channel_credentials.lease_channel_config_credentials(config, reason="source-test")
        source.status = "disabled"
        await db_session.commit()
        assert cached_source.status == "active"
        with channel_credentials.use_channel_credential_session(db):
            with pytest.raises(ValueError, match="unavailable"):
                await channel_credentials.lease_channel_config_credentials(config, reason="source-test")
    assert len(lease_calls) == 1
