"""Workspace chat keeps the authenticated author on every shared human turn."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apps.api.routers import workspace_chat
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import WorkspaceStaff
from packages.core.services import conversation_history
from packages.core.services import workspace_runtime
from packages.core.services.workspace_runtime import (
    process_workspace_task_comment,
)
from tests.test_document_permissions import _create_entity_user


@pytest.mark.asyncio
async def test_two_same_name_members_keep_distinct_persisted_authors(
    client,
    db_session,
    monkeypatch,
) -> None:
    scheduled: list[dict] = []
    monkeypatch.setattr(
        workspace_chat,
        "_schedule_workspace_chat_processing",
        AsyncMock(side_effect=lambda **kwargs: scheduled.append(kwargs)),
    )

    registered = await client.post("/api/v1/auth/register", json={
        "email": "ws-sender-owner@example.com",
        "password": "test-password-123",
        "entity_name": "Shared sender workspace",
    })
    assert registered.status_code in (200, 201), registered.text
    owner_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    owner_row = (await db_session.execute(
        select(User).where(User.id == owner["id"])
    )).scalar_one()
    owner_row.display_name = "Alex Chen"
    await db_session.commit()

    workspace = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Sender identity"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    member = await _create_entity_user(
        owner["entity_id"],
        "ws_sender_member",
        display_name="Alex Chen",
    )
    db_session.add(WorkspaceStaff(
        workspace_id=workspace_id,
        user_id=member["id"],
        role="editor",
        status="active",
    ))
    await db_session.commit()

    route = f"/api/v1/workspaces/{workspace_id}/chat/messages"
    posted = []
    for headers, body in (
        (owner_headers, "Please ship the launch report."),
        (member["headers"], "Please wait for legal review."),
    ):
        response = await client.post(route, headers=headers, json={"body": body})
        assert response.status_code == 201, response.text
        posted.append(response.json())

    assert [message["author_user_id"] for message in posted] == [
        owner["id"], member["id"],
    ]
    assert [message["author_user_name"] for message in posted] == [
        "Alex Chen", "Alex Chen",
    ]
    assert [request["user_id"] for request in scheduled] == [
        owner["id"], member["id"],
    ]
    assert [request["message_id"] for request in scheduled] == [
        message["id"] for message in posted
    ]

    for message, expected_user_id in zip(posted, (owner["id"], member["id"])):
        persisted = (await db_session.execute(
            select(Message).where(Message.id == message["id"])
        )).scalar_one()
        assert persisted.meta["author_user_id"] == expected_user_id
        assert persisted.content == message["body"]

    history = await client.get(route, headers=member["headers"])
    assert history.status_code == 200, history.text
    by_id = {message["id"]: message for message in history.json()}
    for message in posted:
        assert by_id[message["id"]]["author_user_id"] == message["author_user_id"]
        assert by_id[message["id"]]["author_user_name"] == "Alex Chen"

    model_history = await conversation_history.load_conversation_history(
        db_session,
        posted[0]["conversation_id"],
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
    )
    first_turn = next(
        entry for entry in model_history
        if "Please ship the launch report." in entry["content"]
    )
    second_turn = next(
        entry for entry in model_history
        if "Please wait for legal review." in entry["content"]
    )
    first_label = re.search(r"P-[A-F0-9]{12}", first_turn["content"])
    second_label = re.search(r"P-[A-F0-9]{12}", second_turn["content"])
    assert first_label and second_label
    assert first_label.group() != second_label.group()
    assert "Alex Chen" in first_turn["content"]
    assert "Alex Chen" in second_turn["content"]
    assert model_history[-1]["role"] == "system"
    assert second_label.group() in model_history[-1]["content"]
    assert first_label.group() not in model_history[-1]["content"]
    assert owner["id"] not in str(model_history)
    assert member["id"] not in str(model_history)

    without_current = await conversation_history.load_conversation_history(
        db_session,
        posted[0]["conversation_id"],
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
        exclude_message_id=posted[1]["id"],
    )
    assert sum(
        "Please wait for legal review." in entry["content"]
        for entry in without_current
    ) == 0
    assert second_label.group() in without_current[-1]["content"]

    db_session.add(Message(
        id=generate_ulid(),
        conversation_id=posted[0]["conversation_id"],
        role="user",
        author_kind="user",
        content="Legacy sender was not recorded.",
        meta={},
    ))
    await db_session.commit()
    with_legacy = await conversation_history.load_conversation_history(
        db_session,
        posted[0]["conversation_id"],
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
    )
    legacy_turn = next(
        entry for entry in with_legacy
        if "Legacy sender was not recorded." in entry["content"]
    )
    assert "Unknown participant" in legacy_turn["content"]
    assert first_label.group() not in legacy_turn["content"]
    assert second_label.group() not in legacy_turn["content"]

    foreign_registration = await client.post("/api/v1/auth/register", json={
        "email": "ws-sender-foreign@example.com",
        "password": "test-password-123",
        "entity_name": "Different company",
    })
    assert foreign_registration.status_code in (200, 201), foreign_registration.text
    foreign_headers = {
        "Authorization": f"Bearer {foreign_registration.json()['access_token']}"
    }
    foreign = (await client.get("/api/v1/auth/me", headers=foreign_headers)).json()
    foreign_row = (await db_session.execute(
        select(User).where(User.id == foreign["id"])
    )).scalar_one()
    foreign_row.display_name = "Private Outside Name"
    db_session.add(Message(
        id=generate_ulid(),
        conversation_id=posted[0]["conversation_id"],
        role="user",
        author_kind="user",
        content="Foreign author metadata must not reveal a name.",
        meta={"author_user_id": foreign["id"]},
    ))
    await db_session.commit()
    with_foreign = await conversation_history.load_conversation_history(
        db_session,
        posted[0]["conversation_id"],
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
    )
    foreign_turn = next(
        entry for entry in with_foreign
        if "Foreign author metadata must not reveal a name." in entry["content"]
    )
    assert "Unknown participant" in foreign_turn["content"]
    assert "Private Outside Name" not in str(with_foreign)
    assert foreign["id"] not in str(with_foreign)

    base_time = datetime.now(timezone.utc) + timedelta(minutes=1)
    db_session.add_all([
        Message(
            id=generate_ulid(),
            conversation_id=posted[0]["conversation_id"],
            role="user",
            author_kind="user",
            content=f"Older shared question {index}",
            meta={"author_user_id": owner["id"] if index % 2 else member["id"]},
            created_at=base_time + timedelta(seconds=index),
        )
        for index in range(12)
    ])
    await db_session.commit()
    summarize = AsyncMock()
    monkeypatch.setattr(conversation_history, "update_conversation_summary", summarize)
    await conversation_history.load_conversation_history(
        db_session,
        posted[0]["conversation_id"],
        token_budget=1,
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
    )
    await asyncio.sleep(0)
    assert summarize.await_count == 1
    summary_call = summarize.await_args
    assert summary_call.args == (posted[0]["conversation_id"],)
    conversation = await conversation_history._get_conversation_for_history(
        db_session, posted[0]["conversation_id"],
    )
    summary_page = await conversation_history._list_summary_page(
        db_session, conversation, **summary_call.kwargs,
    )
    _, summary_snapshots = await conversation_history._summary_page_snapshots(
        db_session, conversation, summary_page,
    )
    summary_snapshot = "\n".join(
        item.content for item in summary_snapshots
    )
    assert first_label.group() in summary_snapshot
    assert second_label.group() in summary_snapshot
    assert owner["id"] not in summary_snapshot
    assert member["id"] not in summary_snapshot

    task_thread_id = generate_ulid()
    db_session.add(Conversation(
        id=task_thread_id,
        entity_id=owner["entity_id"],
        workspace_id=workspace_id,
        scope="workspace_thread",
        channel="workspace",
        thread_ref_kind="task",
        thread_ref_id=generate_ulid(),
    ))
    db_session.add_all([
        Message(
            id=generate_ulid(),
            conversation_id=task_thread_id,
            role="user",
            author_kind="user",
            content=body,
            meta={"author_user_id": user_id},
            created_at=base_time + timedelta(minutes=2, seconds=index),
        )
        for index, (user_id, body) in enumerate((
            (owner["id"], "Task question from first Alex."),
            (member["id"], "Task question from second Alex."),
        ))
    ])
    await db_session.commit()
    task_history = await conversation_history.load_conversation_history(
        db_session,
        task_thread_id,
        include_workspace_senders=True,
        current_sender_user_id=member["id"],
    )
    task_labels = [
        re.search(r"P-[A-F0-9]{12}", entry["content"]).group()
        for entry in task_history
        if entry["role"] == "user"
    ]
    assert len(task_labels) == 2
    assert task_labels[0] != task_labels[1]
    assert task_labels[1] in task_history[-1]["content"]
    assert owner["id"] not in str(task_history)
    assert member["id"] not in str(task_history)


@pytest.mark.asyncio
async def test_a_posted_message_runs_as_a_durable_turn_the_chat_can_see(client, db_session, monkeypatch) -> None:
    """Staging 2026-11-03: a turn from POST /chat/messages ran 10 minutes inside the API process and read as idle."""
    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.models.task import Message
    from packages.core.tasks import runtime_tasks

    published: list[str] = []

    async def publish(run_id):
        published.append(run_id)
        return True

    monkeypatch.setattr(runtime_tasks, "publish_runtime_run_outbox_now", publish)
    registered = await client.post("/api/v1/auth/register", json={
        "email": "ws-durable-post@example.com", "password": "test-password-123", "entity_name": "Durable post",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Durable"})).json()
    url = f"/api/v1/workspaces/{workspace['id']}/chat/messages"

    posted = await client.post(url, headers=headers, json={"body": "Draft five ideas", "audience": "ai"})
    assert posted.status_code == 201, posted.text
    [run] = (await db_session.execute(select(RuntimeRun).where(RuntimeRun.workspace_id == workspace["id"]))).scalars().all()
    assert published == [run.id] and run.status == "queued"
    assert run.execution_payload["message"] == "Draft five ideas"
    assert run.execution_payload["surface"] == "workspace_chat"
    assert run.execution_payload["runtime_metadata"] == {"origin_user_message_id": posted.json()["id"]}
    placeholder = await db_session.get(Message, run.assistant_message_id)
    assert placeholder.role == "assistant" and placeholder.meta["stream_status"] == "running"

    page = (await client.get(f"{url}/page", headers=headers)).json()
    assert page["has_active_runtime_run"] in (True, None)

    # One turn at a time; the refused message is not kept.
    again = await client.post(url, headers=headers, json={"body": "And five more", "audience": "ai"})
    assert again.status_code == 409, again.text
    bodies = [row.content for row in (await db_session.execute(
        select(Message).where(Message.conversation_id == run.conversation_id, Message.role == "user"))).scalars()]
    assert "And five more" not in bodies

    # A note for the team starts no turn.
    team = await client.post(url, headers=headers, json={"body": "FYI", "audience": "team"})
    assert team.status_code == 201, team.text
    assert published == [run.id]


@pytest.mark.asyncio
async def test_task_comment_thread_uses_authenticated_author_not_label(
    monkeypatch,
) -> None:
    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def fake_session():
        yield db

    add_message = AsyncMock(return_value=SimpleNamespace(id="task-message-2"))
    runtime = AsyncMock(return_value={"content": ""})
    task = SimpleNamespace(
        id="task-1",
        workspace_id="workspace-1",
        title="Shared task",
        conversation_id=None,
    )
    monkeypatch.setattr("packages.core.database.async_session", fake_session)
    monkeypatch.setattr(
        workspace_runtime,
        "_load_task_for_runtime",
        AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        workspace_runtime,
        "ensure_workspace_task_conversation",
        AsyncMock(return_value=SimpleNamespace(id="task-thread-1")),
    )
    monkeypatch.setattr(
        "packages.core.services.task_session.resolve_task_session_host",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "packages.core.services.task_service.task_executing_agent_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("packages.core.services.conversation_messages.add_message", add_message)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_run_chat_turn", runtime)

    await process_workspace_task_comment(
        task_id="task-1",
        entity_id="entity-1",
        user_id="user-2",
        author_label="Owner",  # Display text must not become identity.
        comment="Who approved this?",
        log_id="task-log-2",
    )

    assert add_message.await_count == 1
    assert add_message.await_args.kwargs["meta"]["author_user_id"] == "user-2"
    assert add_message.await_args.kwargs["content"] == "Who approved this?"
    assert runtime.await_count == 1
    assert runtime.await_args.kwargs["user_id"] == "user-2"
    assert runtime.await_args.kwargs["runtime_metadata"] == {
        "origin_user_message_id": "task-message-2",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["workspace_main", "workspace_thread"])
async def test_workspace_summary_preserves_sender_labels_in_model_prompt(
    scope,
    monkeypatch,
) -> None:
    conversation = SimpleNamespace(
        entity_id="entity-1",
        workspace_id="workspace-1",
        user_id=None,
        channel="workspace",
        scope=scope,
        summary=None,
    )
    db = SimpleNamespace()

    @asynccontextmanager
    async def fake_session():
        yield db

    complete = AsyncMock(return_value=SimpleNamespace(content="P-ABCDEF123456 asked for review."))
    monkeypatch.setattr("packages.core.database.async_session", fake_session)
    monkeypatch.setattr(
        conversation_history,
        "_get_conversation_for_history",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        complete,
    )
    monkeypatch.setattr(
        conversation_history,
        "_summary_source_is_active",
        AsyncMock(return_value=True),
    )
    publish = AsyncMock()
    monkeypatch.setattr(conversation_history, "_publish_conversation_summary", publish)

    await conversation_history.update_conversation_summary(
        "conversation-1",
        [SimpleNamespace(
            role="user",
            content='[Server-recorded sender: {"label": "P-ABCDEF123456", "name": "Alex Chen"}]\nPlease review.',
        )],
    )

    prompt = complete.await_args.kwargs["text_block"]
    assert "preserve the server-recorded sender label" in prompt
    assert "Do not assign an unknown participant's words" in prompt
    assert "P-ABCDEF123456" in prompt
    assert "Please review." in prompt
    publish.assert_awaited_once_with(
        "conversation-1", conversation, "P-ABCDEF123456 asked for review.",
    )
