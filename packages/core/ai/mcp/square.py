"""
Square MCP server — in-process MCP for the Square API v2.

Auth: ``bearer_token`` is a JSON blob (credentials auth_type) decoded here:
  {
    "access_token": "EAAA…",          # Square access token
    "environment": "production",        # or "sandbox" (default: production)
    "location_id": "L123…"              # optional default location
  }

Tools follow ``mcp__square__{tool_name}``. Read + write across locations,
catalog (items), orders, customers and inventory.

Money is in the smallest currency unit (e.g. cents). Mutations send a
caller-supplied idempotency key so retries cannot create duplicate objects.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_VERSION = "2025-01-23"  # Square-Version header
_MAX_CHARS = 12_000
_TIMEOUT = 30.0
_MAX_LIMIT = 100


def _base(env: str) -> str:
    return (
        "https://connect.squareupsandbox.com/v2"
        if str(env).lower() == "sandbox"
        else "https://connect.squareup.com/v2"
    )


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
        return _error("Square credentials malformed (expected JSON).")
    if not isinstance(cfg, dict):
        return _error("Square credentials malformed (expected JSON object).")
    token = cfg.get("access_token") or cfg.get("token")
    if _is_blank(token):
        return _error("Square needs an access_token.")
    if not isinstance(token, str):
        return _error("Square access_token must be a string.")
    environment_value = cfg.get("environment", "production")
    if not isinstance(environment_value, str):
        return _error("Square environment must be a string.")
    token = token.strip()
    environment = environment_value.strip().lower()
    if environment not in {"sandbox", "production"}:
        return _error("Square environment must be 'sandbox' or 'production'.")
    base = _base(environment)

    try:
        text = await handler(base, token, cfg, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except _SquareError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("Square MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class _SquareError(RuntimeError):
    """Raised on non-2xx so call_tool surfaces it as isError, not as a
    success payload the model would mistake for a normal result."""


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise _SquareError("limit must be an integer between 1 and 100.")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise _SquareError("limit must be an integer between 1 and 100.") from None
    if isinstance(value, float) and value != limit:
        raise _SquareError("limit must be an integer between 1 and 100.")
    if limit < 1 or limit > _MAX_LIMIT:
        raise _SquareError("limit must be an integer between 1 and 100.")
    return limit


def _idempotency_key(
    args: Dict[str, Any],
    *,
    max_length: int | None = None,
) -> str:
    value = args.get("idempotency_key")
    if not isinstance(value, str) or not value.strip():
        raise _SquareError("idempotency_key must be a non-empty string.")
    if max_length is not None and len(value) > max_length:
        raise _SquareError(
            f"idempotency_key must be a string of 1-{max_length} characters."
        )
    return value


# ── REST client ───────────────────────────────────────────────────────────────

async def _api(
    base: str, token: str,
    method: str, path: str,
    body: Optional[Dict] = None,
) -> str:
    url = f"{base}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Square-Version": _VERSION,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        raise _SquareError("Square authentication failed. Reconnect Square (check access token / environment).")
    if resp.status_code == 404:
        raise _SquareError("Not found.")
    if not resp.is_success:
        raise _SquareError(f"Square API error ({resp.status_code}): {resp.text[:300]}")

    if not resp.text:
        return json.dumps({"ok": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return out[:_MAX_CHARS] + "\n… (truncated)" if len(out) > _MAX_CHARS else out


# ── Read ────────────────────────────────────────────────────────────────────

async def _list_locations(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", "locations")


async def _search_catalog_items(base, token, cfg, args) -> str:
    body: Dict[str, Any] = {"limit": _bounded_limit(args.get("limit", 20))}
    if args.get("query"):
        body["text_filter"] = args["query"]
    return await _api(base, token, "POST", "catalog/search-catalog-items", body)


async def _get_catalog_object(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", f"catalog/object/{args['object_id']}")


async def _search_orders(base, token, cfg, args) -> str:
    location_ids = args.get("location_ids") or cfg.get("location_id")
    if isinstance(location_ids, str):
        location_ids = [s.strip() for s in location_ids.split(",") if s.strip()]
    if not location_ids:
        raise _SquareError(
            "Provide location_ids (or set a default location_id in credentials)."
        )
    body: Dict[str, Any] = {"location_ids": location_ids, "limit": _bounded_limit(args.get("limit", 20))}
    if args.get("state"):
        body["query"] = {"filter": {"state_filter": {"states": [args["state"]]}}}
    return await _api(base, token, "POST", "orders/search", body)


async def _get_order(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", f"orders/{args['order_id']}")


async def _list_customers(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", "customers")


async def _get_customer(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", f"customers/{args['customer_id']}")


async def _get_inventory(base, token, cfg, args) -> str:
    return await _api(base, token, "GET", f"inventory/{args['catalog_object_id']}")


# ── Write ─────────────────────────────────────────────────────────────────────

async def _create_catalog_item(base, token, cfg, args) -> str:
    """Upsert a new ITEM with one priced variation."""
    price = {
        "amount": int(args["price_amount"]),
        "currency": args.get("currency", "USD"),
    }
    obj = {
        "type": "ITEM",
        "id": "#item",
        "item_data": {
            "name": args["name"],
            "description": args.get("description", ""),
            "variations": [{
                "type": "ITEM_VARIATION",
                "id": "#variation",
                "item_variation_data": {
                    "name": args.get("variation_name", "Regular"),
                    "pricing_type": "FIXED_PRICING",
                    "price_money": price,
                },
            }],
        },
    }
    return await _api(base, token, "POST", "catalog/object", {
        "idempotency_key": _idempotency_key(args, max_length=128), "object": obj,
    })


async def _delete_catalog_object(base, token, cfg, args) -> str:
    object_id = quote(str(args["object_id"]), safe="")
    return await _api(base, token, "DELETE", f"catalog/object/{object_id}")


async def _create_customer(base, token, cfg, args) -> str:
    body: Dict[str, Any] = {"idempotency_key": _idempotency_key(args)}
    for k in ("given_name", "family_name", "email_address", "phone_number", "company_name"):
        if args.get(k) is not None:
            body[k] = args[k]
    return await _api(base, token, "POST", "customers", body)


async def _update_customer(base, token, cfg, args) -> str:
    body: Dict[str, Any] = {}
    for k in ("given_name", "family_name", "email_address", "phone_number", "company_name"):
        if args.get(k) is not None:
            body[k] = args[k]
    return await _api(base, token, "PUT", f"customers/{args['customer_id']}", body)


async def _adjust_inventory(base, token, cfg, args) -> str:
    location_id = args.get("location_id") or cfg.get("location_id")
    if not location_id:
        raise _SquareError("Provide location_id (or set a default in credentials).")
    change = {
        "type": "ADJUSTMENT",
        "adjustment": {
            "catalog_object_id": args["catalog_object_id"],
            "location_id": location_id,
            "from_state": args.get("from_state", "NONE"),
            "to_state": args.get("to_state", "IN_STOCK"),
            "quantity": str(args["quantity"]),
        },
    }
    return await _api(base, token, "POST", "inventory/changes/batch-create", {
        "idempotency_key": _idempotency_key(args, max_length=128),
        "changes": [change],
    })


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # Read
    "list_locations": {
        "description": "List the merchant's Square locations",
        "properties": {}, "required": [],
    },
    "search_catalog_items": {
        "description": "Search catalog items by text",
        "properties": {
            "query": _prop("Text to search item names (optional)"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": [],
    },
    "get_catalog_object": {
        "description": "Get a catalog object (item/variation/etc.) by id",
        "properties": {"object_id": _prop("Catalog object id")},
        "required": ["object_id"],
    },
    "search_orders": {
        "description": "Search orders for one or more locations",
        "properties": {
            "location_ids": _prop("Location ids (comma-separated or array; defaults to credential location)"),
            "state": _prop("OPEN, COMPLETED, or CANCELED (optional)"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": [],
    },
    "get_order": {
        "description": "Get an order by id",
        "properties": {"order_id": _prop("Order id")},
        "required": ["order_id"],
    },
    "list_customers": {
        "description": "List customers",
        "properties": {}, "required": [],
    },
    "get_customer": {
        "description": "Get a customer by id",
        "properties": {"customer_id": _prop("Customer id")},
        "required": ["customer_id"],
    },
    "get_inventory": {
        "description": "Get inventory counts for a catalog object (variation)",
        "properties": {"catalog_object_id": _prop("Catalog object (variation) id")},
        "required": ["catalog_object_id"],
    },
    # Write
    "create_catalog_item": {
        "description": "Create a catalog item with one priced variation",
        "properties": {
            "name": _prop("Item name"),
            "price_amount": _prop("Price in the smallest currency unit (e.g. cents)", "integer"),
            "currency": _prop("ISO currency (default: USD)"),
            "description": _prop("Item description"),
            "variation_name": _prop("Variation name (default: Regular)"),
            "idempotency_key": _prop(
                "Caller-generated retry key (1-128 characters)",
                minLength=1,
                maxLength=128,
            ),
        },
        "required": ["name", "price_amount", "idempotency_key"],
    },
    "delete_catalog_object": {
        "description": "Delete a catalog object (use to clean up staging fixtures)",
        "properties": {"object_id": _prop("Catalog object id")},
        "required": ["object_id"],
    },
    "create_customer": {
        "description": "Create a customer",
        "properties": {
            "given_name": _prop("First name"),
            "family_name": _prop("Last name"),
            "email_address": _prop("Email"),
            "phone_number": _prop("Phone"),
            "company_name": _prop("Company"),
            "idempotency_key": _prop(
                "Caller-generated retry key",
                minLength=1,
            ),
        },
        "required": ["idempotency_key"],
    },
    "update_customer": {
        "description": "Update a customer's fields",
        "properties": {
            "customer_id": _prop("Customer id"),
            "given_name": _prop("First name"),
            "family_name": _prop("Last name"),
            "email_address": _prop("Email"),
            "phone_number": _prop("Phone"),
            "company_name": _prop("Company"),
        },
        "required": ["customer_id"],
    },
    "adjust_inventory": {
        "description": "Adjust inventory count for a catalog variation at a location",
        "properties": {
            "catalog_object_id": _prop("Catalog object (variation) id"),
            "quantity": _prop("Quantity to move", "integer"),
            "location_id": _prop("Location id (defaults to credential location)"),
            "from_state": _prop("From state (default: NONE)"),
            "to_state": _prop("To state (default: IN_STOCK)"),
            "idempotency_key": _prop(
                "Caller-generated retry key (1-128 characters)",
                minLength=1,
                maxLength=128,
            ),
        },
        "required": ["catalog_object_id", "quantity", "idempotency_key"],
    },
}


_HANDLERS = {
    "list_locations": _list_locations,
    "search_catalog_items": _search_catalog_items,
    "get_catalog_object": _get_catalog_object,
    "search_orders": _search_orders,
    "get_order": _get_order,
    "list_customers": _list_customers,
    "get_customer": _get_customer,
    "get_inventory": _get_inventory,
    "create_catalog_item": _create_catalog_item,
    "delete_catalog_object": _delete_catalog_object,
    "create_customer": _create_customer,
    "update_customer": _update_customer,
    "adjust_inventory": _adjust_inventory,
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
