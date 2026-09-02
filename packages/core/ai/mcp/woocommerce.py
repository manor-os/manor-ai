"""
WooCommerce MCP server — in-process MCP for the WooCommerce REST API v3.

Auth: ``bearer_token`` is a JSON blob (credentials auth_type) decoded here:
  {
    "site_url": "https://store.example.com",   # WordPress site root
    "consumer_key": "ck_xxx",
    "consumer_secret": "cs_xxx"
  }
WooCommerce REST uses HTTP Basic auth with the consumer key/secret over HTTPS.

Tools follow ``mcp__woocommerce__{tool_name}``. Read + write across products,
orders, customers and stock.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_MAX_CHARS = 12_000
_TIMEOUT = 30.0
_MAX_PER_PAGE = 100


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if _is_blank(arguments.get(p))]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        cfg = json.loads(bearer_token) if bearer_token else {}
    except Exception:
        return _error("WooCommerce credentials malformed (expected JSON).")
    if not isinstance(cfg, dict):
        return _error("WooCommerce credentials malformed (expected JSON object).")
    base_value = cfg.get("site_url") or cfg.get("url")
    key = cfg.get("consumer_key") or cfg.get("key")
    secret = cfg.get("consumer_secret") or cfg.get("secret")
    if base_value is not None and not isinstance(base_value, str):
        return _error("WooCommerce site_url must be a string.")
    if key is not None and not isinstance(key, str):
        return _error("WooCommerce consumer_key must be a string.")
    if secret is not None and not isinstance(secret, str):
        return _error("WooCommerce consumer_secret must be a string.")
    base = (base_value or "").strip().rstrip("/")
    if _is_blank(base) or _is_blank(key) or _is_blank(secret):
        return _error("WooCommerce needs site_url, consumer_key and consumer_secret.")
    key = key.strip()
    secret = secret.strip()

    try:
        text = await handler(base, key, secret, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("WooCommerce MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class _WooError(RuntimeError):
    """Raised on non-2xx so call_tool surfaces it as isError, not as a
    success payload the model would mistake for a normal result."""


def _bounded_int(value: Any, name: str, minimum: int, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise _WooError(f"{name} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise _WooError(f"{name} must be an integer.") from None
    if isinstance(value, float) and value != parsed:
        raise _WooError(f"{name} must be an integer.")
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise _WooError(f"{name} must be in range {bound}.")
    return parsed


def _pagination(args: Dict[str, Any]) -> Dict[str, int]:
    return {
        "per_page": _bounded_int(args.get("per_page", 20), "per_page", 1, _MAX_PER_PAGE),
        "page": _bounded_int(args.get("page", 1), "page", 1),
    }


# ── REST client ───────────────────────────────────────────────────────────────

async def _api(
    base: str, key: str, secret: str,
    method: str, path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict] = None,
) -> str:
    qs = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            qs = "?" + urlencode(clean)
    url = f"{base}/wp-json/wc/v3/{path.lstrip('/')}{qs}"
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        raise _WooError("WooCommerce authentication failed. Check the consumer key/secret.")
    if resp.status_code == 404:
        raise _WooError("Not found.")
    if not resp.is_success:
        raise _WooError(f"WooCommerce API error ({resp.status_code}): {resp.text[:300]}")

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return out[:_MAX_CHARS] + "\n… (truncated)" if len(out) > _MAX_CHARS else out


# ── Products ──────────────────────────────────────────────────────────────────

async def _list_products(base, key, secret, args) -> str:
    pagination = _pagination(args)
    return await _api(base, key, secret, "GET", "products", {
        "search": args.get("search"),
        "status": args.get("status"),
        "sku": args.get("sku"),
        **pagination,
    })


async def _get_product(base, key, secret, args) -> str:
    return await _api(base, key, secret, "GET", f"products/{args['product_id']}")


async def _create_product(base, key, secret, args) -> str:
    body: Dict[str, Any] = {
        "name": args["name"],
        "type": args.get("type", "simple"),
        "status": args.get("status") or "draft",
    }
    for k_arg, k_api in (
        ("regular_price", "regular_price"), ("description", "description"),
        ("short_description", "short_description"), ("sku", "sku"),
    ):
        if args.get(k_arg) is not None:
            body[k_api] = str(args[k_arg]) if "price" in k_api else args[k_arg]
    if args.get("stock_quantity") is not None:
        body["manage_stock"] = True
        body["stock_quantity"] = int(args["stock_quantity"])
    return await _api(base, key, secret, "POST", "products", None, body)


async def _delete_product(base, key, secret, args) -> str:
    return await _api(
        base,
        key,
        secret,
        "DELETE",
        f"products/{args['product_id']}",
        {"force": "true" if args.get("force") else "false"},
    )


async def _update_product(base, key, secret, args) -> str:
    body: Dict[str, Any] = {}
    for k in ("name", "regular_price", "sale_price", "description",
              "short_description", "sku", "status"):
        if args.get(k) is not None:
            body[k] = str(args[k]) if "price" in k else args[k]
    return await _api(base, key, secret, "PUT", f"products/{args['product_id']}", None, body)


async def _set_stock(base, key, secret, args) -> str:
    return await _api(base, key, secret, "PUT", f"products/{args['product_id']}", None, {
        "manage_stock": True,
        "stock_quantity": int(args["stock_quantity"]),
    })


# ── Orders ────────────────────────────────────────────────────────────────────

async def _list_orders(base, key, secret, args) -> str:
    pagination = _pagination(args)
    return await _api(base, key, secret, "GET", "orders", {
        "status": args.get("status"),
        "search": args.get("search"),
        "customer": args.get("customer_id"),
        **pagination,
    })


async def _get_order(base, key, secret, args) -> str:
    return await _api(base, key, secret, "GET", f"orders/{args['order_id']}")


async def _update_order_status(base, key, secret, args) -> str:
    return await _api(base, key, secret, "PUT", f"orders/{args['order_id']}", None, {
        "status": args["status"],
    })


# ── Customers ─────────────────────────────────────────────────────────────────

async def _list_customers(base, key, secret, args) -> str:
    pagination = _pagination(args)
    return await _api(base, key, secret, "GET", "customers", {
        "search": args.get("search"),
        **pagination,
    })


async def _get_customer(base, key, secret, args) -> str:
    return await _api(base, key, secret, "GET", f"customers/{args['customer_id']}")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_products": {
        "description": "List/search WooCommerce products",
        "properties": {
            "search": _prop("Search term"),
            "status": _prop("publish, draft, pending, private"),
            "sku": _prop("Filter by SKU"),
            "per_page": _prop("Results per page (default: 20)", "integer"),
            "page": _prop("Page (default: 1)", "integer"),
        },
        "required": [],
    },
    "get_product": {
        "description": "Get a product by id",
        "properties": {"product_id": _prop("Product id", "integer")},
        "required": ["product_id"],
    },
    "create_product": {
        "description": "Create a product (defaults to draft)",
        "properties": {
            "name": _prop("Product name"),
            "type": _prop("simple, variable, grouped, external (default: simple)"),
            "regular_price": _prop("Price, e.g. '19.99'"),
            "description": _prop("Full description (HTML allowed)"),
            "short_description": _prop("Short description"),
            "sku": _prop("SKU"),
            "status": _prop("publish or draft (default: draft)"),
            "stock_quantity": _prop("Initial stock (enables stock management)", "integer"),
        },
        "required": ["name"],
    },
    "delete_product": {
        "description": "Move a product to trash, or permanently delete it with force=true",
        "properties": {
            "product_id": _prop("Product id", "integer"),
            "force": _prop("Permanently delete instead of moving to trash", "boolean"),
        },
        "required": ["product_id"],
    },
    "update_product": {
        "description": "Update a product's fields",
        "properties": {
            "product_id": _prop("Product id", "integer"),
            "name": _prop("New name"),
            "regular_price": _prop("New regular price"),
            "sale_price": _prop("Sale price"),
            "description": _prop("Description"),
            "short_description": _prop("Short description"),
            "sku": _prop("SKU"),
            "status": _prop("publish or draft"),
        },
        "required": ["product_id"],
    },
    "set_stock": {
        "description": "Set a product's managed stock quantity",
        "properties": {
            "product_id": _prop("Product id", "integer"),
            "stock_quantity": _prop("New stock quantity", "integer"),
        },
        "required": ["product_id", "stock_quantity"],
    },
    "list_orders": {
        "description": "List/search orders",
        "properties": {
            "status": _prop("pending, processing, on-hold, completed, cancelled, refunded"),
            "search": _prop("Search term"),
            "customer_id": _prop("Filter by customer id", "integer"),
            "per_page": _prop("Results per page (default: 20)", "integer"),
            "page": _prop("Page (default: 1)", "integer"),
        },
        "required": [],
    },
    "get_order": {
        "description": "Get an order by id",
        "properties": {"order_id": _prop("Order id", "integer")},
        "required": ["order_id"],
    },
    "update_order_status": {
        "description": "Update an order's status (fulfill / cancel / refund flow)",
        "properties": {
            "order_id": _prop("Order id", "integer"),
            "status": _prop("processing, completed, cancelled, refunded, on-hold, …"),
        },
        "required": ["order_id", "status"],
    },
    "list_customers": {
        "description": "List/search customers",
        "properties": {
            "search": _prop("Search term"),
            "per_page": _prop("Results per page (default: 20)", "integer"),
            "page": _prop("Page (default: 1)", "integer"),
        },
        "required": [],
    },
    "get_customer": {
        "description": "Get a customer by id",
        "properties": {"customer_id": _prop("Customer id", "integer")},
        "required": ["customer_id"],
    },
}


_HANDLERS = {
    "list_products": _list_products,
    "get_product": _get_product,
    "create_product": _create_product,
    "delete_product": _delete_product,
    "update_product": _update_product,
    "set_stock": _set_stock,
    "list_orders": _list_orders,
    "get_order": _get_order,
    "update_order_status": _update_order_status,
    "list_customers": _list_customers,
    "get_customer": _get_customer,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
