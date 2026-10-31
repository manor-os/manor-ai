"""Large group file paths keep memory bounded and fail before publication."""
from __future__ import annotations

import asyncio
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from apps.api.routers import chat_rooms
from apps.api.middleware.room_file_ingress import RoomFileIngressLimitMiddleware
from apps.api.file_responses import PinnedFileStreamingResponse, _SnapshotBudget
from packages.core.services import upload_security


class BoundedSource(io.BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= 1024 * 1024, "unbounded file read"
        return super().read(size)


def test_room_file_digest_copies_bounded_chunks_and_rewinds(monkeypatch):
    payload = b"a" * (2 * 1024 * 1024 + 7)
    monkeypatch.setattr(chat_rooms, "_ROOM_FILE_MAX_BYTES", len(payload))
    source, destination = BoundedSource(payload), io.BytesIO()
    size, digest = chat_rooms._room_file_digest(source, destination)
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert source.tell() == destination.tell() == 0
    assert destination.getvalue() == payload
    monkeypatch.setattr(chat_rooms, "_ROOM_FILE_MAX_BYTES", len(payload) - 1)
    with pytest.raises(HTTPException) as rejected:
        chat_rooms._room_file_digest(source)
    assert rejected.value.status_code == 413
    assert source.tell() == 0


def test_room_file_digest_rejects_empty_source():
    with pytest.raises(HTTPException) as rejected:
        chat_rooms._room_file_digest(BoundedSource())
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_stream_upload_scan_receives_all_bytes_and_rewinds(monkeypatch):
    payload = b"safe text" * 1000
    source = BoundedSource(payload)
    seen = bytearray()

    def scan(stream):
        while chunk := stream.read(256):
            seen.extend(chunk)

    monkeypatch.setattr(upload_security, "_clamav_scan", scan)
    source.seek(12)
    assert await upload_security.inspect_upload_stream(source, filename="sample.txt") == "text/plain"
    assert bytes(seen) == payload
    assert source.tell() == 0


@pytest.mark.asyncio
async def test_stream_upload_scan_rejection_rewinds_and_propagates(monkeypatch):
    source = BoundedSource(b"safe-looking text")

    def reject(stream):
        stream.read(2)
        raise upload_security.UploadSecurityError("Rejected by test scanner")

    monkeypatch.setattr(upload_security, "_clamav_scan", reject)
    with pytest.raises(upload_security.UploadSecurityError, match="Rejected"):
        await upload_security.inspect_upload_stream(source, filename="sample.txt")
    assert source.tell() == 0


@pytest.mark.asyncio
async def test_stream_header_rejection_precedes_scanner(monkeypatch):
    def unexpected(_stream):
        raise AssertionError("invalid header reached scanner")

    monkeypatch.setattr(upload_security, "_clamav_scan", unexpected)
    source = BoundedSource(b"MZexecutable")
    with pytest.raises(upload_security.UploadSecurityError, match="Executable"):
        await upload_security.inspect_upload_stream(source, filename="sample.txt")
    assert source.tell() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_copy", [False, True])
async def test_upload_and_copy_share_admission_and_release_on_cancel(cancel_copy):
    both_started = asyncio.Event()
    finish = asyncio.Event()
    admitted = 0

    async def app(scope, receive, send):
        nonlocal admitted
        if scope["method"] == "POST" and scope["path"] in (upload_path, copy_path):
            admitted += 1
            if admitted == 2:
                both_started.set()
            await finish.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RoomFileIngressLimitMiddleware(app)
    upload_path = "/api/v1/chat/rooms/room/files"
    copy_path = upload_path + "/from-knowledge"

    async def invoke(path, method="POST"):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"small", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware({"type": "http", "method": method, "path": path, "headers": []}, receive, send)
        return messages

    first = asyncio.create_task(invoke(copy_path if cancel_copy else upload_path))
    second = asyncio.create_task(invoke(upload_path if cancel_copy else copy_path))
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        for path in (upload_path, copy_path):
            denied = await invoke(path)
            assert denied[0]["status"] == 503
            assert (b"retry-after", b"5") in denied[0]["headers"]
        assert (await invoke("/api/v1/other"))[0]["status"] == 204
        assert (await invoke(upload_path, method="GET"))[0]["status"] == 204
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        assert (await second)[0]["status"] == 204
        for path in (upload_path, copy_path):
            assert (await invoke(path))[0]["status"] == 204
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/from-knowledge"])
async def test_file_admission_releases_after_application_error(suffix):
    async def app(scope, receive, send):
        raise RuntimeError("copy failed")

    middleware = RoomFileIngressLimitMiddleware(app)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/rooms/room/files" + suffix, "headers": []}
    for _ in range(3):
        with pytest.raises(RuntimeError, match="copy failed"):
            await middleware(scope, AsyncMock(return_value={
                "type": "http.request", "body": b"small", "more_body": False,
            }), AsyncMock())


@pytest.mark.asyncio
async def test_knowledge_copy_keeps_original_json_request_stream():
    message = {"type": "http.request", "body": b'{"document_id":"test"}', "more_body": False}
    receive = AsyncMock(return_value=message)

    async def app(scope, forwarded_receive, send):
        assert forwarded_receive is receive
        assert receive.await_count == 0
        assert await forwarded_receive() is message

    middleware = RoomFileIngressLimitMiddleware(app)
    await middleware({
        "type": "http", "method": "POST", "path": "/api/v1/chat/rooms/room/files/from-knowledge",
        "headers": [(b"content-type", b"application/json")],
    }, receive, AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_private_download_budget_releases_on_prepare_error_or_response_close(monkeypatch, fails):
    budget = _SnapshotBudget(max_files=1, max_bytes=10)
    monkeypatch.setattr(chat_rooms, "room_file_snapshot_budget", budget)
    monkeypatch.setattr(chat_rooms, "_authorized_room_download", AsyncMock(
        return_value=(SimpleNamespace(size=3, chunk_count=1), None),
    ))

    async def build(*args, on_close):
        assert budget._bytes == 3
        if fails:
            raise HTTPException(409, "File changed")
        return PinnedFileStreamingResponse(io.BytesIO(b"abc"), on_close=on_close)

    monkeypatch.setattr(chat_rooms, "_build_room_file_download_response", build)
    if fails:
        with pytest.raises(HTTPException):
            await chat_rooms._room_file_download_response(None, "room", "file", None)
    else:
        response = await chat_rooms._room_file_download_response(None, "room", "file", None)
        assert budget._bytes == 3

        async def send(_message):
            raise RuntimeError("client disconnected")

        with pytest.raises(RuntimeError):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
        response.close()
        assert response._handle.closed
    assert budget._bytes == 0 and budget._files == 0
