"""Durable operator controls for Workflow runs.

Pause is cooperative: an in-flight node may finish, but the runner must not
start another node after the pause is recorded. Cancellation follows the same
node-boundary rule and is terminal. External side effects that already reached
a provider cannot be rolled back by either control.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CONTROL_KEY = "_workflow_run_control"
MANUAL_PAUSE_STATE = "manual_paused"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def control_state(run: Any) -> str | None:
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    control = trigger_data.get(CONTROL_KEY)
    if not isinstance(control, dict):
        return None
    state = str(control.get("state") or "").strip()
    return state or None


def is_manually_paused(run: Any) -> bool:
    if run.status != "paused":
        return False
    if control_state(run) == MANUAL_PAUSE_STATE:
        return True
    # Execution tracing also updates trigger_data. If a Pause request arrives
    # during a node, that trace write can race with the audit marker even though
    # the independently stored run status remains paused. Native wait/HITL
    # pauses always leave the current node result itself in ``paused``; a
    # manual node-boundary pause does not.
    current_result = (run.step_results or {}).get(run.current_step_id)
    current_result = current_result if isinstance(current_result, dict) else {}
    return str(current_result.get("status") or "").lower() != "paused"


def pause_run(run: Any, *, actor_id: str, at: datetime | None = None) -> None:
    """Persist a cooperative manual pause request on a pending/running run."""
    if run.status not in {"pending", "running"}:
        raise ValueError(f"Run is not active (status: {run.status})")
    paused_at = at or _now()
    trigger_data = dict(run.trigger_data or {})
    prior = trigger_data.get(CONTROL_KEY)
    control = dict(prior) if isinstance(prior, dict) else {}
    control.update({
        "state": MANUAL_PAUSE_STATE,
        "paused_at": paused_at.isoformat(),
        "paused_by": actor_id,
    })
    control.pop("resumed_at", None)
    control.pop("resumed_by", None)
    control.pop("cancelled_at", None)
    control.pop("cancelled_by", None)
    trigger_data[CONTROL_KEY] = control
    run.trigger_data = trigger_data
    run.status = "paused"
    run.completed_at = None


def resume_manual_run(run: Any, *, actor_id: str, at: datetime | None = None) -> bool:
    """Clear a manual pause without mutating a wait/HITL node result."""
    if not is_manually_paused(run):
        return False
    resumed_at = at or _now()
    trigger_data = dict(run.trigger_data or {})
    control = dict(trigger_data.get(CONTROL_KEY) or {})
    control.update({
        "state": "resumed",
        "resumed_at": resumed_at.isoformat(),
        "resumed_by": actor_id,
    })
    trigger_data[CONTROL_KEY] = control
    run.trigger_data = trigger_data
    run.status = "running"
    run.error = None
    run.completed_at = None
    return True


def cancel_run(
    run: Any,
    *,
    actor_id: str,
    at: datetime | None = None,
    allow_problem_terminal: bool = False,
) -> None:
    """Mark a cancellable run cancelled and retain an audit marker."""
    if run.status == "cancelled" or (
        run.status in {"completed", "failed"}
        and not allow_problem_terminal
    ):
        raise ValueError(f"Run already {run.status}")
    cancelled_at = at or _now()
    trigger_data = dict(run.trigger_data or {})
    prior = trigger_data.get(CONTROL_KEY)
    control = dict(prior) if isinstance(prior, dict) else {}
    control.update({
        "state": "cancelled",
        "cancelled_at": cancelled_at.isoformat(),
        "cancelled_by": actor_id,
    })
    trigger_data[CONTROL_KEY] = control
    run.trigger_data = trigger_data
    run.status = "cancelled"
    run.completed_at = cancelled_at
