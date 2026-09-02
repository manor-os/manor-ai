"""Unit tests for the QuickBooks Online in-process MCP server."""

from __future__ import annotations

import json

import pytest

import packages.core.ai.mcp.quickbooks as qb


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"ok": True}
        self.text = json.dumps(self._payload)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    calls: list[dict] = []
    response = _FakeResponse()

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self.response

    async def post(self, url, *, headers=None, params=None, json=None):
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "params": params, "json": json}
        )
        return self.response

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "json": json}
        )
        return self.response


@pytest.fixture
def http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse()
    monkeypatch.setattr(qb.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _last_call() -> dict:
    assert _FakeClient.calls, "expected one HTTP request"
    return _FakeClient.calls[-1]


def test_quickbooks_schema_handler_parity() -> None:
    assert {tool["name"] for tool in qb.list_tools()} == set(qb._HANDLERS)


async def test_quickbooks_blank_token_is_rejected_without_http(http) -> None:
    result = await qb.call_tool("get_company_info", {"realm_id": "123"}, "   ")

    assert result["isError"] is True
    assert "access token" in result["content"][0]["text"].lower()
    assert not http.calls


async def test_quickbooks_blank_required_argument_is_rejected_without_http(http) -> None:
    result = await qb.call_tool("get_company_info", {"realm_id": "   "}, "qb-token")

    assert result["isError"] is True
    assert "realm_id" in result["content"][0]["text"]
    assert not http.calls


async def test_quickbooks_uses_realm_from_oauth_credential_bundle(http) -> None:
    result = await qb.call_tool(
        "get_company_info",
        {},
        json.dumps({"access_token": "qb-token", "realm_id": "realm-123"}),
    )

    assert result["isError"] is False
    assert "/realm-123/companyinfo/realm-123" in _last_call()["url"]
    assert _last_call()["headers"]["Authorization"] == "Bearer qb-token"


@pytest.mark.parametrize("manor_environment", ["dev", "staging", "digitalocean-test"])
def test_quickbooks_defaults_to_sandbox_outside_production(monkeypatch, manor_environment: str) -> None:
    monkeypatch.delenv("QUICKBOOKS_ENVIRONMENT", raising=False)
    monkeypatch.delenv("QBO_ENVIRONMENT", raising=False)
    monkeypatch.setenv("MANOR_ENV", manor_environment)

    assert qb.quickbooks_base_url() == qb._API_SANDBOX


def test_quickbooks_environment_name_takes_precedence_over_legacy_alias(monkeypatch) -> None:
    monkeypatch.setenv("QUICKBOOKS_ENVIRONMENT", "production")
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")

    assert qb.quickbooks_base_url() == qb._API_PROD


def test_quickbooks_rejects_unknown_environment(monkeypatch) -> None:
    monkeypatch.delenv("QUICKBOOKS_ENVIRONMENT", raising=False)
    monkeypatch.setenv("QBO_ENVIRONMENT", "stagin")

    with pytest.raises(ValueError, match="QBO_ENVIRONMENT"):
        qb.quickbooks_base_url()


async def test_quickbooks_encodes_realm_and_resource_path_ids(http, monkeypatch) -> None:
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")
    result = await qb.call_tool(
        "get_customer",
        {"realm_id": "realm/1", "customer_id": "customer/7"},
        "qb-token",
    )

    assert result["isError"] is False
    assert _last_call()["url"] == (
        f"{qb._API_SANDBOX}/realm%2F1/customer/customer%2F7"
    )


@pytest.mark.parametrize("limit", [0, -1, 1001, 1.5, "1.5", True])
async def test_quickbooks_rejects_invalid_query_limits_without_http(http, limit) -> None:
    result = await qb.call_tool(
        "query_customers",
        {"realm_id": "123", "limit": limit},
        "qb-token",
    )

    assert result["isError"] is True
    assert "limit" in result["content"][0]["text"]
    assert not http.calls


async def test_quickbooks_void_invoice_uses_sync_token_and_encoded_id(http, monkeypatch) -> None:
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")
    result = await qb.call_tool(
        "void_invoice",
        {"realm_id": "123", "invoice_id": "invoice/7", "sync_token": "2"},
        "qb-token",
    )

    assert result["isError"] is False
    call = _last_call()
    assert call["method"] == "POST"
    assert call["url"] == f"{qb._API_SANDBOX}/123/invoice/invoice%2F7/void"
    assert call["json"] == {"Id": "invoice/7", "SyncToken": "2"}
    assert call["params"]["minorversion"] == "65"
