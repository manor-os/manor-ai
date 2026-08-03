"""Experiment lifecycle controller (M13).

``start_experiment`` freezes a deterministic baseline from the ledger and
hangs an ``_experiment_overlay`` on the target's config JSONB — WITHOUT
``bump_revision``: an experiment is not a formal change, only a promoted
``automation_change`` proposal ever bumps the revision. The scheduler's
``_dispatch_job`` merges the overlay patch per run via
``effective_dispatch_config`` (only while the experiment is still
``running``) and stamps the run's dispatch event with the
``xp:{experiment_id}:{period_key}`` correlation so the cohort is
recoverable from the ledger alone.

``check_experiment_guardrails`` (Celery beat ``experiments.guardrail_tick``)
stops running experiments on consecutive cohort failures, ``max_runs``, or
``ends_at``; ``evaluate_experiment`` then compares the pre-declared
``success_metrics`` against the frozen baseline — pure arithmetic, no LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ledger import event_types as et
from packages.core.models.experiment import Experiment
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace_event import WorkspaceEvent

logger = logging.getLogger(__name__)

EXPERIMENT_OVERLAY_KEY = "_experiment_overlay"
DEFAULT_DURATION_DAYS = 7
DEFAULT_ROLLBACK_ON_CONSECUTIVE_FAILURES = 2
BASELINE_RUN_SAMPLE = 10

# v1 metric vocabulary the deterministic evaluator understands. Anything
# else declared in success_metrics is recorded as {"status": "unsupported"}.
SUPPORTED_METRICS = ("success_rate", "run_count")

_RUN_COMPLETED_EVENTS = (et.AUTOMATION_RUN_COMPLETED, et.WORKFLOW_RUN_COMPLETED)
_RUN_FAILED_EVENTS = (et.AUTOMATION_RUN_FAILED, et.WORKFLOW_RUN_FAILED)
_RUN_FINAL_EVENTS = _RUN_COMPLETED_EVENTS + _RUN_FAILED_EVENTS

# scope.target_kind → (ORM model, name of the JSONB config field carrying
# the overlay). ScheduledJob dispatch reads execution_target; a
# WorkflowBinding's ``config`` holds context overrides (``variables`` is
# merged verbatim into run variables, so the overlay marker must not live
# there).
_TARGET_SPECS: dict[str, tuple[type, str]] = {
    "scheduled_job": (ScheduledJob, "execution_target"),
    "workflow_binding": (WorkflowBinding, "config"),
}


class ExperimentError(Exception):
    """Invalid experiment lifecycle transition or malformed scope."""


class ExperimentTargetError(ExperimentError):
    """The experiment's target row is missing / out of scope (mirrors the
    StaleRevisionError pattern: named fields + a rendered message)."""

    def __init__(self, *, target_kind: str, target_id: str, reason: str):
        self.target_kind = target_kind
        self.target_id = target_id
        self.reason = reason
        super().__init__(f"{target_kind} {target_id}: {reason}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _scope(experiment: Experiment) -> tuple[str, str]:
    scope = experiment.scope if isinstance(experiment.scope, dict) else {}
    target_kind = str(scope.get("target_kind") or "")
    target_id = str(scope.get("target_id") or "")
    if target_kind not in _TARGET_SPECS:
        raise ExperimentError(
            f"experiment {experiment.id}: unsupported target_kind "
            f"{target_kind!r}; must be one of {sorted(_TARGET_SPECS)}"
        )
    if not target_id:
        raise ExperimentError(f"experiment {experiment.id}: scope.target_id missing")
    return target_kind, target_id


async def _load_target(
    db: AsyncSession, experiment: Experiment, *, required: bool,
) -> tuple[Optional[Any], str, str]:
    """Load + validate the target row. ``required=False`` tolerates a
    deleted target (stop path must always succeed)."""
    target_kind, target_id = _scope(experiment)
    model, _ = _TARGET_SPECS[target_kind]
    row = await db.get(model, target_id)
    if row is None:
        if required:
            raise ExperimentTargetError(
                target_kind=target_kind, target_id=target_id, reason="not found",
            )
        return None, target_kind, target_id
    row_workspace = getattr(row, "workspace_id", None)
    if row_workspace and row_workspace != experiment.workspace_id:
        if required:
            raise ExperimentTargetError(
                target_kind=target_kind,
                target_id=target_id,
                reason=f"belongs to workspace {row_workspace}, "
                       f"not {experiment.workspace_id}",
            )
        return None, target_kind, target_id
    return row, target_kind, target_id


def _config_field(target_kind: str) -> str:
    return _TARGET_SPECS[target_kind][1]


# ── ledger ─────────────────────────────────────────────────────────


async def _record_experiment_event(
    db: AsyncSession,
    experiment: Experiment,
    event_type: str,
    *,
    suffix: str,
    payload: Optional[dict] = None,
    causation_id: Optional[str] = None,
) -> None:
    """Best-effort experiment_* ledger fact (adapter discipline: never fatal)."""
    from packages.core.ledger.service import record_event

    try:
        await record_event(
            db,
            entity_id=experiment.entity_id,
            workspace_id=experiment.workspace_id,
            event_type=event_type,
            source_kind="experiment",
            source_id=experiment.id,
            status=experiment.status,
            causation_id=causation_id,
            payload=payload,
            idempotency_key=f"xp:{experiment.id}:{suffix}",
        )
    except Exception:  # noqa: BLE001 — ledger writes must never break lifecycle
        logger.warning(
            "experiment ledger write failed (ignored): %s %s",
            experiment.id, event_type, exc_info=True,
        )


# ── cohort queries (correlation prefix xp:{id}:) ───────────────────


async def _cohort_dispatch_events(
    db: AsyncSession, experiment: Experiment,
) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == experiment.workspace_id,
            WorkspaceEvent.event_type == et.AUTOMATION_RUN_DISPATCHED,
            WorkspaceEvent.correlation_id.like(f"xp:{experiment.id}:%"),
        ).order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


async def _cohort_final_events(
    db: AsyncSession, experiment: Experiment, run_ids: list[str],
) -> dict[str, WorkspaceEvent]:
    """Final run outcome per cohort run_id (last final event wins)."""
    if not run_ids:
        return {}
    rows = list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == experiment.workspace_id,
            WorkspaceEvent.event_type.in_(_RUN_FINAL_EVENTS),
            WorkspaceEvent.run_id.in_(run_ids),
        ).order_by(WorkspaceEvent.id.asc())
    )).scalars().all())
    final: dict[str, WorkspaceEvent] = {}
    for event in rows:
        if event.run_id:
            final[event.run_id] = event
    return final


async def _cohort_stats(db: AsyncSession, experiment: Experiment) -> dict:
    dispatched = await _cohort_dispatch_events(db, experiment)
    run_ids = [e.run_id for e in dispatched if e.run_id]
    final = await _cohort_final_events(db, experiment, run_ids)
    ordered_outcomes = [
        final[run_id].event_type for run_id in run_ids if run_id in final
    ]
    completed = sum(1 for t in ordered_outcomes if t in _RUN_COMPLETED_EVENTS)
    failed = len(ordered_outcomes) - completed
    trailing_failures = 0
    for event_type in reversed(ordered_outcomes):
        if event_type in _RUN_FAILED_EVENTS:
            trailing_failures += 1
        else:
            break
    cost_values = [
        (event.payload or {}).get("cost")
        for event in [*dispatched, *final.values()]
    ]
    costs = [float(v) for v in cost_values if isinstance(v, (int, float))]
    return {
        "run_count": len(run_ids),
        "finished_count": len(ordered_outcomes),
        "completed": completed,
        "failed": failed,
        "success_rate": (
            completed / len(ordered_outcomes) if ordered_outcomes else None
        ),
        "trailing_consecutive_failures": trailing_failures,
        "cost": sum(costs) if costs else None,
    }


# ── baseline ───────────────────────────────────────────────────────


async def _freeze_baseline(
    db: AsyncSession, experiment: Experiment, target_kind: str, target_id: str,
) -> dict:
    """Deterministic aggregate of the target's last N run outcomes from the
    ledger (success_rate, avg_duration_ms when present, run_count)."""
    source_kind = "scheduled_job" if target_kind == "scheduled_job" else "workflow"
    rows = list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == experiment.workspace_id,
            WorkspaceEvent.source_kind == source_kind,
            WorkspaceEvent.source_id == target_id,
            WorkspaceEvent.event_type.in_(_RUN_FINAL_EVENTS),
        ).order_by(WorkspaceEvent.id.desc()).limit(BASELINE_RUN_SAMPLE)
    )).scalars().all())
    completed = sum(1 for e in rows if e.event_type in _RUN_COMPLETED_EVENTS)
    durations = [
        float((e.payload or {}).get("duration_ms"))
        for e in rows
        if isinstance((e.payload or {}).get("duration_ms"), (int, float))
    ]
    return {
        "run_count": len(rows),
        "success_rate": (completed / len(rows)) if rows else None,
        "avg_duration_ms": (sum(durations) / len(durations)) if durations else None,
        "sampled_at": _utcnow().isoformat(),
        "sample_limit": BASELINE_RUN_SAMPLE,
    }


# ── lifecycle ──────────────────────────────────────────────────────


async def start_experiment(db: AsyncSession, experiment: Experiment) -> Experiment:
    """pending → running: freeze baseline, hang the overlay, emit the fact."""
    if experiment.status != "pending":
        raise ExperimentError(
            f"experiment {experiment.id} cannot start from status "
            f"{experiment.status!r} (must be 'pending')"
        )
    target, target_kind, target_id = await _load_target(db, experiment, required=True)

    experiment.baseline_snapshot = await _freeze_baseline(
        db, experiment, target_kind, target_id,
    )

    # Apply the overlay WITHOUT bump_revision — an experiment is not a
    # formal change (M13): the overlay is a temporary, reversible layer on
    # top of the current config; only a promoted automation_change proposal
    # bumps the revision (and that goes through the M10 apply CAS).
    field = _config_field(target_kind)
    config = dict(getattr(target, field) or {})
    config[EXPERIMENT_OVERLAY_KEY] = {
        "experiment_id": experiment.id,
        "patch": dict(experiment.overlay_patch or {}),
    }
    setattr(target, field, config)

    now = _utcnow()
    scope = experiment.scope if isinstance(experiment.scope, dict) else {}
    try:
        duration_days = int(scope.get("duration_days") or DEFAULT_DURATION_DAYS)
    except (TypeError, ValueError):
        duration_days = DEFAULT_DURATION_DAYS
    experiment.status = "running"
    experiment.started_at = now
    experiment.ends_at = now + timedelta(days=duration_days)
    await db.flush()

    await _record_experiment_event(
        db, experiment, et.EXPERIMENT_STARTED,
        suffix="started",
        causation_id=experiment.proposal_item_id,
        payload={
            "target_kind": target_kind,
            "target_id": target_id,
            "hypothesis": (experiment.hypothesis or "")[:500],
            "ends_at": experiment.ends_at.isoformat(),
            "max_runs": scope.get("max_runs"),
        },
    )
    return experiment


async def stop_experiment(
    db: AsyncSession,
    experiment: Experiment,
    *,
    outcome: str,
    reason: Optional[str] = None,
) -> Experiment:
    """running → completed|stopped_guardrail. Idempotent: any other current
    status is a no-op. Always removes the overlay (tolerating a deleted
    target) so the target is restored on every stop path."""
    if outcome not in ("completed", "stopped_guardrail"):
        raise ValueError(
            f"outcome must be completed|stopped_guardrail, got {outcome!r}"
        )
    if experiment.status != "running":
        return experiment

    target, target_kind, _target_id = await _load_target(
        db, experiment, required=False,
    )
    if target is not None:
        field = _config_field(target_kind)
        config = dict(getattr(target, field) or {})
        overlay = config.get(EXPERIMENT_OVERLAY_KEY)
        # Only remove OUR overlay — a newer experiment may already own the slot.
        if isinstance(overlay, dict) and overlay.get("experiment_id") == experiment.id:
            config.pop(EXPERIMENT_OVERLAY_KEY, None)
            setattr(target, field, config)

    experiment.status = outcome
    await db.flush()

    event_type = (
        et.EXPERIMENT_GUARDRAIL_TRIGGERED
        if outcome == "stopped_guardrail"
        else et.EXPERIMENT_COMPLETED
    )
    suffix = "guardrail" if outcome == "stopped_guardrail" else "completed"
    await _record_experiment_event(
        db, experiment, event_type,
        suffix=suffix,
        causation_id=experiment.proposal_item_id,
        payload={"reason": reason} if reason else None,
    )
    return experiment


# ── dispatch-time overlay merge ────────────────────────────────────


async def effective_dispatch_config(
    db: AsyncSession, execution_target: Optional[dict],
) -> tuple[dict, Optional[str], Optional[dict]]:
    """Resolve the per-run effective config for a dispatch.

    Returns ``(config, experiment_id, patch)``. The overlay marker itself is
    always stripped from the returned config; the patch is shallow-merged
    over it ONLY while the owning experiment is still ``running`` (a stale
    overlay — stopped/evaluated/deleted experiment — is ignored, so a beat
    race between guardrail-stop and dispatch can never run a dead patch).
    """
    config = dict(execution_target or {})
    overlay = config.pop(EXPERIMENT_OVERLAY_KEY, None)
    if not isinstance(overlay, dict):
        return config, None, None
    experiment_id = overlay.get("experiment_id")
    patch = overlay.get("patch")
    if not experiment_id or not isinstance(patch, dict) or not patch:
        return config, None, None
    experiment = await db.get(Experiment, experiment_id)
    if experiment is None or experiment.status != "running":
        return config, None, None
    return {**config, **patch}, str(experiment_id), dict(patch)


# ── guardrail monitor (beat) ───────────────────────────────────────


async def check_experiment_guardrails(db: AsyncSession) -> list[dict]:
    """One guardrail sweep over all running experiments.

    Stop rules (in priority order):
      * trailing consecutive cohort failures ≥
        ``guardrails.rollback_on_consecutive_failures`` (default 2)
        → ``stopped_guardrail``
      * cohort run count ≥ ``scope.max_runs`` → ``completed``
      * now ≥ ``ends_at`` → ``completed``

    Freshly stopped experiments are auto-evaluated in the same pass.
    Returns a summary row per experiment acted on.
    """
    now = _utcnow()
    running = list((await db.execute(
        select(Experiment).where(Experiment.status == "running")
        .order_by(Experiment.id.asc())
    )).scalars().all())

    results: list[dict] = []
    for experiment in running:
        try:
            stats = await _cohort_stats(db, experiment)
            guardrails = (
                experiment.guardrails
                if isinstance(experiment.guardrails, dict) else {}
            )
            scope = experiment.scope if isinstance(experiment.scope, dict) else {}
            try:
                failure_threshold = int(
                    guardrails.get("rollback_on_consecutive_failures")
                    or DEFAULT_ROLLBACK_ON_CONSECUTIVE_FAILURES
                )
            except (TypeError, ValueError):
                failure_threshold = DEFAULT_ROLLBACK_ON_CONSECUTIVE_FAILURES
            max_runs = scope.get("max_runs")

            outcome = None
            reason = None
            if stats["trailing_consecutive_failures"] >= failure_threshold:
                outcome, reason = "stopped_guardrail", (
                    f"{stats['trailing_consecutive_failures']} consecutive "
                    f"cohort failure(s) >= threshold {failure_threshold}"
                )
            elif isinstance(max_runs, int) and max_runs > 0 and stats["run_count"] >= max_runs:
                outcome, reason = "completed", f"max_runs {max_runs} reached"
            elif experiment.ends_at is not None and now >= experiment.ends_at:
                outcome, reason = "completed", "duration elapsed"

            if outcome is None:
                continue
            await stop_experiment(db, experiment, outcome=outcome, reason=reason)
            await evaluate_experiment(db, experiment)
            results.append({
                "experiment_id": experiment.id,
                "outcome": outcome,
                "reason": reason,
                "run_count": stats["run_count"],
            })
        except Exception:  # noqa: BLE001 — one bad experiment must not kill the sweep
            logger.exception(
                "experiment guardrail check failed for %s", experiment.id,
            )
    return results


# ── deterministic evaluation ───────────────────────────────────────


async def evaluate_experiment(db: AsyncSession, experiment: Experiment) -> Experiment:
    """stopped_guardrail|completed → evaluated.

    Pure arithmetic over the ledger cohort vs the frozen baseline; only
    pre-declared success_metrics are compared (v1 vocabulary: success_rate,
    run_count — anything else is recorded as unsupported, never guessed).
    """
    if experiment.status not in ("stopped_guardrail", "completed"):
        raise ExperimentError(
            f"experiment {experiment.id} cannot be evaluated from status "
            f"{experiment.status!r} (must be stopped_guardrail|completed)"
        )

    stats = await _cohort_stats(db, experiment)
    baseline = (
        experiment.baseline_snapshot
        if isinstance(experiment.baseline_snapshot, dict) else {}
    )
    declared = (
        experiment.success_metrics
        if isinstance(experiment.success_metrics, dict) else {}
    )

    metrics: dict[str, dict] = {}
    met_count = 0
    evaluable = 0
    for name, spec in declared.items():
        spec = spec if isinstance(spec, dict) else {}
        if name not in SUPPORTED_METRICS:
            metrics[name] = {"status": "unsupported"}
            continue
        target_value = spec.get("target")
        baseline_value = baseline.get(name, spec.get("baseline"))
        cohort_value = stats.get(name)
        met: Optional[bool] = None
        if cohort_value is not None and isinstance(target_value, (int, float)):
            met = bool(float(cohort_value) >= float(target_value))
            evaluable += 1
            if met:
                met_count += 1
        metrics[name] = {
            "baseline": baseline_value,
            "cohort": cohort_value,
            "target": target_value,
            "met": met,
        }

    notes: list[str] = []
    if stats["cost"] is None:
        notes.append("no cost data present on cohort events")
    guardrail_violations = experiment.status == "stopped_guardrail"

    now = _utcnow()
    experiment.evaluation = {
        "metrics": metrics,
        "cohort": {
            "run_count": stats["run_count"],
            "finished_count": stats["finished_count"],
            "completed": stats["completed"],
            "failed": stats["failed"],
            "success_rate": stats["success_rate"],
        },
        "guardrail_violations": guardrail_violations,
        "cost": stats["cost"],
        "notes": notes,
        "evaluated_at": now.isoformat(),
    }
    experiment.status = "evaluated"
    experiment.evaluated_at = now
    await db.flush()

    await _record_experiment_event(
        db, experiment, et.EXPERIMENT_EVALUATED,
        suffix="evaluated",
        causation_id=experiment.proposal_item_id,
        payload={
            "metrics_met": met_count,
            "metrics_evaluable": evaluable,
            "metrics_declared": len(declared),
            "guardrail_violations": guardrail_violations,
            "run_count": stats["run_count"],
            "success_rate": stats["success_rate"],
            "cost": stats["cost"],
        },
    )
    return experiment
