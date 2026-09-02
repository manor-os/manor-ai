from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session


def test_task_service_queues_realtime_updates_after_commit() -> None:
    from packages.core.services import task_service

    for mutation in (task_service.create_task, task_service.update_task):
        source = inspect.getsource(mutation)
        assert "queue_task_update_after_commit" in source
        assert "await broadcast_task_update" not in source


@pytest.mark.asyncio
async def test_task_runtime_update_carries_task_and_plan_projection_ids(
    monkeypatch,
) -> None:
    from packages.core.services import realtime

    calls: list[tuple[str, str | None, dict]] = []

    async def capture(
        entity_id: str,
        payload: dict,
        *,
        workspace_id: str | None = None,
    ) -> None:
        calls.append((entity_id, workspace_id, payload))

    monkeypatch.setattr(realtime, "broadcast_task_update", capture)

    await realtime.broadcast_task_runtime_update(
        "entity-1",
        task_id="task-1",
        workspace_id="workspace-1",
        plan_id="plan-1",
        status="waiting_on_customer",
        title="Review the result",
        event="task.hitl_requested",
    )

    assert calls == [(
        "entity-1",
        "workspace-1",
        {
            "id": "task-1",
            "task_id": "task-1",
            "plan_id": "plan-1",
            "status": "waiting_on_customer",
            "title": "Review the result",
            "event": "task.hitl_requested",
        },
    )]


@pytest.mark.asyncio
async def test_workspace_task_update_uses_workspace_relay_target(monkeypatch) -> None:
    from packages.core.services import realtime

    calls: list[dict] = []

    async def capture(payload: dict) -> None:
        calls.append(payload)

    monkeypatch.setattr(realtime, "_redis_publish", capture)

    await realtime.broadcast_task_update(
        "entity-1",
        {"task_id": "task-1", "title": "Private task"},
        workspace_id="workspace-1",
    )

    assert calls == [{
        "target": "workspace",
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "event": "task_update",
        "data": {"task_id": "task-1", "title": "Private task"},
    }]


@pytest.mark.asyncio
async def test_personal_realtime_event_carries_exact_entity_scope(monkeypatch) -> None:
    from packages.core.services import realtime

    calls: list[dict] = []

    async def capture(payload: dict) -> None:
        calls.append(payload)

    monkeypatch.setattr(realtime, "_redis_publish", capture)

    await realtime.push_notification(
        "user-1",
        {"id": "notification-1"},
        entity_id="entity-1",
    )

    assert calls == [{
        "target": "user",
        "user_id": "user-1",
        "entity_id": "entity-1",
        "event": "notification",
        "data": {"id": "notification-1"},
    }]


@pytest.mark.asyncio
async def test_workspace_notification_carries_workspace_relay_scope(monkeypatch) -> None:
    from packages.core.services import realtime

    calls: list[dict] = []

    async def capture(payload: dict) -> None:
        calls.append(payload)

    monkeypatch.setattr(realtime, "_redis_publish", capture)
    await realtime.push_notification(
        "user-1",
        {"id": "notification-1"},
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert calls == [{
        "target": "workspace_user",
        "user_id": "user-1",
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "event": "notification",
        "data": {"id": "notification-1"},
    }]


@pytest.mark.asyncio
async def test_task_update_queue_publishes_only_after_commit(monkeypatch) -> None:
    from packages.core.services import realtime

    calls: list[tuple] = []
    published = asyncio.Event()

    async def capture_users(user_ids, payload, *, entity_id) -> None:
        calls.append(("users", entity_id, tuple(user_ids), payload))

    async def capture_broadcast(
        entity_id: str,
        payload: dict,
        *,
        workspace_id: str | None = None,
    ) -> None:
        calls.append(("broadcast", entity_id, workspace_id, payload))
        published.set()

    monkeypatch.setattr(realtime, "push_task_update_multi", capture_users)
    monkeypatch.setattr(realtime, "broadcast_task_update", capture_broadcast)
    sync_session = Session()
    sync_session.begin()
    db = SimpleNamespace(sync_session=sync_session)
    payload = {"task_id": "task-1", "status": "pending"}
    realtime.queue_task_update_after_commit(
        db,
        "entity-1",
        payload,
        user_ids=("user-1", "user-2"),
    )
    payload["status"] = "mutated-after-queue"

    await asyncio.sleep(0)
    assert calls == []
    sync_session.commit()
    await asyncio.wait_for(published.wait(), timeout=1)

    assert calls == [
        ("users", "entity-1", ("user-1", "user-2"), {"task_id": "task-1", "status": "pending"}),
        ("broadcast", "entity-1", None, {"task_id": "task-1", "status": "pending"}),
    ]
    sync_session.close()


@pytest.mark.asyncio
async def test_task_update_queue_discards_rolled_back_events(monkeypatch) -> None:
    from packages.core.services import realtime

    published = asyncio.Event()

    async def capture(*_args, **_kwargs) -> None:
        published.set()

    monkeypatch.setattr(realtime, "broadcast_task_update", capture)
    sync_session = Session()
    sync_session.begin()
    db = SimpleNamespace(sync_session=sync_session)
    realtime.queue_task_update_after_commit(
        db,
        "entity-1",
        {"task_id": "task-1"},
    )

    sync_session.rollback()
    await asyncio.sleep(0)

    assert not published.is_set()
    sync_session.close()


@pytest.mark.asyncio
async def test_task_update_queue_keeps_outer_event_after_savepoint_rollback(
    monkeypatch,
) -> None:
    from packages.core.services import realtime

    calls: list[str] = []
    published = asyncio.Event()

    async def capture(
        _entity_id: str,
        payload: dict,
        *,
        workspace_id: str | None = None,
    ) -> None:
        assert workspace_id == "workspace-1"
        calls.append(payload["task_id"])
        published.set()

    monkeypatch.setattr(realtime, "broadcast_task_update", capture)
    sync_session = Session()
    sync_session.begin()
    db = SimpleNamespace(sync_session=sync_session)
    realtime.queue_task_update_after_commit(
        db,
        "entity-1",
        {"task_id": "outer"},
        workspace_id="workspace-1",
    )
    savepoint = sync_session.begin_nested()
    realtime.queue_task_update_after_commit(
        db,
        "entity-1",
        {"task_id": "rolled-back"},
        workspace_id="workspace-1",
    )

    savepoint.rollback()
    sync_session.commit()
    await asyncio.wait_for(published.wait(), timeout=1)

    assert calls == ["outer"]
    sync_session.close()
