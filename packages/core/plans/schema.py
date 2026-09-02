"""Pydantic models for Plan / PlanStep.

Two purposes:

  1. Validate Planner LLM output before we trust it. JSON shape is
     enforced; anything off-schema fails fast with a structured error
     the Planner can re-prompt against.
  2. Document the canonical shape of ``execution_plans.plan_dag`` JSONB.
     The DB column is freeform JSONB but everything that reads it goes
     through ``Plan.model_validate(...)`` first.

Step kinds match ``execution_steps.kind``:
  llm | action | sleep | human | subagent | parallel_fanout | gather | code

Per-kind required-field semantics are enforced via model_validators so
a Planner error like "action step without provider" is caught here,
not after a lease is dispatched.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.core.ai.runtime import capability_for_id, runtime_capability_id_for_action_key

StepKind = Literal[
    "llm", "action", "sleep", "human",
    "subagent", "parallel_fanout", "gather", "code",
]
RiskLevel = Literal["low", "medium", "high"]
PROMPT_PARAM_KEYS = ("prompt", "user_prompt", "instructions", "instruction", "message", "task")


class PlanStep(BaseModel):
    """One node in the plan DAG."""

    key: str = Field(..., min_length=1, max_length=64)
    """Unique identifier within the plan; ``${{ steps.<key>.result }}``
    references resolve against this. Convention: snake_case verb_object
    like ``draft_tweet`` / ``publish_tweet`` / ``collect_metrics``."""

    kind: StepKind

    service_key: Optional[str] = None
    """Semantic role intent. Required for kinds that need an agent
    (llm / action / subagent / code); the Planner is constrained to
    the (task.owner_service_key + task.delegate_service_keys) set."""

    # Action-only fields
    provider: Optional[str] = None
    action_key: Optional[str] = None
    capability_id: Optional[str] = None
    integration_id: Optional[str] = None

    params: dict[str, Any] = Field(default_factory=dict)
    """Step input. May contain ``${{ steps.X.result.path }}`` refs that
    the executor resolves at dispatch time against prior step results."""

    expected_input_schema: Optional[dict[str, Any]] = None
    expected_output_schema: Optional[dict[str, Any]] = None
    """JSON Schema. For action kinds, the Planner can leave these null;
    Runtime action binding hydration attaches provider schemas before
    ExecutionStep materialization when the catalog knows them. For
    llm/subagent kinds, use this for an exact custom payload when no canonical
    output_shape fits; Runtime provenance-marks and hard-validates it. A
    terminal task-authored expected_output is bound as a hard nested payload
    contract during materialization and takes precedence."""

    output_shape: Optional[str] = None
    """Canonical shape name from packages.core.contracts.shapes (e.g.
    ``ArtifactResult``). Preferred over a hand-written
    ``expected_output_schema`` for llm/subagent kinds: the plan-time linker
    derives ``expected_output_schema`` from the shape so producer, normalizer,
    and validator share one vocabulary. References to a canonical shape use
    its payload fields directly (for example ``result.files``); the
    ``result.outputs.*`` wrapper is reserved for the legacy StepResult envelope
    and the terminal Task.expected_output contract. A new llm/subagent must
    declare either this or an exact expected_output_schema, except the unique
    terminal step governed by Task.expected_output. Legacy materialized schemas
    without provenance remain readable through the advisory path."""

    depends_on: list[str] = Field(default_factory=list)
    """List of prior step keys that must reach ``done`` before this
    step can be dispatched. The Planner must produce a DAG (no cycles);
    cycle detection happens at validation time."""

    risk_level: RiskLevel = "low"
    requires_approval: bool = False
    """Hard stop before dispatch.

    Planner prompts should not use this to encode governance preferences.
    Runtime approvals, "always allow", and denies are owned by workspace
    governance policy; this field is reserved for explicit system-authored
    step gates that must pause regardless of planner intent.
    """

    max_attempts: int = Field(default=3, ge=1, le=10)

    expects: Optional[list[str]] = None
    """Deterministic completion expectations checked against CAPTURED tool
    evidence, never model claims. Vocabulary: "publish" (an external
    publish/send effect must be evidenced by a successful tool result),
    "files" (a materialized artifact must be evidenced). Unmet expects on a
    mechanically-completed plan trigger replan instead of false success."""

    description: Optional[str] = Field(default=None, max_length=500)
    """Human-readable one-liner shown in the workspace_chat step
    receipt and in the plan UI."""

    @field_validator("key")
    @classmethod
    def _key_chars(cls, v: str) -> str:
        # Keep keys safe to interpolate — alnum + underscore only.
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError(
                f"step key must be alnum + underscore, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _per_kind_required(self) -> "PlanStep":
        if self.kind == "action":
            if not self.provider:
                raise ValueError(f"step {self.key}: action kind requires provider")
            if not self.action_key:
                raise ValueError(f"step {self.key}: action kind requires action_key")
            inferred_capability_id = runtime_capability_id_for_action_key(
                self.action_key,
                provider=self.provider,
            )
            if self.capability_id:
                if capability_for_id(self.capability_id) is None:
                    raise ValueError(
                        f"step {self.key}: unknown runtime capability_id "
                        f"{self.capability_id!r}"
                    )
                if inferred_capability_id and self.capability_id != inferred_capability_id:
                    raise ValueError(
                        f"step {self.key}: capability_id={self.capability_id!r} "
                        f"does not match provider/action capability "
                        f"{inferred_capability_id!r}"
                    )
            elif inferred_capability_id:
                self.capability_id = inferred_capability_id
            if not self.service_key:
                raise ValueError(
                    f"step {self.key}: action kind requires service_key "
                    f"(which subscription will execute it)"
                )

        if self.kind == "sleep":
            # Presence check, not truthy — ``seconds=0`` is a legal
            # (degenerate) sleep used in tests + by the executor when
            # an ``until`` deadline already passed.
            if "seconds" not in self.params and "until" not in self.params:
                raise ValueError(
                    f"step {self.key}: sleep kind requires "
                    f"params.seconds or params.until"
                )

        if self.kind == "human":
            if not self.params.get("prompt"):
                raise ValueError(
                    f"step {self.key}: human kind requires params.prompt"
                )

        if self.kind in ("llm", "subagent") and not self.service_key:
            raise ValueError(
                f"step {self.key}: {self.kind} kind requires service_key"
            )
        if self.kind in ("llm", "subagent"):
            prompt = next((self.params.get(k) for k in PROMPT_PARAM_KEYS if self.params.get(k)), None)
            if not prompt:
                raise ValueError(
                    f"step {self.key}: {self.kind} kind requires params.prompt "
                    "(or instructions/instruction/user_prompt/message/task)"
                )
            # Normalize historical/planner synonyms so downstream workers
            # receive the canonical field even if the LLM used a synonym.
            self.params.setdefault("prompt", prompt)

        return self


class PlanAcceptanceCriterion(BaseModel):
    """One task-specific success condition and the steps that prove it."""

    key: str = Field(..., min_length=1, max_length=128)
    deliverable_name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1, max_length=1000)
    evidence_step_keys: list[str] = Field(..., min_length=1)


class PlanAcceptanceContract(BaseModel):
    """Task acceptance criteria snapshotted onto an executable Plan."""

    expected_result: str = Field(..., min_length=1, max_length=2000)
    criteria: list[PlanAcceptanceCriterion] = Field(..., min_length=1)
    task_expected_output: Optional[dict[str, Any]] = None


class PlanMetadata(BaseModel):
    """Free-form fields the Planner attaches for audit / display."""

    planner_model: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    estimated_duration_seconds: Optional[int] = None
    rationale: Optional[str] = None
    acceptance_contract: Optional[PlanAcceptanceContract] = None
    # Why the Planner chose this shape — shown in the plan detail UI
    # so the user understands the agent's reasoning at approve time.

    model_config = {"extra": "allow"}


class Plan(BaseModel):
    """Full plan output by the Planner."""

    steps: list[PlanStep] = Field(..., min_length=1)
    metadata: PlanMetadata = Field(default_factory=PlanMetadata)

    @model_validator(mode="after")
    def _check_dag(self) -> "Plan":
        keys = {s.key for s in self.steps}
        if len(keys) != len(self.steps):
            raise ValueError("step keys must be unique within a plan")

        # All depends_on references must resolve.
        for s in self.steps:
            for dep in s.depends_on:
                if dep not in keys:
                    raise ValueError(
                        f"step {s.key} depends_on unknown step {dep!r}"
                    )

        acceptance_contract = self.metadata.acceptance_contract
        if acceptance_contract is not None:
            unknown_evidence = sorted({
                evidence_key
                for criterion in acceptance_contract.criteria
                for evidence_key in criterion.evidence_step_keys
                if evidence_key not in keys
            })
            if unknown_evidence:
                raise ValueError(
                    "acceptance contract references unknown evidence steps: "
                    + ", ".join(unknown_evidence)
                )

        # Cycle check via DFS — small N, no need for fancier algo.
        graph = {s.key: list(s.depends_on) for s in self.steps}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in graph}

        def visit(k: str) -> None:
            color[k] = GRAY
            for dep in graph[k]:
                if color[dep] == GRAY:
                    raise ValueError(
                        f"plan DAG has a cycle involving step {dep!r}"
                    )
                if color[dep] == WHITE:
                    visit(dep)
            color[k] = BLACK

        for k in list(graph):
            if color[k] == WHITE:
                visit(k)

        return self

    def step_by_key(self, key: str) -> Optional[PlanStep]:
        return next((s for s in self.steps if s.key == key), None)

    def topo_order(self) -> list[PlanStep]:
        """Steps in dependency order — useful for materialisation."""
        ordered: list[str] = []
        visiting: set[str] = set()
        keys = {s.key: s for s in self.steps}

        def visit(k: str) -> None:
            if k in ordered:
                return
            if k in visiting:
                # _check_dag already rejected cycles.
                return
            visiting.add(k)
            for dep in keys[k].depends_on:
                visit(dep)
            visiting.discard(k)
            ordered.append(k)

        for k in list(keys):
            visit(k)

        return [keys[k] for k in ordered]
