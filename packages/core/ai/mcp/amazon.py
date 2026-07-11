"""
Amazon MCP server — in-process MCP for the Amazon Selling Partner API (SP-API).

Auth: ``bearer_token`` is a JSON blob (credentials auth_type) decoded here:
  {
    "refresh_token": "Atzr|...",       # LWA refresh token
    "lwa_client_id": "amzn1...",
    "lwa_client_secret": "...",
    "region": "na",                     # na | eu | fe (default: na)
    "marketplace_id": "ATVPDKIKX0DER",  # default marketplace
    "seller_id": "A1..."                # default selling partner id (for listings)
    # alternatively: "access_token": "Atza|..." to skip LWA exchange
  }

SP-API no longer requires AWS SigV4 for most operations — the LWA access token
in the ``x-amz-access-token`` header is sufficient. The access token is fetched
from the LWA refresh token (cached in-process ~ its TTL) unless one is supplied.

Tools follow ``mcp__amazon__{tool_name}``. Read across orders, catalog,
inventory and listings; write via generic listings patch/put.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

_LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_REGION_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
_MAX_CHARS = 12_000
_TIMEOUT = 30.0

# Cache LWA access tokens by refresh-token prefix → (token, expires_monotonic).
_token_cache: Dict[str, tuple[str, float]] = {}


class _AmazonError(RuntimeError):
    pass


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
        return _error("Amazon credentials malformed (expected JSON).")
    if not cfg.get("access_token") and not (
        cfg.get("refresh_token") and cfg.get("lwa_client_id") and cfg.get("lwa_client_secret")
    ):
        return _error(
            "Amazon needs either access_token, or refresh_token + lwa_client_id + lwa_client_secret."
        )

    try:
        text = await handler(cfg, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except _AmazonError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("Amazon MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Auth + client ──────────────────────────────────────────────────────────────

async def _access_token(cfg: Dict[str, Any]) -> str:
    if cfg.get("access_token"):
        return cfg["access_token"]
    rt = cfg["refresh_token"]
    # Key on a hash of the full token, not a prefix — two refresh tokens that
    # share a prefix must never collide and serve each other's access token.
    key = hashlib.sha256(rt.encode()).hexdigest()
    cached = _token_cache.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_LWA_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": cfg["lwa_client_id"],
            "client_secret": cfg["lwa_client_secret"],
        })
    if not resp.is_success:
        raise _AmazonError(f"LWA token exchange failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise _AmazonError("LWA token response missing access_token.")
    ttl = int(data.get("expires_in", 3600))
    _token_cache[key] = (token, time.monotonic() + max(ttl - 60, 60))
    return token


def _base(cfg: Dict[str, Any]) -> str:
    return _REGION_HOSTS.get(str(cfg.get("region", "na")).lower(), _REGION_HOSTS["na"])


async def _api(
    cfg: Dict[str, Any],
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict] = None,
) -> str:
    token = await _access_token(cfg)
    qs = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            qs = "?" + urlencode(clean)
    url = f"{_base(cfg)}{path}{qs}"
    headers = {"x-amz-access-token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code in (401, 403):
        return f"Amazon authorization failed ({resp.status_code}). Check LWA creds / roles: {resp.text[:200]}"
    if resp.status_code == 404:
        return "Not found."
    if not resp.is_success:
        return f"Amazon SP-API error ({resp.status_code}): {resp.text[:300]}"

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return out[:_MAX_CHARS] + "\n… (truncated)" if len(out) > _MAX_CHARS else out


def _marketplace(cfg, args) -> Optional[str]:
    return args.get("marketplace_id") or cfg.get("marketplace_id")


# ── Orders ────────────────────────────────────────────────────────────────────

async def _get_orders(cfg, args) -> str:
    mp = _marketplace(cfg, args)
    if not mp:
        return "Provide marketplace_id (or set a default in credentials)."
    params: Dict[str, Any] = {"MarketplaceIds": mp}
    if args.get("created_after"):
        params["CreatedAfter"] = args["created_after"]
    else:
        # SP-API getOrders requires CreatedAfter or LastUpdatedAfter; default
        # to the last 30 days so a bare get_orders call doesn't 400.
        params["CreatedAfter"] = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.get("order_statuses"):
        st = args["order_statuses"]
        params["OrderStatuses"] = ",".join(st) if isinstance(st, list) else st
    if args.get("max_results"):
        params["MaxResultsPerPage"] = int(args["max_results"])
    return await _api(cfg, "GET", "/orders/v0/orders", params)


async def _get_order(cfg, args) -> str:
    return await _api(cfg, "GET", f"/orders/v0/orders/{quote(args['order_id'], safe='')}")


async def _get_order_items(cfg, args) -> str:
    return await _api(cfg, "GET", f"/orders/v0/orders/{quote(args['order_id'], safe='')}/orderItems")


# ── Catalog ───────────────────────────────────────────────────────────────────

async def _search_catalog_items(cfg, args) -> str:
    mp = _marketplace(cfg, args)
    if not mp:
        return "Provide marketplace_id (or set a default in credentials)."
    params: Dict[str, Any] = {
        "marketplaceIds": mp,
        "includedData": args.get("included_data", "summaries"),
        "pageSize": int(args.get("page_size", 10)),
    }
    if args.get("keywords"):
        params["keywords"] = args["keywords"]
    if args.get("identifiers"):
        ids = args["identifiers"]
        params["identifiers"] = ",".join(ids) if isinstance(ids, list) else ids
        params["identifiersType"] = args.get("identifiers_type", "ASIN")
    return await _api(cfg, "GET", "/catalog/2022-04-01/items", params)


async def _get_catalog_item(cfg, args) -> str:
    mp = _marketplace(cfg, args)
    if not mp:
        return "Provide marketplace_id (or set a default in credentials)."
    return await _api(cfg, "GET", f"/catalog/2022-04-01/items/{quote(args['asin'], safe='')}", {
        "marketplaceIds": mp,
        "includedData": args.get("included_data", "summaries,attributes"),
    })


# ── Inventory ─────────────────────────────────────────────────────────────────

async def _get_inventory_summaries(cfg, args) -> str:
    mp = _marketplace(cfg, args)
    if not mp:
        return "Provide marketplace_id (or set a default in credentials)."
    return await _api(cfg, "GET", "/fba/inventory/v1/summaries", {
        "granularityType": "Marketplace",
        "granularityId": mp,
        "marketplaceIds": mp,
        "details": "true" if args.get("details") else None,
    })


# ── Listings ──────────────────────────────────────────────────────────────────

def _seller(cfg, args) -> Optional[str]:
    return args.get("seller_id") or cfg.get("seller_id")


async def _get_listing_item(cfg, args) -> str:
    mp = _marketplace(cfg, args)
    seller = _seller(cfg, args)
    if not (mp and seller):
        return "Provide marketplace_id and seller_id (or set defaults in credentials)."
    return await _api(
        cfg, "GET",
        f"/listings/2021-08-01/items/{quote(seller, safe='')}/{quote(args['sku'], safe='')}",
        {"marketplaceIds": mp, "includedData": args.get("included_data", "summaries,attributes,offers")},
    )


async def _patch_listing(cfg, args) -> str:
    """Partial update via JSON Patch-style ops (e.g. price / quantity)."""
    mp = _marketplace(cfg, args)
    seller = _seller(cfg, args)
    if not (mp and seller):
        return "Provide marketplace_id and seller_id (or set defaults in credentials)."
    patches = args["patches"]
    if isinstance(patches, str):
        patches = json.loads(patches)
    return await _api(
        cfg, "PATCH",
        f"/listings/2021-08-01/items/{quote(seller, safe='')}/{quote(args['sku'], safe='')}",
        {"marketplaceIds": mp},
        {"productType": args["product_type"], "patches": patches},
    )


async def _put_listing(cfg, args) -> str:
    """Create/replace a listing item (caller supplies the attributes map)."""
    mp = _marketplace(cfg, args)
    seller = _seller(cfg, args)
    if not (mp and seller):
        return "Provide marketplace_id and seller_id (or set defaults in credentials)."
    attributes = args["attributes"]
    if isinstance(attributes, str):
        attributes = json.loads(attributes)
    body: Dict[str, Any] = {"productType": args["product_type"], "attributes": attributes}
    if args.get("requirements"):
        body["requirements"] = args["requirements"]
    return await _api(
        cfg, "PUT",
        f"/listings/2021-08-01/items/{quote(seller, safe='')}/{quote(args['sku'], safe='')}",
        {"marketplaceIds": mp},
        body,
    )


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_MP = _prop("Marketplace id (defaults to credential marketplace)")
_SELLER = _prop("Seller (selling partner) id (defaults to credential seller_id)")

_TOOLS: Dict[str, Dict[str, Any]] = {
    # Orders
    "get_orders": {
        "description": "List orders (filter by date / status)",
        "properties": {
            "created_after": _prop("ISO 8601 timestamp (CreatedAfter)"),
            "order_statuses": _prop("Status(es): Unshipped, Shipped, Canceled, … (comma/array)"),
            "max_results": _prop("Max results per page", "integer"),
            "marketplace_id": _MP,
        },
        "required": [],
    },
    "get_order": {
        "description": "Get a single order by Amazon order id",
        "properties": {"order_id": _prop("Amazon order id (3-7-7 format)")},
        "required": ["order_id"],
    },
    "get_order_items": {
        "description": "Get the line items of an order",
        "properties": {"order_id": _prop("Amazon order id")},
        "required": ["order_id"],
    },
    # Catalog
    "search_catalog_items": {
        "description": "Search the Amazon catalog by keywords or identifiers",
        "properties": {
            "keywords": _prop("Search keywords"),
            "identifiers": _prop("Identifiers (ASIN/EAN/UPC…), comma-separated or array"),
            "identifiers_type": _prop("Identifier type (default: ASIN)"),
            "included_data": _prop("e.g. summaries,attributes,images (default: summaries)"),
            "page_size": _prop("Page size (default: 10)", "integer"),
            "marketplace_id": _MP,
        },
        "required": [],
    },
    "get_catalog_item": {
        "description": "Get a catalog item by ASIN",
        "properties": {
            "asin": _prop("ASIN"),
            "included_data": _prop("Default: summaries,attributes"),
            "marketplace_id": _MP,
        },
        "required": ["asin"],
    },
    # Inventory
    "get_inventory_summaries": {
        "description": "Get FBA inventory summaries for the marketplace",
        "properties": {
            "details": _prop("Include detailed breakdown", "boolean"),
            "marketplace_id": _MP,
        },
        "required": [],
    },
    # Listings (read + write)
    "get_listing_item": {
        "description": "Get one of your listings by SKU",
        "properties": {
            "sku": _prop("Seller SKU"),
            "included_data": _prop("Default: summaries,attributes,offers"),
            "marketplace_id": _MP,
            "seller_id": _SELLER,
        },
        "required": ["sku"],
    },
    "patch_listing": {
        "description": "Partially update a listing (e.g. price/quantity) via JSON Patch ops",
        "properties": {
            "sku": _prop("Seller SKU"),
            "product_type": _prop("Amazon product type (e.g. LUGGAGE, SHIRT)"),
            "patches": _prop("JSON Patch ops: [{op, path, value}] (array or JSON)", "array"),
            "marketplace_id": _MP,
            "seller_id": _SELLER,
        },
        "required": ["sku", "product_type", "patches"],
    },
    "put_listing": {
        "description": "Create or fully replace a listing (caller supplies attributes)",
        "properties": {
            "sku": _prop("Seller SKU"),
            "product_type": _prop("Amazon product type"),
            "attributes": _prop("Attributes map (object or JSON)", "object"),
            "requirements": _prop("e.g. LISTING, LISTING_OFFER_ONLY (optional)"),
            "marketplace_id": _MP,
            "seller_id": _SELLER,
        },
        "required": ["sku", "product_type", "attributes"],
    },
}


_HANDLERS = {
    "get_orders": _get_orders,
    "get_order": _get_order,
    "get_order_items": _get_order_items,
    "search_catalog_items": _search_catalog_items,
    "get_catalog_item": _get_catalog_item,
    "get_inventory_summaries": _get_inventory_summaries,
    "get_listing_item": _get_listing_item,
    "patch_listing": _patch_listing,
    "put_listing": _put_listing,
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
