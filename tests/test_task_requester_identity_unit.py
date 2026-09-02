from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from packages.core.constants.task import TaskLogType
from packages.core.constants.task_actors import TASK_ACTOR_META_KEY, TaskActor
from packages.core.models.task import Task, TaskLog


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_forged_strategist_marker_is_not_system_provenance():
    from packages.core.services.task_requester_identity import (
        _has_trusted_system_task_provenance,
    )

    statements = []

    class FakeDb:
        async def execute(self, statement):
            statements.append(statement)
            return _ScalarResult(None)

    task = SimpleNamespace(
        id="task-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        details={"strategist_review_id": "forged-review"},
    )

    assert not await _has_trusted_system_task_provenance(FakeDb(), task)
    assert len(statements) == 1
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "proposal_items" in compiled
    assert "proposals" in compiled
    assert "task-1" in compiled
    assert "entity-1" in compiled
    assert "workspace-1" in compiled


@pytest.mark.asyncio
async def test_legacy_strategist_task_uses_scoped_work_batch_provenance():
    from packages.core.services.task_requester_identity import (
        _has_trusted_system_task_provenance,
    )

    statements = []
    results = iter([
        None,
        SimpleNamespace(
            task_ids=["task-1", "task-2"],
            details={"strategist_review_id": "legacy-review"},
        ),
    ])

    class FakeDb:
        async def execute(self, statement):
            statements.append(statement)
            return _ScalarResult(next(results))

    task = SimpleNamespace(
        id="task-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        details={
            "strategist_review_id": "legacy-review",
            "workspace_work_batch_id": "batch-1",
        },
    )

    assert await _has_trusted_system_task_provenance(FakeDb(), task)
    assert len(statements) == 2
    compiled = str(statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "workspace_work_batches.id = 'batch-1'" in compiled
    assert "workspace_work_batches.entity_id = 'entity-1'" in compiled
    assert "workspace_work_batches.workspace_id = 'workspace-1'" in compiled
    assert "workspace_work_batches.source_kind = 'strategist_proposal'" in compiled


@pytest.mark.asyncio
async def test_legacy_strategist_work_batch_must_contain_exact_task_and_review():
    from packages.core.services.task_requester_identity import (
        _has_trusted_system_task_provenance,
    )

    results = iter([
        None,
        SimpleNamespace(
            task_ids=["different-task"],
            details={"strategist_review_id": "different-review"},
        ),
    ])

    class FakeDb:
        async def execute(self, _statement):
            return _ScalarResult(next(results))

    task = SimpleNamespace(
        id="task-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        details={
            "strategist_review_id": "legacy-review",
            "workspace_work_batch_id": "batch-1",
        },
    )

    assert not await _has_trusted_system_task_provenance(FakeDb(), task)


@pytest.mark.asyncio
async def test_scheduled_system_provenance_requires_exact_task_link():
    from packages.core.services.task_requester_identity import (
        _has_trusted_system_task_provenance,
    )

    statements = []

    class FakeDb:
        async def execute(self, statement):
            statements.append(statement)
            return _ScalarResult("scheduled-row-id")

    task = SimpleNamespace(
        id="task-1",
        entity_id="entity-1",
        workspace_id=None,
        details={"scheduled_job_id": "scheduler:daily"},
    )

    assert await _has_trusted_system_task_provenance(FakeDb(), task)
    assert len(statements) == 1
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "scheduled_jobs.manor_task_id = 'task-1'" in compiled
    assert "scheduled_jobs.entity_id = 'entity-1'" in compiled
    assert "scheduled_jobs.workspace_id IS NULL" in compiled
    assert "scheduler:daily" in compiled


@pytest.mark.asyncio
async def test_scheduled_system_provenance_accepts_immutable_creation_receipt():
    from packages.core.services.task_requester_identity import (
        _has_trusted_system_task_provenance,
    )

    statements = []

    class FakeDb:
        async def execute(self, statement):
            statements.append(statement)
            return _ScalarResult("task-log-receipt")

    task = SimpleNamespace(
        id="task-older-occurrence",
        entity_id="entity-1",
        workspace_id="workspace-1",
        details={
            "scheduled_job_id": "scheduler:daily",
            "scheduled_run_id": "run-older-occurrence",
        },
    )

    assert await _has_trusted_system_task_provenance(FakeDb(), task)
    assert len(statements) == 1
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "task_logs.task_id = 'task-older-occurrence'" in compiled
    assert "task_logs.log_type = 'create'" in compiled
    assert "actor_kind" in compiled
    assert "entity_id" in compiled
    assert "entity-1" in compiled
    assert "workspace_id" in compiled
    assert "workspace-1" in compiled
    assert "scheduled_job_id" in compiled
    assert "scheduler:daily" in compiled
    assert "scheduled_run_id" in compiled
    assert "run-older-occurrence" in compiled
    assert "scheduled_jobs" not in compiled


@pytest.mark.asyncio
async def test_scheduled_task_keeps_user_owner_with_system_creation_receipt():
    from packages.core.services.task_service import create_task

    class FakeDb:
        def __init__(self):
            self.sync_session = Session()
            self.added = []

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    db = FakeDb()
    try:
        task = await create_task(
            db,
            "entity-1",
            title="Scheduled task",
            creator_id="user-1",
            details={
                "scheduled_job_id": "scheduler:daily",
                "scheduled_run_id": "run-1",
            },
            creation_logged_by_system=True,
            creation_log_metadata={
                "entity_id": "entity-1",
                "workspace_id": None,
                "scheduled_job_id": "scheduler:daily",
                "scheduled_run_id": "run-1",
            },
        )

        creation_log = next(
            value for value in db.added
            if isinstance(value, TaskLog) and value.log_type == TaskLogType.CREATE.value
        )
        assert isinstance(task, Task)
        assert task.creator_id == "user-1"
        assert task.owner_id == "user-1"
        assert creation_log.created_by == "system"
        assert creation_log.meta[TASK_ACTOR_META_KEY] == TaskActor.SYSTEM.value
        assert creation_log.meta["scheduled_job_id"] == "scheduler:daily"
        assert creation_log.meta["scheduled_run_id"] == "run-1"
    finally:
        db.sync_session.close()


def test_scheduler_records_manual_task_provenance_link():
    import inspect

    from packages.core.tasks import scheduler_tasks

    source = inspect.getsource(scheduler_tasks._dispatch_job)
    assert "job.manor_task_id = task.id" in source
    assert '"entity_id": job.entity_id' in source
    assert '"workspace_id": job.workspace_id' in source
    assert '"scheduled_run_id": run_id' in source
    assert "creation_log_metadata=" in source
    assert "creation_logged_by_system=True" in source
