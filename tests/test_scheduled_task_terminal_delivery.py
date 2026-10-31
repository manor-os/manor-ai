"""Task terminal transitions durably stage their scheduled-occurrence delivery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from packages.core.constants.task import TaskStatus
from packages.core.models.runtime_run import RuntimeOutboxEvent, RuntimeRun
from packages.core.services.task_runtime_approvals import TASK_RUNTIME_RESUME_EVENT


def _scheduled_task(status: str = "blocked"):
    return SimpleNamespace(
        id="01TASKSETTLE000000000000001",
        agent_id="01AGENTSETTLE00000000000001",
        status=status,
        details={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )


@pytest.mark.asyncio
async def test_terminal_status_stages_one_gateway_delivery(db_session):
    from packages.core.services.scheduled_task_terminal_delivery import (
        scheduled_task_terminal_dedupe_key,
        stage_scheduled_task_terminal_delivery,
    )

    await db_session.execute(delete(RuntimeOutboxEvent))
    task = _scheduled_task()

    staged = await stage_scheduled_task_terminal_delivery(db_session, task, "cancelled")
    # A second terminal write for the same occurrence must not fail the
    # caller's transaction (unique dedupe_key) nor stage a second event.
    staged_again = await stage_scheduled_task_terminal_delivery(
        db_session, task, "cancelled",
    )
    await db_session.flush()

    rows = (await db_session.execute(select(RuntimeOutboxEvent))).scalars().all()
    assert staged is True
    assert staged_again is False
    assert len(rows) == 1
    event = rows[0]
    assert event.event_type == TASK_RUNTIME_RESUME_EVENT
    assert event.aggregate_id == task.id
    assert event.dedupe_key == scheduled_task_terminal_dedupe_key("run-1", task.id)
    assert event.dedupe_key == f"scheduled-task-terminal:run-1:{task.id}"
    # Exactly the kwargs run_agent_task(...) takes: the dispatcher sends the
    # payload as-is via send_task(event_type, kwargs=payload).
    assert event.payload == {
        "task_id": task.id,
        "agent_id": task.agent_id,
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
        "claim_recheck": True,
    }


@pytest.mark.asyncio
async def test_terminal_status_accepts_task_status_enum(db_session):
    from packages.core.services.scheduled_task_terminal_delivery import (
        stage_scheduled_task_terminal_delivery,
    )

    await db_session.execute(delete(RuntimeOutboxEvent))

    assert await stage_scheduled_task_terminal_delivery(
        db_session, _scheduled_task(), TaskStatus.COMPLETED,
    ) is True
    await db_session.flush()
    assert len((await db_session.execute(select(RuntimeOutboxEvent))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_non_scheduled_or_non_terminal_task_stages_nothing(db_session):
    from packages.core.services.scheduled_task_terminal_delivery import (
        stage_scheduled_task_terminal_delivery,
    )

    await db_session.execute(delete(RuntimeOutboxEvent))
    unscheduled = SimpleNamespace(
        id="01TASKSETTLE000000000000002", status="in_progress", details={},
    )

    assert await stage_scheduled_task_terminal_delivery(
        db_session, unscheduled, "completed",
    ) is False
    assert await stage_scheduled_task_terminal_delivery(
        db_session, _scheduled_task(), "blocked",
    ) is False
    assert await stage_scheduled_task_terminal_delivery(
        db_session, _scheduled_task(), TaskStatus.BLOCKED,
    ) is False
    await db_session.flush()
    assert (await db_session.execute(select(RuntimeOutboxEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_status_transition_stages_delivery_for_terminal_status(monkeypatch):
    from packages.core.services import task_state_machine

    staged = []

    async def fake_stage(db, task, new_status):
        staged.append((task.id, new_status))
        return True

    async def fake_ledger(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.scheduled_task_terminal_delivery."
        "stage_scheduled_task_terminal_delivery",
        fake_stage,
    )
    monkeypatch.setattr(
        "packages.core.ledger.adapters.record_task_transition",
        fake_ledger,
    )
    task = SimpleNamespace(
        id="task-1",
        status="blocked",
        started_at=None,
        completed_at=None,
        status_changed_at=None,
        updated_at=None,
        cancel_source=None,
        details={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )

    await task_state_machine.apply_task_status_transition(task, "cancelled", db=object())

    assert staged == [("task-1", "cancelled")]


@pytest.mark.asyncio
async def test_staged_delivery_is_published_as_the_gateway_envelope(
    db_session, monkeypatch,
):
    from packages.core.services.scheduled_task_terminal_delivery import (
        stage_scheduled_task_terminal_delivery,
    )
    from packages.core.tasks.runtime_tasks import dispatch_runtime_outbox_once

    # The global publisher first recovers expired runs (an earlier test's run
    # gains a terminal notice): reset the queue and its recovery source.
    await db_session.execute(delete(RuntimeOutboxEvent))
    await db_session.execute(delete(RuntimeRun))
    await stage_scheduled_task_terminal_delivery(
        db_session, _scheduled_task(), "completed",
    )
    await db_session.commit()
    sent = []
    monkeypatch.setattr(
        "packages.core.tasks.runtime_tasks.celery_app.send_task",
        lambda name, **kwargs: sent.append((name, kwargs)),
    )

    assert await dispatch_runtime_outbox_once() == 1

    assert sent[0][0] == "packages.core.tasks.ai_tasks.run_agent_task"
    assert sent[0][1]["kwargs"] == {
        "task_id": "01TASKSETTLE000000000000001",
        "agent_id": "01AGENTSETTLE00000000000001",
        "scheduled_run_id": "run-1",
        "scheduled_job_id": "job-1",
        "claim_recheck": True,
    }

    # The envelope must bind to the real gateway signature.
    import inspect

    from packages.core.tasks import ai_tasks

    inspect.signature(ai_tasks.run_agent_task.run).bind(**sent[0][1]["kwargs"])


def test_gateway_settles_a_terminal_task_with_its_real_response(monkeypatch):
    """A re-delivered terminal Task reaches the settlement with `response`.

    The transition cannot carry the result (callers write ``actual_output``
    after it), so the gateway must rebuild it from the Task row.
    """

    from packages.core.tasks import ai_tasks
    import packages.core.database as database

    task = SimpleNamespace(
        id="task-1",
        status="completed",
        actual_output={"response": "Weekly report is ready."},
        workspace_id="ws-1",
        started_at=None,
        completed_at=None,
        details={"scheduled_run_id": "run-1", "scheduled_job_id": "job-1"},
    )
    terminal = ai_tasks._task_terminal_result(task)
    assert terminal["response"] == "Weekly report is ready."

    settled = []

    def claimed(awaitable):
        awaitable.close()
        return True, terminal

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", claimed)
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda _sf, task_id, result, **kwargs: settled.append(
            (task_id, result, kwargs)
        ),
    )

    result = ai_tasks.run_agent_task.run(
        "task-1",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        claim_recheck=True,
    )

    assert result["status"] == "completed"
    assert len(settled) == 1
    assert settled[0][0] == "task-1"
    assert settled[0][1]["response"] == "Weekly report is ready."
    assert settled[0][2]["scheduled_run_id"] == "run-1"
    assert settled[0][2]["scheduled_job_id"] == "job-1"


@pytest.mark.asyncio
async def test_gateway_does_not_settle_a_run_admitted_for_another_task(monkeypatch):
    """A Task pointing at a run admitted for ANOTHER Task must not settle it."""

    from packages.core.constants.execution import (
        ScheduledChildAdmissionStatus,
    )
    from packages.core.tasks import ai_tasks

    async def mismatch(*_args, **_kwargs):
        return SimpleNamespace(
            admitted=False,
            status=ScheduledChildAdmissionStatus.CLOSED,
            reason="scheduled_child_identity_mismatch",
        )

    ran = []

    async def execute(*_args, **_kwargs):
        ran.append(True)
        return True, {"status": "completed"}

    monkeypatch.setattr(ai_tasks, "_admit_scheduled_child", mismatch)
    monkeypatch.setattr(ai_tasks, "_run_agent_task_with_execution_claim", execute)

    class _NoTaskSession:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    @asynccontextmanager
    async def session_factory():
        yield _NoTaskSession()

    claimed, result = await ai_tasks._run_scheduled_agent_task_if_open(
        session_factory,
        "wrong-task",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        claim_recheck=True,
    )

    assert claimed is False
    assert ran == []
    assert result["scheduled_occurrence_closed"] is True
    assert result["reason"] == "scheduled_child_identity_mismatch"
    assert result["child_terminalized"] is False

    # ...and run_agent_task returns that closed result without settling.
    import packages.core.database as database

    def closed(awaitable):
        awaitable.close()
        return False, result

    monkeypatch.setattr(database, "create_worker_session", lambda: object())
    monkeypatch.setattr(ai_tasks, "_run_async", closed)
    monkeypatch.setattr(
        ai_tasks,
        "_update_job_run_status",
        lambda *_args, **_kwargs: pytest.fail(
            "a mismatched child must not settle the occurrence"
        ),
    )
    assert ai_tasks.run_agent_task.run(
        "wrong-task",
        "agent-1",
        scheduled_run_id="run-1",
        scheduled_job_id="job-1",
        claim_recheck=True,
    )["scheduled_occurrence_closed"] is True
