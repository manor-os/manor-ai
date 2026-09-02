from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_startup_greetings_group_services_by_agent_without_changing_subscriptions(
    db_session, monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.workspace_setup_service import dispatch_workspace_post_commit
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="AI SDE Preparation",
        status="active", heartbeat_enabled=False,
        settings={"provisioning": {"post_commit_dispatch": "pending"}},
    )
    agents = [
        Agent(id=generate_ulid(), entity_id=entity_id, name=name, system_prompt=name)
        for name in ["Interview Coach", "Interview Coach", "Behavioral Coach"]
    ]
    now = datetime.now(timezone.utc)
    specifications = [
        (agents[0], "course_delivery", "active", workspace.id),
        (agents[1], "resume_optimization", "active", workspace.id),
        (agents[0], "interview_preparation", "active", workspace.id),
        (agents[0], "mock_assessment", "active", workspace.id),
        (agents[2], None, "active", workspace.id),
        (agents[0], "mock_assessment", "active", workspace.id),
        (agents[0], "inactive_service", "paused", workspace.id),
        (agents[0], "other_workspace_service", "active", generate_ulid()),
    ]
    subscriptions = [
        AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, agent_id=agent.id,
            workspace_id=workspace_id, service_key=service, status=status,
            created_at=now + timedelta(seconds=index),
        )
        for index, (agent, service, status, workspace_id) in enumerate(specifications)
    ]
    db_session.add_all([workspace, *agents, *subscriptions])
    await db_session.commit()
    dispatched: list[tuple] = []
    progress: list[tuple] = []
    monkeypatch.setattr(ai_tasks.send_agent_greetings, "delay", lambda *args: dispatched.append(args))

    result = await dispatch_workspace_post_commit(
        db_session, workspace_id=workspace.id, entity_id=entity_id,
        progress=lambda step, payload: progress.append((step, payload)),
    )
    await db_session.commit()

    assert result["agent_greetings_dispatched"] is True
    assert len(dispatched) == 1
    payload = dispatched[0][4]
    assert [item["agent_id"] for item in payload] == [agent.id for agent in agents]
    assert [item["subscription_id"] for item in payload] == [
        subscriptions[index].id for index in (0, 1, 4)
    ]
    assert [item["service_key"] for item in payload] == [
        "course_delivery, interview_preparation, mock_assessment",
        "resume_optimization",
        "general",
    ]
    assert [item["agent_name"] for item in payload] == [agent.name for agent in agents]
    assert ("agent_greetings_dispatched", {"count": 3}) in progress
    persisted = list((await db_session.execute(
        select(AgentSubscription).where(AgentSubscription.entity_id == entity_id)
    )).scalars().all())
    assert {row.id: (row.agent_id, row.service_key, row.status, row.workspace_id) for row in persisted} == {
        row.id: (agent.id, service, status, workspace_id)
        for row, (agent, service, status, workspace_id) in zip(subscriptions, specifications)
    }
    replay = await dispatch_workspace_post_commit(
        db_session, workspace_id=workspace.id, entity_id=entity_id,
    )
    assert replay["reason"] == "already_dispatched"
    assert len(dispatched) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_grouped_greeting_reaches_runtime_and_preserves_playback_metadata(monkeypatch, fallback):
    import asyncio

    from packages.core.ai import runtime
    from packages.core.tasks import ai_tasks
    from packages.core.workspace_chat import notifiers

    completions: list[dict] = []
    messages: list[dict] = []

    async def complete(**kwargs):
        completions.append(kwargs)
        if fallback:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(content="Hello, I can help with all three services.")

    async def notify(**kwargs):
        messages.append(kwargs)

    monkeypatch.setattr(ai_tasks, "_run_async", asyncio.run)
    monkeypatch.setattr(runtime, "runtime_execute_agent_greeting_completion", complete)
    monkeypatch.setattr(notifiers, "notify_agent_greeting", notify)
    services = "course_delivery, interview_preparation, mock_assessment"
    ai_tasks.send_agent_greetings.run(
        "entity-1", "workspace-1", "AI SDE Preparation", "training",
        [
            {"subscription_id": "subscription-1", "agent_id": "agent-1",
             "agent_name": "Coach", "service_key": services},
            {"subscription_id": "subscription-2", "agent_id": "agent-2",
             "agent_name": "Resume Coach", "service_key": "resume_optimization"},
        ],
    )
    assert len(completions) == len(messages) == 2
    assert completions[0]["service_key"] == services
    assert [message["subscription_id"] for message in messages] == ["subscription-1", "subscription-2"]
    assert [message["sequence"] for message in messages] == [0, 1]
    assert all(message["total"] == 2 for message in messages)
    if fallback:
        assert services.replace("_", " ") in messages[0]["greeting"]


@pytest.mark.asyncio
async def test_agent_greeting_is_persisted_with_playback_metadata(monkeypatch):
    from packages.core.workspace_chat import notifiers

    events: list[str] = []
    posted: dict = {}
    message = SimpleNamespace(id="message-1")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            events.append("commit")

    async def fake_post_message(_db, **kwargs):
        events.append("post")
        posted.update(kwargs)
        return message

    async def fake_publish(entity_id, *, workspace_id, message):
        events.append("publish")
        assert entity_id == "entity-1"
        assert workspace_id == "workspace-1"
        assert message.id == "message-1"

    monkeypatch.setattr(notifiers, "async_session", FakeSession)
    monkeypatch.setattr(notifiers.chat, "post_message", fake_post_message)
    monkeypatch.setattr(
        notifiers.chat,
        "publish_workspace_chat_message_event",
        fake_publish,
    )

    await notifiers.notify_agent_greeting(
        entity_id="entity-1",
        workspace_id="workspace-1",
        subscription_id="subscription-1",
        greeting="Hello from Alex",
        sequence=1,
        total=3,
    )

    assert posted["message_kind"] == "agent_update"
    assert posted["author_subscription_id"] == "subscription-1"
    assert posted["publish_event"] is False
    assert posted["meta"] == {
        "agent_greeting": True,
        "agent_greeting_sequence": 1,
        "agent_greeting_total": 3,
    }
    assert events == ["post", "commit", "publish"]


@pytest.mark.asyncio
async def test_agent_greeting_is_not_published_when_commit_fails(monkeypatch):
    from packages.core.workspace_chat import notifiers

    events: list[str] = []

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            events.append("commit")
            raise RuntimeError("commit failed")

    async def fake_post_message(_db, **_kwargs):
        events.append("post")
        return SimpleNamespace(id="message-1")

    async def fake_publish(*_args, **_kwargs):
        events.append("publish")

    monkeypatch.setattr(notifiers, "async_session", FailingSession)
    monkeypatch.setattr(notifiers.chat, "post_message", fake_post_message)
    monkeypatch.setattr(
        notifiers.chat,
        "publish_workspace_chat_message_event",
        fake_publish,
    )

    await notifiers.notify_agent_greeting(
        entity_id="entity-1",
        workspace_id="workspace-1",
        subscription_id="subscription-1",
        greeting="Hello from Alex",
        sequence=0,
        total=1,
    )

    assert events == ["post", "commit"]


@pytest.mark.asyncio
async def test_agent_greeting_marker_is_published_with_workspace_chat_event(monkeypatch):
    from packages.core.workspace_chat import service

    published: dict = {}

    async def fake_publish(entity_id, data):
        published["entity_id"] = entity_id
        published["data"] = data

    monkeypatch.setattr(service, "_publish_workspace_chat_event", fake_publish)

    message = SimpleNamespace(
        id="message-1",
        message_kind="agent_update",
        author_kind="agent",
        pending_action=None,
        meta={
            "agent_greeting": True,
            "agent_greeting_sequence": 2,
            "agent_greeting_total": 4,
        },
    )
    await service.publish_workspace_chat_message_event(
        "entity-1",
        workspace_id="workspace-1",
        message=message,
    )

    assert published["data"] == {
        "workspace_id": "workspace-1",
        "message_id": "message-1",
        "message_kind": "agent_update",
        "author_kind": "agent",
        "has_pending_action": False,
        "action_kind": None,
        "agent_greeting": True,
        "agent_greeting_sequence": 2,
        "agent_greeting_total": 4,
    }


@pytest.mark.asyncio
async def test_regular_workspace_chat_event_does_not_gain_greeting_fields(monkeypatch):
    from packages.core.workspace_chat import service

    published: dict = {}

    async def fake_publish(_entity_id, data):
        published.update(data)

    monkeypatch.setattr(service, "_publish_workspace_chat_event", fake_publish)

    message = SimpleNamespace(
        id="message-2",
        message_kind="text",
        author_kind="user",
        pending_action=None,
        meta={},
    )
    await service.publish_workspace_chat_message_event(
        "entity-1",
        workspace_id="workspace-1",
        message=message,
    )

    assert published == {
        "workspace_id": "workspace-1",
        "message_id": "message-2",
        "message_kind": "text",
        "author_kind": "user",
        "has_pending_action": False,
        "action_kind": None,
    }
