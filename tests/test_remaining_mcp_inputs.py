from __future__ import annotations

import json

import pytest


@pytest.mark.parametrize(
    ("module_name", "tool_name", "arguments", "credentials"),
    [
        (
            "amazon",
            "get_orders",
            {},
            json.dumps({"access_token": {"bad": "token"}, "region": "na"}),
        ),
        (
            "tiktok_shop",
            "get_authorized_shops",
            {},
            json.dumps({"app_key": ["bad"], "app_secret": "secret", "access_token": "token"}),
        ),
        ("jimeng", "generate_image", {"prompt": "qa"}, {"token": "bad"}),
        ("producthunt", "me", {}, {"token": "bad"}),
    ],
)
@pytest.mark.asyncio
async def test_remaining_mcp_rejects_non_string_token_before_http(
    monkeypatch, module_name, tool_name, arguments, credentials
):
    module = __import__(f"packages.core.ai.mcp.{module_name}", fromlist=[module_name])

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("malformed credentials must fail before creating an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", _UnexpectedClient)
    result = await module.call_tool(tool_name, arguments, credentials)

    assert result["isError"] is True


@pytest.mark.parametrize(
    ("module_name", "tool_name", "credentials"),
    [
        ("amazon", "get_orders", json.dumps({"access_token": "token", "region": "na"})),
        (
            "tiktok_shop",
            "get_authorized_shops",
            json.dumps({"app_key": "key", "app_secret": "secret", "access_token": "token"}),
        ),
        ("jimeng", "generate_image", "sessionid"),
        ("producthunt", "me", "oauth-token"),
    ],
)
@pytest.mark.asyncio
async def test_remaining_mcp_rejects_non_object_arguments_before_http(
    monkeypatch, module_name, tool_name, credentials
):
    module = __import__(f"packages.core.ai.mcp.{module_name}", fromlist=[module_name])

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("non-object arguments must fail before creating an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", _UnexpectedClient)
    result = await module.call_tool(tool_name, [], credentials)

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
