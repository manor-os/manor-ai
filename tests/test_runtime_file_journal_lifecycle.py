"""Runtime file journals follow exact hard-purge ownership, never Blueprint portability."""
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace


def journal(entity_id, *, owner=None, state="prepared", path=None):
    row_id = generate_ulid()
    rel_path = path or f"runtime-only/{row_id}.mp4"
    return RuntimeFileCommit(
        id=row_id, transaction_id=generate_ulid(), entity_id=entity_id,
        rel_path=rel_path, path_sha256=hashlib.sha256(rel_path.encode()).hexdigest(),
        state=state, owner=owner or {},
        snapshot={"private_recovery_marker": row_id, "backup_name": f".{row_id}.backup"},
        content_sha256=hashlib.sha256(b"synthetic journal bytes").hexdigest(),
    )


async def remaining_journal_ids(db, *entity_ids):
    return set((await db.scalars(select(RuntimeFileCommit.id).where(
        RuntimeFileCommit.entity_id.in_(entity_ids),
    ))).all())


@pytest.mark.asyncio
async def test_workspace_purge_removes_only_exact_entity_and_explicit_workspace_owner(db_session, tmp_path):
    from packages.core.services.entity_service import purge_workspace

    entity = Entity(id=generate_ulid(), name="Journal lifecycle")
    foreign_entity = Entity(id=generate_ulid(), name="Unrelated entity")
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Purged", deleted_at=datetime.now(timezone.utc))
    peer = Workspace(id=generate_ulid(), entity_id=entity.id, name="Preserved")
    removed = [journal(entity.id, owner={"workspace_id": workspace.id}, state=state)
               for state in ("prepared", "committed", "reconciliation_required")]
    retained = [
        journal(entity.id, owner={"workspace_id": peer.id}),
        journal(foreign_entity.id, owner={"workspace_id": workspace.id}),
        journal(entity.id, path=f"Workspaces/{workspace.id}/unowned.mp4"),
        journal(entity.id, owner={"origin": {"workspace_id": workspace.id}}),
    ]
    db_session.add_all([entity, foreign_entity, workspace, peer, *removed, *retained])
    await db_session.commit()
    artifact = tmp_path / "artifact.mp4"
    backup = tmp_path / ".artifact.backup"
    artifact.write_bytes(b"artifact remains for existing purge owner")
    backup.write_bytes(b"backup remains for existing purge owner")

    assert await purge_workspace(db_session, workspace.id)
    await db_session.commit()

    assert await remaining_journal_ids(db_session, entity.id, foreign_entity.id) == {row.id for row in retained}
    assert await db_session.get(Workspace, workspace.id) is None
    assert await db_session.get(Workspace, peer.id) is not None
    assert artifact.read_bytes() == b"artifact remains for existing purge owner"
    assert backup.read_bytes() == b"backup remains for existing purge owner"


@pytest.mark.asyncio
async def test_workspace_soft_delete_and_restore_keep_recovery_state(db_session, monkeypatch):
    from packages.core.services.entity_service import restore_workspace, soft_delete_workspace

    entity = Entity(id=generate_ulid(), name="Restorable journal")
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Restorable")
    row = journal(entity.id, owner={"workspace_id": workspace.id})
    db_session.add_all([entity, workspace, row])
    await db_session.commit()
    monkeypatch.setattr("packages.core.services.plan_gate.enforce_workspace_capacity", AsyncMock())
    monkeypatch.setattr("packages.core.services.workspace_runtime.sync_workspace_runtime_schedules", AsyncMock())

    assert await soft_delete_workspace(db_session, workspace.id, entity.id)
    await db_session.commit()
    assert await remaining_journal_ids(db_session, entity.id) == {row.id}
    assert await restore_workspace(db_session, workspace.id, entity.id) is not None
    await db_session.commit()
    assert await remaining_journal_ids(db_session, entity.id) == {row.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("last_user", [False, True])
async def test_user_purge_removes_entity_journals_only_with_actual_entity_delete(db_session, last_user):
    from packages.core.services.user_lifecycle import purge_user

    deleted_at = datetime.now(timezone.utc)
    entity = Entity(id=generate_ulid(), name="User journal purge", deleted_at=deleted_at)
    foreign_entity = Entity(id=generate_ulid(), name="Other entity")
    user = User(id=generate_ulid(), entity_id=entity.id, email=f"{generate_ulid()}@test.invalid",
                password_hash="unused", role="owner", status="active", deleted_at=deleted_at)
    owned = journal(entity.id)
    foreign = journal(foreign_entity.id)
    db_session.add_all([entity, foreign_entity, user, owned, foreign])
    if not last_user:
        db_session.add(User(id=generate_ulid(), entity_id=entity.id, email=f"{generate_ulid()}@test.invalid",
                            password_hash="unused", role="owner", status="active"))
    await db_session.commit()

    assert await purge_user(db_session, user.id)
    await db_session.commit()

    assert await remaining_journal_ids(db_session, entity.id, foreign_entity.id) == ({foreign.id} if last_user else {owned.id, foreign.id})
    assert (await db_session.get(Entity, entity.id) is None) is last_user
    assert await db_session.get(Entity, foreign_entity.id) is not None


@pytest.mark.asyncio
async def test_blueprint_export_does_not_include_runtime_file_journals(db_session):
    from packages.core.blueprints.exporter import export_workspace

    from packages.core.governance.approval_policy_service import (
        account_defaults, initialize_workspace_approval,
    )
    entity = Entity(id=generate_ulid(), name="Portable config",
        settings={"approval_defaults": account_defaults()})
    workspace = Workspace(id=generate_ulid(), entity_id=entity.id, name="Portable workspace",
                          operating_model={}, settings={})
    db_session.add_all([entity, workspace])
    await db_session.flush()
    await initialize_workspace_approval(db_session, workspace)
    await db_session.commit()
    before = await export_workspace(db_session, workspace.id, title="Portable config")
    row = journal(entity.id, owner={"workspace_id": workspace.id})
    db_session.add(row)
    await db_session.commit()

    after = await export_workspace(db_session, workspace.id, title="Portable config")

    assert after == before
    encoded = json.dumps(after)
    for runtime_value in ("runtime_file_commits", "private_recovery_marker", row.id, row.transaction_id, row.rel_path):
        assert runtime_value not in encoded
    assert await remaining_journal_ids(db_session, entity.id) == {row.id}
