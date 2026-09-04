from types import SimpleNamespace

import pytest


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
