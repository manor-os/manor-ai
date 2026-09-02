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
import packages.core.ai.mcp.stripe as st


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

    async def get(self, url, headers=None, params=None, auth=None):
        _FakeClient.calls.append({"method": "GET", "url": url, "headers": headers, "params": params, "auth": auth})
        return _FakeClient.response

    async def delete(self, url, headers=None, auth=None):
        _FakeClient.calls.append({"method": "DELETE", "url": url, "headers": headers, "auth": auth})
        return _FakeClient.response


def _last():
    assert _FakeClient.calls, "no HTTP request made"
    return _FakeClient.calls[-1]


@pytest.fixture
def http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResp(200, {"ok": True})
    for mod in (sh, wc, sq, st):
        monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


# Credentials blobs (what the dispatcher passes as bearer_token)
SH_CREDS = json.dumps({"shop_domain": "demo.myshopify.com", "access_token": "shpat_x"})
WC_CREDS = json.dumps({"site_url": "https://shop.example.com", "consumer_key": "ck_1", "consumer_secret": "cs_2"})
SQ_CREDS = json.dumps({"access_token": "EAAA_tok", "environment": "sandbox", "location_id": "L1"})
STRIPE_KEY = "sk_test_x"


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


async def test_shopify_normalizes_admin_url_credentials(http):
    """A copied Shopify admin URL must resolve to the API host."""
    http.response = _FakeResp(200, {"data": {"shop": {"name": "Demo"}}})
    creds = json.dumps(
        {"shop_domain": " https://Demo.myshopify.com/admin/ ", "access_token": "shpat_x"}
    )
    await sh.call_tool("get_shop", {}, creds)
    assert _last()["url"] == f"https://demo.myshopify.com/admin/api/{sh._ADMIN_API_VERSION}/graphql.json"


async def test_shopify_non_object_credentials_are_rejected_without_http(http):
    out = await sh.call_tool("get_shop", {}, "[]")
    assert out["isError"] is True
    assert "malformed" in out["content"][0]["text"]
    assert not http.calls


async def test_shopify_non_string_token_is_rejected_without_http(http):
    out = await sh.call_tool(
        "get_shop", {}, json.dumps({"shop_domain": "demo.myshopify.com", "access_token": {"value": "bad"}})
    )
    assert out["isError"] is True
    assert "access_token" in out["content"][0]["text"]
    assert not http.calls


async def test_shopify_non_object_arguments_are_rejected_without_http(http):
    out = await sh.call_tool("get_shop", [], SH_CREDS)
    assert out["isError"] is True
    assert "object" in out["content"][0]["text"].lower()
    assert not http.calls


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


async def test_shopify_create_product_defaults_to_draft(http):
    http.response = _FakeResp(200, {"data": {"productCreate": {"userErrors": []}}})

    await sh.call_tool("create_product", {"title": "Staging product"}, SH_CREDS)

    assert _last()["json"]["variables"]["input"]["status"] == "DRAFT"


async def test_shopify_delete_product_is_exposed_and_uses_product_gid(http):
    http.response = _FakeResp(
        200,
        {"data": {"productDelete": {"deletedProductId": "gid://shopify/Product/7", "userErrors": []}}},
    )

    result = await sh.call_tool("delete_product", {"product_id": "7"}, SH_CREDS)

    assert result["isError"] is False
    call = _last()
    assert "productDelete" in call["json"]["query"]
    assert call["json"]["variables"]["input"] == {
        "id": "gid://shopify/Product/7"
    }


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


async def test_shopify_blank_token_is_rejected_without_http(http):
    out = await sh.call_tool(
        "get_shop",
        {},
        json.dumps({"shop_domain": "demo.myshopify.com", "access_token": "   "}),
    )
    assert out["isError"] is True
    assert "access_token" in out["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize("first", [0, -1, 251])
async def test_shopify_list_products_rejects_invalid_first_without_http(http, first):
    out = await sh.call_tool("list_products", {"first": first}, SH_CREDS)
    assert out["isError"] is True
    assert "first" in out["content"][0]["text"]
    assert not http.calls


async def test_shopify_blank_title_is_rejected_without_http(http):
    out = await sh.call_tool("create_product", {"title": "   "}, SH_CREDS)
    assert out["isError"] is True
    assert "title" in out["content"][0]["text"]
    assert not http.calls


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


async def test_woo_create_product_defaults_to_draft(http):
    http.response = _FakeResp(201, {"id": 7})

    await wc.call_tool("create_product", {"name": "Staging product"}, WC_CREDS)

    assert _last()["json"]["status"] == "draft"


async def test_woo_delete_product_defaults_to_recoverable_trash(http):
    http.response = _FakeResp(200, {"id": 7, "status": "trash"})

    result = await wc.call_tool("delete_product", {"product_id": 7}, WC_CREDS)

    assert result["isError"] is False
    call = _last()
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/products/7?force=false")


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


async def test_woo_non_object_credentials_are_rejected_without_http(http):
    out = await wc.call_tool("list_products", {}, "[]")
    assert out["isError"] is True
    assert "malformed" in out["content"][0]["text"]
    assert not http.calls


async def test_woo_non_string_key_is_rejected_without_http(http):
    out = await wc.call_tool(
        "list_products",
        {},
        json.dumps({"site_url": "https://shop.example.com", "consumer_key": ["bad"], "consumer_secret": "cs_2"}),
    )
    assert out["isError"] is True
    assert "consumer_key" in out["content"][0]["text"]
    assert not http.calls


async def test_woo_non_object_arguments_are_rejected_without_http(http):
    out = await wc.call_tool("list_products", [], WC_CREDS)
    assert out["isError"] is True
    assert "object" in out["content"][0]["text"].lower()
    assert not http.calls


async def test_woo_blank_credentials_are_rejected_without_http(http):
    out = await wc.call_tool(
        "list_products",
        {},
        json.dumps({"site_url": "https://shop.example.com", "consumer_key": "ck_1", "consumer_secret": "  "}),
    )
    assert out["isError"] is True
    assert "consumer_secret" in out["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize("per_page", [0, -1, 101])
async def test_woo_list_products_rejects_invalid_per_page_without_http(http, per_page):
    out = await wc.call_tool("list_products", {"per_page": per_page}, WC_CREDS)
    assert out["isError"] is True
    assert "per_page" in out["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize("page", [0, -1])
async def test_woo_list_products_rejects_invalid_page_without_http(http, page):
    out = await wc.call_tool("list_products", {"page": page}, WC_CREDS)
    assert out["isError"] is True
    assert "page" in out["content"][0]["text"]
    assert not http.calls


async def test_woo_blank_product_name_is_rejected_without_http(http):
    out = await wc.call_tool("create_product", {"name": "   "}, WC_CREDS)
    assert out["isError"] is True
    assert "name" in out["content"][0]["text"]
    assert not http.calls


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


async def test_square_unknown_environment_is_rejected_without_http(http):
    out = await sq.call_tool(
        "list_locations",
        {},
        json.dumps({"access_token": "t", "environment": "staging"}),
    )
    assert out["isError"] is True
    assert "environment" in out["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize("tool_name", ["search_catalog_items", "search_orders"])
@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_square_search_limit_is_bounded_without_http(http, tool_name, limit):
    args = {"limit": limit}
    if tool_name == "search_orders":
        args["location_ids"] = ["L1"]
    out = await sq.call_tool(tool_name, args, SQ_CREDS)
    assert out["isError"] is True
    assert "limit" in out["content"][0]["text"]
    assert not http.calls


async def test_square_search_orders_uses_default_location(http):
    http.response = _FakeResp(200, {"orders": []})
    await sq.call_tool("search_orders", {"state": "OPEN"}, SQ_CREDS)
    call = _last()
    assert call["url"].endswith("/v2/orders/search")
    assert call["json"]["location_ids"] == ["L1"]
    assert call["json"]["query"]["filter"]["state_filter"]["states"] == ["OPEN"]


async def test_square_search_orders_requires_location(http):
    result = await sq.call_tool(
        "search_orders",
        {},
        json.dumps({"access_token": "t", "environment": "sandbox"}),
    )

    assert result["isError"] is True
    assert "location_ids" in result["content"][0]["text"]
    assert not http.calls


async def test_square_non_2xx_surfaces_as_error(http):
    """Regression: a non-2xx upstream response must report isError."""
    http.response = _FakeResp(403, text='{"errors":[{"detail":"forbidden"}]}')
    out = await sq.call_tool("list_locations", {}, SQ_CREDS)
    assert out["isError"] is True
    assert "403" in out["content"][0]["text"]


async def test_square_create_catalog_item_shape(http):
    http.response = _FakeResp(200, {"catalog_object": {"id": "X"}})
    await sq.call_tool(
        "create_catalog_item",
        {"name": "Mug", "price_amount": 1200, "idempotency_key": "catalog-create-1"},
        SQ_CREDS,
    )
    body = _last()["json"]
    assert body["idempotency_key"] == "catalog-create-1"
    item = body["object"]
    assert item["type"] == "ITEM" and item["item_data"]["name"] == "Mug"
    var = item["item_data"]["variations"][0]["item_variation_data"]
    assert var["price_money"] == {"amount": 1200, "currency": "USD"}


async def test_square_adjust_inventory_requires_location(http):
    out = await sq.call_tool(
        "adjust_inventory",
        {
            "catalog_object_id": "V1",
            "quantity": 3,
            "idempotency_key": "inventory-location-check",
        },
        json.dumps({"access_token": "t"}),  # no location_id in creds
    )
    assert out["isError"] is True
    assert "location_id" in out["content"][0]["text"]
    assert not http.calls


async def test_square_adjust_inventory_change_body(http):
    http.response = _FakeResp(200, {"counts": []})
    await sq.call_tool(
        "adjust_inventory",
        {
            "catalog_object_id": "V1",
            "quantity": 3,
            "idempotency_key": "inventory-adjust-1",
        },
        SQ_CREDS,
    )
    body = _last()["json"]
    assert body["idempotency_key"] == "inventory-adjust-1"
    change = body["changes"][0]
    assert change["type"] == "ADJUSTMENT"
    assert change["adjustment"]["catalog_object_id"] == "V1"
    assert change["adjustment"]["location_id"] == "L1"
    assert change["adjustment"]["quantity"] == "3"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("create_catalog_item", {"name": "Mug", "price_amount": 1200}),
        ("create_customer", {"given_name": "Ada"}),
        ("adjust_inventory", {"catalog_object_id": "V1", "quantity": 3}),
    ],
)
async def test_square_mutations_require_caller_idempotency_key(http, tool, args):
    result = await sq.call_tool(tool, args, SQ_CREDS)

    assert result["isError"] is True
    assert "idempotency_key" in result["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_catalog_item", {"name": "Mug", "price_amount": 1200}),
        ("adjust_inventory", {"catalog_object_id": "V1", "quantity": 3}),
    ],
)
async def test_square_catalog_and_inventory_accept_128_char_idempotency_key(
    http,
    tool,
    args,
):
    http.response = _FakeResp(200, {"ok": True})
    key = "x" * 128

    result = await sq.call_tool(tool, {**args, "idempotency_key": key}, SQ_CREDS)

    assert result["isError"] is False
    assert _last()["json"]["idempotency_key"] == key


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("create_catalog_item", {"name": "Mug", "price_amount": 1200}),
        ("adjust_inventory", {"catalog_object_id": "V1", "quantity": 3}),
    ],
)
async def test_square_catalog_and_inventory_reject_129_char_idempotency_key(
    http,
    tool,
    args,
):
    result = await sq.call_tool(
        tool,
        {**args, "idempotency_key": "x" * 129},
        SQ_CREDS,
    )

    assert result["isError"] is True
    assert "128" in result["content"][0]["text"]
    assert not http.calls


async def test_square_customer_does_not_apply_unpublished_45_char_limit(http):
    http.response = _FakeResp(200, {"customer": {"id": "C1"}})
    key = "x" * 46

    result = await sq.call_tool(
        "create_customer",
        {"given_name": "Ada", "idempotency_key": key},
        SQ_CREDS,
    )

    assert result["isError"] is False
    assert _last()["json"]["idempotency_key"] == key


async def test_square_forwards_caller_idempotency_key_unchanged(http):
    http.response = _FakeResp(200, {"catalog_object": {"id": "X"}})
    key = "  catalog-retry-key  "

    result = await sq.call_tool(
        "create_catalog_item",
        {"name": "Mug", "price_amount": 1200, "idempotency_key": key},
        SQ_CREDS,
    )

    assert result["isError"] is False
    assert _last()["json"]["idempotency_key"] == key


async def test_square_delete_catalog_object_is_exposed(http):
    http.response = _FakeResp(200, {"deleted_object_ids": ["ITEM1"]})

    result = await sq.call_tool(
        "delete_catalog_object",
        {"object_id": "ITEM1"},
        SQ_CREDS,
    )

    assert result["isError"] is False
    call = _last()
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/v2/catalog/object/ITEM1")


async def test_square_blank_token_is_rejected_without_http(http):
    out = await sq.call_tool("list_locations", {}, json.dumps({"access_token": "  "}))
    assert out["isError"] is True
    assert "access_token" in out["content"][0]["text"]
    assert not http.calls


async def test_square_non_object_credentials_are_rejected_without_http(http):
    out = await sq.call_tool("list_locations", {}, "[]")
    assert out["isError"] is True
    assert "malformed" in out["content"][0]["text"]
    assert not http.calls


async def test_square_non_string_token_is_rejected_without_http(http):
    out = await sq.call_tool(
        "list_locations", {}, json.dumps({"access_token": {"value": "bad"}, "environment": "sandbox"})
    )
    assert out["isError"] is True
    assert "access_token" in out["content"][0]["text"]
    assert not http.calls


async def test_square_non_object_arguments_are_rejected_without_http(http):
    out = await sq.call_tool("list_locations", [], SQ_CREDS)
    assert out["isError"] is True
    assert "object" in out["content"][0]["text"].lower()
    assert not http.calls


async def test_square_blank_required_name_is_rejected_without_http(http):
    out = await sq.call_tool("create_catalog_item", {"name": "   ", "price_amount": 100}, SQ_CREDS)
    assert out["isError"] is True
    assert "name" in out["content"][0]["text"]
    assert not http.calls


# ── Stripe (REST, Basic auth) ────────────────────────────────────────────────


async def test_stripe_blank_key_is_rejected_without_http(http):
    out = await st.call_tool("get_balance", {}, "   ")
    assert out["isError"] is True
    assert "key" in out["content"][0]["text"]
    assert not http.calls


@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_stripe_list_customers_rejects_invalid_limit_without_http(http, limit):
    out = await st.call_tool("list_customers", {"limit": limit}, STRIPE_KEY)
    assert out["isError"] is True
    assert "limit" in out["content"][0]["text"]
    assert not http.calls


async def test_stripe_non_2xx_surfaces_as_error(http):
    http.response = _FakeResp(500, text="upstream down")
    out = await st.call_tool("get_balance", {}, STRIPE_KEY)
    assert out["isError"] is True
    assert "500" in out["content"][0]["text"]


async def test_stripe_json_error_preserves_provider_message(http):
    http.response = _FakeResp(400, {"error": {"message": "amount is invalid"}})
    out = await st.call_tool("get_balance", {}, STRIPE_KEY)
    assert out["isError"] is True
    assert out["content"][0]["text"] == "Stripe API error (400): amount is invalid"


# ── Shared guards ────────────────────────────────────────────────────────────


async def test_malformed_credentials(http):
    out = await wc.call_tool("list_products", {}, "not-json")
    assert out["isError"] is True and "malformed" in out["content"][0]["text"]


async def test_missing_required_param(http):
    out = await sq.call_tool("get_order", {}, SQ_CREDS)
    assert out["isError"] is True and "Missing required" in out["content"][0]["text"]
    assert not http.calls
