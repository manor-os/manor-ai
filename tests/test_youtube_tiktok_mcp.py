"""Unit tests for the YouTube and TikTok in-process MCP servers.

HTTP is faked by swapping each module's ``httpx.AsyncClient`` — assertions are
on the method, URL, query params and JSON body the handler would send to the
platform API (no network). Mirrors tests/test_github_mcp.py.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

import packages.core.ai.mcp.youtube as yt
import packages.core.ai.mcp.tiktok as tk

_TOKEN = "oauth-test-token"


# ── httpx fake ───────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status=200, json_body=None, text=None):
        self.status_code = status
        self._json = json_body
        if text is not None:
            self.text = text
        elif json_body is not None:
            self.text = json.dumps(json_body)
        else:
            self.text = ""

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    calls: list = []
    response = _FakeResp(200, {"ok": True})
    route = None  # optional callable(method, url) -> _FakeResp

    def __init__(self, *_a, **kw):
        _FakeClient.init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def request(self, method, url, headers=None, json=None):
        _FakeClient.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        if _FakeClient.route is not None:
            return _FakeClient.route(method, url)
        return _FakeClient.response


def _split(url):
    parts = urlsplit(url)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


@pytest.fixture
def yt_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResp(200, {"ok": True})
    _FakeClient.route = None
    monkeypatch.setattr(yt.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.fixture
def tk_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResp(200, {"ok": True})
    _FakeClient.route = None
    monkeypatch.setattr(tk.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _last(fake):
    assert fake.calls, "no HTTP request was made"
    return fake.calls[-1]


# ── Registration parity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mod", [yt, tk])
def test_schema_handler_parity(mod):
    names = {t["name"] for t in mod.list_tools()}
    assert names == set(mod._HANDLERS)
    # every tool_def is well-formed
    for t in mod.list_tools():
        assert t["name"] and "inputSchema" in t and "description" in t


# ── YouTube: read ────────────────────────────────────────────────────────────


async def test_yt_search(yt_http):
    yt_http.response = _FakeResp(200, {"items": []})
    await yt.call_tool("search", {"query": "lofi", "max_results": 5}, _TOKEN)
    path, q = _split(_last(yt_http)["url"])
    assert path.endswith("/youtube/v3/search")
    assert q["q"] == "lofi" and q["type"] == "video" and q["maxResults"] == "5"
    assert q["part"] == "snippet"


async def test_yt_get_channel_mine(yt_http):
    yt_http.response = _FakeResp(200, {"items": []})
    await yt.call_tool("get_channel", {"mine": True}, _TOKEN)
    path, q = _split(_last(yt_http)["url"])
    assert path.endswith("/channels") and q["mine"] == "true"


async def test_yt_get_channel_requires_a_selector(yt_http):
    out = await yt.call_tool("get_channel", {}, _TOKEN)
    assert "Provide channel_id" in out["content"][0]["text"]
    assert not yt_http.calls


# ── YouTube: publish / engagement ────────────────────────────────────────────


async def test_yt_post_comment_body(yt_http):
    yt_http.response = _FakeResp(200, {"id": "c1"})
    await yt.call_tool("post_comment", {"video_id": "v1", "text": "nice"}, _TOKEN)
    call = _last(yt_http)
    path, q = _split(call["url"])
    assert call["method"] == "POST" and path.endswith("/commentThreads")
    assert q["part"] == "snippet"
    snip = call["json"]["snippet"]
    assert snip["videoId"] == "v1"
    assert snip["topLevelComment"]["snippet"]["textOriginal"] == "nice"


async def test_yt_rate_video(yt_http):
    yt_http.response = _FakeResp(204, None, "")
    out = await yt.call_tool("rate_video", {"video_id": "v1", "rating": "like"}, _TOKEN)
    call = _last(yt_http)
    path, q = _split(call["url"])
    assert call["method"] == "POST" and path.endswith("/videos/rate")
    assert q["id"] == "v1" and q["rating"] == "like"
    assert json.loads(out["content"][0]["text"])["ok"] is True


async def test_yt_rate_video_rejects_bad_rating(yt_http):
    out = await yt.call_tool("rate_video", {"video_id": "v1", "rating": "love"}, _TOKEN)
    assert "rating must be" in out["content"][0]["text"]
    assert not yt_http.calls


def _yt_update_route(snippet):
    """Route GET videos -> current snippet, PUT videos -> ok. update_video
    reads the current snippet before replacing it (part=snippet is a full
    replace and requires title + categoryId)."""

    def _route(method, url):
        if method == "GET":
            return _FakeResp(200, {"items": [{"snippet": snippet}]})
        return _FakeResp(200, {"id": "v1"})

    return _route


async def test_yt_update_video_tags_csv_to_list(yt_http):
    yt_http.route = _yt_update_route({"title": "Orig", "categoryId": "22"})
    await yt.call_tool("update_video", {"video_id": "v1", "title": "T", "tags": "a, b ,c"}, _TOKEN)
    call = _last(yt_http)
    assert call["method"] == "PUT"
    assert call["json"]["snippet"]["tags"] == ["a", "b", "c"]
    assert call["json"]["snippet"]["title"] == "T"


async def test_yt_update_video_preserves_title_and_category(yt_http):
    """Regression: a description-only update must keep the existing title and
    NOT reset the category to the '22' default (videos.update part=snippet
    replaces the whole snippet)."""
    yt_http.route = _yt_update_route({"title": "Original Title", "categoryId": "27", "description": "old"})
    await yt.call_tool("update_video", {"video_id": "v1", "description": "new body"}, _TOKEN)
    call = _last(yt_http)
    snip = call["json"]["snippet"]
    assert call["method"] == "PUT"
    assert snip["title"] == "Original Title"  # not dropped -> no 400
    assert snip["categoryId"] == "27"  # not clobbered to "22"
    assert snip["description"] == "new body"


async def test_yt_update_video_missing_video(yt_http):
    yt_http.route = lambda m, u: _FakeResp(200, {"items": []})
    out = await yt.call_tool("update_video", {"video_id": "gone", "title": "x"}, _TOKEN)
    assert "not found" in out["content"][0]["text"].lower()


async def test_yt_add_to_playlist_resource(yt_http):
    yt_http.response = _FakeResp(200, {"id": "pi1"})
    await yt.call_tool("add_to_playlist", {"playlist_id": "PL1", "video_id": "v9"}, _TOKEN)
    snip = _last(yt_http)["json"]["snippet"]
    assert snip["playlistId"] == "PL1"
    assert snip["resourceId"] == {"kind": "youtube#video", "videoId": "v9"}


async def test_yt_delete_comment_confirmation(yt_http):
    yt_http.response = _FakeResp(204, None, "")
    out = await yt.call_tool("delete_comment", {"comment_id": "c9"}, _TOKEN)
    path, q = _split(_last(yt_http)["url"])
    assert _last(yt_http)["method"] == "DELETE" and q["id"] == "c9"
    assert json.loads(out["content"][0]["text"])["ok"] is True


# ── TikTok: read ─────────────────────────────────────────────────────────────


async def test_tk_get_user_info_default_fields(tk_http):
    tk_http.response = _FakeResp(200, {"data": {}})
    await tk.call_tool("get_user_info", {}, _TOKEN)
    call = _last(tk_http)
    path, q = _split(call["url"])
    assert call["method"] == "GET" and path.endswith("/v2/user/info/")
    assert "follower_count" in q["fields"]


async def test_tk_list_videos_body(tk_http):
    tk_http.response = _FakeResp(200, {"data": {"videos": []}})
    await tk.call_tool("list_videos", {"max_count": 5}, _TOKEN)
    call = _last(tk_http)
    path, q = _split(call["url"])
    assert call["method"] == "POST" and path.endswith("/v2/video/list/")
    assert call["json"] == {"max_count": 5}
    assert "id" in q["fields"]


async def test_tk_list_videos_clamps_max_count(tk_http):
    """TikTok caps page size at 20; an over-range value is clamped locally."""
    tk_http.response = _FakeResp(200, {"data": {"videos": []}})
    await tk.call_tool("list_videos", {"max_count": 99}, _TOKEN)
    assert _last(tk_http)["json"]["max_count"] == 20
    tk_http.calls = []
    await tk.call_tool("list_videos", {"max_count": 0}, _TOKEN)
    assert _last(tk_http)["json"]["max_count"] == 1


async def test_tk_query_videos_csv_ids(tk_http):
    tk_http.response = _FakeResp(200, {"data": {"videos": []}})
    await tk.call_tool("query_videos", {"video_ids": "a, b"}, _TOKEN)
    call = _last(tk_http)
    assert call["json"] == {"filters": {"video_ids": ["a", "b"]}}


# ── TikTok: publish ──────────────────────────────────────────────────────────


async def test_tk_post_video_pull_from_url(tk_http):
    tk_http.response = _FakeResp(200, {"data": {"publish_id": "p1"}})
    await tk.call_tool(
        "post_video",
        {"video_url": "https://cdn.example.com/v.mp4", "title": "hi"},
        _TOKEN,
    )
    call = _last(tk_http)
    path, _ = _split(call["url"])
    assert path.endswith("/v2/post/publish/video/init/")
    body = call["json"]
    assert body["source_info"] == {"source": "PULL_FROM_URL", "video_url": "https://cdn.example.com/v.mp4"}
    # unaudited apps can only post privately — default must be SELF_ONLY
    assert body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert body["post_info"]["title"] == "hi"


async def test_tk_post_photo_urls_and_mode(tk_http):
    tk_http.response = _FakeResp(200, {"data": {"publish_id": "p2"}})
    await tk.call_tool(
        "post_photo",
        {"photo_urls": ["https://x/1.jpg", "https://x/2.jpg"], "title": "t"},
        _TOKEN,
    )
    body = _last(tk_http)["json"]
    assert body["media_type"] == "PHOTO" and body["post_mode"] == "DIRECT_POST"
    assert body["source_info"]["photo_images"] == ["https://x/1.jpg", "https://x/2.jpg"]


async def test_tk_get_publish_status(tk_http):
    tk_http.response = _FakeResp(200, {"data": {"status": "PROCESSING_UPLOAD"}})
    await tk.call_tool("get_publish_status", {"publish_id": "p1"}, _TOKEN)
    call = _last(tk_http)
    path, _ = _split(call["url"])
    assert path.endswith("/v2/post/publish/status/fetch/")
    assert call["json"] == {"publish_id": "p1"}


# ── Shared guards ────────────────────────────────────────────────────────────


async def test_yt_missing_required_param(yt_http):
    out = await yt.call_tool("get_video", {}, _TOKEN)
    assert out["isError"] is True and "Missing required" in out["content"][0]["text"]
    assert not yt_http.calls


async def test_tk_unknown_tool(tk_http):
    out = await tk.call_tool("nope", {}, _TOKEN)
    assert out["isError"] is True


async def test_auth_failure_surfaces_friendly_message(yt_http):
    yt_http.response = _FakeResp(401, None, "")
    out = await yt.call_tool("get_video", {"video_id": "v1"}, _TOKEN)
    assert "authentication failed" in out["content"][0]["text"].lower()
