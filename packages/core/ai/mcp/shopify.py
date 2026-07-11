"""
Shopify MCP server — in-process MCP for the Shopify Admin GraphQL API.

Auth: ``bearer_token`` is a JSON blob (credentials auth_type) decoded here:
  {
    "shop_domain": "my-store.myshopify.com",
    "access_token": "shpat_…"            # Admin API access token
  }
Uses Shopify's Admin GraphQL API with ``X-Shopify-Access-Token``.

Tools follow ``mcp__shopify__{tool_name}``. Read + write across shop, products,
orders, customers and inventory.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_ADMIN_API_VERSION = "2026-04"
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
        return _error("Shopify credentials malformed (expected JSON).")
    domain = cfg.get("shop_domain") or cfg.get("myshopify_domain") or cfg.get("domain")
    token = cfg.get("access_token") or cfg.get("admin_access_token")
    if not (domain and token):
        return _error("Shopify needs shop_domain and access_token.")
    if not str(domain).endswith(".myshopify.com") and "." not in str(domain):
        domain = f"{domain}.myshopify.com"

    try:
        text = await handler(domain, token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except _ShopifyError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("Shopify MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


class _ShopifyError(RuntimeError):
    pass


# ── GraphQL client ────────────────────────────────────────────────────────────

async def _gql(domain: str, token: str, query: str, variables: Optional[Dict] = None) -> str:
    url = f"https://{domain}/admin/api/{_ADMIN_API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json={
            "query": query, "variables": variables or {},
        })

    if resp.status_code == 401:
        raise _ShopifyError("Shopify authentication failed. Check shop_domain / access token.")
    if not resp.is_success:
        raise _ShopifyError(f"Shopify API error ({resp.status_code}): {resp.text[:300]}")

    body = resp.json()
    if body.get("errors"):
        raise _ShopifyError(f"GraphQL errors: {str(body['errors'])[:600]}")
    data = body.get("data") or {}
    # Surface mutation userErrors as a clear failure instead of silent success.
    for v in data.values():
        if isinstance(v, dict) and v.get("userErrors"):
            raise _ShopifyError(f"userErrors: {json.dumps(v['userErrors'])[:600]}")
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return out[:_MAX_CHARS] + "\n… (truncated)" if len(out) > _MAX_CHARS else out


def _gid(kind: str, value: Any) -> str:
    s = str(value)
    return s if s.startswith("gid://") else f"gid://shopify/{kind}/{s}"


# ── Read ────────────────────────────────────────────────────────────────────

_PRODUCT_FIELDS = """
id title handle status vendor productType totalInventory
variants(first: 10) { edges { node { id title sku price inventoryQuantity } } }
"""

_ORDER_FIELDS = """
id name createdAt displayFinancialStatus displayFulfillmentStatus
totalPriceSet { shopMoney { amount currencyCode } }
customer { id displayName email }
"""


async def _get_shop(domain, token, args) -> str:
    return await _gql(domain, token, """
        query { shop { name email myshopifyDomain currencyCode
                       plan { displayName } } }
    """)


async def _list_products(domain, token, args) -> str:
    return await _gql(domain, token, f"""
        query($first: Int!, $query: String) {{
          products(first: $first, query: $query) {{
            edges {{ node {{ {_PRODUCT_FIELDS} }} }}
            pageInfo {{ hasNextPage }}
          }}
        }}
    """, {"first": int(args.get("first", 20)), "query": args.get("query")})


async def _get_product(domain, token, args) -> str:
    return await _gql(domain, token, f"""
        query($id: ID!) {{ product(id: $id) {{ {_PRODUCT_FIELDS} descriptionHtml }} }}
    """, {"id": _gid("Product", args["product_id"])})


async def _list_orders(domain, token, args) -> str:
    return await _gql(domain, token, f"""
        query($first: Int!, $query: String) {{
          orders(first: $first, query: $query) {{
            edges {{ node {{ {_ORDER_FIELDS} }} }}
            pageInfo {{ hasNextPage }}
          }}
        }}
    """, {"first": int(args.get("first", 20)), "query": args.get("query")})


async def _get_order(domain, token, args) -> str:
    return await _gql(domain, token, f"""
        query($id: ID!) {{ order(id: $id) {{ {_ORDER_FIELDS}
            lineItems(first: 50) {{ edges {{ node {{ title quantity sku }} }} }} }} }}
    """, {"id": _gid("Order", args["order_id"])})


async def _list_customers(domain, token, args) -> str:
    return await _gql(domain, token, """
        query($first: Int!, $query: String) {
          customers(first: $first, query: $query) {
            edges { node { id displayName email phone numberOfOrders } }
          }
        }
    """, {"first": int(args.get("first", 20)), "query": args.get("query")})


# ── Write ─────────────────────────────────────────────────────────────────────

async def _create_product(domain, token, args) -> str:
    pinput: Dict[str, Any] = {"title": args["title"]}
    for k_arg, k_api in (
        ("description", "descriptionHtml"), ("vendor", "vendor"),
        ("product_type", "productType"), ("status", "status"),
    ):
        if args.get(k_arg) is not None:
            pinput[k_api] = args[k_arg]
    if args.get("tags") is not None:
        tags = args["tags"]
        pinput["tags"] = tags if isinstance(tags, list) else [
            t.strip() for t in str(tags).split(",") if t.strip()
        ]
    return await _gql(domain, token, """
        mutation productCreate($input: ProductInput!) {
          productCreate(input: $input) {
            product { id title status }
            userErrors { field message }
          }
        }
    """, {"input": pinput})


async def _update_product(domain, token, args) -> str:
    pinput: Dict[str, Any] = {"id": _gid("Product", args["product_id"])}
    for k_arg, k_api in (
        ("title", "title"), ("description", "descriptionHtml"),
        ("vendor", "vendor"), ("product_type", "productType"), ("status", "status"),
    ):
        if args.get(k_arg) is not None:
            pinput[k_api] = args[k_arg]
    if args.get("tags") is not None:
        tags = args["tags"]
        pinput["tags"] = tags if isinstance(tags, list) else [
            t.strip() for t in str(tags).split(",") if t.strip()
        ]
    return await _gql(domain, token, """
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title status }
            userErrors { field message }
          }
        }
    """, {"input": pinput})


async def _add_order_tags(domain, token, args) -> str:
    tags = args["tags"]
    tags = tags if isinstance(tags, list) else [
        t.strip() for t in str(tags).split(",") if t.strip()
    ]
    return await _gql(domain, token, """
        mutation tagsAdd($id: ID!, $tags: [String!]!) {
          tagsAdd(id: $id, tags: $tags) {
            node { id }
            userErrors { field message }
          }
        }
    """, {"id": _gid("Order", args["order_id"]), "tags": tags})


async def _create_customer(domain, token, args) -> str:
    cinput: Dict[str, Any] = {}
    for k_arg, k_api in (
        ("first_name", "firstName"), ("last_name", "lastName"),
        ("email", "email"), ("phone", "phone"),
    ):
        if args.get(k_arg) is not None:
            cinput[k_api] = args[k_arg]
    return await _gql(domain, token, """
        mutation customerCreate($input: CustomerInput!) {
          customerCreate(input: $input) {
            customer { id email displayName }
            userErrors { field message }
          }
        }
    """, {"input": cinput})


async def _adjust_inventory(domain, token, args) -> str:
    change = {
        "delta": int(args["delta"]),
        "inventoryItemId": _gid("InventoryItem", args["inventory_item_id"]),
        "locationId": _gid("Location", args["location_id"]),
    }
    return await _gql(domain, token, """
        mutation inventoryAdjust($input: InventoryAdjustQuantitiesInput!) {
          inventoryAdjustQuantities(input: $input) {
            inventoryAdjustmentGroup { createdAt reason }
            userErrors { field message }
          }
        }
    """, {"input": {
        "reason": args.get("reason", "correction"),
        "name": args.get("name", "available"),
        "changes": [change],
    }})


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # Read
    "get_shop": {
        "description": "Get the shop's profile (name, currency, plan)",
        "properties": {}, "required": [],
    },
    "list_products": {
        "description": "List/search products (Shopify query syntax, e.g. 'status:active')",
        "properties": {
            "query": _prop("Shopify search query (optional)"),
            "first": _prop("Number of products (default: 20)", "integer"),
        },
        "required": [],
    },
    "get_product": {
        "description": "Get a product by numeric id or gid",
        "properties": {"product_id": _prop("Product id (numeric or gid://shopify/Product/…)")},
        "required": ["product_id"],
    },
    "list_orders": {
        "description": "List/search orders (e.g. 'financial_status:paid')",
        "properties": {
            "query": _prop("Shopify search query (optional)"),
            "first": _prop("Number of orders (default: 20)", "integer"),
        },
        "required": [],
    },
    "get_order": {
        "description": "Get an order (with line items) by numeric id or gid",
        "properties": {"order_id": _prop("Order id (numeric or gid)")},
        "required": ["order_id"],
    },
    "list_customers": {
        "description": "List/search customers",
        "properties": {
            "query": _prop("Shopify search query (optional)"),
            "first": _prop("Number of customers (default: 20)", "integer"),
        },
        "required": [],
    },
    # Write
    "create_product": {
        "description": "Create a product",
        "properties": {
            "title": _prop("Product title"),
            "description": _prop("Description (HTML)"),
            "vendor": _prop("Vendor"),
            "product_type": _prop("Product type"),
            "status": _prop("ACTIVE, DRAFT, or ARCHIVED"),
            "tags": _prop("Tags (comma-separated or array)"),
        },
        "required": ["title"],
    },
    "update_product": {
        "description": "Update a product's fields",
        "properties": {
            "product_id": _prop("Product id (numeric or gid)"),
            "title": _prop("Title"),
            "description": _prop("Description (HTML)"),
            "vendor": _prop("Vendor"),
            "product_type": _prop("Product type"),
            "status": _prop("ACTIVE, DRAFT, or ARCHIVED"),
            "tags": _prop("Tags (comma-separated or array)"),
        },
        "required": ["product_id"],
    },
    "add_order_tags": {
        "description": "Add tags to an order (e.g. mark for fulfillment / follow-up)",
        "properties": {
            "order_id": _prop("Order id (numeric or gid)"),
            "tags": _prop("Tags to add (comma-separated or array)"),
        },
        "required": ["order_id", "tags"],
    },
    "create_customer": {
        "description": "Create a customer",
        "properties": {
            "first_name": _prop("First name"),
            "last_name": _prop("Last name"),
            "email": _prop("Email"),
            "phone": _prop("Phone (E.164)"),
        },
        "required": [],
    },
    "adjust_inventory": {
        "description": "Adjust available inventory for an item at a location (by delta)",
        "properties": {
            "inventory_item_id": _prop("Inventory item id (numeric or gid)"),
            "location_id": _prop("Location id (numeric or gid)"),
            "delta": _prop("Quantity change (+/-)", "integer"),
            "name": _prop("Quantity name (default: available)"),
            "reason": _prop("Reason (default: correction)"),
        },
        "required": ["inventory_item_id", "location_id", "delta"],
    },
}


_HANDLERS = {
    "get_shop": _get_shop,
    "list_products": _list_products,
    "get_product": _get_product,
    "list_orders": _list_orders,
    "get_order": _get_order,
    "list_customers": _list_customers,
    "create_product": _create_product,
    "update_product": _update_product,
    "add_order_tags": _add_order_tags,
    "create_customer": _create_customer,
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
