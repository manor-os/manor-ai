"""Closed Strategist lifecycle vocabularies and typed cycle outcomes."""
from __future__ import annotations

from packages.core.constants.proposal import (
    PROPOSAL_ITEM_OPEN_STATUSES,
    PROPOSAL_ITEM_TERMINAL_STATUSES,
    ProposalDecisionKind,
    ProposalItemKind,
    ProposalItemStatus,
    ProposalStatus,
)
from packages.core.constants.review import (
    ConsolidationReportStatus,
    ReviewCycleOutcome,
    ReviewRunStatus,
    ReviewSkipReason,
)
from packages.core.strategist.orchestrator import ReviewCycleResult


def test_proposal_item_status_partition_is_complete() -> None:
    assert set(PROPOSAL_ITEM_OPEN_STATUSES).isdisjoint(
        PROPOSAL_ITEM_TERMINAL_STATUSES
    )
    assert set(PROPOSAL_ITEM_OPEN_STATUSES) | set(
        PROPOSAL_ITEM_TERMINAL_STATUSES
    ) == set(ProposalItemStatus)


def test_domain_enums_preserve_wire_values() -> None:
    assert str(ProposalStatus.OPEN) == "open"
    assert str(ProposalItemKind.WORKFLOW_RUN) == "workflow_run"
    assert str(ProposalDecisionKind.APPROVED) == "approved"
    assert str(ReviewRunStatus.SUCCEEDED) == "succeeded"
    assert str(ReviewSkipReason.OPEN_PROPOSALS) == "open_proposals"
    assert str(ConsolidationReportStatus.PARTIAL) == "partial"


def test_review_cycle_result_classifies_legacy_dict_once() -> None:
    completed = ReviewCycleResult.from_payload({"workspace_id": "ws_1"})
    skipped = ReviewCycleResult.from_payload({
        "workspace_id": "ws_1",
        "skipped": True,
        "reason": "active_work_batch",
    })
    blocked = ReviewCycleResult.from_payload({
        "workspace_id": "ws_1",
        "skipped": True,
        "needs_decision": True,
        "reason": "open_proposals",
    })

    assert completed.outcome is ReviewCycleOutcome.COMPLETED
    assert completed.skipped is False
    assert skipped.outcome is ReviewCycleOutcome.SKIPPED
    assert skipped.skip_reason is ReviewSkipReason.ACTIVE_WORK_BATCH
    assert blocked.outcome is ReviewCycleOutcome.NEEDS_DECISION
    assert blocked.skip_reason is ReviewSkipReason.OPEN_PROPOSALS
    assert blocked.skipped is True
