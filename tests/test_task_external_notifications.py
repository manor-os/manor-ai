import pytest

from packages.core.models.channel import ChannelConfig
from packages.core.models.base import generate_ulid
from packages.core.services.task_external_notifications import (
    DEFAULT_EXTERNAL_EVENTS,
    _deliver_external_chat,
    task_event_external_channel_enabled,
    task_notification_policy_config,
)


def test_task_notification_policy_workspace_overrides_entity():
    entity_settings = {
        "notification_policy": {
            "task_events": {
                "email": {"enabled": True, "events": ["task.failed"]},
                "external_chat": {"enabled": False},
            }
        }
    }
    workspace_settings = {
        "notification_policy": {
            "task_events": {
                "external_chat": {"enabled": True, "target": "#ops"},
            }
        }
    }

    policy = task_notification_policy_config(entity_settings, workspace_settings)

    assert policy["email"]["enabled"] is True
    assert policy["external_chat"]["enabled"] is True
    assert policy["external_chat"]["target"] == "#ops"


def test_external_channel_enabled_uses_default_events_and_overrides():
    policy = {
        "email": {"enabled": True},
        "external_chat": {"enabled": True, "events": ["task.failed"]},
        "events": {"task.succeeded": {"email": True}},
    }

    assert task_event_external_channel_enabled(policy, "email", DEFAULT_EXTERNAL_EVENTS[0])
    assert task_event_external_channel_enabled(policy, "email", "task.succeeded")
    assert task_event_external_channel_enabled(policy, "external_chat", "task.failed")
    assert not task_event_external_channel_enabled(policy, "external_chat", "task.hitl_requested")
    assert not task_event_external_channel_enabled(policy, "sms", "task.failed")


@pytest.mark.asyncio
async def test_external_chat_requires_explicit_channel_config_ids(db_session, monkeypatch):
    """A task policy without a concrete config must not fan out to every
    active channel in the entity.
    """
    entity_id = generate_ulid()
    db_session.add_all([
        ChannelConfig(
            entity_id=entity_id,
            owner_user_id="owner_a",
            channel_type="slack",
            provider="slack_app",
            config={"default_target": "#ops-a"},
            credentials={},
            status="active",
        ),
        ChannelConfig(
            entity_id=entity_id,
            owner_user_id="owner_b",
            channel_type="slack",
            provider="slack_app",
            config={"default_target": "#ops-b"},
            credentials={},
            status="active",
        ),
    ])
    await db_session.commit()

    sent_to: list[str] = []

    async def record_send(config, _text, _policy):
        sent_to.append(config.id)
        return True

    monkeypatch.setattr(
        "packages.core.services.task_external_notifications._send_channel_message",
        record_send,
    )

    with pytest.raises(RuntimeError, match="channel_config_ids is required"):
        await _deliver_external_chat(
            db_session,
            entity_id,
            "task.failed",
            {},
            None,
            {"channel_types": ["slack"]},
        )

    assert sent_to == []


@pytest.mark.asyncio
async def test_external_chat_reports_configured_channel_failure(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    config = ChannelConfig(
        entity_id=entity_id,
        owner_user_id="owner_a",
        channel_type="slack",
        provider="slack_app",
        config={"default_target": "#ops-a"},
        credentials={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    async def failed_send(_config, _text, _policy):
        return False

    monkeypatch.setattr(
        "packages.core.services.task_external_notifications._send_channel_message",
        failed_send,
    )

    with pytest.raises(RuntimeError, match="task external chat failed"):
        await _deliver_external_chat(
            db_session,
            entity_id,
            "task.failed",
            {},
            None,
            {"channel_types": ["slack"], "channel_config_ids": [config.id]},
        )


@pytest.mark.asyncio
async def test_external_chat_retry_skips_already_completed_channel(
    db_session,
    monkeypatch,
):
    entity_id = generate_ulid()
    first = ChannelConfig(
        entity_id=entity_id,
        workspace_id="workspace-b",
        owner_user_id="owner_a",
        channel_type="slack",
        provider="slack_app",
        config={"default_target": "#ops-a"},
        credentials={},
        status="active",
    )
    second = ChannelConfig(
        entity_id=entity_id,
        workspace_id="workspace-a",
        owner_user_id="owner_b",
        channel_type="slack",
        provider="slack_app",
        config={"default_target": "#ops-b"},
        credentials={},
        status="active",
    )
    db_session.add_all([first, second])
    await db_session.commit()

    completed: dict[str, bool] = {}
    sent_to: list[str] = []
    fail_second = True

    async def mark_completed(key: str) -> None:
        completed[key] = True

    async def record_send(config, _text, _policy):
        sent_to.append(config.id)
        if config.id == second.id and fail_second:
            return False
        return True

    monkeypatch.setattr(
        "packages.core.services.task_external_notifications._send_channel_message",
        record_send,
    )

    with pytest.raises(RuntimeError, match="task external chat failed"):
        await _deliver_external_chat(
            db_session,
            entity_id,
            "task.failed",
            {},
            None,
            {
                "channel_types": ["slack"],
                "channel_config_ids": [first.id, second.id],
            },
            completed_sinks=completed,
            mark_sink_completed=mark_completed,
        )

    assert sent_to == [first.id, second.id]
    assert completed == {f"task_external.external_chat:{first.id}": True}

    fail_second = False
    await _deliver_external_chat(
        db_session,
        entity_id,
        "task.failed",
        {},
        None,
        {
            "channel_types": ["slack"],
            "channel_config_ids": [first.id, second.id],
        },
        completed_sinks=completed,
        mark_sink_completed=mark_completed,
    )

    assert sent_to == [first.id, second.id, second.id]
    assert completed == {
        f"task_external.external_chat:{first.id}": True,
        f"task_external.external_chat:{second.id}": True,
    }
