"""Central task status state machine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from packages.core.constants.task import TASK_STATUSES, VALID_STATUSES


TERMINAL_STATUSES = {"completed", "cancelled", "failed"}


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
    "failed": {"pending", "in_progress", "waiting_on_customer", "on_hold", "cancelled"},
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


def apply_task_status_transition(task, new_status: str, *, now: datetime | None = None) -> TaskStatusTransition:
    """Validate and apply a status transition to a Task-like ORM object."""
    old_status = task.status
    transition = assert_valid_transition(old_status, new_status)
    if transition.is_noop:
        return transition

    ts = now or datetime.now(timezone.utc)
    task.status = new_status
    task.updated_at = ts

    if new_status == "in_progress" and not task.started_at:
        task.started_at = ts

    if new_status in TERMINAL_STATUSES:
        task.completed_at = ts
    elif old_status in TERMINAL_STATUSES:
        task.completed_at = None

    return transition
