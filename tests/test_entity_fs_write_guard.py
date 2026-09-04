from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.tools.generate_file.tool import _generate_file_handler as _generate_file

from packages.core.ai.runtime.generated_files import runtime_generate_document_file
from packages.core.config import get_settings
from packages.core.services.entity_fs import (
    claim_entity_editor_write_intent,
    copy_entity_file_atomic,
    EditorWriteIntentStatus,
    EntityFileWriteError,
    EntityFilesystemError,
    mark_entity_editor_write_intent_committed,
    mark_entity_editor_write_intent_digest_committed,
    resolve_path,
    write_entity_file_atomic,
)


@pytest.fixture
def fs_settings(tmp_path):
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"
    try:
        yield settings
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


def test_write_entity_file_atomic_persists_verified_bytes(fs_settings):
    path = write_entity_file_atomic(
        "entity_1",
        "videos/out.mp4",
        b"video-bytes",
        expected_size=len(b"video-bytes"),
    )

    assert path.endswith(os.path.join("entity_1", "videos", "out.mp4"))
    assert open(path, "rb").read() == b"video-bytes"


def test_editor_write_fence_allows_only_identical_same_sequence_retries(tmp_path):
    entity_root = str(tmp_path / "entity_1")

    first = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        "newest",
    )
    assert first.status is EditorWriteIntentStatus.CLAIMED
    pending_retry = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        "newest",
    )
    assert pending_retry.status is EditorWriteIntentStatus.CLAIMED
    mark_entity_editor_write_intent_committed(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        "newest",
        receipt={"document_id": "doc_1"},
    )
    replay = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        "newest",
    )
    assert replay.status is EditorWriteIntentStatus.REPLAYED
    assert replay.receipt == {"document_id": "doc_1"}
    different = claim_entity_editor_write_intent(
        entity_root,
        "/site/app.ts",
        "editor-session",
        2,
        "different",
    )
    assert different.status is EditorWriteIntentStatus.STALE
    older = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        1,
        "older",
    )
    assert older.status is EditorWriteIntentStatus.STALE


def test_editor_write_fence_can_commit_a_precomputed_digest(tmp_path):
    entity_root = str(tmp_path / "entity_1")
    content = b"large-content-placeholder"
    claim = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        content,
    )
    assert claim.status is EditorWriteIntentStatus.CLAIMED

    mark_entity_editor_write_intent_digest_committed(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        hashlib.sha256(content).hexdigest(),
        receipt={"document_id": "doc_1"},
    )

    replay = claim_entity_editor_write_intent(
        entity_root,
        "site/app.ts",
        "editor-session",
        2,
        content,
    )
    assert replay.status is EditorWriteIntentStatus.REPLAYED
    assert replay.receipt == {"document_id": "doc_1"}


@pytest.mark.asyncio
async def test_filesystem_mutation_finishes_after_request_cancellation():
    from apps.api.routers import filesystem

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def paired_mutation():
        started.set()
        await release.wait()
        finished.set()
        return "consistent"

    request_task = asyncio.create_task(
        filesystem._finish_filesystem_mutation(paired_mutation()),
    )
    await started.wait()
    request_task.cancel()
    await asyncio.sleep(0)

    assert not request_task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_filesystem_mutation_finishes_after_repeated_request_cancellation():
    from apps.api.routers import filesystem

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def paired_mutation():
        started.set()
        await release.wait()
        finished.set()
        return "consistent"

    request_task = asyncio.create_task(
        filesystem._finish_filesystem_mutation(paired_mutation()),
    )
    await started.wait()
    request_task.cancel()
    await asyncio.sleep(0)
    request_task.cancel()
    await asyncio.sleep(0)

    assert not request_task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_filesystem_mutation_releases_completed_result_after_cancellation():
    from packages.core.services.entity_fs import finish_entity_filesystem_mutation

    started = asyncio.Event()
    release = asyncio.Event()
    result = SimpleNamespace(closed=False)

    async def paired_mutation():
        started.set()
        await release.wait()
        return result

    request_task = asyncio.create_task(
        finish_entity_filesystem_mutation(
            paired_mutation(),
            release_result=lambda completed: setattr(completed, "closed", True),
        ),
    )
    await started.wait()
    request_task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert result.closed is True


@pytest.mark.asyncio
async def test_runtime_projection_commit_finishes_after_request_cancellation(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.file_actions import (
        RuntimeFileProjectionTransactionFactory,
    )
    from packages.core.services import knowledge_sync, workspace_artifact_purge

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old")

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    class FakeDB:
        committed = False
        rolled_back = False

        async def commit(self):
            commit_started.set()
            await release_commit.wait()
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
        )

    async def persist_cleanup_intents(*_args, **_kwargs):
        return set()

    async def clear_cleanup_intents(*_args, **_kwargs):
        return None

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(
        knowledge_sync,
        "sync_file_to_knowledge",
        successful_sync,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        persist_cleanup_intents,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "clear_artifact_cleanup_jobs",
        clear_cleanup_intents,
    )

    async def commit_projection():
        content = b"new"
        digest = hashlib.sha256(content).hexdigest()
        async with RuntimeFileProjectionTransactionFactory.create(
            "entity_1",
        ) as transaction:
            written = transaction.write_bytes(
                "report.md",
                content,
                expected_content_sha256=digest,
                expected_size=len(content),
            )
            await transaction.project_file(
                abs_path=written,
                entity_root=entity_root,
                source="ai_generated",
                created_by="user_1",
                expected_content_sha256=digest,
            )
            await transaction.commit()

    request_task = asyncio.create_task(commit_projection())
    await commit_started.wait()
    request_task.cancel()
    release_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    with open(target, "rb") as file:
        assert file.read() == b"new"
    assert not [
        name
        for name in os.listdir(os.path.dirname(target))
        if name.startswith(".report.md.tmp-")
    ]


@pytest.mark.asyncio
async def test_runtime_projection_ignores_session_teardown_error_after_commit(
    fs_settings,
    monkeypatch,
    caplog,
):
    from packages.core import database
    from packages.core.ai.runtime.file_actions import (
        RuntimeFileProjectionTransactionFactory,
    )
    from packages.core.services import knowledge_sync, workspace_artifact_purge

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old")

    class FakeDB:
        committed = 0
        rolled_back = False
        closed = 0

        async def commit(self):
            self.committed += 1

        async def rollback(self):
            self.rolled_back = True

        async def close(self):
            self.closed += 1

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            raise OSError("session close failed")

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
        )

    async def persist_cleanup_intents(*_args, **_kwargs):
        return set()

    async def clear_cleanup_intents(*_args, **_kwargs):
        return None

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(
        knowledge_sync,
        "sync_file_to_knowledge",
        successful_sync,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        persist_cleanup_intents,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "clear_artifact_cleanup_jobs",
        clear_cleanup_intents,
    )

    content = b"new"
    digest = hashlib.sha256(content).hexdigest()
    async with RuntimeFileProjectionTransactionFactory.create(
        "entity_1",
    ) as transaction:
        written = transaction.write_bytes(
            "report.md",
            content,
            expected_content_sha256=digest,
            expected_size=len(content),
        )
        await transaction.project_file(
            abs_path=written,
            entity_root=entity_root,
            source="ai_generated",
            created_by="user_1",
            expected_content_sha256=digest,
        )
        await transaction.commit()

    assert fake_db.committed >= 1
    assert fake_db.rolled_back is False
    assert fake_db.closed == 1
    with open(target, "rb") as file:
        assert file.read() == content
    assert "Committed Runtime file transaction session cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_runtime_projection_retries_a_failed_staged_write_rollback(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.file_actions import (
        RuntimeFileCommitError,
        RuntimeFileProjectionTransaction,
        RuntimeFileProjectionTransactionFactory,
    )
    from packages.core.services.entity_fs import EntityFileRollbackSnapshot

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old")

    class FakeDB:
        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    rollback_calls = 0
    original_rollback = EntityFileRollbackSnapshot.rollback

    def fail_first_rollback(self, expected_source_version):
        nonlocal rollback_calls
        rollback_calls += 1
        if rollback_calls == 1:
            raise OSError("transient rollback failure")
        return original_rollback(self, expected_source_version)

    def fail_written_file_record(self, **_kwargs):
        raise OSError("post-write verification failed")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(EntityFileRollbackSnapshot, "rollback", fail_first_rollback)
    monkeypatch.setattr(
        RuntimeFileProjectionTransaction,
        "_record_written_file",
        fail_written_file_record,
    )

    with pytest.raises(RuntimeFileCommitError, match="post-write verification failed"):
        async with RuntimeFileProjectionTransactionFactory.create(
            "entity_1",
        ) as transaction:
            content = b"new"
            transaction.write_bytes(
                "report.md",
                content,
                expected_content_sha256=hashlib.sha256(content).hexdigest(),
                expected_size=len(content),
            )

    assert rollback_calls == 2
    with open(target, "rb") as file:
        assert file.read() == b"old"


@pytest.mark.asyncio
async def test_runtime_projection_restores_existing_file_when_written_target_disappears(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.file_actions import (
        RuntimeFileCommitError,
        RuntimeFileProjectionTransaction,
        RuntimeFileProjectionTransactionFactory,
    )

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old")

    class FakeDB:
        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    def remove_written_target(self, **_kwargs):
        os.unlink(target)
        raise OSError("post-write verification failed")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(
        RuntimeFileProjectionTransaction,
        "_record_written_file",
        remove_written_target,
    )

    with pytest.raises(RuntimeFileCommitError, match="post-write verification failed"):
        async with RuntimeFileProjectionTransactionFactory.create(
            "entity_1",
        ) as transaction:
            content = b"new"
            transaction.write_bytes(
                "report.md",
                content,
                expected_content_sha256=hashlib.sha256(content).hexdigest(),
                expected_size=len(content),
            )

    with open(target, "rb") as file:
        assert file.read() == b"old"


@pytest.mark.asyncio
async def test_runtime_delete_retries_snapshot_handoff_when_staging_restore_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.file_actions import (
        RuntimeFileCommitError,
        RuntimeFileProjectionTransactionFactory,
    )
    from packages.core.services import entity_fs

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"keep")

    class FakeDB:
        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    original_fsync = entity_fs.os.fsync
    fsync_calls = 0

    def fail_staging_fsync_once(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("staging fsync failed")
        return original_fsync(fd)

    original_replace = entity_fs.os.replace
    replace_calls = 0

    def fail_first_restore(src, dst, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("first restore failed")
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(entity_fs.os, "fsync", fail_staging_fsync_once)
    monkeypatch.setattr(entity_fs.os, "replace", fail_first_restore)

    with pytest.raises(RuntimeFileCommitError, match="staging fsync failed"):
        async with RuntimeFileProjectionTransactionFactory.create(
            "entity_1",
        ) as transaction:
            transaction.delete_path(
                "report.md",
                expected_resolved_path=target,
            )

    assert replace_calls == 3
    with open(target, "rb") as file:
        assert file.read() == b"keep"


def test_all_filesystem_projection_mutations_use_cancellation_boundary():
    from apps.api.routers import filesystem
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransaction
    from packages.core.ai.tools import bash_tool, file_tools
    from packages.core.ai.tools import sandbox_file_tools, sandbox_tools
    from packages.core.ai.tools.generate_file import code

    for handler in (
        filesystem.write_file,
        filesystem.make_directory,
        filesystem.move_file,
        filesystem.delete_file,
        filesystem.upload_file,
    ):
        source = inspect.getsource(handler)
        assert "_finish_filesystem_mutation" in source
        assert "_entity_filesystem_mutation_boundary" in source
        assert source.index("_entity_filesystem_mutation_boundary") < source.index(
            "_finish_filesystem_mutation",
        )
    assert "entity_filesystem_mutation_lock" in inspect.getsource(bash_tool._bash)
    assert "runtime_entity_filesystem_mutation_lock" in inspect.getsource(
        file_tools._patch_file,
    )
    for handler in (
        sandbox_file_tools._save_sandbox_file,
        sandbox_tools._sandbox_save_result,
        code.handle_code,
    ):
        assert "runtime_entity_filesystem_mutation_lock" in inspect.getsource(handler)
    assert "finish_entity_filesystem_mutation" in inspect.getsource(
        RuntimeFileProjectionTransaction.commit,
    )
    assert "finish_entity_filesystem_mutation" in inspect.getsource(
        RuntimeFileProjectionTransaction.__aexit__,
    )


@pytest.mark.asyncio
async def test_entity_filesystem_mutation_lock_serializes_callers(tmp_path):
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    entity_root = str(tmp_path / "entity_1")
    owner = entity_filesystem_mutation_lock(entity_root)
    await owner.__aenter__()
    waiter_entered = asyncio.Event()

    async def wait_for_lock():
        async with entity_filesystem_mutation_lock(entity_root):
            waiter_entered.set()

    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0.05)
    try:
        assert not waiter_entered.is_set()
    finally:
        await owner.__aexit__(None, None, None)

    await asyncio.wait_for(waiter, timeout=0.5)
    assert waiter_entered.is_set()


@pytest.mark.asyncio
async def test_entity_filesystem_mutation_lock_accepts_symlinked_config_root(
    tmp_path,
):
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    configured_root = tmp_path / "configured-root"
    configured_root.symlink_to(real_root, target_is_directory=True)
    entity_root = configured_root / "entity_1"

    async with entity_filesystem_mutation_lock(str(entity_root)):
        lock_file = (
            real_root
            / "entity_1"
            / ".ai"
            / "write-locks"
            / "entity-mutation.lock"
        )
        assert lock_file.is_file()

    assert configured_root.is_symlink()


@pytest.mark.asyncio
async def test_entity_filesystem_read_locks_share_and_block_mutations(tmp_path):
    from packages.core.services import entity_fs

    entity_root = str(tmp_path / "entity_1")
    first_reader = entity_fs.entity_filesystem_read_lock(entity_root)
    second_reader = entity_fs.entity_filesystem_read_lock(entity_root)
    await first_reader.__aenter__()
    await second_reader.__aenter__()
    try:
        writer = await asyncio.to_thread(
            entity_fs._try_acquire_entity_mutation_lock,
            entity_root,
        )
        assert writer is None
    finally:
        await second_reader.__aexit__(None, None, None)
        await first_reader.__aexit__(None, None, None)

    writer = await asyncio.to_thread(
        entity_fs._try_acquire_entity_mutation_lock,
        entity_root,
    )
    assert writer is not None
    entity_fs._release_entity_mutation_lock(writer)


@pytest.mark.asyncio
async def test_entity_filesystem_mutation_lock_wait_is_bounded(tmp_path):
    from packages.core.services.entity_fs import (
        EntityFilesystemBusyError,
        entity_filesystem_mutation_lock,
    )

    entity_root = str(tmp_path / "entity_1")
    owner = entity_filesystem_mutation_lock(entity_root)
    await owner.__aenter__()
    try:
        with pytest.raises(EntityFilesystemBusyError):
            async with entity_filesystem_mutation_lock(
                entity_root,
                timeout_seconds=0.02,
            ):
                raise AssertionError("bounded waiter must not enter")
    finally:
        await owner.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_cancelled_entity_lock_attempt_releases_late_acquisition(
    tmp_path, monkeypatch,
):
    from packages.core.services import entity_fs

    attempt_started = threading.Event()
    finish_attempt = threading.Event()
    lock_released = threading.Event()
    fake_handle = object()

    def delayed_attempt(_entity_root):
        attempt_started.set()
        finish_attempt.wait(timeout=1)
        return fake_handle

    def record_release(handle):
        assert handle is fake_handle
        lock_released.set()

    monkeypatch.setattr(entity_fs, "_try_acquire_entity_mutation_lock", delayed_attempt)
    monkeypatch.setattr(entity_fs, "_release_entity_mutation_lock", record_release)

    async def wait_for_lock():
        async with entity_fs.entity_filesystem_mutation_lock(str(tmp_path)):
            raise AssertionError("a cancelled waiter must not enter")

    waiter = asyncio.create_task(wait_for_lock())
    assert await asyncio.to_thread(attempt_started.wait, 1)
    waiter.cancel()
    finish_attempt.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await asyncio.to_thread(lock_released.wait, 1)


def test_all_filesystem_projection_mutations_use_shared_path_locks():
    from apps.api.routers import filesystem

    for handler in (
        filesystem.write_file,
        filesystem.make_directory,
        filesystem.move_file,
        filesystem.delete_file,
        filesystem.upload_file,
    ):
        assert "_acquire_path_mutation_locks" in inspect.getsource(handler)


def test_document_creation_keeps_bytes_and_acl_inside_entity_boundary():
    from apps.api.routers import documents

    for handler in (
        documents.upload_document,
        documents.create_blank_document,
        documents._generate_ai_draft_content,
        documents.upload_from_google_drive,
    ):
        source = inspect.getsource(handler)
        assert "_document_filesystem_mutation" in source
        assert "_finish_document_filesystem_mutation" in source
        assert "_rollback_database_best_effort" in source
        assert "_remove_or_quarantine_document_file" in source

    for handler in (
        documents.upload_document,
        documents.upload_from_google_drive,
    ):
        assert "_commit_document_and_dispatch_embeddings" in inspect.getsource(handler)

    for handler in (
        documents.create_blank_document,
        documents._generate_ai_draft_content,
    ):
        source = inspect.getsource(handler)
        assert "await db.commit()" in source
        assert "_invalidate_committed_document_cache" in source

    sync_source = inspect.getsource(documents.sync_google_drive_document)
    assert "_read_document_file_for_rollback" in sync_source
    assert "_restore_document_file_after_failure" in sync_source
    assert "_commit_document_and_dispatch_embeddings" in sync_source


@pytest.mark.asyncio
async def test_document_rollback_quarantines_file_when_unlink_fails(
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "private", "contract.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"private contract")

    original_remove = os.remove

    def fail_target_remove(path):
        if os.path.realpath(path) == os.path.realpath(target):
            raise OSError("simulated unlink failure")
        return original_remove(path)

    monkeypatch.setattr(documents.os, "remove", fail_target_remove)

    await documents._remove_or_quarantine_document_file(
        "entity_1",
        "private/contract.txt",
    )

    assert not os.path.exists(target)
    quarantine_dir = os.path.join(
        entity_root,
        ".ai",
        "document-rollbacks",
    )
    quarantined = os.listdir(quarantine_dir)
    assert len(quarantined) == 1
    with open(os.path.join(quarantine_dir, quarantined[0]), "rb") as file:
        assert file.read() == b"private contract"


@pytest.mark.asyncio
async def test_google_sync_failure_restores_previous_document_bytes(fs_settings):
    from apps.api.routers import documents

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "drive", "report.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"previous report")

    previous = await documents._read_document_file_for_rollback(
        "entity_1",
        "drive/report.txt",
    )
    await documents._write_document_bytes_atomic(
        "entity_1",
        "drive/report.txt",
        b"new report",
        allow_empty=False,
    )
    await documents._restore_document_file_after_failure(
        "entity_1",
        "drive/report.txt",
        previous,
    )

    with open(target, "rb") as file:
        assert file.read() == b"previous report"


@pytest.mark.asyncio
async def test_google_sync_does_not_recreate_a_document_trashed_during_download(
    client,
    fs_settings,
    monkeypatch,
):
    import httpx

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "synctrashrace",
            "email": "synctrashrace@test.com",
            "password": "pass123",
            "entity_name": "Sync Trash Race Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("drive.txt", b"previous drive bytes", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.source = "google_drive"
        row.metadata_ = {
            "external": {
                "google_drive": {
                    "file_id": "drive-file-1",
                    "modified_time": "old",
                }
            }
        }
        await db.commit()

    class DownloadThatTrashesDocument:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            trashed = await client.post(
                f"/api/v1/documents/{document_id}/trash",
                headers=headers,
            )
            assert trashed.status_code == 200, trashed.text
            return SimpleNamespace(status_code=200, content=b"new drive bytes")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: DownloadThatTrashesDocument(),
    )

    response = await client.post(
        f"/api/v1/documents/{document_id}/sync-google-drive",
        headers=headers,
        json={
            "file_id": "drive-file-1",
            "name": "drive.txt",
            "mime_type": "text/plain",
            "modified_time": "new",
            "access_token": "test-token",
        },
    )

    assert response.status_code == 404, response.text
    assert not os.path.exists(
        os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "drive.txt")
    )
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path.startswith(f".trash/documents/{document_id}/")
        trashed_path = os.path.join(
            fs_settings.MANOR_FS_ROOT,
            entity_id,
            row.fs_path,
        )
    with open(trashed_path, "rb") as file:
        assert file.read() == b"previous drive bytes"


@pytest.mark.asyncio
async def test_google_sync_commits_fresh_bytes_and_metadata(
    client,
    fs_settings,
    monkeypatch,
):
    import httpx

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "syncfresh",
            "email": "syncfresh@test.com",
            "password": "pass123",
            "entity_name": "Sync Fresh Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("fresh.txt", b"old bytes", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.source = "google_drive"
        row.metadata_ = {
            "external": {
                "google_drive": {
                    "file_id": "drive-file-2",
                    "modified_time": "old",
                }
            }
        }
        await db.commit()

    class SuccessfulDownload:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200, content=b"fresh bytes")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: SuccessfulDownload(),
    )
    response = await client.post(
        f"/api/v1/documents/{document_id}/sync-google-drive",
        headers=headers,
        json={
            "file_id": "drive-file-2",
            "name": "fresh.txt",
            "mime_type": "text/plain",
            "modified_time": "new",
            "access_token": "test-token",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "synced"}
    with open(
        os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "fresh.txt"),
        "rb",
    ) as file:
        assert file.read() == b"fresh bytes"
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is False
        assert row.metadata_["external"]["google_drive"]["modified_time"] == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_delay", [0, 1.1])
async def test_google_sync_rejects_an_older_download_after_a_newer_sync_commits(
    client,
    fs_settings,
    monkeypatch,
    lookup_delay,
):
    import httpx

    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "syncversionrace",
            "email": "syncversionrace@test.com",
            "password": "pass123",
            "entity_name": "Sync Version Race Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("versioned.txt", b"original bytes", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.source = "google_drive"
        row.metadata_ = {
            "external": {
                "google_drive": {
                    "file_id": "drive-file-race",
                    "modified_time": "version-1",
                }
            }
        }
        await db.commit()

    original_lookup = documents.get_visible_document

    async def delayed_lookup(*args, **kwargs):
        await asyncio.sleep(lookup_delay)
        return await original_lookup(*args, **kwargs)

    monkeypatch.setattr(documents, "get_visible_document", delayed_lookup)
    older_download_started = asyncio.Event()
    release_older_download = asyncio.Event()

    class OrderedDownloads:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                older_download_started.set()
                await release_older_download.wait()
                return SimpleNamespace(status_code=200, content=b"older bytes")
            return SimpleNamespace(status_code=200, content=b"newest bytes")

    downloads = OrderedDownloads()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: downloads,
    )
    older_request = asyncio.create_task(
        client.post(
            f"/api/v1/documents/{document_id}/sync-google-drive",
            headers=headers,
            json={
                "file_id": "drive-file-race",
                "name": "versioned.txt",
                "mime_type": "text/plain",
                "modified_time": "version-2",
                "access_token": "test-token",
            },
        )
    )
    download_started = asyncio.create_task(older_download_started.wait())
    try:
        # Order the race by the download barrier, not by how quickly CI can
        # complete authentication and database-backed permission checks.
        done, _ = await asyncio.wait(
            {download_started, older_request},
            timeout=10,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if older_request in done:
            response = older_request.result()
            pytest.fail(
                f"Sync returned {response.status_code} before downloading: {response.text}"
            )
        assert download_started in done, "Sync did not reach the download barrier"
        newer_response = await asyncio.wait_for(
            client.post(
                f"/api/v1/documents/{document_id}/sync-google-drive",
                headers=headers,
                json={
                    "file_id": "drive-file-race",
                    "name": "versioned.txt",
                    "mime_type": "text/plain",
                    "modified_time": "version-3",
                    "access_token": "test-token",
                },
            ),
            timeout=10,
        )
        release_older_download.set()
        older_response = await asyncio.wait_for(older_request, timeout=10)
    finally:
        release_older_download.set()
        for task in (download_started, older_request):
            if not task.done():
                task.cancel()
        await asyncio.gather(download_started, older_request, return_exceptions=True)

    assert newer_response.status_code == 200, newer_response.text
    assert newer_response.json() == {"status": "synced"}
    assert older_response.status_code == 409, older_response.text
    with open(
        os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "versioned.txt"),
        "rb",
    ) as file:
        assert file.read() == b"newest bytes"
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.metadata_["external"]["google_drive"]["modified_time"] == "version-3"


def test_raw_filesystem_readers_use_shared_entity_read_boundary():
    from apps.api.routers import filesystem
    from packages.core.ai.tools import bash_tool

    assert "entity_filesystem_read_lock" in inspect.getsource(bash_tool._bash)
    for handler in (
        filesystem.list_directory,
        filesystem.directory_tree,
        filesystem.read_file,
        filesystem.file_info,
        filesystem.search_files,
        filesystem.resolve_wiki_links,
        filesystem.wiki_index,
        filesystem.lint_knowledge_base,
        filesystem.serve_entity_file,
    ):
        assert "_entity_filesystem_read_boundary" in inspect.getsource(handler)


@pytest.mark.asyncio
async def test_filesystem_read_holds_shared_lock_across_acl_and_bytes(
    fs_settings, monkeypatch,
):
    from apps.api.routers import filesystem
    from packages.core.services import entity_fs

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    os.makedirs(entity_root, exist_ok=True)
    with open(os.path.join(entity_root, "report.md"), "w", encoding="utf-8") as file:
        file.write("authorized snapshot")
    acl_started = asyncio.Event()
    release_acl = asyncio.Event()

    async def delayed_acl(*_args, **_kwargs):
        acl_started.set()
        await release_acl.wait()

    monkeypatch.setattr(filesystem, "_assert_path_readable", delayed_acl)
    user = SimpleNamespace(id="user_1", entity_id="entity_1", role="member")
    request = asyncio.create_task(filesystem.read_file(
        user=user,
        path="report.md",
        db=SimpleNamespace(),
    ))
    await asyncio.wait_for(acl_started.wait(), timeout=1)
    writer = await asyncio.to_thread(
        entity_fs._try_acquire_entity_mutation_lock,
        entity_root,
    )
    assert writer is None

    release_acl.set()
    result = await request
    assert result["content"] == "authorized snapshot"


@pytest.mark.asyncio
async def test_streamed_file_response_releases_read_lock_on_cancellation(
    tmp_path, monkeypatch,
):
    from starlette.responses import FileResponse

    from apps.api.routers import filesystem

    released = asyncio.Event()

    class ReadBoundary:
        async def __aexit__(self, *_args):
            released.set()

    async def cancel_stream(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(FileResponse, "__call__", cancel_stream)
    path = tmp_path / "report.md"
    path.write_text("snapshot", encoding="utf-8")
    response = filesystem._ReadLockedFileResponse(
        path,
        read_boundary=ReadBoundary(),
    )

    with pytest.raises(asyncio.CancelledError):
        await response({}, None, None)
    assert released.is_set()


@pytest.mark.asyncio
async def test_snapshot_file_response_does_not_delegate_temporary_pathsend(
    tmp_path,
    monkeypatch,
):
    from apps.api import file_responses

    EntitySnapshotFileResponse = file_responses.EntitySnapshotFileResponse
    budget = file_responses._SnapshotBudget(max_files=1, max_bytes=100)
    monkeypatch.setattr(file_responses, "_snapshot_budget", budget)

    released = asyncio.Event()

    class ReadBoundary:
        async def __aexit__(self, *_args):
            released.set()

    source = tmp_path / "pathsend.md"
    source.write_bytes(b"stable snapshot")
    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    response = EntitySnapshotFileResponse(
        source,
        read_boundary=ReadBoundary(),
    )
    await response(
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "extensions": {"http.response.pathsend": {}},
        },
        None,
        send,
    )

    assert released.is_set()
    assert not any(message["type"] == "http.response.pathsend" for message in messages)
    assert b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ) == b"stable snapshot"
    assert budget.try_reserve(100) is True
    budget.release(100)


@pytest.mark.asyncio
async def test_snapshot_file_response_rejects_oversized_files_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    from apps.api import file_responses

    released = asyncio.Event()

    class ReadBoundary:
        async def __aexit__(self, *_args):
            released.set()

    source = tmp_path / "oversized.bin"
    source.write_bytes(b"too large")
    messages: list[dict] = []
    monkeypatch.setattr(file_responses, "_MAX_SNAPSHOT_FILE_BYTES", 4)

    async def send(message):
        messages.append(message)

    response = file_responses.EntitySnapshotFileResponse(
        source,
        read_boundary=ReadBoundary(),
    )
    await response(
        {"type": "http", "method": "GET", "headers": []},
        None,
        send,
    )

    assert released.is_set()
    assert messages[0]["status"] == 413


@pytest.mark.asyncio
async def test_snapshot_file_response_returns_retryable_busy_response(
    tmp_path,
    monkeypatch,
):
    from apps.api import file_responses

    released = asyncio.Event()

    class ReadBoundary:
        async def __aexit__(self, *_args):
            released.set()

    source = tmp_path / "busy.bin"
    source.write_bytes(b"content")
    messages: list[dict] = []
    monkeypatch.setattr(
        file_responses,
        "_snapshot_budget",
        file_responses._SnapshotBudget(max_files=0, max_bytes=0),
    )

    async def send(message):
        messages.append(message)

    response = file_responses.EntitySnapshotFileResponse(
        source,
        read_boundary=ReadBoundary(),
    )
    await response(
        {"type": "http", "method": "GET", "headers": []},
        None,
        send,
    )

    assert released.is_set()
    assert messages[0]["status"] == 503
    assert (b"retry-after", b"1") in messages[0]["headers"]


def test_main_document_editor_uses_shared_entity_write_boundary():
    from packages.core.services import document_service

    for handler in (
        document_service.save_document_content,
        document_service.save_document_file,
    ):
        source = inspect.getsource(handler)
        assert "entity_filesystem_mutation_lock" in source
        assert "claim_entity_editor_write_intent" in source
        assert "_claim_metadata_editor_write_intent" in source
        assert "_repair_entity_editor_write_receipt" in source


@pytest.mark.asyncio
async def test_filesystem_write_fence_rejects_a_late_older_editor_save(
    fs_settings, monkeypatch,
):
    from fastapi import HTTPException

    from apps.api.routers import filesystem
    from packages.core.services import knowledge_sync

    async def allow_write(*_args, **_kwargs):
        return None

    async def sync_write(**_kwargs):
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    monkeypatch.setattr(filesystem, "_require_path_write_access", allow_write)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync_write)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)
    user = SimpleNamespace(
        id="user_1",
        entity_id="entity_1",
        email="writer@test.com",
    )

    newest = await filesystem.write_file(
        filesystem.WriteRequest(
            path="site/app.ts",
            content="newest",
            save_session_id="editor-session",
            save_sequence=2,
        ),
        user=user,
        db=SimpleNamespace(),
    )
    assert newest["status"] == "ok"

    with pytest.raises(HTTPException) as exc_info:
        await filesystem.write_file(
            filesystem.WriteRequest(
                path="site/app.ts",
                content="late-old",
                save_session_id="editor-session",
                save_sequence=1,
            ),
            user=user,
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_write_intent"
    with open(
        os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "site", "app.ts"),
        encoding="utf-8",
    ) as file:
        assert file.read() == "newest"


@pytest.mark.asyncio
async def test_filesystem_write_replay_skips_duplicate_knowledge_sync(
    fs_settings, monkeypatch,
):
    from apps.api.routers import filesystem
    from packages.core.services import knowledge_sync

    sync_calls = 0

    async def allow_write(*_args, **_kwargs):
        return None

    async def sync_write(**_kwargs):
        nonlocal sync_calls
        sync_calls += 1
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    monkeypatch.setattr(filesystem, "_require_path_write_access", allow_write)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync_write)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)
    user = SimpleNamespace(
        id="user_1",
        entity_id="entity_1",
        email="writer@test.com",
    )
    request = filesystem.WriteRequest(
        path="site/app.ts",
        content="saved-once",
        save_session_id="editor-session",
        save_sequence=1,
    )

    first = await filesystem.write_file(request, user=user, db=SimpleNamespace())
    replay = await filesystem.write_file(request, user=user, db=SimpleNamespace())

    assert first["knowledge_sync"]["document_id"] == "doc_1"
    assert replay["knowledge_sync"]["document_id"] == "doc_1"
    assert sync_calls == 1


@pytest.mark.asyncio
async def test_main_editor_write_fence_rejects_a_late_auxiliary_save(
    fs_settings, monkeypatch,
):
    from fastapi import HTTPException

    from apps.api.routers import filesystem
    from packages.core.services import document_service, knowledge_sync, version_service

    async def allow_write(*_args, **_kwargs):
        return None

    async def sync_write(**_kwargs):
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    async def flush():
        return None

    async def commit():
        return None

    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="site/app.ts",
        name="app.ts",
        metadata_={},
        file_size=0,
        file_type="ts",
        mime_type="text/typescript",
    )
    monkeypatch.setattr(filesystem, "_require_path_write_access", allow_write)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync_write)
    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(version_service, "create_version", no_op)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)

    saved = await document_service.save_document_content(
        SimpleNamespace(flush=flush, commit=commit),
        "doc_1",
        "entity_1",
        "main-editor-newest",
        save_session_id="shared-editor-session",
        save_sequence=2,
    )
    assert saved is True

    user = SimpleNamespace(id="user_1", entity_id="entity_1", email="writer@test.com")
    with pytest.raises(HTTPException) as exc_info:
        await filesystem.write_file(
            filesystem.WriteRequest(
                path="site/app.ts",
                content="late-auxiliary",
                save_session_id="shared-editor-session",
                save_sequence=1,
            ),
            user=user,
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 409
    with open(
        os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "site", "app.ts"),
        encoding="utf-8",
    ) as file:
        assert file.read() == "main-editor-newest"


@pytest.mark.asyncio
async def test_main_editor_stale_path_claim_keeps_document_state_clean(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service
    from packages.core.services.entity_fs import EntityFilesystemStaleWriteError

    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="site/app.ts",
        name="app.ts",
        metadata_={"existing": "metadata"},
        file_size=17,
        file_type="ts",
        mime_type="text/typescript",
    )

    async def get_document(*_args, **_kwargs):
        return document

    monkeypatch.setattr(document_service, "get_document", get_document)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    claim_entity_editor_write_intent(
        entity_root,
        document.fs_path,
        "shared-editor-session",
        2,
        "auxiliary-newest",
    )

    with pytest.raises(EntityFilesystemStaleWriteError):
        await document_service.save_document_content(
            SimpleNamespace(),
            document.id,
            document.entity_id,
            "late-main-editor",
            save_session_id="shared-editor-session",
            save_sequence=1,
        )

    assert document.metadata_ == {"existing": "metadata"}
    assert document.file_size == 17
    assert document.file_type == "ts"
    assert document.mime_type == "text/typescript"


@pytest.mark.asyncio
async def test_document_editor_replay_skips_duplicate_versions(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service, version_service

    effects = {"commits": 0, "versions": 0}
    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="site/app.ts",
        name="app.ts",
        metadata_={},
        file_size=0,
        file_type="ts",
        mime_type="text/typescript",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    async def create_version(*_args, **_kwargs):
        effects["versions"] += 1

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            effects["commits"] += 1

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(version_service, "create_version", create_version)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)
    db = FakeDb()

    for _attempt in range(2):
        saved = await document_service.save_document_content(
            db,
            "doc_1",
            "entity_1",
            "same bytes",
            save_session_id="editor-session",
            save_sequence=1,
        )
        assert saved is True

    assert effects == {"commits": 1, "versions": 1}


@pytest.mark.asyncio
async def test_document_editor_path_replay_repairs_only_metadata(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service, version_service

    effects = {"cache_bumps": 0, "commits": 0, "versions": 0}
    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="reports/weekly.txt",
        name="weekly.txt",
        metadata_={},
        file_size=17,
        file_type="txt",
        mime_type="text/plain",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def bump_cache(*_args, **_kwargs):
        effects["cache_bumps"] += 1

    async def create_version(*_args, **_kwargs):
        effects["versions"] += 1

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            effects["commits"] += 1

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", bump_cache)
    monkeypatch.setattr(version_service, "create_version", create_version)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    intent = document_service._document_content_write_intent("same logical content")
    claim_entity_editor_write_intent(
        entity_root,
        document.fs_path,
        "editor-session",
        1,
        intent,
    )
    mark_entity_editor_write_intent_committed(
        entity_root,
        document.fs_path,
        "editor-session",
        1,
        intent,
        receipt={"document_id": document.id},
    )

    assert await document_service.save_document_content(
        FakeDb(),
        document.id,
        document.entity_id,
        "same logical content",
        save_session_id="editor-session",
        save_sequence=1,
    )

    assert effects == {"cache_bumps": 1, "commits": 1, "versions": 0}
    assert document.file_size == 17
    assert document.mime_type == "text/plain"
    receipt = document.metadata_["_editor_write_fence"]["sessions"]["editor-session"]
    assert receipt["committed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "file_type", "mime_type"),
    [
        ("proposal.docx", "document", "application/octet-stream"),
        ("renamed.bin", "xlsx", "application/octet-stream"),
        (
            "renamed.bin",
            "file",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ],
)
async def test_document_content_save_rejects_office_binary_documents(
    fs_settings,
    monkeypatch,
    name,
    file_type,
    mime_type,
):
    from packages.core.services import document_service

    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path=f"reports/{name}",
        name=name,
        metadata_={},
        file_size=17,
        file_type=file_type,
        mime_type=mime_type,
    )

    async def get_document(*_args, **_kwargs):
        return document

    monkeypatch.setattr(document_service, "get_document", get_document)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)

    with pytest.raises(ValueError, match="complete binary files"):
        await document_service.save_document_content(
            SimpleNamespace(),
            document.id,
            document.entity_id,
            "flattened content",
        )


def test_document_editor_keeps_v1_receipt_compatible_with_previous_reader():
    from packages.core.services import document_service

    content = "same logical content"
    intent = document_service._document_content_write_intent(content)
    status, metadata = document_service._claim_metadata_editor_write_intent(
        {},
        session_id="rolling-deploy-session",
        sequence=3,
        content=intent,
        compatible_legacy_digests=(
            document_service._compatible_legacy_document_content_digests(content)
        ),
    )

    assert status is EditorWriteIntentStatus.CLAIMED
    receipt = metadata["_editor_write_fence"]["sessions"]["rolling-deploy-session"]
    assert receipt["content_digest"] == hashlib.sha256(
        b"manor-document-content-v1\0same logical content"
    ).hexdigest()
    assert "digest_scheme" not in receipt


def test_document_editor_accepts_pre_v1_raw_text_receipt():
    from packages.core.services import document_service

    content = "same logical content"
    metadata = {
        "_editor_write_fence": {
            "sessions": {
                "rolling-deploy-session": {
                    "sequence": 3,
                    "content_digest": hashlib.sha256(content.encode()).hexdigest(),
                    "committed": True,
                },
            },
        },
    }

    status, _metadata = document_service._claim_metadata_editor_write_intent(
        metadata,
        session_id="rolling-deploy-session",
        sequence=3,
        content=document_service._document_content_write_intent(content),
        compatible_legacy_digests=(
            document_service._compatible_legacy_document_content_digests(content)
        ),
    )

    assert status is EditorWriteIntentStatus.REPLAYED


@pytest.mark.asyncio
async def test_plain_editor_rejects_changed_pre_scheme_receipt(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service

    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="reports/legacy.txt",
        name="legacy.txt",
        metadata_={
            "_editor_write_fence": {
                "sessions": {
                    "rolling-deploy-session": {
                        "sequence": 7,
                        "content_digest": hashlib.sha256(b"expected-old-bytes").hexdigest(),
                        "committed": True,
                    },
                },
            },
        },
        file_size=0,
        file_type="txt",
        mime_type="text/plain",
    )

    async def get_document(*_args, **_kwargs):
        return document

    monkeypatch.setattr(document_service, "get_document", get_document)
    target = os.path.join(
        fs_settings.MANOR_FS_ROOT,
        "entity_1",
        "reports",
        "legacy.txt",
    )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"expected-old-bytes")

    with pytest.raises(document_service.EntityFilesystemStaleWriteError):
        await document_service.save_document_content(
            SimpleNamespace(),
            "doc_1",
            "entity_1",
            "different logical content",
            save_session_id="rolling-deploy-session",
            save_sequence=7,
        )


@pytest.mark.asyncio
async def test_document_editor_reconciles_a_failed_filesystem_receipt(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service, entity_fs, version_service

    effects = {"commits": 0, "versions": 0, "receipt_attempts": 0}
    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="site/app.ts",
        name="app.ts",
        metadata_={},
        file_size=0,
        file_type="ts",
        mime_type="text/typescript",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    async def create_version(*_args, **_kwargs):
        effects["versions"] += 1

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            effects["commits"] += 1

    original_mark = entity_fs.mark_entity_editor_write_intent_digest_committed

    def fail_first_receipt(*args, **kwargs):
        effects["receipt_attempts"] += 1
        if effects["receipt_attempts"] == 1:
            raise OSError("receipt unavailable")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        entity_fs,
        "mark_entity_editor_write_intent_digest_committed",
        fail_first_receipt,
    )
    monkeypatch.setattr(version_service, "create_version", create_version)
    os.makedirs(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1"), exist_ok=True)
    db = FakeDb()

    for _attempt in range(2):
        assert await document_service.save_document_content(
            db,
            "doc_1",
            "entity_1",
            "same bytes",
            save_session_id="editor-session",
            save_sequence=1,
        )

    assert effects == {"commits": 1, "versions": 1, "receipt_attempts": 2}


@pytest.mark.asyncio
async def test_metadata_document_editor_fences_replays_and_stale_sequences(
    db_session, monkeypatch,
):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.document_version import DocumentVersion
    from packages.core.services import document_service
    from packages.core.services.entity_fs import EntityFilesystemStaleWriteError

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", False)
    monkeypatch.setattr(
        document_service,
        "bump_tool_cache_version",
        AsyncMock(return_value=None),
    )

    entity_id = generate_ulid()
    document = Document(
        entity_id=entity_id,
        name="cloud-note.md",
        fs_path=None,
        file_type="md",
        source="generated",
        metadata_={},
    )
    db_session.add(document)
    await db_session.commit()

    for _attempt in range(2):
        assert await document_service.save_document_content(
            db_session,
            document.id,
            entity_id,
            "newest cloud content",
            save_session_id="cloud-editor-session",
            save_sequence=2,
        )

    versions = list((await db_session.execute(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
        )
    )).scalars())
    assert len(versions) == 1

    with pytest.raises(EntityFilesystemStaleWriteError):
        await document_service.save_document_content(
            db_session,
            document.id,
            entity_id,
            "late stale content",
            save_session_id="cloud-editor-session",
            save_sequence=1,
        )

    await db_session.rollback()
    await db_session.refresh(document)
    assert document.metadata_["content"] == "newest cloud content"
    versions = list((await db_session.execute(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
        )
    )).scalars())
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_document_editor_cancellation_keeps_lock_through_commit(
    fs_settings, monkeypatch,
):
    from packages.core.services import document_service, entity_fs, version_service

    write_started = threading.Event()
    release_write = threading.Event()
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    commit_finished = asyncio.Event()
    document = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        fs_path="site/app.ts",
        name="app.ts",
        metadata_={},
        file_size=0,
        file_type="ts",
        mime_type="text/typescript",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    def slow_write(*_args, **_kwargs):
        write_started.set()
        assert release_write.wait(timeout=2)
        return os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "site", "app.ts")

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            commit_started.set()
            await release_commit.wait()
            commit_finished.set()

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", slow_write)
    monkeypatch.setattr(version_service, "create_version", no_op)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    os.makedirs(entity_root, exist_ok=True)

    save_task = asyncio.create_task(document_service.save_document_content(
        FakeDb(),
        "doc_1",
        "entity_1",
        "newest",
    ))
    assert await asyncio.to_thread(write_started.wait, 1)
    save_task.cancel()
    await asyncio.sleep(0)
    assert not save_task.done()

    release_write.set()
    await commit_started.wait()
    contender = await asyncio.to_thread(
        entity_fs._try_acquire_entity_mutation_lock,
        entity_root,
    )
    assert contender is None

    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await save_task
    assert commit_finished.is_set()


@pytest.mark.asyncio
async def test_path_write_lock_waiters_do_not_starve_the_default_executor(tmp_path):
    from apps.api.routers import filesystem

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    root = str(tmp_path)
    owner = await filesystem._acquire_path_write_lock(root, "site/app.ts")

    async def wait_then_release():
        handle = await filesystem._acquire_path_write_lock(root, "site/app.ts")
        await asyncio.to_thread(filesystem._release_path_write_lock, handle)

    waiters = [asyncio.create_task(wait_then_release()) for _ in range(2)]
    await asyncio.sleep(0.05)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(lambda: True), timeout=0.5)
    finally:
        await asyncio.to_thread(filesystem._release_path_write_lock, owner)
        await asyncio.gather(*waiters)


@pytest.mark.asyncio
async def test_path_write_lock_wait_is_bounded(tmp_path):
    from fastapi import HTTPException

    from apps.api.routers import filesystem

    root = str(tmp_path)
    owner = await filesystem._acquire_path_write_lock(root, "site/app.ts")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await filesystem._acquire_path_write_lock(
                root,
                "site/app.ts",
                timeout_seconds=0.02,
            )
        assert exc_info.value.status_code == 423
    finally:
        filesystem._release_path_write_lock(owner)


@pytest.mark.asyncio
async def test_cancelled_path_lock_attempt_releases_late_acquisition(
    tmp_path, monkeypatch,
):
    from apps.api.routers import filesystem

    attempt_started = threading.Event()
    finish_attempt = threading.Event()
    lock_released = threading.Event()
    fake_handle = object()

    def delayed_attempt(_root, _rel_path):
        attempt_started.set()
        finish_attempt.wait(timeout=1)
        return fake_handle

    def record_release(handle):
        assert handle is fake_handle
        lock_released.set()

    monkeypatch.setattr(filesystem, "_try_acquire_path_write_lock", delayed_attempt)
    monkeypatch.setattr(filesystem, "_release_path_write_lock", record_release)

    waiter = asyncio.create_task(filesystem._acquire_path_write_lock(
        str(tmp_path),
        "site/app.ts",
    ))
    assert await asyncio.to_thread(attempt_started.wait, 1)
    waiter.cancel()
    finish_attempt.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await asyncio.to_thread(lock_released.wait, 1)


@pytest.mark.asyncio
async def test_cancelled_write_stops_while_waiting_for_path_lock(
    fs_settings, monkeypatch,
):
    from apps.api.routers import filesystem

    async def allow_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(filesystem, "_require_path_write_access", allow_write)
    root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    os.makedirs(os.path.join(root, "site"), exist_ok=True)
    target = os.path.join(root, "site", "app.ts")
    with open(target, "w", encoding="utf-8") as file:
        file.write("original")
    owner = await filesystem._acquire_path_write_lock(root, "site/app.ts")
    user = SimpleNamespace(id="user_1", entity_id="entity_1", email="writer@test.com")

    write_task = asyncio.create_task(filesystem.write_file(
        filesystem.WriteRequest(path="site/app.ts", content="cancelled"),
        user=user,
        db=SimpleNamespace(),
    ))
    await asyncio.sleep(0.03)
    write_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(write_task, timeout=0.5)
    finally:
        filesystem._release_path_write_lock(owner)

    with open(target, encoding="utf-8") as file:
        assert file.read() == "original"


@pytest.mark.asyncio
async def test_delete_waits_for_same_path_write_projection(
    fs_settings, monkeypatch,
):
    from apps.api.routers import filesystem
    from packages.core.services import knowledge_sync

    write_sync_started = asyncio.Event()
    release_write_sync = asyncio.Event()
    trash_started = asyncio.Event()

    async def allow_write(*_args, **_kwargs):
        return None

    async def allow_mutation(*_args, **_kwargs):
        return [SimpleNamespace(id="doc_1")]

    async def sync_write(**_kwargs):
        write_sync_started.set()
        await release_write_sync.wait()
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    async def trash_path(*_args, **_kwargs):
        trash_started.set()
        return True

    monkeypatch.setattr(filesystem, "_require_path_write_access", allow_write)
    monkeypatch.setattr(filesystem, "_require_path_mutation_access", allow_mutation)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync_write)
    monkeypatch.setattr(knowledge_sync, "trash_path", trash_path)
    root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    os.makedirs(os.path.join(root, "site"), exist_ok=True)
    target = os.path.join(root, "site", "app.ts")
    with open(target, "w", encoding="utf-8") as file:
        file.write("original")
    user = SimpleNamespace(id="user_1", entity_id="entity_1", email="writer@test.com")

    write_task = asyncio.create_task(filesystem.write_file(
        filesystem.WriteRequest(path="site/app.ts", content="newest"),
        user=user,
        db=SimpleNamespace(),
    ))
    await asyncio.wait_for(write_sync_started.wait(), timeout=0.5)
    delete_task = asyncio.create_task(filesystem.delete_file(
        filesystem.DeleteRequest(path="site/app.ts"),
        user=user,
        db=SimpleNamespace(),
    ))
    await asyncio.sleep(0.05)
    assert not trash_started.is_set()

    release_write_sync.set()
    await asyncio.gather(write_task, delete_task)
    assert trash_started.is_set()
    assert not os.path.exists(target)


def test_write_entity_file_atomic_handles_long_unicode_basename_tmp(fs_settings):
    filename = f"{'长' * 75}.mp4"
    assert len(filename.encode("utf-8")) < 255

    path = write_entity_file_atomic(
        "entity_1",
        f"videos/{filename}",
        b"video-bytes",
        expected_size=len(b"video-bytes"),
    )

    assert path.endswith(os.path.join("entity_1", "videos", filename))
    assert open(path, "rb").read() == b"video-bytes"


def test_write_entity_file_atomic_rejects_size_mismatch(fs_settings):
    with pytest.raises(EntityFileWriteError, match="size mismatch"):
        write_entity_file_atomic("entity_1", "videos/out.mp4", b"abc", expected_size=4)

    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "videos", "out.mp4"))


def test_resolve_path_rejects_entity_root_prefix_escape(fs_settings):
    assert resolve_path("entity_1", "../entity_10/leak.txt") is None


@pytest.mark.asyncio
async def test_runtime_generate_document_file_restores_prior_bytes_when_sync_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.runtime import file_actions
    from packages.core.services import knowledge_sync

    target = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old report")

    async def allow_file_mutation(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(
        file_actions,
        "runtime_guard_file_mutation",
        allow_file_mutation,
    )
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = json.loads(await runtime_generate_document_file(
        entity_id="entity_1",
        user_id="user_1",
        conversation_id="conversation_1",
        name="report.md",
        content="# New report\n",
        file_type="md",
    ))

    assert "rolled back" in result["error"]
    with open(target, "rb") as file:
        assert file.read() == b"old report"


@pytest.mark.asyncio
async def test_runtime_generate_document_file_builds_response_before_commit(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime import file_actions, generated_files
    from packages.core.services import knowledge_sync

    class FakeDB:
        commits = 0
        rollbacks = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    async def allow_file_mutation(**_kwargs):
        return None

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=False,
        )

    async def capture_metadata(_target):
        assert fake_db.commits == 0
        return {"source_sha256": "source_hash", "mtime_ns": 123}

    async def capture_document(db, *, entity_id, document_id):
        assert db is fake_db
        assert fake_db.commits == 0
        assert entity_id == "entity_1"
        assert document_id == "doc_1"
        return SimpleNamespace(
            id="doc_1",
            name="report.md",
            file_type="md",
            file_size=13,
            mime_type="text/markdown",
            source="ai_generated",
            vector_status="pending",
            created_at=None,
            folder_id=None,
            fs_path="report.md",
        )

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(file_actions, "runtime_get_document_for_entity", capture_document)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", successful_sync)
    monkeypatch.setattr(generated_files, "runtime_generated_file_metadata", capture_metadata)

    result = json.loads(await runtime_generate_document_file(
        entity_id="entity_1",
        user_id="user_1",
        conversation_id="conversation_1",
        name="report.md",
        content="# New report\n",
        file_type="md",
    ))

    assert result["created"] is True
    assert result["document"]["document_id"] == "doc_1"
    assert result["document"]["source_sha256"] == "source_hash"
    assert result["document"]["mtime_ns"] == 123
    assert fake_db.commits == 1
    assert fake_db.rollbacks == 0


@pytest.mark.asyncio
async def test_runtime_generate_document_file_rolls_back_when_response_metadata_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime import file_actions, generated_files
    from packages.core.services import knowledge_sync

    target = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old report")

    class FakeDB:
        commits = 0
        rollbacks = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    async def allow_file_mutation(**_kwargs):
        return None

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=False,
        )

    async def fail_metadata(_target):
        raise OSError("metadata extraction failed")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", successful_sync)
    monkeypatch.setattr(generated_files, "runtime_generated_file_metadata", fail_metadata)

    result = json.loads(await runtime_generate_document_file(
        entity_id="entity_1",
        user_id="user_1",
        conversation_id="conversation_1",
        name="report.md",
        content="# New report\n",
        file_type="md",
    ))

    assert result["error"] == "Document was not committed: metadata extraction failed"
    assert fake_db.commits == 0
    assert fake_db.rollbacks == 1
    with open(target, "rb") as file:
        assert file.read() == b"old report"


@pytest.mark.asyncio
async def test_generate_file_restores_prior_bytes_when_projection_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync

    target = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old report")

    async def allow_file_mutation(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = json.loads(
        await _generate_file(
            "entity_1",
            kind="document",
            name="report.md",
            content="new report",
            user_id="user_1",
        )
    )

    assert "storage limit" in result["error"]
    with open(target, "rb") as file:
        assert file.read() == b"old report"


@pytest.mark.asyncio
async def test_patch_file_restores_prior_bytes_when_projection_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync

    target = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old report")

    async def allow_file_access(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = json.loads(
        await file_tools._patch_file(
            "entity_1",
            path="report.md",
            user_id="user_1",
            operations=[{"op": "text.replace", "old_text": "old", "new_text": "new"}],
        )
    )

    assert result["edited"] is False
    assert result["knowledge_sync_reason"] == "storage_limit"
    with open(target, "rb") as file:
        assert file.read() == b"old report"


@pytest.mark.asyncio
async def test_generate_file_uses_only_runtime_identity_for_permission_and_provenance(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync

    class FakeDB:
        committed = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            return None

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    guard_calls = []
    sync_calls = []

    async def capture_guard(**kwargs):
        guard_calls.append(kwargs)
        return None

    async def capture_sync(**kwargs):
        sync_calls.append(kwargs)
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
        )

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_get_document_for_entity", AsyncMock(return_value=None))
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", capture_guard)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", capture_guard)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", capture_sync)

    result = json.loads(
        await _generate_file(
            "entity_1",
            kind="document",
            name="report.md",
            content="trusted",
            agent_id="forged_agent",
            _user_id_from_context="trusted_user",
            _agent_id_from_context="trusted_agent",
        )
    )

    assert result["created"] is True
    assert guard_calls[0]["user_id"] == "trusted_user"
    assert sync_calls[0]["created_by"] == "trusted_user"
    assert sync_calls[0]["user_id"] == "trusted_user"
    assert sync_calls[0]["agent_id"] == "trusted_agent"


@pytest.mark.asyncio
async def test_generate_file_rolls_back_when_response_metadata_cannot_be_built(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync

    class FakeDB:
        commits = 0
        rollbacks = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    async def allow_file_mutation(**_kwargs):
        return None

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
        )

    def fail_metadata(*_args, **_kwargs):
        raise OSError("metadata extraction failed")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(file_tools, "_file_meta", fail_metadata)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", successful_sync)

    result = json.loads(
        await _generate_file(
            "entity_1",
            kind="document",
            name="report.md",
            content="report text",
            _user_id_from_context="trusted_user",
        )
    )

    assert result["error"] == "Document was not committed: metadata extraction failed"
    assert fake_db.commits == 0
    assert fake_db.rollbacks == 1
    assert not os.path.exists(
        os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md"),
    )


@pytest.mark.asyncio
async def test_generate_file_retains_durable_cleanup_intent_when_backup_cleanup_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync, workspace_artifact_purge
    from packages.core.services.entity_fs import EntityFileRollbackSnapshot

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "report.md")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"old")

    class FakeDB:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    enqueued = []
    cleared = []

    async def allow_file_mutation(**_kwargs):
        return None

    async def successful_sync(**_kwargs):
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
            created=False,
        )

    async def capture_enqueue(_db, entity_id, paths, *, target_kind):
        enqueued.append((entity_id, set(paths), target_kind))
        return set(paths)

    async def capture_clear(_db, entity_id, paths):
        cleared.append((entity_id, set(paths)))

    def fail_cleanup(_self):
        raise OSError("unlink unavailable")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", successful_sync)
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        capture_enqueue,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "clear_artifact_cleanup_jobs",
        capture_clear,
    )
    monkeypatch.setattr(EntityFileRollbackSnapshot, "commit", fail_cleanup)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_get_document_for_entity", AsyncMock(return_value=None))

    result = json.loads(
        await _generate_file(
            "entity_1",
            kind="document",
            name="report.md",
            content="new",
            _user_id_from_context="trusted_user",
        )
    )

    assert result["created"] is True
    assert open(target, "rb").read() == b"new"
    assert len(enqueued) == 1
    entity_id, cleanup_paths, target_kind = enqueued[0]
    assert entity_id == "entity_1"
    assert target_kind == workspace_artifact_purge.ARTIFACT_CLEANUP_KIND_FILE
    assert len(cleanup_paths) == 1
    cleanup_path = next(iter(cleanup_paths))
    assert cleanup_path.startswith(".report.md.tmp-")
    assert os.path.isfile(os.path.join(entity_root, cleanup_path))
    assert cleared == []


@pytest.mark.asyncio
async def test_delete_file_restores_path_when_knowledge_trash_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync

    target = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"keep me")

    async def allow_file_access(**_kwargs):
        return None

    async def fail_trash(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(knowledge_sync, "trash_path", fail_trash)

    result = json.loads(await file_tools._delete_file(
        "entity_1",
        path="report.md",
        user_id="user_1",
    ))

    assert "database unavailable" in result["error"]
    with open(target, "rb") as file:
        assert file.read() == b"keep me"


@pytest.mark.parametrize("target_kind", ["file", "directory"])
@pytest.mark.asyncio
async def test_delete_file_commits_physical_and_knowledge_retirement(
    fs_settings,
    monkeypatch,
    target_kind,
):
    from packages.core import database
    from packages.core.ai.tools import file_tools
    from packages.core.services import (
        knowledge_sync,
        tool_cache_version,
        workspace_artifact_purge,
    )

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "retire-me")
    os.makedirs(entity_root, exist_ok=True)
    if target_kind == "file":
        with open(target, "wb") as file:
            file.write(b"retire me")
    else:
        os.mkdir(target)

    class FakeDB:
        committed = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            return None

    fake_db = FakeDB()

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *_exc):
            return None

    trash_calls = []

    async def allow_file_access(**_kwargs):
        return None

    async def successful_trash(*_args, **kwargs):
        trash_calls.append(kwargs)
        return True

    async def no_cache_bump(*_args, **_kwargs):
        return None

    async def persist_cleanup_intents(*_args, **_kwargs):
        return set()

    async def clear_cleanup_intents(*_args, **_kwargs):
        return None

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(knowledge_sync, "trash_path", successful_trash)
    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_cache_bump)
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        persist_cleanup_intents,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "clear_artifact_cleanup_jobs",
        clear_cleanup_intents,
    )

    result = json.loads(await file_tools._delete_file(
        "entity_1",
        path="retire-me",
        user_id="user_1",
    ))

    assert result["deleted"] is True
    assert result["type"] == target_kind
    assert fake_db.committed is True
    assert trash_calls == [{
        "is_directory": target_kind == "directory",
        "db": fake_db,
    }]
    assert not os.path.exists(target)
    assert not [
        name for name in os.listdir(entity_root) if name.startswith(".retire-me.tmp-")
    ]


@pytest.mark.asyncio
async def test_delete_file_retains_durable_cleanup_intent_when_backup_cleanup_fails(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.tools import file_tools
    from packages.core.services import knowledge_sync, workspace_artifact_purge
    from packages.core.services.entity_fs import EntityPathDeleteSnapshot

    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1")
    target = os.path.join(entity_root, "retire-me.txt")
    os.makedirs(entity_root, exist_ok=True)
    with open(target, "wb") as file:
        file.write(b"retire me")

    class FakeDB:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    enqueued = []
    cleared = []

    async def allow_file_access(**_kwargs):
        return None

    async def successful_trash(*_args, **_kwargs):
        return True

    async def capture_enqueue(_db, entity_id, paths, *, target_kind):
        enqueued.append((entity_id, set(paths), target_kind))
        return set(paths)

    async def capture_clear(_db, entity_id, paths):
        cleared.append((entity_id, set(paths)))

    def fail_cleanup(_self):
        raise OSError("unlink unavailable")

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr("packages.core.ai.runtime.file_actions.runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(knowledge_sync, "trash_path", successful_trash)
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        capture_enqueue,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "clear_artifact_cleanup_jobs",
        capture_clear,
    )
    monkeypatch.setattr(EntityPathDeleteSnapshot, "commit", fail_cleanup)

    result = json.loads(await file_tools._delete_file(
        "entity_1",
        path="retire-me.txt",
        _user_id_from_context="trusted_user",
    ))

    assert result["deleted"] is True
    assert not os.path.exists(target)
    assert len(enqueued) == 1
    entity_id, cleanup_paths, target_kind = enqueued[0]
    assert entity_id == "entity_1"
    assert target_kind == workspace_artifact_purge.ARTIFACT_CLEANUP_KIND_FILE
    assert len(cleanup_paths) == 1
    cleanup_path = next(iter(cleanup_paths))
    assert cleanup_path.startswith(".retire-me.txt.tmp-")
    assert os.path.isfile(os.path.join(entity_root, cleanup_path))
    assert cleared == []


@pytest.mark.asyncio
async def test_save_sandbox_file_rolls_back_failed_knowledge_projection(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.tools import sandbox_file_tools
    from packages.core.services import knowledge_sync

    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")

    class FakeSandboxClient:
        async def read_file_base64(self, **_kwargs):
            return SimpleNamespace(
                content_base64=base64.b64encode(b"result").decode("ascii"),
            )

        async def close(self):
            return None

    async def allow_file_mutation(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(sandbox_file_tools, "_get_client", lambda: FakeSandboxClient())
    monkeypatch.setattr(
        sandbox_file_tools,
        "runtime_guard_file_mutation",
        allow_file_mutation,
    )
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = json.loads(await sandbox_file_tools._save_sandbox_file(
        "entity_1",
        filename="result.txt",
        sandbox_id="sandbox_1",
        file_path="/tmp/result.txt",
        user_id="user_1",
    ))

    assert result["saved"] is False
    assert result["knowledge_sync_reason"] == "storage_limit"
    assert not os.path.exists(
        os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "result.txt"),
    )


@pytest.mark.asyncio
async def test_save_sandbox_file_uses_runtime_identity_and_canonical_document_metadata(
    fs_settings,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.tools import sandbox_file_tools
    from packages.core.services import knowledge_sync

    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")

    class FakeSandboxClient:
        async def read_file_base64(self, **_kwargs):
            return SimpleNamespace(
                content_base64=base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode("ascii"),
            )

        async def close(self):
            return None

    class FakeDB:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDB()

        async def __aexit__(self, *_exc):
            return None

    guard_calls = []
    sync_calls = []

    async def capture_guard(**kwargs):
        guard_calls.append(kwargs)
        return None

    async def capture_sync(**kwargs):
        sync_calls.append(kwargs)
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
            name="result.png",
            file_size=15,
            mime_type="image/png",
            fs_path="result.txt",
        )

    monkeypatch.setattr(database, "async_session", lambda: FakeSessionContext())
    monkeypatch.setattr(sandbox_file_tools, "_get_client", lambda: FakeSandboxClient())
    monkeypatch.setattr(sandbox_file_tools, "runtime_guard_file_mutation", capture_guard)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", capture_sync)

    result = json.loads(await sandbox_file_tools._save_sandbox_file(
        "entity_1",
        filename="result.txt",
        sandbox_id="sandbox_1",
        file_path="/tmp/result.txt",
        user_id="forged_user",
        agent_id="forged_agent",
        _user_id_from_context="trusted_user",
        _agent_id_from_context="trusted_agent",
    ))

    assert result["saved"] is True
    assert result["name"] == "result.png"
    assert result["mime_type"] == "image/png"
    assert guard_calls[0]["user_id"] == "trusted_user"
    assert sync_calls[0]["created_by"] == "trusted_user"
    assert sync_calls[0]["user_id"] == "trusted_user"
    assert sync_calls[0]["agent_id"] == "trusted_agent"


@pytest.mark.asyncio
async def test_sandbox_save_result_rolls_back_failed_knowledge_projection(
    fs_settings,
    monkeypatch,
):
    from packages.core.ai.tools import sandbox_tools
    from packages.core.services import knowledge_sync

    class FakeSandboxClient:
        async def read_file_base64(self, **_kwargs):
            return SimpleNamespace(
                content_base64=base64.b64encode(b"result").decode("ascii"),
            )

        async def close(self):
            return None

    async def get_sandbox_client(_sandbox_id: str):
        return FakeSandboxClient()

    async def allow_file_mutation(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(
        sandbox_tools,
        "_get_client_for_sandbox",
        get_sandbox_client,
    )
    monkeypatch.setattr(
        sandbox_tools,
        "runtime_guard_file_mutation",
        allow_file_mutation,
    )
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = await sandbox_tools._sandbox_save_result(
        entity_id="entity_1",
        sandbox_id="sandbox_1",
        file_path="/tmp/result.txt",
        filename="result.txt",
        user_id="user_1",
    )

    assert "rolled back: storage_limit" in result
    assert not os.path.exists(
        os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "result.txt"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_name", "source_format", "source_mime", "requested_name", "expected_name"),
    [
        (
            "legacy-deck.ppt",
            "ppt",
            "application/vnd.ms-powerpoint",
            "legacy-deck.pptx",
            "legacy-deck.pptx",
        ),
        (
            "legacy-deck.dps",
            "dps",
            "application/vnd.ms-powerpoint",
            "legacy-deck.dps.pptx",
            "legacy-deck.pptx",
        ),
        (
            "renamed-deck.json",
            "ppt",
            "application/json",
            "renamed-deck.json.pptx",
            "renamed-deck.pptx",
        ),
    ],
)
async def test_save_document_file_migrates_legacy_office_name_and_path(
    fs_settings,
    monkeypatch,
    source_name,
    source_format,
    source_mime,
    requested_name,
    expected_name,
):
    from packages.core.services import document_service, version_service

    document = SimpleNamespace(
        id="doc_legacy_ppt",
        entity_id="entity_1",
        name=source_name,
        fs_path=f"presentations/{source_name}",
        file_type=source_format,
        mime_type=source_mime,
        metadata_={},
        vector_status="ready",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(version_service, "create_version", no_op)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, document.entity_id)
    old_path = os.path.join(entity_root, document.fs_path)
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    with open(old_path, "wb") as file:
        file.write(b"legacy")

    replacement = b"PK\x03\x04editable-pptx"
    result = await document_service.save_document_file(
        FakeDb(),
        document.id,
        document.entity_id,
        replacement,
        filename=requested_name,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )

    assert result is not None and result.replayed is False
    assert document.name == expected_name
    assert document.fs_path == f"presentations/{expected_name}"
    assert document.file_type == "pptx"
    assert document.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert not os.path.exists(old_path)
    with open(os.path.join(entity_root, document.fs_path), "rb") as file:
        assert file.read() == replacement


@pytest.mark.asyncio
async def test_editor_save_allows_compatibility_ooxml_package_rebuild(
    fs_settings, monkeypatch,
):
    import io
    import zipfile

    from packages.core.services import document_service, version_service

    document = SimpleNamespace(
        id="doc_native_pptx",
        entity_id="entity_1",
        name="renamed-deck.bin",
        fs_path="presentations/native-deck.pptx",
        file_type="pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        metadata_={},
        vector_status="ready",
    )

    async def get_document(*_args, **_kwargs):
        return document

    async def no_op(*_args, **_kwargs):
        return None

    class FakeDb:
        async def flush(self):
            return None

        async def commit(self):
            return None

    def pptx_bytes(*, include_custom_part: bool, slide_text: str) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            archive.writestr("ppt/slides/slide1.xml", f"<p:sld>{slide_text}</p:sld>")
            if include_custom_part:
                archive.writestr("customXml/preserved.xml", "<preserve>unknown</preserve>")
        return output.getvalue()

    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(version_service, "create_version", no_op)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, document.entity_id)
    full_path = os.path.join(entity_root, document.fs_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    original = pptx_bytes(include_custom_part=True, slide_text="original")
    with open(full_path, "wb") as file:
        file.write(original)

    rebuilt = pptx_bytes(include_custom_part=False, slide_text="edited")
    result = await document_service.save_document_file(
        FakeDb(),
        document.id,
        document.entity_id,
        rebuilt,
        filename=document.name,
        mime_type=document.mime_type,
        save_session_id="editor-session",
        save_sequence=1,
    )

    assert result is not None and result.replayed is False
    with open(full_path, "rb") as file:
        assert file.read() == rebuilt


@pytest.mark.asyncio
async def test_filesystem_write_requires_document_edit_access(
    client, db_session, fs_settings,
):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.permission import (
        Capability,
        ResourceGrant,
        ResourceType,
        SubjectType,
    )
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fswriteowner",
            "email": "fswriteowner@test.com",
            "password": "pass123",
            "entity_name": "FS Write Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    member = User(
        entity_id=entity_id,
        email="fswritemember@test.com",
        display_name="fswritemember",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(member)
    await db_session.flush()
    token = create_access_token(member.id, entity_id, member.role)
    headers = {"Authorization": f"Bearer {token}"}

    path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "site", "styles.css")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write("body { color: black; }")
    document = Document(
        entity_id=entity_id,
        name="styles.css",
        fs_path="site/styles.css",
        file_type="css",
        mime_type="text/css",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    db_session.add(document)
    await db_session.commit()

    denied = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "site/styles.css", "content": "body { color: red; }"},
    )
    assert denied.status_code == 403, denied.text
    with open(path, encoding="utf-8") as file:
        assert file.read() == "body { color: black; }"

    db_session.add(ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=document.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.EDIT],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    ))
    await db_session.commit()

    allowed = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "site/styles.css", "content": "body { color: green; }"},
    )
    assert allowed.status_code == 200, allowed.text
    with open(path, encoding="utf-8") as file:
        assert file.read() == "body { color: green; }"


@pytest.mark.asyncio
async def test_filesystem_mutations_reject_deleted_workspace_even_for_admin(
    client,
    fs_settings,
):
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsdeletedworkspaceadmin",
            "email": "fsdeletedworkspaceadmin@test.com",
            "password": "pass123",
            "entity_name": "Deleted Workspace FS Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Deleted filesystem scope"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace = workspace_response.json()
    artifact_folder_id = workspace["artifact_folder_id"]

    physical_path = f"Workspaces/_by_id/{artifact_folder_id}/report.md"
    logical_dir = f"Workspaces/{workspace['name']}"
    created = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": physical_path, "content": "before deletion"},
    )
    assert created.status_code == 200, created.text
    source = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "move-source.txt", "content": "stay outside"},
    )
    assert source.status_code == 200, source.text

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    denied_write = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": physical_path, "content": "must not replace"},
    )
    denied_mkdir = await client.post(
        "/api/v1/fs/mkdir",
        headers=headers,
        json={"path": f"{logical_dir}/new-directory"},
    )
    denied_delete = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": physical_path},
    )
    denied_move = await client.post(
        "/api/v1/fs/move",
        headers=headers,
        json={
            "src": "move-source.txt",
            "dest": f"{logical_dir}/moved.txt",
        },
    )

    assert denied_write.status_code == 403, denied_write.text
    assert denied_mkdir.status_code == 403, denied_mkdir.text
    assert denied_delete.status_code == 403, denied_delete.text
    assert denied_move.status_code == 403, denied_move.text
    absolute_report = os.path.join(
        fs_settings.MANOR_FS_ROOT,
        entity_id,
        *physical_path.split("/"),
    )
    with open(absolute_report, encoding="utf-8") as file:
        assert file.read() == "before deletion"
    assert os.path.isfile(os.path.join(
        fs_settings.MANOR_FS_ROOT,
        entity_id,
        "move-source.txt",
    ))

    restored = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    allowed_write = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": physical_path, "content": "after restore"},
    )
    assert allowed_write.status_code == 200, allowed_write.text


@pytest.mark.asyncio
async def test_filesystem_write_serializes_with_workspace_delete(
    client,
    fs_settings,
):
    import packages.core.database as db_module
    from packages.core.services.entity_service import soft_delete_workspace

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsworkspacedeleterace",
            "email": "fsworkspacedeleterace@test.com",
            "password": "pass123",
            "entity_name": "Workspace FS Delete Race Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Filesystem delete race"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace = workspace_response.json()
    rel_path = (
        f"Workspaces/_by_id/{workspace['artifact_folder_id']}/race-report.md"
    )
    created = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": rel_path, "content": "before deletion"},
    )
    assert created.status_code == 200, created.text

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace["id"],
            entity_id,
        ) is True
        request_task = asyncio.create_task(client.post(
            "/api/v1/fs/write",
            headers=headers,
            json={"path": rel_path, "content": "must not cross deletion"},
        ))
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 403, response.text
    absolute_path = os.path.join(
        fs_settings.MANOR_FS_ROOT,
        entity_id,
        *rel_path.split("/"),
    )
    with open(absolute_path, encoding="utf-8") as file:
        assert file.read() == "before deletion"


@pytest.mark.asyncio
async def test_mutable_display_name_never_grants_document_ownership(monkeypatch):
    from unittest.mock import AsyncMock

    from apps.api.routers import documents
    from packages.core.services import document_access, filesystem_access

    owner = SimpleNamespace(id="owner", display_name="Shared Label")
    impostor = SimpleNamespace(
        id="impostor",
        entity_id="entity",
        email="immutable-impostor@test.com",
        display_name="Shared Label",
        role="member",
    )
    document = SimpleNamespace(
        entity_id="entity",
        created_by=owner.display_name,
        owner_id=owner.id,
    )
    db = SimpleNamespace()
    monkeypatch.setattr(
        document_access,
        "_resolve_current_actor_for_entity",
        AsyncMock(return_value=SimpleNamespace(role="member")),
    )
    monkeypatch.setattr(
        document_access,
        "effective_document_capabilities_for_user",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        filesystem_access,
        "user_is_effective_entity_admin",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        filesystem_access,
        "user_has_document_capability",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        documents,
        "user_is_effective_entity_admin",
        AsyncMock(return_value=False),
    )

    assert await document_access.user_can_edit_document(db, document, user=impostor) is False
    assert await filesystem_access._user_can_delete_document(
        db,
        user=impostor,
        document=document,
    ) is False
    assert await documents._can_manage_document(db, impostor, document) is False

    legacy_id_document = SimpleNamespace(owner_id=None, created_by=impostor.id)
    assert await documents._can_manage_document(db, impostor, legacy_id_document) is True

    stale_admin = SimpleNamespace(
        id="stale-admin",
        entity_id="entity",
        email="stale-admin@test.com",
        display_name="Stale Admin",
        role="admin",
    )
    assert await documents._can_manage_document(db, stale_admin, document) is False


@pytest.mark.asyncio
async def test_custom_staff_role_overrides_stale_legacy_admin_permissions(db_session):
    from fastapi import HTTPException

    from apps.api.routers.workspaces import _require_workspace_manage
    from packages.core.models.base import generate_ulid
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.models.workspace import Workspace
    from packages.core.permissions import (
        Permission,
        effective_user_has_permission,
        effective_user_role_name,
        user_effective_permission_keys,
        user_has_effective_permission,
    )
    from packages.core.services.auth_service import hash_password
    from packages.core.services.document_access import DocumentAccessContext, _resolve_user_role
    from packages.core.services.filesystem_access import (
        FilesystemAccessDenied,
        require_directory_write_access,
    )
    from packages.core.services.workspace_access import (
        user_can_control_workspace_run,
        user_can_manage_workspace,
        user_can_write_workspace_id,
        user_can_write_workspace_artifacts,
        user_writable_workspace_ids,
    )

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Demoted admin test")
    user = User(
        entity_id=entity_id,
        email=f"demoted-admin-{entity_id.lower()}@test.com",
        display_name="Demoted Admin",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    role = StaffRole(
        entity_id=entity_id,
        name="Restricted",
        permissions=[],
        status="active",
    )
    workspace = Workspace(entity_id=entity_id, name="Restricted Workspace")
    db_session.add_all([entity, user, role, workspace])
    await db_session.flush()
    membership = UserMembership(
        user_id=user.id,
        entity_id=entity_id,
        role="admin",
        status="active",
        is_primary=True,
    )
    staff = Staff(
        entity_id=entity_id,
        kind="employee",
        name="Demoted Admin",
        email=user.email,
        user_id=user.id,
        role_id=role.id,
        status="active",
    )
    db_session.add_all([membership, staff])
    await db_session.flush()

    assert Permission.ADMIN_SETTINGS.value not in await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    )
    assert not await user_has_effective_permission(
        db_session,
        user.id,
        entity_id,
        user.role,
        Permission.ADMIN_SETTINGS,
    )
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )

    assert await _resolve_user_role(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) == "restricted"
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert access_ctx.role == "restricted"
    assert access_ctx.is_admin is False
    assert access_ctx.can_read_entity_documents is False

    with pytest.raises(FilesystemAccessDenied, match="cannot create or upload"):
        await require_directory_write_access(
            db_session,
            user=user,
            rel_path="",
        )
    assert not await user_can_write_workspace_id(
        db_session,
        workspace_id=workspace.id,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert await user_writable_workspace_ids(
        db_session,
        entity_id=entity_id,
        workspace_ids={workspace.id},
        user_id=user.id,
        role=user.role,
    ) == set()
    assert not await user_can_write_workspace_artifacts(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        entity_role=user.role,
    )
    assert not await user_can_manage_workspace(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        entity_role=user.role,
    )
    assert not await user_can_control_workspace_run(
        db_session,
        run=SimpleNamespace(
            entity_id=entity_id,
            workspace_id=workspace.id,
            started_by="another-user",
        ),
        user_id=user.id,
        entity_role=user.role,
    )
    with pytest.raises(HTTPException):
        await _require_workspace_manage(db_session, workspace.id, user)

    # Removing the configured role is an explicit demotion. The active Staff
    # row remains authoritative and must not reactivate the stale User.role.
    staff.role_id = None
    await db_session.flush()

    assert await effective_user_role_name(db_session, user) == ""
    assert await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    ) == set()
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )
    assert await _resolve_user_role(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) is None
    assert not await user_can_manage_workspace(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        entity_role=user.role,
    )


@pytest.mark.asyncio
async def test_custom_staff_role_document_read_uses_permission_keys(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import Entity, User
    from packages.core.permissions import Permission
    from packages.core.services.auth_service import hash_password
    from packages.core.services.document_access import (
        DocumentAccessContext,
        user_can_read_document,
        user_can_read_folder,
    )

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Knowledge Reader Entity")
    user = User(
        entity_id=entity_id,
        email="knowledge-reader@test.com",
        display_name="Knowledge Reader",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    role = StaffRole(
        entity_id=entity_id,
        name="Knowledge Reader",
        permissions=[Permission.DOCS_READ.value],
        status="active",
    )
    folder = DocumentFolder(
        entity_id=entity_id,
        name="Shared",
        visibility="entity",
    )
    db_session.add_all([entity, user, role, folder])
    await db_session.flush()
    document = Document(
        entity_id=entity_id,
        name="brief.md",
        file_type="md",
        source="upload",
        visibility="entity",
        folder_id=folder.id,
    )
    db_session.add_all([
        Staff(
            entity_id=entity_id,
            kind="employee",
            name=user.display_name,
            email=user.email,
            user_id=user.id,
            role_id=role.id,
            status="active",
        ),
        document,
    ])
    await db_session.flush()

    assert await user_can_read_folder(
        db_session,
        folder,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert await user_can_read_document(
        db_session,
        document,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    await access_ctx.preload_documents(db_session, [document])
    assert access_ctx.is_admin is False
    assert await access_ctx.can_read_folder(db_session, folder)
    assert await access_ctx.can_read_document(db_session, document)


@pytest.mark.asyncio
async def test_custom_staff_role_workspace_read_controls_linked_documents(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace
    from packages.core.permissions import Permission
    from packages.core.services.auth_service import hash_password
    from packages.core.services.document_access import (
        DocumentAccessContext,
        user_can_read_document,
    )
    from packages.core.services.workspace_access import (
        filter_workspaces_for_user,
        readable_workspace_ids_for_user,
        user_can_read_workspace_id,
        user_readable_workspace_ids,
    )

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Workspace Knowledge Reader Entity")
    user = User(
        entity_id=entity_id,
        email="workspace-knowledge-reader@test.com",
        display_name="Workspace Knowledge Reader",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    role = StaffRole(
        entity_id=entity_id,
        name="Workspace Knowledge Reader",
        permissions=[Permission.DOCS_READ.value],
        status="active",
    )
    workspace = Workspace(
        entity_id=entity_id,
        name="Shared Knowledge",
        settings={"access_mode": "entity_visible"},
    )
    document = Document(
        entity_id=entity_id,
        name="workspace-brief.md",
        file_type="md",
        source="upload",
        visibility="entity",
    )
    db_session.add_all([entity, user, role, workspace, document])
    await db_session.flush()
    group = DocumentGroup(
        entity_id=entity_id,
        name="Workspace Knowledge",
        workspace_id=workspace.id,
    )
    db_session.add_all([
        Staff(
            entity_id=entity_id,
            kind="employee",
            name=user.display_name,
            email=user.email,
            user_id=user.id,
            role_id=role.id,
            status="active",
        ),
        group,
    ])
    await db_session.flush()
    db_session.add(DocumentGroupMember(group_id=group.id, document_id=document.id))
    await db_session.flush()

    assert not await user_can_read_workspace_id(
        db_session,
        workspace_id=workspace.id,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert await filter_workspaces_for_user(
        db_session,
        workspaces=[workspace],
        user=user,
    ) == []
    assert workspace.id not in await user_readable_workspace_ids(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert workspace.id not in await readable_workspace_ids_for_user(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert not await user_can_read_document(
        db_session,
        document,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    await access_ctx.preload_documents(db_session, [document])
    assert not await access_ctx.can_read_document(db_session, document)

    role.permissions = [
        Permission.DOCS_READ.value,
        Permission.WORKSPACES_READ.value,
    ]
    await db_session.flush()

    assert await user_can_read_workspace_id(
        db_session,
        workspace_id=workspace.id,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert await filter_workspaces_for_user(
        db_session,
        workspaces=[workspace],
        user=user,
    ) == [workspace]
    assert workspace.id in await user_readable_workspace_ids(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert workspace.id in await readable_workspace_ids_for_user(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert await user_can_read_document(
        db_session,
        document,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    await access_ctx.preload_documents(db_session, [document])
    assert await access_ctx.can_read_document(db_session, document)

    document.visibility = "workspace"
    role.permissions = [Permission.WORKSPACES_READ.value]
    await db_session.flush()

    assert await user_can_read_workspace_id(
        db_session,
        workspace_id=workspace.id,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    assert not await user_can_read_document(
        db_session,
        document,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    await access_ctx.preload_documents(db_session, [document])
    assert not await access_ctx.can_read_document(db_session, document)


@pytest.mark.asyncio
async def test_cross_entity_staff_role_cannot_grant_document_read(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import User
    from packages.core.permissions import (
        Permission,
        user_has_permission,
        user_staff_role_summary,
    )
    from packages.core.services.auth_service import hash_password
    from packages.core.services.document_access import DocumentAccessContext

    entity_id = generate_ulid()
    foreign_entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email="foreign-role-reader@test.com",
        display_name="Foreign Role Reader",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    foreign_role = StaffRole(
        entity_id=foreign_entity_id,
        name="Foreign Knowledge Reader",
        permissions=[Permission.DOCS_READ.value],
        status="active",
    )
    db_session.add_all([user, foreign_role])
    await db_session.flush()
    db_session.add(Staff(
        entity_id=entity_id,
        kind="employee",
        name=user.display_name,
        email=user.email,
        user_id=user.id,
        role_id=foreign_role.id,
        status="active",
    ))
    await db_session.flush()

    assert await user_staff_role_summary(
        db_session,
        user.id,
        entity_id,
    ) == (foreign_role.id, None, [])
    assert not await user_has_permission(
        db_session,
        user.id,
        entity_id,
        Permission.DOCS_READ,
    )
    access_ctx = await DocumentAccessContext.load(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role="restricted",
    )
    assert access_ctx.role is None
    assert access_ctx.can_read_entity_documents is False


@pytest.mark.asyncio
async def test_filesystem_new_file_records_immutable_creator_and_owner(
    client, db_session, fs_settings,
):
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.user import User

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsimmutablecreator",
            "email": "fsimmutablecreator@test.com",
            "password": "pass123",
            "entity_name": "Immutable Creator Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    response = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "identity.md", "content": "immutable"},
    )
    assert response.status_code == 200, response.text
    user = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    document = await db_session.scalar(select(Document).where(
        Document.entity_id == entity_id,
        Document.fs_path == "identity.md",
    ))
    assert user is not None and document is not None
    assert document.created_by == user.id
    assert document.owner_id == user.id


@pytest.mark.asyncio
async def test_filesystem_write_fails_closed_for_unindexed_existing_files(
    client, db_session, fs_settings,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsrawowner",
            "email": "fsrawowner@test.com",
            "password": "pass123",
            "entity_name": "FS Raw Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    member = User(
        entity_id=entity_id,
        email="fsrawmember@test.com",
        display_name="fsrawmember",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(member)
    await db_session.commit()
    token = create_access_token(member.id, entity_id, member.role)
    headers = {"Authorization": f"Bearer {token}"}

    path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "site", "unindexed.css")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write("original")

    denied = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "site/unindexed.css", "content": "overwritten"},
    )
    assert denied.status_code == 403, denied.text
    with open(path, encoding="utf-8") as file:
        assert file.read() == "original"


@pytest.mark.asyncio
async def test_filesystem_write_restores_existing_file_when_knowledge_sync_fails(
    client, fs_settings, monkeypatch,
):
    from packages.core.services import knowledge_sync

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fswriterollback",
            "email": "fswriterollback@test.com",
            "password": "pass123",
            "entity_name": "FS Write Rollback Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]
    path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "rollback.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write("original")

    async def fail_sync(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)
    response = await client.post(
        "/api/v1/fs/write",
        headers=headers,
        json={"path": "rollback.txt", "content": "replacement"},
    )

    assert response.status_code == 500, response.text
    with open(path, encoding="utf-8") as file:
        assert file.read() == "original"


@pytest.mark.asyncio
async def test_filesystem_upload_rolls_back_when_knowledge_sync_fails(
    client, fs_settings, monkeypatch,
):
    from packages.core.services import knowledge_sync

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsuploadrollback",
            "email": "fsuploadrollback@test.com",
            "password": "pass123",
            "entity_name": "FS Upload Rollback Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]

    async def fail_sync(**_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)
    response = await client.post(
        "/api/v1/fs/upload",
        headers=headers,
        params={"path": "."},
        files={"file": ("rollback.txt", b"rollback", "text/plain")},
    )

    assert response.status_code == 500, response.text
    assert not os.path.exists(os.path.join(
        fs_settings.MANOR_FS_ROOT,
        entity_id,
        "rollback.txt",
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("fs_enabled", [False, True])
async def test_document_upload_invalidates_cache_after_projection_commit(
    client, fs_settings, monkeypatch, fs_enabled,
):
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    fs_settings.MANOR_FS_ENABLED = fs_enabled
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "documentcachecommit",
            "email": "documentcachecommit@test.com",
            "password": "pass123",
            "entity_name": "Document Cache Commit Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    observed_committed_rows: list[str | None] = []

    async def observe_cache_bump(observed_entity_id: str, namespace: str):
        assert observed_entity_id == entity_id
        assert namespace == "documents"
        async with async_session() as observer_db:
            observed_committed_rows.append(await observer_db.scalar(
                select(Document.id).where(
                    Document.entity_id == entity_id,
                    Document.name == "cache-visible.txt",
                )
            ))

    monkeypatch.setattr(
        tool_cache_version,
        "bump_tool_cache_version",
        observe_cache_bump,
    )
    response = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("cache-visible.txt", b"committed", "text/plain")},
    )

    assert response.status_code == 201, response.text
    assert len(observed_committed_rows) == 1
    assert observed_committed_rows[0] == response.json()["id"]


@pytest.mark.asyncio
async def test_url_import_invalidates_cache_after_placeholder_commit(
    client,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlcachecommit",
            "email": "urlcachecommit@test.com",
            "password": "pass123",
            "entity_name": "URL Cache Commit Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    observed_committed_rows: list[str | None] = []

    async def observe_cache_bump(observed_entity_id: str, namespace: str):
        assert observed_entity_id == entity_id
        assert namespace == "documents"
        async with async_session() as observer_db:
            observed_committed_rows.append(await observer_db.scalar(
                select(Document.id).where(
                    Document.entity_id == entity_id,
                    Document.name == "remote.html",
                )
            ))

    monkeypatch.setattr(
        tool_cache_version,
        "bump_tool_cache_version",
        observe_cache_bump,
    )
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    response = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/remote", "name": "remote.html"},
    )

    assert response.status_code == 201, response.text
    assert observed_committed_rows == [response.json()["id"]]


@pytest.mark.asyncio
async def test_url_import_dispatch_finishes_after_request_cancellation(monkeypatch):
    from apps.api.routers import documents
    from packages.core.models.document import VectorStatus
    from packages.core.tasks import ai_tasks

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    dispatched: list[tuple[str, str]] = []
    document = SimpleNamespace(
        id="doc-1",
        entity_id="entity-1",
        metadata_={},
        vector_status=VectorStatus.PENDING,
    )

    async def no_op(*_args, **_kwargs):
        return None

    async def create_placeholder(*_args, **_kwargs):
        return document

    async def blocking_commit():
        commit_started.set()
        await release_commit.wait()

    monkeypatch.setattr(documents, "_require_document_upload", no_op)
    monkeypatch.setattr(documents, "create_document", create_placeholder)
    monkeypatch.setattr(documents, "_invalidate_committed_document_cache", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda document_id, url: dispatched.append((document_id, url)),
    )
    db = SimpleNamespace(commit=blocking_commit)
    user = SimpleNamespace(
        id="user-1",
        entity_id="entity-1",
        email="user@example.com",
        display_name=None,
    )
    request = asyncio.create_task(
        documents.create_from_url(
            documents.CreateFromUrlRequest(
                url="https://example.com/cancelled",
                name="cancelled.html",
            ),
            _gate=None,
            user=user,
            db=db,
        )
    )
    await commit_started.wait()

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert dispatched == [("doc-1", "https://example.com/cancelled")]


@pytest.mark.asyncio
async def test_embedding_dispatch_finishes_after_request_cancellation(monkeypatch):
    from apps.api.routers import documents
    from packages.core.tasks import ai_tasks

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    dispatched: list[str] = []

    async def blocking_commit():
        commit_started.set()
        await release_commit.wait()

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(documents, "_invalidate_committed_document_cache", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda document_id: dispatched.append(document_id),
    )
    db = SimpleNamespace(commit=blocking_commit)
    request = asyncio.create_task(
        documents._finish_document_filesystem_mutation(
            documents._commit_document_and_dispatch_embeddings(
                db,
                "doc-1",
                "entity-1",
            )
        )
    )
    await commit_started.wait()

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert dispatched == ["doc-1"]


@pytest.mark.asyncio
async def test_ai_draft_launch_finishes_after_request_cancellation(monkeypatch):
    from apps.api.routers import documents
    from packages.core.models.document import VectorStatus

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    generated: list[str] = []
    document = SimpleNamespace(id="doc-1", vector_status=VectorStatus.PENDING)

    async def no_op(*_args, **_kwargs):
        return None

    async def create_placeholder(*_args, **_kwargs):
        return document

    async def response_for_user(*_args, **_kwargs):
        return {"id": document.id}

    async def generate_draft(**kwargs):
        generated.append(kwargs["doc_id"])

    async def blocking_commit():
        commit_started.set()
        await release_commit.wait()

    monkeypatch.setattr(documents, "_require_document_upload", no_op)
    monkeypatch.setattr(
        documents,
        "runtime_text_completion_platform_configured",
        lambda: True,
    )
    monkeypatch.setattr(documents, "create_document", create_placeholder)
    monkeypatch.setattr(documents, "_doc_resp_for_user", response_for_user)
    monkeypatch.setattr(documents, "_generate_ai_draft_content", generate_draft)
    monkeypatch.setattr(documents, "_invalidate_committed_document_cache", no_op)

    db = SimpleNamespace(flush=AsyncMock(), commit=blocking_commit)
    user = SimpleNamespace(
        id="user-1",
        entity_id="entity-1",
        email="user@example.com",
        display_name=None,
    )
    request = asyncio.create_task(
        documents.create_ai_draft(
            documents.AiDraftRequest(prompt="Create a launch plan"),
            _gate=None,
            user=user,
            db=db,
        )
    )
    await commit_started.wait()

    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0)
    assert generated == ["doc-1"]


@pytest.mark.asyncio
async def test_ai_draft_generation_does_not_write_after_placeholder_is_trashed(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.models.user import User
    from packages.core.tasks import ai_tasks
    from sqlalchemy import select

    dispatched: list[str] = []

    async def no_op(*_args, **_kwargs):
        return None

    async def generated_content(**_kwargs):
        return "Generated after trash"

    monkeypatch.setattr(
        documents,
        "generate_document_ai_draft_content",
        generated_content,
    )
    monkeypatch.setattr(documents, "_invalidate_committed_document_cache", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda doc_id, *_args, **_kwargs: dispatched.append(doc_id),
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trasheddraft",
            "email": "trasheddraft@test.com",
            "password": "pass123",
            "entity_name": "Trashed Draft Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    async with async_session() as db:
        owner = await db.scalar(
            select(User).where(User.email == "trasheddraft@test.com")
        )
        assert owner is not None
        document = Document(
            entity_id=entity_id,
            name="draft.md",
            file_type="md",
            mime_type="text/markdown",
            source="ai-draft",
            created_by=owner.id,
            owner_id=owner.id,
            is_trashed=False,
            vector_status=VectorStatus.GENERATING,
        )
        db.add(document)
        await db.commit()
        document_id = document.id

    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text

    await documents._generate_ai_draft_content(
        doc_id=document_id,
        entity_id=entity_id,
        user_id=registered.json()["user_id"],
        prompt="Generate a document",
        ext="md",
        original_name="draft.md",
        display_name="Draft Owner",
    )

    assert not os.path.exists(
        os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "draft.md")
    )

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.vector_status == VectorStatus.FAILED
        assert (
            row.metadata_["restore_blocked_reason"]
            == documents.AI_DRAFT_CANCELLED_BY_TRASH
        )

        # Reproduce rows trashed before the cancellation marker existed.
        row.vector_status = VectorStatus.PENDING
        row.metadata_ = {}
        await db.commit()

    restored = await client.post(
        f"/api/v1/documents/{document_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 409, restored.text
    assert "no content to restore" in restored.json()["detail"]
    assert dispatched == []

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path is None
        assert row.vector_status == VectorStatus.FAILED
        assert (
            row.metadata_["restore_blocked_reason"]
            == documents.AI_DRAFT_CANCELLED_BY_TRASH
        )


@pytest.mark.asyncio
async def test_ai_draft_render_failure_marks_the_placeholder_failed(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.tasks import ai_tasks

    dispatched: list[str] = []

    async def generated_content(**_kwargs):
        return "Generated presentation"

    async def fail_presentation_render(*_args, **_kwargs):
        raise RuntimeError("presentation renderer unavailable")

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        documents,
        "generate_document_ai_draft_content",
        generated_content,
    )
    monkeypatch.setattr(documents, "_generate_pptx_bytes", fail_presentation_render)
    monkeypatch.setattr(documents, "_invalidate_committed_document_cache", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda document_id: dispatched.append(document_id),
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "faileddraftrender",
            "email": "faileddraftrender@test.com",
            "password": "pass123",
            "entity_name": "Failed Draft Render Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    async with async_session() as db:
        document = Document(
            entity_id=entity_id,
            name="AI Draft.pptx",
            file_type="pptx",
            mime_type=documents.PPTX_MIME,
            source="ai-draft",
            vector_status=VectorStatus.GENERATING,
        )
        db.add(document)
        await db.commit()
        document_id = document.id

    await documents._generate_ai_draft_content(
        doc_id=document_id,
        entity_id=entity_id,
        user_id=registered.json()["user_id"],
        prompt="Generate a presentation",
        ext="pptx",
        original_name="AI Draft.pptx",
        display_name="Draft Owner",
    )

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.vector_status == VectorStatus.FAILED
        assert row.fs_path is None
    assert dispatched == []


def test_url_import_worker_uses_document_mutation_boundary_and_cache_version():
    from apps.api.routers import documents
    from packages.core.tasks import ai_tasks

    source = inspect.getsource(ai_tasks.fetch_and_index_url_document.run)
    assert "entity_filesystem_mutation_lock" in source
    assert "finish_entity_filesystem_mutation" in source
    assert "await db.rollback()" in source
    assert "bump_tool_cache_version" in source
    trash_source = inspect.getsource(documents.trash_one_document)
    assert "_document_filesystem_mutation" in trash_source
    assert "await db.commit()" in trash_source


@pytest.mark.asyncio
async def test_url_import_dispatch_failure_is_committed(client, monkeypatch):
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.tasks import ai_tasks

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlfailedcommit",
            "email": "urlfailedcommit@test.com",
            "password": "pass123",
            "entity_name": "URL Failed Commit Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        fail_dispatch,
    )
    response = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/failed", "name": "failed.html"},
    )

    assert response.status_code == 201, response.text
    async with async_session() as observer_db:
        row = await observer_db.get(Document, response.json()["id"])
        assert row is not None
        assert row.vector_status == VectorStatus.FAILED
        assert row.metadata_["file_integrity"]["source"] == "url_dispatch"


@pytest.mark.asyncio
async def test_url_import_worker_detects_format_and_fences_duplicate_delivery(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.services import embedding_service, tool_cache_version, web_fetch
    from packages.core.tasks import ai_tasks

    fs_settings.MANOR_FS_ENABLED = False

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlindexclaim",
            "email": "urlindexclaim@test.com",
            "password": "pass123",
            "entity_name": "URL Index Claim Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    response = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/claim", "name": "claim.md"},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.metadata_ = {**dict(row.metadata_ or {}), "content_text": ""}
        await db.commit()
    observed_statuses: list[str] = []
    observed_content: list[str | None] = []
    observed_formats: list[tuple[str, str | None, str | None]] = []
    observed_allow_ready: list[bool] = []
    fetch_count = 0

    async def fake_fetch_url(*_args, **_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return SimpleNamespace(
            content=b'{"title":"Remote content"}',
            content_type="text/html; charset=utf-8",
        )

    async def fake_index_document(db, observed_document_id, *, allow_ready=True):
        row = await db.get(Document, observed_document_id)
        assert row is not None
        observed_allow_ready.append(allow_ready)
        if row.vector_status == VectorStatus.READY and not allow_ready:
            return True
        observed_statuses.append(row.vector_status)
        observed_content.append(dict(row.metadata_ or {}).get("content_text"))
        observed_formats.append((row.name, row.file_type, row.mime_type))
        row.vector_status = VectorStatus.READY
        return True

    monkeypatch.setattr(web_fetch, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(embedding_service, "index_document", fake_index_document)

    result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/claim",
    )

    assert result == {"document_id": document_id, "status": "ready"}
    assert observed_statuses == [VectorStatus.PENDING]
    assert json.loads(observed_content[0] or "") == {"title": "Remote content"}
    assert observed_formats == [("claim.json", "json", "application/json")]
    assert observed_allow_ready == [False]

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.vector_status = VectorStatus.FAILED
        await db.commit()

    retry_result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/claim",
    )

    assert retry_result == {"document_id": document_id, "status": "ready"}
    assert fetch_count == 1
    assert observed_statuses == [VectorStatus.PENDING, VectorStatus.FAILED]
    assert observed_allow_ready == [False, False]

    duplicate_result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/claim",
    )

    assert duplicate_result == {"document_id": document_id, "status": "ready"}
    assert fetch_count == 1
    assert observed_statuses == [VectorStatus.PENDING, VectorStatus.FAILED]
    assert observed_allow_ready == [False, False]


@pytest.mark.asyncio
async def test_url_import_worker_does_not_reindex_ready_filesystem_document(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.services import embedding_service, tool_cache_version, web_fetch
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlreadyfile",
            "email": "urlreadyfile@test.com",
            "password": "pass123",
            "entity_name": "URL Ready File Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    response = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/ready", "name": "ready.html"},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    fetch_count = 0
    index_work_count = 0
    observed_allow_ready: list[bool] = []

    async def fake_fetch_url(*_args, **_kwargs):
        nonlocal fetch_count
        fetch_count += 1
        return SimpleNamespace(
            content=b"<!doctype html><html><body>Ready</body></html>",
            content_type="text/html; charset=utf-8",
        )

    async def fake_index_document(db, observed_document_id, *, allow_ready=True):
        nonlocal index_work_count
        row = await db.get(Document, observed_document_id)
        assert row is not None
        observed_allow_ready.append(allow_ready)
        if row.vector_status == VectorStatus.READY and not allow_ready:
            return True
        index_work_count += 1
        row.vector_status = VectorStatus.READY
        return True

    monkeypatch.setattr(web_fetch, "fetch_url", fake_fetch_url)
    monkeypatch.setattr(embedding_service, "index_document", fake_index_document)

    first_result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/ready",
    )
    duplicate_result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/ready",
    )

    assert first_result == {"document_id": document_id, "status": "ready"}
    assert duplicate_result == {"document_id": document_id, "status": "ready"}
    assert fetch_count == 1
    assert index_work_count == 1
    assert observed_allow_ready == [False, False]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.fs_path
        assert os.path.isfile(
            os.path.join(fs_settings.MANOR_FS_ROOT, row.entity_id, row.fs_path)
        )


@pytest.mark.asyncio
async def test_url_import_worker_skips_a_trashed_placeholder(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.services import embedding_service, tool_cache_version, web_fetch
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urltrashfence",
            "email": "urltrashfence@test.com",
            "password": "pass123",
            "entity_name": "URL Trash Fence Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/trashed", "name": "trashed.html"},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text

    fetch = AsyncMock(return_value=SimpleNamespace(content=b"must not persist", content_type="text/html"))
    index = AsyncMock(return_value=True)
    monkeypatch.setattr(web_fetch, "fetch_url", fetch)
    monkeypatch.setattr(embedding_service, "index_document", index)

    result = await asyncio.to_thread(
        ai_tasks.fetch_and_index_url_document.run,
        document_id,
        "https://example.com/trashed",
    )

    assert result == {"document_id": document_id, "status": "skipped"}
    fetch.assert_not_awaited()
    index.assert_not_awaited()
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, registered.json()["entity_id"], "trashed.html"))


@pytest.mark.asyncio
async def test_restore_requeues_a_url_placeholder_without_content(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlrestorerequeue",
            "email": "urlrestorerequeue@test.com",
            "password": "pass123",
            "entity_name": "URL Restore Requeue Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    source_url = "https://example.com/restore"
    created = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": source_url, "name": "restore.html"},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda observed_document_id, url: dispatched.append((observed_document_id, url)),
    )
    restored = await client.post(
        f"/api/v1/documents/{document_id}/restore",
        headers=headers,
    )

    assert restored.status_code == 200, restored.text
    assert dispatched == [(document_id, source_url)]


@pytest.mark.asyncio
async def test_restore_requeues_a_regular_document_for_embeddings(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.services import tool_cache_version
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "regularrestorerequeue",
            "email": "regularrestorerequeue@test.com",
            "password": "pass123",
            "entity_name": "Regular Restore Requeue Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("restore.txt", b"restore me", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text

    dispatched: list[str] = []
    monkeypatch.setattr(
        ai_tasks.process_document_embeddings,
        "delay",
        lambda observed_document_id: dispatched.append(observed_document_id),
    )
    restored = await client.post(
        f"/api/v1/documents/{document_id}/restore",
        headers=headers,
    )

    assert restored.status_code == 200, restored.text
    assert dispatched == [document_id]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.vector_status == VectorStatus.PENDING


@pytest.mark.asyncio
async def test_trash_restores_file_when_database_work_fails(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trashrollback",
            "email": "trashrollback@test.com",
            "password": "pass123",
            "entity_name": "Trash Rollback Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("keep.txt", b"keep me", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    original_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "keep.txt")
    original_trash_document = documents.trash_document

    async def fail_after_file_move(*args, **kwargs):
        assert await original_trash_document(*args, **kwargs)
        raise RuntimeError("database commit unavailable")

    monkeypatch.setattr(documents, "trash_document", fail_after_file_move)
    with pytest.raises(RuntimeError, match="database commit unavailable"):
        await client.post(
            f"/api/v1/documents/{document_id}/trash",
            headers=headers,
        )
    assert os.path.isfile(original_path)
    with open(original_path, "rb") as file:
        assert file.read() == b"keep me"
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is False
        assert row.fs_path == "keep.txt"


@pytest.mark.asyncio
async def test_restore_retrashes_file_when_database_work_fails(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "restorerollback",
            "email": "restorerollback@test.com",
            "password": "pass123",
            "entity_name": "Restore Rollback Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("restore.txt", b"restore me", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text
    active_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "restore.txt")
    trash_rel_path = os.path.join(
        ".trash",
        "documents",
        document_id,
        "restore.txt",
    )
    trash_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, trash_rel_path)
    original_restore_document = documents.restore_document

    async def fail_after_file_move(*args, **kwargs):
        assert await original_restore_document(*args, **kwargs)
        raise RuntimeError("database commit unavailable")

    monkeypatch.setattr(documents, "restore_document", fail_after_file_move)
    with pytest.raises(RuntimeError, match="database commit unavailable"):
        await client.post(
            f"/api/v1/documents/{document_id}/restore",
            headers=headers,
        )

    assert not os.path.exists(active_path)
    assert os.path.isfile(trash_path)
    with open(trash_path, "rb") as file:
        assert file.read() == b"restore me"
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path == trash_rel_path


@pytest.mark.asyncio
async def test_restore_does_not_retrash_after_database_commit(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "restorepostcommit",
            "email": "restorepostcommit@test.com",
            "password": "pass123",
            "entity_name": "Restore Post Commit Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("committed.txt", b"committed", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text
    active_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "committed.txt")
    trash_path = os.path.join(
        fs_settings.MANOR_FS_ROOT,
        entity_id,
        ".trash",
        "documents",
        document_id,
        "committed.txt",
    )

    async def fail_after_commit(*_args, **_kwargs):
        raise RuntimeError("post-commit publication failed")

    monkeypatch.setattr(
        documents,
        "_dispatch_document_embeddings_and_invalidate_cache",
        fail_after_commit,
    )
    with pytest.raises(RuntimeError, match="post-commit publication failed"):
        await client.post(
            f"/api/v1/documents/{document_id}/restore",
            headers=headers,
        )

    assert os.path.isfile(active_path)
    assert not os.path.exists(trash_path)
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is False
        assert row.fs_path == "committed.txt"


@pytest.mark.asyncio
async def test_empty_trash_keeps_files_when_database_commit_fails(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "emptytrashrollback",
            "email": "emptytrashrollback@test.com",
            "password": "pass123",
            "entity_name": "Empty Trash Rollback Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("preserve.txt", b"preserve me", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text
    trash_rel_path = os.path.join(
        ".trash",
        "documents",
        document_id,
        "preserve.txt",
    )
    trash_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, trash_rel_path)
    original_empty_trash = documents.empty_trash

    async def empty_with_failed_commit(db, observed_entity_id):
        original_commit = db.commit

        async def fail_commit():
            raise RuntimeError("database commit unavailable")

        db.commit = fail_commit
        try:
            return await original_empty_trash(db, observed_entity_id)
        finally:
            db.commit = original_commit

    monkeypatch.setattr(documents, "empty_trash", empty_with_failed_commit)
    with pytest.raises(RuntimeError, match="database commit unavailable"):
        await client.post("/api/v1/documents/trash/empty", headers=headers)

    assert os.path.isfile(trash_path)
    with open(trash_path, "rb") as file:
        assert file.read() == b"preserve me"
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path == trash_rel_path


@pytest.mark.asyncio
async def test_empty_trash_rejects_paths_outside_the_entity_root(
    client,
    fs_settings,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services.version_service import empty_trash

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trashpathboundary",
            "email": "trashpathboundary@test.com",
            "password": "pass123",
            "entity_name": "Trash Path Boundary Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    outside_path = os.path.join(fs_settings.MANOR_FS_ROOT, "outside.txt")
    with open(outside_path, "wb") as file:
        file.write(b"must survive")

    async with async_session() as db:
        absolute = Document(
            entity_id=entity_id,
            name="absolute.txt",
            fs_path=outside_path,
            is_trashed=True,
        )
        traversal = Document(
            entity_id=entity_id,
            name="traversal.txt",
            fs_path="../outside.txt",
            is_trashed=True,
        )
        db.add_all([absolute, traversal])
        await db.commit()

        assert await empty_trash(db, entity_id) == 2
        assert await db.get(Document, absolute.id) is None
        assert await db.get(Document, traversal.id) is None

    assert os.path.isfile(outside_path)
    with open(outside_path, "rb") as file:
        assert file.read() == b"must survive"


@pytest.mark.asyncio
async def test_empty_trash_preserves_file_referenced_by_active_historical_row(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.models.user import User
    from packages.core.services import tool_cache_version
    from sqlalchemy import select

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "emptytrashsharedpath",
            "email": "emptytrashsharedpath@test.com",
            "password": "pass123",
            "entity_name": "Empty Trash Shared Path Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("shared.txt", b"active bytes", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    active_document_id = uploaded.json()["id"]

    async with async_session() as db:
        user = await db.scalar(select(User).where(User.email == "emptytrashsharedpath@test.com"))
        assert user is not None
        duplicate = Document(
            entity_id=entity_id,
            name="legacy-duplicate.txt",
            # Historical rows can spell the same safe path differently. File
            # cleanup must compare resolved paths, not raw database strings.
            fs_path="./shared.txt",
            created_by=user.id,
            owner_id=user.id,
            is_trashed=True,
            metadata_={"restore_blocked_reason": "duplicate_filesystem_projection"},
        )
        db.add(duplicate)
        await db.commit()
        duplicate_id = duplicate.id
        active = await db.get(Document, active_document_id)
        assert active is not None
        assert active.is_trashed is False
        assert active.fs_path == "shared.txt"

    emptied = await client.post("/api/v1/documents/trash/empty", headers=headers)
    assert emptied.status_code == 204, emptied.text
    active_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "shared.txt")
    assert os.path.isfile(active_path)
    with open(active_path, "rb") as file:
        assert file.read() == b"active bytes"
    async with async_session() as db:
        assert await db.get(Document, duplicate_id) is None
        active = await db.get(Document, active_document_id)
        assert active is not None
        assert active.is_trashed is False


@pytest.mark.asyncio
async def test_trash_authorizes_only_after_waiting_for_the_entity_lock(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from fastapi import HTTPException
    from packages.core.services import entity_fs, tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trashauthafterlock",
            "email": "trashauthafterlock@test.com",
            "password": "pass123",
            "entity_name": "Trash Auth Lock Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("locked.txt", b"locked", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    authorize = AsyncMock(side_effect=HTTPException(status_code=403, detail="permission revoked"))
    monkeypatch.setattr(documents, "_require_document_delete", authorize)
    entity_root = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id)

    async with entity_fs.entity_filesystem_mutation_lock(entity_root):
        request = asyncio.create_task(
            client.post(
                f"/api/v1/documents/{uploaded.json()['id']}/trash",
                headers=headers,
            )
        )
        await asyncio.sleep(0.1)
        assert not request.done()
        authorize.assert_not_awaited()

    response = await request
    assert response.status_code == 403, response.text
    authorize.assert_awaited_once()
    assert os.path.isfile(os.path.join(entity_root, "locked.txt"))


@pytest.mark.asyncio
async def test_restore_waits_for_empty_trash_entity_mutation(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "restoreemptylock",
            "email": "restoreemptylock@test.com",
            "password": "pass123",
            "entity_name": "Restore Empty Lock Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("serialized.txt", b"serialized", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    trashed = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text
    empty_started = asyncio.Event()
    release_empty = asyncio.Event()
    original_empty_trash = documents.empty_trash
    original_restore_document = documents.restore_document

    async def blocking_empty_trash(*args, **kwargs):
        empty_started.set()
        await release_empty.wait()
        return await original_empty_trash(*args, **kwargs)

    restore_document = AsyncMock(wraps=original_restore_document)
    monkeypatch.setattr(documents, "empty_trash", blocking_empty_trash)
    monkeypatch.setattr(documents, "restore_document", restore_document)
    empty_request = asyncio.create_task(client.post("/api/v1/documents/trash/empty", headers=headers))
    await empty_started.wait()
    restore_request = asyncio.create_task(
        client.post(
            f"/api/v1/documents/{document_id}/restore",
            headers=headers,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not restore_request.done()
        restore_document.assert_not_awaited()
    finally:
        release_empty.set()

    empty_response, restore_response = await asyncio.gather(
        empty_request,
        restore_request,
    )
    assert empty_response.status_code == 204, empty_response.text
    assert restore_response.status_code == 404, restore_response.text
    restore_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_trash_releases_auth_transaction_before_waiting_for_fs_lock(
    monkeypatch,
):
    from apps.api.routers import documents

    db = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(entity_id="entity-1")
    lock_commit_counts: list[int] = []

    class ObservedLock:
        async def __aenter__(self):
            lock_commit_counts.append(db.commit.await_count)

        async def __aexit__(self, *_args):
            return None

    async def skip_mutation(operation):
        operation.close()
        return True

    monkeypatch.setattr(documents.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(
        documents,
        "_document_filesystem_mutation",
        lambda _entity_id: ObservedLock(),
    )
    monkeypatch.setattr(
        documents,
        "_finish_document_filesystem_mutation",
        skip_mutation,
    )

    response = await documents.trash_one_document(
        "document-1",
        user=user,
        db=db,
    )

    assert response == {"trashed": True}
    db.commit.assert_awaited_once()
    assert lock_commit_counts == [1]


@pytest.mark.asyncio
async def test_url_import_write_and_trash_share_the_entity_mutation_lock(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import (
        embedding_service,
        entity_fs,
        tool_cache_version,
        web_fetch,
    )
    from packages.core.tasks import ai_tasks

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urltrashrace",
            "email": "urltrashrace@test.com",
            "password": "pass123",
            "entity_name": "URL Trash Race Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/race", "name": "race.html"},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]

    monkeypatch.setattr(
        web_fetch,
        "fetch_url",
        AsyncMock(
            return_value=SimpleNamespace(
                content=b"serialized content",
                content_type="text/html",
            )
        ),
    )
    monkeypatch.setattr(
        embedding_service,
        "index_document",
        AsyncMock(return_value=True),
    )
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = entity_fs.write_entity_file_atomic

    def blocking_write(*args, **kwargs):
        write_started.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError("test did not release URL document write")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", blocking_write)
    worker = asyncio.create_task(
        asyncio.to_thread(
            ai_tasks.fetch_and_index_url_document.run,
            document_id,
            "https://example.com/race",
        )
    )
    assert await asyncio.to_thread(write_started.wait, 5)
    document_lookup_started = asyncio.Event()
    permission_check_started = asyncio.Event()
    original_get_visible_document = documents.get_visible_document
    original_require_document_delete = documents._require_document_delete

    async def observe_document_lookup(*args, **kwargs):
        document_lookup_started.set()
        return await original_get_visible_document(*args, **kwargs)

    async def observe_permission_check(*args, **kwargs):
        permission_check_started.set()
        return await original_require_document_delete(*args, **kwargs)

    monkeypatch.setattr(
        documents,
        "get_visible_document",
        observe_document_lookup,
    )
    monkeypatch.setattr(
        documents,
        "_require_document_delete",
        observe_permission_check,
    )
    trash = asyncio.create_task(
        client.post(
            f"/api/v1/documents/{document_id}/trash",
            headers=headers,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not trash.done()
        assert not document_lookup_started.is_set()
        assert not permission_check_started.is_set()
    finally:
        release_write.set()

    worker_result, trash_response = await asyncio.gather(worker, trash)
    assert worker_result == {"document_id": document_id, "status": "ready"}
    assert trash_response.status_code == 200, trash_response.text
    assert document_lookup_started.is_set()
    assert permission_check_started.is_set()

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path.startswith(f".trash/documents/{document_id}/")
        trashed_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, row.fs_path)
    assert os.path.isfile(trashed_path)
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "race.html"))


@pytest.mark.asyncio
async def test_trash_optional_embedding_cleanup_uses_a_savepoint(
    client,
    fs_settings,
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import tool_cache_version

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trashsavepoint",
            "email": "trashsavepoint@test.com",
            "password": "pass123",
            "entity_name": "Trash Savepoint Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("savepoint.txt", b"keep transaction alive", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    original_text = documents.text
    monkeypatch.setattr(
        documents,
        "text",
        lambda _statement: original_text("SELECT 1 / 0"),
    )

    response = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.is_trashed is True
        assert row.fs_path.startswith(f".trash/documents/{document_id}/")
        trashed_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, row.fs_path)
    assert os.path.isfile(trashed_path)


@pytest.mark.asyncio
async def test_document_index_extracts_from_snapshot_outside_the_entity_lock(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services import (
        embedding_service,
        entity_fs,
        text_extraction,
        tool_cache_version,
    )

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "trashreadlock",
            "email": "trashreadlock@test.com",
            "password": "pass123",
            "entity_name": "Trash Read Lock Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("reading.txt", b"read before trash", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    async with async_session() as db:
        document = await db.get(Document, document_id)
        assert document is not None
        db.expunge(document)

    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def blocking_extract(*_args, **_kwargs):
        read_started.set()
        await release_read.wait()
        return "read before trash"

    monkeypatch.setattr(text_extraction, "extract_text", blocking_extract)
    reading = asyncio.create_task(
        embedding_service._read_document_content(document),
    )
    await read_started.wait()
    writer = await asyncio.to_thread(
        entity_fs._try_acquire_entity_mutation_lock,
        entity_fs.get_entity_root(document.entity_id),
    )
    try:
        assert writer is not None
    finally:
        if writer is not None:
            entity_fs._release_entity_mutation_lock(writer)
        release_read.set()

    assert await reading == "read before trash"
    response = await client.post(
        f"/api/v1/documents/{document_id}/trash",
        headers=headers,
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_stale_url_fetch_failure_does_not_overwrite_ready_document(
    client,
    fs_settings,
    monkeypatch,
):
    from packages.core.database import async_session
    from packages.core.models.document import Document, VectorStatus
    from packages.core.services import tool_cache_version, web_fetch
    from packages.core.tasks import ai_tasks

    fs_settings.MANOR_FS_ENABLED = False

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", no_op)
    monkeypatch.setattr(
        ai_tasks.fetch_and_index_url_document,
        "delay",
        lambda *_args, **_kwargs: None,
    )
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "urlstalefailure",
            "email": "urlstalefailure@test.com",
            "password": "pass123",
            "entity_name": "URL Stale Failure Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = await client.post(
        "/api/v1/documents/from-url",
        headers=headers,
        json={"url": "https://example.com/ready", "name": "ready.html"},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]
    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        row.vector_status = VectorStatus.READY
        await db.commit()

    monkeypatch.setattr(
        web_fetch,
        "fetch_url",
        AsyncMock(side_effect=RuntimeError("older delivery failed")),
    )

    def raise_original(*, exc, **_kwargs):
        raise exc

    monkeypatch.setattr(ai_tasks.fetch_and_index_url_document, "retry", raise_original)
    with pytest.raises(RuntimeError, match="older delivery failed"):
        await asyncio.to_thread(
            ai_tasks.fetch_and_index_url_document.run,
            document_id,
            "https://example.com/ready",
        )

    async with async_session() as db:
        row = await db.get(Document, document_id)
        assert row is not None
        assert row.vector_status == VectorStatus.READY
        assert "fetch_error" not in dict(row.metadata_ or {})


@pytest.mark.asyncio
async def test_committed_document_cache_invalidation_is_best_effort(monkeypatch):
    from apps.api.routers import documents
    from packages.core.services import tool_cache_version

    async def fail_cache_bump(*_args, **_kwargs):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(tool_cache_version, "bump_tool_cache_version", fail_cache_bump)

    await documents._invalidate_committed_document_cache("entity-1")


@pytest.mark.asyncio
async def test_committed_document_cache_invalidation_respects_cancellation(
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.services import tool_cache_version

    bump_started = asyncio.Event()
    release_bump = asyncio.Event()
    bump_finished = asyncio.Event()

    async def blocking_cache_bump(*_args, **_kwargs):
        bump_started.set()
        await release_bump.wait()
        bump_finished.set()

    monkeypatch.setattr(
        tool_cache_version,
        "bump_tool_cache_version",
        blocking_cache_bump,
    )
    invalidation = asyncio.create_task(
        documents._invalidate_committed_document_cache("entity-1"),
    )
    await bump_started.wait()
    invalidation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invalidation
    assert not bump_finished.is_set()
    release_bump.set()
    await asyncio.wait_for(bump_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_committed_document_cache_invalidation_has_a_hard_timeout(monkeypatch):
    from apps.api.routers import documents
    from packages.core.services import tool_cache_version

    bump_started = asyncio.Event()
    release_bump = asyncio.Event()
    bump_finished = asyncio.Event()

    async def blocking_cache_bump(*_args, **_kwargs):
        bump_started.set()
        await release_bump.wait()
        bump_finished.set()

    monkeypatch.setattr(
        tool_cache_version,
        "bump_tool_cache_version",
        blocking_cache_bump,
    )
    monkeypatch.setattr(
        documents,
        "_DOCUMENT_CACHE_INVALIDATION_TIMEOUT_SECONDS",
        0.01,
    )

    await asyncio.wait_for(
        documents._invalidate_committed_document_cache("entity-1"),
        timeout=0.5,
    )
    assert bump_started.is_set()
    assert not bump_finished.is_set()
    release_bump.set()
    await asyncio.wait_for(bump_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_committed_document_cache_invalidation_cancels_stuck_background_task(
    monkeypatch,
):
    from apps.api.routers import documents
    from packages.core.services import tool_cache_version

    bump_started = asyncio.Event()
    bump_cancelled = asyncio.Event()

    async def stuck_cache_bump(*_args, **_kwargs):
        bump_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            bump_cancelled.set()
            raise

    monkeypatch.setattr(
        tool_cache_version,
        "bump_tool_cache_version",
        stuck_cache_bump,
    )
    monkeypatch.setattr(
        documents,
        "_DOCUMENT_CACHE_INVALIDATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        documents,
        "_DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TIMEOUT_SECONDS",
        0.01,
    )

    await asyncio.wait_for(
        documents._invalidate_committed_document_cache("entity-1"),
        timeout=0.5,
    )
    assert bump_started.is_set()
    await asyncio.wait_for(bump_cancelled.wait(), timeout=0.5)
    background_tasks = tuple(
        documents._DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TASKS
    )
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    assert not documents._DOCUMENT_CACHE_INVALIDATION_BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_filesystem_move_and_delete_enforce_document_capabilities(
    client, db_session, fs_settings,
):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.permission import (
        Capability,
        ResourceGrant,
        ResourceType,
        SubjectType,
    )
    from packages.core.models.user import User
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error
    from packages.core.services.auth_service import create_access_token, hash_password

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsmutationowner",
            "email": "fsmutationowner@test.com",
            "password": "pass123",
            "entity_name": "FS Mutation Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    member = User(
        entity_id=entity_id,
        email="fsmutationmember@test.com",
        display_name="fsmutationmember",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(member)
    await db_session.flush()
    headers = {
        "Authorization": f"Bearer {create_access_token(member.id, entity_id, member.role)}"
    }

    original = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "site", "protected.css")
    renamed = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "site", "renamed.css")
    os.makedirs(os.path.dirname(original), exist_ok=True)
    with open(original, "w", encoding="utf-8") as file:
        file.write("body{}")
    document = Document(
        entity_id=entity_id,
        name="protected.css",
        fs_path="site/protected.css",
        file_type="css",
        mime_type="text/css",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
    )
    db_session.add(document)
    await db_session.commit()

    denied_move = await client.post(
        "/api/v1/fs/move",
        headers=headers,
        json={"src": "site/protected.css", "dest": "site/renamed.css"},
    )
    assert denied_move.status_code == 403, denied_move.text
    assert os.path.isfile(original)
    bash_denied = await _bash_resource_mutation_error(
        entity_id=entity_id,
        user_id=member.id,
        cwd=os.path.join(fs_settings.MANOR_FS_ROOT, entity_id),
        command="rm site/protected.css",
        paths=["site/protected.css"],
    )
    assert bash_denied is not None
    assert json.loads(bash_denied)["error"] == "file_permission_denied"

    grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=document.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.EDIT],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    )
    db_session.add(grant)
    await db_session.commit()

    moved = await client.post(
        "/api/v1/fs/move",
        headers=headers,
        json={"src": "site/protected.css", "dest": "site/renamed.css"},
    )
    assert moved.status_code == 200, moved.text
    assert not os.path.exists(original)
    assert os.path.isfile(renamed)

    denied_delete = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": "site/renamed.css"},
    )
    assert denied_delete.status_code == 403, denied_delete.text
    assert os.path.isfile(renamed)

    grant.capabilities = [Capability.EDIT, Capability.DELETE]
    await db_session.commit()
    assert await _bash_resource_mutation_error(
        entity_id=entity_id,
        user_id=member.id,
        cwd=os.path.join(fs_settings.MANOR_FS_ROOT, entity_id),
        command="rm site/renamed.css",
        paths=["site/renamed.css"],
    ) is None
    deleted = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": "site/renamed.css"},
    )
    assert deleted.status_code == 200, deleted.text
    assert not os.path.exists(renamed)
    await db_session.refresh(document)
    assert document.is_trashed is True
    assert document.fs_path is None
    assert document.metadata_["deleted_fs_path"] == "site/renamed.css"
    assert document.metadata_["restore_blocked_reason"] == "filesystem_path_deleted"


@pytest.mark.asyncio
async def test_filesystem_directory_mutations_require_folder_ownership(
    client, db_session, fs_settings,
):
    from datetime import datetime, timezone
    from sqlalchemy import select

    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.permission import (
        Capability,
        ResourceGrant,
        ResourceType,
        SubjectType,
    )
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsfolderowner",
            "email": "fsfolderowner@test.com",
            "password": "pass123",
            "entity_name": "FS Folder Owner Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    member = User(
        entity_id=entity_id,
        email="fsfoldermember@test.com",
        display_name="fsfoldermember",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    folder = DocumentFolder(
        entity_id=entity_id,
        name="protected-folder",
        owner_id=owner.id,
    )
    db_session.add_all([member, folder])
    await db_session.flush()
    document = Document(
        entity_id=entity_id,
        name="index.html",
        fs_path="protected-folder/index.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        folder_id=folder.id,
    )
    db_session.add(document)
    await db_session.flush()
    db_session.add(ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=document.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.EDIT, Capability.DELETE],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    ))
    await db_session.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token(member.id, entity_id, member.role)}"
    }
    folder_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "protected-folder")
    os.makedirs(folder_path, exist_ok=True)
    with open(os.path.join(folder_path, "index.html"), "w", encoding="utf-8") as file:
        file.write("<html></html>")

    denied_move = await client.post(
        "/api/v1/fs/move",
        headers=headers,
        json={"src": "protected-folder", "dest": "renamed-folder"},
    )
    assert denied_move.status_code == 403, denied_move.text
    denied_delete = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": "protected-folder"},
    )
    assert denied_delete.status_code == 403, denied_delete.text
    assert os.path.isdir(folder_path)

    denied_mkdir = await client.post(
        "/api/v1/fs/mkdir",
        headers=headers,
        json={"path": "protected-folder/member-child"},
    )
    assert denied_mkdir.status_code == 403, denied_mkdir.text
    denied_upload = await client.post(
        "/api/v1/fs/upload",
        headers=headers,
        params={"path": "protected-folder"},
        files={"file": ("member-note.txt", b"not allowed", "text/plain")},
    )
    assert denied_upload.status_code == 403, denied_upload.text
    assert not os.path.exists(os.path.join(folder_path, "member-note.txt"))

    db_session.add(ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT_FOLDER,
        resource_id=folder.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.UPLOAD_TO],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    ))
    await db_session.commit()
    allowed_mkdir = await client.post(
        "/api/v1/fs/mkdir",
        headers=headers,
        json={"path": "protected-folder/member-child"},
    )
    assert allowed_mkdir.status_code == 200, allowed_mkdir.text
    allowed_upload = await client.post(
        "/api/v1/fs/upload",
        headers=headers,
        params={"path": "protected-folder"},
        files={"file": ("member-note.txt", b"allowed", "text/plain")},
    )
    assert allowed_upload.status_code == 200, allowed_upload.text
    assert os.path.isfile(os.path.join(folder_path, "member-note.txt"))

    created = await client.post(
        "/api/v1/fs/mkdir",
        headers=headers,
        json={"path": "member-owned-empty"},
    )
    assert created.status_code == 200, created.text
    deleted_owned = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": "member-owned-empty"},
    )
    assert deleted_owned.status_code == 200, deleted_owned.text


@pytest.mark.asyncio
async def test_filesystem_directory_delete_removes_folder_projection(
    client, db_session, fs_settings,
):
    from sqlalchemy import select

    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.user import User

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsfolderdelete",
            "email": "fsfolderdelete@test.com",
            "password": "pass123",
            "entity_name": "FS Folder Delete Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    root_folder = DocumentFolder(
        entity_id=entity_id,
        name="obsolete",
        owner_id=owner.id,
    )
    db_session.add(root_folder)
    await db_session.flush()
    child_folder = DocumentFolder(
        entity_id=entity_id,
        name="nested",
        parent_id=root_folder.id,
        owner_id=owner.id,
    )
    db_session.add(child_folder)
    await db_session.flush()
    document = Document(
        entity_id=entity_id,
        name="note.md",
        fs_path="obsolete/nested/note.md",
        file_type="md",
        mime_type="text/markdown",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        folder_id=child_folder.id,
    )
    db_session.add(document)
    await db_session.commit()
    document_id = document.id
    folder_ids = {root_folder.id, child_folder.id}
    folder_path = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "obsolete", "nested")
    os.makedirs(folder_path, exist_ok=True)
    with open(os.path.join(folder_path, "note.md"), "w", encoding="utf-8") as file:
        file.write("obsolete")

    deleted = await client.post(
        "/api/v1/fs/delete",
        headers=headers,
        json={"path": "obsolete"},
    )
    assert deleted.status_code == 200, deleted.text
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "obsolete"))

    db_session.expire_all()
    folders = list((await db_session.scalars(
        select(DocumentFolder).where(DocumentFolder.id.in_(folder_ids))
    )).all())
    document = await db_session.get(Document, document_id)
    assert folders == []
    assert document is not None
    assert document.is_trashed is True
    assert document.fs_path is None
    assert document.folder_id is None


@pytest.mark.asyncio
async def test_filesystem_move_refuses_implicit_destination_overwrite(
    client, db_session, fs_settings,
):
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.models.user import User

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "fsmoveconflict",
            "email": "fsmoveconflict@test.com",
            "password": "pass123",
            "entity_name": "FS Move Conflict Corp",
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    entity_id = registered.json()["entity_id"]
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    root = os.path.join(fs_settings.MANOR_FS_ROOT, entity_id, "site")
    os.makedirs(root, exist_ok=True)
    for name, content in (("source.css", "source"), ("destination.css", "destination")):
        with open(os.path.join(root, name), "w", encoding="utf-8") as file:
            file.write(content)
        db_session.add(Document(
            entity_id=entity_id,
            name=name,
            fs_path=f"site/{name}",
            file_type="css",
            mime_type="text/css",
            source="upload",
            created_by=owner.id,
            owner_id=owner.id,
        ))
    await db_session.commit()

    conflict = await client.post(
        "/api/v1/fs/move",
        headers=headers,
        json={"src": "site/source.css", "dest": "site/destination.css"},
    )
    assert conflict.status_code == 409, conflict.text
    with open(os.path.join(root, "source.css"), encoding="utf-8") as file:
        assert file.read() == "source"
    with open(os.path.join(root, "destination.css"), encoding="utf-8") as file:
        assert file.read() == "destination"


def test_copy_entity_file_atomic_persists_verified_file(fs_settings, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")

    path = copy_entity_file_atomic(
        "entity_1",
        "videos/copied.mp4",
        str(source),
        expected_size=source.stat().st_size,
    )

    assert open(path, "rb").read() == b"source-video"


def test_copy_entity_file_atomic_rejects_source_alias(fs_settings, tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"source")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(source)

    with pytest.raises(EntityFileWriteError, match="Source file does not exist"):
        copy_entity_file_atomic(
            "entity_1",
            "documents/copied.txt",
            str(alias),
        )
