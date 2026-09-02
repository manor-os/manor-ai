"""Generic outbound webhook MCP contract tests."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: object | None = None):
        self.status_code = status_code
        self.text = json.dumps(body if body is not None else {})


class _FakeClient:
    calls: list[dict] = []
    response = _FakeResponse()
    error: Exception | None = None

    def __init__(self, *_args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "timeout": self.timeout, **kwargs})
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def http(monkeypatch):
    from packages.core.ai.mcp import webhook

    async def allow_public_endpoint(url: str):
        parsed = urlsplit(url)
        return SimpleNamespace(
            url=url,
            hostname=parsed.hostname or "hooks.example.test",
            port=parsed.port or 443,
            addresses=("93.184.216.34",),
        )

    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(200, {"accepted": True})
    _FakeClient.error = None
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        webhook,
        "_resolve_public_endpoint",
        allow_public_endpoint,
        raising=False,
    )
    return _FakeClient


def test_webhook_schema_handler_parity():
    from packages.core.ai.mcp import webhook

    assert {tool["name"] for tool in webhook.list_tools()} == set(webhook._HANDLERS)


@pytest.mark.asyncio
async def test_send_uses_configured_auth_and_returns_idempotency_key(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"payload": {"event": "build.done"}, "idempotency_key": "idem-1"},
        json.dumps(
            {
                "url": "https://hooks.example.test/manor",
                "bearer_token": "bearer-secret",
                "secret": "signing-secret",
                "headers": {"X-Configured": "yes"},
            }
        ),
    )

    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])
    assert data == {"status_code": 200, "delivered": True, "idempotency_key": "idem-1"}
    call = http.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://hooks.example.test/manor"
    assert call["json"] == {"event": "build.done"}
    assert call["headers"]["Authorization"] == "Bearer bearer-secret"
    assert call["headers"]["X-Webhook-Secret"] == "signing-secret"
    assert call["headers"]["X-Configured"] == "yes"
    assert call["headers"]["Idempotency-Key"] == "idem-1"


@pytest.mark.asyncio
async def test_send_supports_public_nonstandard_https_port(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"url": "https://hooks.example.test:8443/manor", "payload": {"ok": True}},
        json.dumps({}),
    )

    assert result["isError"] is False
    assert http.calls[-1]["url"] == "https://hooks.example.test:8443/manor"


@pytest.mark.asyncio
async def test_send_requires_endpoint_and_payload_without_http(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool("send", {}, json.dumps({}))

    assert result["isError"] is True
    assert "payload" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_5xx_is_retryable_without_response_body_or_secret(http):
    from packages.core.ai.mcp import webhook

    http.response = _FakeResponse(503, {"echo": "bearer-secret"})
    result = await webhook.call_tool(
        "send",
        {"url": "https://hooks.example.test", "payload": {"ok": True}},
        json.dumps({
            "url": "https://hooks.example.test",
            "bearer_token": "bearer-secret",
        }),
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "503" in text
    assert "retryable" in text
    assert "bearer-secret" not in text


@pytest.mark.asyncio
async def test_send_rejects_credentialed_endpoint_override_before_http(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"url": "https://attacker.example.test", "payload": {"ok": True}},
        json.dumps({
            "url": "https://hooks.example.test",
            "bearer_token": "bearer-secret",
        }),
    )

    assert result["isError"] is True
    assert "configured" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/manor",
        "http://169.254.169.254/latest/meta-data",
        "https://169.254.169.254/latest/meta-data",
        "https://hooks.example.test:0/manor",
    ],
)
async def test_send_rejects_non_public_https_endpoint_before_http(http, url: str):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"url": url, "payload": {}},
        json.dumps({}),
    )

    assert result["isError"] is True
    assert "public" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_private_dns_resolution_before_http(monkeypatch):
    from packages.core.ai.mcp import webhook
    from packages.core.services import dashboard_http

    async def resolve_private_address(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 443))]

    _FakeClient.calls = []
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", resolve_private_address)

    result = await webhook.call_tool(
        "send",
        {"url": "https://hooks.example.test/manor", "payload": {}},
        json.dumps({}),
    )

    assert result["isError"] is True
    assert "private" in result["content"][0]["text"].lower()
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_send_rejects_invalid_timeout_and_url_before_http(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"url": "ftp://hooks.example.test", "payload": {}, "timeout_seconds": 60},
        json.dumps({}),
    )

    assert result["isError"] is True
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_non_object_arguments_before_http(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool("send", [], json.dumps({}))

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_non_string_idempotency_key_before_http(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"payload": {"event": "build.done"}, "idempotency_key": 123},
        json.dumps({"url": "https://hooks.example.test"}),
    )

    assert result["isError"] is True
    assert "idempotency_key" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_header_injection_in_idempotency_key(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {
            "payload": {"event": "build.done"},
            "idempotency_key": "safe\r\nHost: internal",
        },
        json.dumps({"url": "https://hooks.example.test"}),
    )

    assert result["isError"] is True
    assert "idempotency_key" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_non_object_empty_headers(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {
            "url": "https://hooks.example.test",
            "payload": {"event": "build.done"},
            "headers": [],
        },
        json.dumps({}),
    )

    assert result["isError"] is True
    assert "headers must be objects" in result["content"][0]["text"]
    assert http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer attacker"},
        {"X-WEBHOOK-SECRET": "attacker"},
        {"Host": "127.0.0.1"},
        {"Content-Length": "0"},
        {"Idempotency-Key": "attacker"},
    ],
)
async def test_send_rejects_managed_or_transport_headers_before_http(http, headers):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {"payload": {"ok": True}, "headers": headers},
        json.dumps({
            "url": "https://hooks.example.test",
            "bearer_token": "configured-secret",
        }),
    )

    assert result["isError"] is True
    assert "header" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_send_rejects_case_insensitive_configured_header_override(http):
    from packages.core.ai.mcp import webhook

    result = await webhook.call_tool(
        "send",
        {
            "payload": {"ok": True},
            "headers": {"x-configured": "attacker"},
        },
        json.dumps({
            "url": "https://hooks.example.test",
            "headers": {"X-Configured": "trusted"},
        }),
    )

    assert result["isError"] is True
    assert "configured" in result["content"][0]["text"].lower()
    assert http.calls == []


@pytest.mark.asyncio
async def test_pinned_webhook_backend_connects_to_validated_address_only() -> None:
    from packages.core.services.dashboard_http import (
        PublicHttpsEndpoint,
        _PinnedPublicNetworkBackend,
    )

    calls: list[tuple[str, int]] = []
    stream = object()

    class FakeBackend:
        async def connect_tcp(self, host, port, **_kwargs):
            calls.append((host, port))
            return stream

        async def connect_unix_socket(self, *_args, **_kwargs):
            raise AssertionError("public HTTP must not use a Unix socket")

        async def sleep(self, _seconds):
            return None

    endpoint = PublicHttpsEndpoint(
        url="https://hooks.example.test/manor",
        hostname="hooks.example.test",
        port=443,
        addresses=("93.184.216.34",),
    )
    backend = _PinnedPublicNetworkBackend(endpoint, backend=FakeBackend())

    result = await backend.connect_tcp("hooks.example.test", 443)

    assert result is stream
    assert calls == [("93.184.216.34", 443)]
