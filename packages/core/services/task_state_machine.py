"""Central task status state machine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from packages.core.constants.task import (
    TASK_STATUSES,
    VALID_STATUSES,
    CancelSource,
    TaskStatus,
)


TERMINAL_STATUSES = {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.FAILED.value}


class TaskStatusTransitionError(ValueError):
    """Raised when code attempts an invalid task status transition."""

    def __init__(self, old_status: str, new_status: str, reason: str):
        self.old_status = old_status
        self.new_status = new_status
        super().__init__(reason)


@dataclass(frozen=True)
class TaskStatusTransition:
    old_status: str
    new_status: str
    is_noop: bool = False


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"proposed", "pending", "scheduled", "in_progress", "cancelled"},
    "proposed": {"pending", "in_progress", "cancelled"},
    "pending": {
        "scheduled",
        "in_progress",
        "waiting_on_customer",
        "on_hold",
        "blocked",
        "completed",
        "cancelled",
        "failed",
    },
    "scheduled": {"pending", "in_progress", "on_hold", "cancelled", "failed"},
    "in_progress": {
        "pending",
        "waiting_on_customer",
        "on_hold",
        "blocked",
        "completed",
        "cancelled",
        "failed",
    },
    "waiting_on_customer": {
        "pending",
        "in_progress",
        "on_hold",
        "blocked",
        "completed",
        "cancelled",
        "failed",
    },
    "on_hold": {"pending", "scheduled", "in_progress", "blocked", "cancelled", "failed"},
    "blocked": {"pending", "scheduled", "in_progress", "waiting_on_customer", "on_hold", "cancelled", "failed"},
    # Explicit reopen/retry paths. Keeping these in the table makes manual
    # recovery intentional rather than an accidental side effect.
    "completed": {"pending", "in_progress", "on_hold", "cancelled"},
    "cancelled": {"pending", "scheduled", "in_progress"},
    "failed": {"pending", "in_progress", "waiting_on_customer", "on_hold", "cancelled", "completed"},
}


def assert_valid_transition(old_status: str, new_status: str) -> TaskStatusTransition:
    """Validate a task status transition and return a normalized record."""
    if old_status not in VALID_STATUSES:
        raise TaskStatusTransitionError(
            old_status,
            new_status,
            f"Unknown current task status: {old_status}",
        )
    if new_status not in VALID_STATUSES:
        raise TaskStatusTransitionError(
            old_status,
            new_status,
            f"Unknown target task status: {new_status}",
        )
    if old_status == new_status:
        return TaskStatusTransition(old_status, new_status, is_noop=True)
    if new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
        raise TaskStatusTransitionError(
            old_status,
            new_status,
            f"Invalid task status transition: {old_status} -> {new_status}",
        )
    return TaskStatusTransition(old_status, new_status)


def allowed_next_statuses(status: str) -> list[str]:
    """Return valid target statuses for a current status, sorted by board order."""
    if status not in VALID_STATUSES:
        raise TaskStatusTransitionError(
            status,
            status,
            f"Unknown current task status: {status}",
        )
    allowed = set(_ALLOWED_TRANSITIONS.get(status, set()))
    allowed.add(status)
    return sorted(allowed, key=lambda item: TASK_STATUSES[item]["order"])


def status_transition_map() -> dict[str, list[str]]:
    """Return the full transition map for API/frontend consumers."""
    return {
        status: allowed_next_statuses(status)
        for status in sorted(VALID_STATUSES, key=lambda item: TASK_STATUSES[item]["order"])
    }


async def apply_task_status_transition(
    task,
    new_status: str,
    *,
    now: datetime | None = None,
    db=None,
    actor_kind: str = "system",
    actor_id: str | None = None,
    config_versions: dict | None = None,
    cancel_source: str | None = None,
) -> TaskStatusTransition:
    """Validate and apply a status transition to a Task-like ORM object.

    This is the unified entry point for every task status change, so it is
    also the wiring point for the workspace event ledger (M1): when the
    caller passes its ``db`` session, an ``execution_*`` event is appended in
    the same transaction for workspace-scoped tasks. The ledger write is
    best-effort — the adapter swallows its own errors and can never make the
    transition fail. ``actor_kind``/``actor_id`` attribute the event when the
    caller knows who acted (defaults to ``system``).

    ``config_versions`` (M11) is the optional agent/skill revision stamp
    for the emitted event — passed by callers that already resolved which
    config produced the run (see ``PlanExecutor._finalize``).

    ``cancel_source`` records who cancelled (``CancelSource``); without it a
    ``user`` actor is a user cancel and anything else the system. Cancelling
    a ``proposed`` Task records ``proposal`` unless the caller explicitly
    passes another source than ``system``.
    """
    old_status = task.status
    transition = assert_valid_transition(old_status, new_status)
    if transition.is_noop:
        return transition

    ts = now or datetime.now(timezone.utc)
    task.status = new_status
    task.status_changed_at = ts
    task.updated_at = ts

    if new_status == "in_progress" and not task.started_at:
        task.started_at = ts

    if new_status in TERMINAL_STATUSES:
        task.completed_at = ts
    elif old_status in TERMINAL_STATUSES:
        task.completed_at = None

    if new_status == TaskStatus.CANCELLED.value:
        # Who ended it. Only a person counts as a user cancel; an agent or the
        # platform is the system. Callers that know better pass it. A
        # proposal withdrawn by anyone but a named person never became
        # work: it is recorded as a proposal cancel, which no outcome counts.
        if old_status == TaskStatus.PROPOSED.value and cancel_source in (
            None, CancelSource.SYSTEM.value,
        ):
            cancel_source = CancelSource.PROPOSAL.value
        task.cancel_source = cancel_source or (
            CancelSource.USER.value if actor_kind == "user" else CancelSource.SYSTEM.value
        )
    elif old_status == TaskStatus.CANCELLED.value:
        task.cancel_source = None

    if db is not None:
        from packages.core.ledger.adapters import record_task_transition

        # Never fatal — see packages/core/ledger/adapters.py.
        await record_task_transition(
            db, task, new_status,
            actor_kind=actor_kind, actor_id=actor_id, occurred_at=ts,
            config_versions=config_versions,
        )
        if new_status in TERMINAL_STATUSES:
            from packages.core.services import scheduled_task_terminal_delivery

            # Same transaction as the status write: a scheduled occurrence is
            # re-delivered to the gateway exactly when its Task becomes
            # terminal, whichever path finalized it. The gateway then settles
            # it with the Task's real result (written after this call).
            await scheduled_task_terminal_delivery.stage_scheduled_task_terminal_delivery(
                db, task, new_status,
            )

    return transition
