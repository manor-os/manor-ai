"""ExecutionConsolidator — M4.2 (`domain=execution`, critical).

L0 digest of window ``execution_*`` events: counts by (source_kind,
status), success rate, waiting_human backlog (direct ``execution_steps``
query — steps carry ``workspace_id`` themselves, no plan/task join
needed), repeated-failure grouping by ``owner_service_key``.

L1 (opt-in, ``MANOR_CONSOLIDATOR_L1``): when ≥3 executions failed in the
window, the unstructured ``step.error`` texts are clustered by a single
LLM call into ``failure_cluster`` observations. The L0 digest above is
unchanged either way — L1 only ever ADDS observations, and
``coverage.sources["l1"]`` records whether it ran.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ExecutionStepStatus,
)
from packages.core.consolidators import l1 as l1_layer
from packages.core.consolidators.base import SnapshotContext, age_hours, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.workspace_event import WorkspaceEvent

LONG_BLOCKED_HOURS = 24.0
REPEATED_FAILURE_THRESHOLD = 3
FAILURE_CLUSTER_MIN_COUNT = 2

_EXECUTION_EVENTS = (
    et.EXECUTION_REQUESTED,
    et.EXECUTION_STARTED,
    et.EXECUTION_COMPLETED,
    et.EXECUTION_FAILED,
    et.EXECUTION_CANCELLED,
)


def _error_text(value) -> str:
    """Best-effort human-readable error string out of a step/task/event blob."""
    if isinstance(value, dict):
        message = value.get("message") or value.get("error") or value.get("detail")
        kind = value.get("type") or value.get("code")
        if message and kind:
            return f"{kind}: {message}"
        return str(message or kind or "")
    return str(value or "")


async def _failure_errors_by_task(
    db: AsyncSession, task_ids: set[str],
) -> dict[str, str]:
    """``task_id → first non-empty failed-step error text`` for the window.

    Two cheap indexed lookups (plans by task, failed steps by plan); the
    error strings themselves are already-recorded executor output, so no
    new data is exposed by reading them here.
    """
    if not task_ids:
        return {}
    plans = list((await db.execute(
        select(ExecutionPlan.id, ExecutionPlan.task_id).where(
            ExecutionPlan.task_id.in_(task_ids)
        )
    )).all())
    task_by_plan = {plan_id: task_id for plan_id, task_id in plans}
    if not task_by_plan:
        return {}
    steps = list((await db.execute(
        select(ExecutionStep).where(
            ExecutionStep.plan_id.in_(task_by_plan.keys()),
            ExecutionStep.step_status == ExecutionStepStatus.FAILED,
        )
    )).scalars().all())
    errors: dict[str, str] = {}
    for step in steps:
        task_id = task_by_plan.get(step.plan_id)
        text = _error_text(step.error)
        if task_id and text and task_id not in errors:
            errors[task_id] = text
    return errors


class ExecutionConsolidator:
    domain = "execution"
    # v2 — adds the opt-in L1 failure-cluster layer (L0 output unchanged).
    analyzer_version = "execution-consolidator-v2"
    critical = True

    async def _failure_records(
        self,
        db: AsyncSession,
        failed_events: list[WorkspaceEvent],
        tasks_by_id: dict[str, Task],
    ) -> tuple[list[dict], list[list[WorkspaceEvent]]]:
        """Compact, anonymized failure records + their backing events.

        Records carry only ``{count, sample_error, service_key}`` — the
        error text truncated to 200 chars, no ids, no params, no payloads
        beyond what the executor already wrote into the error string.
        Identical (service, error) pairs collapse into one record so the
        L1 prompt stays small; the parallel ``record_events`` list keeps
        the evidence refs for whatever the model groups together.
        """
        errors_by_task = await _failure_errors_by_task(
            db,
            {
                event.source_id for event in failed_events
                if event.source_kind == "task"
            },
        )

        grouped: dict[tuple[str, str], list[WorkspaceEvent]] = {}
        for event in failed_events:
            task = tasks_by_id.get(event.source_id)
            service_key = (task.owner_service_key if task is not None else None) or ""
            text = errors_by_task.get(event.source_id) or ""
            if not text and task is not None:
                text = _error_text((task.details or {}).get("error"))
            if not text:
                text = _error_text((event.payload or {}).get("error"))
            text = l1_layer.truncate_error(text)
            if not text:
                continue  # nothing unstructured to cluster
            grouped.setdefault((service_key, text), []).append(event)

        records: list[dict] = []
        record_events: list[list[WorkspaceEvent]] = []
        for (service_key, text), member_events in sorted(
            grouped.items(), key=lambda item: (-len(item[1]), item[0]),
        )[:l1_layer.MAX_L1_INPUT_RECORDS]:
            records.append({
                "count": len(member_events),
                "sample_error": text,
                "service_key": service_key,
            })
            record_events.append(member_events)
        return records, record_events

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        events = ctx.events_of(*_EXECUTION_EVENTS)

        counts: dict[str, dict[str, int]] = {}
        for event in events:
            by_type = counts.setdefault(event.source_kind, {})
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1

        completed = sum(1 for e in events if e.event_type == et.EXECUTION_COMPLETED)
        failed = sum(1 for e in events if e.event_type == et.EXECUTION_FAILED)
        finished = completed + failed
        success_rate = (completed / finished) if finished else None

        # waiting_human backlog — execution_steps carries workspace_id
        # directly, so no ExecutionPlan→Task join is required.
        waiting_steps = list((await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.workspace_id == ctx.review.workspace_id,
                ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN,
            )
        )).scalars().all())
        waiting_ages = [
            age for age in (
                age_hours(step.updated_at or step.created_at, ctx.now)
                for step in waiting_steps
            ) if age is not None
        ]
        oldest_waiting_hours = max(waiting_ages, default=None)

        observations: list[Observation] = []

        # repeated_failure_pattern — >=3 execution_failed sharing an
        # owner_service_key (resolved through the failed events' Task rows).
        failed_events = [e for e in events if e.event_type == et.EXECUTION_FAILED]
        failed_task_events = [e for e in failed_events if e.source_kind == "task"]
        task_ids = {e.source_id for e in failed_task_events}
        tasks_by_id: dict[str, Task] = {}
        if task_ids:
            tasks_by_id = {
                task.id: task
                for task in (await db.execute(
                    select(Task).where(Task.id.in_(task_ids))
                )).scalars().all()
            }
        failures_by_owner: dict[str, list] = {}
        for event in failed_task_events:
            task = tasks_by_id.get(event.source_id)
            owner = task.owner_service_key if task is not None else None
            if owner:
                failures_by_owner.setdefault(owner, []).append(event)
        for owner, owner_events in sorted(failures_by_owner.items()):
            if len(owner_events) >= REPEATED_FAILURE_THRESHOLD:
                observations.append(Observation(
                    type="repeated_failure_pattern",
                    description=(
                        f"{len(owner_events)} execution failures for tasks owned "
                        f"by service {owner!r} this window"
                    ),
                    evidence_refs=evidence_ids(owner_events),
                ))

        # ── L1 (opt-in): semantic clustering of unstructured errors ─────
        # M4.2 — only when the window holds ≥3 failures, and at most ONE
        # LLM call per review (single call site, no retry: the budget is
        # structural, see consolidators/l1.py).
        l1_marker = l1_layer.L1_DISABLED
        if l1_layer.l1_enabled():
            l1_marker = l1_layer.L1_SKIPPED
            records, record_events = await self._failure_records(
                db, failed_events, tasks_by_id,
            )
            if len(failed_events) >= REPEATED_FAILURE_THRESHOLD and records:
                clusters = await l1_layer.summarize_failure_clusters(
                    records,
                    entity_id=ctx.review.entity_id,
                    workspace_id=ctx.review.workspace_id,
                )
                if clusters is None:
                    l1_marker = l1_layer.L1_UNAVAILABLE
                else:
                    l1_marker = l1_layer.L1_USED
                    for cluster in clusters:
                        members: list[WorkspaceEvent] = []
                        for index in cluster["member_indexes"]:
                            members.extend(record_events[index])
                        # Counted from the evidence, not from the model:
                        # the description must stay a verifiable fact.
                        if len(members) < FAILURE_CLUSTER_MIN_COUNT:
                            continue
                        observations.append(Observation(
                            type="failure_cluster",
                            description=(
                                f"{cluster['cluster']} ({len(members)} executions)"
                            ),
                            evidence_refs=evidence_ids(members),
                        ))

        for step in waiting_steps:
            waited = age_hours(step.updated_at or step.created_at, ctx.now)
            if waited is not None and waited > LONG_BLOCKED_HOURS:
                observations.append(Observation(
                    type="long_blocked_execution",
                    description=(
                        f"step {step.step_key!r} has been waiting on human input "
                        f"for {waited:.1f}h"
                    ),
                    evidence_refs=[f"step:{step.id}"],
                ))

        metrics = {
            "events_by_source_kind": counts,
            "completed_total": completed,
            "failed_total": failed,
            "cancelled_total": sum(
                1 for e in events if e.event_type == et.EXECUTION_CANCELLED
            ),
            "success_rate": success_rate,
            "waiting_human_steps": len(waiting_steps),
            "oldest_waiting_human_hours": oldest_waiting_hours,
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(events)} execution event(s): {completed} completed, "
                f"{failed} failed; {len(waiting_steps)} step(s) waiting on human"
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(events),
            coverage=Coverage(
                records_examined=len(events) + len(waiting_steps) + len(tasks_by_id),
                sources={
                    "l1": l1_marker,
                    "execution_events": len(events),
                    "waiting_human_steps": len(waiting_steps),
                    "tasks_resolved": len(tasks_by_id),
                },
            ),
            analyzer_version=self.analyzer_version,
        )
