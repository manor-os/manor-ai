"""ConsolidationReport Pydantic contract (M3).

The contract is the "observation-only" firewall between consolidators
(which summarize facts) and the Strategist (which decides). Two
blacklists are enforced at model-validation time, before anything is
persisted:

* ``FORBIDDEN_KEYS`` — advisory/decision vocabulary. A consolidator that
  smuggles a ``recommendation``/``suggested_action``/… key anywhere into
  ``metrics``/``observations``/``relationships`` fails validation.
  裁定 1: Consolidator 不建议不执行.
* ``FORBIDDEN_HUMAN_METRICS`` — per-participant performance vocabulary,
  enforced only for ``domain == "human_participation"`` (M4.5 隐私红线):
  human data never becomes a hidden scorecard.

Both checks are recursive over nested dicts/lists and case-insensitive.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HUMAN_PARTICIPATION_DOMAIN = "human_participation"

FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "recommendation",
    "suggested_action",
    "priority",
    "proposed_patch",
    "should_create_task",
    "should_pause_automation",
    "next_step",
    "advice",
})

FORBIDDEN_HUMAN_METRICS: frozenset[str] = frozenset({
    "response_time",
    "efficiency",
    "performance_score",
    "productivity",
    "speed_rank",
})


def _walk_keys(value: Any, forbidden: frozenset[str], path: str) -> None:
    """Recursively reject any dict key in ``forbidden`` (case-insensitive)."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in forbidden:
                raise ValueError(
                    f"forbidden key {key!r} at {path} — consolidation reports "
                    f"carry observations, not advice"
                )
            _walk_keys(child, forbidden, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _walk_keys(child, forbidden, f"{path}[{index}]")


class Observation(BaseModel):
    """One factual observation, always backed by evidence refs."""

    model_config = ConfigDict(extra="forbid")

    type: str
    description: str
    evidence_refs: list[str] = Field(default_factory=list)
    baseline: bool = False


class Uncertainty(BaseModel):
    """A named blind spot the consolidator could not resolve."""

    model_config = ConfigDict(extra="forbid")

    code: str
    description: str


class Coverage(BaseModel):
    """How much of the domain's data this report actually examined."""

    model_config = ConfigDict(extra="forbid")

    records_examined: int
    records_missing_details: int = 0
    sources: dict[str, Any] = Field(default_factory=dict)
    reused: bool = False


class ConsolidationReportModel(BaseModel):
    """The validated in-memory report a consolidator returns (M3)."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    status: Literal["complete", "partial", "failed"]
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    relationships: list[dict] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    coverage: Coverage
    analyzer_version: str
    # Filled by registry.run_all (sha256 of domain/version/watermarks/revisions);
    # consolidators may leave it empty.
    input_hash: str = ""

    @model_validator(mode="after")
    def validate_no_forbidden_keys(self) -> "ConsolidationReportModel":
        payloads = {
            "metrics": self.metrics,
            "observations": [obs.model_dump() for obs in self.observations],
            "relationships": self.relationships,
        }
        for name, payload in payloads.items():
            _walk_keys(payload, FORBIDDEN_KEYS, name)
        if self.domain == HUMAN_PARTICIPATION_DOMAIN:
            # M4.5 privacy red line: no per-participant performance metrics.
            _walk_keys(self.metrics, FORBIDDEN_HUMAN_METRICS, "metrics")
        return self
