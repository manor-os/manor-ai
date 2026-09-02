"""Proposal cohort and item lifecycle vocabularies."""
from __future__ import annotations

from enum import Enum


class ProposalStatus(str, Enum):
    """Every persisted state of a proposal cohort."""

    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"

    __str__ = str.__str__
    __format__ = str.__format__


class ProposalItemKind(str, Enum):
    """Closed set of Strategist proposal item execution handlers."""

    TASK = "task"
    HUMAN_REQUEST = "human_request"
    AUTOMATION_CHANGE = "automation_change"
    WORKFLOW_CHANGE = "workflow_change"
    GOAL_CHANGE = "goal_change"
    EXPERIMENT = "experiment"
    WORKFLOW_RUN = "workflow_run"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class ProposalItemStatus(str, Enum):
    """Every persisted state of a governed proposal item."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class ProposalDecisionKind(str, Enum):
    """Human/system decisions accepted by the proposal decision API."""

    APPROVED = "approved"
    REJECTED = "rejected"

    __str__ = str.__str__
    __format__ = str.__format__


PROPOSAL_ITEM_OPEN_STATUSES: tuple[ProposalItemStatus, ...] = (
    ProposalItemStatus.PROPOSED,
    ProposalItemStatus.APPROVED,
    ProposalItemStatus.EXECUTING,
)

PROPOSAL_ITEM_TERMINAL_STATUSES: tuple[ProposalItemStatus, ...] = (
    ProposalItemStatus.REJECTED,
    ProposalItemStatus.EXPIRED,
    ProposalItemStatus.SUCCEEDED,
    ProposalItemStatus.FAILED,
    ProposalItemStatus.CANCELLED,
)


assert set(PROPOSAL_ITEM_OPEN_STATUSES).isdisjoint(PROPOSAL_ITEM_TERMINAL_STATUSES)
assert set(PROPOSAL_ITEM_OPEN_STATUSES) | set(PROPOSAL_ITEM_TERMINAL_STATUSES) == set(
    ProposalItemStatus
)
