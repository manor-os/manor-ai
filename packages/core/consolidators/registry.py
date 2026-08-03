"""Consolidator registry + orchestration (M3).

``run_all(db, review)`` executes every registered domain consolidator
against the review's frozen snapshot and persists exactly one
``consolidation_reports`` row per domain. Guarantees:

* **Preload once** — workspace row + ledger window are loaded a single
  time and handed to all consolidators via ``SnapshotContext``.
* **Cache** — ``input_hash = sha256(domain:analyzer_version:
  watermark_start:watermark_end:workspace_revision:policy_revision)``.
  When the latest *succeeded* review already holds a non-failed report
  for the domain with the same hash, that report is copied
  (``coverage.reused = true``) and the consolidator never runs.
* **Failure isolation** — a consolidator exception becomes a
  ``status='failed'`` report row (summary = truncated message, coverage
  zeros) and the loop continues; the review itself never dies here.
* **Read-only discipline** — consolidators only read; ALL persistence
  happens in this module *after* each ``run`` returns (tests attach a
  ``before_flush`` listener during ``run`` to enforce this).
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.artifact_knowledge import ArtifactKnowledgeConsolidator
from packages.core.consolidators.automation_portfolio import AutomationPortfolioConsolidator
from packages.core.consolidators.base import Consolidator, SnapshotContext
from packages.core.consolidators.capacity_cost import CapacityCostConsolidator
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
)
from packages.core.consolidators.execution import ExecutionConsolidator
from packages.core.consolidators.goal import GoalConsolidator
from packages.core.consolidators.human_participation import HumanParticipationConsolidator
from packages.core.consolidators.l1 import l1_enabled
from packages.core.consolidators.learning_evidence import LearningEvidenceConsolidator
from packages.core.consolidators.risk_governance import RiskGovernanceConsolidator
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.review_run import ReviewRun
from packages.core.models.workspace import Workspace
from packages.core.review import events_in_window, latest_succeeded_review

logger = logging.getLogger(__name__)

MAX_FAILURE_SUMMARY_CHARS = 500

REGISTRY: dict[str, Consolidator] = {
    consolidator.domain: consolidator
    for consolidator in (
        GoalConsolidator(),
        ExecutionConsolidator(),
        AutomationPortfolioConsolidator(),
        ArtifactKnowledgeConsolidator(),
        HumanParticipationConsolidator(),
        CapacityCostConsolidator(),
        RiskGovernanceConsolidator(),
        LearningEvidenceConsolidator(),
    )
}


def compute_input_hash(domain: str, analyzer_version: str, review: ReviewRun) -> str:
    """Deterministic identity of a consolidator run over a frozen snapshot.

    The L1 flag is part of the identity: a report produced with the LLM
    layer off is NOT interchangeable with one produced with it on, so
    flipping ``MANOR_CONSOLIDATOR_L1`` invalidates the cache instead of
    silently reusing observations from the other mode.
    """
    material = (
        f"{domain}:{analyzer_version}:{review.watermark_start}:"
        f"{review.watermark_end}:{review.workspace_revision}:{review.policy_revision}:"
        f"l1={int(l1_enabled())}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _reusable_report(
    db: AsyncSession, review: ReviewRun, domain: str, input_hash: str,
) -> ConsolidationReport | None:
    """The latest succeeded review's report for this domain, if the inputs
    are identical (same hash) and it did not fail."""
    previous = await latest_succeeded_review(db, review.workspace_id)
    if previous is None:
        return None
    report = (await db.execute(
        select(ConsolidationReport).where(
            ConsolidationReport.review_id == previous.id,
            ConsolidationReport.domain == domain,
            ConsolidationReport.input_hash == input_hash,
        )
    )).scalar_one_or_none()
    if report is None or report.status == "failed":
        # Never propagate a failed report through the cache.
        return None
    return report


def _failed_report(
    domain: str, analyzer_version: str, input_hash: str, exc: Exception,
) -> ConsolidationReportModel:
    return ConsolidationReportModel(
        domain=domain,
        status="failed",
        summary=str(exc)[:MAX_FAILURE_SUMMARY_CHARS] or exc.__class__.__name__,
        coverage=Coverage(records_examined=0, records_missing_details=0),
        analyzer_version=analyzer_version,
        input_hash=input_hash,
    )


def _to_row(review: ReviewRun, model: ConsolidationReportModel) -> ConsolidationReport:
    return ConsolidationReport(
        entity_id=review.entity_id,
        workspace_id=review.workspace_id,
        review_id=review.id,
        domain=model.domain,
        scope={},
        status=model.status,
        summary=model.summary,
        metrics=model.metrics,
        observations=[obs.model_dump() for obs in model.observations],
        relationships=model.relationships,
        uncertainties=[u.model_dump() for u in model.uncertainties],
        evidence_refs=model.evidence_refs,
        coverage=model.coverage.model_dump(),
        analyzer_version=model.analyzer_version,
        input_hash=model.input_hash,
    )


def _copy_row(review: ReviewRun, cached: ConsolidationReport) -> ConsolidationReport:
    coverage = dict(cached.coverage or {})
    coverage["reused"] = True
    return ConsolidationReport(
        entity_id=review.entity_id,
        workspace_id=review.workspace_id,
        review_id=review.id,
        domain=cached.domain,
        scope=dict(cached.scope or {}),
        status=cached.status,
        summary=cached.summary,
        metrics=cached.metrics,
        observations=cached.observations,
        relationships=cached.relationships,
        uncertainties=cached.uncertainties,
        evidence_refs=cached.evidence_refs,
        coverage=coverage,
        analyzer_version=cached.analyzer_version,
        input_hash=cached.input_hash,
    )


async def run_all(db: AsyncSession, review: ReviewRun) -> list[ConsolidationReport]:
    """Run every registered consolidator for ``review``; return the persisted
    ``consolidation_reports`` rows (one per domain, in registry order)."""
    workspace = await db.get(Workspace, review.workspace_id)
    events = await events_in_window(db, review)
    ctx = SnapshotContext(review=review, workspace=workspace, events=events)

    rows: list[ConsolidationReport] = []
    for domain, consolidator in REGISTRY.items():
        input_hash = compute_input_hash(domain, consolidator.analyzer_version, review)

        cached = await _reusable_report(db, review, domain, input_hash)
        if cached is not None:
            row = _copy_row(review, cached)
        else:
            try:
                model = await consolidator.run(db, ctx)
                model.input_hash = input_hash
            except Exception as exc:  # noqa: BLE001 — failure isolation by design
                logger.warning(
                    "consolidator %s failed for review %s", domain, review.id,
                    exc_info=True,
                )
                model = _failed_report(
                    domain, consolidator.analyzer_version, input_hash, exc,
                )
            row = _to_row(review, model)

        # Persist AFTER the consolidator returned — run() itself is read-only.
        db.add(row)
        rows.append(row)

    await db.flush()
    return rows
