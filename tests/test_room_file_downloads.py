"""Download tickets and disk ZIPs retain the room-file authorization boundary."""
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from fastapi import HTTPException, Response

from apps.api.file_responses import PinnedFileStreamingResponse
from apps.api.routers import room_file_downloads as downloads
from packages.core.services.actor_authorization import AuthenticatedUserCredential

ROOM = "01" + "R" * 24
FILE = "01" + "F" * 24


def credential():
    return AuthenticatedUserCredential(user_id="actor", entity_id="company", token_version=4)


def test_ticket_is_exact_scoped_tamper_proof_and_expiring(monkeypatch):
    body = downloads.RoomFileDownloadRequest(file_ids=[FILE])
    monkeypatch.setattr(downloads.time, "time", lambda: 1000)
    token = downloads._ticket(ROOM, body, credential())
    recovered, actor = downloads._read_ticket(token, ROOM)
    assert recovered == body
    assert actor.user_id == "actor" and actor.token_version == 4
    for bad_token, room in ((token + "0", ROOM), (token, "01" + "X" * 24)):
        with pytest.raises(HTTPException) as error:
            downloads._read_ticket(bad_token, room)
        assert error.value.status_code == 403
    monkeypatch.setattr(downloads.time, "time", lambda: 1601)
    with pytest.raises(HTTPException):
        downloads._read_ticket(token, ROOM)


def test_ticket_never_outlives_original_login(monkeypatch):
    monkeypatch.setattr(downloads.time, "time", lambda: 1000)
    token = downloads._ticket(ROOM, downloads.RoomFileDownloadRequest(file_ids=[FILE]),
        AuthenticatedUserCredential("actor", "company", 4, expires_at_epoch=1001))
    monkeypatch.setattr(downloads.time, "time", lambda: 1002)
    with pytest.raises(HTTPException):
        downloads._read_ticket(token, ROOM)


def test_archive_names_are_flat_safe_and_case_insensitive():
    names = ["../report.csv", "REPORT.csv", "report (2).csv", "a\\CON.txt", "..", "bad?.txt"]
    rows = [(str(i), name, 1) for i, name in enumerate(names)]
    assert downloads._archive_names(rows) == ["report.csv", "REPORT (3).csv", "report (2).csv", "_CON.txt", "file", "bad_.txt"]


@pytest.mark.asyncio
async def test_selection_accepts_large_file_and_rejects_aggregate_over_five_gib(monkeypatch):
    from apps.api.routers import chat_rooms
    calls = []
    async def authorize(db, room, file_id, actor, *, metadata_only):
        assert metadata_only and room == ROOM and actor == credential()
        calls.append(file_id)
        return SimpleNamespace(id=file_id, name="large.mp4", size=1024 ** 3,
                               content_sha256="a" * 64, document_id=None), None
    monkeypatch.setattr(chat_rooms, "_authorized_room_download", authorize)
    db = SimpleNamespace(expunge=lambda row: None)
    body = downloads.RoomFileDownloadRequest(file_ids=[f"{i:026d}" for i in range(5)])
    _, size = await downloads._selection(db, ROOM, body, credential())
    assert size == 5 * 1024 ** 3
    body.file_ids.append("9" * 26)
    with pytest.raises(HTTPException) as error:
        await downloads._selection(db, ROOM, body, credential())
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_selection_rejects_duplicate_or_multiple_nonarchive_files():
    for body in (downloads.RoomFileDownloadRequest(file_ids=[FILE, FILE]),
                 downloads.RoomFileDownloadRequest(file_ids=[FILE, "2" * 26], archive=False)):
        with pytest.raises(HTTPException) as error:
            await downloads._selection(None, ROOM, body, credential())
        assert error.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "denied", "changed", "cancelled"])
async def test_archive_is_zip64_and_closes_all_private_files(monkeypatch, failure):
    from apps.api.routers import chat_rooms
    body = downloads.RoomFileDownloadRequest(file_ids=[FILE, "2" * 26])
    rows = [(FILE, "report.csv", 3, None, None), ("2" * 26, "report.csv", 3, None, None)]
    selected = 0
    async def selection(*args):
        nonlocal selected
        selected += 1
        if failure == "denied" and selected == 2:
            raise HTTPException(404, "File not found")
        if failure == "changed" and selected == 2:
            return [(FILE, "report.csv", 3, "changed", None), rows[1]], 6
        return rows, 6
    monkeypatch.setattr(downloads, "_selection", selection)
    monkeypatch.setattr(downloads, "_read_ticket", lambda *args: (body, credential()))
    source = tempfile.TemporaryFile("w+b")
    source.write(b"abc")
    source.seek(0)
    returned = 0
    async def response(*args):
        nonlocal returned
        returned += 1
        if returned == 1:
            return PinnedFileStreamingResponse(source)
        return Response(b"def")
    monkeypatch.setattr(chat_rooms, "_room_file_download_response", response)
    db = SimpleNamespace(commit=AsyncMock(), expire_all=lambda: None)
    request = SimpleNamespace(is_disconnected=AsyncMock(side_effect=[False, failure == "cancelled", False, False]))
    before = downloads._budget._bytes
    if failure:
        with pytest.raises(HTTPException):
            await downloads.stream_room_file_download(ROOM, request, "token", db)
    else:
        result = await downloads.stream_room_file_download(ROOM, request, "token", db)
        assert isinstance(result, PinnedFileStreamingResponse)
        with ZipFile(result._handle) as archive:
            assert archive.namelist() == ["report.csv", "report (2).csv"]
            assert archive.read("report.csv") == b"abc"
            assert archive.read("report (2).csv") == b"def"
            assert all(item.extract_version == 45 for item in archive.infolist())
        # Exercise response lifecycle including send failure before body.
        async def send(message):
            raise RuntimeError("client disconnected")
        with pytest.raises(RuntimeError):
            await result({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
        assert result._handle.closed
    assert source.closed
    assert downloads._budget._bytes == before
