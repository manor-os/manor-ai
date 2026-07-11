from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from packages.core.services.workspace_work_reconciliation import (
    build_work_batch_reconciliation,
    stale_reconciliation_results,
)


NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def _batch(*task_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="batch_1",
        task_ids=list(task_ids),
        source_kind="strategist_proposal",
        summary="Strategist task wave",
    )


def _task(task_id: str, status: str, *, age_hours: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        title=f"Task {task_id}",
        status=status,
        owner_service_key="content",
        updated_at=NOW - timedelta(hours=age_hours),
        started_at=None,
        created_at=NOW - timedelta(hours=age_hours + 1),
    )


def test_fresh_active_batch_is_not_stale() -> None:
    result = build_work_batch_reconciliation(
        _batch("task_1"),
        [_task("task_1", "in_progress", age_hours=2)],
        now=NOW,
    )

    assert result["status"] == "active"
    assert result["open_task_ids"] == ["task_1"]
    assert result["stale_task_ids"] == []
    assert result["stale"] is False


def test_blocked_task_becomes_stale_after_threshold() -> None:
    result = build_work_batch_reconciliation(
        _batch("task_1"),
        [_task("task_1", "blocked", age_hours=7)],
        now=NOW,
    )

    assert result["status"] == "stalled"
    assert result["stale_task_ids"] == ["task_1"]
    assert result["stale_tasks"][0]["status"] == "blocked"
    assert result["stale_tasks"][0]["stale_after_hours"] == 6.0


def test_missing_task_row_stalls_batch() -> None:
    result = build_work_batch_reconciliation(
        _batch("missing_task"),
        [],
        now=NOW,
    )

    assert result["status"] == "stalled"
    assert result["missing_task_ids"] == ["missing_task"]
    assert result["stale"] is True


def test_all_terminal_tasks_complete_batch_snapshot() -> None:
    result = build_work_batch_reconciliation(
        _batch("task_1", "task_2"),
        [
            _task("task_1", "completed", age_hours=30),
            _task("task_2", "failed", age_hours=30),
        ],
        now=NOW,
    )

    assert result["status"] == "completed"
    assert result["all_terminal"] is True
    assert result["open_task_ids"] == []
    assert result["terminal_count"] == 2


def test_empty_batch_completes_snapshot() -> None:
    result = build_work_batch_reconciliation(
        _batch(),
        [],
        now=NOW,
    )

    assert result["status"] == "completed"
    assert result["all_terminal"] is True
    assert result["total_count"] == 0


def test_stale_reconciliation_results_filters_only_stale() -> None:
    stale = {"batch_id": "batch_1", "stale": True}
    fresh = {"batch_id": "batch_2", "stale": False}

    assert stale_reconciliation_results([stale, fresh]) == [stale]
