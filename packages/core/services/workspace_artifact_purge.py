"""Durably clean artifact files after their database projections are removed."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import (
    Document,
    DocumentChunk,
    DocumentFolder,
    DocumentGroupMember,
)
from packages.core.models.document_version import DocumentVersion
from packages.core.models.artifact_purge import WorkspaceArtifactPurgeJob
from packages.core.models.permission import (
    ResourceGrant,
    ResourceGrantPending,
    ResourceType,
    Share,
)
from packages.core.models.workspace import Workspace
from packages.core.services.entity_fs import (
    EntityFilesystemError,
    assert_entity_filesystem_ready,
    entity_filesystem_mutation_lock,
    get_entity_root,
)
from packages.core.services.comment_service import delete_resource_comments
from packages.core.services.workspace_artifacts import workspace_artifact_storage_base


logger = logging.getLogger(__name__)


ARTIFACT_CLEANUP_KIND_TREE = "tree"
ARTIFACT_CLEANUP_KIND_FILE = "file"
ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY = "empty_directory"
DOCUMENT_DERIVED_TREE_CACHE_ROOTS = (
    ".document-page-cache",
    ".slide-cache",
    ".slide-object-cache",
    ".doc-thumb-cache",
)
_ARTIFACT_CLEANUP_KINDS = {
    ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
    ARTIFACT_CLEANUP_KIND_TREE,
    ARTIFACT_CLEANUP_KIND_FILE,
}


_SAFE_DIR_FD_REMOVAL = (
    bool(getattr(shutil.rmtree, "avoids_symlink_attacks", False))
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


def document_derived_tree_cleanup_bases(document_ids) -> set[str]:
    normalized_ids = {
        normalized
        for value in document_ids
        if (normalized := str(value or "").strip())
    }
    return {
        f"{cache_root}/{document_id}"
        for cache_root in DOCUMENT_DERIVED_TREE_CACHE_ROOTS
        for document_id in normalized_ids
    }


def document_derived_file_cleanup_bases(document_ids) -> set[str]:
    return {
        f".manor-cache/document-thumbnails/{normalized}.jpg"
        for value in document_ids
        if (normalized := str(value or "").strip())
    }


def _validated_cleanup_bases(storage_bases) -> set[str]:
    validated: set[str] = set()
    for value in storage_bases:
        storage_base = str(value or "").strip().replace("\\", "/")
        parts = storage_base.split("/")
        if (
            not storage_base
            or storage_base.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Artifact cleanup path must stay below the entity root")
        validated.add(storage_base)
    return validated


def _validated_cleanup_kind(target_kind: str) -> str:
    normalized = str(target_kind or "").strip().lower()
    if normalized not in _ARTIFACT_CLEANUP_KINDS:
        raise ValueError(
            "Artifact cleanup target kind must be tree, file, or empty_directory"
        )
    return normalized


async def enqueue_artifact_cleanup_jobs(
    db: AsyncSession,
    entity_id: str,
    storage_bases,
    *,
    target_kind: str = ARTIFACT_CLEANUP_KIND_TREE,
) -> set[str]:
    """Persist idempotent entity-relative cleanup intents in this transaction."""
    cleanup_bases = _validated_cleanup_bases(storage_bases)
    cleanup_kind = _validated_cleanup_kind(target_kind)
    if not cleanup_bases:
        return set()

    values = [
        {
            "id": generate_ulid(),
            "entity_id": entity_id,
            "storage_base": cleanup_base,
            "target_kind": cleanup_kind,
            "attempt_count": 0,
        }
        for cleanup_base in sorted(cleanup_bases)
    ]
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert

        statement = (
            dialect_insert(WorkspaceArtifactPurgeJob)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    WorkspaceArtifactPurgeJob.entity_id,
                    WorkspaceArtifactPurgeJob.storage_base,
                ]
            )
        )
        await db.execute(statement)
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

        statement = (
            dialect_insert(WorkspaceArtifactPurgeJob)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    WorkspaceArtifactPurgeJob.entity_id,
                    WorkspaceArtifactPurgeJob.storage_base,
                ]
            )
        )
        await db.execute(statement)
    else:
        for value in values:
            try:
                async with db.begin_nested():
                    db.add(WorkspaceArtifactPurgeJob(**value))
                    await db.flush()
            except IntegrityError:
                continue

    existing_jobs = (await db.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == entity_id,
            WorkspaceArtifactPurgeJob.storage_base.in_(cleanup_bases),
        ).with_for_update()
    )).scalars().all()
    conflicting = [
        job.storage_base
        for job in existing_jobs
        if job.target_kind != cleanup_kind
    ]
    if conflicting:
        raise ValueError(
            "Artifact cleanup path already has a different target kind: "
            f"{conflicting[0]}"
        )
    persisted_bases = {job.storage_base for job in existing_jobs}
    missing = cleanup_bases - persisted_bases
    if missing:
        raise RuntimeError(
            "Artifact cleanup intent was not persisted: "
            f"{sorted(missing)[0]}"
        )
    await db.flush()
    return cleanup_bases


async def clear_artifact_cleanup_jobs(
    db: AsyncSession,
    entity_id: str,
    storage_bases,
) -> None:
    """Remove cleanup intents only after their exact targets were removed."""
    cleanup_bases = _validated_cleanup_bases(storage_bases)
    if not cleanup_bases:
        return
    await db.execute(delete(WorkspaceArtifactPurgeJob).where(
        WorkspaceArtifactPurgeJob.entity_id == entity_id,
        WorkspaceArtifactPurgeJob.storage_base.in_(cleanup_bases),
    ))
    await db.flush()


def _artifact_link_tombstone_name(target_name: str) -> str:
    digest = hashlib.sha256(target_name.encode("utf-8")).hexdigest()[:24]
    return f".manor-artifact-purge-{digest}"


def _open_owned_directory(
    parent_fd: int,
    name: str,
    *,
    symlink_error: str,
    not_directory_error: str,
) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        try:
            mode = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            raise EntityFilesystemError(symlink_error) from exc
        if not stat.S_ISDIR(mode):
            raise EntityFilesystemError(not_directory_error) from exc
        raise EntityFilesystemError(
            f"Workspace artifact purge directory cannot be opened safely: {name}"
        ) from exc


@contextmanager
def _artifact_target_parent(
    entity_id: str,
    storage_base: str,
) -> Iterator[tuple[int, str, str] | None]:
    fs_root = assert_entity_filesystem_ready()
    if not _SAFE_DIR_FD_REMOVAL:
        raise EntityFilesystemError(
            "Workspace artifact purge requires symlink-safe directory operations"
        )
    fs_root = os.path.realpath(fs_root)
    entity_root = os.path.abspath(os.path.join(fs_root, entity_id))
    if (
        entity_root == fs_root
        or os.path.commonpath([fs_root, entity_root]) != fs_root
    ):
        raise ValueError("Workspace artifact purge entity escaped the filesystem root")
    target = os.path.abspath(os.path.join(entity_root, storage_base))
    if (
        target == entity_root
        or os.path.commonpath([entity_root, target]) != entity_root
    ):
        raise ValueError("Workspace artifact purge path escaped the entity filesystem")

    relative_parts = os.path.relpath(target, entity_root).split(os.sep)
    open_fds: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fs_root_fd = os.open(fs_root, flags)
        except OSError as exc:
            raise EntityFilesystemError(
                f"Entity filesystem root is unavailable: {fs_root}"
            ) from exc
        open_fds.append(fs_root_fd)

        entity_fd = _open_owned_directory(
            fs_root_fd,
            entity_id,
            symlink_error="Entity filesystem root must not be a symlink",
            not_directory_error="Entity filesystem root is not a directory",
        )
        if entity_fd is None:
            yield None
            return
        open_fds.append(entity_fd)

        parent_fd = entity_fd
        current = entity_root
        for part in relative_parts[:-1]:
            current = os.path.join(current, part)
            next_fd = _open_owned_directory(
                parent_fd,
                part,
                symlink_error=(
                    f"Workspace artifact purge parent is a symlink: {current}"
                ),
                not_directory_error=(
                    f"Workspace artifact purge parent is not a directory: {current}"
                ),
            )
            if next_fd is None:
                yield None
                return
            open_fds.append(next_fd)
            parent_fd = next_fd

        yield parent_fd, relative_parts[-1], target
    finally:
        for directory_fd in reversed(open_fds):
            os.close(directory_fd)


def _remove_artifact_tree(entity_id: str, storage_base: str) -> None:
    with _artifact_target_parent(entity_id, storage_base) as resolved:
        if resolved is None:
            return
        parent_fd, target_name, target = resolved
        tombstone_name = _artifact_link_tombstone_name(target_name)
        try:
            tombstone_mode = os.stat(
                tombstone_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            tombstone_mode = None
        if tombstone_mode is not None:
            if not stat.S_ISLNK(tombstone_mode):
                raise EntityFilesystemError(
                    "Workspace artifact purge tombstone has an unexpected type: "
                    f"{target}"
                )
            os.unlink(tombstone_name, dir_fd=parent_fd)

        try:
            target_mode = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(target_mode):
            # Move the exact directory entry before deleting it. If the entry
            # changed after the lstat, the unexpected object is restored and
            # the durable cleanup job is retained instead of deleting user data.
            os.rename(
                target_name,
                tombstone_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            moved_mode = os.stat(
                tombstone_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
            if not stat.S_ISLNK(moved_mode):
                try:
                    os.stat(
                        target_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    os.rename(
                        tombstone_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                raise EntityFilesystemError(
                    "Workspace artifact purge target changed during deletion: "
                    f"{target}"
                )
            os.unlink(tombstone_name, dir_fd=parent_fd)
            return
        if not stat.S_ISDIR(target_mode):
            raise EntityFilesystemError(
                f"Workspace artifact purge target is not a directory: {target}"
            )
        shutil.rmtree(target_name, dir_fd=parent_fd)


def _remove_artifact_file(
    entity_id: str,
    storage_base: str,
    cleanup_token: str,
) -> None:
    """Atomically quarantine and unlink one expected regular file or symlink."""
    with _artifact_target_parent(entity_id, storage_base) as resolved:
        if resolved is None:
            return
        parent_fd, target_name, target = resolved
        tombstone_name = _artifact_link_tombstone_name(
            f"file:{cleanup_token}:{target_name}",
        )
        try:
            tombstone_mode = os.stat(
                tombstone_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            tombstone_mode = None
        if tombstone_mode is not None:
            if not (stat.S_ISREG(tombstone_mode) or stat.S_ISLNK(tombstone_mode)):
                raise EntityFilesystemError(
                    f"Artifact file cleanup tombstone has an unexpected type: {target}"
                )
            os.unlink(tombstone_name, dir_fd=parent_fd)
            return

        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not (stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode)):
            raise EntityFilesystemError(
                f"Artifact file cleanup target is not a file: {target}"
            )

        os.rename(
            target_name,
            tombstone_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        moved_stat = os.stat(
            tombstone_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        same_entry = (
            moved_stat.st_dev == target_stat.st_dev
            and moved_stat.st_ino == target_stat.st_ino
            and stat.S_IFMT(moved_stat.st_mode) == stat.S_IFMT(target_stat.st_mode)
        )
        if not same_entry:
            try:
                os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.rename(
                    tombstone_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            raise EntityFilesystemError(
                f"Artifact file cleanup target changed during deletion: {target}"
            )
        os.unlink(tombstone_name, dir_fd=parent_fd)


def _remove_empty_artifact_directory(entity_id: str, storage_base: str) -> None:
    """Remove only an empty directory; never widen cleanup to its contents."""
    with _artifact_target_parent(entity_id, storage_base) as resolved:
        if resolved is None:
            return
        parent_fd, target_name, target = resolved
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(target_stat.st_mode):
            raise EntityFilesystemError(
                f"Artifact empty-directory cleanup target is not a directory: {target}"
            )
        os.rmdir(target_name, dir_fd=parent_fd)


async def _remove_artifact_job_target(
    db: AsyncSession,
    job: WorkspaceArtifactPurgeJob,
) -> None:
    if job.target_kind == ARTIFACT_CLEANUP_KIND_FILE:
        # The database deletion and physical retry are intentionally separate.
        # A writer can legitimately reuse this path between them, so recheck
        # ownership while holding the same entity filesystem lock as writers.
        reused_document_id = (await db.execute(
            select(Document.id)
            .where(
                Document.entity_id == job.entity_id,
                Document.fs_path == job.storage_base,
            )
            .limit(1)
        )).scalar_one_or_none()
        if reused_document_id is not None:
            return
        await asyncio.to_thread(
            _remove_artifact_file,
            job.entity_id,
            job.storage_base,
            job.id,
        )
        return
    if job.target_kind == ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY:
        await asyncio.to_thread(
            _remove_empty_artifact_directory,
            job.entity_id,
            job.storage_base,
        )
        return
    await asyncio.to_thread(
        _remove_artifact_tree,
        job.entity_id,
        job.storage_base,
    )


async def purge_workspace_artifacts(
    db: AsyncSession,
    workspace: Workspace,
) -> None:
    """Delete one Workspace's unique artifact root, projections, and folders."""

    artifact_folder_id = str(workspace.artifact_folder_id or "").strip()
    if not artifact_folder_id:
        return
    storage_base = workspace_artifact_storage_base(artifact_folder_id)
    folder_tree = select(DocumentFolder.id).where(
        DocumentFolder.id == artifact_folder_id,
        DocumentFolder.entity_id == workspace.entity_id,
    ).cte("workspace_artifact_folder_tree", recursive=True)
    folder_tree = folder_tree.union(
        select(DocumentFolder.id).join(
            folder_tree,
            DocumentFolder.parent_id == folder_tree.c.id,
        ).where(DocumentFolder.entity_id == workspace.entity_id)
    )
    folder_ids = set((await db.execute(select(folder_tree.c.id))).scalars().all())
    path_clause = or_(
        Document.fs_path == storage_base,
        Document.fs_path.startswith(f"{storage_base}/", autoescape=True),
    )
    document_scope = (
        or_(path_clause, Document.folder_id.in_(folder_ids))
        if folder_ids
        else path_clause
    )
    document_rows = (await db.execute(
        select(Document.id, Document.fs_path)
        .where(
            Document.entity_id == workspace.entity_id,
            document_scope,
        )
        .order_by(Document.id)
        .with_for_update()
    )).all()
    document_ids = {row.id for row in document_rows}
    document_source_bases = _validated_cleanup_bases(
        row.fs_path for row in document_rows if row.fs_path
    )
    external_document_source_bases = {
        source_base
        for source_base in document_source_bases
        if source_base != storage_base
        and not source_base.startswith(f"{storage_base}/")
    }
    if document_ids:
        await delete_resource_comments(
            db,
            workspace.entity_id,
            "document",
            document_ids,
        )
        await db.execute(delete(DocumentGroupMember).where(
            DocumentGroupMember.document_id.in_(document_ids)
        ))
        await db.execute(delete(DocumentChunk).where(
            DocumentChunk.document_id.in_(document_ids)
        ))
        await db.execute(delete(DocumentVersion).where(
            DocumentVersion.document_id.in_(document_ids)
        ))
        for model in (ResourceGrant, ResourceGrantPending, Share):
            await db.execute(delete(model).where(
                model.resource_type == ResourceType.DOCUMENT,
                model.resource_id.in_(document_ids),
            ))
        await db.execute(delete(Document).where(Document.id.in_(document_ids)))

    if folder_ids:
        for model in (ResourceGrant, ResourceGrantPending, Share):
            await db.execute(delete(model).where(
                model.resource_type == ResourceType.DOCUMENT_FOLDER,
                model.resource_id.in_(folder_ids),
            ))
        await db.execute(delete(DocumentFolder).where(DocumentFolder.id.in_(folder_ids)))

    cleanup_bases = {storage_base} | document_derived_tree_cleanup_bases(document_ids)
    await enqueue_artifact_cleanup_jobs(
        db,
        workspace.entity_id,
        cleanup_bases,
    )
    await enqueue_artifact_cleanup_jobs(
        db,
        workspace.entity_id,
        document_derived_file_cleanup_bases(document_ids)
        | external_document_source_bases,
        target_kind=ARTIFACT_CLEANUP_KIND_FILE,
    )


async def drain_workspace_artifact_purge_jobs(
    db: AsyncSession,
    *,
    limit: int = 100,
    now: datetime | None = None,
    entity_id: str | None = None,
    storage_bases=None,
    storage_base_prefix: str | None = None,
    target_kind: str | None = None,
) -> tuple[int, int]:
    """Remove artifact targets only after their database deletion has committed.

    Each durable job is committed independently. A failed or interrupted
    cleanup therefore remains available to the next nightly purge sweep.
    Callers must invoke this only after committing their own mutations.
    """

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    conditions = [or_(
        WorkspaceArtifactPurgeJob.next_attempt_at.is_(None),
        WorkspaceArtifactPurgeJob.next_attempt_at <= checked_at,
    )]
    if entity_id is not None:
        conditions.append(WorkspaceArtifactPurgeJob.entity_id == entity_id)
    if storage_bases is not None:
        selected_bases = _validated_cleanup_bases(storage_bases)
        if not selected_bases:
            return 0, 0
        conditions.append(WorkspaceArtifactPurgeJob.storage_base.in_(selected_bases))
    if storage_base_prefix is not None:
        selected_prefix = str(storage_base_prefix).strip().replace("\\", "/")
        if not selected_prefix.endswith("/"):
            raise ValueError("Artifact cleanup prefix must end with a slash")
        _validated_cleanup_bases({f"{selected_prefix}target"})
        conditions.append(WorkspaceArtifactPurgeJob.storage_base.startswith(
            selected_prefix,
            autoescape=True,
        ))
    if target_kind is not None:
        conditions.append(
            WorkspaceArtifactPurgeJob.target_kind
            == _validated_cleanup_kind(target_kind)
        )
    job_ids = list((await db.execute(
        select(WorkspaceArtifactPurgeJob.id)
        .where(*conditions)
        .order_by(
            func.coalesce(
                WorkspaceArtifactPurgeJob.next_attempt_at,
                WorkspaceArtifactPurgeJob.created_at,
            ),
            WorkspaceArtifactPurgeJob.id,
        )
        .limit(max(1, min(int(limit), 1000)))
    )).scalars().all())
    await db.rollback()

    cleaned = 0
    failed = 0
    for job_id in job_ids:
        try:
            job = (await db.execute(
                select(WorkspaceArtifactPurgeJob)
                .where(
                    WorkspaceArtifactPurgeJob.id == job_id,
                    or_(
                        WorkspaceArtifactPurgeJob.next_attempt_at.is_(None),
                        WorkspaceArtifactPurgeJob.next_attempt_at <= checked_at,
                    ),
                )
                .with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if job is None:
                await db.rollback()
                continue
            try:
                entity_root = get_entity_root(job.entity_id)
                try:
                    entity_root_mode = os.lstat(entity_root).st_mode
                except FileNotFoundError:
                    entity_root_mode = None
                if entity_root_mode is None or stat.S_ISDIR(entity_root_mode):
                    assert_entity_filesystem_ready()
                    async with entity_filesystem_mutation_lock(entity_root):
                        await _remove_artifact_job_target(db, job)
                else:
                    await _remove_artifact_job_target(db, job)
            except Exception as exc:  # noqa: BLE001
                attempts = int(job.attempt_count or 0) + 1
                delay_minutes = min(24 * 60, 15 * (2 ** min(attempts - 1, 7)))
                job.attempt_count = attempts
                job.last_error = f"{exc.__class__.__name__}: {exc}"[:2000]
                job.next_attempt_at = checked_at + timedelta(minutes=delay_minutes)
                await db.commit()
                failed += 1
                logger.warning(
                    "Artifact cleanup failed job=%s entity=%s attempt=%s",
                    job.id,
                    job.entity_id,
                    job.attempt_count,
                )
                continue
            await db.delete(job)
            await db.commit()
            cleaned += 1
        except Exception:  # noqa: BLE001
            await db.rollback()
            failed += 1
            logger.exception(
                "Artifact cleanup transaction failed job=%s",
                job_id,
            )
    return cleaned, failed


__all__ = [
    "ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY",
    "ARTIFACT_CLEANUP_KIND_FILE",
    "ARTIFACT_CLEANUP_KIND_TREE",
    "DOCUMENT_DERIVED_TREE_CACHE_ROOTS",
    "clear_artifact_cleanup_jobs",
    "document_derived_file_cleanup_bases",
    "document_derived_tree_cleanup_bases",
    "drain_workspace_artifact_purge_jobs",
    "enqueue_artifact_cleanup_jobs",
    "purge_workspace_artifacts",
]
