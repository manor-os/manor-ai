"""Consolidator protocol + shared helpers (M3/M4).

A consolidator is a *read-only*, deterministic (L0, v1 — no LLM calls)
summarizer for one domain of the workspace. ``registry.run_all`` hands
every consolidator the same cheap :class:`SnapshotContext` bundle —
the frozen review, the workspace row, and the window's ledger events
preloaded once — and expects back a validated
:class:`~packages.core.consolidators.contract.ConsolidationReportModel`.

Read-only discipline: ``run`` must never add/mutate/delete ORM objects
or flush the session; all persistence happens in ``run_all`` *after*
the consolidator returns (enforced by a ``before_flush`` listener in
tests). Consolidators also must not import strategist/proposals/
approvals decision modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.contract import ConsolidationReportModel
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent


@dataclass
class SnapshotContext:
    """Cheap bundle handed to every consolidator.

    ``events`` is the review's frozen ledger window
    (``events_in_window(review)``), preloaded once by ``run_all`` so the
    eight consolidators do not re-scan the ledger. ``workspace`` may be
    ``None`` when the workspace row has been deleted mid-review.
    """

    review: ReviewRun
    workspace: Optional[Workspace]
    events: list[WorkspaceEvent]

    def events_of(self, *event_types: str) -> list[WorkspaceEvent]:
        wanted = set(event_types)
        return [event for event in self.events if event.event_type in wanted]

    @property
    def now(self) -> datetime:
        """The review's clock: frozen window end, falling back to wall time."""
        return self.review.window_end or datetime.now(timezone.utc)

    def window_duration_seconds(self) -> Optional[float]:
        if self.review.window_start is None or self.review.window_end is None:
            return None
        return (self.review.window_end - self.review.window_start).total_seconds()


@runtime_checkable
class Consolidator(Protocol):
    domain: str
    analyzer_version: str
    critical: bool  # True → missing report forbids high-risk item kinds (M8 validator)

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        ...


def evidence_ids(events: list[WorkspaceEvent]) -> list[str]:
    """workspace_events ids as evidence refs (stable, replayable)."""
    return [event.id for event in events]


def age_hours(started: Optional[datetime], now: datetime) -> Optional[float]:
    """Hours between ``started`` and ``now`` (naive datetimes treated as UTC)."""
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max((now - started).total_seconds() / 3600.0, 0.0)
