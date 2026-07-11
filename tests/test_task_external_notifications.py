from packages.core.services.task_external_notifications import (
    DEFAULT_EXTERNAL_EVENTS,
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
