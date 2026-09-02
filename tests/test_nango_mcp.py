"""Focused error-envelope tests for the in-process Nango MCP adapter."""

from __future__ import annotations

import json

import pytest

from packages.core.ai.mcp import nango


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = nango.httpx.Request("GET", "https://nango.test")
            response = nango.httpx.Response(
                self.status_code,
                request=request,
                text=self.text,
            )
            raise nango.httpx.HTTPStatusError(
                "upstream rejected request",
                request=request,
                response=response,
            )


class _Client:
    response = _Response(200, {})

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return self.response

    async def request(self, *_args, **_kwargs):
        return self.response


@pytest.fixture
def http(monkeypatch):
    _Client.response = _Response(200, {})
    monkeypatch.setattr(nango.httpx, "AsyncClient", _Client)
    return _Client


@pytest.mark.parametrize(
    "payload",
    [
        {"configs": [{"unique_key": "github", "provider": "github"}]},
        [{"unique_key": "github", "provider": "github"}],
    ],
)
async def test_list_providers_accepts_dict_and_list_config_shapes(http, payload):
    http.response = _Response(200, payload)

    result = await nango.call_tool("nango_list_providers", {}, "secret")

    assert result["isError"] is False
    body = json.loads(result["content"][0]["text"])
    assert body == {
        "count": 1,
        "providers": [
            {
                "provider_config_key": "github",
                "provider": "github",
                "auth_mode": None,
            }
        ],
    }


async def test_proxy_non_2xx_is_an_error_envelope(http):
    http.response = _Response(403, {"error": "forbidden"})
    nango.set_call_context({"nango_allowed_connection_ids": ["connection-1"]})
    try:
        result = await nango.call_tool(
            "nango_proxy",
            {
                "provider_config_key": "github",
                "connection_id": "connection-1",
                "method": "GET",
                "endpoint": "/user",
            },
            "secret",
        )
    finally:
        nango.clear_call_context()

    assert result["isError"] is True
    assert "403" in result["content"][0]["text"]
