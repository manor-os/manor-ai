"""Real PostgreSQL recovery boundaries; all files are synthetic and temporary."""
import hashlib
import os
import stat

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_file_commit import RuntimeFileCommit
from packages.core.services.entity_fs import (
    EntityFileRollbackSnapshot,
    open_entity_file_snapshot,
    stage_entity_file_rollback_snapshot,
    write_entity_file_atomic,
)
from packages.core.services.runtime_file_journal import RuntimeFileJournal, recover_runtime_file_commits


@pytest.fixture
def storage(db_session, monkeypatch, tmp_path):
    from packages.core import database
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    root = tmp_path / generate_ulid()
    root.mkdir()
    return root


def fail_first_commit(monkeypatch, *, after_commit=False):
    """Lose either the transaction or only its ACK; leave later sessions real."""
    from packages.core import database

    factory = database.async_session
    calls = 0

    def session():
        db = factory()
        commit = db.commit

        async def interrupted_commit():
            nonlocal calls
            calls += 1
            if calls == 1:
                if after_commit:
                    await commit()
                raise OSError("injected commit interruption")
            await commit()

        db.commit = interrupted_commit
        return db

    monkeypatch.setattr(database, "async_session", session)


async def stage(storage, *, existing=True, install=True):
    if existing:
        (storage / "report.md").write_bytes(b"old")
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    if install:
        write_entity_file_atomic(storage.name, "report.md", b"new", expected_source_version=snapshot.source_version)
        with open_entity_file_snapshot(storage.name, "report.md") as current:
            await journal.installed("report.md", current.version)
    return journal, snapshot


async def recover(storage):
    from packages.core.ai.runtime.file_actions import runtime_entity_filesystem_mutation_lock

    async with runtime_entity_filesystem_mutation_lock(str(storage)):
        return await recover_runtime_file_commits(storage.name)


async def journal_row(db_session, storage):
    db_session.expire_all()
    return (await db_session.execute(select(RuntimeFileCommit).where(
        RuntimeFileCommit.entity_id == storage.name,
    ))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_prepare_lost_ack_confirms_exact_row_without_retrying_insert(db_session, storage, monkeypatch, existing):
    fail_first_commit(monkeypatch, after_commit=True)
    journal, snapshot = await stage(storage, existing=existing)
    row = await journal_row(db_session, storage)
    assert journal.entries == {"report.md": row.id}
    assert row.transaction_id == journal.transaction_id
    assert row.path_sha256 == hashlib.sha256(b"report.md").hexdigest()
    assert row.written_version is not None
    assert await recover(storage) == {"recovered": 1, "unresolved": 0}
    if existing:
        assert (storage / "report.md").read_bytes() == b"old"
        assert not (storage / snapshot.backup_rel_path).exists()
    else:
        assert not (storage / "report.md").exists()


@pytest.mark.asyncio
async def test_prepare_failure_before_commit_does_not_claim_a_durable_entry(db_session, storage, monkeypatch):
    (storage / "report.md").write_bytes(b"old")
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    fail_first_commit(monkeypatch)
    with pytest.raises(OSError, match="commit interruption"):
        await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    assert journal.entries == {}
    assert (await db_session.execute(select(RuntimeFileCommit).where(
        RuntimeFileCommit.entity_id == storage.name,
    ))).scalars().all() == []
    snapshot.discard_unwritten()
    assert (storage / "report.md").read_bytes() == b"old"


@pytest.mark.asyncio
async def test_uncertain_prepare_retains_identity_when_confirmation_is_unavailable(db_session, storage, monkeypatch):
    from packages.core import database

    (storage / "report.md").write_bytes(b"old")
    snapshot = stage_entity_file_rollback_snapshot(storage.name, "report.md")
    journal = RuntimeFileJournal(storage.name)
    fail_first_commit(monkeypatch, after_commit=True)
    factory = database.async_session
    sessions = 0

    def unavailable_confirmation():
        nonlocal sessions
        sessions += 1
        db = factory()
        if sessions == 2:
            async def unavailable(*args, **kwargs):
                raise OSError("confirmation unavailable")
            db.execute = unavailable
        return db

    monkeypatch.setattr(database, "async_session", unavailable_confirmation)
    with pytest.raises(OSError, match="confirmation unavailable"):
        await journal.prepare(snapshot, hashlib.sha256(b"new").hexdigest())
    row = await journal_row(db_session, storage)
    assert journal.entries == {"report.md": row.id}
    # The caller installed nothing and can discard staging. Losing the source
    # hard-link changes ctime, but later recovery still has exact source proof.
    snapshot.discard_unwritten()
    assert await recover(storage) == {"recovered": 1, "unresolved": 0}
    assert (storage / "report.md").read_bytes() == b"old"


@pytest.mark.asyncio
async def test_atomic_writer_rollback_allows_cleaning_exact_unchanged_source_backup(db_session, storage, monkeypatch):
    _journal, snapshot = await stage(storage, install=False)
    fsync = os.fsync
    interrupted = False

    def interrupt_install_directory_sync(fd):
        nonlocal interrupted
        if not interrupted and stat.S_ISDIR(os.fstat(fd).st_mode):
            interrupted = True
            raise OSError("injected installation directory sync failure")
        return fsync(fd)

    # The atomic writer installs bytes, then rolls itself back before returning
    # an installation receipt. Its internal hard-link changes the old ctime.
    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", interrupt_install_directory_sync)
        with pytest.raises(OSError, match="directory sync failure"):
            write_entity_file_atomic(storage.name, "report.md", b"new", expected_source_version=snapshot.source_version)
    assert interrupted
    path = storage / "report.md"
    assert path.read_bytes() == b"old"
    assert snapshot.source_version.same_identity(path.stat())
    assert not snapshot.source_version.matches(path.stat())
    assert (storage / snapshot.backup_rel_path).exists()
    assert (await journal_row(db_session, storage)).written_version is None
    assert await recover(storage) == {"recovered": 1, "unresolved": 0}
    assert path.read_bytes() == b"old"
    assert not (storage / snapshot.backup_rel_path).exists()
    assert (await journal_row(db_session, storage)).state == "rolled_back"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing,install", [(False, True), (True, True), (True, False)])
async def test_recovery_retries_after_filesystem_rollback_before_database_commit(db_session, storage, monkeypatch, existing, install):
    await stage(storage, existing=existing, install=install)
    fail_first_commit(monkeypatch)
    with pytest.raises(OSError, match="commit interruption"):
        await recover(storage)
    row = await journal_row(db_session, storage)
    assert row.state == "prepared"
    assert row.cleanup_pending
    if existing:
        assert (storage / "report.md").read_bytes() == b"old"
    else:
        assert not (storage / "report.md").exists()
    assert await recover(storage) == {"recovered": 1, "unresolved": 0}
    row = await journal_row(db_session, storage)
    assert row.state == "rolled_back"
    assert not row.cleanup_pending
    assert await recover(storage) == {"recovered": 0, "unresolved": 0}


@pytest.mark.asyncio
async def test_recovery_lost_commit_ack_does_not_repeat_rollback(db_session, storage, monkeypatch):
    await stage(storage)
    fail_first_commit(monkeypatch, after_commit=True)
    with pytest.raises(OSError, match="commit interruption"):
        await recover(storage)
    assert (storage / "report.md").read_bytes() == b"old"
    assert (await journal_row(db_session, storage)).state == "rolled_back"
    assert await recover(storage) == {"recovered": 0, "unresolved": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_inode", [False, True])
async def test_interrupted_recovery_does_not_claim_later_user_bytes(db_session, storage, monkeypatch, replace_inode):
    await stage(storage)
    fail_first_commit(monkeypatch)
    with pytest.raises(OSError, match="commit interruption"):
        await recover(storage)
    path = storage / "report.md"
    original = path.stat()
    if replace_inode:
        # Identical bytes and timestamps do not establish inode ownership.
        write_entity_file_atomic(storage.name, "report.md", b"old")
    else:
        # Even the same inode, size and restored mtime require original bytes.
        path.write_bytes(b"bad")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    version = path.stat()
    content = path.read_bytes()
    assert await recover(storage) == {"recovered": 0, "unresolved": 1}
    assert path.read_bytes() == content
    assert path.stat().st_ino == version.st_ino
    row = await journal_row(db_session, storage)
    assert row.state == "reconciliation_required"
    assert not row.cleanup_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("after_cleanup", [False, True])
async def test_committed_cleanup_failure_remains_pending_and_retries(db_session, storage, monkeypatch, after_cleanup):
    journal, snapshot = await stage(storage)
    await journal.mark_committed(db_session, {"report.md": "document_receipt"})
    await db_session.commit()
    commit = EntityFileRollbackSnapshot.commit
    calls = 0

    def interrupted_cleanup(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            if after_cleanup:
                commit(self)
            raise OSError("injected cleanup interruption")
        commit(self)

    monkeypatch.setattr(EntityFileRollbackSnapshot, "commit", interrupted_cleanup)
    assert await recover(storage) == {"recovered": 0, "unresolved": 1}
    row = await journal_row(db_session, storage)
    assert row.state == "committed"
    assert row.cleanup_pending
    assert "cleanup interruption" in row.error
    assert (storage / "report.md").read_bytes() == b"new"
    assert await recover(storage) == {"recovered": 1, "unresolved": 0}
    row = await journal_row(db_session, storage)
    assert row.state == "committed"
    assert not row.cleanup_pending
    assert row.error is None
    assert not (storage / snapshot.backup_rel_path).exists()
    assert (storage / "report.md").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_committed_cleanup_never_deletes_a_replaced_backup(db_session, storage):
    journal, snapshot = await stage(storage)
    await journal.mark_committed(db_session, {"report.md": "document_receipt"})
    await db_session.commit()
    # Same path and bytes are not the retained inode owned by this operation.
    write_entity_file_atomic(storage.name, snapshot.backup_rel_path, b"old")
    backup = storage / snapshot.backup_rel_path
    inode = backup.stat().st_ino
    for _ in range(2):
        assert await recover(storage) == {"recovered": 0, "unresolved": 1}
        assert backup.read_bytes() == b"old"
        assert backup.stat().st_ino == inode
        assert (storage / "report.md").read_bytes() == b"new"
        row = await journal_row(db_session, storage)
        assert row.state == "committed"
        assert row.cleanup_pending
