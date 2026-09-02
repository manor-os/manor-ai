from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("module_name", "tool_name", "arguments"),
    [
        ("linkedin", "get_profile", {}),
        ("twitter_x", "get_me", {}),
        ("youtube", "search", {"query": "qa"}),
        ("tiktok", "get_user_info", {}),
    ],
)
@pytest.mark.asyncio
async def test_social_mcp_rejects_non_string_token_before_http(
    monkeypatch, module_name, tool_name, arguments
):
    module = __import__(f"packages.core.ai.mcp.{module_name}", fromlist=[module_name])

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("non-string tokens must fail before creating an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", _UnexpectedClient)
    result = await module.call_tool(tool_name, arguments, {"token": "bad"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()


@pytest.mark.parametrize(
    ("module_name", "tool_name"),
    [
        ("linkedin", "get_profile"),
        ("twitter_x", "get_me"),
        ("youtube", "search"),
        ("tiktok", "get_user_info"),
    ],
)
@pytest.mark.asyncio
async def test_social_mcp_rejects_non_object_arguments_before_http(
    monkeypatch, module_name, tool_name
):
    module = __import__(f"packages.core.ai.mcp.{module_name}", fromlist=[module_name])

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("non-object arguments must fail before creating an HTTP client")

    monkeypatch.setattr(module.httpx, "AsyncClient", _UnexpectedClient)
    arguments = [] if tool_name != "search" else []
    result = await module.call_tool(tool_name, arguments, "oauth-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
