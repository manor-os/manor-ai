import pytest

from packages.core.services.event_emitter import (
    deliver_task_external_event,
    deliver_webhook_event,
    emit_in_session,
)


@pytest.mark.asyncio
async def test_emit_in_session_logs_and_notifies(monkeypatch):
    import packages.core.services.event_service as event_service
    import packages.core.services.task_event_notifications as task_notifications

    calls = []

    async def fake_log_event(db, entity_id, event_type, *, source=None, payload=None):
        calls.append(("log", db, entity_id, event_type, source, payload))

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

    async def fake_deliver_task_external_notifications(entity_id, event_type, payload):
        calls.append((entity_id, event_type, payload))
        return {"email": 1, "external_chat": 1}

    monkeypatch.setattr(
        external_notifications,
        "deliver_task_external_notifications",
        fake_deliver_task_external_notifications,
    )

    await deliver_task_external_event("entity-1", "task.failed", {"task_id": "task-1"})

    assert calls == [("entity-1", "task.failed", {"task_id": "task-1"})]
