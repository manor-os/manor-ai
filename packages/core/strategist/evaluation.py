"""Outcome labeling for completed Strategist proposals.

The Strategist predicts ``estimated_impact.metric_delta`` for each task.
After the task completes and the goal's ``outcome_window_days`` have
elapsed, we look at how the goal *actually* moved during the window and
label the outcome:

  * ``won``     — actual delta ≥ 1.5x predicted (over-delivered)
  * ``washed``  — actual within ±50% of predicted (well-calibrated)
  * ``lost``    — actual < 0.5x predicted (over-promised)
  * ``harmed``  — actual moved the metric the wrong way (negative delta)

Each label is persisted on the Task (``details.outcome_label``) and on
the GoalTaskLink (``actual_impact``). When a meaningful pattern emerges
(several harmed proposals from the same service_key, large calibration
drift) a ``learning`` memory entry is written so the next Strategist
run sees it.

Single entry: ``evaluate_workspace_outcomes(workspace_id, db)``. Called
by the daily ``scheduler.tick`` via ``execution_type='outcome_evaluation'``.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.memory.service import record_memory
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.task import Task
from packages.core.models.workspace import Workspace, WorkspaceActivity
from packages.core.models.base import generate_ulid

logger = logging.getLogger(__name__)


# Outcome thresholds (relative to predicted delta).
WIN_RATIO = 1.5
WASH_RATIO_LO = 0.5
WASH_RATIO_HI = 1.5  # symmetric — anything in [0.5x, 1.5x] is "well-calibrated"

# Pattern thresholds — when do we promote to a learning memory.
PATTERN_MIN_SAMPLES = 3   # need at least N proposals to call it a pattern
PATTERN_HARMED_RATE = 0.5 # ≥50% harmed → write a "stop doing this" learning
PATTERN_LOST_RATE = 0.6   # ≥60% lost → write a "this over-promises" learning


# ── Public entry ─────────────────────────────────────────────────────

async def evaluate_workspace_outcomes(
    db: AsyncSession,
    workspace_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Label all unlabeled completed proposals for one workspace.

    Returns a stats dict suitable for the WorkspaceActivity log.
    Caller commits.
    """
    now = now or datetime.now(timezone.utc)

    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        return {"workspace_id": workspace_id, "skipped": True, "reason": "not_found"}
    if workspace.status != "active":
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "reason": f"workspace_{workspace.status}",
        }

    # Pull goals once — keyed by id for outcome_window lookup.
    goals = list((await db.execute(
        select(Goal).where(Goal.workspace_id == workspace_id)
    )).scalars().all())
    goals_by_id = {g.id: g for g in goals}

    # Candidates: tasks that came from a Strategist proposal, are
    # completed, and haven't been labeled yet. Bound at 200 per run so
    # one workspace can't monopolise the daily job.
    candidates = list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.status == "completed",
            Task.details["strategist_review_id"].astext.isnot(None),
            Task.details["outcome_label"].astext.is_(None),
        ).order_by(desc(Task.completed_at)).limit(200)
    )).scalars().all())

    labeled = 0
    skipped_too_recent = 0
    by_label: dict[str, int] = defaultdict(int)
    by_owner_label: dict[tuple[str, str], int] = defaultdict(int)

    for task in candidates:
        details = dict(task.details or {})
        impact = details.get("estimated_impact") or {}
        goal_id = impact.get("goal_id")
        predicted = impact.get("metric_delta")

        # Untracked task (no goal link) — mark as 'untracked' so we
        # don't keep evaluating it.
        if not goal_id or predicted is None:
            details["outcome_label"] = "untracked"
            details["outcome_evaluated_at"] = now.isoformat()
            task.details = details
            labeled += 1
            by_label["untracked"] += 1
            continue

        goal = goals_by_id.get(goal_id)
        if goal is None:
            # Goal vanished (deleted, abandoned). Don't keep retrying.
            details["outcome_label"] = "goal_missing"
            details["outcome_evaluated_at"] = now.isoformat()
            task.details = details
            labeled += 1
            by_label["goal_missing"] += 1
            continue

        window = timedelta(days=int(goal.outcome_window_days or 7))
        completed_at = task.completed_at
        if completed_at is None:
            continue
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)

        if (now - completed_at) < window:
            skipped_too_recent += 1
            continue  # not enough time has passed yet

        actual = await _measure_window_delta(
            db, goal_id, start=completed_at, end=completed_at + window,
        )
        label = _classify_outcome(predicted=float(predicted), actual=actual)

        details["outcome_label"] = label
        details["outcome_evaluated_at"] = now.isoformat()
        details["outcome_actual_delta"] = actual
        details["outcome_window_days"] = int(goal.outcome_window_days or 7)
        task.details = details

        # Mirror onto GoalTaskLink so attribution analytics work without
        # peeking into Task.details.
        await _upsert_link(
            db,
            goal_id=goal_id, task_id=task.id,
            estimated_impact=Decimal(str(predicted)),
            actual_impact=Decimal(str(actual)),
        )

        labeled += 1
        by_label[label] += 1
        owner = task.owner_service_key or "_none_"
        by_owner_label[(owner, label)] += 1

    await db.flush()

    # Pattern detection → learning memory writes.
    learnings_written = await _emit_pattern_learnings(
        db,
        workspace=workspace,
        by_owner_label=by_owner_label,
        now=now,
    )

    # Activity log entry — operator can see the workspace evolving.
    if labeled or learnings_written:
        await _log_activity(
            db,
            workspace=workspace,
            labeled=labeled,
            by_label=dict(by_label),
            learnings_written=learnings_written,
            skipped_too_recent=skipped_too_recent,
        )
        await _record_outcome_runtime_evidence(
            db,
            workspace=workspace,
            labeled=labeled,
            by_label=dict(by_label),
            learnings_written=learnings_written,
            skipped_too_recent=skipped_too_recent,
        )

    return {
        "workspace_id": workspace_id,
        "labeled": labeled,
        "by_label": dict(by_label),
        "skipped_too_recent": skipped_too_recent,
        "learnings_written": learnings_written,
    }


# ── Helpers ──────────────────────────────────────────────────────────

async def _measure_window_delta(
    db: AsyncSession, goal_id: str, *, start: datetime, end: datetime,
) -> float:
    """Goal value at ``end`` minus value at ``start`` (or as close as the
    measurement series gets to those timestamps)."""
    val_start = await _value_at_or_before(db, goal_id, start)
    val_end = await _value_at_or_before(db, goal_id, end)
    if val_start is None or val_end is None:
        return 0.0
    return float(val_end) - float(val_start)


async def _value_at_or_before(
    db: AsyncSession, goal_id: str, ts: datetime,
) -> Optional[Decimal]:
    row = (await db.execute(
        select(GoalMeasurement.value).where(
            GoalMeasurement.goal_id == goal_id,
            GoalMeasurement.measured_at <= ts,
        ).order_by(desc(GoalMeasurement.measured_at)).limit(1)
    )).scalar_one_or_none()
    return row


def _classify_outcome(*, predicted: float, actual: float) -> str:
    if predicted == 0:
        # No prediction to compare to — call it "untracked" rather than
        # forcing a ratio with /0.
        return "untracked"
    if predicted > 0 and actual < 0:
        return "harmed"
    if predicted < 0 and actual > 0:
        # Predicted decline but it grew — also treat as harmed-shape
        # (mis-prediction in the wrong direction).
        return "harmed"
    ratio = actual / predicted if predicted else 0.0
    if ratio >= WIN_RATIO:
        return "won"
    if ratio < WASH_RATIO_LO:
        return "lost"
    return "washed"


async def _upsert_link(
    db: AsyncSession, *, goal_id: str, task_id: str,
    estimated_impact: Decimal, actual_impact: Decimal,
) -> None:
    existing = (await db.execute(
        select(GoalTaskLink).where(
            GoalTaskLink.goal_id == goal_id,
            GoalTaskLink.task_id == task_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.estimated_impact = estimated_impact
        existing.actual_impact = actual_impact
        return
    db.add(GoalTaskLink(
        goal_id=goal_id, task_id=task_id,
        contribution="direct",
        estimated_impact=estimated_impact,
        actual_impact=actual_impact,
    ))


async def _emit_pattern_learnings(
    db: AsyncSession,
    *,
    workspace: Workspace,
    by_owner_label: dict[tuple[str, str], int],
    now: datetime,
) -> int:
    """Look across this run's labels grouped by owner_service_key and
    write a ``learning`` memory whenever a clear pattern emerges.

    Conservative — only fires when ``PATTERN_MIN_SAMPLES`` are present
    AND the bad-outcome rate clears the threshold. Confidence capped at
    0.8 so an autonomously-written learning never out-weighs an
    operator-written one (which can sit at 1.0).
    """
    by_owner: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (owner, label), n in by_owner_label.items():
        by_owner[owner][label] = n

    written = 0
    for owner, counts in by_owner.items():
        if owner == "_none_":
            continue
        total = sum(counts.values())
        if total < PATTERN_MIN_SAMPLES:
            continue

        harmed = counts.get("harmed", 0)
        lost = counts.get("lost", 0)

        if harmed / total >= PATTERN_HARMED_RATE:
            await record_memory(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                scope="learning",
                title=f"Stop proposing {owner} work — last {total} hurt the metric",
                body=(
                    f"Outcome evaluation on {now.date().isoformat()} found "
                    f"{harmed}/{total} recent proposals routed to "
                    f"`{owner}` actually moved the goal *down*. Stop "
                    f"proposing this pattern unless the operator explicitly "
                    f"asks for it.\n\n"
                    f"Counts: {dict(counts)}"
                ),
                tags=["calibration", "auto", owner],
                source=f"outcome_eval:{generate_ulid()}",
                importance=8,
                confidence=0.75,
            )
            written += 1
            continue  # don't double-write the lost pattern below

        if lost / total >= PATTERN_LOST_RATE:
            await record_memory(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                scope="learning",
                title=f"`{owner}` proposals over-predict impact — discount predictions",
                body=(
                    f"Outcome evaluation on {now.date().isoformat()} found "
                    f"{lost}/{total} recent `{owner}` proposals fell short "
                    f"of their predicted metric_delta by 2x or more. The "
                    f"Strategist should be more conservative when "
                    f"estimating impact for this service, or propose "
                    f"smaller-scope work.\n\n"
                    f"Counts: {dict(counts)}"
                ),
                tags=["calibration", "auto", owner],
                source=f"outcome_eval:{generate_ulid()}",
                importance=6,
                confidence=0.7,
            )
            written += 1

    return written


async def _log_activity(
    db: AsyncSession,
    *,
    workspace: Workspace,
    labeled: int,
    by_label: dict[str, int],
    learnings_written: int,
    skipped_too_recent: int,
) -> None:
    db.add(WorkspaceActivity(
        id=generate_ulid(),
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
        event_type="outcome_evaluation",
        summary=_outcome_summary(
            labeled=labeled,
            by_label=by_label,
            learnings_written=learnings_written,
            skipped_too_recent=skipped_too_recent,
        ),
        details={
            "by_label": by_label,
            "learnings_written": learnings_written,
            "skipped_too_recent": skipped_too_recent,
        },
    ))


async def _record_outcome_runtime_evidence(
    db: AsyncSession,
    *,
    workspace: Workspace,
    labeled: int,
    by_label: dict[str, int],
    learnings_written: int,
    skipped_too_recent: int,
) -> None:
    """Mirror outcome evaluation into runtime evidence for future loops."""
    try:
        from packages.core.services.runtime_learning import record_runtime_evidence

        await record_runtime_evidence(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            evidence_type="outcome_evaluation",
            source="strategist",
            status="succeeded",
            summary=_outcome_summary(
                labeled=labeled,
                by_label=by_label,
                learnings_written=learnings_written,
                skipped_too_recent=skipped_too_recent,
            ),
            details={
                "by_label": by_label,
                "learnings_written": learnings_written,
                "skipped_too_recent": skipped_too_recent,
            },
            metrics={
                "labeled": labeled,
                "learnings_written": learnings_written,
                "skipped_too_recent": skipped_too_recent,
            },
        )
    except Exception:
        logger.debug("outcome evaluation runtime evidence skipped", exc_info=True)


def _outcome_summary(
    *,
    labeled: int,
    by_label: dict[str, int],
    learnings_written: int,
    skipped_too_recent: int,
) -> str:
    summary_parts = [f"Labeled {labeled} proposal(s)"]
    if by_label:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_label.items()))
        summary_parts.append(f"({breakdown})")
    if learnings_written:
        summary_parts.append(f"+ {learnings_written} learning memorie(s)")
    if skipped_too_recent:
        summary_parts.append(f"({skipped_too_recent} too recent)")
    return " ".join(summary_parts)
