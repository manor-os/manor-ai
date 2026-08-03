"""CapacityCostConsolidator — M4.6 (`domain=capacity_cost`).

L0 digest of budget and activity volume: workspace budget fields (M8),
window execution-event counts as activity volume, and cost summed from
event payloads where present (missing cost payloads are declared in
``coverage.sources`` rather than guessed).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.base import SnapshotContext, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et

BUDGET_WARNING_REMAINING_PCT = 20.0

_ACTIVITY_EVENTS = (
    et.EXECUTION_REQUESTED,
    et.EXECUTION_STARTED,
    et.EXECUTION_COMPLETED,
    et.EXECUTION_FAILED,
    et.EXECUTION_CANCELLED,
    et.AUTOMATION_RUN_DISPATCHED,
    et.AUTOMATION_RUN_COMPLETED,
    et.AUTOMATION_RUN_FAILED,
)


class CapacityCostConsolidator:
    domain = "capacity_cost"
    analyzer_version = "capacity_cost-consolidator-v1"
    critical = False

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        workspace = ctx.workspace
        budget: Optional[float] = None
        spent = 0.0
        if workspace is not None:
            budget = float(workspace.monthly_budget_usd) if workspace.monthly_budget_usd is not None else None
            spent = float(workspace.monthly_spent_usd or 0)
        remaining_pct: Optional[float] = None
        if budget and budget > 0:
            remaining_pct = max((budget - spent) / budget * 100.0, 0.0)

        activity_events = ctx.events_of(*_ACTIVITY_EVENTS)
        window_cost = 0.0
        events_with_cost = 0
        for event in activity_events:
            cost = (event.payload or {}).get("cost")
            try:
                if cost is not None:
                    window_cost += float(cost)
                    events_with_cost += 1
            except (TypeError, ValueError):
                pass

        observations: list[Observation] = []
        if remaining_pct is not None and remaining_pct < BUDGET_WARNING_REMAINING_PCT:
            observations.append(Observation(
                type="budget_threshold_crossed",
                description=(
                    f"monthly budget {remaining_pct:.1f}% remaining "
                    f"(spent {spent:.2f} of {budget:.2f} USD)"
                ),
                evidence_refs=[f"workspace:{ctx.review.workspace_id}"],
            ))

        metrics = {
            "monthly_budget_usd": budget,
            "monthly_spent_usd": spent,
            "budget_remaining_pct": remaining_pct,
            "activity_events": len(activity_events),
            "window_cost_usd": window_cost,
            "events_with_cost_payload": events_with_cost,
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(activity_events)} activity event(s); spent {spent:.2f} USD "
                f"of {'uncapped' if budget is None else f'{budget:.2f} USD'} budget"
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(activity_events),
            coverage=Coverage(
                records_examined=len(activity_events) + (1 if workspace is not None else 0),
                records_missing_details=len(activity_events) - events_with_cost,
                sources={
                    "activity_events": len(activity_events),
                    "cost_payloads": (
                        f"{events_with_cost}/{len(activity_events)} events carried "
                        f"a cost payload; the rest counted as 0"
                    ),
                },
            ),
            analyzer_version=self.analyzer_version,
        )
