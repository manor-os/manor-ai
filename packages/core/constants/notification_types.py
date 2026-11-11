"""Catalog of known notification event types.

Producers across the codebase already call ``notify()`` /
``create_notification()`` with various ``type`` strings (``task_assigned``,
``task_failed``, ``video``, ``workspace_digest``, …). The routing layer needs a
canonical list so:

  - the user preference UI can render a matrix of (event_kind × channel)
    without the frontend having to invent labels for every legacy type
  - the dispatcher knows the default severity for an unknown event
  - new producers can pick a stable kind from one place

Each entry maps an event kind to a category + default severity. The
category buckets related events together in the UI so users don't have to
toggle 30 individual rows.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, NotRequired, TypedDict

Severity = Literal["info", "warn", "critical"]
Category = Literal["task", "agent", "media", "system", "billing", "calendar", "marketplace"]


class NotificationChannel(str, Enum):
    """Stable channel keys shared by routing and delivery."""

    INAPP = "inapp"
    EMAIL = "email"
    TELEGRAM = "telegram"
    WECHAT = "wechat"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    DISCORD = "discord"
    TWILIO_SMS = "twilio_sms"
    DATABASE = "db"
    WEBSOCKET = "ws"
    BROADCAST = "broadcast"


class NotificationDispatchStatus(str, Enum):
    """Visibility lifecycle of the in-app Notification parent."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    CANCELED = "canceled"


class NotificationOutboxStatus(str, Enum):
    """Durable external fan-out lifecycle."""

    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELED = "canceled"


class NotificationDeliveryStatus(str, Enum):
    """Per-channel actionable delivery lifecycle."""

    PENDING = "pending"
    SENT = "sent"
    RESOLVED = "resolved"
    FAILED = "failed"
    EXPIRED = "expired"


class NotificationCallbackDisposition(str, Enum):
    """How an actionable callback changes its delivery lifecycle."""

    RESOLVED = "resolved"
    RETRYABLE = "retryable"
    TERMINAL_FAILURE = "terminal_failure"


class EventDescriptor(TypedDict):
    kind: str
    category: Category
    severity: Severity
    label: str
    description: str
    #: Channels this kind uses until the user overrides the kind. It outranks
    #: the user's global ``default_channels`` and workspace/entity default
    #: routes, but not a per-kind override. Without it, those apply.
    default_channels: NotRequired[list[str]]


# Order matters: it's the order the UI renders them.
EVENT_CATALOG: list[EventDescriptor] = [
    # Task lifecycle
    {
        "kind": "task_hitl_requested",
        "category": "task",
        "severity": "warn",
        "label": "Task needs your input",
        "description": "A task paused waiting for human review or approval.",
    },
    {
        "kind": "task_hitl_reminder",
        "category": "task",
        "severity": "info",
        "label": "Task input reminder",
        "description": "Reminder that a paused task is still waiting on you.",
    },
    {
        "kind": "task_assigned",
        "category": "task",
        "severity": "info",
        "label": "Task assigned to you",
        "description": "Someone (human or agent) assigned a task to you.",
    },
    {
        "kind": "task_failed",
        "category": "task",
        "severity": "warn",
        "label": "Task failed",
        "description": "A task ended in failure and may need attention.",
    },
    {
        "kind": "task_succeeded",
        "category": "task",
        "severity": "info",
        "label": "Task completed",
        "description": "A task you own finished successfully.",
    },
    {
        "kind": "task_retried",
        "category": "task",
        "severity": "info",
        "label": "Task retried",
        "description": "A task was restarted from a previous failure.",
    },
    {
        "kind": "task_sla_breach",
        "category": "task",
        "severity": "warn",
        "label": "Task SLA breach",
        "description": "A task crossed its SLA deadline.",
    },
    {
        "kind": "task_overdue",
        "category": "task",
        "severity": "info",
        "label": "Task past its deadline",
        "description": "A task you are on passed its deadline and is not finished.",
    },
    {
        "kind": "task_comment_mention",
        "category": "task",
        "severity": "info",
        "label": "Mentioned in a task comment",
        "description": "Someone mentioned you in a comment on a task.",
    },
    {
        "kind": "workflow",
        "category": "task",
        "severity": "info",
        "label": "Workflow notification",
        "description": "A Workflow's notify step sent you an update.",
    },
    # Calendar / bookings
    {
        "kind": "booking_confirmed",
        "category": "calendar",
        "severity": "info",
        "label": "Booking confirmed",
        "description": "Someone booked time through one of your booking links.",
    },
    # Media / generation jobs
    {
        "kind": "video",
        "category": "media",
        "severity": "info",
        "label": "Video ready",
        "description": "A requested video generation finished.",
    },
    {
        "kind": "image",
        "category": "media",
        "severity": "info",
        "label": "Image ready",
        "description": "A requested image generation finished.",
    },
    {
        "kind": "document",
        "category": "media",
        "severity": "info",
        "label": "Document ready",
        "description": "A generated PDF / report / file is ready.",
    },
    # Agent / chat
    {
        "kind": "agent_message",
        "category": "agent",
        "severity": "info",
        "label": "Agent message",
        "description": "An agent posted a message addressed to you.",
    },
    {
        "kind": "proposal",
        "category": "agent",
        "severity": "info",
        "label": "Strategist proposal",
        "description": "The Workspace Strategist proposed work for you to review, or started work you pre-approved.",
        # Stays on the bell unless the person chooses more channels.
        "default_channels": ["inapp"],
    },
    {
        "kind": "agent_initiative_update",
        "category": "agent",
        "severity": "info",
        "label": "Agent initiative update",
        "description": "A proactive check you set up for an agent found something to tell you.",
    },
    {
        "kind": "channel_inbound_message",
        "category": "agent",
        "severity": "info",
        "label": "Message from a contact",
        "description": "Someone wrote in through a connected channel, such as email or a chat app.",
    },
    # System
    {
        "kind": "workspace_setup_required",
        "category": "system",
        "severity": "warn",
        "label": "Workspace connection needs attention",
        "description": "A required Workspace integration or session disconnected or was added by a Blueprint update.",
    },
    {
        "kind": "integration_account_issue",
        "category": "system",
        "severity": "warn",
        "label": "Connected account needs attention",
        "description": "A connected account stopped working and the agents that use it are affected.",
    },
    {
        "kind": "integration_session_expired",
        "category": "system",
        "severity": "warn",
        "label": "Connected session expired",
        "description": "A signed-in browser session an integration uses expired and needs reconnecting.",
    },
    {
        "kind": "workspace_digest",
        "category": "system",
        "severity": "info",
        "label": "Daily Workspace digest",
        "description": "Once a day: what changed, what is waiting on you, and accounts that need attention.",
    },
    {
        "kind": "integration_shared",
        "category": "system",
        "severity": "info",
        "label": "Connected account shared with you",
        "description": "A teammate shared one of their connected accounts so your agents can use it.",
    },
    {
        "kind": "automation_paused",
        "category": "system",
        "severity": "warn",
        "label": "Automation paused",
        "description": "An automation was paused after repeated failures.",
    },
    {
        "kind": "workspace_update_available",
        "category": "system",
        "severity": "info",
        "label": "Workspace updates",
        "description": "The provider of a Workspace delivered to you sent a new version.",
    },
    {
        "kind": "system",
        "category": "system",
        "severity": "info",
        "label": "System message",
        "description": "Generic platform announcement.",
    },
]


# Stable key → descriptor for fast lookup.
EVENTS_BY_KIND: dict[str, EventDescriptor] = {e["kind"]: e for e in EVENT_CATALOG}


# Channel keys recognised by the routing layer. ``inapp`` is the bell-icon
# experience inside the Manor web/mobile UI (db row + WebSocket push);
# everything else corresponds to a ``ChannelAdapter`` registered in
# ``packages.core.services.channels``.
SUPPORTED_CHANNELS: list[str] = [
    channel.value
    for channel in (
        NotificationChannel.INAPP,
        NotificationChannel.EMAIL,
        NotificationChannel.TELEGRAM,
        NotificationChannel.WECHAT,
        NotificationChannel.SLACK,
        NotificationChannel.DISCORD,
        NotificationChannel.TWILIO_SMS,
    )
]


def event_default_severity(kind: str) -> Severity:
    """Return the catalog severity for ``kind``, defaulting to ``info``."""
    entry = EVENTS_BY_KIND.get(kind)
    return entry["severity"] if entry else "info"


def event_category(kind: str) -> Category | None:
    entry = EVENTS_BY_KIND.get(kind)
    return entry["category"] if entry else None


def event_default_channels(kind: str) -> list[str]:
    entry = EVENTS_BY_KIND.get(kind)
    return list(entry.get("default_channels") or []) if entry else []
