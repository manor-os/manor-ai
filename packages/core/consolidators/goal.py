"""GoalConsolidator — M4.1 (`domain=goal`, critical).

L0 digest of active goals: per-goal window movement + pace facts from
``goal_*`` ledger events, measurement-gap detection against the
``goal_measurements`` baseline, and a MEASUREMENT_SOURCE_UNAVAILABLE
uncertainty when a goal's measurement provider has no active
integration credential.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.consolidators.base import SnapshotContext, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
    Uncertainty,
)
from packages.core.ledger import event_types as et
from packages.core.models.document import Integration
from packages.core.models.goal import Goal, GoalMeasurement


def _float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class GoalConsolidator:
    domain = "goal"
    analyzer_version = "goal-consolidator-v1"
    critical = True

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        review = ctx.review
        goals = list((await db.execute(
            select(Goal).where(
                Goal.workspace_id == review.workspace_id,
                Goal.status == "active",
            ).order_by(Goal.priority.asc(), Goal.id.asc())
        )).scalars().all())

        goal_events = ctx.events_of(
            et.GOAL_MEASURED, et.GOAL_PACE_CHANGED, et.GOAL_ACHIEVED, et.GOAL_CHANGED,
        )
        events_by_goal: dict[str, list] = {}
        for event in goal_events:
            events_by_goal.setdefault(event.source_id, []).append(event)

        execution_happened = bool(ctx.events_of(et.EXECUTION_COMPLETED))

        observations: list[Observation] = []
        uncertainties: list[Uncertainty] = []
        digests: list[dict] = []
        goals_without_recent_measurement = 0

        for goal in goals:
            own_events = events_by_goal.get(goal.id, [])
            measured = [e for e in own_events if e.event_type == et.GOAL_MEASURED]
            values = [v for v in (_float((e.payload or {}).get("value")) for e in measured) if v is not None]
            window_delta = (values[-1] - values[0]) if len(values) >= 2 else 0.0

            digests.append({
                "goal_id": goal.id,
                "title": goal.title,
                "metric_key": goal.metric_key,
                "current": _float(goal.current_value),
                "target": _float(goal.target_value),
                "pace_status": goal.pace_status,
                "window_delta": window_delta,
                "measurement_count": len(measured),
                "last_measured_at": (
                    goal.current_value_updated_at.isoformat()
                    if goal.current_value_updated_at else None
                ),
                "deadline": goal.deadline.isoformat() if goal.deadline else None,
                # M11 CAS token — the Strategist copies this into a
                # goal_change's expected_revision so a change decided on this
                # briefing fails fast if the goal moved meanwhile.
                "revision": int(goal.revision or 1),
            })

            # pace_degraded — from goal_pace_changed facts in the window.
            for event in own_events:
                if event.event_type != et.GOAL_PACE_CHANGED:
                    continue
                payload = event.payload or {}
                if payload.get("new_pace") in ("behind", "at_risk"):
                    observations.append(Observation(
                        type="pace_degraded",
                        description=(
                            f"goal {goal.title!r} pace changed "
                            f"{payload.get('old_pace')} -> {payload.get('new_pace')}"
                        ),
                        evidence_refs=[event.id],
                    ))

            for event in own_events:
                if event.event_type == et.GOAL_ACHIEVED:
                    observations.append(Observation(
                        type="goal_achieved",
                        description=f"goal {goal.title!r} reached its target",
                        evidence_refs=[event.id],
                    ))

            # measurement_gap — cadence declared but nothing measured in the
            # window AND the latest goal_measurements row predates the window.
            if goal.measurement_cadence and not measured:
                last_measured_at = (await db.execute(
                    select(func.max(GoalMeasurement.measured_at)).where(
                        GoalMeasurement.goal_id == goal.id
                    )
                )).scalar()
                stale = (
                    last_measured_at is None
                    or review.window_start is None
                    or last_measured_at < review.window_start
                )
                if stale:
                    goals_without_recent_measurement += 1
                    observations.append(Observation(
                        type="measurement_gap",
                        description=(
                            f"goal {goal.title!r} declares cadence "
                            f"{goal.measurement_cadence!r} but was not measured this window"
                        ),
                        evidence_refs=[f"goal:{goal.id}"],
                    ))

            # goal_stalled — execution happened, metric did not move.
            if execution_happened and window_delta == 0.0 and len(values) >= 2:
                observations.append(Observation(
                    type="goal_stalled",
                    description=(
                        f"goal {goal.title!r} metric unchanged this window "
                        f"despite completed executions"
                    ),
                    evidence_refs=[f"goal:{goal.id}"] + evidence_ids(measured),
                ))

            # MEASUREMENT_SOURCE_UNAVAILABLE — declared provider, no active
            # credential (cheap per-goal existence query on integrations).
            provider = (goal.measurement_source or {}).get("provider")
            if provider:
                has_integration = (await db.execute(
                    select(Integration.id).where(
                        Integration.entity_id == review.entity_id,
                        Integration.provider == provider,
                        Integration.status == "active",
                    ).limit(1)
                )).scalar_one_or_none()
                if has_integration is None:
                    uncertainties.append(Uncertainty(
                        code="MEASUREMENT_SOURCE_UNAVAILABLE",
                        description=(
                            f"goal {goal.title!r} measurement provider "
                            f"{provider!r} has no active integration"
                        ),
                    ))

        metrics = {
            "goals_total": len(goals),
            "goals_active": len(goals),
            "goals": digests,
            "goals_without_recent_measurement": goals_without_recent_measurement,
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(goals)} active goal(s); {len(goal_events)} goal event(s) "
                f"in window; {goals_without_recent_measurement} measurement gap(s)"
            ),
            metrics=metrics,
            observations=observations,
            uncertainties=uncertainties,
            evidence_refs=evidence_ids(goal_events),
            coverage=Coverage(
                records_examined=len(goals) + len(goal_events),
                sources={"goals": len(goals), "goal_events": len(goal_events)},
            ),
            analyzer_version=self.analyzer_version,
        )
