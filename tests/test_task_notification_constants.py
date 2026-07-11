from packages.core.constants.task_notifications import (
    task_notification_channels,
    task_notification_events,
)
from packages.core.services.task_event_notifications import _TASK_EVENT_COPY


def test_task_notification_event_matrix_matches_runtime_notifications():
    events = task_notification_events()

    assert set(_TASK_EVENT_COPY).issubset(events)
    for event_type, (notification_type, _title) in _TASK_EVENT_COPY.items():
        event = events[event_type]
        assert event["notification_type"] == notification_type
        assert "event_log" in event["default_channels"]
        assert "in_app" in event["default_channels"]
        assert "websocket" in event["default_channels"]
        assert "email" in event["configurable_channels"]
        assert "external_chat" in event["configurable_channels"]


def test_task_notification_channels_are_api_safe_copies():
    channels = task_notification_channels()
    channels["email"]["status"] = "active"

    fresh = task_notification_channels()
    assert fresh["email"]["status"] == "available"
    assert fresh["external_chat"]["status"] == "available"
    assert fresh["webhook"]["status"] == "active"
