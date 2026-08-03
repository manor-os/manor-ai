"""Per-kind payload schemas for proposal items (M7 tagged union).

Every kind in ``ITEM_KINDS`` is now produced by the v2 (briefing) review
path: ``task`` / ``human_request`` / ``experiment`` and the three
configuration-change kinds (``automation_change`` / ``workflow_change`` /
``goal_change``). The authoritative LLM-facing validation for each lives
in ``packages.core.strategist.proposal``; the models here are the
persisted ``proposal_items.payload`` contract that the M7 validator and
the M10 executor (``packages.core.proposals.change_executor``) read back.

``TaskItemPayload`` deliberately wraps the ``ProposedTask`` dump loosely
(extra fields pass through) — the authoritative task validation already
happened in ``packages.core.strategist.proposal`` before persistence;
the item payload is a bookkeeping copy plus the persisted ``task_id``.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Basis(BaseModel):
    """Evidence citations for a proposal item — validate-if-present in v1."""

    report_refs: list[str] = Field(default_factory=list)
    """Consolidation report ids or domain names from this review."""

    evidence_refs: list[str] = Field(default_factory=list)
    """Ledger/evidence ids shown as [evidence: ...] in the briefing."""


class TaskItemPayload(BaseModel):
    """kind="task" — the ProposedTask dump + the persisted Task id."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    title: Optional[str] = None
    owner_service_key: Optional[str] = None


class HumanRequestPayload(BaseModel):
    """kind="human_request" — the ProposedHumanRequest dump."""

    request_kind: Literal["decision", "review", "input", "manual_work"]
    role_required: Optional[str] = None
    participant_id: Optional[str] = None
    question: str
    options: list[str] = Field(default_factory=list)
    expected_by: Optional[str] = None
    context_refs: list[str] = Field(default_factory=list)


class _ChangePayloadBase(BaseModel):
    """Shared shape of the three configuration-change payloads.

    ``expected_revision`` is the M11 CAS token: the row revision the
    Strategist decided against, copied from the briefing. The validator
    prechecks it and the executor re-checks it under a row lock.
    """

    model_config = ConfigDict(extra="allow")

    change_key: str
    target_id: Optional[str] = None      # None on create
    expected_revision: Optional[int] = None  # None on create
    patch: dict = Field(default_factory=dict)
    rationale: Optional[str] = None


class AutomationChangePayload(_ChangePayloadBase):
    """kind="automation_change" — a formal, revision-bumping change to a
    ScheduledJob or a WorkflowBinding (contrast: an experiment overlay,
    which never bumps)."""

    target_kind: Literal["scheduled_job", "workflow_binding"]
    operation: Literal["create", "update", "pause", "resume", "delete"]


class WorkflowChangePayload(_ChangePayloadBase):
    """kind="workflow_change" — a change to a WorkflowDefinition (the
    template) or a WorkflowBinding (the deployment)."""

    target_kind: Literal["workflow_definition", "workflow_binding"]
    operation: Literal["create", "update", "pause", "resume", "delete"]


class GoalChangePayload(_ChangePayloadBase):
    """kind="goal_change" — a change to a tracked Goal."""

    target_kind: Literal["goal"] = "goal"
    operation: Literal["create", "update_target", "update_deadline", "pause", "archive"]


class ExperimentPayload(BaseModel):
    """kind="experiment" — the ProposedExperiment dump (M13)."""

    hypothesis: str
    scope: dict = Field(default_factory=dict)          # {automation_id?, max_runs, duration_days}
    success_metrics: dict = Field(default_factory=dict)  # {name: {baseline, target}}
    guardrails: dict = Field(default_factory=dict)       # {max_cost, rollback_on_consecutive_failures}
    overlay_patch: dict = Field(default_factory=dict)


PAYLOAD_MODEL_BY_KIND: dict[str, type[BaseModel]] = {
    "task": TaskItemPayload,
    "human_request": HumanRequestPayload,
    "automation_change": AutomationChangePayload,
    "workflow_change": WorkflowChangePayload,
    "goal_change": GoalChangePayload,
    "experiment": ExperimentPayload,
}
