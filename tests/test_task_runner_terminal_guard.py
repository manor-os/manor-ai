"""Task-run finalization must tolerate an already-terminal task.

When the agent runner finishes and tries to mark the task ``completed``, a
concurrent failure path may have already marked it ``failed`` (or vice
versa). The opposite terminal transition is intentionally rejected by the
state machine — but the runner must not let that raise, because it would
make the Celery task retry a run that has actually finished, forever.
"""

from __future__ import annotations

import asyncio

import packages.core.ai.task_runner as task_runner
from packages.core.services.task_state_machine import TaskStatusTransitionError


def test_finalize_persists_output_without_status_when_already_terminal(monkeypatch):
    calls = []

    async def fake_update_task(db, task_id, entity_id, **fields):
        calls.append(fields)
        if calls and len(calls) == 1 and fields.get("status") is not None:
            raise TaskStatusTransitionError("failed", "completed", "boom")
        return "TASK"

    monkeypatch.setattr(task_runner, "update_task", fake_update_task)

    result = asyncio.run(
        task_runner._finalize_with_terminal_guard(
            None,
            "t1",
            "e1",
            status="completed",
            actual_output="out",
            details={"a": 1},
        )
    )

    assert result == "TASK"
    assert len(calls) == 2
    # First attempt carries the status; the retry omits it but keeps output.
    assert calls[0].get("status") == "completed"
    assert "status" not in calls[1]
    assert calls[1].get("actual_output") == "out"
    assert calls[1].get("details") == {"a": 1}


def test_finalize_passthrough_on_normal_success(monkeypatch):
    async def fake_update_task(db, task_id, entity_id, **fields):
        return "OK"

    monkeypatch.setattr(task_runner, "update_task", fake_update_task)

    result = asyncio.run(
        task_runner._finalize_with_terminal_guard(
            None,
            "t1",
            "e1",
            status="completed",
            actual_output="out",
            details={},
        )
    )
    assert result == "OK"
