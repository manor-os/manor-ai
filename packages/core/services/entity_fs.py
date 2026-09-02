"""
Entity Filesystem — JuiceFS-backed POSIX filesystem per entity.

Each entity gets a real filesystem at /mnt/manor/{entity_id}/ with:
  - MANOR.md  — AI schema (how AI works in this filesystem)
  - index.md  — AI-maintained master catalog of all knowledge pages
  - log.md    — Chronological record of AI actions
  - .ai/      — Hidden AI workspace (agent memory, skills, temp)
  - Everything else: free-form user content
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, BinaryIO, Callable, Coroutine, Iterator, TypeVar

from packages.core.config import get_settings

logger = logging.getLogger(__name__)


class EntityFilesystemError(RuntimeError):
    """Raised when the entity filesystem cannot safely persist data."""


class EntityFileWriteError(EntityFilesystemError):
    """Raised when a file write cannot be verified after persistence."""


class EntityFilesystemBusyError(EntityFilesystemError):
    """Raised when another entity mutation does not release its lock in time."""


class EntityFilesystemStaleWriteError(EntityFilesystemError):
    """Raised when an older editor save arrives after a newer one."""


class EntityPathDeleteStagingError(EntityFilesystemError):
    """Raised when a renamed delete target still needs outer rollback."""

    def __init__(self, message: str, snapshot: EntityPathDeleteSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__(message)


class EditorWriteIntentStatus(str, Enum):
    """Result of comparing one editor save with the durable path fence."""

    CLAIMED = "claimed"
    REPLAYED = "replayed"
    STALE = "stale"


class EntityFileWriteLockMode(str, Enum):
    """Whether an atomic writer owns or inherits the canonical path lock."""

    ACQUIRE = "acquire"
    ALREADY_HELD = "already_held"


class EntityFileCommitState(str, Enum):
    """Atomic replacement phase used to choose the correct rollback."""

    STAGING = "staging"
    INSTALLED = "installed"
    VERIFIED = "verified"


class EntityFileRollbackState(str, Enum):
    """Lifecycle of a file snapshot retained for a larger projection commit."""

    STAGED = "staged"
    COMMITTED = "committed"
    RESTORED = "restored"


class EntityFileTargetKind(str, Enum):
    """Filesystem target kinds returned by canonical mutations."""

    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class EntityFileVersion:
    """Stable file identity captured after source fingerprint validation."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> EntityFileVersion:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )

    def matches(self, value: os.stat_result) -> bool:
        return (
            self.device == value.st_dev
            and self.inode == value.st_ino
            and self.size == value.st_size
            and self.mtime_ns == value.st_mtime_ns
            and self.ctime_ns == value.st_ctime_ns
        )

    def same_identity(self, value: os.stat_result) -> bool:
        """Return whether a directory entry still references the staged inode."""
        return self.device == value.st_dev and self.inode == value.st_ino


@dataclass
class EntityFileRollbackSnapshot:
    """Exact pre-write file state held until its Knowledge projection commits."""

    entity_id: str
    rel_path: str
    resolved_path: str
    existed: bool
    source_version: EntityFileVersion | None
    backup_name: str | None
    cleanup_parent_paths: tuple[str, ...]
    state: EntityFileRollbackState = EntityFileRollbackState.STAGED

    @property
    def backup_rel_path(self) -> str | None:
        """Entity-relative path retained for durable post-commit cleanup."""
        if self.backup_name is None:
            return None
        parent = os.path.dirname(self.rel_path)
        return os.path.join(parent, self.backup_name).replace("\\", "/")

    def _remove_empty_staged_parents(self) -> None:
        if self.existed:
            return
        entity_root = canonical_entity_root(self.entity_id)
        for parent_path in self.cleanup_parent_paths:
            try:
                delete_entity_path(
                    self.entity_id,
                    os.path.relpath(parent_path, entity_root),
                    expected_resolved_path=parent_path,
                )
            except EntityFilesystemError as exc:
                if "disappeared before delete" in str(exc):
                    continue
                if "Directory not empty" in str(exc):
                    break
                raise

    def commit(self) -> None:
        """Discard the retained hard-link after the outer commit succeeds."""
        if self.state is not EntityFileRollbackState.STAGED:
            return
        if self.backup_name is not None:
            with _open_entity_target_parent(
                self.entity_id,
                self.resolved_path,
                create=False,
            ) as (parent_fd, _target_name, parent_path):
                if not _directory_fd_matches_path(parent_fd, parent_path):
                    raise EntityFilesystemError(
                        "Entity file parent changed before rollback snapshot cleanup",
                    )
                try:
                    os.unlink(self.backup_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                os.fsync(parent_fd)
        self.backup_name = None
        self.state = EntityFileRollbackState.COMMITTED

    def discard_unwritten(self) -> None:
        """Discard staging state after the atomic writer installed nothing."""
        self.commit()
        self._remove_empty_staged_parents()

    def rollback(self, expected_source_version: EntityFileVersion | None) -> None:
        """Restore the prior inode, or remove a newly-created target."""
        if self.state is not EntityFileRollbackState.STAGED:
            return
        with _open_entity_target_parent(
            self.entity_id,
            self.resolved_path,
            create=False,
        ) as (parent_fd, target_name, parent_path):
            if not _directory_fd_matches_path(parent_fd, parent_path):
                raise EntityFilesystemError(
                    "Entity file parent changed before projection rollback",
                )
            target_exists = _path_exists_at(parent_fd, target_name)
            if target_exists:
                if expected_source_version is None:
                    raise EntityFilesystemError(
                        "Entity file rollback target cannot be verified",
                    )
                _validate_expected_source_version(
                    parent_fd,
                    target_name,
                    expected_source_version,
                )
                if self.existed:
                    if self.backup_name is None:
                        raise EntityFilesystemError(
                            "Entity file rollback snapshot is missing its retained backup",
                        )
                    os.replace(
                        self.backup_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    self.backup_name = None
                else:
                    os.unlink(target_name, dir_fd=parent_fd)
            elif self.existed:
                if self.backup_name is None:
                    raise EntityFilesystemError(
                        "Entity file rollback snapshot is missing its retained backup",
                    )
                os.replace(
                    self.backup_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                self.backup_name = None
            os.fsync(parent_fd)

        self._remove_empty_staged_parents()
        self.state = EntityFileRollbackState.RESTORED


@dataclass
class EntityPathDeleteSnapshot:
    """A file or empty directory renamed aside until projection commits."""

    entity_id: str
    rel_path: str
    resolved_path: str
    target_kind: EntityFileTargetKind
    staged_version: EntityFileVersion
    backup_name: str
    state: EntityFileRollbackState = EntityFileRollbackState.STAGED

    @property
    def backup_rel_path(self) -> str:
        """Entity-relative path retained for durable post-commit cleanup."""
        parent = os.path.dirname(self.rel_path)
        return os.path.join(parent, self.backup_name).replace("\\", "/")

    def _open_parent(self):
        return _open_entity_target_parent(
            self.entity_id,
            self.resolved_path,
            create=False,
        )

    def rollback(self) -> None:
        """Put the staged path back when its Knowledge retirement fails."""
        if self.state is not EntityFileRollbackState.STAGED:
            return
        with self._open_parent() as (parent_fd, target_name, parent_path):
            if not _directory_fd_matches_path(parent_fd, parent_path):
                raise EntityFilesystemError(
                    "Entity file parent changed before delete rollback",
                )
            if _path_exists_at(parent_fd, target_name):
                raise EntityFilesystemError(
                    "Entity file delete target was recreated before rollback",
                )
            backup_stat = os.stat(
                self.backup_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not self.staged_version.same_identity(backup_stat):
                raise EntityFilesystemError(
                    "Entity file staged delete changed before rollback",
                )
            if (
                self.target_kind is EntityFileTargetKind.FILE
                and not stat.S_ISREG(backup_stat.st_mode)
            ) or (
                self.target_kind is EntityFileTargetKind.DIRECTORY
                and not stat.S_ISDIR(backup_stat.st_mode)
            ):
                raise EntityFilesystemError(
                    "Entity file staged delete changed type before rollback",
                )
            os.replace(
                self.backup_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        self.state = EntityFileRollbackState.RESTORED

    def commit(self) -> None:
        """Permanently remove the staged path after its projection commits."""
        if self.state is not EntityFileRollbackState.STAGED:
            return
        with self._open_parent() as (parent_fd, _target_name, parent_path):
            if not _directory_fd_matches_path(parent_fd, parent_path):
                raise EntityFilesystemError(
                    "Entity file parent changed before staged delete cleanup",
                )
            try:
                backup_stat = os.stat(
                    self.backup_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                self.state = EntityFileRollbackState.COMMITTED
                return
            if not self.staged_version.matches(backup_stat):
                raise EntityFilesystemError(
                    "Entity file staged delete changed before cleanup",
                )
            if self.target_kind is EntityFileTargetKind.FILE:
                if not stat.S_ISREG(backup_stat.st_mode):
                    raise EntityFilesystemError(
                        "Entity file staged delete changed type before cleanup",
                    )
                os.unlink(self.backup_name, dir_fd=parent_fd)
            else:
                if not stat.S_ISDIR(backup_stat.st_mode):
                    raise EntityFilesystemError(
                        "Entity file staged delete changed type before cleanup",
                    )
                os.rmdir(self.backup_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        self.state = EntityFileRollbackState.COMMITTED


@dataclass(frozen=True)
class EntityFileSnapshot:
    """Pinned regular file used by Knowledge type detection."""

    abs_path: str
    descriptor_path: str
    stat: os.stat_result
    version: EntityFileVersion
    content_sha256: str


@dataclass(frozen=True)
class EditorWriteIntentResult:
    status: EditorWriteIntentStatus
    receipt: dict[str, Any] | None = None

# System files/dirs hidden from user view
SYSTEM_FILES = frozenset({"MANOR.md", "index.md", "log.md"})
SYSTEM_DIRS = frozenset({".ai"})
_POSIX_NAME_MAX_BYTES = 255
_ENTITY_MUTATION_LOCK_RETRY_SECONDS = 0.01
_ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS = 5.0
_MAX_EDITOR_WRITE_FENCE_SESSIONS = 64
_MutationResult = TypeVar("_MutationResult")


def _read_editor_write_sessions(handle: BinaryIO) -> dict[str, Any]:
    handle.seek(0)
    try:
        payload = json.loads(handle.read().decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    sessions = payload.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _write_editor_write_sessions(handle: BinaryIO, sessions: dict[str, Any]) -> None:
    if len(sessions) > _MAX_EDITOR_WRITE_FENCE_SESSIONS:
        sessions = dict(sorted(
            sessions.items(),
            key=lambda item: item[1].get("updated_at", 0)
            if isinstance(item[1], dict) else 0,
            reverse=True,
        )[:_MAX_EDITOR_WRITE_FENCE_SESSIONS])
    serialized = json.dumps(
        {"sessions": sessions},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    handle.seek(0)
    handle.truncate()
    handle.write(serialized)
    handle.flush()
    os.fsync(handle.fileno())


def claim_editor_write_intent(
    handle: BinaryIO,
    session_id: str,
    sequence: int,
    content: bytes | str,
) -> EditorWriteIntentResult:
    """Persist a bounded per-editor high-water mark while a path is locked."""
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    content_digest = hashlib.sha256(content_bytes).hexdigest()
    sessions = _read_editor_write_sessions(handle)
    previous = sessions.get(session_id)
    previous_sequence = previous.get("sequence", 0) if isinstance(previous, dict) else 0
    if isinstance(previous_sequence, int):
        if sequence < previous_sequence:
            return EditorWriteIntentResult(EditorWriteIntentStatus.STALE)
        if sequence == previous_sequence:
            if previous.get("content_digest") != content_digest:
                return EditorWriteIntentResult(EditorWriteIntentStatus.STALE)
            if previous.get("committed") is True:
                receipt = previous.get("receipt")
                return EditorWriteIntentResult(
                    EditorWriteIntentStatus.REPLAYED,
                    receipt=receipt if isinstance(receipt, dict) else None,
                )
            return EditorWriteIntentResult(EditorWriteIntentStatus.CLAIMED)

    sessions[session_id] = {
        "sequence": sequence,
        "content_digest": content_digest,
        "committed": False,
        "updated_at": time.time_ns(),
    }
    _write_editor_write_sessions(handle, sessions)
    return EditorWriteIntentResult(EditorWriteIntentStatus.CLAIMED)


def mark_editor_write_intent_committed(
    handle: BinaryIO,
    session_id: str,
    sequence: int,
    content: bytes | str,
    *,
    receipt: dict[str, Any] | None = None,
) -> None:
    """Mark a claimed intent terminal only after all durable effects succeed."""
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    content_digest = hashlib.sha256(content_bytes).hexdigest()
    mark_editor_write_intent_digest_committed(
        handle,
        session_id,
        sequence,
        content_digest,
        receipt=receipt,
    )


def mark_editor_write_intent_digest_committed(
    handle: BinaryIO,
    session_id: str,
    sequence: int,
    content_digest: str,
    *,
    receipt: dict[str, Any] | None = None,
) -> None:
    """Mark a claimed intent committed using its already-computed digest."""
    sessions = _read_editor_write_sessions(handle)
    current = sessions.get(session_id)
    if (
        not isinstance(current, dict)
        or current.get("sequence") != sequence
        or current.get("content_digest") != content_digest
    ):
        raise EntityFilesystemStaleWriteError(
            "Editor save intent changed before it could be committed",
        )
    current["committed"] = True
    current["updated_at"] = time.time_ns()
    if receipt is not None:
        current["receipt"] = receipt
    _write_editor_write_sessions(handle, sessions)


@contextmanager
def _entity_editor_write_intent_lock(
    entity_root: str,
    rel_path: str,
    *,
    timeout_seconds: float,
) -> Iterator[BinaryIO]:
    lock_dir = os.path.join(entity_root, ".ai", "write-locks")
    os.makedirs(lock_dir, exist_ok=True)
    normalized_path = rel_path.replace("\\", "/").lstrip("/")
    path_hash = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    handle = open(os.path.join(lock_dir, f"{path_hash}.lock"), "a+b")
    deadline = time.monotonic() + max(timeout_seconds, 0)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EntityFilesystemBusyError(
                        "File is busy with another mutation",
                    )
                time.sleep(_ENTITY_MUTATION_LOCK_RETRY_SECONDS)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def claim_entity_editor_write_intent(
    entity_root: str,
    rel_path: str,
    session_id: str,
    sequence: int,
    content: bytes | str,
    *,
    timeout_seconds: float = _ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
) -> EditorWriteIntentResult:
    """Claim an editor save sequence using the shared per-path lock file."""
    with _entity_editor_write_intent_lock(
        entity_root,
        rel_path,
        timeout_seconds=timeout_seconds,
    ) as handle:
        return claim_editor_write_intent(
            handle,
            session_id,
            sequence,
            content,
        )


def mark_entity_editor_write_intent_committed(
    entity_root: str,
    rel_path: str,
    session_id: str,
    sequence: int,
    content: bytes | str,
    *,
    receipt: dict[str, Any] | None = None,
    timeout_seconds: float = _ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
) -> None:
    with _entity_editor_write_intent_lock(
        entity_root,
        rel_path,
        timeout_seconds=timeout_seconds,
    ) as handle:
        mark_editor_write_intent_committed(
            handle,
            session_id,
            sequence,
            content,
            receipt=receipt,
        )


def mark_entity_editor_write_intent_digest_committed(
    entity_root: str,
    rel_path: str,
    session_id: str,
    sequence: int,
    content_digest: str,
    *,
    receipt: dict[str, Any] | None = None,
    timeout_seconds: float = _ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Commit a path receipt without loading the original content into memory."""
    with _entity_editor_write_intent_lock(
        entity_root,
        rel_path,
        timeout_seconds=timeout_seconds,
    ) as handle:
        mark_editor_write_intent_digest_committed(
            handle,
            session_id,
            sequence,
            content_digest,
            receipt=receipt,
        )


async def finish_entity_filesystem_mutation(
    operation: Coroutine[Any, Any, _MutationResult],
    *,
    release_result: Callable[[_MutationResult], None] | None = None,
) -> _MutationResult:
    """Drain a started mutation before propagating request cancellation."""
    task = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()
        except Exception:  # noqa: BLE001
            pass

    try:
        result = task.result()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        if cancellation is not None:
            logger.exception(
                "entity filesystem mutation failed while cancellation was pending",
            )
            raise cancellation
        raise
    if cancellation is not None:
        if release_result is not None:
            try:
                release_result(result)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "entity filesystem mutation result cleanup failed while "
                    "cancellation was pending",
                )
        raise cancellation
    return result


def _try_acquire_entity_lock_once(
    entity_root: str,
    lock_operation: int,
) -> BinaryIO | None:
    entity_root = os.path.abspath(entity_root)
    parent_path, entity_name = os.path.split(entity_root)
    parent_path = os.path.realpath(parent_path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fds: list[int] = []
    lock_fd: int | None = None

    def open_or_create_directory(parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, mode=0o777, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, directory_flags, dir_fd=parent_fd)
        except OSError as exc:
            try:
                mode = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                ).st_mode
            except FileNotFoundError:
                raise EntityFilesystemError(
                    f"Entity filesystem lock directory changed while opening: {name}",
                ) from exc
            if stat.S_ISLNK(mode):
                raise EntityFilesystemError(
                    f"Entity filesystem lock directory must not be a symlink: {name}",
                ) from exc
            if not stat.S_ISDIR(mode):
                raise EntityFilesystemError(
                    f"Entity filesystem lock path is not a directory: {name}",
                ) from exc
            raise EntityFilesystemError(
                f"Entity filesystem lock directory cannot be opened safely: {name}",
            ) from exc

    try:
        try:
            parent_fd = os.open(parent_path, directory_flags)
        except OSError as exc:
            raise EntityFilesystemError(
                f"Entity filesystem lock parent is unavailable: {parent_path}",
            ) from exc
        directory_fds.append(parent_fd)
        for directory_name in (entity_name, ".ai", "write-locks"):
            directory_fd = open_or_create_directory(
                directory_fds[-1],
                directory_name,
            )
            directory_fds.append(directory_fd)

        try:
            lock_fd = os.open(
                "entity-mutation.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fds[-1],
            )
        except OSError as exc:
            raise EntityFilesystemError(
                "Entity filesystem mutation lock cannot be opened safely",
            ) from exc
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise EntityFilesystemError(
                "Entity filesystem mutation lock is not a regular file",
            )
        handle = os.fdopen(lock_fd, "r+b")
        lock_fd = None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)

    try:
        fcntl.flock(handle.fileno(), lock_operation | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except Exception:
        handle.close()
        raise
    return handle


def _try_acquire_entity_lock(
    entity_root: str,
    lock_operation: int,
) -> BinaryIO | None:
    """Open and acquire the entity lock, retrying one path-creation race.

    Shared readers can initialize ``.ai/write-locks`` concurrently. On some
    filesystems an ``openat(O_CREAT)`` can briefly observe ENOENT while that
    directory chain is being reconciled. Re-open the chain once from the
    trusted entity parent; all symlink/type checks are repeated.
    """
    for attempt in range(2):
        try:
            return _try_acquire_entity_lock_once(entity_root, lock_operation)
        except EntityFilesystemError as exc:
            cause: BaseException | None = exc
            retryable = False
            while cause is not None:
                if isinstance(cause, FileNotFoundError):
                    retryable = True
                    break
                cause = cause.__cause__
            if attempt == 0 and retryable:
                continue
            raise
    return None


def _try_acquire_entity_mutation_lock(entity_root: str) -> BinaryIO | None:
    """Try once to exclusively lock entity filesystem mutations."""
    return _try_acquire_entity_lock(entity_root, fcntl.LOCK_EX)


def _try_acquire_entity_read_lock(entity_root: str) -> BinaryIO | None:
    """Try once to share-lock one consistent entity filesystem read."""
    return _try_acquire_entity_lock(entity_root, fcntl.LOCK_SH)


def _release_entity_mutation_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _release_abandoned_entity_mutation_lock(
    attempt: asyncio.Task[BinaryIO | None],
) -> None:
    """Release a lock acquired after its awaiting request was cancelled."""
    try:
        handle = attempt.result()
    except BaseException:
        return
    if handle is not None:
        _release_entity_mutation_lock(handle)


async def _try_acquire_entity_lock_cancellably(
    entity_root: str,
    acquire_lock: Callable[[str], BinaryIO | None],
) -> BinaryIO | None:
    attempt = asyncio.create_task(asyncio.to_thread(
        acquire_lock,
        entity_root,
    ))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        # The worker thread cannot be cancelled. It may still acquire the lock
        # after its caller exits, so arrange deterministic cleanup of its result.
        attempt.add_done_callback(_release_abandoned_entity_mutation_lock)
        raise


async def _try_acquire_entity_mutation_lock_cancellably(
    entity_root: str,
) -> BinaryIO | None:
    return await _try_acquire_entity_lock_cancellably(
        entity_root,
        _try_acquire_entity_mutation_lock,
    )


async def _try_acquire_entity_read_lock_cancellably(
    entity_root: str,
) -> BinaryIO | None:
    return await _try_acquire_entity_lock_cancellably(
        entity_root,
        _try_acquire_entity_read_lock,
    )


@asynccontextmanager
async def entity_filesystem_mutation_lock(
    entity_root: str,
    *,
    timeout_seconds: float = _ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """Serialize filesystem + Knowledge mutations with bounded, cancellable waiting."""
    handle: BinaryIO | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0)
    while handle is None:
        handle = await _try_acquire_entity_mutation_lock_cancellably(entity_root)
        if handle is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise EntityFilesystemBusyError(
                    "Entity filesystem is busy with another mutation",
                )
            await asyncio.sleep(min(_ENTITY_MUTATION_LOCK_RETRY_SECONDS, remaining))
    try:
        yield
    finally:
        _release_entity_mutation_lock(handle)


@asynccontextmanager
async def entity_filesystem_read_lock(
    entity_root: str,
    *,
    timeout_seconds: float = _ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """Keep one physical read and its Knowledge authorization snapshot aligned."""
    handle: BinaryIO | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0)
    while handle is None:
        handle = await _try_acquire_entity_read_lock_cancellably(entity_root)
        if handle is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise EntityFilesystemBusyError(
                    "Entity filesystem is busy with a mutation",
                )
            await asyncio.sleep(min(_ENTITY_MUTATION_LOCK_RETRY_SECONDS, remaining))
    try:
        yield
    finally:
        _release_entity_mutation_lock(handle)


def get_entity_root(entity_id: str) -> str:
    """Return the filesystem root for an entity."""
    safe_id = _sanitize_entity_id(entity_id)
    return os.path.join(get_settings().MANOR_FS_ROOT, safe_id)


def canonical_entity_root(entity_id: str) -> str:
    """Resolve the configured storage root without following the Entity entry."""
    storage_root = os.path.realpath(get_settings().MANOR_FS_ROOT)
    return os.path.join(storage_root, _sanitize_entity_id(entity_id))


def _sanitize_entity_id(entity_id: str) -> str:
    """Sanitize entity_id to prevent path traversal."""
    eid = str(entity_id).strip()
    eid = re.sub(r'[/\\]', '_', eid)
    eid = eid.replace('..', '_')
    if not eid or eid in ('.', '..'):
        raise ValueError(f"Invalid entity_id: {entity_id!r}")
    return eid


def _truncate_utf8(value: str, max_bytes: int) -> str:
    data = (value or "").encode("utf-8")
    if len(data) <= max_bytes:
        return value or ""
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _atomic_tmp_path(parent: str, full_path: str) -> str:
    suffix = f".tmp-{os.getpid()}-{time.monotonic_ns()}-{uuid.uuid4().hex}"
    max_prefix_bytes = max(16, _POSIX_NAME_MAX_BYTES - len(suffix.encode("utf-8")))
    prefix = _truncate_utf8(f".{os.path.basename(full_path)}", max_prefix_bytes).rstrip(" .")
    if prefix in {"", "."}:
        prefix = ".entity-file"
    return os.path.join(parent, f"{prefix}{suffix}")


def _directory_fd_matches_path(directory_fd: int, directory_path: str) -> bool:
    """Return whether a pathname still names the directory pinned by ``fd``."""
    try:
        fd_stat = os.fstat(directory_fd)
        path_stat = os.stat(directory_path, follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return False
    return (
        stat.S_ISDIR(path_stat.st_mode)
        and fd_stat.st_dev == path_stat.st_dev
        and fd_stat.st_ino == path_stat.st_ino
    )


@contextmanager
def _open_entity_target_parent(
    entity_id: str,
    full_path: str,
    *,
    create: bool,
    created_directories: list[str] | None = None,
) -> Iterator[tuple[int, str, str]]:
    """Pin a target parent without following mutable directory aliases.

    Walking from the canonical storage root with ``openat(O_NOFOLLOW)`` keeps
    the eventual rename/unlink bound to the authorized directory inode even if
    another process replaces a pathname component after authorization.
    """
    storage_root = os.path.realpath(assert_entity_filesystem_ready())
    entity_root = canonical_entity_root(entity_id)
    canonical_target = os.path.abspath(full_path)
    try:
        if os.path.commonpath([storage_root, entity_root]) != storage_root:
            raise EntityFilesystemError("Entity filesystem root escaped storage root")
        if os.path.commonpath([entity_root, canonical_target]) != entity_root:
            raise EntityFilesystemError("Entity file target escaped its entity root")
    except ValueError as exc:
        raise EntityFilesystemError("Entity file target could not be resolved") from exc

    target_rel = os.path.relpath(canonical_target, storage_root)
    parts = target_rel.split(os.sep)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise EntityFilesystemError("Entity file target has an invalid path component")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fds: list[int] = []
    try:
        try:
            directory_fds.append(os.open(storage_root, directory_flags))
        except OSError as exc:
            raise EntityFilesystemError(
                f"Entity filesystem storage root is unavailable: {storage_root}",
            ) from exc
        current_path = storage_root
        for directory_name in parts[:-1]:
            parent_fd = directory_fds[-1]
            directory_path = os.path.join(current_path, directory_name)
            if create:
                try:
                    os.mkdir(directory_name, mode=0o777, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                else:
                    if created_directories is not None:
                        created_directories.append(directory_path)
            try:
                directory_fd = os.open(
                    directory_name,
                    directory_flags,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise EntityFilesystemError(
                    "Entity file parent changed or is not a safe directory: "
                    f"{directory_name}",
                ) from exc
            directory_fds.append(directory_fd)
            current_path = directory_path

        parent_path = os.path.dirname(canonical_target)
        parent_fd = directory_fds[-1]
        if not _directory_fd_matches_path(parent_fd, parent_path):
            raise EntityFilesystemError(
                "Entity file parent changed while it was being opened",
            )
        yield parent_fd, parts[-1], parent_path
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _path_exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _target_stat_at(parent_fd: int, name: str) -> os.stat_result:
    try:
        target_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise EntityFilesystemStaleWriteError(
            "Entity file source disappeared before commit",
        ) from exc
    if not stat.S_ISREG(target_stat.st_mode):
        raise EntityFilesystemStaleWriteError(
            "Entity file source is no longer a regular file",
        )
    return target_stat


def _validate_expected_source_version(
    parent_fd: int,
    target_name: str,
    expected_source_version: EntityFileVersion | None,
) -> None:
    if expected_source_version is None:
        return
    target_stat = _target_stat_at(parent_fd, target_name)
    if not expected_source_version.matches(target_stat):
        raise EntityFilesystemStaleWriteError(
            "Entity file source changed before commit",
        )


def _sha256_descriptor(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    while chunk := os.read(file_descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


@contextmanager
def open_entity_file_snapshot(
    entity_id: str,
    rel_path: str,
    *,
    expected_resolved_path: str | None = None,
    expected_content_sha256: str | None = None,
) -> Iterator[EntityFileSnapshot]:
    """Open one regular file without following aliases and pin its parent."""
    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = os.path.abspath(os.path.join(canonical_entity_root(entity_id), safe_rel))
    if expected_resolved_path is not None and os.path.abspath(expected_resolved_path) != full_path:
        raise EntityFilesystemError("Entity file snapshot path changed before open")

    with _open_entity_target_parent(entity_id, full_path, create=False) as (
        parent_fd,
        target_name,
        parent_path,
    ):
        try:
            file_fd = os.open(
                target_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise EntityFilesystemError(
                "Entity file snapshot target changed or is not a safe file",
            ) from exc
        try:
            target_stat = os.fstat(file_fd)
            if not stat.S_ISREG(target_stat.st_mode):
                raise EntityFilesystemError("Entity file snapshot target is not a regular file")
            expected_digest = str(expected_content_sha256 or "").strip().lower()
            content_sha256 = _sha256_descriptor(file_fd) if expected_digest else ""
            if expected_digest and content_sha256 != expected_digest:
                raise EntityFilesystemStaleWriteError(
                    "Entity file content changed before Knowledge projection",
                )
            descriptor_root = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
            snapshot = EntityFileSnapshot(
                abs_path=full_path,
                descriptor_path=os.path.join(descriptor_root, str(file_fd)),
                stat=target_stat,
                version=EntityFileVersion.from_stat(target_stat),
                content_sha256=content_sha256,
            )
            yield snapshot
            if not _directory_fd_matches_path(parent_fd, parent_path):
                raise EntityFilesystemError(
                    "Entity file parent changed during Knowledge projection",
                )
            current_stat = _target_stat_at(parent_fd, target_name)
            if not snapshot.version.matches(current_stat):
                raise EntityFilesystemError(
                    "Entity file changed during Knowledge projection",
                )
        finally:
            os.close(file_fd)


def stage_entity_file_rollback_snapshot(
    entity_id: str,
    rel_path: str,
    *,
    expected_source_version: EntityFileVersion | None = None,
) -> EntityFileRollbackSnapshot:
    """Retain an exact hard-link of a target until a larger commit finishes."""
    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFilesystemError(f"Path traversal not allowed: {rel_path!r}")

    entity_root = canonical_entity_root(entity_id)
    created_directories: list[str] = []

    with _open_entity_target_parent(
        entity_id,
        full_path,
        create=True,
        created_directories=created_directories,
    ) as (parent_fd, target_name, parent_path):
        if not _directory_fd_matches_path(parent_fd, parent_path):
            raise EntityFilesystemError(
                "Entity file parent changed before rollback snapshot staging",
            )
        backup_name: str | None = None
        source_version: EntityFileVersion | None = None
        existed = _path_exists_at(parent_fd, target_name)
        if expected_source_version is not None and not existed:
            raise EntityFilesystemStaleWriteError(
                "Entity file source disappeared before commit",
            )
        if existed:
            target_stat = _target_stat_at(parent_fd, target_name)
            if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                raise EntityFilesystemError(
                    "Entity file rollback target is not a safe regular file",
                )
            _validate_expected_source_version(
                parent_fd,
                target_name,
                expected_source_version,
            )
            backup_name = os.path.basename(_atomic_tmp_path(parent_path, full_path))
            os.link(
                target_name,
                backup_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
            source_version = EntityFileVersion.from_stat(
                _target_stat_at(parent_fd, target_name),
            )

    return EntityFileRollbackSnapshot(
        entity_id=entity_id,
        rel_path=safe_rel,
        resolved_path=full_path,
        existed=existed,
        source_version=source_version,
        backup_name=backup_name,
        cleanup_parent_paths=tuple(
            path
            for path in reversed(created_directories)
            if os.path.abspath(path) != entity_root
        ),
    )


def stage_entity_path_delete_snapshot(
    entity_id: str,
    rel_path: str,
    *,
    expected_resolved_path: str,
    expected_source_version: EntityFileVersion | None = None,
) -> EntityPathDeleteSnapshot:
    """Rename a file or empty directory aside for an outer transaction."""
    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFilesystemError(f"Path traversal not allowed: {rel_path!r}")
    if os.path.abspath(full_path) != os.path.abspath(expected_resolved_path):
        raise EntityFilesystemError("Entity file target changed before delete")

    with _open_entity_target_parent(entity_id, full_path, create=False) as (
        parent_fd,
        target_name,
        parent_path,
    ):
        if not _directory_fd_matches_path(parent_fd, parent_path):
            raise EntityFilesystemError("Entity file parent changed before delete")
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise EntityFilesystemError(
                "Entity file target disappeared before delete",
            ) from exc
        if stat.S_ISLNK(target_stat.st_mode):
            raise EntityFilesystemError(
                "Entity file target became a filesystem alias",
            )
        if stat.S_ISREG(target_stat.st_mode):
            _validate_expected_source_version(
                parent_fd,
                target_name,
                expected_source_version,
            )
            target_kind = EntityFileTargetKind.FILE
        elif stat.S_ISDIR(target_stat.st_mode):
            if expected_source_version is not None:
                raise EntityFilesystemStaleWriteError(
                    "Expected file source became a directory before delete",
                )
            directory_fd = os.open(
                target_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                if os.listdir(directory_fd):
                    raise EntityFilesystemError(
                        "Directory not empty. Remove contents first.",
                    )
            finally:
                os.close(directory_fd)
            target_kind = EntityFileTargetKind.DIRECTORY
        else:
            raise EntityFilesystemError(
                "Entity file target has an unsupported type",
            )

        backup_name = os.path.basename(_atomic_tmp_path(parent_path, full_path))
        os.replace(
            target_name,
            backup_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        snapshot = EntityPathDeleteSnapshot(
            entity_id=entity_id,
            rel_path=safe_rel,
            resolved_path=full_path,
            target_kind=target_kind,
            staged_version=EntityFileVersion.from_stat(target_stat),
            backup_name=backup_name,
        )
        try:
            backup_stat = os.stat(
                backup_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            snapshot.staged_version = EntityFileVersion.from_stat(backup_stat)
            if target_kind is EntityFileTargetKind.DIRECTORY:
                directory_fd = os.open(
                    backup_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    if os.listdir(directory_fd):
                        raise EntityFilesystemError(
                            "Directory not empty. Remove contents first.",
                        )
                finally:
                    os.close(directory_fd)
            os.fsync(parent_fd)
        except Exception as exc:
            try:
                snapshot.rollback()
            except Exception as rollback_exc:
                raise EntityPathDeleteStagingError(str(exc), snapshot) from rollback_exc
            raise

    return snapshot


def _persist_entity_file_atomic(
    entity_id: str,
    safe_rel: str,
    full_path: str,
    *,
    expected: int,
    write_temporary: Callable[[BinaryIO], None],
    write_precondition: Callable[[str, bool, str], None] | None,
    expected_source_version: EntityFileVersion | None,
    lock_mode: EntityFileWriteLockMode,
) -> str:
    """Commit one file through a pinned parent directory descriptor."""
    lock_boundary = (
        nullcontext()
        if lock_mode is EntityFileWriteLockMode.ALREADY_HELD
        else _entity_editor_write_intent_lock(
            get_entity_root(entity_id),
            safe_rel,
            timeout_seconds=_ENTITY_MUTATION_LOCK_TIMEOUT_SECONDS,
        )
    )
    with lock_boundary:
        with _open_entity_target_parent(entity_id, full_path, create=True) as (
            parent_fd,
            target_name,
            parent_path,
        ):
            tmp_name = os.path.basename(_atomic_tmp_path(parent_path, full_path))
            backup_name: str | None = None
            commit_state = EntityFileCommitState.STAGING
            try:
                target_exists = _path_exists_at(parent_fd, target_name)
                _validate_expected_source_version(
                    parent_fd,
                    target_name,
                    expected_source_version,
                )
                if write_precondition is not None:
                    write_precondition(safe_rel, target_exists, full_path)
                if not _directory_fd_matches_path(parent_fd, parent_path):
                    raise EntityFileWriteError(
                        f"Entity file parent changed before commit: {safe_rel}",
                    )

                tmp_fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o666,
                    dir_fd=parent_fd,
                )
                with os.fdopen(tmp_fd, "wb") as temporary:
                    write_temporary(temporary)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                tmp_stat = os.stat(
                    tmp_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(tmp_stat.st_mode) or tmp_stat.st_size != expected:
                    raise EntityFileWriteError(
                        f"Temporary file size mismatch for {safe_rel}: "
                        f"expected {expected}, got {tmp_stat.st_size}",
                    )
                if not _directory_fd_matches_path(parent_fd, parent_path):
                    raise EntityFileWriteError(
                        f"Entity file parent changed before commit: {safe_rel}",
                    )
                _validate_expected_source_version(
                    parent_fd,
                    target_name,
                    expected_source_version,
                )
                target_exists = _path_exists_at(parent_fd, target_name)
                if write_precondition is not None:
                    write_precondition(safe_rel, target_exists, full_path)
                if target_exists:
                    existing_stat = _target_stat_at(parent_fd, target_name)
                    if stat.S_ISREG(existing_stat.st_mode):
                        backup_name = os.path.basename(
                            _atomic_tmp_path(parent_path, full_path),
                        )
                        os.link(
                            target_name,
                            backup_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                os.replace(
                    tmp_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                commit_state = EntityFileCommitState.INSTALLED
                target_stat = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(target_stat.st_mode):
                    raise EntityFileWriteError(
                        f"Persisted file missing after write: {safe_rel}",
                    )
                if target_stat.st_size != expected:
                    raise EntityFileWriteError(
                        f"Persisted file size mismatch for {safe_rel}: "
                        f"expected {expected}, got {target_stat.st_size}",
                    )
                if not _directory_fd_matches_path(parent_fd, parent_path):
                    raise EntityFileWriteError(
                        f"Entity file parent changed during commit: {safe_rel}",
                    )
                commit_state = EntityFileCommitState.VERIFIED
                if backup_name is not None:
                    try:
                        os.unlink(backup_name, dir_fd=parent_fd)
                    except OSError:
                        logger.warning(
                            "Failed to remove committed entity file backup %s",
                            backup_name,
                            exc_info=True,
                        )
                    else:
                        backup_name = None
                return full_path
            except Exception:
                try:
                    os.unlink(tmp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug(
                        "Failed to remove temporary entity file %s",
                        tmp_name,
                        exc_info=True,
                    )
                if (
                    commit_state is EntityFileCommitState.INSTALLED
                    and backup_name is not None
                ):
                    try:
                        os.replace(
                            backup_name,
                            target_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        backup_name = None
                    except OSError as rollback_error:
                        raise EntityFileWriteError(
                            f"Entity file rollback failed for {safe_rel}; "
                            f"the prior content remains in {backup_name}",
                        ) from rollback_error
                elif commit_state is EntityFileCommitState.INSTALLED:
                    try:
                        os.unlink(target_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.debug(
                            "Failed to remove unverified new entity file %s",
                            target_name,
                            exc_info=True,
                        )
                elif backup_name is not None:
                    try:
                        os.unlink(backup_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.debug(
                            "Failed to remove unused entity file backup %s",
                            backup_name,
                            exc_info=True,
                        )
                raise


def is_fs_enabled() -> bool:
    """Check if JuiceFS entity filesystem is enabled."""
    return get_settings().MANOR_FS_ENABLED


def assert_entity_filesystem_ready() -> str:
    """Return MANOR_FS_ROOT after validating it is safe for persistent writes.

    In cloud deployments the root must be an actual mount point. This prevents
    workers from silently writing generated media into an ephemeral container
    directory when JuiceFS failed to mount.
    """
    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        raise EntityFilesystemError("Entity filesystem is disabled (MANOR_FS_ENABLED=false)")

    fs_root = os.path.abspath(settings.MANOR_FS_ROOT)
    return fs_root


def write_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    data: bytes,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
    write_precondition: Callable[[str, bool, str], None] | None = None,
    expected_source_version: EntityFileVersion | None = None,
    lock_mode: EntityFileWriteLockMode = EntityFileWriteLockMode.ACQUIRE,
) -> str:
    """Atomically write bytes into an entity filesystem and verify the result."""
    assert_entity_filesystem_ready()
    if not allow_empty and not data:
        raise EntityFileWriteError("Refusing to persist an empty generated file")

    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFileWriteError(f"Path traversal not allowed: {rel_path!r}")

    expected = len(data) if expected_size is None else int(expected_size)
    return _persist_entity_file_atomic(
        entity_id,
        safe_rel,
        full_path,
        expected=expected,
        write_temporary=lambda temporary: temporary.write(data),
        write_precondition=write_precondition,
        expected_source_version=expected_source_version,
        lock_mode=lock_mode,
    )


def copy_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    source_path: str,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
    write_precondition: Callable[[str, bool, str], None] | None = None,
    expected_source_version: EntityFileVersion | None = None,
    lock_mode: EntityFileWriteLockMode = EntityFileWriteLockMode.ACQUIRE,
) -> str:
    """Atomically copy an existing local file into an entity filesystem."""
    assert_entity_filesystem_ready()
    try:
        source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise EntityFileWriteError(f"Source file does not exist: {source_path}") from exc
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise EntityFileWriteError(f"Source is not a regular file: {source_path}")
        source_size = source_stat.st_size
        if not allow_empty and source_size <= 0:
            raise EntityFileWriteError("Refusing to persist an empty generated file")
        expected = source_size if expected_size is None else int(expected_size)
        if source_size != expected:
            raise EntityFileWriteError(
                f"Source file size mismatch for {source_path}: expected {expected}, got {source_size}"
            )

        safe_rel = rel_path.replace("\\", "/").lstrip("/")
        full_path = resolve_path(entity_id, safe_rel)
        if full_path is None:
            raise EntityFileWriteError(f"Path traversal not allowed: {rel_path!r}")

        def copy_source(temporary: BinaryIO) -> None:
            os.lseek(source_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(source_fd), "rb") as source:
                shutil.copyfileobj(source, temporary)

        return _persist_entity_file_atomic(
            entity_id,
            safe_rel,
            full_path,
            expected=expected,
            write_temporary=copy_source,
            write_precondition=write_precondition,
            expected_source_version=expected_source_version,
            lock_mode=lock_mode,
        )
    finally:
        os.close(source_fd)


def delete_entity_path(
    entity_id: str,
    rel_path: str,
    *,
    expected_resolved_path: str,
    expected_source_version: EntityFileVersion | None = None,
) -> EntityFileTargetKind:
    """Delete one authorized file or empty directory through a pinned parent."""
    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    full_path = resolve_path(entity_id, safe_rel)
    if full_path is None:
        raise EntityFilesystemError(f"Path traversal not allowed: {rel_path!r}")
    if os.path.abspath(full_path) != os.path.abspath(expected_resolved_path):
        raise EntityFilesystemError("Entity file target changed before delete")

    with _open_entity_target_parent(entity_id, full_path, create=False) as (
        parent_fd,
        target_name,
        parent_path,
    ):
        if not _directory_fd_matches_path(parent_fd, parent_path):
            raise EntityFilesystemError("Entity file parent changed before delete")
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise EntityFilesystemError("Entity file target disappeared before delete") from exc
        if stat.S_ISLNK(target_stat.st_mode):
            raise EntityFilesystemError("Entity file target became a filesystem alias")
        if stat.S_ISREG(target_stat.st_mode):
            _validate_expected_source_version(
                parent_fd,
                target_name,
                expected_source_version,
            )
            os.unlink(target_name, dir_fd=parent_fd)
            return EntityFileTargetKind.FILE
        if stat.S_ISDIR(target_stat.st_mode):
            if expected_source_version is not None:
                raise EntityFilesystemStaleWriteError(
                    "Expected file source became a directory before delete",
                )
            try:
                os.rmdir(target_name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise EntityFilesystemError(
                        "Directory not empty. Remove contents first.",
                    ) from exc
                raise
            return EntityFileTargetKind.DIRECTORY
        raise EntityFilesystemError("Entity file target has an unsupported type")


def unlink_entity_file_entry(
    entity_id: str,
    rel_path: str,
    *,
    allow_symlink: bool = False,
) -> bool:
    """Unlink one exact file entry through a pinned, non-symlink parent.

    Unlike :func:`resolve_path`, this intentionally does not follow the final
    directory entry. Cleanup jobs can therefore remove a final symlink itself
    without ever touching its destination. Parent-directory aliases still fail
    closed through ``_open_entity_target_parent``.
    """
    safe_rel = rel_path.replace("\\", "/").lstrip("/")
    if not safe_rel or any(part in {"", ".", ".."} for part in safe_rel.split("/")):
        raise EntityFilesystemError(f"Invalid entity file path: {rel_path!r}")
    entity_root = canonical_entity_root(entity_id)
    full_path = os.path.abspath(os.path.join(entity_root, safe_rel))
    try:
        if os.path.commonpath([entity_root, full_path]) != entity_root:
            raise EntityFilesystemError(f"Path traversal not allowed: {rel_path!r}")
    except ValueError as exc:
        raise EntityFilesystemError(f"Invalid entity file path: {rel_path!r}") from exc

    with _open_entity_target_parent(entity_id, full_path, create=False) as (
        parent_fd,
        target_name,
        parent_path,
    ):
        if not _directory_fd_matches_path(parent_fd, parent_path):
            raise EntityFilesystemError("Entity file parent changed before unlink")
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(target_stat.st_mode):
            if not allow_symlink:
                raise EntityFilesystemError("Entity file target became a filesystem alias")
        elif not stat.S_ISREG(target_stat.st_mode):
            raise EntityFilesystemError("Entity file target is not a regular file")
        os.unlink(target_name, dir_fd=parent_fd)
        return True


def entity_fs_exists(entity_id: str) -> bool:
    """Check if an entity's filesystem has been provisioned."""
    root = get_entity_root(entity_id)
    return os.path.isdir(root) and os.path.isfile(os.path.join(root, "MANOR.md"))


def is_system_path(name: str) -> bool:
    """Check if a file/dir name is a hidden system file."""
    return name in SYSTEM_FILES or name in SYSTEM_DIRS or name.startswith(".")


def provision_entity_filesystem(entity_id: str, entity_name: str = "") -> str:
    """
    Create the entity's filesystem structure.
    Idempotent — safe to call multiple times.
    Returns the entity root path.
    """
    root = get_entity_root(entity_id)
    display_name = entity_name or f"Entity {entity_id}"

    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "agents"), exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "skills"), exist_ok=True)
    os.makedirs(os.path.join(root, ".ai", "temp"), exist_ok=True)

    manor_md_path = os.path.join(root, "MANOR.md")
    if not os.path.exists(manor_md_path):
        with open(manor_md_path, "w") as f:
            f.write(_build_manor_md(display_name))
        logger.info("Created MANOR.md for entity %s", entity_id)

    index_path = os.path.join(root, "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w") as f:
            f.write(
                f"# {display_name} Knowledge Index\n\n"
                f"_Auto-maintained by AI. Upload files or ask AI to create content._\n"
            )

    log_path = os.path.join(root, "log.md")
    if not os.path.exists(log_path):
        with open(log_path, "w") as f:
            f.write(
                f"# Activity Log\n\n"
                f"[PROVISION] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} "
                f"Entity filesystem created for {display_name}\n"
            )

    logger.info("Entity filesystem provisioned: %s → %s", entity_id, root)
    return root


def _sanitize_agent_id(agent_id: str) -> str:
    """Sanitize agent_id to prevent path traversal."""
    return re.sub(r'[/\\]', '_', str(agent_id).strip())


def get_agent_dir(entity_id: str, agent_id: str) -> str:
    """Return the filesystem path for an agent's definition directory."""
    root = get_entity_root(entity_id)
    safe_agent = _sanitize_agent_id(agent_id)
    return os.path.join(root, ".ai", "agents", safe_agent)


def provision_agent_workspace(entity_id: str, agent_id: str) -> str:
    """Create an agent's workspace within the entity filesystem.

    For the master agent (_master), seeds comprehensive platform-level
    defaults. Custom agents get minimal templates to be filled in.
    """
    agent_dir = get_agent_dir(entity_id, agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "memory"), exist_ok=True)

    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    if agent_id == MANOR_AGENT_FS_ID:
        defaults = _MASTER_AGENT_DEFAULTS
    else:
        defaults = _custom_agent_defaults(agent_id)

    for fname, content in defaults:
        fpath = os.path.join(agent_dir, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                f.write(content)

    return agent_dir


# ── Platform defaults for the master agent ──────────────────────────────────

_MASTER_SOUL = """\
# Manor AI

You are **Manor AI**, the primary intelligent assistant for this organization.

## Personality
- Professional yet approachable — adapt tone to the user's style
- Proactive: anticipate follow-up needs and surface relevant info
- Concise by default, detailed when the topic warrants it
- Confident but honest — say "I don't know" rather than fabricate

## Voice
- First person ("I can help with that")
- Active voice, short sentences
- No filler phrases ("certainly!", "great question!")
- Use the user's language (detect from their messages)

## Things to Avoid
- Never fabricate data, citations, or numbers
- Never expose internal system details, API keys, or entity IDs
- Never make commitments on behalf of the organization
- Never share one user's data with another user
"""

_MASTER_AGENT = """\
# Manor AI — Capabilities

## Role
You are the central AI assistant for this organization. You help team members
with day-to-day work across all business functions.

## Core Capabilities
- **Task management** — create, assign, update, and track tasks
- **Knowledge base** — search, summarize, and create documents
- **Data analysis** — query data, generate reports, spot trends
- **Communication** — draft messages, summarize threads, prepare briefs
- **Agent coordination** — delegate specialized work to other agents
- **Workflow automation** — trigger and monitor multi-step workflows

## How to Respond
1. Understand the user's intent before acting
2. Use available tools when they add value — don't narrate tool calls
3. When a task is better suited for a specialized agent, delegate it
4. Present results clearly: tables for data, bullet points for lists
5. After completing a task, briefly confirm what was done

## Context Awareness
- You know the current user, their role, and their organization
- You have access to the organization's knowledge base and tools
- You can see conversation history for continuity
- Use memories to personalize interactions over time
"""

_MASTER_TOOLS = """\
# Tool Guidelines

## General
- Use tools when they provide more accurate or current information
- Execute tools without narrating — just do it and present results
- When multiple tools could help, pick the most direct path
- If a tool errors, explain clearly and suggest alternatives

## Knowledge Search
- Search the knowledge base before giving general answers on org-specific topics
- Cite document names when referencing knowledge base content

## Web Search & Fetch
- Use for current events, external data, or questions beyond the knowledge base
- Summarize web content — don't dump raw HTML

## Task & Document Tools
- Confirm destructive actions (delete, overwrite) before executing
- When creating documents, match the organization's existing format/style
"""

_MASTER_RULES = """\
# Operating Rules

## Privacy & Security
- Never share data between users unless they have shared access
- Never output raw API keys, tokens, or credentials
- Respect document permissions and workspace boundaries

## Accuracy
- Prefer tool results over your own knowledge for factual claims
- When uncertain, say so — offer to search or verify
- Distinguish between facts (from tools/documents) and your reasoning

## Boundaries
- Do not execute financial transactions without explicit confirmation
- Do not send external communications (email, Slack) without user approval
- Escalate to a human when the request is outside your capabilities
"""

_MASTER_GOALS = """\
# Current Goals

No specific goals set. The master agent serves all general requests.
"""

_MASTER_AGENT_DEFAULTS = [
    ("SOUL.md", _MASTER_SOUL),
    ("AGENT.md", _MASTER_AGENT),
    ("TOOLS.md", _MASTER_TOOLS),
    ("RULES.md", _MASTER_RULES),
    ("GOALS.md", _MASTER_GOALS),
]


def _custom_agent_defaults(agent_id: str) -> list[tuple[str, str]]:
    """Minimal templates for custom agents."""
    return [
        ("SOUL.md", f"# {agent_id}\n\nDescribe this agent's personality and voice.\n"),
        ("AGENT.md", f"# {agent_id}\n\n## Domain & Expertise\n\nDescribe this agent's expertise.\n\n## Instructions\n\nAdd specific instructions.\n"),
        ("TOOLS.md", "# Tool Guidelines\n\nList preferred tools and any restrictions.\n"),
        ("RULES.md", f"# Operating Rules\n\nDefine rules and guardrails for {agent_id}.\n"),
        ("GOALS.md", "# Current Goals\n\nNo goals set yet.\n"),
        ("STYLE.md", "# Style\n\nDescribe this agent's preferred communication and output style.\n"),
        ("SKILLS.md", "# Skills\n\nList reusable skills this agent has learned.\n"),
        ("LEARNINGS.md", "# Learnings\n\nCapture durable execution lessons and failure patterns.\n"),
        ("MEMORY.md", "# Memory\n\nCapture stable facts and preferences for this agent.\n"),
    ]


def append_log(entity_id: str, entry_type: str, message: str) -> None:
    """Append an entry to the entity's log.md."""
    root = get_entity_root(entity_id)
    log_path = os.path.join(root, "log.md")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{entry_type.upper()}] {timestamp} {message}\n"
    try:
        with open(log_path, "a") as f:
            f.write(line)
    except Exception as e:
        logger.warning("Failed to append to log.md for entity %s: %s", entity_id, e)


def resolve_path(entity_id: str, relative_path: str) -> str | None:
    """
    Resolve a relative path within an entity's filesystem.
    Returns absolute path, or None if it escapes the entity root.
    """
    root = canonical_entity_root(entity_id)
    full = os.path.realpath(os.path.join(root, relative_path))
    try:
        within_root = os.path.commonpath([root, full]) == root
    except ValueError:
        within_root = False
    if not within_root:
        logger.warning("Path traversal attempt: entity=%s path=%s", entity_id, relative_path)
        return None
    return full


# ── MANOR.md template ────────────────────────────────────────────────────────

def _build_manor_md(entity_name: str) -> str:
    return f"""# {entity_name} Knowledge Base

This filesystem is the entity's raw internal workspace. Both humans and AI
agents may read and write here using standard tools, but not every path is
user-facing Knowledge.

## Layout

- `MANOR.md` — This file. Schema for how AI works in this filesystem.
- `index.md` — AI-maintained master catalog of all knowledge pages.
- `log.md` — Chronological record of AI actions (append-only).
- `.ai/` — AI internal workspace (hidden). Agent memory, skills, temp files.
- Everything else: user-organized content. No fixed structure.

## Knowledge vs Filesystem Tools

- `list_documents` / `search_documents` are the user-facing Knowledge tools.
  Use them when the user asks what files/documents are available or when
  resolving user-visible references.
- `list_files` / `glob_files` / `grep_files` inspect the raw entity filesystem,
  including internal/system paths. Use them for debugging or low-level file
  operations only. Do not present their raw output as the user's visible file
  list.

## AI Rules

### Reading
- Read any file. No restrictions within this entity.
- Start with `index.md` to understand what knowledge exists.
- Use `rg` (ripgrep) for full-text search across all files.
- Use `find` to discover file structure.

### Writing Markdown (knowledge pages)
- Create .md files alongside the content they describe.
- Every .md file starts with YAML frontmatter:
  ```yaml
  ---
  summary: One-line description of this page
  tags: [relevant, tags, here]
  sources: [relative/path/to/source.pdf]
  updated: YYYY-MM-DD
  author: ai | username
  ---
  ```
- Use [[relative/path/to/Page Name]] for cross-references (Obsidian-compatible).
- Update `index.md` when creating or significantly updating pages.
- Append to `log.md` for every action.

### Writing Other Files (documents, images, presentations)
- Place generated files where users would expect them.
- Match the user's existing naming conventions.
- Record in `log.md`.

### Modifying User Files
- NEVER modify user-uploaded files (PDFs, images) without explicit permission.
- AI-created .md files (author: ai in frontmatter) can be freely updated.
- If unsure, check `author` in frontmatter.

### Organizing
- Respect the user's folder structure. Don't reorganize without permission.
- When users create new content areas, update `index.md` accordingly.

## Operations

### Ingest (new file added)
1. Read the new file.
2. Create or update relevant .md pages nearby.
3. Add [[links]] to related existing pages.
4. Update `index.md`.
5. Append `log.md`: `[INGEST] date path → pages updated`

### Query (user asks a question)
1. Check `index.md` for relevant pages.
2. `rg "keyword"` for full-text matches.
3. Read relevant .md pages (compiled knowledge).
4. If answer is new knowledge worth keeping, create a .md page.
5. Append `log.md`: `[QUERY] date question → source`

### Compile (periodic maintenance)
1. Scan for files with no nearby .md knowledge page.
2. Check for stale pages (source newer than page).
3. Update cross-references.
4. Append `log.md`: `[COMPILE] date pages updated`

### Lint (health check)
1. Find orphaned pages (no incoming [[links]]).
2. Find broken [[links]] (target doesn't exist).
3. Find unprocessed files (no knowledge page).
4. Append `log.md`: `[LINT] date issues found`
"""
