import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.constants.notification_types import NotificationOutboxStatus
from packages.core.models.base import generate_ulid
from packages.core.models.event import EventLog
from packages.core.models.workspace import Workspace
from packages.core.services.event_emitter import (
    deliver_task_external_event,
    deliver_webhook_event,
    emit_in_session,
)


async def _yield_background_delivery() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def test_worker_runtime_drains_external_delivery_before_loop_closes(monkeypatch):
    from packages.core import database
    from packages.core.services import event_emitter
    from packages.core.tasks._runtime import run_in_worker

    deliveries = []
    dispose_calls = []

    class _Engine:
        async def dispose(self, close=True):
            dispose_calls.append(close)

    class _Session:
        info = {event_emitter._EXTERNAL_EVENT_QUEUE_KEY: ["event-1"]}

        @staticmethod
        def in_nested_transaction():
            return False

    async def capture_delivery(event_ids):
        await asyncio.sleep(0)
        deliveries.append(event_ids)

    async def worker_main():
        event_emitter._schedule_external_events_after_commit(_Session())

    monkeypatch.setattr(database, "engine", _Engine())
    monkeypatch.setattr(
        event_emitter,
        "_EXTERNAL_EVENT_DELIVERY_TASKS",
        set(),
    )
    monkeypatch.setattr(
        event_emitter,
        "_deliver_committed_external_events",
        capture_delivery,
    )

    run_in_worker(worker_main())

    assert deliveries == [("event-1",)]
    assert dispose_calls == [False, True]
    assert event_emitter._EXTERNAL_EVENT_DELIVERY_TASKS == set()


def test_worker_runtime_drains_emit_persistence_before_loop_closes(monkeypatch):
    from packages.core import database
    from packages.core.services import event_emitter
    from packages.core.tasks._runtime import run_in_worker

    persisted = []

    class _Engine:
        async def dispose(self, close=True):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    async def capture_emit(
        db,
        entity_id,
        event_type,
        *,
        source=None,
        payload=None,
        notify=True,
        deliver_after_commit=False,
    ):
        persisted.append(
            (db, entity_id, event_type, source, payload, deliver_after_commit)
        )

    async def worker_main():
        event_emitter.emit(
            "entity-worker",
            "task.failed",
            source="worker-test",
            payload={"task_id": "task-worker"},
        )

    monkeypatch.setattr(database, "engine", _Engine())
    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(event_emitter, "emit_in_session", capture_emit)
    monkeypatch.setattr(event_emitter, "_EVENT_PERSISTENCE_TASKS", set())
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_DELIVERY_TASKS", set())

    run_in_worker(worker_main())

    assert len(persisted) == 1
    assert persisted[0][1:] == (
        "entity-worker",
        "task.failed",
        "worker-test",
        {"task_id": "task-worker"},
        True,
    )
    assert event_emitter._EVENT_PERSISTENCE_TASKS == set()


@pytest.mark.asyncio
async def test_worker_external_delivery_drain_is_bounded(monkeypatch):
    from packages.core.services import event_emitter

    started = asyncio.Event()
    canceled = asyncio.Event()

    async def blocked_delivery():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(event_emitter, "_EVENT_PERSISTENCE_TASKS", set())
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_DELIVERY_TASKS", set())
    event_emitter._track_external_event_delivery(
        asyncio.create_task(blocked_delivery())
    )
    await started.wait()

    await event_emitter.drain_external_event_deliveries(
        persistence_timeout_seconds=0.01,
        delivery_timeout_seconds=0.01,
    )

    assert canceled.is_set()
    assert event_emitter._EXTERNAL_EVENT_DELIVERY_TASKS == set()


@pytest.mark.asyncio
async def test_emit_in_session_logs_and_notifies(monkeypatch):
    import packages.core.services.event_service as event_service
    import packages.core.services.task_event_notifications as task_notifications

    calls = []

    async def fake_log_event(db, entity_id, event_type, *, source=None, payload=None):
        calls.append(("log", db, entity_id, event_type, source, payload))
        return SimpleNamespace()

    async def fake_notify_task_event(db, entity_id, event_type, payload):
        calls.append(("notify", db, entity_id, event_type, payload))
        return 2

    monkeypatch.setattr(event_service, "log_event", fake_log_event)
    monkeypatch.setattr(task_notifications, "notify_task_event", fake_notify_task_event)

    db = object()
    delivered = await emit_in_session(
        db,
        "entity-1",
        "task.hitl_reminder",
        source="test",
        payload={"task_id": "task-1"},
    )

    assert delivered == 2
    assert calls == [
        ("log", db, "entity-1", "task.hitl_reminder", "test", {"task_id": "task-1"}),
        ("notify", db, "entity-1", "task.hitl_reminder", {"task_id": "task-1"}),
    ]


@pytest.mark.asyncio
async def test_deliver_webhook_event_uses_committed_payload(monkeypatch):
    import packages.core.services.webhook_service as webhook_service

    calls = []

    async def fake_deliver_event(entity_id, event_type, payload):
        calls.append((entity_id, event_type, payload))

    monkeypatch.setattr(webhook_service, "deliver_event", fake_deliver_event)

    await deliver_webhook_event("entity-1", "task.hitl_reminder", {"task_id": "task-1"})

    assert calls == [("entity-1", "task.hitl_reminder", {"task_id": "task-1"})]


@pytest.mark.asyncio
async def test_deliver_task_external_event_uses_committed_payload(monkeypatch):
    import packages.core.services.task_external_notifications as external_notifications

    calls = []

    async def fake_deliver_task_external_notifications(
        entity_id,
        event_type,
        payload,
        **_kwargs,
    ):
        calls.append((entity_id, event_type, payload))
        return {"email": 1, "external_chat": 1}

    monkeypatch.setattr(
        external_notifications,
        "deliver_task_external_notifications",
        fake_deliver_task_external_notifications,
    )

    await deliver_task_external_event("entity-1", "task.failed", {"task_id": "task-1"})

    assert calls == [("entity-1", "task.failed", {"task_id": "task-1"})]


@pytest.mark.asyncio
async def test_transactional_external_delivery_waits_for_outer_commit(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    deliveries = []
    delivery_complete = asyncio.Event()

    async def capture_webhook(entity_id, event_type, payload):
        deliveries.append(("webhook", entity_id, event_type, dict(payload)))

    async def capture_external(entity_id, event_type, payload, **_kwargs):
        deliveries.append(("external", entity_id, event_type, dict(payload)))
        delivery_complete.set()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_webhook)
    monkeypatch.setattr(
        event_emitter,
        "deliver_task_external_event",
        capture_external,
    )

    await emit_in_session(
        db_session,
        "entity-transactional",
        "document.uploaded",
        payload={"document_id": "doc-1"},
        notify=False,
        deliver_after_commit=True,
    )
    await _yield_background_delivery()
    assert deliveries == []

    await db_session.commit()
    await asyncio.wait_for(delivery_complete.wait(), timeout=2)
    await event_emitter.drain_external_event_deliveries(
        persistence_timeout_seconds=1,
        delivery_timeout_seconds=2,
    )

    assert deliveries == [
        (
            "webhook",
            "entity-transactional",
            "document.uploaded",
            {"document_id": "doc-1"},
        ),
        (
            "external",
            "entity-transactional",
            "document.uploaded",
            {"document_id": "doc-1"},
        ),
    ]
    db_session.expire_all()
    entry = (await db_session.scalars(
        select(EventLog).where(EventLog.entity_id == "entity-transactional")
    )).one()
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_emit_in_session_records_workspace_id_for_external_delivery(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    def skip_fast_path(session):
        session.info.pop(event_emitter._EXTERNAL_EVENT_QUEUE_KEY, None)

    monkeypatch.setattr(
        event_emitter,
        "_schedule_external_events_after_commit",
        skip_fast_path,
    )
    workspace_id = generate_ulid()
    await emit_in_session(
        db_session,
        "entity-workspace-event",
        "document.uploaded",
        payload={"document_id": "doc-workspace-event"},
        workspace_id=workspace_id,
        notify=False,
        deliver_after_commit=True,
    )
    await db_session.commit()

    entry = (await db_session.scalars(
        select(EventLog).where(EventLog.entity_id == "entity-workspace-event")
    )).one()
    assert entry.workspace_id == workspace_id


@pytest.mark.asyncio
async def test_transactional_external_delivery_is_discarded_on_rollback(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    deliveries = []

    async def capture(*args, **_kwargs):
        deliveries.append(args)

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", capture)

    await emit_in_session(
        db_session,
        "entity-rollback",
        "document.uploaded",
        payload={"document_id": "doc-rollback"},
        notify=False,
        deliver_after_commit=True,
    )
    await db_session.rollback()
    await db_session.commit()
    await _yield_background_delivery()

    assert deliveries == []
    assert (await db_session.scalars(
        select(EventLog).where(EventLog.entity_id == "entity-rollback")
    )).all() == []


@pytest.mark.asyncio
async def test_transactional_external_delivery_filters_rolled_back_savepoint(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    deliveries = []
    delivery_checked = asyncio.Event()
    deliver_committed = event_emitter._deliver_committed_external_events

    async def capture(*args, **_kwargs):
        deliveries.append(args)

    async def capture_committed(event_ids):
        try:
            await deliver_committed(event_ids)
        finally:
            delivery_checked.set()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", capture)
    monkeypatch.setattr(
        event_emitter,
        "_deliver_committed_external_events",
        capture_committed,
    )

    nested = await db_session.begin_nested()
    await emit_in_session(
        db_session,
        "entity-savepoint",
        "document.uploaded",
        payload={"document_id": "doc-savepoint"},
        notify=False,
        deliver_after_commit=True,
    )
    await nested.rollback()
    await db_session.commit()
    await asyncio.wait_for(delivery_checked.wait(), timeout=2)

    assert deliveries == []
    assert (await db_session.scalars(
        select(EventLog).where(EventLog.entity_id == "entity-savepoint")
    )).all() == []


@pytest.mark.asyncio
async def test_interrupted_external_delivery_is_reclaimed_from_event_log(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    def skip_fast_path(session):
        session.info.pop(event_emitter._EXTERNAL_EVENT_QUEUE_KEY, None)

    monkeypatch.setattr(
        event_emitter,
        "_schedule_external_events_after_commit",
        skip_fast_path,
    )
    await emit_in_session(
        db_session,
        "entity-recovery",
        "document.uploaded",
        payload={"document_id": "doc-recovery"},
        notify=False,
        deliver_after_commit=True,
    )
    await db_session.commit()
    event_id = (await db_session.scalars(
        select(EventLog.id).where(EventLog.entity_id == "entity-recovery")
    )).one()
    await db_session.rollback()

    started = asyncio.Event()

    async def blocked_delivery(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", blocked_delivery)
    delivery = asyncio.create_task(
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    db_session.expire_all()
    entry = (await db_session.scalars(
        select(EventLog).where(EventLog.entity_id == "entity-recovery")
    )).one()
    assert entry.external_delivery_status == NotificationOutboxStatus.PROCESSING.value
    assert entry.external_delivery_attempt_count == 1
    assert entry.external_delivery_locked_until is not None
    reclaim_at = entry.external_delivery_locked_until + timedelta(seconds=1)
    await db_session.rollback()

    calls = []

    async def capture_delivery(entity_id, event_type, payload, **_kwargs):
        calls.append((entity_id, event_type, dict(payload)))

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_delivery)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", capture_delivery)
    result = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
        when=reclaim_at,
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert result["delivered"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value
    assert entry.external_delivery_attempt_count == 2
    assert entry.external_delivery_delivered_at is not None
    assert calls == [
        (
            "entity-recovery",
            "document.uploaded",
            {"document_id": "doc-recovery"},
        ),
        (
            "entity-recovery",
            "document.uploaded",
            {"document_id": "doc-recovery"},
        ),
    ]


@pytest.mark.asyncio
async def test_external_delivery_claim_prevents_concurrent_duplicate_send(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    def skip_fast_path(session):
        session.info.pop(event_emitter._EXTERNAL_EVENT_QUEUE_KEY, None)

    monkeypatch.setattr(
        event_emitter,
        "_schedule_external_events_after_commit",
        skip_fast_path,
    )
    await emit_in_session(
        db_session,
        "entity-concurrent-delivery",
        "document.uploaded",
        payload={"document_id": "doc-concurrent-delivery"},
        notify=False,
        deliver_after_commit=True,
    )
    await db_session.commit()
    event_id = (await db_session.scalars(
        select(EventLog.id).where(
            EventLog.entity_id == "entity-concurrent-delivery"
        )
    )).one()
    await db_session.rollback()

    calls = []

    async def capture_delivery(entity_id, event_type, payload, **_kwargs):
        calls.append((entity_id, event_type, dict(payload)))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_delivery)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", capture_delivery)
    results = await asyncio.gather(
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        ),
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        ),
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert sorted(result["claimed"] for result in results) == [0, 1]
    assert sum(result["delivered"] for result in results) == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_external_delivery_bounds_parallel_provider_work(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    event_ids = []
    entity_id = generate_ulid()
    for index in range(8):
        event_id = generate_ulid()
        event_ids.append(event_id)
        db_session.add(
            EventLog(
                id=event_id,
                entity_id=entity_id,
                event_type="document.uploaded",
                payload={"document_id": f"doc-{index}"},
                external_delivery_status=NotificationOutboxStatus.PENDING.value,
                external_delivery_available_at=datetime.now(timezone.utc),
            )
        )
    await db_session.commit()

    active_deliveries = 0
    maximum_active_deliveries = 0

    async def capture_webhook(*_args):
        nonlocal active_deliveries, maximum_active_deliveries
        active_deliveries += 1
        maximum_active_deliveries = max(
            maximum_active_deliveries,
            active_deliveries,
        )
        await asyncio.sleep(0.05)
        active_deliveries -= 1

    async def noop_external(*_args, **_kwargs):
        return None

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_webhook)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", noop_external)

    results = await asyncio.gather(*(
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        )
        for event_id in event_ids
    ))

    assert maximum_active_deliveries <= 2
    assert sum(result["claimed"] for result in results) == len(event_ids)
    assert sum(result["delivered"] for result in results) == len(event_ids)


@pytest.mark.asyncio
async def test_external_delivery_provider_failure_retries_event_log(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    event_id = generate_ulid()
    db_session.add(
        EventLog(
            id=event_id,
            entity_id="entity-provider-failure",
            event_type="document.uploaded",
            payload={"document_id": "doc-provider-failure"},
            external_delivery_status=NotificationOutboxStatus.PENDING.value,
            external_delivery_available_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    async def fail_webhook(*_args):
        raise RuntimeError("provider refused delivery")

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", fail_webhook)
    result = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert result["claimed"] == 1
    assert result["retried"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.PENDING.value
    assert entry.external_delivery_attempt_count == 1
    assert "provider refused delivery" in entry.external_delivery_last_error


@pytest.mark.asyncio
async def test_external_delivery_retry_skips_already_completed_sink(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    event_id = generate_ulid()
    entity_id = generate_ulid()
    db_session.add(
        EventLog(
            id=event_id,
            entity_id=entity_id,
            event_type="task.failed",
            payload={"task_id": "task-partial-provider-failure"},
            external_delivery_status=NotificationOutboxStatus.PENDING.value,
            external_delivery_available_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    calls = []
    external_attempts = 0

    async def capture_webhook(entity_id, event_type, payload):
        calls.append(("webhook", entity_id, event_type, dict(payload)))

    async def flaky_external(entity_id, event_type, payload, **_kwargs):
        nonlocal external_attempts
        external_attempts += 1
        calls.append(("external", entity_id, event_type, dict(payload)))
        if external_attempts == 1:
            raise RuntimeError("external chat unavailable")

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_webhook)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", flaky_external)

    first = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
    )
    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    retry_at = entry.external_delivery_available_at
    assert first["retried"] == 1
    assert entry.external_delivery_completed_sinks == {"webhook": True}

    second = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
        when=retry_at + timedelta(seconds=1),
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert second["delivered"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value
    assert calls == [
        (
            "webhook",
            entity_id,
            "task.failed",
            {"task_id": "task-partial-provider-failure"},
        ),
        (
            "external",
            entity_id,
            "task.failed",
            {"task_id": "task-partial-provider-failure"},
        ),
        (
            "external",
            entity_id,
            "task.failed",
            {"task_id": "task-partial-provider-failure"},
        ),
    ]


@pytest.mark.asyncio
async def test_external_delivery_cancels_inactive_workspace_event(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    workspace_id = generate_ulid()
    event_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id="entity-inactive-workspace",
            name="Paused Workspace",
            status="paused",
        )
    )
    db_session.add(
        EventLog(
            id=event_id,
            entity_id="entity-inactive-workspace",
            workspace_id=workspace_id,
            event_type="document.uploaded",
            payload={"document_id": "doc-inactive-workspace"},
            external_delivery_status=NotificationOutboxStatus.PENDING.value,
            external_delivery_available_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    async def unexpected_delivery(*_args):
        raise AssertionError("inactive workspace event should not deliver")

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", unexpected_delivery)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", unexpected_delivery)
    result = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert result["claimed"] == 0
    assert result["canceled"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.CANCELED.value


@pytest.mark.asyncio
async def test_workspace_external_delivery_holds_lifecycle_lock_until_finish(
    db_session,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.services import event_emitter

    workspace_id = generate_ulid()
    event_id = generate_ulid()
    entity_id = "entity-workspace-lock"
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Lifecycle Locked Workspace",
            status="active",
        )
    )
    db_session.add(
        EventLog(
            id=event_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            event_type="document.uploaded",
            payload={"document_id": "doc-workspace-lock"},
            external_delivery_status=NotificationOutboxStatus.PENDING.value,
            external_delivery_available_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    pause_waiting = asyncio.Event()
    pause_acquired = asyncio.Event()

    async def blocked_webhook(*_args):
        provider_started.set()
        await provider_release.wait()

    async def noop_external(*_args, **_kwargs):
        return None

    async def pause_workspace():
        async with async_session() as db:
            pause_waiting.set()
            workspace = (await db.execute(
                select(Workspace)
                .where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                )
                .with_for_update()
            )).scalar_one()
            pause_acquired.set()
            workspace.status = "paused"
            await db.commit()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", blocked_webhook)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", noop_external)

    delivery = asyncio.create_task(
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        )
    )
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    pause = asyncio.create_task(pause_workspace())
    await asyncio.wait_for(pause_waiting.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert not pause_acquired.is_set()

    provider_release.set()
    result = await asyncio.wait_for(delivery, timeout=2)
    await asyncio.wait_for(pause, timeout=2)

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    workspace = await db_session.get(Workspace, workspace_id)
    assert result["delivered"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value
    assert workspace.status == "paused"


@pytest.mark.asyncio
async def test_workspace_external_delivery_cancel_preserves_completed_sink(
    db_session,
    monkeypatch,
):
    from packages.core.services import event_emitter

    workspace_id = generate_ulid()
    event_id = generate_ulid()
    entity_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Partially Delivered Workspace",
            status="active",
        )
    )
    db_session.add(
        EventLog(
            id=event_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            event_type="task.failed",
            payload={"task_id": "task-workspace-partial"},
            external_delivery_status=NotificationOutboxStatus.PENDING.value,
            external_delivery_available_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    calls = []
    external_started = asyncio.Event()

    async def capture_webhook(entity_id, event_type, payload):
        calls.append(("webhook", entity_id, event_type, dict(payload)))

    async def blocked_external(entity_id, event_type, payload, **_kwargs):
        calls.append(("external", entity_id, event_type, dict(payload)))
        external_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_webhook)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", blocked_external)

    delivery = asyncio.create_task(
        event_emitter.dispatch_due_external_events(
            batch_size=1,
            event_ids=(event_id,),
        )
    )
    await asyncio.wait_for(external_started.wait(), timeout=2)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert entry.external_delivery_status == NotificationOutboxStatus.PROCESSING.value
    assert entry.external_delivery_completed_sinks == {"webhook": True}
    reclaim_at = entry.external_delivery_locked_until + timedelta(seconds=1)
    await db_session.rollback()

    async def capture_external(entity_id, event_type, payload, **_kwargs):
        calls.append(("external", entity_id, event_type, dict(payload)))

    monkeypatch.setattr(event_emitter, "deliver_task_external_event", capture_external)
    result = await event_emitter.dispatch_due_external_events(
        batch_size=1,
        event_ids=(event_id,),
        when=reclaim_at,
    )

    db_session.expire_all()
    entry = await db_session.get(EventLog, event_id)
    assert result["delivered"] == 1
    assert entry.external_delivery_status == NotificationOutboxStatus.DELIVERED.value
    assert calls == [
        (
            "webhook",
            entity_id,
            "task.failed",
            {"task_id": "task-workspace-partial"},
        ),
        (
            "external",
            entity_id,
            "task.failed",
            {"task_id": "task-workspace-partial"},
        ),
        (
            "external",
            entity_id,
            "task.failed",
            {"task_id": "task-workspace-partial"},
        ),
    ]
