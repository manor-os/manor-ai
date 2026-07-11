from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from packages.core.services import integration_health


def _load_wechat_mcp_module():
    path = Path(__file__).resolve().parents[1] / "packages/core/ai/mcp/wechat_personal.py"
    spec = importlib.util.spec_from_file_location("wechat_personal_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wechat_mcp = _load_wechat_mcp_module()


def _load_ilink_client_module():
    path = Path(__file__).resolve().parents[1] / "apps/wechat_personal_runner/ilink_client.py"
    spec = importlib.util.spec_from_file_location("ilink_client_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ilink_client = _load_ilink_client_module()


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, module, transport: httpx.MockTransport) -> None:
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        return real_async_client(
            transport=transport,
            timeout=kwargs.get("timeout"),
            headers=kwargs.get("headers"),
        )

    monkeypatch.setattr(module.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_wechat_personal_health_uses_session_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer runner-secret"
        if request.url.path == "/sessions/sid-123/status":
            return httpx.Response(
                200,
                json={
                    "session_id": "sid-123",
                    "online": True,
                    "callback_configured": True,
                },
            )
        return httpx.Response(410, json={"detail": "legacy endpoint"})

    _patch_async_client(monkeypatch, integration_health, httpx.MockTransport(handler))

    result = await integration_health.test_wechat_personal(
        {
            "runner_url": "https://runner.example",
            "bearer_token": "runner-secret",
            "session_id": "sid-123",
        },
        wiring_ctx={
            "expected_url": ("https://app.example/api/v1/channels/wechat_personal/callback?config_id=cc1"),
        },
    )

    assert result["ok"] is True
    assert result["detail"] == "session online + callback registered"
    assert result["wiring"]["ok"] is True
    assert [req.url.path for req in seen] == ["/sessions/sid-123/status"]


@pytest.mark.asyncio
async def test_wechat_personal_health_flags_missing_runner_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sessions/sid-123/status"
        return httpx.Response(
            200,
            json={
                "session_id": "sid-123",
                "online": True,
                "callback_configured": False,
            },
        )

    _patch_async_client(monkeypatch, integration_health, httpx.MockTransport(handler))

    result = await integration_health.test_wechat_personal(
        {"runner_url": "https://runner.example", "session_id": "sid-123"},
    )

    assert result["ok"] is False
    assert "callback is not registered" in result["detail"]
    assert result["wiring"]["ok"] is False


@pytest.mark.asyncio
async def test_wechat_personal_mcp_uses_session_scoped_status_and_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer runner-secret"
        if request.url.path == "/sessions/sid-123/status":
            return httpx.Response(
                200,
                json={
                    "session_id": "sid-123",
                    "online": True,
                    "callback_configured": True,
                    "known_peers": ["peer-1"],
                },
            )
        if request.url.path == "/sessions/sid-123/messages":
            return httpx.Response(200, json={"success": True, "msg_id": "msg-1"})
        return httpx.Response(410, json={"detail": "legacy endpoint"})

    _patch_async_client(monkeypatch, wechat_mcp, httpx.MockTransport(handler))
    token = json.dumps(
        {
            "runner_url": "https://runner.example",
            "bearer_token": "runner-secret",
            "session_id": "sid-123",
        }
    )

    status = await wechat_mcp.call_tool("get_bot_status", {}, token)
    contacts = await wechat_mcp.call_tool("list_contacts", {}, token)
    sent = await wechat_mcp.call_tool(
        "send_direct_message",
        {"contact_id": "peer-1", "content": "hello"},
        token,
    )

    assert status["isError"] is False
    assert contacts["isError"] is False
    assert sent["isError"] is False
    assert seen == [
        ("GET", "/sessions/sid-123/status", None),
        ("GET", "/sessions/sid-123/status", None),
        (
            "POST",
            "/sessions/sid-123/messages",
            {"kind": "direct", "target": "peer-1", "body": "hello"},
        ),
    ]


@pytest.mark.asyncio
async def test_wechat_personal_mcp_requires_session_id() -> None:
    result = await wechat_mcp.call_tool(
        "get_bot_status",
        {},
        json.dumps({"runner_url": "https://runner.example"}),
    )

    assert result["isError"] is True
    assert "missing session_id" in result["content"][0]["text"]


def test_ilink_parse_message_accepts_protocol_from_user_id() -> None:
    parsed = ilink_client.ILinkClient.parse_message(
        {
            "from_user_id": "peer-1",
            "to_user_id": "bot-1",
            "message_type": 1,
            "context_token": "ctx-1",
            "item_list": [
                {"type": 1, "text_item": {"text": "hello"}},
            ],
        }
    )

    assert parsed is not None
    assert parsed.ilink_user_id == "peer-1"
    assert parsed.text == "hello"
    assert parsed.context_token == "ctx-1"
    assert parsed.is_from_bot is False
