"""Catalog of known notification event types.

Producers across the codebase already call ``notify()`` /
``create_notification()`` with various ``type`` strings (``task_assigned``,
``task_failed``, ``video``, ``system_health``, …). The routing layer needs a
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

from typing import Literal, TypedDict

Severity = Literal["info", "warn", "critical"]
Category = Literal["task", "agent", "media", "system", "billing", "calendar"]


class EventDescriptor(TypedDict):
    kind: str
    category: Category
    severity: Severity
    label: str
    description: str


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
        "severity": "warn",
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
    # System
    {
        "kind": "system_health",
        "category": "system",
        "severity": "warn",
        "label": "System health alert",
        "description": "A platform-level health check raised an alert.",
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
    "inapp",
    "email",
    "telegram",
    "wechat",
    "whatsapp",
    "slack",
    "discord",
    "twilio_sms",
]


def event_default_severity(kind: str) -> Severity:
    """Return the catalog severity for ``kind``, defaulting to ``info``."""
    entry = EVENTS_BY_KIND.get(kind)
    return entry["severity"] if entry else "info"


def event_category(kind: str) -> Category | None:
    entry = EVENTS_BY_KIND.get(kind)
    return entry["category"] if entry else None
