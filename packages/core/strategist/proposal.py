"""Pydantic schema for Strategist output.

The Strategist LLM call must return a JSON ``Proposal`` matching this
schema. Validation happens once before any Task rows are written —
malformed output triggers a single repair retry, then fails the run.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.core.ai.runtime.task_requirements import (
    STRATEGIST_TASK_CAPABILITY_IDS,
    normalize_task_runtime_capability_ids,
)
from packages.core.contracts.shapes import shape_names


class Deliverable(BaseModel):
    """One concrete output a task must produce, with its contract shape.

    Each proposed task declares at least one deliverable so the Planner /
    worker knows exactly what artifact or value is expected and how it will
    be used downstream.
    """

    name: str = Field(..., min_length=1, max_length=80)
    """Short identifier for the deliverable (e.g. ``drafts``)."""

    kind: Literal["value", "file"]
    """Whether the deliverable is an in-band value or a produced file."""

    shape: str
    """Canonical contract shape name — must be one of ``shape_names()``."""

    acceptance: str = Field(..., min_length=1, max_length=400)
    """How to tell the deliverable is acceptable / complete."""

    usage: str = Field(..., min_length=1, max_length=400)
    """How this deliverable is consumed downstream."""

    @field_validator("shape")
    @classmethod
    def _validate_shape(cls, value: str) -> str:
        if value not in shape_names():
            raise ValueError(
                f"unknown shape {value!r}; must be one of {shape_names()}"
            )
        return value

    @field_validator("acceptance", "usage")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank or whitespace")
        return value


def _normalize_task_key(value: str | None) -> str | None:
    if value is None:
        return None
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value).strip().lower())
    base = re.sub(r"_+", "_", base).strip("_")
    return base[:80] if base else None


class TaskBasis(BaseModel):
    """Evidence citations backing a proposed task (M7 ``basis``).

    Optional in v1: the v2 briefing prompt asks the Strategist to cite
    the briefing reports (``report_refs`` — domain names or report ids)
    and the ``[evidence: ...]`` ids (``evidence_refs``) each task rests
    on. The validator treats basis as validate-if-present — unknown
    report refs are stripped and logged, never rejected. Strict
    basis-required enforcement is deferred past v1.
    """

    report_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("report_refs", "evidence_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return [str(item) for item in v]


class EstimatedImpact(BaseModel):
    """How much this task is expected to move which goal."""

    goal_id: Optional[str] = None
    """The Goal.id the task is meant to move. Null when the task isn't
    directly tied to a tracked goal (e.g. a maintenance task)."""

    metric_delta: Optional[float] = None
    """Approximate change in goal.metric_key the Strategist expects."""

    rationale: Optional[str] = Field(default=None, max_length=400)


class ProposedTask(BaseModel):
    """One task suggestion produced by the Strategist."""

    task_key: Optional[str] = Field(default=None, max_length=80)
    """Stable key within this proposal, used by dependent tasks to refer
    to predecessor outputs before database ids exist."""

    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)

    owner_service_key: str = Field(..., min_length=1)
    """Which workspace service is primarily responsible. Must match
    a subscription's service_key in the target workspace — enforced
    by service.run_review's allowlist check."""

    delegate_service_keys: list[str] = Field(default_factory=list)
    """Other services the Planner is allowed to use for steps."""

    depends_on_task_keys: list[str] = Field(default_factory=list)
    """Other task_key values in this proposal that must complete before
    this task starts. Use when this task needs a predecessor deliverable."""

    priority: int = Field(default=3, ge=1, le=5)

    estimated_impact: Optional[EstimatedImpact] = None
    rationale: Optional[str] = Field(default=None, max_length=600)
    """Why the Strategist proposed this; shown to the user in chat."""

    expected_output: Optional[dict] = None
    """Optional JSON Schema the Planner / Task should aim to produce."""

    deliverables: list[Deliverable] = Field(..., min_length=1)
    """Concrete outputs this task must produce. At least one is required
    (the field is mandatory — omitting it is a validation error, not an empty
    default); each carries a validated contract shape so downstream steps know
    what artifact or value to expect."""

    required_capabilities: list[str] = Field(default_factory=list)
    """Runtime BusinessCapability ids this task expects the worker to have.

    These are capability ids (for example ``workspace.search``), not tool
    names. Tool expansion is handled later by the Manor Runtime Harness.
    """

    basis: Optional[TaskBasis] = None
    """Evidence citations (v2 briefing path). Validate-if-present in v1."""

    correlation_key: Optional[str] = Field(default=None, max_length=96)
    """Optional stable dedupe key for recurring work across review cycles
    (M7 ``correlation_key``); carried onto the proposal item verbatim."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_capability_aliases(cls, data):
        if not isinstance(data, dict) or "required_capabilities" in data:
            return data
        for key in ("business_capabilities", "runtime_capabilities", "capability_ids"):
            if key in data:
                out = dict(data)
                out["required_capabilities"] = data.get(key)
                return out
        return data

    @field_validator("task_key", mode="before")
    @classmethod
    def _normalize_key(cls, v):
        return _normalize_task_key(v)

    @field_validator("delegate_service_keys", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)

    @field_validator("depends_on_task_keys", mode="before")
    @classmethod
    def _coerce_dependency_keys(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            raw = [s.strip() for s in v.split(",") if s.strip()]
        else:
            raw = list(v)
        return [key for key in (_normalize_task_key(item) for item in raw) if key]

    @field_validator("required_capabilities", mode="before")
    @classmethod
    def _coerce_required_capabilities(cls, v):
        return list(normalize_task_runtime_capability_ids(
            v,
            allowed_ids=STRATEGIST_TASK_CAPABILITY_IDS,
            strict=True,
        ))


class ProposedHumanRequest(BaseModel):
    """One request for HUMAN decision/review/input/manual work (M10).

    Human requests are not work tasks: they exist because the workspace
    needs a human's judgment or hands (direction confirmations, blocked
    approvals, offline actions). Per M8 they never mint an
    HitlRequest — the item auto-creates a ``HumanCommitment`` and the
    addressee may decline instead.
    """

    request_key: str = Field(..., min_length=1, max_length=80)
    """Stable snake_case key, unique within this proposal."""

    request_kind: Literal["decision", "review", "input", "manual_work"]

    role_required: Optional[str] = Field(default=None, max_length=40)
    """Workspace role expected to answer (participant resolution is
    deferred in v1 — requests queue by role, never by named person)."""

    question: str = Field(..., min_length=8, max_length=1000)
    """The concrete question / request the human must act on."""

    expected_by_hours: Optional[int] = Field(default=None, ge=1, le=24 * 30)
    """Soft deadline in hours from proposal time; None = no deadline."""

    context: Optional[str] = Field(default=None, max_length=2000)
    """Short context the human needs to answer well."""

    @field_validator("request_key", mode="before")
    @classmethod
    def _normalize_request_key(cls, v):
        return _normalize_task_key(v)


class ExperimentMetricSpec(BaseModel):
    """One pre-declared success metric for an experiment (M13).

    Only metrics declared here are ever evaluated — the deterministic
    evaluator refuses to invent post-hoc success criteria."""

    baseline: Optional[float] = None
    """Believed current value; the controller freezes the authoritative
    baseline from the ledger at start, this is the LLM's prior."""

    target: float
    """Value the experiment must reach for this metric to count as met."""


class ExperimentGuardrails(BaseModel):
    """Blast-radius bounds for an experiment (M13)."""

    max_cost: float = Field(default=20, gt=0, le=10_000)
    """Cost ceiling in USD. ≤ $20 keeps the item at medium risk; anything
    above is classified high risk by the persistence layer (M8 catalog)."""

    rollback_on_consecutive_failures: int = Field(default=2, ge=1, le=10)
    """Consecutive cohort run failures that trigger a guardrail stop."""


class ProposedExperiment(BaseModel):
    """One bounded config experiment proposed by the Strategist (M13).

    v1 targets: ``scheduled_job`` | ``workflow_binding``. A one-off task is
    never an experiment target (it is naturally bounded); prompt-level
    strategy experiments go through the AgentLearningCandidate channel.
    The overlay patch is applied WITHOUT a revision bump and removed on any
    stop path; promotion always requires a separate automation_change
    proposal — an experiment never silently becomes permanent.
    """

    experiment_key: str = Field(..., min_length=1, max_length=80)
    """Stable snake_case key, unique within this proposal."""

    hypothesis: str = Field(..., min_length=16, max_length=2000)
    """What we believe the config change improves, and why (cites basis)."""

    target_kind: Literal["scheduled_job", "workflow_binding"]
    target_id: str = Field(..., min_length=1, max_length=64)

    overlay_patch: dict = Field(...)
    """Shallow config patch merged over the target's effective config per
    run while the experiment is running (e.g. payload_message/params)."""

    max_runs: int = Field(..., ge=1, le=20)
    duration_days: int = Field(default=7, ge=1, le=30)

    success_metrics: dict[str, ExperimentMetricSpec] = Field(..., min_length=1)
    """Pre-declared metrics ({name: {baseline, target}}); v1 evaluator
    vocabulary is success_rate / run_count."""

    guardrails: ExperimentGuardrails = Field(default_factory=ExperimentGuardrails)

    @field_validator("experiment_key", mode="before")
    @classmethod
    def _normalize_experiment_key(cls, v):
        return _normalize_task_key(v)

    @field_validator("overlay_patch")
    @classmethod
    def _require_patch(cls, v):
        if not isinstance(v, dict) or not v:
            raise ValueError("overlay_patch must be a non-empty object")
        return v


class _ProposedChangeBase(BaseModel):
    """Shared shape of the three configuration-change proposals (M7).

    A change is the FORMAL counterpart of an experiment: it edits the
    canonical row (裁定 B) and bumps its ``revision``, so it must carry the
    ``expected_revision`` it was decided against — the M7 validator
    prechecks it and the M10 executor re-checks it under a row lock.
    Subclasses declare their own ``target_kind`` / ``operation`` literals;
    the coherence + patch-whitelist rules live here.
    """

    change_key: str = Field(..., min_length=1, max_length=80)
    """Stable snake_case key, unique within its list in this proposal."""

    target_id: Optional[str] = Field(default=None, max_length=64)
    """Row being changed. Required for every operation except ``create``."""

    expected_revision: Optional[int] = Field(default=None, ge=1)
    """The target row's ``revision`` as shown in the briefing. Required for
    every operation except ``create``; forbidden ON ``create``."""

    patch: dict = Field(default_factory=dict)
    """Field-level patch; keys are whitelisted per target_kind."""

    rationale: str = Field(..., min_length=16, max_length=1000)
    """Why the evidence already settles this — not a hypothesis."""

    basis: Optional[TaskBasis] = None
    """Evidence citations (same contract as ProposedTask.basis)."""

    @field_validator("change_key", mode="before")
    @classmethod
    def _normalize_change_key(cls, v):
        return _normalize_task_key(v)

    @model_validator(mode="after")
    def _validate_change_coherence(self):
        from packages.core.proposals.constants import (
            PATCH_REQUIRED_OPERATIONS,
            REVISION_REQUIRED_OPERATIONS,
            change_patch_whitelist,
        )

        operation = self.operation
        target_kind = self.target_kind
        if operation == "create":
            if self.target_id:
                raise ValueError("operation 'create' must not carry a target_id")
            if self.expected_revision is not None:
                raise ValueError(
                    "operation 'create' must not carry an expected_revision "
                    "(there is no row to compare against yet)"
                )
        else:
            if not self.target_id:
                raise ValueError(f"operation {operation!r} requires a target_id")
            if (
                operation in REVISION_REQUIRED_OPERATIONS
                and self.expected_revision is None
            ):
                raise ValueError(
                    f"operation {operation!r} requires expected_revision "
                    f"(copy the target's current revision from the briefing)"
                )

        if operation in PATCH_REQUIRED_OPERATIONS and not self.patch:
            raise ValueError(f"operation {operation!r} requires a non-empty patch")

        allowed = change_patch_whitelist(target_kind, operation)
        unknown = sorted(set(self.patch or {}) - allowed)
        if unknown:
            raise ValueError(
                f"patch keys {unknown} are not changeable on {target_kind} "
                f"({operation}); allowed keys: {sorted(allowed)}"
            )

        if operation == "update_target" and "target_value" not in self.patch:
            raise ValueError("operation 'update_target' requires patch.target_value")
        if operation == "update_deadline" and "deadline" not in self.patch:
            raise ValueError("operation 'update_deadline' requires patch.deadline")
        return self


class ProposedAutomationChange(_ProposedChangeBase):
    """One formal change to an automation row (ScheduledJob / WorkflowBinding)."""

    target_kind: Literal["scheduled_job", "workflow_binding"] = "scheduled_job"
    operation: Literal["create", "update", "pause", "resume", "delete"]


class ProposedWorkflowChange(_ProposedChangeBase):
    """One formal change to a workflow template or its deployment."""

    target_kind: Literal["workflow_definition", "workflow_binding"]
    operation: Literal["create", "update", "pause", "resume", "delete"]


class ProposedGoalChange(_ProposedChangeBase):
    """One formal change to a tracked Goal."""

    target_kind: Literal["goal"] = "goal"
    operation: Literal[
        "create", "update_target", "update_deadline", "pause", "archive",
    ]


class Proposal(BaseModel):
    """Full Strategist output for one review cycle."""

    review_id: str = Field(..., min_length=1, max_length=64)
    """Stable id for this review cycle. Persisted on each Task's
    ``details.strategist_review_id`` so we can group / re-render the
    cohort, and also used to dedupe re-runs in the same cycle."""

    summary: str = Field(..., min_length=1, max_length=1000)
    """One-paragraph framing the operator sees in chat above the
    task list ("This week the focus is X because Y…")."""

    tasks: list[ProposedTask] = Field(..., min_length=0, max_length=8)
    """0 tasks is legal — Strategist says "nothing new this cycle".
    Cap at 8 to keep the operator from drowning in proposals."""

    notes: Optional[str] = Field(default=None, max_length=1500)
    """Free-form observations — not actioned, surfaced in chat for
    the operator's awareness ("noticed engagement dropping on Tue")."""

    human_requests: list[ProposedHumanRequest] = Field(
        default_factory=list, max_length=3,
    )
    """Requests for human decision/review/input/manual work (M10).
    Only persisted on the v2 (briefing) path; the legacy path ignores
    the field entirely. Capped at 3 per M7."""

    experiments: list[ProposedExperiment] = Field(
        default_factory=list, max_length=1,
    )
    """Bounded config experiments (M13). Only persisted on the v2
    (briefing) path; capped at 1 per review cycle per M7."""

    automation_changes: list[ProposedAutomationChange] = Field(
        default_factory=list, max_length=3,
    )
    """Formal changes to ScheduledJob / WorkflowBinding rows (M7/M10).
    v2 (briefing) path only; capped at 3 per review cycle per M6."""

    workflow_changes: list[ProposedWorkflowChange] = Field(
        default_factory=list, max_length=2,
    )
    """Formal changes to WorkflowDefinition / WorkflowBinding rows.
    v2 (briefing) path only; capped at 2 per review cycle per M6."""

    goal_changes: list[ProposedGoalChange] = Field(
        default_factory=list, max_length=2,
    )
    """Formal changes to Goal rows. v2 (briefing) path only; capped at 2
    per review cycle per M6."""

    @model_validator(mode="after")
    def _validate_change_keys(self):
        for field_name in ("automation_changes", "workflow_changes", "goal_changes"):
            keys = [c.change_key for c in getattr(self, field_name) if c.change_key]
            duplicates = {key for key in keys if keys.count(key) > 1}
            if duplicates:
                raise ValueError(
                    f"duplicate {field_name} change_key values: {sorted(duplicates)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_human_request_keys(self):
        keys = [hr.request_key for hr in self.human_requests if hr.request_key]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(
                f"duplicate human_request request_key values: {sorted(duplicates)}"
            )
        return self

    @model_validator(mode="after")
    def _validate_task_dependency_keys(self):
        keys = [task.task_key for task in self.tasks if task.task_key]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate task_key values: {sorted(duplicates)}")
        known = set(keys)
        for task in self.tasks:
            for dep_key in task.depends_on_task_keys:
                if task.task_key and dep_key == task.task_key:
                    raise ValueError(f"task {task.task_key!r} cannot depend on itself")
                if dep_key not in known:
                    raise ValueError(f"depends_on_task_keys references unknown task_key {dep_key!r}")
        graph = {
            task.task_key: list(task.depends_on_task_keys)
            for task in self.tasks
            if task.task_key
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise ValueError(f"task dependency cycle includes {key!r}")
            visiting.add(key)
            for dep_key in graph.get(key, []):
                visit(dep_key)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        return self
