"""Durable operator controls for Workflow runs.

Pause is cooperative: an in-flight node may finish, but the runner must not
start another node after the pause is recorded. Cancellation follows the same
node-boundary rule and is terminal. External side effects that already reached
a provider cannot be rolled back by either control.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from packages.core.constants.workflow import WORKFLOW_RUN_OPEN_STATUSES
from packages.core.models.workflow import WorkflowRun


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


def run_resume_action(run: Any) -> dict[str, Any] | None:
    """Project a native run-page response from the frozen execution contract."""
    if run.status != "paused":
        return None
    from packages.core.services.workflow_run_trace import execution_snapshot_for_run

    snapshot = execution_snapshot_for_run(None, run)
    if snapshot is None:
        return None
    step = next((item for item in snapshot["steps"] if item.get("id") == run.current_step_id), None)
    if not step:
        return None
    action = {
        "kind": "workflow_resume",
        "workflow_run_id": run.id,
        "step_id": run.current_step_id,
        "title": step.get("name") or run.current_step_id,
        "options": ["resume"],
    }
    if is_manually_paused(run):
        return action
    result = (run.step_results or {}).get(run.current_step_id) or {}
    if step.get("type") != "wait" or result.get("resume_strategy") == "blocked":
        return None
    config = step.get("config") or {}
    wait_type = config.get("wait_type", "approval")
    if wait_type == "event":
        return {**action, "prompt": result.get("output")}
    if wait_type == "input":
        input_schema = config.get("input_schema")
        if not isinstance(input_schema, dict):
            return None
        return {
            **action,
            "kind": "workflow_input",
            "options": ["submit"],
            "response_variable": config.get("response_variable") or "inputs",
            "prompt": result.get("output"),
            "input_schema": input_schema,
        }
    if wait_type != "approval":
        return None
    from packages.core.services.workflow_chat_projection import workflow_chat_context

    if workflow_chat_context(run) is not None:
        # Chat-origin approvals must resolve their existing message/card.
        return None
    return {
        **action,
        "kind": "workflow_approval",
        "options": config.get("options") or ["approve", "reject"],
        "response_variable": config.get("response_variable") or "decision",
        "prompt": result.get("output"),
        "review": result.get("review"),
        "review_title": result.get("review_title"),
        "review_read_only": True,
    }


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


async def active_subworkflow_descendant_ids(
    db: Any,
    parent: WorkflowRun,
) -> list[str]:
    """Discover open descendants without waiting on worker-owned row locks."""

    frontier = {str(parent.id)}
    visited = {str(parent.id)}
    descendants: list[str] = []
    workspace_scope = (
        WorkflowRun.workspace_id.is_(None)
        if parent.workspace_id is None
        else WorkflowRun.workspace_id == parent.workspace_id
    )
    while frontier:
        rows = (await db.execute(
            select(WorkflowRun.id, WorkflowRun.trigger_data)
            .where(
                WorkflowRun.entity_id == parent.entity_id,
                workspace_scope,
                WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
                WorkflowRun.trigger_data["parent_run_id"].as_string().in_(frontier),
            )
        )).all()
        next_frontier: set[str] = set()
        for child_id, trigger_data in rows:
            child_id = str(child_id)
            child_trigger = trigger_data if isinstance(trigger_data, dict) else {}
            if (
                child_id in visited
                or str(child_trigger.get("parent_run_id") or "") not in frontier
            ):
                continue
            visited.add(child_id)
            descendants.append(child_id)
            next_frontier.add(child_id)
        frontier = next_frontier
    return descendants


async def cancel_active_subworkflow_descendants(
    db: Any,
    parent: WorkflowRun,
    *,
    actor_id: str,
    at: datetime | None = None,
    descendant_run_ids: list[str] | None = None,
) -> list[WorkflowRun]:
    """Cancel every currently lockable open descendant within parent scope.

    A long-running worker may own a descendant row through an ORM autoflush.
    ``SKIP LOCKED`` keeps operator cancellation responsive; the caller still
    revokes execution claims for the complete discovery set, and the runner
    settles skipped descendants from their cancelled ancestor.
    """
    cancelled_at = at or _now()
    run_ids = list(dict.fromkeys(
        descendant_run_ids
        if descendant_run_ids is not None
        else await active_subworkflow_descendant_ids(db, parent)
    ))
    if not run_ids:
        return []
    workspace_scope = (
        WorkflowRun.workspace_id.is_(None)
        if parent.workspace_id is None
        else WorkflowRun.workspace_id == parent.workspace_id
    )
    children = (await db.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.id.in_(run_ids),
            WorkflowRun.entity_id == parent.entity_id,
            workspace_scope,
            WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
        )
        .with_for_update(skip_locked=True)
    )).scalars().all()
    for child in children:
        cancel_run(child, actor_id=actor_id, at=cancelled_at)
    return list(children)


async def cancel_run_from_cancelled_ancestor(
    db: Any,
    run: WorkflowRun,
) -> bool:
    """Settle an active child when any in-scope ancestor was cancelled."""

    if run.status not in WORKFLOW_RUN_OPEN_STATUSES:
        return False
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    parent_id = str(trigger_data.get("parent_run_id") or "").strip()
    visited = {str(run.id)}
    for _ in range(32):
        if not parent_id or parent_id in visited:
            return False
        visited.add(parent_id)
        row = (await db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.status,
                WorkflowRun.workspace_id,
                WorkflowRun.trigger_data,
            ).where(
                WorkflowRun.id == parent_id,
                WorkflowRun.entity_id == run.entity_id,
            )
        )).one_or_none()
        if row is None or row.workspace_id != run.workspace_id:
            return False
        ancestor_trigger = row.trigger_data if isinstance(row.trigger_data, dict) else {}
        if row.status == "cancelled":
            control = ancestor_trigger.get(CONTROL_KEY)
            control = control if isinstance(control, dict) else {}
            cancelled_at = None
            try:
                cancelled_at = datetime.fromisoformat(
                    str(control.get("cancelled_at") or "")
                )
                if cancelled_at.tzinfo is None:
                    cancelled_at = cancelled_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                cancelled_at = None
            cancel_run(
                run,
                actor_id=str(control.get("cancelled_by") or run.started_by or "system"),
                at=cancelled_at,
            )
            return True
        parent_id = str(ancestor_trigger.get("parent_run_id") or "").strip()
    return False
