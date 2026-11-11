"""What a watcher returns: the situations that exist right now."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DesiredItem:
    entity_id: str
    workspace_id: Optional[str]
    kind: str
    subject_type: str
    subject_id: str
    urgency: str
    title: str
    opened_at: datetime
    stage: int = 0
    summary: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    assignee_user_id: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        # Stage is deliberately absent: an aging situation stays one item.
        return ":".join((self.kind, self.workspace_id or "-", self.subject_type, self.subject_id))


class Watcher(Protocol):
    """Read-only, deterministic: no LLM, no writes, no side effects."""

    key: str

    async def desired(self, db: AsyncSession, *, now: datetime) -> list[DesiredItem]:
        ...


def stage_for_age(opened_at: datetime, now: datetime, thresholds_hours: tuple[int, ...]) -> int:
    """0 before the first threshold, then one more per threshold passed."""
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    age_hours = (now - opened_at).total_seconds() / 3600
    return sum(1 for hours in thresholds_hours if age_hours >= hours)
