from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from packages.core.services import integration_health
from tests.test_document_permissions import _auth, _create_entity_user


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


@pytest.mark.asyncio
async def test_wechat_runner_restore_fails_closed_without_shared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.routers import integrations as integrations_router

    monkeypatch.setattr(integrations_router, "_WECHAT_RUNNER_BEARER", "")

    with pytest.raises(HTTPException) as exc_info:
        await integrations_router._require_wechat_runner_bearer(None)

    assert exc_info.value.status_code == 503


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, module, transport: httpx.MockTransport) -> None:
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        return real_async_client(
            transport=transport,
            timeout=kwargs.get("timeout"),
            headers=kwargs.get("headers"),
        )

    monkeypatch.setattr(getattr(module, "httpx", module), "AsyncClient", _factory)


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
async def test_finishing_wechat_personal_session_registers_runner_callback(
    client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.api.routers import integrations as integrations_router
    from packages.core.models.document import Integration
    from packages.core.models.user import UserMembership
    from packages.core.services.channels import wechat_personal_adapter

    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer runner-secret"
        if request.url.path == "/sessions":
            return httpx.Response(
                200,
                json={"session_id": "runner-session", "qr_path": "/sessions/runner-session/qr.png"},
            )
        if request.url.path == "/sessions/runner-session/status":
            return httpx.Response(
                200,
                json={
                    "online": True,
                    "account": {"nick_name": "Manor WeChat"},
                },
            )
        if request.url.path == "/sessions/runner-session/credentials":
            return httpx.Response(
                200,
                json={
                    "session_id": "runner-session",
                    "bot_token": "ilink-bot-token",
                    "base_url": "https://ilink.example",
                },
            )
        if request.url.path == "/sessions/runner-session/config":
            assert body and body["callback_url"].startswith(
                "https://manor.test/api/v1/channels/wechat_personal/callback?config_id="
            )
            assert body["bearer_token"] == "runner-secret"
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"Unexpected runner request: {request.method} {request.url.path}")

    _patch_async_client(monkeypatch, httpx, httpx.MockTransport(handler))
    monkeypatch.setattr(integrations_router, "_WECHAT_RUNNER_URL", "https://runner.example")
    monkeypatch.setattr(integrations_router, "_WECHAT_RUNNER_BEARER", "runner-secret")
    monkeypatch.setattr(
        wechat_personal_adapter,
        "get_settings",
        lambda: SimpleNamespace(PUBLIC_BASE_URL="https://manor.test"),
    )
    headers = await _auth(client, "wechat_personal_finish_callback")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member = await _create_entity_user(owner["entity_id"], "wechat_personal_other_user")
    db_session.add(UserMembership(
        user_id=member["id"],
        entity_id=owner["entity_id"],
        role="member",
        status="active",
    ))
    await db_session.commit()

    started = await client.post(
        "/api/v1/integrations/wechat-personal/sessions",
        headers=headers,
    )
    assert started.status_code == 200, started.text

    assert (await client.get(
        "/api/v1/integrations/wechat-personal/sessions/runner-session/status",
        headers=member["headers"],
    )).status_code == 404
    assert (await client.get(
        "/api/v1/integrations/wechat-personal/sessions/runner-session/qr.png",
    )).status_code == 401

    response = await client.post(
        "/api/v1/integrations/wechat-personal/sessions/runner-session/finish",
        headers=headers,
        json={"session_id": "runner-session"},
    )

    assert response.status_code == 201, response.text
    repeat = await client.post(
        "/api/v1/integrations/wechat-personal/sessions/runner-session/finish",
        headers=headers,
        json={"session_id": "runner-session"},
    )
    assert repeat.status_code == 201, repeat.text
    assert repeat.json()["id"] == response.json()["id"]

    integration = (await db_session.execute(
        select(Integration).where(Integration.id == response.json()["id"])
    )).scalar_one()
    assert integration.credentials == {}
    from packages.core.credentials import Requester, get_credential_service
    stored_credentials = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="system", id="wechat-test"),
        reason="wechat_test_assertion",
    )
    assert stored_credentials["bot_token"] == "ilink-bot-token"
    assert stored_credentials["base_url"] == "https://ilink.example"
    restore_path = "/api/v1/integrations/wechat-personal/internal/sessions"
    assert (await client.get(restore_path)).status_code == 401
    restore = await client.get(
        restore_path,
        headers={"Authorization": "Bearer runner-secret"},
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()[0]["session_id"] == "runner-session"
    assert restore.json()[0]["bot_token"] == "ilink-bot-token"
    status = await client.get(
        f"/api/v1/integrations/wechat-personal/{integration.id}/status",
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["online"] is True
    assert (await client.delete(
        "/api/v1/integrations/wechat-personal/sessions/runner-session",
        headers=headers,
    )).status_code == 409
    assert [request[:2] for request in seen] == [
        ("POST", "/sessions"),
        ("GET", "/sessions/runner-session/status"),
        ("GET", "/sessions/runner-session/credentials"),
        ("POST", "/sessions/runner-session/config"),
        ("GET", "/sessions/runner-session/status"),
    ]


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


@pytest.mark.asyncio
async def test_wechat_personal_callback_passes_inbound_receipt_to_worker(
    client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    headers = await _auth(client, "wechat_personal_callback_receipt")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="wechat_personal",
        provider="wechat_personal",
        credentials={
            "runner_url": "https://runner.example",
            "bearer_token": "runner-secret",
            "session_id": "session-1",
        },
        config={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    queued: list[dict] = []
    monkeypatch.setattr(
        dispatch_inbound_task,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    body = json.dumps(
        {
            "from": "peer-1",
            "chat_id": "peer-1",
            "from_name": "Test peer",
            "text": "hello",
            "message_type": "text",
            "msg_id": "wechat-msg-1",
        }
    ).encode()

    response = await client.post(
        f"/api/v1/channels/wechat_personal/callback?config_id={config.id}",
        content=body,
        headers={"Authorization": "Bearer runner-secret"},
    )

    assert response.status_code == 200, response.text
    assert len(queued) == 1
    receipt_id = queued[0].get("inbound_message_log_id")
    assert receipt_id
    receipt = await db_session.get(MessageLog, receipt_id)
    assert receipt is not None
    assert receipt.external_id == "wechat-msg-1"

    duplicate = await client.post(
        f"/api/v1/channels/wechat_personal/callback?config_id={config.id}",
        content=body,
        headers={"Authorization": "Bearer runner-secret"},
    )

    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json() == {
        "ok": True,
        "noop": True,
        "reason": "duplicate_event",
    }
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_wechat_personal_callback_rolls_back_when_worker_queue_fails(
    client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.tasks.channel_tasks import dispatch_inbound_task

    headers = await _auth(client, "wechat_personal_callback_queue_failure")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    config = ChannelConfig(
        entity_id=owner["entity_id"],
        owner_user_id=owner["id"],
        channel_type="wechat_personal",
        provider="wechat_personal",
        credentials={
            "runner_url": "https://runner.example",
            "bearer_token": "runner-secret",
            "session_id": "session-1",
        },
        config={},
        status="active",
    )
    db_session.add(config)
    await db_session.commit()

    def unavailable(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(dispatch_inbound_task, "delay", unavailable)
    body = json.dumps(
        {
            "from": "peer-1",
            "chat_id": "peer-1",
            "text": "retry me",
            "message_type": "text",
            "msg_id": "wechat-msg-queue-failure",
        }
    ).encode()

    response = await client.post(
        f"/api/v1/channels/wechat_personal/callback?config_id={config.id}",
        content=body,
        headers={"Authorization": "Bearer runner-secret"},
    )

    assert response.status_code == 503, response.text
    assert await db_session.scalar(
        select(MessageLog.id).where(
            MessageLog.channel_config_id == config.id,
            MessageLog.external_id == "wechat-msg-queue-failure",
        )
    ) is None


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
