"""Slow providers must not consume queued leases or all foreground DB capacity."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core import database
from packages.core.database_runtime import DatabaseEngineKind
from packages.core.models.base import generate_ulid
from packages.core.models.event import EventLog
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.workspace import Workspace
from packages.core.services import event_emitter, notification_scheduler as scheduler, notify

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("client")]


@pytest_asyncio.fixture
async def limited_db(db_session, request, monkeypatch):
    from conftest import TEST_DATABASE_URL

    settings = SimpleNamespace(
        DATABASE_URL=TEST_DATABASE_URL, DATABASE_ECHO=False,
        DATABASE_POOL_MODE="sqlalchemy", DATABASE_POOL_SIZE=4,
        DATABASE_MAX_OVERFLOW=0, DATABASE_POOL_TIMEOUT=1,
        DATABASE_POOL_RECYCLE=1800, DATABASE_ADMISSION_LIMIT=getattr(request, "param", 1),
        DATABASE_MAX_WAITERS=4, DATABASE_ADMISSION_TIMEOUT=0.1,
        DATABASE_HOLD_WARN_SECONDS=5,
    )
    engine = database.create_database_engine(settings, kind=DatabaseEngineKind.APPLICATION, is_test_database=True)
    factory = database.create_session_factory(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", factory)
    try:
        yield engine.sync_engine._manor_database_runtime, factory
    finally:
        await engine.dispose()


async def seed(db, count=1):
    notifications, events = [], []
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    for i in range(count):
        notification = Notification(
            id=generate_ulid(), entity_id=generate_ulid(), user_id=generate_ulid(),
            type="task_failed", title=f"Capacity {i}", dispatch_status="dispatched",
        )
        event = NotificationOutboxEvent(
            id=generate_ulid(), notification_id=notification.id,
            available_at=now + timedelta(milliseconds=i), payload={}, status="pending",
        )
        db.add(notification)
        await db.flush()
        db.add(event)
        notifications.append(notification)
        events.append(event)
    await db.commit()
    return notifications, events


async def immediate(factory, notification_id):
    async with factory() as db:
        return await scheduler.dispatch_notification_outbox(db, notification_id=notification_id)


async def assert_pending(observer, ids):
    async with observer() as db:
        for event_id in ids:
            event = await db.get(NotificationOutboxEvent, event_id)
            assert event.status == "pending"
            assert event.attempt_count == 0 and event.claim_token is None


async def test_sweep_claims_only_the_notification_being_sent(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    _, events = await seed(db_session, 3)
    started = asyncio.Event()

    async def slow(**kwargs):
        assert await kwargs["before_external_target"]()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(notify, "dispatch_persisted_notification", slow)
    async with factory() as db:
        delivery = asyncio.create_task(scheduler.dispatch_due_notifications(db))
        try:
            await asyncio.wait_for(started.wait(), 2)
            observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
            await assert_pending(observer, [event.id for event in events[1:]])
        finally:
            delivery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await delivery
        assert not db.in_transaction()
        assert runtime.checked_out == 0
    async with factory() as db:
        first = await db.get(NotificationOutboxEvent, events[0].id)
        assert first.status == "processing" and first.attempt_count == 1


async def test_event_and_notification_queues_share_capacity(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    notifications, events = await seed(db_session)
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Capacity", status="active")
    domain_event = EventLog(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        event_type="document.uploaded", payload={}, external_delivery_status="pending",
        external_delivery_available_at=datetime.now(timezone.utc),
    )
    db_session.add_all([workspace, domain_event])
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_event(*args, **kwargs):
        assert runtime.checked_out == 1
        started.set()
        await release.wait()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", slow_event)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    provider = AsyncMock()
    monkeypatch.setattr(notify, "dispatch_persisted_notification", provider)
    monkeypatch.setattr(scheduler, "_OUTBOX_ADMISSION_WAIT_SECONDS", 0.03)
    delivery = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(domain_event.id,)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (await immediate(factory, notifications[0].id))["due"] == 0
        observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
        await assert_pending(observer, [events[0].id])
        provider.assert_not_awaited()
    finally:
        release.set()
        assert (await delivery)["delivered"] == 1
    assert (await immediate(factory, notifications[0].id))["dispatched"] == 1
    assert runtime.checked_out == 0


@pytest.mark.parametrize("limited_db", [2, 3], indirect=True)
async def test_delivery_limit_reserves_a_foreground_transaction(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    count = runtime.policy.limit - 1
    notifications, events = await seed(db_session, count + 1)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow(**kwargs):
        assert await kwargs["before_external_target"]()
        calls.append(kwargs["notification_id"])
        if len(calls) == count:
            started.set()
        await release.wait()

    monkeypatch.setattr(notify, "dispatch_persisted_notification", slow)
    monkeypatch.setattr(scheduler, "_OUTBOX_ADMISSION_WAIT_SECONDS", 0.03)
    deliveries = [asyncio.create_task(immediate(factory, notification.id)) for notification in notifications[:count]]
    try:
        # NullPool reconnects across claim/authorization transactions. Observe
        # occupied delivery slots, not machine-dependent startup performance.
        await asyncio.wait_for(started.wait(), 10)
        assert runtime.checked_out == count
        async with factory() as foreground:
            assert await foreground.scalar(select(literal(1))) == 1
        assert (await immediate(factory, notifications[-1].id))["due"] == 0
        async with factory() as db:
            queued = await db.get(NotificationOutboxEvent, events[-1].id)
            assert queued.attempt_count == 0 and queued.status == "pending"
    finally:
        release.set()
        results = await asyncio.gather(*deliveries)
    assert all(result["dispatched"] == 1 for result in results)
    assert (await immediate(factory, notifications[-1].id))["dispatched"] == 1
    assert runtime.checked_out == 0


@pytest.mark.parametrize("attempts,outcome", [(0, "retried"), (4, "failed")])
async def test_attempt_deadline_releases_locks_and_records_bounded_retry(db_session, limited_db, monkeypatch, attempts, outcome):
    runtime, factory = limited_db
    notifications, events = await seed(db_session)
    events[0].attempt_count = attempts
    await db_session.commit()

    async def stuck(**kwargs):
        assert await kwargs["before_external_target"]()
        assert runtime.checked_out == 1
        await asyncio.Event().wait()

    monkeypatch.setattr(notify, "dispatch_persisted_notification", stuck)
    monkeypatch.setattr(scheduler, "_OUTBOX_ATTEMPT_TIMEOUT_SECONDS", 0.15)
    assert (await asyncio.wait_for(immediate(factory, notifications[0].id), 2))[outcome] == 1
    assert runtime.checked_out == 0
    async with factory() as db:
        saved = await db.scalar(select(NotificationOutboxEvent).where(NotificationOutboxEvent.id == events[0].id).with_for_update())
        assert saved.status == ("pending" if outcome == "retried" else "failed")
        assert saved.attempt_count == attempts + 1 and saved.claim_token is None
        assert "exceeded" in saved.last_error
        if outcome == "retried":
            assert saved.available_at > datetime.now(timezone.utc)
        assert (await db.get(Notification, notifications[0].id)).dispatch_status == "dispatched"


async def test_timeout_retains_completed_target_for_retry(db_session, limited_db, monkeypatch):
    _, factory = limited_db
    notifications, events = await seed(db_session)
    checkpointed = asyncio.Event()

    async def partial(**kwargs):
        assert await kwargs["before_external_target"]()
        await kwargs["on_target_delivered"]("first-target")
        checkpointed.set()
        assert await kwargs["before_external_target"]()
        await asyncio.Event().wait()

    monkeypatch.setattr(notify, "dispatch_persisted_notification", partial)
    # Allow DB authorization and checkpointing to finish before the provider stalls.
    monkeypatch.setattr(scheduler, "_OUTBOX_ATTEMPT_TIMEOUT_SECONDS", 1)
    assert (await immediate(factory, notifications[0].id))["retried"] == 1
    assert checkpointed.is_set()

    async def finish(**kwargs):
        assert kwargs["delivered_target_keys"] == {"first-target"}
        assert await kwargs["before_external_target"]()
        await kwargs["on_target_delivered"]("second-target")

    monkeypatch.setattr(notify, "dispatch_persisted_notification", finish)
    async with factory() as db:
        result = await scheduler.dispatch_due_notifications(db, now=datetime.now(timezone.utc) + timedelta(minutes=2))
        assert result["dispatched"] == 1
        saved = await db.get(NotificationOutboxEvent, events[0].id)
        assert set(saved.payload[notify._DELIVERED_TARGET_KEYS]) == {"first-target", "second-target"}
        assert saved.attempt_count == 2


@pytest.mark.parametrize("trigger,stalled", [("cancel", False), ("cancel", True), ("timeout", False), ("timeout", True)])
async def test_cancellation_drains_receipt_before_releasing_session(db_session, limited_db, monkeypatch, trigger, stalled):
    runtime, factory = limited_db
    notifications, events = await seed(db_session)
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = scheduler._record_delivered_target

    async def checkpoint(*args, **kwargs):
        started.set()
        try:
            await release.wait()
            return await original(*args, **kwargs)
        finally:
            stopped.set()

    async def sent(**kwargs):
        assert await kwargs["before_external_target"]()
        await kwargs["on_target_delivered"]("accepted-target")

    monkeypatch.setattr(scheduler, "_record_delivered_target", checkpoint)
    monkeypatch.setattr(notify, "dispatch_persisted_notification", sent)
    if stalled:
        monkeypatch.setattr(scheduler, "_OUTBOX_CHECKPOINT_DRAIN_SECONDS", 0.03)
    attempt_timeout = None
    draining = asyncio.Event()

    def observed_timeout(delay):
        nonlocal attempt_timeout
        scope = asyncio.timeout(delay)
        if delay == scheduler._OUTBOX_ATTEMPT_TIMEOUT_SECONDS:
            attempt_timeout = scope
        return scope

    async def observed_wait_for(awaitable, timeout):
        draining.set()
        return await asyncio.wait_for(awaitable, timeout)

    # Expire the real timeout only after the receipt starts, so database/CI
    # latency cannot cancel setup. Observe drain entry instead of racing sleeps.
    monkeypatch.setattr(scheduler, "asyncio", SimpleNamespace(
        **{**vars(asyncio), "timeout": observed_timeout, "wait_for": observed_wait_for},
    ))
    delivery = asyncio.create_task(immediate(factory, notifications[0].id))
    await asyncio.wait_for(started.wait(), 10)
    if trigger == "cancel":
        delivery.cancel()
    else:
        assert attempt_timeout is not None
        attempt_timeout.reschedule(asyncio.get_running_loop().time())
    await asyncio.wait_for(draining.wait(), 2)
    if not stalled:
        assert not delivery.done()
        release.set()
    if trigger == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(delivery, 2)
    else:
        assert (await asyncio.wait_for(delivery, 2))["retried"] == 1
    assert stopped.is_set() and runtime.checked_out == 0
    async with factory() as db:
        saved = await db.get(NotificationOutboxEvent, events[0].id)
        assert saved.status == ("processing" if trigger == "cancel" else "pending")
        assert bool(saved.payload.get(notify._DELIVERED_TARGET_KEYS)) is not stalled
    # A canceled send must not leak the shared delivery permit.
    monkeypatch.setattr(notify, "dispatch_persisted_notification", AsyncMock())
    async with factory() as db:
        result = await scheduler.dispatch_due_notifications(db, now=datetime.now(timezone.utc) + timedelta(minutes=6))
        assert result["dispatched"] == 1


async def test_sweep_budget_leaves_the_next_notification_unclaimed(db_session, limited_db, monkeypatch):
    _, factory = limited_db
    _, events = await seed(db_session, 2)

    async def slow(**kwargs):
        await asyncio.sleep(0.04)

    monkeypatch.setattr(notify, "dispatch_persisted_notification", slow)
    monkeypatch.setattr(scheduler, "_OUTBOX_SWEEP_BUDGET_SECONDS", 0.02)
    async with factory() as db:
        assert (await scheduler.dispatch_due_notifications(db))["dispatched"] == 1
    await assert_pending(factory, [events[1].id])


async def test_future_sweep_attempts_each_failed_event_only_once(db_session, limited_db, monkeypatch):
    _, factory = limited_db
    notifications, events = await seed(db_session, 3)
    provider = AsyncMock(side_effect=RuntimeError("synthetic outage"))
    monkeypatch.setattr(notify, "dispatch_persisted_notification", provider)
    async with factory() as db:
        result = await scheduler.dispatch_due_notifications(db, batch_size=3, now=datetime.now(timezone.utc) + timedelta(hours=1))
        assert result["due"] == result["retried"] == 3
        for event in events:
            assert (await db.get(NotificationOutboxEvent, event.id)).attempt_count == 1
    assert {call.kwargs["notification_id"] for call in provider.await_args_list} == {row.id for row in notifications}


async def test_canceled_waiter_does_not_claim_or_leak_capacity(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    notifications, events = await seed(db_session, 2)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        assert await kwargs["before_external_target"]()
        started.set()
        await release.wait()

    monkeypatch.setattr(notify, "dispatch_persisted_notification", slow)
    first = asyncio.create_task(immediate(factory, notifications[0].id))
    await asyncio.wait_for(started.wait(), 2)
    second = asyncio.create_task(immediate(factory, notifications[1].id))
    try:
        await asyncio.sleep(0.03)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
        await assert_pending(observer, [events[1].id])
    finally:
        release.set()
        assert (await first)["dispatched"] == 1
    assert (await immediate(factory, notifications[1].id))["dispatched"] == 1
    assert runtime.checked_out == 0


async def test_connection_bound_session_shares_the_engine_capacity(db_session, limited_db, monkeypatch):
    from packages.core.services.external_delivery_capacity import external_delivery_semaphore

    runtime, factory = limited_db
    notifications, events = await seed(db_session)
    provider = AsyncMock()
    monkeypatch.setattr(notify, "dispatch_persisted_notification", provider)
    monkeypatch.setattr(scheduler, "_OUTBOX_ADMISSION_WAIT_SECONDS", 0.03)
    async with factory.kw["bind"].connect() as connection:
        async with factory(bind=connection) as bound_session:
            async with external_delivery_semaphore(bound_session):
                assert (await immediate(factory, notifications[0].id))["due"] == 0
                provider.assert_not_awaited()
    await assert_pending(factory, [events[0].id])
    assert (await immediate(factory, notifications[0].id))["dispatched"] == 1
    assert runtime.checked_out == 0
