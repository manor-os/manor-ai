"""Domain consolidators (M3 contract/registry + M4 L0 consolidators).

Public surface:

* ``ConsolidationReportModel`` / ``Observation`` / ``Uncertainty`` /
  ``Coverage`` — the validated report contract (observation-only:
  ``FORBIDDEN_KEYS`` + ``FORBIDDEN_HUMAN_METRICS`` blacklists).
* ``SnapshotContext`` / ``Consolidator`` — the read-only run protocol.
* ``REGISTRY`` — domain → consolidator instance (8 L0 domains, v1).
* ``run_all`` — execute all domains for a review, persist one
  ``consolidation_reports`` row each (cache + failure isolation).
* ``l1_enabled`` / ``L1_ENABLED_ENV`` — the opt-in L1 (LLM
  summarization) gate; OFF by default, see ``l1.py``.
"""
from packages.core.consolidators.base import Consolidator, SnapshotContext
from packages.core.consolidators.contract import (
    FORBIDDEN_HUMAN_METRICS,
    FORBIDDEN_KEYS,
    ConsolidationReportModel,
    Coverage,
    Observation,
    Uncertainty,
)
from packages.core.consolidators.l1 import (
    L1_ENABLED_ENV,
    l1_enabled,
    summarize_edit_patterns,
    summarize_failure_clusters,
)
from packages.core.consolidators.registry import (
    REGISTRY,
    compute_input_hash,
    run_all,
)

__all__ = [
    "L1_ENABLED_ENV",
    "l1_enabled",
    "summarize_edit_patterns",
    "summarize_failure_clusters",
    "Consolidator",
    "SnapshotContext",
    "ConsolidationReportModel",
    "Observation",
    "Uncertainty",
    "Coverage",
    "FORBIDDEN_KEYS",
    "FORBIDDEN_HUMAN_METRICS",
    "REGISTRY",
    "compute_input_hash",
    "run_all",
]
