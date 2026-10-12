"""Apify connection probes must stay read-only and never reveal credentials."""
import json

import httpx
import pytest

from packages.core.ai.mcp import _remote
from packages.core.services.apify_client import APIFY_MCP_ENDPOINT
from packages.core.services import integration_health

pytestmark = pytest.mark.oss_smoke

_REQUIRED_TOOLS = {"call-actor", "get-actor-run", "get-dataset-items", "abort-actor-run"}


def _mock_mcp(monkeypatch, *, tools=None, status=200, error=None):
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if error:
            raise error
        if status != 200:
            return httpx.Response(status, text="secret token and account data")
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif body["method"] == "notifications/initialized":
            return httpx.Response(202)
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": name} for name in (_REQUIRED_TOOLS if tools is None else tools)]}
        else:
            raise AssertionError("A connection test must not execute an Actor")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(
        _remote.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests


@pytest.mark.asyncio
async def test_apify_connection_only_initializes_and_lists_official_mcp_tools(monkeypatch):
    requests = _mock_mcp(monkeypatch)
    result = await integration_health.run_test("apify", {"api_key": "  apify_api_test  "})

    assert result["ok"] is True
    assert [body["method"] for _, body in requests] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert all(str(request.url) == APIFY_MCP_ENDPOINT for request, _ in requests)
    assert all(request.headers["Authorization"] == "Bearer apify_api_test" for request, _ in requests)
    assert "Official Apify MCP" in result["detail"]
    assert "apify_api_test" not in result["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", [None, "", "  ", 42])
async def test_apify_missing_key_never_contacts_provider(monkeypatch, api_key):
    requests = _mock_mcp(monkeypatch)
    result = await integration_health.run_test("apify", {"api_key": api_key})

    assert result["ok"] is False
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, -32001), (403, 403), (429, 429), (503, 503)])
async def test_apify_failed_probe_does_not_expose_response(monkeypatch, status, code):
    _mock_mcp(monkeypatch, status=status)
    result = await integration_health.run_test("apify", {"api_key": "apify_api_test"})

    assert result["ok"] is False
    assert str(code) in result["detail"]
    assert "secret" not in result["detail"]
    assert "apify_api_test" not in result["detail"]


@pytest.mark.asyncio
async def test_apify_network_failure_does_not_expose_request(monkeypatch):
    _mock_mcp(monkeypatch, error=RuntimeError("Authorization: Bearer apify_api_test"))
    result = await integration_health.run_test("apify", {"api_key": "apify_api_test"})

    assert result["ok"] is False
    assert "unreachable" in result["detail"]
    assert "apify_api_test" not in result["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_tool", sorted(_REQUIRED_TOOLS))
async def test_apify_connection_requires_each_collection_tool(monkeypatch, missing_tool):
    _mock_mcp(monkeypatch, tools=_REQUIRED_TOOLS - {missing_tool})
    result = await integration_health.run_test("apify", {"api_key": "apify_api_test"})

    assert result["ok"] is False
    assert "missing required tools" in result["detail"]
    assert missing_tool in result["detail"]
