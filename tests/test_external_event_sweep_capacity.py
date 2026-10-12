"""Event sweeps defer queued work and drain every child before returning."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_notification_delivery_capacity import limited_db as limited_db
from packages.core.models.base import generate_ulid
from packages.core.models.event import EventLog
from packages.core.models.workspace import Workspace
from packages.core.services import event_emitter
from packages.core.services.external_delivery_capacity import external_delivery_semaphore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("client")]


async def seed_events(db, count):
    entries = []
    for index in range(count):
        workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Sweep budget", status="active")
        entry = EventLog(
            id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
            event_type="document.uploaded", payload={"index": index},
            external_delivery_status="pending", external_delivery_available_at=datetime.now(timezone.utc),
        )
        db.add_all([workspace, entry])
        entries.append(entry)
    await db.commit()
    return entries


async def assert_pending(factory, event_ids):
    async with factory() as db:
        for event_id in event_ids:
            saved = await db.get(EventLog, event_id)
            assert saved.external_delivery_status == "pending"
            assert saved.external_delivery_attempt_count == 0
            assert saved.external_delivery_claim_token is None


async def test_busy_delivery_capacity_defers_events_without_claiming(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    entries = await seed_events(db_session, 1)
    provider = AsyncMock()
    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_ADMISSION_WAIT_SECONDS", 0.02)
    async with external_delivery_semaphore():
        result = await asyncio.wait_for(event_emitter.dispatch_due_external_events(), 0.5)
    assert result["claimed"] == 0
    provider.assert_not_awaited()
    await assert_pending(factory, [entries[0].id])
    assert (await event_emitter.dispatch_due_external_events())["delivered"] == 1
    assert runtime.checked_out == 0


async def test_sweep_budget_stops_new_claims_after_an_admitted_send(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    entries = await seed_events(db_session, 3)
    clock = [100.0]
    monkeypatch.setattr(event_emitter, "monotonic", lambda: clock[0])
    monkeypatch.setattr(event_emitter, "_EXTERNAL_EVENT_SWEEP_BUDGET_SECONDS", 10.0)

    async def provider(*args, **kwargs):
        clock[0] += 11.0

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    result = await event_emitter.dispatch_due_external_events(batch_size=3)
    assert result["claimed"] == result["delivered"] == 1
    await assert_pending(factory, [entry.id for entry in entries[1:]])
    assert runtime.checked_out == 0
    assert (await event_emitter.dispatch_due_external_events(batch_size=3))["delivered"] == 1


@pytest.mark.parametrize("limited_db", [3], indirect=True)
async def test_batch_uses_bounded_dispatchers_and_drains_fast_events(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    await seed_events(db_session, 8)
    dispatchers = set()

    async def provider(*args, **kwargs):
        dispatchers.add(asyncio.current_task())
        await asyncio.sleep(0)

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    result = await event_emitter.dispatch_due_external_events(batch_size=8)
    assert result["claimed"] == result["delivered"] == 8
    assert len(dispatchers) <= 2
    assert runtime.checked_out == 0


@pytest.mark.parametrize("limited_db", [3], indirect=True)
async def test_claim_error_cancels_and_drains_sibling_send(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    entries = await seed_events(db_session, 3)
    started, canceled = asyncio.Event(), asyncio.Event()
    provider_tasks = []
    original_claim = event_emitter._claim_external_event_deliveries
    calls = 0

    async def claim(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            await started.wait()
            raise RuntimeError("synthetic claim failure")
        return await original_claim(*args, **kwargs)

    async def provider(*args, **kwargs):
        provider_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(event_emitter, "_claim_external_event_deliveries", claim)
    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    try:
        with pytest.raises(RuntimeError, match="synthetic claim failure"):
            await asyncio.wait_for(event_emitter.dispatch_due_external_events(batch_size=2), 2)
        assert canceled.is_set(), "Failed sweep returned with a provider still running"
        assert all(task.done() for task in provider_tasks)
        assert runtime.checked_out == 0
        await assert_pending(factory, [entry.id for entry in entries[1:]])
        async with factory() as db:
            interrupted = await db.get(EventLog, entries[0].id)
            assert interrupted.external_delivery_status == "processing"
            assert interrupted.external_delivery_attempt_count == 1
    finally:
        for task in provider_tasks:
            task.cancel()
        await asyncio.gather(*provider_tasks, return_exceptions=True)


@pytest.mark.parametrize("limited_db", [3], indirect=True)
async def test_cancel_sweep_drains_all_sends_and_preserves_queued_events(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    entries = await seed_events(db_session, 3)
    started, canceled = [], []
    both_started = asyncio.Event()

    async def provider(entity_id, *args, **kwargs):
        started.append(entity_id)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(entity_id)

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    delivery = asyncio.create_task(event_emitter.dispatch_due_external_events(batch_size=3))
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        observer = async_sessionmaker(db_session.bind, expire_on_commit=False)
        await assert_pending(observer, [entries[2].id])
    finally:
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    assert sorted(canceled) == sorted(started)
    assert runtime.checked_out == 0
    await assert_pending(factory, [entries[2].id])


async def test_capacity_arriving_after_sweep_deadline_does_not_claim(db_session, limited_db, monkeypatch):
    runtime, factory = limited_db
    entries = await seed_events(db_session, 1)
    clock = [100.0]
    waiting = asyncio.Event()
    provider = AsyncMock()
    monkeypatch.setattr(event_emitter, "deliver_webhook_event", provider)
    monkeypatch.setattr(event_emitter, "deliver_task_external_event", AsyncMock())
    monkeypatch.setattr(event_emitter, "monotonic", lambda: clock[0])
    gate = external_delivery_semaphore()
    acquire = gate.acquire

    async def observed_acquire():
        waiting.set()
        return await acquire()

    monkeypatch.setattr(gate, "acquire", observed_acquire)
    await acquire()
    delivery = asyncio.create_task(event_emitter.dispatch_due_external_events(event_ids=(entries[0].id,)))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        clock[0] += event_emitter._EXTERNAL_EVENT_SWEEP_BUDGET_SECONDS + 1
    finally:
        gate.release()
        result = await delivery
    provider.assert_not_awaited()
    assert result["claimed"] == 0
    await assert_pending(factory, [entries[0].id])
    assert runtime.checked_out == 0
