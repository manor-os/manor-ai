"""
TikTok Shop MCP server — in-process MCP for the TikTok Shop Partner API (202309).

Distinct from the consumer ``tiktok`` module (content/video): this is the
seller/commerce side — shops, orders, products, price and inventory.

Auth: ``bearer_token`` is a JSON blob (credentials auth_type) decoded here:
  {
    "app_key": "...",
    "app_secret": "...",
    "access_token": "...",        # seller access token (x-tts-access-token)
    "shop_cipher": "..."          # default shop cipher for shop-scoped calls
  }

Every request is signed with HMAC-SHA256 per TikTok Shop's algorithm:
  sign = HMAC_SHA256(app_secret, app_secret + path + sorted(k+v for query
         params except sign/access_token) + body + app_secret).hex()
The access token travels in the ``x-tts-access-token`` header, not the query.

Tools follow ``mcp__tiktok_shop__{tool_name}``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_API = "https://open-api.tiktokglobalshop.com"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0


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

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        cfg = json.loads(bearer_token) if bearer_token else {}
    except Exception:
        return _error("TikTok Shop credentials malformed (expected JSON).")
    if not (cfg.get("app_key") and cfg.get("app_secret") and cfg.get("access_token")):
        return _error("TikTok Shop needs app_key, app_secret and access_token.")

    try:
        text = await handler(cfg, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("TikTok Shop MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Signed API client ──────────────────────────────────────────────────────────

def _sign(app_secret: str, path: str, query: Dict[str, Any], body_str: str) -> str:
    """TikTok Shop HMAC-SHA256 request signature."""
    keys = sorted(k for k in query if k not in ("sign", "access_token"))
    concat = "".join(f"{k}{query[k]}" for k in keys)
    base = f"{app_secret}{path}{concat}{body_str}{app_secret}"
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest()


async def _api(
    cfg: Dict[str, Any],
    method: str,
    path: str,
    query: Optional[Dict[str, Any]] = None,
    body: Optional[Dict] = None,
    *,
    shop_scoped: bool = True,
) -> str:
    q: Dict[str, Any] = {
        "app_key": cfg["app_key"],
        "timestamp": str(int(time.time())),
    }
    if shop_scoped:
        cipher = (query or {}).get("shop_cipher") or cfg.get("shop_cipher")
        if not cipher:
            return "This call needs a shop_cipher (pass it, or set one in credentials)."
        q["shop_cipher"] = cipher
    for k, v in (query or {}).items():
        if v is not None and v != "" and k != "shop_cipher":
            q[k] = v

    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    q["sign"] = _sign(cfg["app_secret"], path, q, body_str)

    url = f"{_API}{path}?{urlencode(q)}"
    headers = {
        "x-tts-access-token": cfg["access_token"],
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(
            method, url, headers=headers,
            content=body_str.encode() if body is not None else None,
        )

    if resp.status_code == 401:
        return "TikTok Shop authentication failed. Reconnect the shop (token/app key)."
    if not resp.is_success:
        return f"TikTok Shop API error ({resp.status_code}): {resp.text[:300]}"

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return out[:_MAX_CHARS] + "\n… (truncated)" if len(out) > _MAX_CHARS else out


# ── Shops ─────────────────────────────────────────────────────────────────────

async def _get_authorized_shops(cfg, args) -> str:
    # Not shop-scoped: returns the shops (and their shop_cipher) this token can access.
    return await _api(cfg, "GET", "/authorization/202309/shops", shop_scoped=False)


# ── Orders ────────────────────────────────────────────────────────────────────

async def _search_orders(cfg, args) -> str:
    body: Dict[str, Any] = {}
    if args.get("order_status"):
        body["order_status"] = args["order_status"]
    if args.get("create_time_ge"):
        body["create_time_ge"] = int(args["create_time_ge"])
    if args.get("create_time_lt"):
        body["create_time_lt"] = int(args["create_time_lt"])
    query = {
        "page_size": int(args.get("page_size", 20)),
        "shop_cipher": args.get("shop_cipher"),
    }
    if args.get("page_token"):
        query["page_token"] = args["page_token"]
    return await _api(cfg, "POST", "/order/202309/orders/search", query, body)


async def _get_order_detail(cfg, args) -> str:
    ids = args["order_ids"]
    if isinstance(ids, list):
        ids = ",".join(str(x) for x in ids)
    return await _api(cfg, "GET", "/order/202309/orders", {
        "ids": ids, "shop_cipher": args.get("shop_cipher"),
    })


# ── Products ──────────────────────────────────────────────────────────────────

async def _search_products(cfg, args) -> str:
    body: Dict[str, Any] = {}
    if args.get("status"):
        body["status"] = args["status"]
    query = {
        "page_size": int(args.get("page_size", 20)),
        "shop_cipher": args.get("shop_cipher"),
    }
    if args.get("page_token"):
        query["page_token"] = args["page_token"]
    return await _api(cfg, "POST", "/product/202309/products/search", query, body)


async def _get_product(cfg, args) -> str:
    pid = args["product_id"]
    return await _api(cfg, "GET", f"/product/202309/products/{pid}", {
        "shop_cipher": args.get("shop_cipher"),
    })


async def _update_price(cfg, args) -> str:
    skus = args["skus"]  # [{id, price: {amount, currency}}]
    if isinstance(skus, str):
        skus = json.loads(skus)
    pid = args["product_id"]
    return await _api(cfg, "POST", f"/product/202309/products/{pid}/prices/update", {
        "shop_cipher": args.get("shop_cipher"),
    }, {"skus": skus})


async def _update_inventory(cfg, args) -> str:
    skus = args["skus"]  # [{id, inventory: [{warehouse_id, quantity}]}]
    if isinstance(skus, str):
        skus = json.loads(skus)
    pid = args["product_id"]
    return await _api(cfg, "POST", f"/product/202309/products/{pid}/inventory/update", {
        "shop_cipher": args.get("shop_cipher"),
    }, {"skus": skus})


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_SHOP = _prop("Shop cipher (defaults to the one in credentials)")

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_authorized_shops": {
        "description": "List shops this token can access (returns each shop_cipher)",
        "properties": {}, "required": [],
    },
    "search_orders": {
        "description": "Search shop orders",
        "properties": {
            "order_status": _prop("e.g. UNPAID, AWAITING_SHIPMENT, IN_TRANSIT, DELIVERED, COMPLETED, CANCELLED"),
            "create_time_ge": _prop("Created at/after (unix seconds)", "integer"),
            "create_time_lt": _prop("Created before (unix seconds)", "integer"),
            "page_size": _prop("Page size (default: 20)", "integer"),
            "page_token": _prop("Pagination token"),
            "shop_cipher": _SHOP,
        },
        "required": [],
    },
    "get_order_detail": {
        "description": "Get order detail(s) by id",
        "properties": {
            "order_ids": _prop("Order id(s), comma-separated or array"),
            "shop_cipher": _SHOP,
        },
        "required": ["order_ids"],
    },
    "search_products": {
        "description": "Search shop products",
        "properties": {
            "status": _prop("e.g. ACTIVATE, DEACTIVATED, DRAFT, PENDING"),
            "page_size": _prop("Page size (default: 20)", "integer"),
            "page_token": _prop("Pagination token"),
            "shop_cipher": _SHOP,
        },
        "required": [],
    },
    "get_product": {
        "description": "Get a product by id",
        "properties": {"product_id": _prop("Product id"), "shop_cipher": _SHOP},
        "required": ["product_id"],
    },
    "update_price": {
        "description": "Update SKU prices for a product",
        "properties": {
            "product_id": _prop("Product id"),
            "skus": _prop("Array (or JSON) of {id, price:{amount, currency}}", "array"),
            "shop_cipher": _SHOP,
        },
        "required": ["product_id", "skus"],
    },
    "update_inventory": {
        "description": "Update SKU inventory for a product",
        "properties": {
            "product_id": _prop("Product id"),
            "skus": _prop("Array (or JSON) of {id, inventory:[{warehouse_id, quantity}]}", "array"),
            "shop_cipher": _SHOP,
        },
        "required": ["product_id", "skus"],
    },
}


_HANDLERS = {
    "get_authorized_shops": _get_authorized_shops,
    "search_orders": _search_orders,
    "get_order_detail": _get_order_detail,
    "search_products": _search_products,
    "get_product": _get_product,
    "update_price": _update_price,
    "update_inventory": _update_inventory,
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
