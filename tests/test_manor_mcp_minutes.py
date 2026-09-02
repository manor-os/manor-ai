from __future__ import annotations

import hashlib
import hmac
import json
import os

import httpx
import pytest

from packages.core.ai.mcp import manor_mcp_minutes


@pytest.fixture(autouse=True)
def _ctx():
    manor_mcp_minutes.clear_call_context()
    yield
    manor_mcp_minutes.clear_call_context()


def _install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(manor_mcp_minutes.httpx, "AsyncClient", factory)


def test_list_tools_exposes_the_minutes_surface():
    names = {t["name"] for t in manor_mcp_minutes.list_tools()}
    assert names == {
        "search_meetings", "get_transcript", "get_summary",
        "list_recent_meetings", "get_action_items", "chat_with_meeting",
        "get_meeting_details", "get_meeting_stats",
    }
    for tool in manor_mcp_minutes.list_tools():
        assert tool["inputSchema"]["type"] == "object"


async def test_missing_entity_context_is_an_error(monkeypatch):
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "svc-key")
    result = await manor_mcp_minutes.call_tool(
        "search_meetings", {"query": "x"}, ""
    )
    assert result["isError"] is True
    assert "entity" in result["content"][0]["text"].lower()


def _clear_key_sources(monkeypatch):
    monkeypatch.delenv("MEETING_NOTE_TAKER_API_KEY", raising=False)
    monkeypatch.delenv("MINUTES_OAUTH_CLIENT_ID", raising=False)
    for key in list(os.environ):
        if key.startswith("MANOR_OAUTH_CLIENT_"):
            monkeypatch.delenv(key, raising=False)


async def test_missing_api_key_is_an_error(monkeypatch):
    _clear_key_sources(monkeypatch)
    manor_mcp_minutes.set_call_context({"entity_id": "ent-1"})
    result = await manor_mcp_minutes.call_tool(
        "search_meetings", {"query": "x"}, ""
    )
    assert result["isError"] is True
    assert "MEETING_NOTE_TAKER_API_KEY" in result["content"][0]["text"]


def test_service_key_derived_from_minutes_oauth_client_secret(monkeypatch):
    _clear_key_sources(monkeypatch)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_MINUTES_CLOUD_SECRET", "shared-client-secret")

    expected = hmac.new(
        b"shared-client-secret",
        b"manor-minutes-mcp-service-key-v1",
        hashlib.sha256,
    ).hexdigest()
    assert manor_mcp_minutes._resolve_service_key() == expected

    # An explicit key always wins over the derivation.
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "explicit-key")
    assert manor_mcp_minutes._resolve_service_key() == "explicit-key"


def test_service_key_derivation_respects_client_id_overrides(monkeypatch):
    _clear_key_sources(monkeypatch)
    # A different client's secret must not be used for Minutes.
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_SECRET", "other-secret")
    assert manor_mcp_minutes._resolve_service_key() is None

    # Custom client id mapped via MANOR_OAUTH_CLIENT_<SLUG>_CLIENT_ID.
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_MEETINGS_SECRET", "meet-secret")
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_MEETINGS_CLIENT_ID", "minutes-cloud")
    expected = hmac.new(
        b"meet-secret", b"manor-minutes-mcp-service-key-v1", hashlib.sha256
    ).hexdigest()
    assert manor_mcp_minutes._resolve_service_key() == expected


async def test_forwards_call_with_service_headers(monkeypatch):
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "svc-key")
    manor_mcp_minutes.set_call_context({"entity_id": "ent-1", "user_id": "u-1"})

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Found 1 meeting"}],
                "isError": False,
            },
        })

    _install_transport(monkeypatch, handler)

    result = await manor_mcp_minutes.call_tool(
        "search_meetings", {"query": "mobile", "limit": 5}, ""
    )

    assert result == {
        "content": [{"type": "text", "text": "Found 1 meeting"}],
        "isError": False,
    }
    assert seen["headers"]["x-api-key"] == "svc-key"
    assert seen["headers"]["x-entity-id"] == "ent-1"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {
        "name": "search_meetings",
        "arguments": {"query": "mobile", "limit": 5},
    }


async def test_service_401_surfaces_as_error(monkeypatch):
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "svc-key")
    manor_mcp_minutes.set_call_context({"entity_id": "ent-1"})
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": "unauthorized"}),
    )

    result = await manor_mcp_minutes.call_tool("get_meeting_stats", {}, "")
    assert result["isError"] is True
    assert "credentials" in result["content"][0]["text"].lower()


async def test_rpc_error_surfaces_as_error(monkeypatch):
    monkeypatch.setenv("MEETING_NOTE_TAKER_API_KEY", "svc-key")
    manor_mcp_minutes.set_call_context({"entity_id": "ent-1"})
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32602, "message": "Unknown tool"},
        }),
    )

    result = await manor_mcp_minutes.call_tool(
        "get_transcript", {"meeting_id": "m1"}, ""
    )
    assert result["isError"] is True
    assert "-32602" in result["content"][0]["text"]


async def test_unknown_tool_rejected_locally():
    result = await manor_mcp_minutes.call_tool("not_a_tool", {}, "")
    assert result["isError"] is True


# ── App-subscription gating ─────────────────────────────────────────────────


async def test_permission_gate_requires_app_subscription(monkeypatch):
    from packages.core.services import minutes_app
    from packages.core.services.agent_permission_service import can_use_integration

    async def _subscribed(db, entity_id):
        return entity_id == "ent-subscribed"

    monkeypatch.setattr(minutes_app, "minutes_app_subscribed", _subscribed)

    allowed = await can_use_integration(
        None, user_id="u1", entity_id="ent-subscribed",
        provider="manor_mcp_minutes",
    )
    assert allowed.allowed is True
    assert allowed.scope == "internal"

    denied = await can_use_integration(
        None, user_id="u1", entity_id="ent-other",
        provider="manor_mcp_minutes",
    )
    assert denied.allowed is False
    assert "Meeting Minutes app" in denied.reason


def test_minutes_app_environment_override(monkeypatch):
    from packages.core.services.minutes_app import minutes_app_available_in_environment

    monkeypatch.delenv("MEETING_MINUTES_APP_AVAILABLE", raising=False)
    assert minutes_app_available_in_environment() is True
    monkeypatch.setenv("MEETING_MINUTES_APP_AVAILABLE", "0")
    assert minutes_app_available_in_environment() is False
    monkeypatch.setenv("MEETING_MINUTES_APP_AVAILABLE", "true")
    assert minutes_app_available_in_environment() is True


