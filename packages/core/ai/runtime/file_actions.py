"""Runtime-owned facade for entity filesystem mutations and Knowledge sync."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from packages.core.ai.runtime.file_contracts import FileMutationAction

if TYPE_CHECKING:
    from packages.core.services.entity_fs import EntityFileTargetKind

logger = logging.getLogger(__name__)


class RuntimeFileProjectionError(RuntimeError):
    """A visible file could not be committed with its Knowledge projection."""

    def __init__(self, rel_path: str, reason: str | None) -> None:
        self.rel_path = rel_path
        self.reason = reason or "missing_document_id"
        super().__init__(
            f"Knowledge projection failed for {rel_path}: {self.reason}",
        )


class RuntimeFileCommitError(RuntimeError):
    """The filesystem portion of a Runtime file transaction could not commit."""


@dataclass
class _RuntimeProjectedFile:
    rollback_snapshot: Any
    written_version: Any
    projected: bool = False


@dataclass
class _RuntimeDeletedPath:
    delete_snapshot: Any
    projected: bool = False


class RuntimeFileProjectionTransaction:
    """Commit filesystem bytes and Knowledge rows as one Runtime operation."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.db: Any | None = None
        self._session_context: Any | None = None
        self._files: dict[str, _RuntimeProjectedFile] = {}
        self._deletions: dict[str, _RuntimeDeletedPath] = {}
        self._document_ids: list[str] = []
        self._committed = False

    async def __aenter__(self) -> RuntimeFileProjectionTransaction:
        from packages.core.database import async_session

        self._session_context = async_session()
        self.db = await self._session_context.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        from packages.core.services.entity_fs import finish_entity_filesystem_mutation

        async def close_transaction() -> None:
            rollback_error: Exception | None = None
            if not self._committed:
                if self.db is not None:
                    try:
                        await self.db.rollback()
                    except Exception as error:  # noqa: BLE001
                        rollback_error = error
                for projected_file in reversed(list(self._files.values())):
                    try:
                        self._rollback_projected_file(projected_file)
                    except Exception as error:  # noqa: BLE001
                        logger.critical(
                            "Runtime file projection rollback failed for %s",
                            projected_file.rollback_snapshot.rel_path,
                            exc_info=True,
                        )
                        rollback_error = rollback_error or error
                for deleted_path in reversed(list(self._deletions.values())):
                    try:
                        deleted_path.delete_snapshot.rollback()
                    except Exception as error:  # noqa: BLE001
                        logger.critical(
                            "Runtime file delete rollback failed for %s",
                            deleted_path.delete_snapshot.rel_path,
                            exc_info=True,
                        )
                        rollback_error = rollback_error or error
            if self._session_context is not None:
                try:
                    await self._session_context.__aexit__(exc_type, exc, traceback)
                except Exception:  # noqa: BLE001
                    if not self._committed:
                        raise
                    logger.warning(
                        "Committed Runtime file transaction session cleanup failed "
                        "for entity %s",
                        self.entity_id,
                        exc_info=True,
                    )
                    close_session = getattr(self.db, "close", None)
                    if callable(close_session):
                        try:
                            await close_session()
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "Committed Runtime file transaction fallback session "
                                "close failed for entity %s",
                                self.entity_id,
                                exc_info=True,
                            )
            if rollback_error is not None:
                raise RuntimeError(
                    "Runtime file projection rollback could not restore prior state",
                ) from rollback_error

        await finish_entity_filesystem_mutation(close_transaction())
        return False

    def _rollback_projected_file(self, projected_file: _RuntimeProjectedFile) -> None:
        """Refresh the installed version when possible, then restore safely."""
        if projected_file.written_version is None:
            from packages.core.services.entity_fs import open_entity_file_snapshot

            snapshot = projected_file.rollback_snapshot
            try:
                with open_entity_file_snapshot(
                    self.entity_id,
                    snapshot.rel_path,
                    expected_resolved_path=snapshot.resolved_path,
                ) as current:
                    projected_file.written_version = current.version
            except Exception:  # noqa: BLE001
                # rollback() distinguishes a genuinely missing target from an
                # existing target that cannot be verified and fails closed.
                pass
        projected_file.rollback_snapshot.rollback(
            projected_file.written_version,
        )

    def _assert_can_stage(self, rel_path: str) -> None:
        if self.db is None or self._committed:
            raise RuntimeError("Runtime file projection transaction is not writable")
        entity_root = runtime_entity_file_root(self.entity_id)
        if not entity_root:
            raise RuntimeError("Entity filesystem is not enabled")
        expected_path = os.path.abspath(os.path.join(entity_root, rel_path))
        if expected_path in self._files or expected_path in self._deletions:
            raise RuntimeError(f"Duplicate Runtime file transaction path: {rel_path}")

    def _record_written_file(
        self,
        *,
        rel_path: str,
        resolved_path: str,
        expected_content_sha256: str,
        rollback_snapshot: Any,
    ) -> None:
        from packages.core.services.entity_fs import open_entity_file_snapshot

        key = os.path.abspath(resolved_path)
        projected_file = self._files.get(key)
        if projected_file is None:
            raise RuntimeError("Runtime write target changed after snapshot staging")
        with open_entity_file_snapshot(
            self.entity_id,
            rel_path,
            expected_resolved_path=resolved_path,
            expected_content_sha256=expected_content_sha256,
        ) as written:
            if projected_file.rollback_snapshot is not rollback_snapshot:
                raise RuntimeError("Runtime write rollback snapshot changed")
            projected_file.written_version = written.version

    def _stage_and_write(
        self,
        *,
        rel_path: str,
        expected_content_sha256: str,
        expected_source_version: Any | None,
        writer: Callable[[Any | None], str],
    ) -> str:
        from packages.core.services.entity_fs import stage_entity_file_rollback_snapshot

        rollback_snapshot = None
        try:
            self._assert_can_stage(rel_path)
            rollback_snapshot = stage_entity_file_rollback_snapshot(
                self.entity_id,
                rel_path,
                expected_source_version=expected_source_version,
            )
            key = os.path.abspath(rollback_snapshot.resolved_path)
            self._files[key] = _RuntimeProjectedFile(
                rollback_snapshot=rollback_snapshot,
                written_version=None,
            )
            resolved_path = writer(rollback_snapshot.source_version)
            if os.path.abspath(resolved_path) != key:
                raise RuntimeError("Runtime writer returned a different target path")
            self._record_written_file(
                rel_path=rel_path,
                resolved_path=resolved_path,
                expected_content_sha256=expected_content_sha256,
                rollback_snapshot=rollback_snapshot,
            )
            return resolved_path
        except Exception as exc:
            if rollback_snapshot is None:
                raise RuntimeFileCommitError(str(exc)) from exc
            try:
                projected_file = self._files[
                    os.path.abspath(rollback_snapshot.resolved_path)
                ]
                self._rollback_projected_file(projected_file)
            except Exception:  # noqa: BLE001
                logger.critical(
                    "Runtime staged write rollback failed for %s",
                    rel_path,
                    exc_info=True,
                )
            raise RuntimeFileCommitError(str(exc)) from exc

    def delete_path(
        self,
        rel_path: str,
        *,
        expected_resolved_path: str,
        expected_source_version: Any | None = None,
    ) -> EntityFileTargetKind:
        """Stage a file or empty directory deletion until projection commits."""
        from packages.core.services.entity_fs import (
            EntityPathDeleteStagingError,
            stage_entity_path_delete_snapshot,
        )

        try:
            self._assert_can_stage(rel_path)
            snapshot = stage_entity_path_delete_snapshot(
                self.entity_id,
                rel_path,
                expected_resolved_path=expected_resolved_path,
                expected_source_version=expected_source_version,
            )
            self._deletions[os.path.abspath(snapshot.resolved_path)] = (
                _RuntimeDeletedPath(delete_snapshot=snapshot)
            )
            return snapshot.target_kind
        except EntityPathDeleteStagingError as exc:
            snapshot = exc.snapshot
            self._deletions[os.path.abspath(snapshot.resolved_path)] = (
                _RuntimeDeletedPath(delete_snapshot=snapshot)
            )
            raise RuntimeFileCommitError(str(exc)) from exc
        except Exception as exc:
            raise RuntimeFileCommitError(str(exc)) from exc

    def write_bytes(
        self,
        rel_path: str,
        data: bytes,
        *,
        expected_content_sha256: str,
        expected_size: int | None = None,
        allow_empty: bool = False,
        require_missing: bool = False,
        expected_source_version: Any | None = None,
    ) -> str:
        """Stage prior state, atomically write bytes, and pin the new version."""
        return self._stage_and_write(
            rel_path=rel_path,
            expected_content_sha256=expected_content_sha256,
            expected_source_version=expected_source_version,
            writer=lambda staged_version: runtime_write_entity_file_atomic(
                self.entity_id,
                rel_path,
                data,
                expected_size=expected_size,
                allow_empty=allow_empty,
                require_missing=require_missing,
                expected_source_version=staged_version,
            ),
        )

    def copy_file(
        self,
        rel_path: str,
        source_path: str,
        *,
        expected_content_sha256: str,
        expected_size: int | None = None,
        allow_empty: bool = False,
        require_missing: bool = False,
        expected_source_version: Any | None = None,
    ) -> str:
        """Stage prior state, atomically copy a file, and pin the new version."""
        return self._stage_and_write(
            rel_path=rel_path,
            expected_content_sha256=expected_content_sha256,
            expected_source_version=expected_source_version,
            writer=lambda staged_version: runtime_copy_entity_file_atomic(
                self.entity_id,
                rel_path,
                source_path,
                expected_size=expected_size,
                allow_empty=allow_empty,
                require_missing=require_missing,
                expected_source_version=staged_version,
            ),
        )

    async def project_file(self, **kwargs: Any) -> Any:
        """Project one staged file using the transaction's database session."""
        if self.db is None:
            raise RuntimeError("Runtime file projection transaction is not open")
        resolved_path = os.path.abspath(str(kwargs.get("abs_path") or ""))
        projected_file = self._files.get(resolved_path)
        if projected_file is None:
            raise RuntimeError("Knowledge projection target was not written by this transaction")
        if projected_file.projected:
            raise RuntimeError("Runtime file already has a staged Knowledge projection")
        sync = await runtime_sync_entity_file_to_knowledge(
            entity_id=self.entity_id,
            db=self.db,
            **kwargs,
        )
        rel_path = projected_file.rollback_snapshot.rel_path
        if not sync.synced or not sync.document_id:
            raise RuntimeFileProjectionError(rel_path, sync.reason)
        projected_file.projected = True
        if getattr(sync, "content_changed", True):
            self._document_ids.append(sync.document_id)
        return sync

    async def project_delete(self, rel_path: str) -> bool:
        """Retire Knowledge rows for one staged physical deletion."""
        from packages.core.services.entity_fs import EntityFileTargetKind

        if self.db is None:
            raise RuntimeError("Runtime file projection transaction is not open")
        entity_root = runtime_entity_file_root(self.entity_id)
        if not entity_root:
            raise RuntimeError("Entity filesystem is not enabled")
        resolved_path = os.path.abspath(os.path.join(entity_root, rel_path))
        deleted_path = self._deletions.get(resolved_path)
        if deleted_path is None:
            raise RuntimeError("Knowledge retirement target was not staged by this transaction")
        if deleted_path.projected:
            raise RuntimeError("Runtime file delete already has a staged Knowledge projection")
        changed = await runtime_trash_knowledge_path(
            self.entity_id,
            rel_path,
            is_directory=(
                deleted_path.delete_snapshot.target_kind
                is EntityFileTargetKind.DIRECTORY
            ),
            db=self.db,
        )
        deleted_path.projected = True
        return changed

    async def commit(self) -> None:
        """Commit all Document rows, then discard the exact file backups."""
        from packages.core.services.entity_fs import finish_entity_filesystem_mutation

        if self.db is None:
            raise RuntimeError("Runtime file projection transaction is not open")
        if self._committed:
            raise RuntimeError("Runtime file projection transaction is already committed")
        if not self._files and not self._deletions:
            raise RuntimeError("Runtime file projection transaction has no staged paths")
        if (
            any(not item.projected for item in self._files.values())
            or any(not item.projected for item in self._deletions.values())
        ):
            raise RuntimeError("Every staged Runtime path must have a Knowledge projection")

        async def commit_transaction() -> None:
            from packages.core.services.entity_fs import EntityFileTargetKind
            from packages.core.services.workspace_artifact_purge import (
                ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
                ARTIFACT_CLEANUP_KIND_FILE,
                clear_artifact_cleanup_jobs,
                enqueue_artifact_cleanup_jobs,
            )

            if not runtime_entity_file_root(self.entity_id):
                raise RuntimeError("Entity filesystem is not enabled")

            cleanup_records: list[tuple[Any, str, str]] = []
            for projected_file in self._files.values():
                snapshot = projected_file.rollback_snapshot
                backup_rel_path = snapshot.backup_rel_path
                if backup_rel_path:
                    cleanup_records.append((
                        snapshot,
                        ARTIFACT_CLEANUP_KIND_FILE,
                        backup_rel_path,
                    ))
            for deleted_path in self._deletions.values():
                snapshot = deleted_path.delete_snapshot
                cleanup_kind = (
                    ARTIFACT_CLEANUP_KIND_FILE
                    if snapshot.target_kind is EntityFileTargetKind.FILE
                    else ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY
                )
                cleanup_records.append((
                    snapshot,
                    cleanup_kind,
                    snapshot.backup_rel_path,
                ))

            for cleanup_kind in {
                ARTIFACT_CLEANUP_KIND_FILE,
                ARTIFACT_CLEANUP_KIND_EMPTY_DIRECTORY,
            }:
                cleanup_paths = {
                    rel_path
                    for _snapshot, kind, rel_path in cleanup_records
                    if kind == cleanup_kind
                }
                if cleanup_paths:
                    await enqueue_artifact_cleanup_jobs(
                        self.db,
                        self.entity_id,
                        cleanup_paths,
                        target_kind=cleanup_kind,
                    )

            await self.db.commit()
            self._committed = True
            cleaned_paths: set[str] = set()
            for projected_file in self._files.values():
                try:
                    projected_file.rollback_snapshot.commit()
                    cleaned_paths.update(
                        rel_path
                        for snapshot, _kind, rel_path in cleanup_records
                        if snapshot is projected_file.rollback_snapshot
                    )
                except Exception:  # noqa: BLE001
                    logger.critical(
                        "Committed Runtime file backup cleanup failed for %s",
                        projected_file.rollback_snapshot.rel_path,
                        exc_info=True,
                    )
            for deleted_path in self._deletions.values():
                try:
                    deleted_path.delete_snapshot.commit()
                    cleaned_paths.update(
                        rel_path
                        for snapshot, _kind, rel_path in cleanup_records
                        if snapshot is deleted_path.delete_snapshot
                    )
                except Exception:  # noqa: BLE001
                    logger.critical(
                        "Committed Runtime staged delete cleanup failed for %s",
                        deleted_path.delete_snapshot.rel_path,
                        exc_info=True,
                    )
            if cleaned_paths:
                try:
                    await clear_artifact_cleanup_jobs(
                        self.db,
                        self.entity_id,
                        cleaned_paths,
                    )
                    await self.db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await self.db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning(
                        "Committed Runtime file cleanup intents remain queued",
                        exc_info=True,
                    )
            from packages.core.services.tool_cache_version import bump_tool_cache_version

            await bump_tool_cache_version(self.entity_id, "documents")
            for document_id in dict.fromkeys(self._document_ids):
                runtime_trigger_document_embeddings(document_id)
        await finish_entity_filesystem_mutation(commit_transaction())


class RuntimeFileProjectionTransactionFactory:
    """Create the shared file/Knowledge commit boundary for Runtime tools."""

    @staticmethod
    def create(entity_id: str) -> RuntimeFileProjectionTransaction:
        return RuntimeFileProjectionTransaction(entity_id)


class RuntimeFileCommitGuardFactory:
    """Build commit-time guards for all Runtime-owned file writers."""

    @staticmethod
    def create(
        entity_id: str,
        *,
        require_missing: bool = False,
    ) -> Callable[[str, bool, str], None]:
        from packages.core.ai.runtime.authorization_receipts import (
            RuntimeToolAuthorizationWriteError,
            runtime_current_tool_authorization_receipt,
        )

        def revalidate(path: str, target_exists: bool, resolved_path: str) -> None:
            if require_missing and target_exists:
                raise RuntimeToolAuthorizationWriteError(
                    "File target appeared after collision-safe path selection"
                )
            receipt = runtime_current_tool_authorization_receipt()
            if not receipt or not receipt.workspace_id or receipt.capability_id != "file.write":
                return
            import os

            entity_root = runtime_entity_file_root(entity_id)
            normalized_path = runtime_normalize_entity_file_path(path)
            if not entity_root or not normalized_path:
                raise RuntimeToolAuthorizationWriteError(
                    "Workspace file target could not be resolved at commit"
                )
            root = os.path.abspath(entity_root)
            resolved_target = os.path.realpath(os.path.join(root, normalized_path))
            if os.path.abspath(resolved_path) != resolved_target:
                raise RuntimeToolAuthorizationWriteError(
                    "Workspace file target changed before commit"
                )
            try:
                resolved_rel = runtime_normalize_entity_file_path(
                    os.path.relpath(resolved_target, root)
                )
            except ValueError as exc:
                raise RuntimeToolAuthorizationWriteError(
                    "Workspace file target escaped its entity at commit"
                ) from exc
            if resolved_rel != normalized_path:
                raise RuntimeToolAuthorizationWriteError(
                    "Workspace file targets may not traverse filesystem aliases"
                )
            if receipt.authorizes_workspace_file_commit(
                entity_id=entity_id,
                path=normalized_path,
                target_exists=target_exists,
            ):
                return
            raise RuntimeToolAuthorizationWriteError(
                "Workspace file target or existence changed after approval"
            )

        return revalidate


def runtime_entity_file_root(entity_id: str) -> str | None:
    """Return the entity filesystem root when Runtime file storage is enabled."""

    from packages.core.config import get_settings
    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        canonical_entity_root,
    )

    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        return None
    root = canonical_entity_root(entity_id)
    if os.path.lexists(root) and (os.path.islink(root) or not os.path.isdir(root)):
        raise EntityFilesystemError(
            "Entity filesystem root changed or is not a safe directory"
        )
    return root


def runtime_user_visible_file_path(path: str) -> bool:
    """Return whether a relative entity path is user-visible Knowledge content."""

    from packages.core.services.knowledge_visibility import is_user_visible_path

    return is_user_visible_path(path)


def runtime_normalize_entity_file_path(path: str) -> str:
    """Normalize an entity-relative file path using Knowledge visibility rules."""

    from packages.core.services.knowledge_visibility import normalize_rel_path

    return normalize_rel_path(path)


def runtime_write_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    data: bytes,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
    require_missing: bool = False,
    expected_source_version: Any | None = None,
) -> str:
    """Persist bytes through the entity filesystem write guard."""

    from packages.core.services.entity_fs import write_entity_file_atomic

    return write_entity_file_atomic(
        entity_id,
        rel_path,
        data,
        expected_size=expected_size,
        allow_empty=allow_empty,
        write_precondition=RuntimeFileCommitGuardFactory.create(
            entity_id,
            require_missing=require_missing,
        ),
        expected_source_version=expected_source_version,
    )


def runtime_copy_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    source_path: str,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
    require_missing: bool = False,
    expected_source_version: Any | None = None,
) -> str:
    """Persist a local file through the same Runtime commit guard as bytes."""

    from packages.core.services.entity_fs import copy_entity_file_atomic

    return copy_entity_file_atomic(
        entity_id,
        rel_path,
        source_path,
        expected_size=expected_size,
        allow_empty=allow_empty,
        write_precondition=RuntimeFileCommitGuardFactory.create(
            entity_id,
            require_missing=require_missing,
        ),
        expected_source_version=expected_source_version,
    )


@asynccontextmanager
async def runtime_entity_filesystem_mutation_lock(entity_root: str):
    """Hold the shared entity mutation lock across file and Knowledge writes."""

    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    async with entity_filesystem_mutation_lock(entity_root):
        yield


async def runtime_guard_file_mutation(
    *,
    entity_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    action: FileMutationAction | str,
    paths: list[str],
    approval_token: str | None = None,
    content_preview: Any = None,
    approval_payload: Any = None,
) -> str | None:
    """Run AI file mutation approval policy through the Runtime file boundary."""

    from packages.core.services.ai_file_permissions import guard_ai_file_mutation

    return await guard_ai_file_mutation(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        task_id=task_id,
        runtime_envelope=runtime_envelope,
        tool_name=tool_name,
        action=action,
        paths=paths,
        approval_token=approval_token,
        content_preview=content_preview,
        approval_payload=approval_payload,
    )


async def runtime_guard_file_resource_access(
    *,
    entity_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    action: FileMutationAction | str,
    paths: list[str],
) -> str | None:
    """Check a mutation target's ACL before inspecting its bytes or metadata."""

    from packages.core.services.ai_file_permissions import (
        guard_ai_file_resource_access,
    )

    return await guard_ai_file_resource_access(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        task_id=task_id,
        runtime_envelope=runtime_envelope,
        tool_name=tool_name,
        action=action,
        paths=paths,
    )


async def runtime_guard_file_read_access(
    *,
    entity_id: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    paths: list[str],
) -> str | None:
    """Check Knowledge read ACLs before using existing files as input."""

    from packages.core.services.ai_file_permissions import guard_ai_file_read_access

    return await guard_ai_file_read_access(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        runtime_envelope=runtime_envelope,
        tool_name=tool_name,
        paths=paths,
    )


async def runtime_sync_entity_file_to_knowledge(
    *,
    entity_id: str,
    abs_path: str,
    entity_root: str,
    source: str,
    created_by: str,
    force: bool | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    tool_name: str | None = None,
    expected_content_sha256: str | None = None,
    db: Any | None = None,
) -> Any:
    """Sync an entity filesystem file into Knowledge through the Runtime boundary."""

    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    sync_kwargs = dict(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source=source,
        created_by=created_by,
        force=force,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
        expected_content_sha256=expected_content_sha256,
    )
    if db is not None:
        sync_kwargs["db"] = db
    return await sync_file_to_knowledge(**sync_kwargs)


@asynccontextmanager
async def runtime_entity_filesystem_read_lock(entity_root: str):
    """Hold the shared entity read lock through authorization and readback."""

    from packages.core.services.entity_fs import entity_filesystem_read_lock

    async with entity_filesystem_read_lock(entity_root):
        yield


def runtime_open_entity_file_snapshot(
    entity_id: str,
    rel_path: str,
    *,
    expected_resolved_path: str | None = None,
    expected_content_sha256: str | None = None,
):
    """Open a pinned regular-file snapshot through the Runtime boundary."""

    from packages.core.services.entity_fs import open_entity_file_snapshot

    return open_entity_file_snapshot(
        entity_id,
        rel_path,
        expected_resolved_path=expected_resolved_path,
        expected_content_sha256=expected_content_sha256,
    )


def runtime_delete_entity_path(
    entity_id: str,
    rel_path: str,
    *,
    expected_resolved_path: str,
    expected_source_version: Any | None = None,
):
    """Delete a pinned, version-checked entity path."""

    from packages.core.services.entity_fs import delete_entity_path

    return delete_entity_path(
        entity_id,
        rel_path,
        expected_resolved_path=expected_resolved_path,
        expected_source_version=expected_source_version,
    )


async def runtime_trash_knowledge_path(
    entity_id: str,
    rel_path: str,
    *,
    is_directory: bool | None = None,
    db: Any | None = None,
) -> bool:
    """Mark a user-visible filesystem path as trashed in Knowledge."""

    from packages.core.services.knowledge_sync import trash_path

    trash_kwargs: dict[str, Any] = {}
    if is_directory is not None:
        trash_kwargs["is_directory"] = is_directory
    if db is not None:
        trash_kwargs["db"] = db
    return await trash_path(entity_id, rel_path, **trash_kwargs)


async def runtime_get_document_for_entity(
    db: Any,
    *,
    entity_id: str,
    document_id: str,
) -> Any:
    """Load a document record through the Runtime file boundary."""

    from packages.core.services.document_service import get_document

    return await get_document(db, document_id, entity_id)


def runtime_trigger_document_embeddings(document_id: str | None) -> None:
    """Trigger document embeddings best-effort after a Runtime file write."""

    if not document_id:
        return
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        process_document_embeddings.delay(document_id)
    except Exception:
        return
