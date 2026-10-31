"""Migration 20261025_20 closes the retired Agent task runner's in-flight work once."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Conversation, Message, Task, TaskLog
from packages.core.models.task_coordinator import TaskCoordinatorRun

_MARKER_KEY = "_runtime_tool_hitl_v1"


def _migration():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/migrations"))
    return ScriptDirectory.from_config(config).get_revision("20261025_20").module


def _task(db, entity_id, *, status, workspace_id=None, agent_id="agent-1", details=None):
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title="t",
        status=status, agent_id=agent_id, details=details or {},
    )
    db.add(task)
    return task


@pytest.mark.asyncio
async def test_retirement_blocks_the_runners_in_flight_tasks_and_leaves_everything_else(db_session):
    entity_id = generate_ulid()
    conversation = Conversation(id=generate_ulid(), entity_id=entity_id, scope="channel")
    db_session.add(conversation)
    running = _task(db_session, entity_id, status="in_progress")
    waiting = _task(db_session, entity_id, status="waiting_on_customer", details={
        _MARKER_KEY: {"state": "waiting", "hitl_id": "h"},
    })
    resuming = _task(db_session, entity_id, status="pending", details={_MARKER_KEY: {"state": "resume_queued"}})
    # Not the runner's: a fresh start, a manual Task, a Workspace Task, a loop run.
    fresh = _task(db_session, entity_id, status="pending")
    manual = _task(db_session, entity_id, status="in_progress", agent_id=None)
    workspace_task = _task(db_session, entity_id, status="in_progress", workspace_id=generate_ulid())
    loop_task = _task(db_session, entity_id, status="waiting_on_customer")
    db_session.add(TaskCoordinatorRun(
        task_id=loop_task.id, entity_id=entity_id, workspace_id=None, root_subscription_id=None,
        root_agent_id="agent-1", selection_fingerprint="f", lifecycle_generation="g",
        engine="agent_loop", phase="waiting_human",
    ))
    question = HitlRequest(entity_id=entity_id, origin_kind="task", origin_task_id=waiting.id,
                           dedup_key=f"card:{waiting.id}", status="pending")
    loop_question = HitlRequest(entity_id=entity_id, origin_kind="task", origin_task_id=loop_task.id,
                                dedup_key=f"card:{loop_task.id}", status="pending")
    db_session.add_all([question, loop_question])
    await db_session.flush()
    card = Message(conversation_id=conversation.id, role="assistant", content="Which unit?",
                   pending_action={"kind": "human_input", "task_id": waiting.id,
                                   "approval_request_id": question.id})
    loop_card = Message(conversation_id=conversation.id, role="assistant", content="Which date?",
                        pending_action={"kind": "human_input", "task_id": loop_task.id,
                                        "approval_request_id": loop_question.id})
    resume = RuntimeOutboxEvent(
        event_type="packages.core.tasks.ai_tasks.run_agent_task", aggregate_id=resuming.id,
        dedupe_key=f"packages.core.tasks.ai_tasks.run_agent_task:approval:{generate_ulid()}",
        payload={}, available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db_session.add_all([card, loop_card, resume])
    await db_session.flush()

    def migrate(sync_session):
        with Operations.context(MigrationContext.configure(sync_session.connection())):
            _migration().upgrade()

    await db_session.run_sync(migrate)
    rows = (running, waiting, resuming, fresh, manual, workspace_task, loop_task,
            question, loop_question, card, loop_card, resume)
    for row in rows:
        await db_session.refresh(row)

    assert (running.status, waiting.status, resuming.status) == ("blocked", "blocked", "blocked")
    # No recovery may still treat them as parked on the retired runner's approval.
    assert waiting.details[_MARKER_KEY] == {
        "state": "finished", "hitl_id": "h", "result_status": "failed", "failure_reason": "task_runner_retired",
    }
    assert resuming.details[_MARKER_KEY]["state"] == "finished"
    assert _MARKER_KEY not in running.details
    assert question.status == "expired" and card.resolved_at is not None
    assert resume.delivered_at is not None
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == waiting.id))).scalars().all()
    assert [log.meta["error"] for log in logs] == ["runtime_migrated"]
    # A fresh start runs on the loop; the rest were never the runner's.
    assert (fresh.status, manual.status, workspace_task.status, loop_task.status) == (
        "pending", "in_progress", "in_progress", "waiting_on_customer",
    )
    assert loop_question.status == "pending" and loop_card.resolved_at is None

    # Idempotent: nothing is left to close, no second log line.
    await db_session.run_sync(migrate)
    logs = (await db_session.execute(select(TaskLog).where(TaskLog.task_id == waiting.id))).scalars().all()
    assert len(logs) == 1
    await db_session.rollback()
