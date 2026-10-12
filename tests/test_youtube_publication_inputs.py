"""YouTube publication inputs retain complete inventory and exact attachment ACLs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from packages.core.ai.mcp import youtube as yt
from packages.core.models.base import generate_ulid
from tests.test_chat_attachment_read_access import seed_attachment


@pytest.fixture(autouse=True)
def clear_context():
    yt.clear_call_context()
    yield
    yt.clear_call_context()


@pytest.fixture
def video_fs(monkeypatch, tmp_path):
    from packages.core.config import get_settings
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def youtube_http(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/test"})
        return httpx.Response(200, json={"id": "new-video", "status": {"privacyStatus": "private"}})

    transport = httpx.MockTransport(respond)
    client = httpx.AsyncClient

    def make_client(**kwargs):
        kwargs["transport"] = transport
        return client(**kwargs)

    monkeypatch.setattr(yt.httpx, "AsyncClient", make_client)
    return calls


async def _video_attachment(db_session, video_fs, *, workspace=False):
    user, conv, msg, ws, source, path = await seed_attachment(db_session, video_fs, workspace=workspace)
    video = source.with_suffix(".mp4")
    source.rename(video)
    path = str(Path(path).with_suffix(".mp4"))
    msg.attachments = [{"kind": "chat_upload", "path": path}]
    await db_session.commit()
    ctx = {
        "entity_id": user.entity_id, "user_id": user.id,
        "conversation_id": conv.id,
    }
    if ws:
        ctx["workspace_id"] = ws.id
    return ctx, msg, video, path


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("url_form", [False, True])
async def test_original_chat_attachment_uploads_exact_bytes(
    db_session, video_fs, youtube_http, monkeypatch, workspace, url_form,
):
    ctx, _msg, original, path = await _video_attachment(db_session, video_fs, workspace=workspace)
    original_bytes = original.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    staged = []
    stage = yt._stage_video_source

    async def capture_stage(value):
        result = await stage(value)
        staged.append(result[0])
        return result

    monkeypatch.setattr(yt, "_stage_video_source", capture_stage)
    yt.set_call_context(ctx)
    reference = f"/api/v1/fs/{ctx['entity_id']}/{path}" if url_form else path
    result = await yt.call_tool("upload_video", {
        "video_url": reference, "title": "Approved original attachment", "made_for_kids": False,
    }, "test-token")

    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["id"] == "new-video"
    assert [request.method for request in youtube_http] == ["POST", "PUT"]
    assert youtube_http[-1].content == original_bytes
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_digest
    assert staged and all(not temporary.exists() for temporary in staged)
    assert staged[0] != original


@pytest.mark.parametrize("restriction", [
    "foreign_user", "foreign_conversation", "foreign_workspace", "foreign_entity",
    "unpersisted", "symlink", "missing", "missing_context",
])
async def test_attachment_denial_precedes_upload(
    db_session, video_fs, youtube_http, restriction,
):
    ctx, msg, original, path = await _video_attachment(db_session, video_fs)
    if restriction.startswith("foreign_"):
        key = {
            "foreign_user": "user_id", "foreign_conversation": "conversation_id",
            "foreign_workspace": "workspace_id", "foreign_entity": "entity_id",
        }[restriction]
        ctx[key] = generate_ulid()
    elif restriction == "unpersisted":
        msg.attachments = None
        msg.content = path  # Mentioning a real file is not an attachment grant.
        await db_session.commit()
    elif restriction == "symlink":
        private = original.with_name("private.mp4")
        original.rename(private)
        original.symlink_to(private)
    elif restriction == "missing":
        original.unlink()
    else:
        ctx = {}
    yt.set_call_context(ctx)
    result = await yt.call_tool("upload_video", {
        "video_url": path, "title": "Never upload this",
    }, "test-token")
    assert result["isError"] is True
    assert not youtube_http


async def test_chat_staging_cancel_removes_partial_copy(db_session, video_fs, monkeypatch):
    ctx, _msg, original, path = await _video_attachment(db_session, video_fs)
    original_bytes = original.read_bytes()
    yt.set_call_context(ctx)
    temporary = []
    mkstemp = yt.tempfile.mkstemp

    def capture_temp(**kwargs):
        fd, name = mkstemp(**kwargs)
        temporary.append(Path(name))
        return fd, name

    async def cancel_after_chunk(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(yt.tempfile, "mkstemp", capture_temp)
    monkeypatch.setattr(yt, "asyncio", SimpleNamespace(sleep=cancel_after_chunk))
    with pytest.raises(asyncio.CancelledError):
        await yt._stage_video_source(path)
    assert temporary and all(not file.exists() for file in temporary)
    assert original.read_bytes() == original_bytes


async def test_public_https_staging_still_streams_without_chat_context(monkeypatch):
    data = b"public-video-bytes"
    requests = []

    async def resolve(url):
        return SimpleNamespace(url=url)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=data, headers={"content-type": "video/mp4"})

    monkeypatch.setattr(yt, "resolve_public_https_target", resolve)
    monkeypatch.setattr(yt, "create_public_https_transport", lambda _target: httpx.MockTransport(respond))
    path, size, mime = await yt._stage_video_source("https://cdn.example.org/approved.mp4")
    try:
        assert path.read_bytes() == data
        assert (size, mime) == (len(data), "video/mp4")
        assert "authorization" not in requests[0].headers
    finally:
        path.unlink()


def _install_inventory_http(monkeypatch, pages):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{
                "id": "my-channel", "contentDetails": {"relatedPlaylists": {"uploads": "my-uploads"}},
            }]})
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])

    client = httpx.AsyncClient
    monkeypatch.setattr(yt.httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    return calls


async def test_own_inventory_keeps_twelve_long_items_and_pagination(monkeypatch):
    items = [{
        "id": f"playlist-item-{i}",
        "snippet": {
            "title": f"Video {i}", "description": "long caption " * 1500,
            "thumbnails": {"default": {"url": "https://images.example/a" * 100}},
            "resourceId": {"videoId": f"video-{i}"},
        },
        "contentDetails": {"videoId": f"video-{i}", "videoPublishedAt": "2026-09-14T00:00:00Z"},
        "status": {"privacyStatus": "private" if i == 11 else "public"},
    } for i in range(12)]
    pages = {
        "": {"items": items[:4], "nextPageToken": "middle-page", "pageInfo": {"totalResults": 12, "resultsPerPage": 4}},
        "middle-page": {"items": items[4:8], "nextPageToken": "last-page", "pageInfo": {"totalResults": 12, "resultsPerPage": 4}},
        "last-page": {"items": items[8:], "prevPageToken": "middle-page", "pageInfo": {"totalResults": 12, "resultsPerPage": 4}},
    }
    calls = _install_inventory_http(monkeypatch, pages)
    next_cursor = None
    found = []
    for _ in range(3):
        page = await yt.call_tool("list_my_videos", {"max_results": 12, "page_token": next_cursor}, "test-token")
        raw = page["content"][0]["text"]
        assert page["isError"] is False
        parsed = json.loads(raw)
        assert parsed["source"] == "uploads_playlist"
        assert parsed["pageInfo"]["totalResults"] == 12
        assert len(raw) < 4000 and "long caption" not in raw and "truncated" not in raw
        found.extend(parsed["items"])
        next_cursor = parsed.get("nextPageToken")
    assert [item["id"]["videoId"] for item in found] == [f"video-{i}" for i in range(12)]
    assert found[-1]["status"]["privacyStatus"] == "private"
    assert parsed["prevPageToken"] == "middle-page" and "nextPageToken" not in parsed
    playlist_calls = [request for request in calls if request.url.path.endswith("/playlistItems")]
    assert len(playlist_calls) == 3
    assert playlist_calls[-1].url.params["pageToken"] == "last-page"
    assert all(request.url.params["playlistId"] == "my-uploads" for request in playlist_calls)
    assert all(request.url.params["maxResults"] == "4" for request in playlist_calls)
    assert not any(request.url.path.endswith("/search") for request in calls)


@pytest.mark.parametrize("title", ['"\\\n\t' * 25, "\x01" * 100, "完整的视频标题" * 14 + "完整"])
async def test_largest_inventory_page_keeps_every_identity_in_model_context(monkeypatch, title):
    from packages.core.ai.agentic_loop import (
        TOOL_RESULT_MAX_CHARS, _compact_tool_result_for_context,
    )

    ids = [f"abcdefghij{i}" for i in range(4)]
    next_cursor, previous_cursor = "N" * 100, "P" * 100
    calls = _install_inventory_http(monkeypatch, {"": {
        "nextPageToken": next_cursor, "prevPageToken": previous_cursor,
        "pageInfo": {"totalResults": 10000, "resultsPerPage": 4},
        "items": [{
            "snippet": {"title": title, "description": "caption" * 10000},
            "contentDetails": {"videoId": video_id, "videoPublishedAt": "2026-09-14T00:00:00Z"},
            "status": {"privacyStatus": "unlisted"},
        } for video_id in ids],
    }})
    response = await yt.call_tool("list_my_videos", {"max_results": 50}, "test-token")
    raw = response["content"][0]["text"]
    visible = _compact_tool_result_for_context("mcp__youtube__list_my_videos", raw)
    parsed = json.loads(visible)
    assert len(raw) <= TOOL_RESULT_MAX_CHARS
    assert [item["id"]["videoId"] for item in parsed["items"]] == ids
    assert all(item["snippet"]["title"] == title for item in parsed["items"])
    assert parsed["nextPageToken"] == next_cursor
    assert parsed["prevPageToken"] == previous_cursor
    assert calls[-1].url.params["maxResults"] == "4"


@pytest.mark.parametrize("arguments", [{"query": "Manor"}, {"order": "viewCount"}])
async def test_explicit_search_remains_paginated_and_reports_incomplete_coverage(monkeypatch, arguments):
    calls = _install_inventory_http(monkeypatch, {"next": {
        "items": [{"id": {"videoId": "found-video"}, "snippet": {"title": "Found", "description": "x" * 15000}}],
        "nextPageToken": "later", "pageInfo": {"totalResults": 5},
    }})
    result = await yt.call_tool("list_my_videos", {**arguments, "page_token": "next"}, "test-token")
    data = json.loads(result["content"][0]["text"])
    assert data["source"] == "search" and "lag or omit" in data["note"]
    assert data["nextPageToken"] == "later" and len(data["items"]) == 1
    assert len(calls) == 1 and calls[0].url.path.endswith("/search")
    assert calls[0].url.params["pageToken"] == "next"


async def test_inventory_never_silently_drops_an_unidentified_item(monkeypatch):
    _install_inventory_http(monkeypatch, {"": {"items": [{"snippet": {"title": "Missing ID"}}]}})
    result = await yt.call_tool("list_my_videos", {}, "test-token")
    assert result["isError"] is True
    assert "inventory is incomplete" in result["content"][0]["text"]


async def test_unexpectedly_large_inventory_page_requires_retry_of_same_cursor(monkeypatch):
    _install_inventory_http(monkeypatch, {"original-page": {
        "items": [{"snippet": {"title": "unexpected provider title" * 500,
                                "resourceId": {"videoId": "must-not-skip"}}}],
        "nextPageToken": "do-not-advance",
    }})
    response = await yt.call_tool("list_my_videos", {"page_token": "original-page"}, "test-token")
    assert response["isError"] is True
    message = response["content"][0]["text"]
    assert "same page_token" in message and "max_results=1" in message
    assert "incomplete" in message and "do-not-advance" not in message
