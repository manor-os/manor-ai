"""Workspace evaluation scorecard.

This module builds a read-only operating snapshot for a workspace. It is
intended for two callers:

* the API/UI, so an operator can see whether a workspace is actually working;
* the Strategist, so the next planning loop reasons from measured outcomes
  instead of only recent task titles.

The snapshot deliberately keeps billing, execution, goal, feedback, learning,
and governance dimensions separate. A workspace can be cheap but ineffective,
or productive but unsafe; the caller should be able to see both.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.budget import get_budget_status
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
from packages.core.models.task import Task
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.workspace import Workspace
from packages.core.services.credit_service import usd_to_credits


DEFAULT_WINDOW_DAYS = 30
EVALUATION_SNAPSHOT_EVIDENCE_TYPE = "workspace_evaluation_snapshot"


async def build_workspace_evaluation(
    db: AsyncSession,
    workspace_id: str,
    *,
    entity_id: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable evaluation snapshot for a workspace.

    The function is read-only and has no side effects. ``window_days`` controls
    task/evidence/usage recency; monthly budget fields still come from the
    workspace budget accumulator because that is the billing guardrail surface.
    """

    now = _ensure_aware(now or datetime.now(timezone.utc))
    window_days = max(1, min(int(window_days or DEFAULT_WINDOW_DAYS), 365))
    since = now - timedelta(days=window_days)

    ws_stmt = select(Workspace).where(Workspace.id == workspace_id)
    if entity_id:
        ws_stmt = ws_stmt.where(Workspace.entity_id == entity_id)
    workspace = (await db.execute(ws_stmt)).scalar_one_or_none()
    if workspace is None:
        raise ValueError(f"workspace {workspace_id!r} not found")

    goals = await _load_goals(db, workspace)
    tasks = await _load_recent_tasks(db, workspace, since)
    plans = await _load_recent_plans(db, workspace, since)
    steps = await _load_recent_steps(db, workspace, since)
    evidence = await _load_recent_evidence(db, workspace, since)
    history = await _load_evaluation_history(db, workspace, since)
    candidates = await _load_recent_learning_candidates(db, workspace, since)
    token_logs = await _load_token_logs(db, workspace, since)
    tool_logs = await _load_tool_logs(db, workspace, since)
    measurements = await _load_goal_measurements(db, [g.id for g in goals], since)
    goal_links = await _load_goal_task_links(db, [g.id for g in goals])

    budget = await get_budget_status(db, workspace.id)
    goal_impact = _build_goal_impact(goals, measurements, goal_links)
    execution = _build_execution_health(tasks, plans, steps)
    cost = _build_cost_efficiency(
        steps=steps,
        token_logs=token_logs,
        evidence=evidence,
        budget=budget.__dict__ if budget else None,
        completed_task_count=execution["completed_task_count"],
        goal_progress_points=goal_impact["aggregate"]["progress_points"],
    )
    time_efficiency = _build_time_efficiency(tasks, plans, steps)
    output_quality = _build_output_quality(tasks, steps, evidence)
    user_feedback = _build_user_feedback(evidence)
    governance = _build_governance_health(steps, evidence)
    learning = _build_learning_health(evidence, candidates)

    dimensions = {
        "goal_impact": goal_impact,
        "cost_efficiency": cost,
        "time_efficiency": time_efficiency,
        "execution_health": execution,
        "output_quality": output_quality,
        "user_feedback": user_feedback,
        "governance": governance,
        "learning": learning,
    }
    overall = _build_overall(dimensions)

    snapshot = {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "generated_at": now.isoformat(),
        "window": {
            "days": window_days,
            "start": since.isoformat(),
            "end": now.isoformat(),
        },
        "overall": overall,
        "dimensions": dimensions,
        "recommendations": _build_recommendations(dimensions, overall),
        "evidence_summary": {
            "goal_count": len(goals),
            "task_count": len(tasks),
            "plan_count": len(plans),
            "step_count": len(steps),
            "runtime_evidence_count": len(evidence),
            "learning_candidate_count": len(candidates),
            "token_log_count": len(token_logs),
            "tool_call_count": len(tool_logs),
        },
        "history": history,
    }
    snapshot["trend"] = _build_evaluation_trend(overall, history)
    return snapshot


async def record_workspace_evaluation_snapshot(
    db: AsyncSession,
    snapshot: dict[str, Any],
    *,
    entity_id: str,
    workspace_id: str,
    source: str = "strategist",
    trace_id: str | None = None,
) -> RuntimeEvidence:
    """Persist a compact scorecard history point in the runtime evidence ledger."""

    overall = snapshot.get("overall") or {}
    score = overall.get("score")
    dims = snapshot.get("dimensions") or {}
    dimension_scores = {
        key: section.get("score")
        for key, section in dims.items()
        if isinstance(section, dict)
    }
    clean_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key not in {"history"}
    }
    clean_snapshot = _json_safe(clean_snapshot)
    evidence = RuntimeEvidence(
        entity_id=entity_id,
        workspace_id=workspace_id,
        trace_id=trace_id[:64] if trace_id else None,
        evidence_type=EVALUATION_SNAPSHOT_EVIDENCE_TYPE,
        source=source,
        status="succeeded",
        summary=f"Workspace evaluation snapshot recorded with overall score {_fmt_score(score)}.",
        details={
            "snapshot": clean_snapshot,
            "window": clean_snapshot.get("window") or {},
            "recommendations": clean_snapshot.get("recommendations") or [],
            "trend": clean_snapshot.get("trend") or {},
        },
        metrics={
            "overall_score": score,
            "window_days": (snapshot.get("window") or {}).get("days"),
            "dimension_scores": dimension_scores,
        },
    )
    db.add(evidence)
    await db.flush()
    return evidence


def format_workspace_evaluation_for_prompt(snapshot: dict[str, Any] | None) -> str:
    """Compact markdown block for Strategist prompts."""

    if not snapshot:
        return ""
    overall = snapshot.get("overall") or {}
    dims = snapshot.get("dimensions") or {}
    lines = [
        f"Overall score: {_fmt_score(overall.get('score'))} "
        f"(confidence={overall.get('confidence') or 'unknown'})",
    ]
    for key, label in (
        ("goal_impact", "Goal impact"),
        ("cost_efficiency", "Cost efficiency"),
        ("time_efficiency", "Time efficiency"),
        ("execution_health", "Execution health"),
        ("output_quality", "Output quality"),
        ("user_feedback", "User feedback"),
        ("governance", "Governance"),
        ("learning", "Learning"),
    ):
        section = dims.get(key) or {}
        lines.append(f"- {label}: {_fmt_score(section.get('score'))} - {section.get('summary') or 'no summary'}")

    recommendations = snapshot.get("recommendations") or []
    if recommendations:
        lines.append("Recommended attention:")
        for item in recommendations[:5]:
            lines.append(f"- {item}")
    return "\n".join(lines)


async def _load_goals(db: AsyncSession, workspace: Workspace) -> list[Goal]:
    return list((await db.execute(
        select(Goal).where(
            Goal.workspace_id == workspace.id,
            Goal.entity_id == workspace.entity_id,
        ).order_by(Goal.created_at.desc())
    )).scalars().all())


async def _load_recent_tasks(db: AsyncSession, workspace: Workspace, since: datetime) -> list[Task]:
    return list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace.id,
            Task.entity_id == workspace.entity_id,
            or_(
                Task.created_at >= since,
                Task.completed_at >= since,
                Task.started_at >= since,
            ),
        ).order_by(Task.created_at.desc())
    )).scalars().all())


async def _load_recent_plans(db: AsyncSession, workspace: Workspace, since: datetime) -> list[ExecutionPlan]:
    return list((await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.workspace_id == workspace.id,
            ExecutionPlan.entity_id == workspace.entity_id,
            or_(
                ExecutionPlan.created_at >= since,
                ExecutionPlan.started_at >= since,
                ExecutionPlan.completed_at >= since,
            ),
        ).order_by(ExecutionPlan.created_at.desc())
    )).scalars().all())


async def _load_recent_steps(db: AsyncSession, workspace: Workspace, since: datetime) -> list[ExecutionStep]:
    return list((await db.execute(
        select(ExecutionStep).where(
            ExecutionStep.workspace_id == workspace.id,
            ExecutionStep.entity_id == workspace.entity_id,
            or_(
                ExecutionStep.created_at >= since,
                ExecutionStep.started_at >= since,
                ExecutionStep.finished_at >= since,
            ),
        ).order_by(ExecutionStep.created_at.desc())
    )).scalars().all())


async def _load_recent_evidence(db: AsyncSession, workspace: Workspace, since: datetime) -> list[RuntimeEvidence]:
    return list((await db.execute(
        select(RuntimeEvidence).where(
            RuntimeEvidence.workspace_id == workspace.id,
            RuntimeEvidence.entity_id == workspace.entity_id,
            RuntimeEvidence.created_at >= since,
            RuntimeEvidence.evidence_type != EVALUATION_SNAPSHOT_EVIDENCE_TYPE,
        ).order_by(RuntimeEvidence.created_at.desc())
    )).scalars().all())


async def _load_evaluation_history(
    db: AsyncSession,
    workspace: Workspace,
    since: datetime,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows = list((await db.execute(
        select(RuntimeEvidence).where(
            RuntimeEvidence.workspace_id == workspace.id,
            RuntimeEvidence.entity_id == workspace.entity_id,
            RuntimeEvidence.evidence_type == EVALUATION_SNAPSHOT_EVIDENCE_TYPE,
            RuntimeEvidence.created_at >= since,
        ).order_by(RuntimeEvidence.created_at.desc()).limit(limit)
    )).scalars().all())
    history: list[dict[str, Any]] = []
    for row in rows:
        metrics = row.metrics or {}
        details = row.details or {}
        snapshot = details.get("snapshot") if isinstance(details.get("snapshot"), dict) else {}
        history.append({
            "evidence_id": row.id,
            "recorded_at": row.created_at.isoformat() if row.created_at else None,
            "source": row.source,
            "overall_score": metrics.get("overall_score"),
            "confidence": (snapshot.get("overall") or {}).get("confidence"),
            "window_days": metrics.get("window_days") or (snapshot.get("window") or {}).get("days"),
            "dimension_scores": metrics.get("dimension_scores") or {},
        })
    return history


async def _load_recent_learning_candidates(
    db: AsyncSession, workspace: Workspace, since: datetime
) -> list[AgentLearningCandidate]:
    return list((await db.execute(
        select(AgentLearningCandidate).where(
            AgentLearningCandidate.workspace_id == workspace.id,
            AgentLearningCandidate.entity_id == workspace.entity_id,
            AgentLearningCandidate.created_at >= since,
        ).order_by(AgentLearningCandidate.created_at.desc())
    )).scalars().all())


async def _load_token_logs(db: AsyncSession, workspace: Workspace, since: datetime) -> list[TokenUsageLog]:
    return list((await db.execute(
        select(TokenUsageLog).where(
            TokenUsageLog.workspace_id == workspace.id,
            TokenUsageLog.entity_id == workspace.entity_id,
            TokenUsageLog.created_at >= since,
        )
    )).scalars().all())


async def _load_tool_logs(db: AsyncSession, workspace: Workspace, since: datetime) -> list[ToolCallLog]:
    return list((await db.execute(
        select(ToolCallLog).where(
            ToolCallLog.workspace_id == workspace.id,
            ToolCallLog.entity_id == workspace.entity_id,
            ToolCallLog.created_at >= since,
        )
    )).scalars().all())


async def _load_goal_measurements(
    db: AsyncSession, goal_ids: list[str], since: datetime
) -> dict[str, list[GoalMeasurement]]:
    if not goal_ids:
        return {}
    rows = list((await db.execute(
        select(GoalMeasurement).where(
            GoalMeasurement.goal_id.in_(goal_ids),
            GoalMeasurement.measured_at >= since,
        ).order_by(GoalMeasurement.measured_at.asc())
    )).scalars().all())
    out: dict[str, list[GoalMeasurement]] = {goal_id: [] for goal_id in goal_ids}
    for row in rows:
        out.setdefault(row.goal_id, []).append(row)
    return out


async def _load_goal_task_links(
    db: AsyncSession, goal_ids: list[str]
) -> dict[str, list[tuple[GoalTaskLink, Task | None]]]:
    if not goal_ids:
        return {}
    rows = list((await db.execute(
        select(GoalTaskLink, Task)
        .outerjoin(Task, Task.id == GoalTaskLink.task_id)
        .where(GoalTaskLink.goal_id.in_(goal_ids))
    )).all())
    out: dict[str, list[tuple[GoalTaskLink, Task | None]]] = {goal_id: [] for goal_id in goal_ids}
    for link, task in rows:
        out.setdefault(link.goal_id, []).append((link, task))
    return out


def _build_goal_impact(
    goals: list[Goal],
    measurements: dict[str, list[GoalMeasurement]],
    goal_links: dict[str, list[tuple[GoalTaskLink, Task | None]]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    progress_values: list[float] = []
    measured = 0
    progress_points = 0.0

    for goal in goals:
        ms = measurements.get(goal.id, [])
        links = goal_links.get(goal.id, [])
        status_counts = Counter((getattr(task, "status", None) or "missing") for _, task in links)
        current = _decimal_to_float(goal.current_value)
        target = _decimal_to_float(goal.target_value)
        baseline = _decimal_to_float(goal.baseline_value) or 0.0
        progress = _progress_fraction(baseline, current, target)
        if progress is not None:
            progress_values.append(progress)
            progress_points += max(0.0, min(1.0, progress)) * 100.0
        if ms or goal.current_value is not None:
            measured += 1

        rows.append({
            "goal_id": goal.id,
            "title": goal.title,
            "metric_key": goal.metric_key,
            "status": goal.status,
            "current_value": current,
            "target_value": target,
            "baseline_value": baseline,
            "progress_fraction": progress,
            "progress_pct": _pct(progress),
            "pace_status": goal.pace_status or "unknown",
            "deadline": goal.deadline.isoformat() if goal.deadline else None,
            "measurement_count": len(ms),
            "last_measured_at": ms[-1].measured_at.isoformat() if ms else (
                goal.current_value_updated_at.isoformat() if goal.current_value_updated_at else None
            ),
            "measurement_source": goal.measurement_source or {},
            "measurement_cadence": goal.measurement_cadence,
            "confidence": _goal_confidence(goal, len(ms)),
            "linked_task_count": len(links),
            "linked_task_status_counts": dict(status_counts),
            "completed_linked_task_count": status_counts.get("completed", 0),
        })

    total = len(goals)
    avg_progress = sum(progress_values) / len(progress_values) if progress_values else None
    measured_rate = measured / total if total else None
    score = None
    if total:
        progress_score = (avg_progress or 0) * 80
        measurement_score = (measured_rate or 0) * 20
        score = _clamp_score(progress_score + measurement_score)
    summary = (
        "No goals defined yet"
        if not total else
        f"{measured}/{total} goals measured; average progress {_fmt_score(_pct(avg_progress))}"
    )
    return {
        "score": score,
        "summary": summary,
        "aggregate": {
            "goal_count": total,
            "measured_goal_count": measured,
            "measured_goal_rate": measured_rate,
            "average_progress_fraction": avg_progress,
            "average_progress_pct": _pct(avg_progress),
            "progress_points": progress_points,
        },
        "goals": rows,
    }


def _build_execution_health(
    tasks: list[Task], plans: list[ExecutionPlan], steps: list[ExecutionStep]
) -> dict[str, Any]:
    task_status = Counter(t.status or "unknown" for t in tasks)
    plan_status = Counter(p.status or "unknown" for p in plans)
    step_status = Counter(s.step_status or "unknown" for s in steps)
    step_kind = Counter(s.kind or "unknown" for s in steps)
    completed_tasks = task_status.get("completed", 0)
    failed_tasks = task_status.get("failed", 0) + task_status.get("cancelled", 0)
    open_tasks = len(tasks) - completed_tasks - failed_tasks
    done_steps = step_status.get("done", 0)
    failed_steps = step_status.get("failed", 0) + step_status.get("cancelled", 0)
    waiting_steps = step_status.get("waiting_human", 0)

    task_completion_rate = completed_tasks / len(tasks) if tasks else None
    step_done_rate = done_steps / len(steps) if steps else None
    dependency_errors = _dependency_errors(steps)

    if tasks or steps:
        task_part = (task_completion_rate if task_completion_rate is not None else 0.6) * 35
        step_part = (step_done_rate if step_done_rate is not None else 0.6) * 35
        failure_penalty = min(30, (failed_tasks + failed_steps) * 8)
        waiting_penalty = min(10, waiting_steps * 2)
        dependency_penalty = min(20, len(dependency_errors) * 5)
        score = _clamp_score(task_part + step_part + 30 - failure_penalty - waiting_penalty - dependency_penalty)
    else:
        score = None

    return {
        "score": score,
        "summary": (
            f"{completed_tasks}/{len(tasks)} tasks completed; "
            f"{done_steps}/{len(steps)} steps done; {failed_tasks + failed_steps} failures"
        ),
        "task_count": len(tasks),
        "completed_task_count": completed_tasks,
        "open_task_count": open_tasks,
        "failed_task_count": failed_tasks,
        "task_status_counts": dict(task_status),
        "plan_count": len(plans),
        "plan_status_counts": dict(plan_status),
        "step_count": len(steps),
        "step_status_counts": dict(step_status),
        "step_kind_counts": dict(step_kind),
        "step_done_rate": step_done_rate,
        "task_completion_rate": task_completion_rate,
        "dependency_errors": dependency_errors,
    }


def _build_cost_efficiency(
    *,
    steps: list[ExecutionStep],
    token_logs: list[TokenUsageLog],
    evidence: list[RuntimeEvidence],
    budget: dict[str, Any] | None,
    completed_task_count: int,
    goal_progress_points: float,
) -> dict[str, Any]:
    step_usd = sum(_cost_usd(s.cost) for s in steps)
    token_usd = sum(float(t.cost_usd or 0) for t in token_logs)
    evidence_usd = sum(_runtime_evidence_cost_usd(ev) for ev in evidence)
    window_usd = step_usd + token_usd + evidence_usd
    window_credits = usd_to_credits(window_usd)

    monthly_spent = int((budget or {}).get("monthly_spent_credits") or 0)
    monthly_budget = (budget or {}).get("monthly_budget_credits")
    pct_used = (budget or {}).get("pct_used")
    cost_per_completed_task = (
        window_credits / completed_task_count if completed_task_count > 0 else None
    )
    cost_per_goal_progress_point = (
        window_credits / goal_progress_points if goal_progress_points > 0 else None
    )

    if monthly_budget:
        if pct_used is None:
            score = 70
        elif pct_used >= 1:
            score = 20
        elif pct_used >= 0.8:
            score = 55
        else:
            score = 90
    elif window_credits > 0:
        score = 70
    else:
        score = 85

    return {
        "score": score,
        "summary": (
            f"{window_credits} attributed credits in window; "
            f"{monthly_spent} monthly credits spent"
        ),
        "window_credits": window_credits,
        "window_usd": round(window_usd, 6),
        "window_step_credits": usd_to_credits(step_usd),
        "window_llm_credits": usd_to_credits(token_usd),
        "window_runtime_evidence_credits": usd_to_credits(evidence_usd),
        "monthly_spent_credits": monthly_spent,
        "monthly_budget_credits": monthly_budget,
        "monthly_remaining_credits": (budget or {}).get("monthly_remaining_credits"),
        "budget_pct_used": pct_used,
        "budget_alert_state": (budget or {}).get("alert_state"),
        "auto_pause_on_budget": bool((budget or {}).get("auto_pause_on_budget", False)),
        "cost_per_completed_task_credits": cost_per_completed_task,
        "cost_per_goal_progress_point_credits": cost_per_goal_progress_point,
    }


def _build_time_efficiency(
    tasks: list[Task], plans: list[ExecutionPlan], steps: list[ExecutionStep]
) -> dict[str, Any]:
    task_hours = [
        _duration_hours(t.started_at or t.created_at, t.completed_at)
        for t in tasks
        if t.completed_at and (t.started_at or t.created_at)
    ]
    plan_hours = [
        _duration_hours(p.started_at or p.created_at, p.completed_at)
        for p in plans
        if p.completed_at and (p.started_at or p.created_at)
    ]
    step_minutes = [
        _duration_hours(s.started_at, s.finished_at) * 60
        for s in steps
        if s.started_at and s.finished_at
    ]
    waiting_human = sum(1 for s in steps if s.step_status == "waiting_human")
    blocked = sum(1 for t in tasks if t.status in {"waiting_on_customer", "blocked"})
    avg_task = _avg(task_hours)
    avg_step = _avg(step_minutes)
    score = 80
    if avg_task is not None:
        score -= min(35, max(0, avg_task - 24) * 1.5)
    if avg_step is not None:
        score -= min(20, max(0, avg_step - 60) * 0.2)
    score -= min(25, (waiting_human + blocked) * 5)
    if not tasks and not steps:
        score = None
    return {
        "score": _clamp_score(score) if score is not None else None,
        "summary": (
            f"avg completed task cycle {_fmt_hours(avg_task)}; "
            f"{waiting_human} step(s) waiting for human input"
        ),
        "avg_completed_task_cycle_hours": avg_task,
        "avg_completed_plan_cycle_hours": _avg(plan_hours),
        "avg_completed_step_minutes": avg_step,
        "waiting_human_step_count": waiting_human,
        "blocked_task_count": blocked,
    }


def _build_output_quality(
    tasks: list[Task], steps: list[ExecutionStep], evidence: list[RuntimeEvidence]
) -> dict[str, Any]:
    approval_like_types = {"approval_decision", "external_message_decision"}
    approvals = 0
    rejections = 0
    revision_requests = 0
    for ev in evidence:
        if ev.evidence_type not in approval_like_types:
            continue
        details = ev.details or {}
        metrics = ev.metrics or {}
        choice = str(details.get("choice") or details.get("decision") or "").lower()
        approved = details.get("approved")
        if approved is None:
            if "approve" in choice or "accept" in choice or "confirm" in choice:
                approved = True
            elif "reject" in choice or "decline" in choice or "cancel" in choice:
                approved = False
            else:
                approved = (metrics.get("approved") == 1)
        if approved:
            approvals += 1
        else:
            rejections += 1
        if "change" in choice or "revision" in choice or "reject" in choice:
            revision_requests += 1

    completed = sum(1 for t in tasks if t.status == "completed")
    failed = sum(1 for t in tasks if t.status in {"failed", "cancelled"})
    schema_failures = sum(
        1 for s in steps
        if s.error and "schema" in str(s.error).lower()
    )
    artifacts = sum(_artifact_count(t.actual_output) for t in tasks)
    approval_total = approvals + rejections
    approval_rate = approvals / approval_total if approval_total else None
    base = approval_rate * 100 if approval_rate is not None else (
        completed / (completed + failed) * 100 if (completed + failed) else None
    )
    score = None if base is None else _clamp_score(base - min(25, schema_failures * 8))
    return {
        "score": score,
        "summary": (
            f"{approvals} approvals, {rejections} rejections, "
            f"{schema_failures} schema failure(s), {artifacts} artifact(s)"
        ),
        "approval_count": approvals,
        "rejection_count": rejections,
        "revision_request_count": revision_requests,
        "approval_rate": approval_rate,
        "schema_failure_count": schema_failures,
        "artifact_count": artifacts,
    }


def _build_user_feedback(evidence: list[RuntimeEvidence]) -> dict[str, Any]:
    signal_types = {
        "user_feedback",
        "approval_decision",
        "external_message_decision",
        "proposal_decision",
        "workspace_operation_decision",
        "hitl_resolution",
        "task_status_change",
    }
    signals = [ev for ev in evidence if ev.evidence_type in signal_types]
    positive = 0
    negative = 0
    for ev in signals:
        details = ev.details or {}
        metrics = ev.metrics or {}
        text = " ".join(str(v).lower() for v in (details.get("choice"), details.get("status"), details.get("decision")) if v)
        if metrics.get("approved") == 1 or "approve" in text or "accepted" in text:
            positive += 1
        if metrics.get("approved") == 0 and ev.evidence_type == "approval_decision":
            negative += 1
        if any(word in text for word in ("reject", "decline", "changes", "failed")):
            negative += 1
    score = None
    if positive or negative:
        score = _clamp_score((positive / max(1, positive + negative)) * 100)
    elif signals:
        score = 70
    return {
        "score": score,
        "summary": f"{len(signals)} user/operator signal(s); {positive} positive, {negative} negative",
        "signal_count": len(signals),
        "positive_signal_count": positive,
        "negative_signal_count": negative,
    }


def _build_governance_health(
    steps: list[ExecutionStep], evidence: list[RuntimeEvidence]
) -> dict[str, Any]:
    approval_required = sum(1 for s in steps if s.requires_approval)
    waiting_human = sum(1 for s in steps if s.step_status == "waiting_human")
    blocked = 0
    violations = 0
    for s in steps:
        error = s.error or {}
        etype = str(error.get("type") or "")
        if "GovernancePolicy" in etype:
            blocked += 1
        if "violation" in str(error).lower():
            violations += 1
    for ev in evidence:
        details = ev.details or {}
        if ev.status == "blocked":
            blocked += 1
        if details.get("policy_violation") or details.get("unauthorized_action"):
            violations += 1
    score = _clamp_score(100 - min(80, violations * 35) - min(20, blocked * 3))
    return {
        "score": score,
        "summary": (
            f"{approval_required} approval-gated step(s), {waiting_human} waiting; "
            f"{violations} policy violation(s)"
        ),
        "approval_required_step_count": approval_required,
        "waiting_human_step_count": waiting_human,
        "blocked_action_count": blocked,
        "policy_violation_count": violations,
    }


def _build_learning_health(
    evidence: list[RuntimeEvidence], candidates: list[AgentLearningCandidate]
) -> dict[str, Any]:
    candidate_status = Counter(c.status or "unknown" for c in candidates)
    candidate_type = Counter(c.candidate_type or "unknown" for c in candidates)
    applied = candidate_status.get("applied", 0)
    accepted = candidate_status.get("accepted", 0)
    proposed = candidate_status.get("proposed", 0)
    rejected = candidate_status.get("rejected", 0)
    evidence_count = len(evidence)
    if not evidence_count and not candidates:
        score = None
    else:
        score = 55 + min(25, evidence_count * 2) + min(20, (applied + accepted) * 8) - min(20, rejected * 4)
        if proposed and not (applied or accepted):
            score -= min(10, proposed * 2)
        score = _clamp_score(score)
    return {
        "score": score,
        "summary": f"{evidence_count} evidence record(s), {len(candidates)} learning candidate(s)",
        "runtime_evidence_count": evidence_count,
        "candidate_count": len(candidates),
        "candidate_status_counts": dict(candidate_status),
        "candidate_type_counts": dict(candidate_type),
    }


def _build_overall(dimensions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    weights = {
        "goal_impact": 0.25,
        "cost_efficiency": 0.12,
        "time_efficiency": 0.10,
        "execution_health": 0.18,
        "output_quality": 0.15,
        "user_feedback": 0.10,
        "governance": 0.06,
        "learning": 0.04,
    }
    weighted = 0.0
    used = 0.0
    for key, weight in weights.items():
        score = dimensions.get(key, {}).get("score")
        if score is None:
            continue
        weighted += float(score) * weight
        used += weight
    score = round(weighted / used) if used else None
    confidence = _overall_confidence(dimensions)
    return {
        "score": score,
        "confidence": confidence,
        "summary": f"Workspace health {_fmt_score(score)} with {confidence} confidence",
        "weights": weights,
    }


def _build_recommendations(dimensions: dict[str, dict[str, Any]], overall: dict[str, Any]) -> list[str]:
    recs: list[str] = []
    goal = dimensions["goal_impact"]
    if not goal["aggregate"]["goal_count"]:
        recs.append("Define measurable workspace goals before asking agents to optimize work.")
    elif (goal.get("score") or 0) < 50:
        recs.append("Refresh goal measurements and link active tasks to the goals they move.")

    cost = dimensions["cost_efficiency"]
    if cost.get("budget_pct_used") is not None and cost["budget_pct_used"] >= 0.8:
        recs.append("Review budget burn before starting the next autonomous work wave.")

    execution = dimensions["execution_health"]
    if execution.get("failed_task_count") or execution.get("dependency_errors"):
        recs.append("Fix failed tasks or broken DAG dependencies before creating more work.")

    feedback = dimensions["user_feedback"]
    if feedback.get("signal_count", 0) == 0:
        recs.append("Collect explicit approval/rejection feedback so workspace quality can be calibrated.")

    learning = dimensions["learning"]
    proposed = learning.get("candidate_status_counts", {}).get("proposed", 0)
    if proposed:
        recs.append("Review proposed learning candidates so recurring feedback becomes memory, rules, or skills.")

    if not recs and overall.get("score") is not None and overall["score"] >= 80:
        recs.append("Continue the current operating loop; monitor goal pace and cost after the next batch.")
    return recs[:6]


def _dependency_errors(steps: list[ExecutionStep]) -> list[dict[str, str]]:
    keys = {s.step_key for s in steps}
    errors: list[dict[str, str]] = []
    for step in steps:
        for dep in step.depends_on or []:
            if dep not in keys:
                errors.append({"step_key": step.step_key, "missing_dependency": str(dep)})
    return errors


def _goal_confidence(goal: Goal, measurement_count: int) -> str:
    source = goal.measurement_source or {}
    provider = str(source.get("provider") or "").lower()
    if measurement_count <= 0 and goal.current_value is None:
        return "low"
    if provider.startswith("integration"):
        return "high"
    if provider in {"workspace_internal", "twitter_x", "stripe", "analytics"}:
        return "high" if measurement_count else "medium"
    if provider == "manual":
        return "medium"
    return "medium" if measurement_count else "low"


def _progress_fraction(baseline: float | None, current: float | None, target: float | None) -> float | None:
    if current is None or target is None:
        return None
    baseline = baseline or 0.0
    gap = target - baseline
    if gap == 0:
        return 1.0 if current >= target else 0.0
    return (current - baseline) / gap


def _cost_usd(cost: dict | None) -> float:
    if not isinstance(cost, dict):
        return 0.0
    try:
        return max(0.0, float(cost.get("usd") or 0))
    except Exception:
        return 0.0


def _runtime_evidence_cost_usd(ev: RuntimeEvidence) -> float:
    metrics = ev.metrics or {}
    details = ev.details or {}
    for source in (metrics, details):
        for key in ("cost_usd", "usd"):
            if source.get(key) is not None:
                try:
                    return max(0.0, float(source[key]))
                except Exception:
                    return 0.0
        if source.get("credits") is not None:
            try:
                return max(0.0, float(source["credits"]) / 1000)
            except Exception:
                return 0.0
    return 0.0


def _artifact_count(output: dict | None) -> int:
    if not isinstance(output, dict):
        return 0
    count = 0
    for key in ("files", "artifacts", "attachments"):
        value = output.get(key)
        if isinstance(value, list):
            count += len(value)
    return count


def _duration_hours(start: datetime | None, end: datetime | None) -> float:
    if not start or not end:
        return 0.0
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    return max(0.0, (end - start).total_seconds() / 3600)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct(value: float | None) -> float | None:
    return round(value * 100, 2) if value is not None else None


def _fmt_score(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.0f}%"
    except Exception:
        return "unknown"


def _fmt_hours(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1:
        return f"{value * 60:.0f}m"
    return f"{value:.1f}h"


def _build_evaluation_trend(overall: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    current = overall.get("score")
    previous = history[0].get("overall_score") if history else None
    if current is None or previous is None:
        return {"previous_score": previous, "delta": None, "direction": "unknown"}
    try:
        delta = round(float(current) - float(previous), 2)
    except Exception:
        return {"previous_score": previous, "delta": None, "direction": "unknown"}
    if delta > 1:
        direction = "improving"
    elif delta < -1:
        direction = "declining"
    else:
        direction = "flat"
    return {
        "previous_score": previous,
        "delta": delta,
        "direction": direction,
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _decimal_to_float(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _clamp_score(value: float | int) -> int:
    return int(round(max(0, min(100, float(value)))))


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _overall_confidence(dimensions: dict[str, dict[str, Any]]) -> str:
    goal_count = dimensions["goal_impact"]["aggregate"]["goal_count"]
    measured_goals = dimensions["goal_impact"]["aggregate"]["measured_goal_count"]
    task_count = dimensions["execution_health"]["task_count"]
    evidence_count = dimensions["learning"]["runtime_evidence_count"]
    if goal_count and measured_goals >= max(1, goal_count // 2) and task_count >= 5 and evidence_count >= 5:
        return "high"
    if goal_count or task_count or evidence_count:
        return "medium"
    return "low"
