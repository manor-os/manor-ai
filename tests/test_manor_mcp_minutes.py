from __future__ import annotations

import json
import httpx
import pytest
from packages.core.ai.mcp import manor_mcp_minutes


@pytest.fixture(autouse=True)
def _ctx():
    manor_mcp_minutes.clear_call_context()
    yield
    manor_mcp_minutes.clear_call_context()


def _transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_list_tools_exposes_the_minutes_surface():
    assert {t["name"] for t in manor_mcp_minutes.list_tools()} == {
        "search_meetings", "get_transcript", "get_summary", "list_recent_meetings",
        "get_action_items", "chat_with_meeting", "get_meeting_details", "get_meeting_stats",
    }


async def test_requires_actor_and_does_not_use_shared_service_keys(monkeypatch):
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "old-service-key")
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_MINUTES_CLOUD_SECRET", "old-client-secret")
    result = await manor_mcp_minutes.call_tool("get_meeting_stats", {}, "opaque")
    assert result["isError"] and "context" in result["content"][0]["text"]
    manor_mcp_minutes.set_call_context({"entity_id": "e", "user_id": "u"})
    result = await manor_mcp_minutes.call_tool("get_meeting_stats", {}, "")
    assert result["isError"] and "OAuth" in result["content"][0]["text"]


async def test_selected_bearer_is_the_only_authentication(monkeypatch):
    manor_mcp_minutes.set_call_context({"entity_id": "e", "user_id": "u"})
    monkeypatch.setenv("MINUTES_MCP_URL", "http://127.0.0.1:18421/api/mcp/")
    seen = []
    def handler(request):
        seen.append(request)
        accepted = {value.strip() for value in request.headers.get("accept", "").split(",")}
        if not {"application/json", "text/event-stream"} <= accepted:
            return httpx.Response(406, json={"error": "Both MCP response media types are required"})
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "real provider result"}], "isError": False}})
    _transport(monkeypatch, handler)
    result = await manor_mcp_minutes.call_tool("search_meetings", {"query": "meeting"}, "opaque-grant")
    assert result["content"][0]["text"] == "real provider result"
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer opaque-grant"
    assert not {"x-api-key", "x-user-id", "x-entity-id"}.intersection(seen[0].headers)
    assert json.loads(seen[0].content)["params"] == {"name": "search_meetings", "arguments": {"query": "meeting"}}


@pytest.mark.parametrize("status", [301, 401, 403, 500])
async def test_errors_never_follow_redirect_retry_or_echo_upstream_secrets(monkeypatch, status):
    manor_mcp_minutes.set_call_context({"entity_id": "e", "user_id": "u"})
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"location": "https://other.example/"}, text="secret-upstream-body")
    _transport(monkeypatch, handler)
    result = await manor_mcp_minutes.call_tool("get_meeting_stats", {}, "opaque")
    assert result["isError"]
    assert "secret-upstream-body" not in json.dumps(result)
    assert len(seen) == 1


async def test_unknown_tool_rejected_locally():
    assert (await manor_mcp_minutes.call_tool("unknown", {}, "opaque"))["isError"]


def test_minutes_requires_account_and_is_not_context_only():
    from packages.core.services.agent_permission_service import provider_requires_integration_account_registry
    from packages.core.ai.tools.mcp_builtin import _is_context_only_server
    assert provider_requires_integration_account_registry("manor_mcp_minutes")
    assert not _is_context_only_server("manor_mcp_minutes")


def test_minutes_app_environment_override(monkeypatch):
    from packages.core.services.minutes_app import minutes_app_available_in_environment

    monkeypatch.delenv("MEETING_MINUTES_APP_AVAILABLE", raising=False)
    assert minutes_app_available_in_environment() is True
    monkeypatch.setenv("MEETING_MINUTES_APP_AVAILABLE", "0")
    assert minutes_app_available_in_environment() is False
    monkeypatch.setenv("MEETING_MINUTES_APP_AVAILABLE", "true")
    assert minutes_app_available_in_environment() is True


