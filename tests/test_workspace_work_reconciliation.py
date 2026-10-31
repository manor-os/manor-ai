from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from packages.core.ai.runtime.strategist import _format_work_batch_reconciliation
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

def test_in_progress_without_live_plan_is_immediately_stale() -> None:
    result = build_work_batch_reconciliation(
        _batch("task_1"),
        [_task("task_1", "in_progress", age_hours=0.1)],
        now=NOW,
        live_plan_task_ids=set(),
    )

    assert result["status"] == "stalled"
    assert result["stale"] is True
    assert result["stale_task_ids"] == ["task_1"]
    assert result["stale_tasks"][0]["stale_after_hours"] == 0.0


def test_in_progress_with_live_plan_is_not_immediately_stale() -> None:
    result = build_work_batch_reconciliation(
        _batch("task_1"),
        [_task("task_1", "in_progress", age_hours=0.1)],
        now=NOW,
        live_plan_task_ids={"task_1"},
    )

    assert result["status"] == "active"
    assert result["stale"] is False
    assert result["stale_task_ids"] == []


def test_stale_tasks_waiting_on_a_person_are_not_rendered_as_agent_recovery_work() -> None:
    result = build_work_batch_reconciliation(
        _batch("review_wait", "approval_wait", "stuck"),
        [
            _task("review_wait", "waiting_on_customer", age_hours=30),
            _task("approval_wait", "blocked", age_hours=7),
            _task("stuck", "blocked", age_hours=7),
        ],
        now=NOW,
        human_request_task_ids={"approval_wait"},
    )

    waiting = {row["task_id"]: row["waiting_on_human"] for row in result["stale_tasks"]}
    assert waiting == {"review_wait": True, "approval_wait": True, "stuck": False}
    rendered = _format_work_batch_reconciliation(SimpleNamespace(work_batch_reconciliation=[result]))
    assert "Task review_wait task_id=review_wait owner=content age_hours=30.0 waiting_on=person" in rendered
    assert "task_id=stuck owner=content age_hours=7.0\n" in rendered + "\n"


async def test_an_in_progress_task_whose_agent_loop_ended_is_stale_at_once(db_session) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.models.workspace import WorkspaceWorkBatch
    from packages.core.services.workspace_work_reconciliation import reconcile_active_work_batches

    workspace = SimpleNamespace(id=generate_ulid(), entity_id=generate_ulid())
    now = datetime.now(timezone.utc)
    tasks = {}
    for name, phase in (("ended", "failed"), ("running", "running"), ("unstarted", None)):
        task = Task(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
                    title=name, status="in_progress")
        db_session.add(task)
        if phase:
            db_session.add(TaskCoordinatorRun(
                task_id=task.id, entity_id=workspace.entity_id, workspace_id=workspace.id,
                root_subscription_id="sub", root_agent_id="agent", selection_fingerprint="f",
                lifecycle_generation="g", engine="agent_loop", phase=phase,
            ))
        tasks[name] = task.id
    db_session.add(WorkspaceWorkBatch(workspace_id=workspace.id, entity_id=workspace.entity_id,
                                      task_ids=list(tasks.values())))
    await db_session.flush()

    (snapshot,) = await reconcile_active_work_batches(db_session, workspace, now=now)

    assert [item["task_id"] for item in snapshot["stale_tasks"]] == [tasks["ended"]]
