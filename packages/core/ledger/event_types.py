"""Workspace event ledger — event type vocabulary (v1).

Every ``workspace_events.event_type`` must be one of these constants.
``ledger.service.record_event`` rejects unknown types (ValueError) so the
vocabulary stays closed: adding a new event kind means adding a constant here,
which keeps consolidators (M4) and Timeline (M14) able to enumerate what they
consume.
"""
from __future__ import annotations

# ── execution (Task / plan) ────────────────────────────────────────
EXECUTION_REQUESTED = "execution_requested"
EXECUTION_STARTED = "execution_started"
EXECUTION_COMPLETED = "execution_completed"
EXECUTION_FAILED = "execution_failed"
EXECUTION_CANCELLED = "execution_cancelled"

# ── automation (ScheduledJob) ──────────────────────────────────────
AUTOMATION_RUN_DISPATCHED = "automation_run_dispatched"
AUTOMATION_RUN_COMPLETED = "automation_run_completed"
AUTOMATION_RUN_FAILED = "automation_run_failed"
AUTOMATION_RUN_MISSED = "automation_run_missed"

# ── workflow ───────────────────────────────────────────────────────
WORKFLOW_RUN_STARTED = "workflow_run_started"
WORKFLOW_RUN_COMPLETED = "workflow_run_completed"
WORKFLOW_RUN_FAILED = "workflow_run_failed"
WORKFLOW_RUN_PAUSED = "workflow_run_paused"

# ── proposals (M7) ─────────────────────────────────────────────────
PROPOSAL_CREATED = "proposal_created"
PROPOSAL_ITEM_APPROVED = "proposal_item_approved"
PROPOSAL_ITEM_REJECTED = "proposal_item_rejected"
PROPOSAL_ITEM_EDITED = "proposal_item_edited"
PROPOSAL_EXPIRED = "proposal_expired"

# ── approvals (HitlRequest) ────────────────────────────────────
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_GRANTED = "approval_granted"
APPROVAL_DENIED = "approval_denied"
APPROVAL_CONSUMED = "approval_consumed"
APPROVAL_EXPIRED = "approval_expired"

# ── human participation (M9) ───────────────────────────────────────
HUMAN_COMMITMENT_OPENED = "human_commitment_opened"
HUMAN_COMMITMENT_FULFILLED = "human_commitment_fulfilled"
HUMAN_COMMITMENT_DECLINED = "human_commitment_declined"
HUMAN_COMMITMENT_EXPIRED = "human_commitment_expired"
HUMAN_CONTRIBUTION_RECORDED = "human_contribution_recorded"

# ── goals ──────────────────────────────────────────────────────────
GOAL_MEASURED = "goal_measured"
GOAL_PACE_CHANGED = "goal_pace_changed"
GOAL_ACHIEVED = "goal_achieved"
GOAL_CHANGED = "goal_changed"

# ── artifacts ──────────────────────────────────────────────────────
ARTIFACT_CREATED = "artifact_created"
ARTIFACT_USED = "artifact_used"

# ── evaluation / learning (M12) ────────────────────────────────────
EVALUATION_RECORDED = "evaluation_recorded"
LEARNING_CANDIDATE_CREATED = "learning_candidate_created"
LEARNING_CANDIDATE_RESOLVED = "learning_candidate_resolved"

# ── experiments (M13) ──────────────────────────────────────────────
EXPERIMENT_STARTED = "experiment_started"
EXPERIMENT_GUARDRAIL_TRIGGERED = "experiment_guardrail_triggered"
EXPERIMENT_COMPLETED = "experiment_completed"
EXPERIMENT_EVALUATED = "experiment_evaluated"

# ── config / governance ────────────────────────────────────────────
CONFIG_CHANGED = "config_changed"
POLICY_CHANGED = "policy_changed"
BUDGET_THRESHOLD_CROSSED = "budget_threshold_crossed"

# ── review lifecycle (M2) ──────────────────────────────────────────
REVIEW_STARTED = "review_started"
REVIEW_SUCCEEDED = "review_succeeded"
REVIEW_FAILED = "review_failed"
REVIEW_SKIPPED = "review_skipped"


ALL_EVENT_TYPES: frozenset[str] = frozenset(
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str) and not name.startswith("_")
)
