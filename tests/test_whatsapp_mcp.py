"""WhatsApp Business Cloud MCP surface and dispatch regression tests."""

from __future__ import annotations

import json

import pytest


_EXPECTED_TOOLS = {
    "get_phone_number",
    "list_phone_numbers",
    "get_business_profile",
    "update_business_profile",
    "send_text",
    "send_template",
    "send_image",
    "send_document",
    "send_audio",
    "send_video",
    "mark_as_read",
    "list_message_templates",
    "create_message_template",
    "delete_message_template",
}


def _credentials(**overrides: str) -> str:
    data = {
        "access_token": "token",
        "phone_number_id": "phone-123",
        "waba_id": "waba-456",
    }
    data.update(overrides)
    return json.dumps(data)


def test_whatsapp_tools_are_registered_and_advertised() -> None:
    from packages.core.ai.mcp import get_module, whatsapp
    from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS

    assert get_module("whatsapp") is whatsapp
    module_names = {tool["name"] for tool in whatsapp.list_tools()}
    deferred_names = {tool["name"] for tool in _SERVER_TOOL_SCHEMAS["whatsapp"]}
    assert module_names == _EXPECTED_TOOLS
    assert deferred_names == _EXPECTED_TOOLS

    for tool in whatsapp.list_tools():
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)


@pytest.mark.asyncio
async def test_missing_or_malformed_credentials_return_friendly_errors() -> None:
    from packages.core.ai.mcp import whatsapp

    missing = await whatsapp.call_tool("get_phone_number", {}, "")
    malformed = await whatsapp.call_tool("get_phone_number", {}, "not-json")

    assert missing["isError"] is True
    assert "access_token" in missing["content"][0]["text"]
    assert malformed["isError"] is True
    assert "malformed" in malformed["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_phone_number_uses_native_meta_graph(monkeypatch) -> None:
    from packages.core.ai.mcp import whatsapp

    captured = {}

    async def _get(path, *, token, params=None):
        captured.update(path=path, token=token, params=params)
        return {"id": "phone-123", "verified_name": "Manor AI"}

    monkeypatch.setattr(whatsapp._graph, "get", _get)
    result = await whatsapp.call_tool("get_phone_number", {}, _credentials())

    assert result["isError"] is False
    assert captured["path"] == "/phone-123"
    assert captured["token"] == "token"
    assert "quality_rating" in captured["params"]["fields"]


@pytest.mark.asyncio
async def test_send_text_dispatches_to_cloud_adapter(monkeypatch) -> None:
    from packages.core.ai.mcp import whatsapp

    captured = {}

    async def _send_text(self, to, text):
        captured.update(
            phone_number_id=self.phone_number_id,
            access_token=self.access_token,
            to=to,
            text=text,
        )
        return {"external_id": "wamid.1", "status": "sent"}

    monkeypatch.setattr(whatsapp.WhatsAppAdapter, "send_text", _send_text)
    result = await whatsapp.call_tool(
        "send_text",
        {"to": "+14155550123", "text": "Hello from Manor"},
        _credentials(),
    )

    assert result["isError"] is False
    assert captured == {
        "phone_number_id": "phone-123",
        "access_token": "token",
        "to": "+14155550123",
        "text": "Hello from Manor",
    }


@pytest.mark.asyncio
async def test_create_template_uses_waba_and_json_body(monkeypatch) -> None:
    from packages.core.ai.mcp import whatsapp

    captured = {}

    async def _post(path, body, *, token, json_body=False):
        captured.update(path=path, body=body, token=token, json_body=json_body)
        return {"id": "template-789", "status": "PENDING"}

    monkeypatch.setattr(whatsapp._graph, "post", _post)
    result = await whatsapp.call_tool(
        "create_message_template",
        {
            "name": "order_update",
            "category": "utility",
            "language": "en_US",
            "components": [
                {"type": "BODY", "text": "Your order {{1}} is ready."},
            ],
            "allow_category_change": True,
        },
        _credentials(),
    )

    assert result["isError"] is False
    assert captured["path"] == "/waba-456/message_templates"
    assert captured["token"] == "token"
    assert captured["json_body"] is True
    assert captured["body"]["category"] == "UTILITY"
    assert captured["body"]["allow_category_change"] is True


@pytest.mark.asyncio
async def test_delete_template_passes_name_as_query_param(monkeypatch) -> None:
    from packages.core.ai.mcp import whatsapp

    captured = {}

    async def _delete(path, *, token, params=None):
        captured.update(path=path, token=token, params=params)
        return {"success": True}

    monkeypatch.setattr(whatsapp._graph, "delete", _delete)
    result = await whatsapp.call_tool(
        "delete_message_template",
        {"name": "order_update"},
        _credentials(),
    )

    assert result["isError"] is False
    assert captured == {
        "path": "/waba-456/message_templates",
        "token": "token",
        "params": {"name": "order_update"},
    }


@pytest.mark.asyncio
async def test_legacy_credential_aliases_remain_supported(monkeypatch) -> None:
    from packages.core.ai.mcp import whatsapp

    async def _get(path, *, token, params=None):
        assert path == "/legacy-phone"
        assert token == "legacy-token"
        return {"id": "legacy-phone"}

    monkeypatch.setattr(whatsapp._graph, "get", _get)
    creds = json.dumps({"api_key": "legacy-token", "phone_id": "legacy-phone"})
    result = await whatsapp.call_tool("get_phone_number", {}, creds)

    assert result["isError"] is False
