"""Notion MCP contract tests."""
from __future__ import annotations

import json

import pytest


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class _FakeClient:
    calls: list[dict] = []
    response = _FakeResponse()

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


@pytest.fixture
def http(monkeypatch):
    from packages.core.ai.mcp import notion

    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(200, {"ok": True})
    monkeypatch.setattr(notion.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def test_notion_schema_handler_parity():
    from packages.core.ai.mcp import notion

    assert {tool["name"] for tool in notion.list_tools()} == set(notion._HANDLERS)


@pytest.mark.asyncio
async def test_notion_search_uses_versioned_bearer_request(http):
    from packages.core.ai.mcp import notion

    http.response = _FakeResponse(200, {"results": []})
    result = await notion.call_tool("search", {"query": "roadmap", "page_size": 10}, "secret-token")

    assert result["isError"] is False
    call = http.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.notion.com/v1/search"
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert call["headers"]["Notion-Version"] == "2022-06-28"
    assert call["json"] == {"query": "roadmap", "page_size": 10}


@pytest.mark.asyncio
async def test_notion_create_page_builds_parent_and_title(http):
    from packages.core.ai.mcp import notion

    http.response = _FakeResponse(200, {"id": "page-1"})
    result = await notion.call_tool(
        "create_page",
        {"parent_id": "parent-1", "title": "Launch plan", "parent_type": "page_id"},
        "secret-token",
    )

    assert result["isError"] is False
    body = http.calls[-1]["json"]
    assert body["parent"] == {"page_id": "parent-1"}
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "Launch plan"


@pytest.mark.asyncio
async def test_notion_update_page_rejects_non_object_properties_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool(
        "update_page",
        {"page_id": "page-1", "properties": []},
        "secret-token",
    )

    assert result["isError"] is True
    assert "properties" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_notion_non_2xx_is_error(http):
    from packages.core.ai.mcp import notion

    http.response = _FakeResponse(403, text="forbidden")
    result = await notion.call_tool("get_page", {"page_id": "page-1"}, "secret-token")

    assert result["isError"] is True
    assert "403" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_notion_requires_token_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool("search", {"query": "roadmap"}, "   ")

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_notion_rejects_non_string_token_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool("search", {"query": "roadmap"}, {"token": "secret-token"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_notion_rejects_non_string_page_id_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool("get_page", {"page_id": 123}, "secret-token")

    assert result["isError"] is True
    assert "page_id" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_notion_rejects_non_object_arguments_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool("search", [], "secret-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_notion_rejects_fractional_page_size_without_http(http):
    from packages.core.ai.mcp import notion

    result = await notion.call_tool(
        "search",
        {"query": "roadmap", "page_size": 1.5},
        "secret-token",
    )

    assert result["isError"] is True
    assert "page_size" in result["content"][0]["text"]
    assert http.calls == []
