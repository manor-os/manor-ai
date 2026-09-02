"""Strategist review and consolidation lifecycle vocabularies."""
from __future__ import annotations

from enum import Enum


DEFAULT_REVIEW_LEASE_SECONDS = 90
LEGACY_REVIEW_RECOVERY_GRACE_SECONDS = 15 * 60


class ReviewRunStatus(str, Enum):
    """Every persisted state of one Strategist review cycle."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class ReviewSkipReason(str, Enum):
    """Stable machine-readable reasons a review may not produce a proposal.

    Free-form detail belongs beside the reason (for example the workspace's
    actual state), never inside the value behavior branches on.
    """

    REVIEW_ALREADY_RUNNING = "review_already_running"
    WORKSPACE_INACTIVE = "workspace_inactive"
    ACTIVE_WORK_BATCH = "active_work_batch"
    OPEN_PROPOSALS = "open_proposals"
    TRIGGER_CONDITION = "trigger_condition"
    UNKNOWN = "unknown"

    __str__ = str.__str__
    __format__ = str.__format__


class ReviewCycleOutcome(str, Enum):
    """Typed outcome used by the Review orchestrator's internal branches."""

    COMPLETED = "completed"
    SKIPPED = "skipped"
    NEEDS_DECISION = "needs_decision"

    __str__ = str.__str__
    __format__ = str.__format__


class ConsolidationReportStatus(str, Enum):
    """Completeness of one observation-only consolidation report."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]
