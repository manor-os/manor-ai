"""Durable file recovery under crashes, collisions and lost commit ACKs."""
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.models.base import generate_ulid
from packages.core.services.entity_fs import (
    open_entity_file_snapshot, stage_entity_file_rollback_snapshot, write_entity_file_atomic,
)
from packages.core.services.runtime_file_journal import RuntimeFileJournal, recover_runtime_file_commits


@pytest.fixture
def storage(monkeypatch, tmp_path):
    from packages.core.config import get_settings
    from packages.core.ai.runtime import file_actions
    from packages.core.services import tool_cache_version

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(file_actions, "runtime_trigger_document_embeddings", lambda _: None)
    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", AsyncMock())
    root = tmp_path / generate_ulid()
    root.mkdir()
    return root


async def staged(storage, *, existing=True, installed=True):
    if existing:
        (storage / "report.md").write_bytes(b"old")
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    write_entity_file_atomic(storage.name, "report.md", b"new", expected_source_version=snapshot.source_version)
    if installed:
        with open_entity_file_snapshot(storage.name, "report.md") as current:
            await journal.installed("report.md", current.version)
    return journal, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_crash_after_install_restores_only_exact_version(db_session, storage, existing):
    await staged(storage, existing=existing)
    report = await recover_runtime_file_commits(storage.name)
    assert report == {"recovered": 1, "unresolved": 0}
    if existing:
        assert (storage / "report.md").read_bytes() == b"old"
    else:
        assert not (storage / "report.md").exists()
    assert await recover_runtime_file_commits(storage.name) == {"recovered": 0, "unresolved": 0}


@pytest.mark.asyncio
async def test_missing_install_receipt_does_not_guess_ownership_from_same_hash(db_session, storage):
    await staged(storage, installed=False)
    assert (await recover_runtime_file_commits(storage.name))["unresolved"] == 1
    assert (storage / "report.md").read_bytes() == b"new"
    row = (await db_session.execute(select(RuntimeFileCommit).where(RuntimeFileCommit.entity_id == storage.name))).scalar_one()
    assert row.state == "reconciliation_required"
    assert row.snapshot["backup_name"]
    second = RuntimeFileJournal(storage.name)
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    try:
        with pytest.raises(ValueError, match="unresolved earlier commit"):
            await second.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    finally:
        snapshot.discard_unwritten()


@pytest.mark.asyncio
async def test_recovery_does_not_overwrite_later_user_change(db_session, storage):
    await staged(storage)
    write_entity_file_atomic(storage.name, "report.md", b"later user edit")
    assert (await recover_runtime_file_commits(storage.name))["unresolved"] == 1
    assert (storage / "report.md").read_bytes() == b"later user edit"


@pytest.mark.asyncio
async def test_committed_marker_only_cleans_backup(db_session, storage):
    journal, snapshot = await staged(storage)
    await journal.mark_committed(db_session, {"report.md": "document_receipt"})
    await db_session.commit()
    assert (await recover_runtime_file_commits(storage.name))["recovered"] == 1
    assert (storage / "report.md").read_bytes() == b"new"
    assert not (storage / snapshot.backup_rel_path).exists()
    assert await journal.committed()


@pytest.mark.asyncio
async def test_prepared_but_not_written_is_safe_to_discard(db_session, storage):
    (storage / "report.md").write_bytes(b"old")
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    assert (await recover_runtime_file_commits(storage.name))["recovered"] == 1
    assert (storage / "report.md").read_bytes() == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_lost_database_commit_ack_preserves_committed_file(db_session, storage, monkeypatch, rollback_fails):
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction

    async def project(**kwargs):
        # The transaction's journal marker is what recovery reads; projection
        # behavior and real Document visibility are covered in the merge suite.
        return SimpleNamespace(synced=True, document_id="document_receipt", content_changed=False)

    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_sync_entity_file_to_knowledge", project)
    async with RuntimeFileProjectionTransaction(storage.name) as transaction:
        path = await transaction.write_bytes("report.md", b"new", expected_content_sha256=hashlib.sha256(b"new").hexdigest())
        await transaction.project_file(abs_path=path)
        original_commit = transaction.db.commit
        calls = 0

        async def lost_ack():
            nonlocal calls
            await original_commit()
            calls += 1
            if calls == 1:
                raise OSError("connection lost after commit")

        monkeypatch.setattr(transaction.db, "commit", lost_ack)
        if rollback_fails:
            monkeypatch.setattr(transaction.db, "rollback", AsyncMock(side_effect=OSError("connection also lost on rollback")))
        await transaction.commit()
    assert (storage / "report.md").read_bytes() == b"new"
    row = (await db_session.execute(select(RuntimeFileCommit).where(RuntimeFileCommit.entity_id == storage.name))).scalar_one()
    assert row.state == "committed"
    assert not row.cleanup_pending


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_file_and_releases_path(db_session, storage, monkeypatch):
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction, RuntimeFileProjectionError

    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_sync_entity_file_to_knowledge", AsyncMock(return_value=SimpleNamespace(synced=False, document_id=None, reason="injected")))
    with pytest.raises(RuntimeFileProjectionError):
        async with RuntimeFileProjectionTransaction(storage.name) as transaction:
            path = await transaction.write_bytes("report.md", b"new", expected_content_sha256=hashlib.sha256(b"new").hexdigest())
            await transaction.project_file(abs_path=path)
    assert not (storage / "report.md").exists()
    row = (await db_session.execute(select(RuntimeFileCommit).where(RuntimeFileCommit.entity_id == storage.name))).scalar_one()
    assert row.state == "rolled_back"


@pytest.mark.asyncio
async def test_nested_lock_is_reentrant_but_child_task_cannot_inherit_ownership(storage):
    from packages.core.ai.runtime.file_actions import runtime_entity_filesystem_mutation_lock

    entered = asyncio.Event()

    async def child():
        async with runtime_entity_filesystem_mutation_lock(str(storage)):
            entered.set()

    async with runtime_entity_filesystem_mutation_lock(str(storage)):
        async with runtime_entity_filesystem_mutation_lock(str(storage)):
            waiting = asyncio.create_task(child())
            await asyncio.sleep(.03)
            assert not entered.is_set()
    await asyncio.wait_for(waiting, 2)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_post_write_validation_failure_never_rolls_back_a_later_file(db_session, storage, monkeypatch):
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction

    def replace_before_validation(self, **kwargs):
        write_entity_file_atomic(storage.name, "report.md", b"later user edit")
        raise OSError("post-write validation failed")

    monkeypatch.setattr(RuntimeFileProjectionTransaction, "_record_written_file", replace_before_validation)
    with pytest.raises(RuntimeError, match="could not restore prior state"):
        async with RuntimeFileProjectionTransaction(storage.name) as transaction:
            await transaction.write_bytes("report.md", b"new", expected_content_sha256=hashlib.sha256(b"new").hexdigest())
    assert (storage / "report.md").read_bytes() == b"later user edit"
    assert (await recover_runtime_file_commits(storage.name))["unresolved"] == 1
    assert (storage / "report.md").read_bytes() == b"later user edit"


@pytest.mark.asyncio
async def test_cancellation_after_install_uses_atomic_ownership_receipt(db_session, storage, monkeypatch):
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction

    (storage / "report.md").write_bytes(b"old")
    async with RuntimeFileProjectionTransaction(storage.name) as transaction:
        monkeypatch.setattr(transaction._journal, "installed", AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await transaction.write_bytes("report.md", b"new", expected_content_sha256=hashlib.sha256(b"new").hexdigest())
    assert (storage / "report.md").read_bytes() == b"old"


@pytest.mark.asyncio
async def test_nested_projection_cannot_recover_live_outer_write(db_session, storage):
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction

    async with RuntimeFileProjectionTransaction(storage.name) as outer:
        await outer.write_bytes("report.md", b"new", expected_content_sha256=hashlib.sha256(b"new").hexdigest())
        with pytest.raises(RuntimeError, match="Nested file projection"):
            async with RuntimeFileProjectionTransaction(storage.name):
                pytest.fail("nested transaction must not recover an active outer transaction")
        assert (storage / "report.md").read_bytes() == b"new"
    assert not (storage / "report.md").exists()
