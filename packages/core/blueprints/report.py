"""Simulation report — "should I promote this sandbox to live?"

Reads the steps that ran during the workspace's simulation window and
produces a digest the operator can stare at for 30 seconds before
clicking Promote. Three sections:

  Activity         What did the workspace do? Step counts by kind /
                   action_key / status.

  Cost             What did it spend? Total credits + projected
                   monthly burn (extrapolated from the simulation
                   period). Also: cost by kind so the operator can
                   spot a runaway category.

  Counterfactual   What would have happened under each governance
                   preset? Re-runs every historic step through Safe
                   and Aggressive variants; reports how many would
                   have been blocked / paused / allowed under each.
                   Lets the operator answer "if I switch to Safe
                   before promoting, what breaks?".

  Goal pace        For each goal in the workspace, did the simulated
                   measurements move toward the target?

Read-only — no side effects.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance import get_policy
from packages.core.governance.policy import decide
from packages.core.governance.presets import list_presets
from packages.core.models.execution import ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.workspace import Workspace
from packages.core.services.credit_service import credits_to_usd, usd_to_credits

logger = logging.getLogger(__name__)


# ── DTOs ──────────────────────────────────────────────────────────────

@dataclass
class ActivitySection:
    total_steps: int
    by_status: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_action_key: dict[str, int] = field(default_factory=dict)
    """Top 10 action_keys by count — small enough to render in a chip row."""

    governance_paused: int = 0
    """Steps the dispatcher paused for HITL approval."""
    governance_denied: int = 0
    """Steps the dispatcher hard-denied via never_allow."""


@dataclass
class CostSection:
    total_credits: int
    total_usd: float
    by_kind_credits: dict[str, int] = field(default_factory=dict)
    """Credits spent per step.kind — surfaces runaway categories."""

    simulation_days: float = 0.0
    daily_avg_credits: float = 0.0
    projected_monthly_credits: int = 0
    """Naïve extrapolation: daily_avg × 30. Operator sees the number,
    decides if they can stomach it."""


@dataclass
class CounterfactualOutcome:
    """What would have happened if the workspace had been on this preset."""

    preset_key: str
    title: str
    allowed: int
    """Steps that would have run."""
    paused_for_hitl: int
    denied: int
    """Steps that would have been blocked outright."""

    # If we re-ran with this preset, how many of the steps that DID
    # complete would have been gated. Helps the operator see "Safe
    # would have stopped 30% of what happened — am I OK with that?".
    delta_blocked_vs_actual: int = 0


@dataclass
class GoalPace:
    goal_id: str
    title: str
    metric_key: str
    target_value: Optional[float]
    baseline_value: Optional[float]
    first_measurement_value: Optional[float]
    last_measurement_value: Optional[float]
    measurement_count: int
    """How much progress was made during the sim, as a 0..>1 fraction
    of the gap baseline → target."""
    progress_fraction: Optional[float] = None


@dataclass
class SimulationReport:
    workspace_id: str
    workspace_name: str
    in_simulation: bool
    """True if settings.sandbox is still on. False after promote — the
    report still works, just shows the historic window."""
    governance_preset: Optional[str]
    """The preset the operator picked at install time. None for
    workspaces not installed from a blueprint."""

    window_start: Optional[datetime]
    """When the simulation started (settings._blueprint.installed_at).
    None if no blueprint metadata is present."""
    window_end: datetime
    """``now`` for in-flight sim, ``promoted_at`` for promoted ones."""

    activity: ActivitySection
    cost: CostSection
    counterfactuals: list[CounterfactualOutcome]
    goals: list[GoalPace]

    notes: list[str] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────

async def simulate_report(
    db: AsyncSession, workspace_id: str,
) -> SimulationReport:
    """Build the report. Read-only."""
    ws = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        raise ValueError(f"workspace {workspace_id!r} not found")

    bp_meta = (ws.settings or {}).get("_blueprint") or {}
    in_simulation = bool((ws.settings or {}).get("sandbox"))
    preset_key = bp_meta.get("governance_preset")

    window_start = _parse_iso(bp_meta.get("installed_at"))
    window_end = _parse_iso(bp_meta.get("promoted_at")) or datetime.now(timezone.utc)

    # ── Pull every step that ran in the window ──
    stmt = select(ExecutionStep).where(
        ExecutionStep.workspace_id == workspace_id,
    )
    if window_start is not None:
        # finished_at is None for in-flight; include those by checking
        # started_at instead so the report covers running plans.
        stmt = stmt.where(ExecutionStep.started_at >= window_start)
    steps = list((await db.execute(stmt)).scalars().all())

    evidence_stmt = select(RuntimeEvidence).where(
        RuntimeEvidence.workspace_id == workspace_id,
    )
    if window_start is not None:
        evidence_stmt = evidence_stmt.where(RuntimeEvidence.created_at >= window_start)
    evidence_stmt = evidence_stmt.where(RuntimeEvidence.created_at <= window_end)
    runtime_evidence = list((await db.execute(evidence_stmt)).scalars().all())

    activity = _build_activity(steps, runtime_evidence)
    cost = _build_cost(steps, runtime_evidence, window_start, window_end)

    counterfactuals = await _build_counterfactuals(db, workspace_id, steps)

    goals = await _build_goal_pace(db, workspace_id, window_start, window_end)

    notes = _build_notes(ws, in_simulation, activity, cost, counterfactuals)

    return SimulationReport(
        workspace_id=ws.id,
        workspace_name=ws.name,
        in_simulation=in_simulation,
        governance_preset=preset_key,
        window_start=window_start,
        window_end=window_end,
        activity=activity,
        cost=cost,
        counterfactuals=counterfactuals,
        goals=goals,
        notes=notes,
    )


# ── Section builders ──────────────────────────────────────────────────

def _build_activity(
    steps: list[ExecutionStep],
    runtime_evidence: list[RuntimeEvidence] | None = None,
) -> ActivitySection:
    by_status: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_action: Counter[str] = Counter()
    paused = 0
    denied = 0
    for s in steps:
        by_status[s.step_status or "unknown"] += 1
        by_kind[s.kind or "unknown"] += 1
        if s.action_key:
            by_action[s.action_key] += 1
        err = s.error or {}
        if err.get("type") == "GovernancePolicyHITL":
            paused += 1
        elif err.get("type") == "GovernancePolicy":
            denied += 1
    for ev in runtime_evidence or []:
        by_status[ev.status or "unknown"] += 1
        by_kind[ev.evidence_type or "runtime_evidence"] += 1
        details = ev.details or {}
        action_key = (
            details.get("action_key")
            or details.get("tool_action_key")
            or details.get("tool_name")
            or details.get("source_kind")
        )
        if action_key:
            by_action[str(action_key)] += 1
        if ev.status == "pending" and details.get("pause_for_hitl"):
            paused += 1
        if ev.status == "blocked":
            denied += 1
    return ActivitySection(
        total_steps=len(steps) + len(runtime_evidence or []),
        by_status=dict(by_status),
        by_kind=dict(by_kind),
        by_action_key=dict(by_action.most_common(10)),
        governance_paused=paused,
        governance_denied=denied,
    )


def _build_cost(
    steps: list[ExecutionStep],
    runtime_evidence: list[RuntimeEvidence] | None,
    window_start: Optional[datetime],
    window_end: datetime,
) -> CostSection:
    total_usd = 0.0
    by_kind_usd: dict[str, float] = {}
    for s in steps:
        usd = float((s.cost or {}).get("usd") or 0)
        if usd <= 0:
            continue
        total_usd += usd
        k = s.kind or "unknown"
        by_kind_usd[k] = by_kind_usd.get(k, 0.0) + usd
    for ev in runtime_evidence or []:
        usd = _runtime_evidence_cost_usd(ev)
        if usd <= 0:
            continue
        total_usd += usd
        k = ev.evidence_type or "runtime_evidence"
        by_kind_usd[k] = by_kind_usd.get(k, 0.0) + usd

    total_credits = usd_to_credits(total_usd)
    by_kind_credits = {k: usd_to_credits(v) for k, v in by_kind_usd.items()}

    if window_start:
        sim_days = max(
            (window_end - window_start).total_seconds() / 86_400.0,
            0.001,  # avoid /0 when looking immediately after install
        )
    else:
        sim_days = 0.0

    daily_avg = total_credits / sim_days if sim_days > 0 else 0.0
    projected_monthly = int(round(daily_avg * 30))

    return CostSection(
        total_credits=total_credits,
        total_usd=total_usd,
        by_kind_credits=by_kind_credits,
        simulation_days=round(sim_days, 2) if sim_days else 0.0,
        daily_avg_credits=round(daily_avg, 2),
        projected_monthly_credits=projected_monthly,
    )


def _runtime_evidence_cost_usd(ev: RuntimeEvidence) -> float:
    metrics = ev.metrics or {}
    details = ev.details or {}
    for source in (metrics, details):
        for key in ("usd", "cost_usd", "total_usd"):
            try:
                value = float(source.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        for key in ("credits", "credit_cost", "total_credits"):
            try:
                value = int(float(source.get(key) or 0))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return credits_to_usd(value)
    return 0.0


async def _build_counterfactuals(
    db: AsyncSession, workspace_id: str, steps: list[ExecutionStep],
) -> list[CounterfactualOutcome]:
    """Replay every historic step through each preset's variant of the
    workspace's current policy. Reports how the verdict would have
    differed."""
    base_policy = await get_policy(db, workspace_id)
    out: list[CounterfactualOutcome] = []

    # Count the actual outcome (post-current-policy) so the operator
    # has a baseline to compare against.
    actual_blocked = sum(
        1 for s in steps
        if (s.error or {}).get("type") in (
            "GovernancePolicy", "GovernancePolicyHITL",
        )
    )

    for preset in list_presets():
        variant = preset.transform(base_policy)
        allowed = paused = denied = 0
        for s in steps:
            d = decide(
                variant,
                kind=s.kind,
                action_key=s.action_key,
                risk_level=s.risk_level or "low",
            )
            if d.allowed:
                allowed += 1
            elif d.pause_for_hitl:
                paused += 1
            else:
                denied += 1
        blocked_under_preset = paused + denied
        out.append(CounterfactualOutcome(
            preset_key=preset.key,
            title=preset.title,
            allowed=allowed,
            paused_for_hitl=paused,
            denied=denied,
            delta_blocked_vs_actual=blocked_under_preset - actual_blocked,
        ))
    return _dedupe_goal_paces(out)


def _dedupe_goal_paces(goals: list[GoalPace]) -> list[GoalPace]:
    by_key: dict[str, GoalPace] = {}
    for goal in goals:
        key = _goal_pace_key(goal)
        existing = by_key.get(key)
        if existing is None or _goal_pace_score(goal) > _goal_pace_score(existing):
            by_key[key] = goal
    return list(by_key.values())


def _goal_pace_key(goal: GoalPace) -> str:
    title = (goal.title or "").strip().lower()
    if title:
        return f"title:{title}"
    return f"metric:{(goal.metric_key or goal.goal_id).strip().lower()}"


def _goal_pace_score(goal: GoalPace) -> int:
    score = goal.measurement_count * 10
    if goal.target_value not in (None, 0):
        score += 5
    if goal.baseline_value is not None:
        score += 3
    if goal.last_measurement_value is not None:
        score += 5
    if goal.progress_fraction is not None:
        score += 2
    return score


async def _build_goal_pace(
    db: AsyncSession, workspace_id: str,
    window_start: Optional[datetime], window_end: datetime,
) -> list[GoalPace]:
    goals = list((await db.execute(
        select(Goal).where(
            Goal.workspace_id == workspace_id,
            Goal.status == "active",
        )
    )).scalars().all())
    if not goals:
        return []

    out: list[GoalPace] = []
    for g in goals:
        m_stmt = select(GoalMeasurement).where(
            GoalMeasurement.goal_id == g.id,
        ).order_by(GoalMeasurement.measured_at.asc())
        if window_start is not None:
            m_stmt = m_stmt.where(GoalMeasurement.measured_at >= window_start)
        m_stmt = m_stmt.where(GoalMeasurement.measured_at <= window_end)
        rows = list((await db.execute(m_stmt)).scalars().all())

        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        baseline = float(g.baseline_value) if g.baseline_value is not None else None
        target = float(g.target_value) if g.target_value is not None else None
        progress = None
        if (
            target is not None and baseline is not None
            and last is not None and target != baseline
        ):
            progress = round(
                (float(last.value) - baseline) / (target - baseline), 4,
            )

        out.append(GoalPace(
            goal_id=g.id,
            title=g.title,
            metric_key=g.metric_key,
            target_value=target,
            baseline_value=baseline,
            first_measurement_value=float(first.value) if first else None,
            last_measurement_value=float(last.value) if last else None,
            measurement_count=len(rows),
            progress_fraction=progress,
        ))
    return out


def _build_notes(
    ws: Workspace,
    in_simulation: bool,
    activity: ActivitySection,
    cost: CostSection,
    counterfactuals: list[CounterfactualOutcome],
) -> list[str]:
    """Operator-readable callouts. Surfaces the things they probably
    care about without making them parse the raw numbers."""
    notes: list[str] = []

    if not in_simulation:
        notes.append(
            "Workspace is already promoted to live — this report covers "
            "the historic simulation window."
        )

    if activity.total_steps == 0:
        notes.append(
            "No steps ran during the simulation window. Either nothing "
            "was scheduled yet, or the workspace is too fresh to evaluate."
        )

    if activity.governance_paused > 0:
        notes.append(
            f"{activity.governance_paused} step(s) paused for your approval "
            "during the simulation — review them before promoting."
        )

    if cost.projected_monthly_credits > 0:
        notes.append(
            f"At the simulation's pace, this workspace would burn "
            f"~{cost.projected_monthly_credits} credits/month."
        )

    safe = next((c for c in counterfactuals if c.preset_key == "safe"), None)
    aggressive = next((c for c in counterfactuals if c.preset_key == "aggressive"), None)
    if safe and safe.delta_blocked_vs_actual > 0:
        notes.append(
            f"Switching to 'Safe' before promote would have paused "
            f"{safe.delta_blocked_vs_actual} additional step(s) for HITL."
        )
    if aggressive and aggressive.delta_blocked_vs_actual < 0:
        notes.append(
            f"Switching to 'Aggressive' would have let "
            f"{abs(aggressive.delta_blocked_vs_actual)} more step(s) "
            "run unsupervised."
        )

    return notes


# ── Helpers ──────────────────────────────────────────────────────────

def _parse_iso(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
