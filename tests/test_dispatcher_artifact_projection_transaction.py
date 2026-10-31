"""Artifact projection must remain inside the lease completion transaction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import WorkLeaseStatus
from packages.core.models.base import generate_ulid
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
