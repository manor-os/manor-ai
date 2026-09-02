from __future__ import annotations

import pytest


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": []}
        self.text = text

    @property
    def is_success(self) -> bool:
        return True

    def json(self) -> dict:
        return self._payload


class _Client:
    calls: list[dict] = []
    response = _Response()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "params": params,
            }
        )
        return self.response


@pytest.fixture
def gmail_http(monkeypatch):
    from packages.core.ai.mcp import gmail

    _Client.calls = []
    _Client.response = _Response()
    monkeypatch.setattr(gmail.httpx, "AsyncClient", _Client)
    return gmail


@pytest.mark.asyncio
async def test_missing_token_is_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", {}, "")

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_non_string_token_is_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", {}, {"token": "gmail-test-token"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_non_object_arguments_are_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", [], "gmail-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_rejects_non_positive_limit_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 0},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_rejects_fractional_limit_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 1.5},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_send_message_rejects_blank_body_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "send_message",
        {"to": "qa@example.com", "subject": "Test", "body": "   "},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "body" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_caps_limit_and_uses_google_bearer(gmail_http):
    _Client.response = _Response(payload={"messages": []})
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 999},
        "gmail-test-token",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["params"]["maxResults"] == 100
    assert call["headers"]["Authorization"] == "Bearer gmail-test-token"


@pytest.mark.asyncio
async def test_gmail_401_returns_reconnect_error_without_leaking_response(gmail_http):
    _Client.response = _Response(
        status_code=401,
        text='{"error":"invalid_grant"}',
    )
    result = await gmail_http.call_tool("get_profile", {}, "gmail-test-token")

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "reconnect" in message.lower()
    assert "invalid_grant" not in message


@pytest.mark.asyncio
async def test_message_id_is_path_encoded(gmail_http):
    await gmail_http.call_tool(
        "get_message",
        {"message_id": "msg?id#fragment"},
        "gmail-test-token",
    )

    call = _Client.calls[0]
    assert call["url"].endswith("/users/me/messages/msg%3Fid%23fragment")
