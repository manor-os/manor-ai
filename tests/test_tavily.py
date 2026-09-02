from __future__ import annotations

import json

import pytest


class _Response:
    status_code = 200
    text = "{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"answer": "ok", "results": []}


class _Client:
    bodies: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url: str, *, json: dict):
        self.bodies.append(json)
        return _Response()


@pytest.mark.asyncio
async def test_tavily_search_caps_result_count_and_preserves_domain_values(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {
            "query": "manor",
            "max_results": 99,
            "include_domains": ["example.com"],
        },
        "tvly-test",
    )

    assert result["isError"] is False
    assert _Client.bodies[0]["max_results"] == 20
    assert _Client.bodies[0]["include_domains"] == ["example.com"]
    assert json.loads(result["content"][0]["text"])["query"] == "manor"


@pytest.mark.asyncio
async def test_tavily_extract_rejects_more_than_twenty_urls_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "extract",
        {"urls": [f"https://example.com/{index}" for index in range(21)]},
        "tvly-test",
    )

    assert result["isError"] is True
    assert "20" in result["content"][0]["text"]
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_search_rejects_boolean_max_results_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {"query": "manor", "max_results": True},
        "tvly-test",
    )

    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_search_rejects_fractional_max_results_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {"query": "manor", "max_results": 1.5},
        "tvly-test",
    )

    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_rejects_non_string_api_key_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {"query": "manor"},
        {"api_key": "tvly-test"},
    )

    assert result["isError"] is True
    assert "api key" in result["content"][0]["text"].lower()
    assert _Client.bodies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1])
async def test_tavily_search_rejects_non_positive_max_results_without_http(monkeypatch, value):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {"query": "manor", "max_results": value},
        "tvly-test",
    )

    assert result["isError"] is True
    assert "at least 1" in result["content"][0]["text"]
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_rejects_blank_api_key_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {"query": "manor"},
        "   ",
    )

    assert result["isError"] is True
    assert "api key" in result["content"][0]["text"].lower()
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_extract_rejects_non_http_urls_without_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "extract",
        {"urls": ["ftp://example.com/article"]},
        "tvly-test",
    )

    assert result["isError"] is True
    assert "http" in result["content"][0]["text"].lower()
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_rejects_non_object_arguments_before_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool("search", [], "tvly-test")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.bodies == []


@pytest.mark.asyncio
async def test_tavily_rejects_invalid_argument_types_before_http(monkeypatch):
    from packages.core.ai.mcp import tavily

    _Client.bodies = []
    monkeypatch.setattr(tavily.httpx, "AsyncClient", _Client)

    result = await tavily.call_tool(
        "search",
        {
            "query": 123,
            "include_domains": ["example.com", 42],
            "include_answer": "true",
        },
        "tvly-test",
    )

    assert result["isError"] is True
    assert any(
        field in result["content"][0]["text"]
        for field in ("query", "include_domains", "include_answer")
    )
    assert _Client.bodies == []
