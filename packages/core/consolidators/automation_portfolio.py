"""AutomationPortfolioConsolidator — M4.3 (`domain=automation_portfolio`, critical).

L0 digest of the workspace's automation surface: per-enabled-job run
counts from window ``automation_run_*`` events, failure streaks from the
job row's ``consecutive_errors``, cadence misses for interval jobs, and
duplicate execution roots (same correlation_id, >1 root) across the
whole window.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.base import SnapshotContext, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding

FAILURE_STREAK_THRESHOLD = 3

_AUTOMATION_EVENTS = (
    et.AUTOMATION_RUN_DISPATCHED,
    et.AUTOMATION_RUN_COMPLETED,
    et.AUTOMATION_RUN_FAILED,
    et.AUTOMATION_RUN_MISSED,
)


def _schedule_summary(job: ScheduledJob) -> str:
    if job.cron_expr:
        return f"cron {job.cron_expr}"
    if job.every_seconds:
        return f"every {job.every_seconds:g}s"
    if job.run_at:
        return f"at {job.run_at}"
    return job.schedule_kind or job.job_type or "unknown"


class AutomationPortfolioConsolidator:
    domain = "automation_portfolio"
    analyzer_version = "automation_portfolio-consolidator-v1"
    critical = True

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        ws_id = ctx.review.workspace_id
        jobs = list((await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.enabled.is_(True),
                or_(
                    ScheduledJob.workspace_id == ws_id,
                    ScheduledJob.execution_target["workspace_id"].astext == ws_id,
                ),
            ).order_by(ScheduledJob.id.asc())
        )).scalars().all())
        bindings = list((await db.execute(
            select(WorkflowBinding).where(
                WorkflowBinding.workspace_id == ws_id,
                WorkflowBinding.enabled.is_(True),
            )
        )).scalars().all())

        run_events = ctx.events_of(*_AUTOMATION_EVENTS)
        events_by_job: dict[str, list] = {}
        for event in run_events:
            events_by_job.setdefault(event.source_id, []).append(event)

        window_seconds = ctx.window_duration_seconds()
        observations: list[Observation] = []
        digests: list[dict] = []

        for job in jobs:
            own = events_by_job.get(job.id, [])
            dispatched = sum(1 for e in own if e.event_type == et.AUTOMATION_RUN_DISPATCHED)
            completed = sum(1 for e in own if e.event_type == et.AUTOMATION_RUN_COMPLETED)
            job_failed = sum(1 for e in own if e.event_type == et.AUTOMATION_RUN_FAILED)
            digests.append({
                "job_id": job.id,
                "job_key": job.job_id,
                "name": job.name,
                "schedule": _schedule_summary(job),
                "runs_dispatched": dispatched,
                "completed": completed,
                "failed": job_failed,
                "consecutive_errors": job.consecutive_errors or 0,
                # M11 CAS token — the Strategist copies this into an
                # automation_change's expected_revision so a change decided
                # on this briefing fails fast if the row moved meanwhile.
                "revision": int(job.revision or 1),
            })

            if (job.consecutive_errors or 0) >= FAILURE_STREAK_THRESHOLD:
                observations.append(Observation(
                    type="failure_streak",
                    description=(
                        f"automation {job.name or job.job_id!r} has "
                        f"{job.consecutive_errors} consecutive errors"
                    ),
                    evidence_refs=[f"scheduled_job:{job.id}"] + evidence_ids(
                        [e for e in own if e.event_type == et.AUTOMATION_RUN_FAILED]
                    ),
                ))

            # cadence_miss — interval job whose window fits >=1 expected run
            # but saw zero dispatches (guard: window must span the interval).
            if (
                job.every_seconds
                and job.every_seconds > 0
                and window_seconds is not None
                and window_seconds >= job.every_seconds
                and dispatched == 0
            ):
                expected = int(window_seconds // job.every_seconds)
                observations.append(Observation(
                    type="cadence_miss",
                    description=(
                        f"automation {job.name or job.job_id!r} expected "
                        f"~{expected} run(s) this window but dispatched none"
                    ),
                    evidence_refs=[f"scheduled_job:{job.id}"],
                ))

        # duplicate_execution_roots — same correlation_id, >1 distinct root
        # (scans the whole window, not just automation events).
        roots_by_correlation: dict[str, set[str]] = {}
        events_by_correlation: dict[str, list] = {}
        for event in ctx.events:
            if event.correlation_id and event.root_execution_id:
                roots_by_correlation.setdefault(event.correlation_id, set()).add(
                    event.root_execution_id
                )
                events_by_correlation.setdefault(event.correlation_id, []).append(event)
        duplicate_roots = {
            correlation: sorted(roots)
            for correlation, roots in roots_by_correlation.items()
            if len(roots) > 1
        }
        for correlation, roots in sorted(duplicate_roots.items()):
            observations.append(Observation(
                type="duplicate_work_detected",
                description=(
                    f"correlation {correlation!r} produced {len(roots)} distinct "
                    f"execution roots this window"
                ),
                evidence_refs=evidence_ids(events_by_correlation[correlation]),
            ))

        metrics = {
            "automations_enabled": len(jobs),
            "workflow_bindings_enabled": len(bindings),
            "automations": digests,
            # Same CAS purpose as the per-job "revision" above, for
            # automation_change / workflow_change items that target a binding.
            "workflow_bindings": [
                {
                    "binding_id": binding.id,
                    "name": binding.name,
                    "workflow_id": binding.workflow_id,
                    "trigger_type": binding.trigger_type,
                    "status": binding.status,
                    "revision": int(binding.revision or 1),
                }
                for binding in bindings
            ],
            "runs_dispatched_total": sum(d["runs_dispatched"] for d in digests),
            "runs_failed_total": sum(d["failed"] for d in digests),
            "duplicate_execution_roots": duplicate_roots,
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(jobs)} enabled automation(s), {len(bindings)} workflow "
                f"binding(s); {len(run_events)} run event(s) in window"
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(run_events),
            coverage=Coverage(
                records_examined=len(jobs) + len(bindings) + len(run_events),
                sources={
                    "scheduled_jobs": len(jobs),
                    "workflow_bindings": len(bindings),
                    "automation_events": len(run_events),
                },
            ),
            analyzer_version=self.analyzer_version,
        )
