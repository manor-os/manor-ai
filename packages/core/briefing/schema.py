"""Pydantic schema for briefing output.

The triage LLM returns one ``Briefing`` per cycle. Each ``BriefingItem``
covers one inbox signal (an email, a Slack DM, a calendar conflict, …)
with a category + an optional draft reply.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


BriefingCategory = Literal[
    "urgent",        # needs operator action today
    "respond",       # routine reply expected; draft attached
    "fyi",           # informational; nothing to do
    "auto_handled",  # the system already actioned this (drafted + sent, etc.)
    "skip",          # ignore (newsletters, transactional receipts)
]
"""Five-bucket classifier — kept tight so the operator can scan a
briefing in <30 seconds. Each item lands in exactly one bucket."""


class BriefingAction(BaseModel):
    """Suggested next step for an item. UI renders as buttons."""

    kind: Literal["send_reply", "schedule", "create_task", "snooze", "archive", "manual"]
    label: str = Field(..., max_length=80)
    """Verb the operator sees on the button."""
    payload: Optional[dict] = None
    """Action-specific args — e.g. {draft: "...", thread_id: "..."} for
    send_reply; {date: "...", topic: "..."} for schedule."""


class BriefingItem(BaseModel):
    source: Literal["gmail", "slack", "calendar", "stripe", "manual"]
    source_ref: str = Field(..., max_length=128)
    """The provider's id for this signal — gmail message id, slack ts,
    calendar event id. Used by actions to know what to act on."""

    category: BriefingCategory
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(..., max_length=600)
    """One-line operator-readable framing. NOT the original message."""

    received_at: Optional[str] = None
    """ISO-8601 string. Plain str so JSON serialisation is trivial."""
    sender: Optional[str] = None

    draft_reply: Optional[str] = Field(default=None, max_length=2000)
    """LLM-drafted reply for ``respond`` items. None for other categories."""

    actions: list[BriefingAction] = Field(default_factory=list)


class Briefing(BaseModel):
    briefing_id: str = Field(..., min_length=1, max_length=64)
    """Stable per-cycle id. Persisted in chat message refs so the
    operator can correlate later."""

    headline: str = Field(..., min_length=1, max_length=300)
    """The opening sentence the operator sees. Should answer "what
    actually matters today?" in <30 words."""

    items: list[BriefingItem] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=600)
    """Free-form observations — surfaced under the items list. Use
    for trends, anomalies, etc. not tied to a specific item."""

    metrics_snapshot: Optional[dict] = None
    """Optional summary of metrics that moved overnight. Briefing
    chat card renders as a compact table."""
