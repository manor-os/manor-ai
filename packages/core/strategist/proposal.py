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
