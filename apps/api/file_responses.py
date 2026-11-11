"""File responses that keep authorized entity-file bytes stable."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager

import anyio
from fastapi import HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable

from packages.core.services.entity_fs import (
    EntityFilesystemBusyError,
    entity_filesystem_read_lock,
)


_MAX_SNAPSHOT_FILE_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_TOTAL_BYTES = 1024 * 1024 * 1024
# Pages load several files at once (result previews, thumbnails, viewers).
# Temp disk stays bounded by the byte budget above.
_MAX_SNAPSHOT_FILES = 8
# Wait this long for a free slot before answering 503. The entity read lock is
# held while waiting, so keep it short.
_SNAPSHOT_WAIT_SECONDS = 5.0
_SNAPSHOT_WAIT_POLL_SECONDS = 0.05


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

    async def reserve(self, size: int, *, timeout: float) -> bool:
        """Reserve, briefly waiting for a slot rather than failing at once."""
        if size < 0 or size > self._max_bytes or self._max_files <= 0:
            return False
        deadline = anyio.current_time() + max(0.0, timeout)
        while not self.try_reserve(size):
            if anyio.current_time() >= deadline:
                return False
            await anyio.sleep(_SNAPSHOT_WAIT_POLL_SECONDS)
        return True

    def release(self, size: int) -> None:
        with self._lock:
            self._files = max(0, self._files - 1)
            self._bytes = max(0, self._bytes - size)


_snapshot_budget = _SnapshotBudget(
    max_files=_MAX_SNAPSHOT_FILES,
    max_bytes=_MAX_SNAPSHOT_TOTAL_BYTES,
)
room_file_snapshot_budget = _SnapshotBudget(max_files=8, max_bytes=12 * 1024 * 1024 * 1024)


class PinnedFileStreamingResponse(StreamingResponse):
    """Own already-verified private bytes, including pre-body disconnects."""

    def __init__(self, handle, *, on_close=None, **kwargs):
        self._handle = handle
        self._on_close = on_close
        self._closed = False
        super().__init__(self._chunks(), **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        finally:
            if self._on_close is not None:
                self._on_close()

    def _chunks(self):
        while chunk := self._handle.read(1024 * 1024):
            yield chunk

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()


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


def _read_file_slice(path: str, start: int, length: int) -> bytes:
    with open(path, "rb") as source:
        source.seek(start)
        return source.read(length)


class EntitySnapshotFileResponse(FileResponse):
    """Snapshot under the read boundary, then stream without an entity lock."""

    def __init__(self, *args, read_boundary, range_slice_bytes: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_boundary = read_boundary
        # A player streaming a recording asks for byte ranges; when set, one range is
        # answered with at most this many bytes read under the boundary, instead of a
        # snapshot of the whole file first. The player asks again for the rest.
        self.range_slice_bytes = range_slice_bytes

    def _requested_slice(self, scope, file_size: int) -> tuple[int, int] | None:
        headers = Headers(scope=scope)
        http_range, http_if_range = headers.get("range"), headers.get("if-range")
        if http_range is None or (http_if_range is not None and not self._should_use_range(http_if_range)):
            return None
        try:
            ranges = self._parse_range_header(http_range, file_size)
        except (MalformedRangeHeader, RangeNotSatisfiable):
            return None
        if len(ranges) != 1:
            return None
        start, end = ranges[0]
        return start, min(end, start + int(self.range_slice_bytes or 0))

    async def _slice_response(self, scope) -> Response | None:
        """One bounded range read under the boundary, or None to take the snapshot path."""
        if not self.range_slice_bytes or scope.get("type") != "http":
            return None
        source_stat = await anyio.to_thread.run_sync(os.stat, str(self.path))
        self.set_stat_headers(source_stat)
        requested = self._requested_slice(scope, source_stat.st_size)
        if requested is None:
            return None
        start, end = requested
        data = await anyio.to_thread.run_sync(_read_file_slice, str(self.path), start, end - start)
        headers = {key: value for key, value in self.headers.items() if key.lower() != "content-length"}
        headers["content-range"] = f"bytes {start}-{start + len(data) - 1}/{source_stat.st_size}"
        return Response(content=b"" if scope.get("method", "").upper() == "HEAD" else data, status_code=206,
                        headers=headers, media_type=self.media_type)

    async def __call__(self, scope, receive, send) -> None:
        try:
            sliced = await self._slice_response(scope)
        except BaseException:
            await self._read_boundary.__aexit__(None, None, None)
            raise
        if sliced is not None:
            await self._read_boundary.__aexit__(None, None, None)
            await sliced(scope, receive, send)
            return
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
                elif not await _snapshot_budget.reserve(
                    file_size,
                    timeout=_SNAPSHOT_WAIT_SECONDS,
                ):
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
