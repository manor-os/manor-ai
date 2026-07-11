from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.services.task_state_machine import (
    TaskStatusTransitionError,
    apply_task_status_transition,
    assert_valid_transition,
    status_transition_map,
)


def _task(status: str):
    return SimpleNamespace(
        status=status,
        started_at=None,
        completed_at=None,
        updated_at=None,
    )


def test_task_state_machine_allows_manual_retry_and_reopen_paths():
    assert assert_valid_transition("failed", "in_progress").new_status == "in_progress"
    assert assert_valid_transition("waiting_on_customer", "pending").new_status == "pending"
    assert assert_valid_transition("completed", "pending").new_status == "pending"


def test_task_state_machine_rejects_unknown_or_invalid_transitions():
    with pytest.raises(TaskStatusTransitionError):
        assert_valid_transition("pending", "not_a_status")

    with pytest.raises(TaskStatusTransitionError):
        assert_valid_transition("completed", "failed")


def test_apply_task_status_transition_updates_lifecycle_timestamps():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    task = _task("pending")

    apply_task_status_transition(task, "in_progress", now=now)
    assert task.status == "in_progress"
    assert task.started_at == now
    assert task.updated_at == now
    assert task.completed_at is None

    done_at = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)
    apply_task_status_transition(task, "completed", now=done_at)
    assert task.status == "completed"
    assert task.completed_at == done_at

    reopened_at = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    apply_task_status_transition(task, "pending", now=reopened_at)
    assert task.status == "pending"
    assert task.completed_at is None
    assert task.updated_at == reopened_at


def test_status_transition_map_includes_noop_and_retry_targets():
    transitions = status_transition_map()

    assert "failed" in transitions["failed"]
    assert "in_progress" in transitions["failed"]
    assert "pending" in transitions["completed"]
