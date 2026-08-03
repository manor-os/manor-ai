"""ArtifactKnowledgeConsolidator — M4.4 (`domain=artifact_knowledge`).

L0 digest of the window's artifact facts: created/used counts and a
deliverable check — completed tasks whose ``expected_output`` declares
deliverables must have non-empty ``actual_output.files``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.base import SnapshotContext, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et
from packages.core.models.task import Task


class ArtifactKnowledgeConsolidator:
    domain = "artifact_knowledge"
    analyzer_version = "artifact_knowledge-consolidator-v1"
    critical = False

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        created_events = ctx.events_of(et.ARTIFACT_CREATED)
        used_events = ctx.events_of(et.ARTIFACT_USED)

        # Deliverable check on this window's completed tasks.
        completed_events = [
            e for e in ctx.events_of(et.EXECUTION_COMPLETED) if e.source_kind == "task"
        ]
        task_ids = {e.source_id for e in completed_events}
        tasks: list[Task] = []
        if task_ids:
            tasks = list((await db.execute(
                select(Task).where(Task.id.in_(task_ids))
            )).scalars().all())

        observations: list[Observation] = []
        deliverable_digests: list[dict] = []
        deliverables_missing = 0
        for task in tasks:
            declared = (task.expected_output or {}).get("deliverables")
            if not declared:
                continue
            files = (task.actual_output or {}).get("files") or []
            delivered = bool(files)
            deliverable_digests.append({
                "task_id": task.id,
                "declared": declared,
                "delivered": delivered,
                "files_count": len(files),
            })
            if not delivered:
                deliverables_missing += 1
                observations.append(Observation(
                    type="deliverable_missing",
                    description=(
                        f"task {task.title!r} completed with declared deliverables "
                        f"but produced no output files"
                    ),
                    evidence_refs=[f"task:{task.id}"],
                ))

        metrics = {
            "artifacts_created": len(created_events),
            "artifacts_used_downstream": len(used_events),
            "deliverables": deliverable_digests,
            "deliverables_missing": deliverables_missing,
        }
        artifact_events = created_events + used_events
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(created_events)} artifact(s) created, {len(used_events)} "
                f"used downstream; {deliverables_missing} deliverable(s) missing"
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(artifact_events),
            coverage=Coverage(
                records_examined=len(artifact_events) + len(completed_events) + len(tasks),
                sources={
                    "artifact_events": len(artifact_events),
                    "completed_task_events": len(completed_events),
                    "tasks_resolved": len(tasks),
                },
            ),
            analyzer_version=self.analyzer_version,
        )
