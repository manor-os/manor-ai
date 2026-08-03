"""LearningEvidenceConsolidator — M4.8 (`domain=learning_evidence`).

L0 digest of the learning loop: outcome-label distribution over labeled
tasks, rejection-reason distribution from window rejection events (excluding
system-generated codes that are not feedback, e.g. SUPERSEDED), and a
minimal numbers-only calibration block (mean actual/predicted ratio +
sample size).

The advisory narration that ``strategist.context._calibration_stats``
produced ("be bolder / more conservative") deliberately does NOT move
here — per M4.8 that interpretation belongs to the Strategist prompt
(M6). This consolidator emits numbers only; ``_calibration_stats`` also
takes no window/cutoff and bundles an approval-rate narrative, so the
minimal ratio is computed directly instead of importing it.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.base import SnapshotContext, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
)
from sqlalchemy import or_

from packages.core.ledger import event_types as et
from packages.core.models.experiment import Experiment
from packages.core.models.task import Task
from packages.core.proposals.constants import LEARNING_EXCLUDED_REASON_CODES

CALIBRATION_BASELINE_DAYS = 90
CALIBRATION_SAMPLE_LIMIT = 80
EXPERIMENT_DIGEST_LIMIT = 20

_EXPERIMENT_EVENT_TYPES = (
    et.EXPERIMENT_STARTED,
    et.EXPERIMENT_GUARDRAIL_TRIGGERED,
    et.EXPERIMENT_COMPLETED,
    et.EXPERIMENT_EVALUATED,
)


def _experiment_verdict_summary(experiment: Experiment) -> dict | None:
    """Numbers-only digest of an experiment's evaluation (no advice)."""
    evaluation = (
        experiment.evaluation if isinstance(experiment.evaluation, dict) else None
    )
    if not evaluation:
        return None
    metrics = evaluation.get("metrics") or {}
    met = sum(
        1 for verdict in metrics.values()
        if isinstance(verdict, dict) and verdict.get("met") is True
    )
    evaluable = sum(
        1 for verdict in metrics.values()
        if isinstance(verdict, dict) and verdict.get("met") is not None
    )
    cohort = evaluation.get("cohort") or {}
    return {
        "metrics_met": met,
        "metrics_evaluable": evaluable,
        "metrics_declared": len(metrics),
        "guardrail_violations": bool(evaluation.get("guardrail_violations")),
        "run_count": cohort.get("run_count"),
        "success_rate": cohort.get("success_rate"),
        "cost": evaluation.get("cost"),
    }


class LearningEvidenceConsolidator:
    domain = "learning_evidence"
    analyzer_version = "learning_evidence-consolidator-v1"
    critical = False

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        evaluation_events = ctx.events_of(et.EVALUATION_RECORDED)
        rejection_events = ctx.events_of(et.PROPOSAL_ITEM_REJECTED)

        # Not every rejection is feedback about the proposal. A cohort
        # dropped because a fresher review superseded it says nothing about
        # its content, so it is excluded from the distribution the
        # Strategist learns from (see LEARNING_EXCLUDED_REASON_CODES).
        rejection_reasons: dict[str, int] = {}
        counted_rejections = 0
        for event in rejection_events:
            payload = event.payload or {}
            if payload.get("rejection_reason_code") in LEARNING_EXCLUDED_REASON_CODES:
                continue
            reason = payload.get("rejection_reason") or "unspecified"
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            counted_rejections += 1

        # Labeled outcomes — 90d calibration baseline (numbers only).
        cutoff = ctx.now - timedelta(days=CALIBRATION_BASELINE_DAYS)
        labeled_tasks = list((await db.execute(
            select(Task).where(
                Task.workspace_id == ctx.review.workspace_id,
                Task.details["outcome_label"].astext.isnot(None),
                Task.created_at >= cutoff,
            ).order_by(Task.created_at.desc()).limit(CALIBRATION_SAMPLE_LIMIT)
        )).scalars().all())

        label_distribution: dict[str, int] = {}
        ratios: list[float] = []
        for task in labeled_tasks:
            details = task.details or {}
            label = details.get("outcome_label")
            if label in ("untracked", "goal_missing"):
                continue
            label_distribution[label] = label_distribution.get(label, 0) + 1
            predicted = (details.get("estimated_impact") or {}).get("metric_delta")
            actual = details.get("outcome_actual_delta")
            if predicted and actual is not None:
                try:
                    ratios.append(float(actual) / float(predicted))
                except (ZeroDivisionError, TypeError, ValueError):
                    pass

        # M13: experiment digest — running/evaluated rows plus any experiment
        # touched by a window experiment_* event. Numbers only, no advice;
        # the Strategist decides what (if anything) to promote.
        experiment_events = ctx.events_of(*_EXPERIMENT_EVENT_TYPES)
        window_experiment_ids = {
            event.source_id for event in experiment_events if event.source_id
        }
        experiment_conditions = [Experiment.status.in_(("running", "evaluated"))]
        if window_experiment_ids:
            experiment_conditions.append(Experiment.id.in_(window_experiment_ids))
        experiment_rows = list((await db.execute(
            select(Experiment).where(
                Experiment.workspace_id == ctx.review.workspace_id,
                or_(*experiment_conditions),
            ).order_by(Experiment.id.desc()).limit(EXPERIMENT_DIGEST_LIMIT)
        )).scalars().all())
        experiment_digest = [
            {
                "id": row.id,
                "status": row.status,
                "verdict_summary": _experiment_verdict_summary(row),
            }
            for row in experiment_rows
        ]

        sample_size = sum(label_distribution.values())
        metrics = {
            "outcome_label_distribution": label_distribution,
            "rejection_reason_distribution": rejection_reasons,
            "evaluations_recorded": len(evaluation_events),
            "experiments": experiment_digest,
            "calibration": {
                "sample_size": sample_size,
                "mean_actual_vs_predicted": (
                    sum(ratios) / len(ratios) if ratios else None
                ),
                "ratio_samples": len(ratios),
            },
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(evaluation_events)} evaluation(s), {counted_rejections} "
                f"rejection(s) this window; {sample_size} labeled outcome(s) in "
                f"{CALIBRATION_BASELINE_DAYS}d baseline"
            ),
            metrics=metrics,
            evidence_refs=(
                evidence_ids(evaluation_events + rejection_events + experiment_events)
                + [f"task:{task.id}" for task in labeled_tasks]
                + [f"experiment:{row.id}" for row in experiment_rows]
            ),
            coverage=Coverage(
                records_examined=(
                    len(evaluation_events) + len(rejection_events)
                    + len(labeled_tasks) + len(experiment_rows)
                ),
                sources={
                    "evaluation_events": len(evaluation_events),
                    "rejection_events": len(rejection_events),
                    "labeled_tasks_90d": len(labeled_tasks),
                    "experiment_events": len(experiment_events),
                    "experiments": len(experiment_rows),
                },
            ),
            analyzer_version=self.analyzer_version,
        )
