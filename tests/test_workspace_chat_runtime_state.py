"""Conversation execution remains authoritative across deployment and paging."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_messages import mark_stale_assistant_streams_interrupted


def _run(conversation_id, entity_id, *, status="running", assistant_message_id=None, **kwargs):
    run_id = generate_ulid()
    return RuntimeRun(
        id=run_id, root_run_id=run_id, conversation_id=conversation_id,
        entity_id=entity_id, user_id=generate_ulid(), status=status,
        assistant_message_id=assistant_message_id, **kwargs,
    )


def _message(conversation_id, *, created_at=None, meta=None):
    return Message(
        id=generate_ulid(), conversation_id=conversation_id, role="assistant",
        content="Still waiting for the video", author_kind="agent", message_kind="text",
        created_at=created_at or datetime.now(timezone.utc), meta=meta,
    )


async def _workspace_chat(client, db_session):
    response = await client.post("/api/v1/auth/register", json={
        "email": f"runtime-{generate_ulid()}@example.com",
        "password": "test-password-123", "entity_name": "Runtime state test",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Video workspace"},
    )
    assert response.status_code in (200, 201), response.text
    workspace_id = response.json()["id"]
    conversation_id = generate_ulid()
    db_session.add(Conversation(
        id=conversation_id, entity_id=me["entity_id"], workspace_id=workspace_id,
        title="Workspace", channel="workspace", scope="workspace_main",
    ))
    await db_session.commit()
    return headers, me["entity_id"], workspace_id, conversation_id


async def _page(client, headers, workspace_id, **params):
    response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/page",
        headers=headers, params={"limit": 75, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mode(monkeypatch, execution="durable", coordination="external-runner"):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", execution)
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", coordination)


@pytest.mark.integration
async def test_old_live_runtime_checkpoints_survive_api_startup_repair(db_session):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    live_messages = []
    for status in RuntimeRunStatus.active_root():
        message = _message(generate_ulid(), created_at=old, meta={"stream_status": "streaming"})
        live_messages.append(message)
        db_session.add_all([message, _run(
            message.conversation_id, generate_ulid(), status=status, assistant_message_id=message.id,
        )])
    stale = _message(generate_ulid(), created_at=old, meta={"stream_status": "streaming"})
    completed = _message(generate_ulid(), created_at=old, meta={"stream_status": "running"})
    db_session.add_all([stale, completed, _run(
        completed.conversation_id, generate_ulid(), status="completed", assistant_message_id=completed.id,
    )])
    await db_session.commit()

    assert await mark_stale_assistant_streams_interrupted(db_session) == 2
    assert await mark_stale_assistant_streams_interrupted(db_session) == 0

    for message in live_messages:
        await db_session.refresh(message)
        assert message.meta == {"stream_status": "streaming"}
    for message in (stale, completed):
        await db_session.refresh(message)
        assert message.meta["stream_status"] == "interrupted"


@pytest.mark.integration
@pytest.mark.parametrize("status", RuntimeRunStatus.active_root())
async def test_active_runtime_outside_message_page_remains_visible(
    client, db_session, monkeypatch, status,
):
    headers, entity_id, workspace_id, conversation_id = await _workspace_chat(client, db_session)
    _mode(monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    # Deployments predating the fix can already have interrupted this marker.
    placeholder = _message(conversation_id, created_at=old, meta={"stream_status": "interrupted"})
    run = _run(conversation_id, entity_id, status=status, assistant_message_id=placeholder.id)
    db_session.add_all([placeholder, run])
    for index in range(80):
        db_session.add(_message(conversation_id, created_at=old + timedelta(seconds=index + 1)))
    await db_session.commit()

    page = await _page(client, headers, workspace_id)

    assert page["has_active_runtime_run"] is True
    assert page["has_more"] is True
    assert placeholder.id not in {message["id"] for message in page["items"]}
    run.status = "completed"
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["has_active_runtime_run"] is False


@pytest.mark.integration
async def test_a_run_waiting_on_an_approval_does_not_lock_the_chat(client, db_session, monkeypatch):
    """It waits for a person's decision, not for this chat: the composer stays
    usable and the next message supersedes it."""
    headers, entity_id, workspace_id, conversation_id = await _workspace_chat(client, db_session)
    _mode(monkeypatch)
    placeholder = _message(conversation_id, meta={"stream_status": "streaming"})
    run = _run(conversation_id, entity_id, status="waiting_resource", assistant_message_id=placeholder.id)
    run.status_reason = "capability_approval"
    db_session.add_all([placeholder, run])
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["has_active_runtime_run"] is False
    run.status_reason = "sandbox_queue"
    await db_session.commit()
    assert (await _page(client, headers, workspace_id))["has_active_runtime_run"] is True


@pytest.mark.integration
@pytest.mark.parametrize("execution,coordination,expected", [
    ("durable", "external-runner", False),
    ("inline", "external-runner", None),
    ("durable", "local", None),
])
async def test_terminal_runtime_overrides_stale_marker_only_in_durable_mode(
    client, db_session, monkeypatch, execution, coordination, expected,
):
    headers, entity_id, workspace_id, conversation_id = await _workspace_chat(client, db_session)
    _mode(monkeypatch, execution, coordination)
    placeholder = _message(conversation_id, meta={"stream_status": "streaming"})
    db_session.add_all([placeholder, _run(
        conversation_id, entity_id, status="completed", assistant_message_id=placeholder.id,
    )])
    await db_session.commit()

    page = await _page(client, headers, workspace_id)

    assert page["has_active_runtime_run"] is expected
    assert next(row for row in page["items"] if row["id"] == placeholder.id)["meta"]["stream_status"] == "streaming"


@pytest.mark.integration
async def test_runtime_state_is_scoped_to_entity_conversation_and_root(
    client, db_session, monkeypatch,
):
    headers, entity_id, workspace_id, conversation_id = await _workspace_chat(client, db_session)
    _mode(monkeypatch)
    thread_id = generate_ulid()
    db_session.add(Conversation(
        id=thread_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Plan thread", channel="workspace", scope="workspace_thread",
        thread_ref_kind="plan", thread_ref_id="video-plan",
    ))
    db_session.add_all([
        _run(thread_id, entity_id),
        _run(conversation_id, entity_id, parent_run_id=generate_ulid()),
        _run(conversation_id, generate_ulid()),
    ])
    await db_session.commit()

    main = await _page(client, headers, workspace_id)
    thread = await _page(
        client, headers, workspace_id, thread_ref_kind="plan", thread_ref_id="video-plan",
    )

    assert main["conversation_id"] == conversation_id
    assert main["has_active_runtime_run"] is False
    assert thread["conversation_id"] == thread_id
    assert thread["has_active_runtime_run"] is True
