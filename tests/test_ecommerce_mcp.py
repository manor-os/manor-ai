"""Unit tests for the e-commerce in-process MCP servers (shopify, woocommerce,
square).

HTTP is faked by swapping each module's ``httpx.AsyncClient`` — assertions are
on auth headers, method/URL and the request body each handler would send (no
network). Credentials are passed as the JSON blob the dispatcher hands to
credentials-type modules. Mirrors tests/test_youtube_tiktok_mcp.py.
"""

from __future__ import annotations

import base64
import json

import pytest

import packages.core.ai.mcp.shopify as sh
import packages.core.ai.mcp.woocommerce as wc
import packages.core.ai.mcp.square as sq


# ── httpx fake ───────────────────────────────────────────────────────────────


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

    def __init__(self, *_a, **kw):
        _FakeClient.init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def request(self, method, url, headers=None, json=None):
        _FakeClient.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return _FakeClient.response

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _FakeClient.response


def _last():
    assert _FakeClient.calls, "no HTTP request made"
    return _FakeClient.calls[-1]


@pytest.fixture
def http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResp(200, {"ok": True})
    for mod in (sh, wc, sq):
        monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


# Credentials blobs (what the dispatcher passes as bearer_token)
SH_CREDS = json.dumps({"shop_domain": "demo.myshopify.com", "access_token": "shpat_x"})
WC_CREDS = json.dumps({"site_url": "https://shop.example.com", "consumer_key": "ck_1", "consumer_secret": "cs_2"})
SQ_CREDS = json.dumps({"access_token": "EAAA_tok", "environment": "sandbox", "location_id": "L1"})


# ── Registration parity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("mod", [sh, wc, sq])
def test_schema_handler_parity(mod):
    names = {t["name"] for t in mod.list_tools()}
    assert names == set(mod._HANDLERS)


# ── Shopify (GraphQL) ─────────────────────────────────────────────────────────


async def test_shopify_uses_graphql_endpoint_and_token_header(http):
    http.response = _FakeResp(200, {"data": {"shop": {"name": "Demo"}}})
    out = await sh.call_tool("get_shop", {}, SH_CREDS)
    call = _last()
    assert call["url"] == f"https://demo.myshopify.com/admin/api/{sh._ADMIN_API_VERSION}/graphql.json"
    assert call["headers"]["X-Shopify-Access-Token"] == "shpat_x"
    assert "query" in call["json"]
    assert out["isError"] is False


async def test_shopify_get_product_normalizes_numeric_id_to_gid(http):
    http.response = _FakeResp(200, {"data": {"product": {"id": "gid://shopify/Product/5"}}})
    await sh.call_tool("get_product", {"product_id": "5"}, SH_CREDS)
    assert _last()["json"]["variables"]["id"] == "gid://shopify/Product/5"


async def test_shopify_passes_through_full_gid(http):
    http.response = _FakeResp(200, {"data": {"product": {}}})
    await sh.call_tool("get_product", {"product_id": "gid://shopify/Product/99"}, SH_CREDS)
    assert _last()["json"]["variables"]["id"] == "gid://shopify/Product/99"


async def test_shopify_create_product_mutation_input(http):
    http.response = _FakeResp(
        200, {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}}}
    )
    await sh.call_tool("create_product", {"title": "Tee", "tags": "a, b", "status": "ACTIVE"}, SH_CREDS)
    body = _last()["json"]
    assert "productCreate" in body["query"]
    assert body["variables"]["input"]["title"] == "Tee"
    assert body["variables"]["input"]["tags"] == ["a", "b"]


async def test_shopify_user_errors_surface_as_error(http):
    http.response = _FakeResp(
        200, {"data": {"productCreate": {"userErrors": [{"field": "title", "message": "blank"}]}}}
    )
    out = await sh.call_tool("create_product", {"title": "x"}, SH_CREDS)
    assert out["isError"] is True
    assert "userErrors" in out["content"][0]["text"]


async def test_shopify_http_error_surfaces_as_error(http):
    """Regression: a non-2xx GraphQL HTTP response must report isError (it
    used to be returned as a success string)."""
    http.response = _FakeResp(500, text="upstream down")
    out = await sh.call_tool("get_shop", {}, SH_CREDS)
    assert out["isError"] is True
    assert "500" in out["content"][0]["text"]


async def test_shopify_missing_credentials(http):
    out = await sh.call_tool("get_shop", {}, json.dumps({"shop_domain": "d.myshopify.com"}))
    assert out["isError"] is True
    assert "access_token" in out["content"][0]["text"]


# ── WooCommerce (REST, basic auth) ────────────────────────────────────────────


async def test_woo_basic_auth_header_and_path(http):
    http.response = _FakeResp(200, [])
    await wc.call_tool("list_products", {"search": "hat"}, WC_CREDS)
    call = _last()
    assert call["method"] == "GET"
    assert call["url"].startswith("https://shop.example.com/wp-json/wc/v3/products")
    expected = "Basic " + base64.b64encode(b"ck_1:cs_2").decode()
    assert call["headers"]["Authorization"] == expected


async def test_woo_create_product_price_coerced_to_string(http):
    http.response = _FakeResp(201, {"id": 7})
    await wc.call_tool("create_product", {"name": "Hat", "regular_price": 19.99, "stock_quantity": 5}, WC_CREDS)
    body = _last()["json"]
    assert body["name"] == "Hat"
    assert body["regular_price"] == "19.99"
    assert body["manage_stock"] is True and body["stock_quantity"] == 5


async def test_woo_set_stock(http):
    http.response = _FakeResp(200, {"id": 7})
    await wc.call_tool("set_stock", {"product_id": 7, "stock_quantity": 42}, WC_CREDS)
    call = _last()
    assert call["method"] == "PUT" and call["url"].endswith("/products/7")
    assert call["json"] == {"manage_stock": True, "stock_quantity": 42}


async def test_woo_set_stock_zero_is_allowed(http):
    """Regression: stock_quantity=0 (mark sold-out) must not be rejected as a
    missing required param, and must actually send 0."""
    http.response = _FakeResp(200, {"id": 7})
    out = await wc.call_tool("set_stock", {"product_id": 7, "stock_quantity": 0}, WC_CREDS)
    assert out["isError"] is False
    call = _last()
    assert call["method"] == "PUT" and call["url"].endswith("/products/7")
    assert call["json"] == {"manage_stock": True, "stock_quantity": 0}


async def test_woo_non_2xx_surfaces_as_error(http):
    """Regression: a non-2xx upstream response must report isError, not a
    success payload the model would mistake for a normal result."""
    http.response = _FakeResp(500, text="boom")
    out = await wc.call_tool("list_products", {}, WC_CREDS)
    assert out["isError"] is True
    assert "500" in out["content"][0]["text"]


async def test_woo_update_order_status(http):
    http.response = _FakeResp(200, {"id": 3})
    await wc.call_tool("update_order_status", {"order_id": 3, "status": "completed"}, WC_CREDS)
    call = _last()
    assert call["method"] == "PUT" and call["url"].endswith("/orders/3")
    assert call["json"] == {"status": "completed"}


async def test_woo_missing_credentials(http):
    out = await wc.call_tool("list_products", {}, json.dumps({"site_url": "https://x.com"}))
    assert out["isError"] is True
    assert "consumer_key" in out["content"][0]["text"]


# ── Square (REST) ─────────────────────────────────────────────────────────────


async def test_square_sandbox_base_and_headers(http):
    http.response = _FakeResp(200, {"locations": []})
    await sq.call_tool("list_locations", {}, SQ_CREDS)
    call = _last()
    assert call["url"] == "https://connect.squareupsandbox.com/v2/locations"
    assert call["headers"]["Authorization"] == "Bearer EAAA_tok"
    assert call["headers"]["Square-Version"] == sq._VERSION


async def test_square_production_base_when_env_omitted(http):
    http.response = _FakeResp(200, {"locations": []})
    await sq.call_tool("list_locations", {}, json.dumps({"access_token": "t"}))
    assert _last()["url"].startswith("https://connect.squareup.com/v2/")


async def test_square_search_orders_uses_default_location(http):
    http.response = _FakeResp(200, {"orders": []})
    await sq.call_tool("search_orders", {"state": "OPEN"}, SQ_CREDS)
    call = _last()
    assert call["url"].endswith("/v2/orders/search")
    assert call["json"]["location_ids"] == ["L1"]
    assert call["json"]["query"]["filter"]["state_filter"]["states"] == ["OPEN"]


async def test_square_non_2xx_surfaces_as_error(http):
    """Regression: a non-2xx upstream response must report isError."""
    http.response = _FakeResp(403, text='{"errors":[{"detail":"forbidden"}]}')
    out = await sq.call_tool("list_locations", {}, SQ_CREDS)
    assert out["isError"] is True
    assert "403" in out["content"][0]["text"]


async def test_square_create_catalog_item_shape(http):
    http.response = _FakeResp(200, {"catalog_object": {"id": "X"}})
    await sq.call_tool("create_catalog_item", {"name": "Mug", "price_amount": 1200}, SQ_CREDS)
    body = _last()["json"]
    assert body["idempotency_key"]  # generated
    item = body["object"]
    assert item["type"] == "ITEM" and item["item_data"]["name"] == "Mug"
    var = item["item_data"]["variations"][0]["item_variation_data"]
    assert var["price_money"] == {"amount": 1200, "currency": "USD"}


async def test_square_adjust_inventory_requires_location(http):
    out = await sq.call_tool(
        "adjust_inventory",
        {"catalog_object_id": "V1", "quantity": 3},
        json.dumps({"access_token": "t"}),  # no location_id in creds
    )
    assert "location_id" in out["content"][0]["text"]
    assert not http.calls


async def test_square_adjust_inventory_change_body(http):
    http.response = _FakeResp(200, {"counts": []})
    await sq.call_tool("adjust_inventory", {"catalog_object_id": "V1", "quantity": 3}, SQ_CREDS)
    body = _last()["json"]
    assert body["idempotency_key"]
    change = body["changes"][0]
    assert change["type"] == "ADJUSTMENT"
    assert change["adjustment"]["catalog_object_id"] == "V1"
    assert change["adjustment"]["location_id"] == "L1"
    assert change["adjustment"]["quantity"] == "3"


# ── Shared guards ────────────────────────────────────────────────────────────


async def test_malformed_credentials(http):
    out = await wc.call_tool("list_products", {}, "not-json")
    assert out["isError"] is True and "malformed" in out["content"][0]["text"]


async def test_missing_required_param(http):
    out = await sq.call_tool("get_order", {}, SQ_CREDS)
    assert out["isError"] is True and "Missing required" in out["content"][0]["text"]
    assert not http.calls
