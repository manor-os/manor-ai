"""File responses that keep authorized entity-file bytes stable."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager

import anyio
from fastapi import HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from packages.core.services.entity_fs import (
    EntityFilesystemBusyError,
    entity_filesystem_read_lock,
)


_MAX_SNAPSHOT_FILE_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_TOTAL_BYTES = 1024 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 2


class _SnapshotBudget:
    """Bound process-local temporary snapshot disk and copy concurrency."""

    def __init__(self, *, max_files: int, max_bytes: int) -> None:
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._files = 0
        self._bytes = 0
        self._lock = threading.Lock()

    def try_reserve(self, size: int) -> bool:
        if size < 0:
            return False
        with self._lock:
            if self._files >= self._max_files:
                return False
            if self._bytes + size > self._max_bytes:
                return False
            self._files += 1
            self._bytes += size
            return True

    def release(self, size: int) -> None:
        with self._lock:
            self._files = max(0, self._files - 1)
            self._bytes = max(0, self._bytes - size)


_snapshot_budget = _SnapshotBudget(
    max_files=_MAX_SNAPSHOT_FILES,
    max_bytes=_MAX_SNAPSHOT_TOTAL_BYTES,
)


def _copy_file_snapshot(path: str) -> str:
    """Copy one authorized inode to a private temporary response file."""

    suffix = os.path.splitext(path)[1]
    fd, snapshot_path = tempfile.mkstemp(prefix="manor-file-response-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as target, open(path, "rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        return snapshot_path
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(snapshot_path)
        except FileNotFoundError:
            pass
        raise


class EntitySnapshotFileResponse(FileResponse):
    """Snapshot under the read boundary, then stream without an entity lock."""

    def __init__(self, *args, read_boundary, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_boundary = read_boundary

    async def __call__(self, scope, receive, send) -> None:
        snapshot_path: str | None = None
        reserved_size: int | None = None
        rejection: PlainTextResponse | None = None
        try:
            try:
                source_stat = await anyio.to_thread.run_sync(
                    os.stat,
                    str(self.path),
                )
                file_size = source_stat.st_size
                if file_size > _MAX_SNAPSHOT_FILE_BYTES:
                    rejection = PlainTextResponse(
                        "File is too large to download safely",
                        status_code=413,
                    )
                elif not _snapshot_budget.try_reserve(file_size):
                    rejection = PlainTextResponse(
                        "File download capacity is busy; retry shortly",
                        status_code=503,
                        headers={"Retry-After": "1"},
                    )
                else:
                    reserved_size = file_size
                    snapshot_path = await anyio.to_thread.run_sync(
                        _copy_file_snapshot,
                        str(self.path),
                    )
                    self.path = snapshot_path
                    # Validators describe the authorized source version, not
                    # the newly created tempfile. Preserve conditional Range.
                    self.stat_result = source_stat
                    self.set_stat_headers(source_stat)
            finally:
                await self._read_boundary.__aexit__(None, None, None)
        except BaseException:
            if snapshot_path is not None:
                try:
                    os.unlink(snapshot_path)
                except FileNotFoundError:
                    pass
            if reserved_size is not None:
                _snapshot_budget.release(reserved_size)
            raise

        if rejection is not None:
            await rejection(scope, receive, send)
            return

        try:
            # A pathsend-capable server may defer opening the path until after
            # this coroutine returns. Force normal chunked reads so cleanup
            # cannot unlink the private snapshot before the server consumes it.
            response_scope = dict(scope)
            extensions = dict(response_scope.get("extensions") or {})
            extensions.pop("http.response.pathsend", None)
            response_scope["extensions"] = extensions
            await super().__call__(response_scope, receive, send)
        finally:
            if snapshot_path is not None:
                try:
                    os.unlink(snapshot_path)
                except FileNotFoundError:
                    pass
            if reserved_size is not None:
                _snapshot_budget.release(reserved_size)


@asynccontextmanager
async def entity_filesystem_read_boundary(root: str):
    """Hold one physical file identity stable through snapshot creation."""
    try:
        async with entity_filesystem_read_lock(root):
            yield
    except EntityFilesystemBusyError as exc:
        raise HTTPException(
            status_code=423,
            detail="Entity filesystem is busy with a mutation; retry shortly",
        ) from exc
