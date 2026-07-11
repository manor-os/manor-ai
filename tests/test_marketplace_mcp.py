"""Unit tests for the marketplace-seller MCP servers (tiktok_shop, amazon).

HTTP is faked by swapping each module's ``httpx.AsyncClient``. Assertions cover
the signed-request shape (TikTok Shop), the LWA token exchange + region routing
(Amazon), and method/URL/body for the tools. Credentials are passed as the JSON
blob the dispatcher hands to credentials-type modules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import pytest

import packages.core.ai.mcp.tiktok_shop as ts
import packages.core.ai.mcp.amazon as az


# ── flexible httpx fake ───────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status=200, json_body=None, text=None):
        self.status_code = status
        self._json = json_body
        if text is not None:
            self.text = text
        elif json_body is not None:
            self.text = json.dumps(json_body)
        else:
            self.text = ""

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    calls: list = []
    response = _FakeResp(200, {"ok": True})
    route = None  # optional callable(url) -> _FakeResp

    def __init__(self, *_a, **kw):
        _FakeClient.init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def _resp(self, url):
        if _FakeClient.route:
            r = _FakeClient.route(url)
            if r is not None:
                return r
        return _FakeClient.response

    async def request(self, method, url, **kw):
        _FakeClient.calls.append({"method": method, "url": url, **kw})
        return self._resp(url)

    async def post(self, url, **kw):
        _FakeClient.calls.append({"method": "POST", "url": url, **kw})
        return self._resp(url)


def _last():
    assert _FakeClient.calls, "no HTTP request made"
    return _FakeClient.calls[-1]


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.fixture
def http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResp(200, {"ok": True})
    _FakeClient.route = None
    monkeypatch.setattr(ts.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(az.httpx, "AsyncClient", _FakeClient)
    az._token_cache.clear()
    return _FakeClient


TS_CREDS = json.dumps(
    {
        "app_key": "ak",
        "app_secret": "s3cr3t",
        "access_token": "tok",
        "shop_cipher": "CIPH",
    }
)


# ── Registration parity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mod", [ts, az])
def test_schema_handler_parity(mod):
    assert {t["name"] for t in mod.list_tools()} == set(mod._HANDLERS)


# ── TikTok Shop: signing ──────────────────────────────────────────────────────


async def test_ts_shops_not_shop_scoped_and_signed(http):
    http.response = _FakeResp(200, {"data": {"shops": []}})
    await ts.call_tool("get_authorized_shops", {}, TS_CREDS)
    call = _last()
    q = _query(call["url"])
    assert "/authorization/202309/shops" in call["url"]
    assert "shop_cipher" not in q  # not shop-scoped
    assert q["app_key"] == "ak" and "timestamp" in q and "sign" in q
    assert call["headers"]["x-tts-access-token"] == "tok"


async def test_ts_search_orders_signs_body_and_sends_as_content(http):
    http.response = _FakeResp(200, {"data": {"orders": []}})
    await ts.call_tool("search_orders", {"order_status": "COMPLETED", "page_size": 50}, TS_CREDS)
    call = _last()
    q = _query(call["url"])
    assert call["method"] == "POST"
    assert "/order/202309/orders/search" in call["url"]
    assert q["shop_cipher"] == "CIPH" and q["page_size"] == "50"
    # body is sent as raw content (so the signed bytes == sent bytes)
    body_str = call["content"].decode()
    assert json.loads(body_str) == {"order_status": "COMPLETED"}
    # recompute the signature over the exact query+body and compare
    expect = ts._sign("s3cr3t", "/order/202309/orders/search", {k: v for k, v in q.items() if k != "sign"}, body_str)
    assert q["sign"] == expect


async def test_ts_shop_scoped_requires_cipher(http):
    creds = json.dumps({"app_key": "ak", "app_secret": "s", "access_token": "t"})  # no shop_cipher
    out = await ts.call_tool("get_product", {"product_id": "p1"}, creds)
    assert "shop_cipher" in out["content"][0]["text"]
    assert not http.calls


async def test_ts_update_price_body(http):
    http.response = _FakeResp(200, {"data": {}})
    skus = [{"id": "S1", "price": {"amount": "9.99", "currency": "USD"}}]
    await ts.call_tool("update_price", {"product_id": "p1", "skus": skus}, TS_CREDS)
    call = _last()
    assert "/product/202309/products/p1/prices/update" in call["url"]
    assert json.loads(call["content"].decode()) == {"skus": skus}


async def test_ts_missing_credentials(http):
    out = await ts.call_tool("get_authorized_shops", {}, json.dumps({"app_key": "ak"}))
    assert out["isError"] is True and "app_secret" in out["content"][0]["text"]


# ── Amazon: token exchange + routing ──────────────────────────────────────────

AZ_TOKEN_CREDS = json.dumps(
    {
        "refresh_token": "Atzr|RT",
        "lwa_client_id": "cid",
        "lwa_client_secret": "csec",
        "region": "na",
        "marketplace_id": "ATVPDKIKX0DER",
        "seller_id": "SELLER1",
    }
)
AZ_DIRECT_CREDS = json.dumps(
    {
        "access_token": "Atza|AT",
        "region": "eu",
        "marketplace_id": "A1PA6795UKMFR9",
        "seller_id": "SELLER1",
    }
)


async def test_az_direct_access_token_skips_exchange_and_routes_region(http):
    http.response = _FakeResp(200, {"orders": []})
    await az.call_tool("get_orders", {}, AZ_DIRECT_CREDS)
    call = _last()
    assert call["url"].startswith("https://sellingpartnerapi-eu.amazon.com/orders/v0/orders")
    assert _query(call["url"])["MarketplaceIds"] == "A1PA6795UKMFR9"
    assert call["headers"]["x-amz-access-token"] == "Atza|AT"
    # only one HTTP call — no LWA exchange
    assert len(http.calls) == 1


async def test_az_lwa_exchange_then_api_call(http):
    def route(url):
        if "api.amazon.com/auth/o2/token" in url:
            return _FakeResp(200, {"access_token": "Atza|FRESH", "expires_in": 3600})
        return _FakeResp(200, {"payload": {"Orders": []}})

    http.route = route
    await az.call_tool("get_orders", {}, AZ_TOKEN_CREDS)
    assert len(http.calls) == 2
    token_call, api_call = http.calls[0], http.calls[1]
    assert token_call["url"] == az._LWA_TOKEN_URL
    assert token_call["data"]["grant_type"] == "refresh_token"
    assert token_call["data"]["refresh_token"] == "Atzr|RT"
    assert api_call["headers"]["x-amz-access-token"] == "Atza|FRESH"
    assert api_call["url"].startswith("https://sellingpartnerapi-na.amazon.com/")


async def test_az_token_is_cached_across_calls(http):
    calls = {"n": 0}

    def route(url):
        if "api.amazon.com/auth/o2/token" in url:
            calls["n"] += 1
            return _FakeResp(200, {"access_token": "Atza|FRESH", "expires_in": 3600})
        return _FakeResp(200, {"ok": True})

    http.route = route
    await az.call_tool("get_orders", {}, AZ_TOKEN_CREDS)
    await az.call_tool("get_order", {"order_id": "111-222-333"}, AZ_TOKEN_CREDS)
    assert calls["n"] == 1  # exchanged once, reused from cache


async def test_az_get_orders_requires_marketplace(http):
    out = await az.call_tool("get_orders", {}, json.dumps({"access_token": "t", "region": "na"}))
    assert "marketplace_id" in out["content"][0]["text"]
    assert not http.calls


async def test_az_get_orders_defaults_created_after(http):
    """SP-API getOrders 400s without CreatedAfter/LastUpdatedAfter; a bare
    call must supply a default CreatedAfter (last 30 days)."""
    http.response = _FakeResp(200, {"orders": []})
    await az.call_tool("get_orders", {}, AZ_DIRECT_CREDS)
    q = _query(_last()["url"])
    assert "CreatedAfter" in q and q["CreatedAfter"].endswith("Z")


async def test_az_get_orders_honours_explicit_created_after(http):
    http.response = _FakeResp(200, {"orders": []})
    await az.call_tool("get_orders", {"created_after": "2024-01-01T00:00:00Z"}, AZ_DIRECT_CREDS)
    assert _query(_last()["url"])["CreatedAfter"] == "2024-01-01T00:00:00Z"


async def test_az_patch_listing_body(http):
    http.response = _FakeResp(200, {"status": "ACCEPTED"})
    patches = [
        {
            "op": "replace",
            "path": "/attributes/fulfillment_availability",
            "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 7}],
        }
    ]
    await az.call_tool(
        "patch_listing",
        {
            "sku": "SKU1",
            "product_type": "SHIRT",
            "patches": patches,
        },
        AZ_DIRECT_CREDS,
    )
    call = _last()
    assert call["method"] == "PATCH"
    assert "/listings/2021-08-01/items/SELLER1/SKU1" in call["url"]
    assert call["json"] == {"productType": "SHIRT", "patches": patches}


async def test_az_put_listing_attributes_json_string(http):
    http.response = _FakeResp(200, {"status": "ACCEPTED"})
    await az.call_tool(
        "put_listing",
        {
            "sku": "SKU2",
            "product_type": "LUGGAGE",
            "attributes": '{"item_name":[{"value":"Bag"}]}',
        },
        AZ_DIRECT_CREDS,
    )
    call = _last()
    assert call["method"] == "PUT"
    assert call["json"]["productType"] == "LUGGAGE"
    assert call["json"]["attributes"] == {"item_name": [{"value": "Bag"}]}


async def test_az_missing_credentials(http):
    out = await az.call_tool("get_orders", {}, json.dumps({"region": "na"}))
    assert out["isError"] is True
    assert "refresh_token" in out["content"][0]["text"]


# ── signature unit check ──────────────────────────────────────────────────────


def test_ts_sign_matches_reference():
    q = {"app_key": "ak", "timestamp": "100", "shop_cipher": "C", "page_size": 20}
    body = '{"x":1}'
    keys = sorted(k for k in q if k not in ("sign", "access_token"))
    base = "sec" + "/p" + "".join(f"{k}{q[k]}" for k in keys) + body + "sec"
    want = hmac.new(b"sec", base.encode(), hashlib.sha256).hexdigest()
    assert ts._sign("sec", "/p", q, body) == want
