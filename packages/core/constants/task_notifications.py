"""Task notification event/channel metadata.

This file is product-facing metadata, not delivery logic. Runtime fan-out
lives in ``services.event_emitter`` and ``services.task_event_notifications``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


TASK_NOTIFICATION_CHANNELS: dict[str, dict[str, Any]] = {
    "task_log": {
        "label": "Task timeline",
        "status": "active",
        "description": "Persistent per-task execution and audit log.",
    },
    "event_log": {
        "label": "Domain event log",
        "status": "active",
        "description": "Workspace-level audit stream for task domain events.",
    },
    "in_app": {
        "label": "In-app notification",
        "status": "active",
        "description": "Notification center row for directly involved users.",
    },
    "websocket": {
        "label": "Realtime push",
        "status": "active",
        "description": "WebSocket push emitted when an in-app notification is created.",
    },
    "webhook": {
        "label": "Webhook",
        "status": "active",
        "description": "Entity webhook delivery for subscribed domain events.",
    },
    "workspace_chat": {
        "label": "Workspace chat",
        "status": "available",
        "description": "Agent messages and cards in workspace/task chat threads.",
    },
    "email": {
        "label": "Email",
        "status": "available",
        "description": "Configurable post-commit email delivery for important task events.",
    },
    "external_chat": {
        "label": "External chat",
        "status": "available",
        "description": "Configurable Slack or team-chat delivery for important task events.",
    },
    "push": {
        "label": "Browser/mobile push",
        "status": "planned",
        "description": "Native push notification for time-sensitive task events.",
    },
    "sms": {
        "label": "SMS",
        "status": "planned",
        "description": "High-urgency fallback channel for escalation policies.",
    },
}


TASK_NOTIFICATION_EVENTS: dict[str, dict[str, Any]] = {
    "task.created": {
        "label": "Task created",
        "notification_type": "task_created",
        "severity": "info",
        "description": "A user-visible task was created.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": None,
    },
    "task.assigned": {
        "label": "Task assigned",
        "notification_type": "task_assigned",
        "severity": "info",
        "description": "A user or staff member was assigned to a task.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": "Open the task to review the assignment.",
    },
    "task.status_changed": {
        "label": "Task status changed",
        "notification_type": "task_status_changed",
        "severity": "info",
        "description": "A task moved to a different status.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": None,
    },
    "task.succeeded": {
        "label": "Task completed",
        "notification_type": "task_succeeded",
        "severity": "success",
        "description": "Plan or legacy agent execution completed successfully.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": None,
    },
    "task.failed": {
        "label": "Task failed",
        "notification_type": "task_failed",
        "severity": "error",
        "description": "Plan or legacy agent execution reached a terminal failure.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": "Review error details or retry failed steps.",
    },
    "task.hitl_requested": {
        "label": "Input requested",
        "notification_type": "task_hitl_requested",
        "severity": "warning",
        "description": "Execution paused because a worker needs structured human input.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": "Submit the requested HITL response.",
    },
    "task.hitl_reminder": {
        "label": "Input reminder",
        "notification_type": "task_hitl_reminder",
        "severity": "warning",
        "description": "A waiting_human step exceeded the reminder threshold.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": "Respond to the pending HITL prompt.",
    },
    "task.retried": {
        "label": "Retry started",
        "notification_type": "task_retried",
        "severity": "info",
        "description": "A task, plan, or failed plan step was manually retried.",
        "default_channels": ["task_log", "event_log", "in_app", "websocket", "webhook"],
        "configurable_channels": ["email", "external_chat"],
        "user_action": None,
    },
}


def task_notification_channels() -> dict[str, dict[str, Any]]:
    """Return a copy safe for API responses."""
    return deepcopy(TASK_NOTIFICATION_CHANNELS)


def task_notification_events() -> dict[str, dict[str, Any]]:
    """Return a copy safe for API responses."""
    return deepcopy(TASK_NOTIFICATION_EVENTS)
