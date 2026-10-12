"""Artifact projection must remain inside the lease completion transaction."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.constants.execution import WorkLeaseStatus
from packages.core.dispatcher.service import Dispatcher, LeaseNotActive
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import WorkLease, Worker
from packages.core.models.workspace import Workspace
from packages.core.services import artifact_knowledge


@pytest.fixture(autouse=True)
def canonical_entity_fs_root(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")


async def _seed_artifact_lease(db: AsyncSession, tmp_path, *, name: str) -> dict[str, str]:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    now = datetime.now(timezone.utc)
    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    (entity_root / name).write_text("completed artifact\n", encoding="utf-8")

    db.add_all(
        [
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Artifact transaction",
                status="active",
                operating_model={},
                settings={},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title="Materialize one artifact",
                status="in_progress",
                details={},
                delegate_service_keys=[],
            ),
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Artifact worker",
                capabilities={"supported_kinds": ["action"]},
                preferences={},
                status="active",
                consecutive_failures=0,
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                step_key="write_artifact",
                kind="action",
                params={"prompt": "write"},
                depends_on=[],
                step_status="running",
                risk_level="low",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
                started_at=now,
            ),
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                worker_id=worker_id,
                status=WorkLeaseStatus.ACTIVE.value,
                lease_until=now + timedelta(minutes=5),
                credential_leases=[],
            ),
        ]
    )
    await db.commit()
    return {
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "step_id": step_id,
        "lease_id": lease_id,
        "name": name,
    }


@pytest.mark.asyncio
async def test_artifact_projection_forwards_the_callers_session(monkeypatch, tmp_path):
    entity_id = "ent_projection_session"
    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    (entity_root / "report.md").write_text("done\n", encoding="utf-8")
    caller_db = object()
    seen: dict[str, object] = {}

    async def sync_file(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    monkeypatch.setattr(artifact_knowledge, "sync_file_to_knowledge", sync_file)
    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id=entity_id,
        refs=[{"type": "file", "fs_path": "report.md"}],
        workspace_id="ws_1",
        db=caller_db,  # type: ignore[arg-type]
    )

    assert projection.failures == []
    assert projection.refs[0]["document_id"] == "doc_1"
    assert seen["db"] is caller_db


@pytest.mark.asyncio
async def test_complete_local_artifact_reuses_locked_dispatcher_session(
    db_session,
    monkeypatch,
    tmp_path,
):
    """A nested session would wait on the Workspace lock held by completion."""

    seeded = await _seed_artifact_lease(db_session, tmp_path, name="report.md")
    nested_sessions = 0
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def low_lock_timeout_session():
        nonlocal nested_sessions
        nested_sessions += 1
        async with session_factory() as nested_db:
            await nested_db.execute(text("SET LOCAL lock_timeout = '250ms'"))
            yield nested_db

    # Pin the historical failure mode to a short timeout: if artifact
    # projection opens a second session, it will fail while trying to lock the
    # Workspace row already owned by complete_lease's transaction.
    import packages.core.database as database_module
    from packages.core.services import knowledge_sync

    monkeypatch.setattr(database_module, "async_session", low_lock_timeout_session)
    monkeypatch.setattr(knowledge_sync, "async_session", low_lock_timeout_session)
    await db_session.execute(text("SET LOCAL lock_timeout = '250ms'"))

    lease = await asyncio.wait_for(
        Dispatcher().complete_lease(
            db_session,
            seeded["lease_id"],
            result={"generated": True, "fs_path": seeded["name"]},
        ),
        timeout=2,
    )
    assert lease.status == WorkLeaseStatus.COMPLETED.value
    # Check before commit: durable chat/event fan-out is intentionally queued
    # after the caller commits and owns a fresh delivery session.  The
    # invariant here is that completion/artifact projection itself never opens
    # a competing session while the dispatcher transaction owns row locks.
    assert nested_sessions == 0
    await db_session.commit()

    documents = list(
        (
            await db_session.execute(
                select(Document).where(Document.entity_id == seeded["entity_id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(documents) == 1
    assert documents[0].fs_path == seeded["name"]
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step is not None
    assert step.step_status == "done"
    assert step.result["document_id"] == documents[0].id


@pytest.mark.asyncio
async def test_concurrent_completion_of_one_artifact_creates_one_document(
    db_session,
    tmp_path,
):
    seeded = await _seed_artifact_lease(db_session, tmp_path, name="concurrent.md")
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    start = asyncio.Event()

    async def complete_once() -> str:
        async with session_factory() as db:
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            await start.wait()
            try:
                await Dispatcher().complete_lease(
                    db,
                    seeded["lease_id"],
                    result={"generated": True, "fs_path": seeded["name"]},
                )
                await db.commit()
                return "completed"
            except LeaseNotActive:
                await db.rollback()
                return "stale"

    first = asyncio.create_task(complete_once())
    second = asyncio.create_task(complete_once())
    await asyncio.sleep(0)
    start.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second),
        timeout=5,
    )

    assert sorted(outcomes) == ["completed", "stale"]
    db_session.expire_all()
    assert await db_session.scalar(
        select(func.count(Document.id)).where(
            Document.entity_id == seeded["entity_id"],
            Document.fs_path == seeded["name"],
        )
    ) == 1
    step = await db_session.get(ExecutionStep, seeded["step_id"])
    assert step is not None
    assert step.step_status == "done"
