from __future__ import annotations

import pytest


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

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

    async def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    async def delete(self, url, **kwargs):
        return self._record("DELETE", url, **kwargs)

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


@pytest.fixture
def stripe_http(monkeypatch):
    from packages.core.ai.mcp import stripe

    _Client.calls = []
    _Client.response = _Response(200, {"ok": True})
    monkeypatch.setattr(stripe.httpx, "AsyncClient", _Client)
    return stripe


@pytest.mark.asyncio
async def test_non_string_key_is_rejected_without_http(stripe_http):
    result = await stripe_http.call_tool("get_balance", {}, {"key": "sk_test_x"})

    assert result["isError"] is True
    assert "key" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_non_object_arguments_are_rejected_without_http(stripe_http):
    result = await stripe_http.call_tool("get_balance", [], "sk_test_x")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_customers_rejects_fractional_limit_without_http(stripe_http):
    result = await stripe_http.call_tool(
        "list_customers", {"limit": 1.5}, "sk_test_x"
    )

    assert result["isError"] is True
    assert "limit" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_payment_intent_rejects_fractional_amount_without_http(stripe_http):
    result = await stripe_http.call_tool(
        "create_payment_intent", {"amount": 10.5}, "sk_test_x"
    )

    assert result["isError"] is True
    assert "amount" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_resource_ids_are_path_encoded(stripe_http):
    await stripe_http.call_tool(
        "get_customer", {"customer_id": "cus?id#fragment"}, "sk_test_x"
    )

    assert _Client.calls[0]["url"].endswith("/customers/cus%3Fid%23fragment")
