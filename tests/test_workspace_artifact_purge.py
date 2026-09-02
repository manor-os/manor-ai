from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models.artifact_purge import (
    ARTIFACT_CLEANUP_STORAGE_BASE_MAX_LENGTH,
    WorkspaceArtifactPurgeJob,
)
from packages.core.models.base import generate_ulid
from packages.core.models.comment import Comment
from packages.core.models.document import (
    Document,
    DocumentChunk,
    DocumentFolder,
    DocumentGroup,
    DocumentGroupMember,
)
from packages.core.models.document_version import DocumentVersion
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services.entity_service import purge_workspace
from packages.core.services.workspace_artifact_purge import (
    ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    ARTIFACT_CLEANUP_KIND_FILE,
    document_derived_file_cleanup_bases,
    document_derived_tree_cleanup_bases,
    drain_workspace_artifact_purge_jobs,
    enqueue_artifact_cleanup_jobs,
)
from packages.core.services.workspace_artifacts import workspace_artifact_storage_base


def test_cleanup_job_storage_base_accommodates_runtime_backup_suffix() -> None:
    assert Document.__table__.c.fs_path.type.length == 1000
    assert (
        WorkspaceArtifactPurgeJob.__table__.c.storage_base.type.length
        == ARTIFACT_CLEANUP_STORAGE_BASE_MAX_LENGTH
    )
    assert (
        ARTIFACT_CLEANUP_STORAGE_BASE_MAX_LENGTH
        >= Document.__table__.c.fs_path.type.length + 80
    )


@pytest.mark.asyncio
async def test_cleanup_job_enqueue_is_concurrency_idempotent(db_session) -> None:
    entity_id = generate_ulid()
    storage_base = ".document-page-cache/concurrent-document"
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def enqueue_once() -> None:
        async with session_factory() as session:
            await enqueue_artifact_cleanup_jobs(
                session,
                entity_id,
                {storage_base},
            )
            await session.commit()

    await asyncio.gather(enqueue_once(), enqueue_once())

    jobs = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base == storage_base,
        )
    )).scalars().all()
    assert len(jobs) == 1
    await db_session.delete(jobs[0])
    await db_session.commit()


@pytest.mark.asyncio
async def test_cleanup_job_enqueue_rejects_a_conflicting_target_kind(
    db_session,
) -> None:
    entity_id = generate_ulid()
    storage_base = "Knowledge/conflicting-target"
    await enqueue_artifact_cleanup_jobs(
        db_session,
        entity_id,
        {storage_base},
    )

    with pytest.raises(ValueError, match="different target kind"):
        await enqueue_artifact_cleanup_jobs(
            db_session,
            entity_id,
            {storage_base},
            target_kind=ARTIFACT_CLEANUP_KIND_FILE,
        )
    await db_session.rollback()
    jobs = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base == storage_base,
        )
    )).scalars().all()
    for job in jobs:
        await db_session.delete(job)
    await db_session.commit()


@pytest.mark.asyncio
async def test_empty_directory_cleanup_never_widens_to_nonempty_tree(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    empty_base = ".runtime-delete-empty"
    nonempty_base = ".runtime-delete-nonempty"
    empty_target = tmp_path / entity_id / empty_base
    nonempty_target = tmp_path / entity_id / nonempty_base
    empty_target.mkdir(parents=True)
    nonempty_target.mkdir()
    (nonempty_target / "keep.txt").write_text("keep", encoding="utf-8")
    await enqueue_artifact_cleanup_jobs(
        db_session,
        entity_id,
        {empty_base, nonempty_base},
        target_kind=ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    )
    await db_session.commit()
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={empty_base},
        target_kind=ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    ) == (1, 0)
    assert not empty_target.exists()

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={nonempty_base},
        target_kind=ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    ) == (0, 1)
    assert (nonempty_target / "keep.txt").read_text(encoding="utf-8") == "keep"
    retained_job = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base == nonempty_base,
        )
    )).scalar_one()
    await db_session.delete(retained_job)
    await db_session.commit()


@pytest.mark.asyncio
async def test_document_cache_cleanup_jobs_are_idempotent_and_filterable(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    selected_base = ".document-page-cache/document-one"
    unrelated_base = ".document-page-cache/document-two"
    selected = tmp_path / entity_id / selected_base
    unrelated = tmp_path / entity_id / unrelated_base
    for target in (selected, unrelated):
        target.mkdir(parents=True)
        (target / "page-1.png").write_bytes(b"page")

    await enqueue_artifact_cleanup_jobs(db_session, entity_id, {selected_base})
    await enqueue_artifact_cleanup_jobs(db_session, entity_id, {selected_base})
    await enqueue_artifact_cleanup_jobs(db_session, entity_id, {unrelated_base})
    await db_session.commit()
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={selected_base},
    ) == (1, 0)

    assert not selected.exists()
    assert unrelated.exists()
    remaining = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
        )
    )).scalars().all()
    assert [job.storage_base for job in remaining] == [unrelated_base]

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={unrelated_base},
    ) == (1, 0)
    assert not unrelated.exists()


@pytest.mark.asyncio
async def test_document_source_cleanup_job_removes_only_the_selected_file(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    source_base = "Workspaces/source.docx"
    sibling_base = "Workspaces/keep.docx"
    source = tmp_path / entity_id / source_base
    sibling = tmp_path / entity_id / sibling_base
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    sibling.write_bytes(b"keep")

    await enqueue_artifact_cleanup_jobs(
        db_session,
        entity_id,
        {source_base},
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    )
    await db_session.commit()
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={source_base},
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    ) == (1, 0)
    assert not source.exists()
    assert sibling.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_empty_directory_cleanup_never_widens_to_new_contents(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    cleanup_base = "Knowledge/.retired.tmp-test"
    cleanup_dir = tmp_path / entity_id / cleanup_base
    cleanup_dir.mkdir(parents=True)
    unexpected = cleanup_dir / "new-content.txt"
    unexpected.write_text("keep", encoding="utf-8")
    await enqueue_artifact_cleanup_jobs(
        db_session,
        entity_id,
        {cleanup_base},
        target_kind=ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    )
    await db_session.commit()
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={cleanup_base},
        target_kind=ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    ) == (0, 1)
    assert unexpected.read_text(encoding="utf-8") == "keep"
    assert (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base == cleanup_base,
        )
    )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_document_source_cleanup_preserves_a_reused_path(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    source_base = "Workspaces/reused.docx"
    source = tmp_path / entity_id / source_base
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new owner")
    db_session.add(Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="reused.docx",
        fs_path=source_base,
    ))
    await enqueue_artifact_cleanup_jobs(
        db_session,
        entity_id,
        {source_base},
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    )
    await db_session.commit()
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
        storage_bases={source_base},
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    ) == (1, 0)
    assert source.read_bytes() == b"new owner"
    assert (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base == source_base,
        )
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_workspace_purge_removes_ledger_files_and_knowledge_projection(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    root_folder_id = generate_ulid()
    artifact_folder_id = generate_ulid()
    ledger_folder_id = generate_ulid()
    document_id = generate_ulid()
    folder_only_document_id = generate_ulid()
    chunk_id = generate_ulid()
    version_id = generate_ulid()
    group_id = generate_ulid()
    storage_base = workspace_artifact_storage_base(artifact_folder_id)
    fs_path = f"{storage_base}/recruiting-ledger/events/event.json"
    external_fs_path = "Knowledge/imports/people-policy.md"

    db_session.add(Entity(id=entity_id, name="Ledger Purge Entity"))
    db_session.add_all([
        DocumentFolder(
            id=root_folder_id,
            entity_id=entity_id,
            name="Workspaces",
            parent_id=None,
        ),
        DocumentFolder(
            id=artifact_folder_id,
            entity_id=entity_id,
            name="Recruiting",
            parent_id=root_folder_id,
        ),
        DocumentFolder(
            id=ledger_folder_id,
            entity_id=entity_id,
            name="recruiting-ledger",
            parent_id=artifact_folder_id,
        ),
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Recruiting",
            artifact_folder_id=artifact_folder_id,
            deleted_at=datetime.now(timezone.utc),
            settings={"ledger_contracts": []},
        ),
        Document(
            id=document_id,
            entity_id=entity_id,
            name="event.json",
            fs_path=fs_path,
            folder_id=ledger_folder_id,
        ),
        Document(
            id=folder_only_document_id,
            entity_id=entity_id,
            name="people-policy.md",
            fs_path=external_fs_path,
            folder_id=ledger_folder_id,
        ),
        DocumentGroup(
            id=group_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            name="Recruiting Ledger",
        ),
        Comment(
            entity_id=entity_id,
            resource_type="document",
            resource_id=document_id,
            user_id=generate_ulid(),
            content="Artifact comment",
        ),
        Comment(
            entity_id=entity_id,
            resource_type="documents",
            resource_id=folder_only_document_id,
            user_id=generate_ulid(),
            content="Legacy alias comment",
        ),
    ])
    await db_session.flush()
    db_session.add_all([
        DocumentChunk(
            id=chunk_id,
            document_id=document_id,
            chunk_index=0,
            content="candidate lifecycle event",
        ),
        DocumentVersion(
            id=version_id,
            document_id=document_id,
            version_number=1,
            name="event.json",
            fs_path=fs_path,
        ),
        DocumentGroupMember(document_id=document_id, group_id=group_id),
    ])
    await db_session.commit()

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )
    file_path = tmp_path / entity_id / fs_path
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}", encoding="utf-8")
    external_file_path = tmp_path / entity_id / external_fs_path
    external_file_path.parent.mkdir(parents=True)
    external_file_path.write_text("policy", encoding="utf-8")
    page_cache = (
        tmp_path
        / entity_id
        / ".document-page-cache"
        / document_id
        / "0123456789abcdef"
    )
    page_cache.mkdir(parents=True)
    (page_cache / "page-1.png").write_bytes(b"page")
    (page_cache / ".complete").write_text("1", encoding="utf-8")

    assert await purge_workspace(db_session, workspace_id) is True
    assert file_path.exists()
    await db_session.rollback()

    assert file_path.exists()
    assert await db_session.get(Workspace, workspace_id) is not None
    assert await db_session.get(Document, document_id) is not None
    assert (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
        )
    )).scalar_one_or_none() is None

    assert await purge_workspace(db_session, workspace_id) is True
    await db_session.commit()

    assert file_path.exists()
    cleanup_jobs = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
        )
    )).scalars().all()
    assert {job.entity_id for job in cleanup_jobs} == {entity_id}
    document_ids = {document_id, folder_only_document_id}
    assert {job.storage_base for job in cleanup_jobs} == (
        {storage_base}
        | document_derived_tree_cleanup_bases(document_ids)
        | document_derived_file_cleanup_bases(document_ids)
        | {external_fs_path}
    )
    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        entity_id=entity_id,
    ) == (12, 0)

    assert not (tmp_path / entity_id / storage_base).exists()
    assert not external_file_path.exists()
    assert not page_cache.exists()
    assert (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
        )
    )).scalar_one_or_none() is None
    assert await db_session.get(Document, document_id) is None
    assert await db_session.get(Document, folder_only_document_id) is None
    assert await db_session.get(DocumentFolder, artifact_folder_id) is None
    assert await db_session.get(DocumentFolder, ledger_folder_id) is None
    assert await db_session.get(DocumentGroup, group_id) is None
    assert await db_session.get(DocumentChunk, chunk_id) is None
    assert await db_session.get(DocumentVersion, version_id) is None
    assert (await db_session.execute(
        select(Comment).where(Comment.resource_id.in_({document_id, folder_only_document_id}))
    )).scalars().all() == []


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_retains_job_when_filesystem_unavailable(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    job = WorkspaceArtifactPurgeJob(
        entity_id=generate_ulid(),
        storage_base="Workspaces/_by_id/fs-unavailable",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=False,
            MANOR_FS_ROOT=str(tmp_path / "missing"),
            DEPLOYMENT_MODE="oss",
        ),
    )
    checked_at = datetime(2026, 8, 23, tzinfo=timezone.utc)
    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        now=checked_at,
    ) == (0, 1)

    retained = await db_session.get(WorkspaceArtifactPurgeJob, job.id)
    assert retained is not None
    assert retained.attempt_count == 1
    assert "filesystem is disabled" in str(retained.last_error)
    assert retained.next_attempt_at == checked_at + timedelta(minutes=15)
    await db_session.delete(retained)
    await db_session.commit()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_unlinks_target_symlink_only(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    storage_base = "Workspaces/_by_id/symlink-target"
    target = tmp_path / entity_id / storage_base
    unrelated = tmp_path / "unrelated-artifacts"
    unrelated.mkdir()
    keep = unrelated / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.symlink_to(unrelated, target_is_directory=True)

    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base=storage_base,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (1, 0)
    assert not target.exists()
    assert not target.is_symlink()
    assert keep.read_text(encoding="utf-8") == "keep"
    assert await db_session.get(WorkspaceArtifactPurgeJob, job_id) is None


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_preserves_replaced_final_file(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    entity_id = generate_ulid()
    storage_base = "Workspaces/_by_id/replaced-final-link"
    target = tmp_path / entity_id / storage_base
    unrelated = tmp_path / "unrelated-final-link"
    unrelated.mkdir()
    target.parent.mkdir(parents=True)
    target.symlink_to(unrelated, target_is_directory=True)
    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base=storage_base,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )
    original_rename = workspace_artifact_purge.os.rename
    replaced = False

    def replace_link_then_rename(src, dst, *args, **kwargs):
        nonlocal replaced
        if src == target.name and not replaced:
            target.unlink()
            target.write_text("preserve replacement", encoding="utf-8")
            replaced = True
        return original_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(
        workspace_artifact_purge.os,
        "rename",
        replace_link_then_rename,
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (0, 1)
    retained = await db_session.get(WorkspaceArtifactPurgeJob, job_id)
    assert retained is not None
    assert "target changed during deletion" in str(retained.last_error)
    assert replaced is True
    assert target.read_text(encoding="utf-8") == "preserve replacement"
    await db_session.delete(retained)
    await db_session.commit()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_locks_before_missing_root_can_appear(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    entity_id = generate_ulid()
    entity_root = tmp_path / entity_id
    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base="Workspaces/_by_id/missing-root-race",
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )
    lock_held = False

    @asynccontextmanager
    async def record_lock(root: str):
        nonlocal lock_held
        assert root == str(entity_root)
        assert not entity_root.exists()
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def remove(_entity_id: str, _storage_base: str) -> None:
        assert lock_held is True

    monkeypatch.setattr(
        workspace_artifact_purge,
        "entity_filesystem_mutation_lock",
        record_lock,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "_remove_artifact_tree",
        remove,
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (1, 0)
    assert lock_held is False


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_missing_root_race_rejects_symlink(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    entity_id = generate_ulid()
    entity_root = tmp_path / entity_id
    outside = tmp_path / "outside-missing-root-race"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base="Workspaces/_by_id/missing-root-symlink-race",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )
    original_lstat = workspace_artifact_purge.os.lstat
    raced = False

    def replace_missing_root_with_symlink(path: str):
        nonlocal raced
        if os.fspath(path) == str(entity_root) and not raced:
            entity_root.symlink_to(outside, target_is_directory=True)
            raced = True
            raise FileNotFoundError(path)
        return original_lstat(path)

    monkeypatch.setattr(
        workspace_artifact_purge.os,
        "lstat",
        replace_missing_root_with_symlink,
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (0, 1)
    retained = await db_session.get(WorkspaceArtifactPurgeJob, job_id)
    assert retained is not None
    assert raced is True
    assert entity_root.is_symlink()
    assert keep.read_text(encoding="utf-8") == "keep"
    assert not (outside / ".ai").exists()
    assert "symlink" in str(retained.last_error).lower()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_anchors_parent_during_delete(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    entity_id = generate_ulid()
    storage_base = "Workspaces/_by_id/race-target"
    entity_root = tmp_path / entity_id
    managed_parent = entity_root / "Workspaces"
    managed_target = managed_parent / "_by_id" / "race-target"
    managed_target.mkdir(parents=True)
    (managed_target / "owned.txt").write_text("owned", encoding="utf-8")

    unrelated_parent = tmp_path / "unrelated-race"
    unrelated_target = unrelated_parent / "_by_id" / "race-target"
    unrelated_target.mkdir(parents=True)
    keep = unrelated_target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    detached_parent = entity_root / "Workspaces-detached"

    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base=storage_base,
    )
    db_session.add(job)
    await db_session.commit()

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )
    original_rmtree = workspace_artifact_purge.shutil.rmtree
    swapped = False

    def swap_parent_then_remove(path, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            managed_parent.rename(detached_parent)
            managed_parent.symlink_to(unrelated_parent, target_is_directory=True)
            swapped = True
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        workspace_artifact_purge.shutil,
        "rmtree",
        swap_parent_then_remove,
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (1, 0)
    assert swapped is True
    assert keep.read_text(encoding="utf-8") == "keep"
    assert not (detached_parent / "_by_id" / "race-target").exists()
    assert managed_parent.is_symlink()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_retains_job_for_unexpected_file(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    storage_base = "Workspaces/_by_id/unexpected-file"
    target = tmp_path / entity_id / storage_base
    target.parent.mkdir(parents=True)
    target.write_text("do not silently discard this", encoding="utf-8")
    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base=storage_base,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (0, 1)
    retained = await db_session.get(WorkspaceArtifactPurgeJob, job_id)
    assert retained is not None
    assert "target is not a directory" in str(retained.last_error)
    assert target.read_text(encoding="utf-8") == "do not silently discard this"
    await db_session.delete(retained)
    await db_session.commit()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_rejects_symlink_parent(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs

    entity_id = generate_ulid()
    storage_base = "Workspaces/_by_id/linked-parent"
    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    unrelated = tmp_path / "unrelated-parent"
    target = unrelated / "_by_id" / "linked-parent"
    target.mkdir(parents=True)
    keep = target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    (entity_root / "Workspaces").symlink_to(
        unrelated,
        target_is_directory=True,
    )

    job = WorkspaceArtifactPurgeJob(
        entity_id=entity_id,
        storage_base=storage_base,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(db_session) == (0, 1)
    retained = await db_session.get(WorkspaceArtifactPurgeJob, job_id)
    assert retained is not None
    assert "parent is a symlink" in str(retained.last_error)
    assert keep.read_text(encoding="utf-8") == "keep"
    await db_session.delete(retained)
    await db_session.commit()


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_retries_after_filesystem_failure(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    job = WorkspaceArtifactPurgeJob(
        entity_id=generate_ulid(),
        storage_base="workspaces/retry-artifacts",
    )
    db_session.add(job)
    await db_session.commit()

    calls = 0

    def flaky_remove(_entity_id: str, _storage_base: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary filesystem failure")

    monkeypatch.setattr(
        workspace_artifact_purge,
        "_remove_artifact_tree",
        flaky_remove,
    )
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    checked_at = datetime(2026, 8, 23, tzinfo=timezone.utc)
    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        now=checked_at,
    ) == (0, 1)
    retained = await db_session.get(WorkspaceArtifactPurgeJob, job.id)
    assert retained is not None
    assert retained.attempt_count == 1
    assert "temporary filesystem failure" in str(retained.last_error)
    assert retained.next_attempt_at == checked_at + timedelta(minutes=15)

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        now=checked_at + timedelta(minutes=5),
    ) == (0, 0)
    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        now=checked_at + timedelta(minutes=15),
    ) == (1, 0)
    assert await db_session.get(WorkspaceArtifactPurgeJob, job.id) is None
    assert calls == 2


@pytest.mark.asyncio
async def test_workspace_artifact_cleanup_failure_does_not_starve_later_jobs(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from packages.core.services import entity_fs, workspace_artifact_purge

    checked_at = datetime(2026, 8, 23, tzinfo=timezone.utc)
    blocked = WorkspaceArtifactPurgeJob(
        entity_id=generate_ulid(),
        storage_base="Workspaces/_by_id/blocked",
        created_at=checked_at - timedelta(minutes=2),
    )
    ready = WorkspaceArtifactPurgeJob(
        entity_id=generate_ulid(),
        storage_base="Workspaces/_by_id/ready",
        created_at=checked_at - timedelta(minutes=1),
    )
    blocked_entity_id = blocked.entity_id
    db_session.add_all([blocked, ready])
    await db_session.flush()
    blocked_id = blocked.id
    ready_id = ready.id
    await db_session.commit()

    def remove(entity_id: str, _storage_base: str) -> None:
        if entity_id == blocked_entity_id:
            raise PermissionError("blocked artifact tree")

    monkeypatch.setattr(workspace_artifact_purge, "_remove_artifact_tree", remove)
    monkeypatch.setattr(
        entity_fs,
        "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ENABLED=True,
            MANOR_FS_ROOT=str(tmp_path),
            DEPLOYMENT_MODE="oss",
        ),
    )

    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        limit=1,
        now=checked_at,
    ) == (0, 1)
    assert await drain_workspace_artifact_purge_jobs(
        db_session,
        limit=1,
        now=checked_at + timedelta(minutes=1),
    ) == (1, 0)
    assert await db_session.get(WorkspaceArtifactPurgeJob, blocked_id) is not None
    assert await db_session.get(WorkspaceArtifactPurgeJob, ready_id) is None
