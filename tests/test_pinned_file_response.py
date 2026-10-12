"""A download owns its pinned bytes, never a live DB session."""
from __future__ import annotations

import asyncio
import io

import pytest

from apps.api.file_responses import PinnedFileStreamingResponse


def _scope():
    return {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}


async def _receive():
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["http.response.start", "http.response.body"])
async def test_disconnected_client_releases_pinned_file(failure_point):
    handle = io.BytesIO(b"verified file")
    response = PinnedFileStreamingResponse(handle)

    async def send(frame):
        if frame["type"] == failure_point:
            raise OSError("client disconnected")

    with pytest.raises(Exception):
        await response(_scope(), _receive, send)
    assert handle.closed


@pytest.mark.asyncio
async def test_cancelled_slow_reader_releases_pinned_file():
    handle = io.BytesIO(b"verified file")
    started = asyncio.Event()

    async def send(frame):
        if frame["type"] == "http.response.body":
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(PinnedFileStreamingResponse(handle)(_scope(), _receive, send))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.closed


@pytest.mark.asyncio
async def test_completed_download_preserves_exact_bytes_and_closes_file():
    handle = io.BytesIO(b"verified file" * 100_000)
    expected = handle.getvalue()
    frames = []

    async def send(frame):
        frames.append(frame)

    await PinnedFileStreamingResponse(handle)(_scope(), _receive, send)
    assert b"".join(frame.get("body", b"") for frame in frames) == expected
    assert handle.closed
