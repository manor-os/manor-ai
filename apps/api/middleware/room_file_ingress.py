"""Bound group-file admission and multipart bodies before request parsing."""
from __future__ import annotations

import re
from tempfile import SpooledTemporaryFile

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


# A 1 GiB file plus bounded multipart headers, text, and mention metadata.
# This middleware intentionally runs outside the multipart parser. A request
# with an oversized body never reaches UploadFile's temporary-file spool.
ROOM_FILE_BODY_MAX_BYTES = 1024 * 1024 * 1024 + 128 * 1024
_MEMORY_SPOOL_MAX_BYTES = 256 * 1024
_REPLAY_CHUNK_BYTES = 64 * 1024
_MAX_ACTIVE_UPLOADS = 2
_ROOM_FILE_PATH = re.compile(r"/api/v1/chat/rooms/[^/]+/files/?\Z")
_ROOM_FILE_COPY_PATH = re.compile(r"/api/v1/chat/rooms/[^/]+/files/from-knowledge/?\Z")


class RoomFileIngressLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self._active_uploads = 0

    async def __call__(self, scope, receive, send):
        path = scope.get("path") or ""
        is_upload = bool(_ROOM_FILE_PATH.fullmatch(path))
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or not (is_upload or _ROOM_FILE_COPY_PATH.fullmatch(path))):
            await self.app(scope, receive, send)
            return

        if is_upload:
            for name, value in scope.get("headers") or ():
                if name.lower() == b"content-length":
                    try:
                        declared_length = int(value)
                    except ValueError:
                        declared_length = -1
                    if declared_length > ROOM_FILE_BODY_MAX_BYTES:
                        await self._reject(scope, receive, send)
                        return

        # Admission has no await between checking and reserving, so concurrent
        # uploads and Knowledge copies share this worker's temporary-disk bound.
        if self._active_uploads >= _MAX_ACTIVE_UPLOADS:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Group file uploads are busy; retry shortly"},
                headers={"Retry-After": "5"},
            )
            await response(scope, receive, send)
            return
        self._active_uploads += 1
        try:
            if is_upload:
                await self._receive_upload(scope, receive, send)
            else:
                # Knowledge copies use normal JSON parsing and its existing
                # limits. Only the file-producing request lifetime is bounded.
                await self.app(scope, receive, send)
        finally:
            self._active_uploads -= 1

    async def _receive_upload(self, scope, receive, send):
        # Consume at most the bounded body before invoking the app. A small
        # SpooledTemporaryFile threshold avoids 1 GiB of RAM per concurrent
        # request; the accepted body is replayed to Starlette in small chunks.
        # Pre-reading here avoids exceptions bubbling through BaseHTTPMiddleware
        # and protects no-Content-Length/chunked uploads before multipart parse.
        with SpooledTemporaryFile(max_size=_MEMORY_SPOOL_MAX_BYTES, mode="w+b") as body:
            received_bytes = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > ROOM_FILE_BODY_MAX_BYTES:
                    await self._reject(scope, receive, send)
                    return
                if chunk:
                    await run_in_threadpool(body.write, chunk)
                if not message.get("more_body", False):
                    break

            await run_in_threadpool(body.seek, 0)
            replay_done = False

            async def replay_receive():
                nonlocal replay_done
                if not replay_done:
                    chunk = await run_in_threadpool(body.read, _REPLAY_CHUNK_BYTES)
                    if chunk:
                        return {"type": "http.request", "body": chunk, "more_body": True}
                    replay_done = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                return await receive()

            await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            status_code=413,
            content={"detail": "Group file request must be 1 GiB or smaller"},
        )
        await response(scope, receive, send)
