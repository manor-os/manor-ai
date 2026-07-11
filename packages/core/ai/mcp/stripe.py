"""
Stripe MCP server — in-process MCP implementation for Stripe REST API.

Auth: Bearer token = Stripe secret key (sk_live_... or sk_test_...) from entity
integration config credentials.secretKey.

Tools follow mcp__stripe__{tool_name} naming via the MCP tool pool.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.stripe.com/v1"
_MAX_CHARS = 12_000


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    """Return MCP tool definitions (tools/list format)."""
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
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Stripe MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Stripe API client ────────────────────────────────────────────────────────

async def _api(
    secret_key: str,
    method: str,
    path: str,
    form_data: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    """Call Stripe API. Uses Basic auth with secret key."""
    url = f"{_API}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    auth = (secret_key, "")  # Stripe uses secret key as username, empty password

    async with httpx.AsyncClient(timeout=20.0) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers, params=params, auth=auth)
        elif method == "POST":
            resp = await client.post(url, headers=headers, data=form_data or {}, auth=auth)
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers, auth=auth)
        else:
            resp = await client.request(method, url, headers=headers, data=form_data, auth=auth)

    if resp.status_code == 401:
        return "Stripe authentication failed. Check your API key on the Integration page."
    if resp.status_code == 403:
        return f"Stripe forbidden: {resp.text[:300]}"
    if resp.status_code == 404:
        return "Not found."
    if resp.status_code == 429:
        return "Stripe rate limit exceeded. Please wait a moment and try again."
    if not resp.is_success:
        try:
            err = resp.json()
            msg = err.get("error", {}).get("message", resp.text[:300])
            return f"Stripe API error ({resp.status_code}): {msg}"
        except Exception:
            return f"Stripe API error ({resp.status_code}): {resp.text[:300]}"

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


# ── Tool handlers ─────────────────────────────────────────────────────────────

# -- Balance --

async def _get_balance(key: str, args: Dict) -> str:
    return await _api(key, "GET", "balance")


# -- Customers --

async def _list_customers(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("email"):
        params["email"] = args["email"]
    if args.get("starting_after"):
        params["starting_after"] = args["starting_after"]
    return await _api(key, "GET", "customers", params=params)


async def _get_customer(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"customers/{args['customer_id']}")


async def _create_customer(key: str, args: Dict) -> str:
    data: Dict[str, Any] = {}
    if args.get("email"):
        data["email"] = args["email"]
    if args.get("name"):
        data["name"] = args["name"]
    if args.get("phone"):
        data["phone"] = args["phone"]
    if args.get("description"):
        data["description"] = args["description"]
    return await _api(key, "POST", "customers", form_data=data)


async def _search_customers(key: str, args: Dict) -> str:
    params = {"query": args["query"], "limit": _clamp(args.get("limit", 10), 1, 100)}
    return await _api(key, "GET", "customers/search", params=params)


# -- Payments --

async def _list_payment_intents(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("customer"):
        params["customer"] = args["customer"]
    if args.get("starting_after"):
        params["starting_after"] = args["starting_after"]
    return await _api(key, "GET", "payment_intents", params=params)


async def _get_payment_intent(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"payment_intents/{args['payment_intent_id']}")


async def _create_payment_intent(key: str, args: Dict) -> str:
    data: Dict[str, Any] = {
        "amount": int(args["amount"]),
        "currency": args.get("currency", "usd"),
    }
    if args.get("customer"):
        data["customer"] = args["customer"]
    if args.get("description"):
        data["description"] = args["description"]
    if args.get("payment_method_types"):
        for i, t in enumerate(args["payment_method_types"].split(",")):
            data[f"payment_method_types[{i}]"] = t.strip()
    else:
        data["payment_method_types[0]"] = "card"
    return await _api(key, "POST", "payment_intents", form_data=data)


# -- Invoices --

async def _list_invoices(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("customer"):
        params["customer"] = args["customer"]
    if args.get("status"):
        params["status"] = args["status"]
    if args.get("starting_after"):
        params["starting_after"] = args["starting_after"]
    return await _api(key, "GET", "invoices", params=params)


async def _get_invoice(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"invoices/{args['invoice_id']}")


async def _create_invoice(key: str, args: Dict) -> str:
    data: Dict[str, Any] = {"customer": args["customer"]}
    if args.get("description"):
        data["description"] = args["description"]
    if args.get("auto_advance") is not None:
        data["auto_advance"] = str(args["auto_advance"]).lower()
    if args.get("collection_method"):
        data["collection_method"] = args["collection_method"]
    return await _api(key, "POST", "invoices", form_data=data)


async def _send_invoice(key: str, args: Dict) -> str:
    return await _api(key, "POST", f"invoices/{args['invoice_id']}/send")


async def _void_invoice(key: str, args: Dict) -> str:
    return await _api(key, "POST", f"invoices/{args['invoice_id']}/void")


# -- Invoice Items --

async def _create_invoice_item(key: str, args: Dict) -> str:
    data: Dict[str, Any] = {"customer": args["customer"]}
    if args.get("invoice"):
        data["invoice"] = args["invoice"]
    if args.get("amount"):
        data["amount"] = int(args["amount"])
    if args.get("currency"):
        data["currency"] = args["currency"]
    if args.get("description"):
        data["description"] = args["description"]
    return await _api(key, "POST", "invoiceitems", form_data=data)


# -- Products & Prices --

async def _list_products(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("active") is not None:
        params["active"] = str(args["active"]).lower()
    return await _api(key, "GET", "products", params=params)


async def _get_product(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"products/{args['product_id']}")


async def _list_prices(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("product"):
        params["product"] = args["product"]
    if args.get("active") is not None:
        params["active"] = str(args["active"]).lower()
    return await _api(key, "GET", "prices", params=params)


# -- Charges --

async def _list_charges(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("customer"):
        params["customer"] = args["customer"]
    if args.get("starting_after"):
        params["starting_after"] = args["starting_after"]
    return await _api(key, "GET", "charges", params=params)


async def _get_charge(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"charges/{args['charge_id']}")


# -- Refunds --

async def _create_refund(key: str, args: Dict) -> str:
    data: Dict[str, Any] = {}
    if args.get("payment_intent"):
        data["payment_intent"] = args["payment_intent"]
    if args.get("charge"):
        data["charge"] = args["charge"]
    if args.get("amount"):
        data["amount"] = int(args["amount"])
    if args.get("reason"):
        data["reason"] = args["reason"]
    return await _api(key, "POST", "refunds", form_data=data)


async def _list_refunds(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("payment_intent"):
        params["payment_intent"] = args["payment_intent"]
    if args.get("charge"):
        params["charge"] = args["charge"]
    return await _api(key, "GET", "refunds", params=params)


# -- Subscriptions --

async def _list_subscriptions(key: str, args: Dict) -> str:
    params: Dict[str, Any] = {"limit": _clamp(args.get("limit", 10), 1, 100)}
    if args.get("customer"):
        params["customer"] = args["customer"]
    if args.get("status"):
        params["status"] = args["status"]
    return await _api(key, "GET", "subscriptions", params=params)


async def _get_subscription(key: str, args: Dict) -> str:
    return await _api(key, "GET", f"subscriptions/{args['subscription_id']}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(value, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    # Balance
    "get_balance": {
        "description": "Get the Stripe account balance (available + pending amounts)",
        "properties": {},
        "required": [],
    },
    # Customers
    "list_customers": {
        "description": "List Stripe customers",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "email": _prop("Filter by exact email address"),
            "starting_after": _prop("Cursor for pagination (customer ID)"),
        },
        "required": [],
    },
    "get_customer": {
        "description": "Get a Stripe customer by ID",
        "properties": {"customer_id": _prop("Customer ID (cus_...)")},
        "required": ["customer_id"],
    },
    "create_customer": {
        "description": "Create a new Stripe customer",
        "properties": {
            "email": _prop("Customer email"),
            "name": _prop("Customer name"),
            "phone": _prop("Customer phone"),
            "description": _prop("Internal description"),
        },
        "required": [],
    },
    "search_customers": {
        "description": "Search Stripe customers (e.g. email:'user@example.com' or name~'John')",
        "properties": {
            "query": _prop("Stripe search query syntax"),
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
        },
        "required": ["query"],
    },
    # Payments
    "list_payment_intents": {
        "description": "List payment intents (recent payments)",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "customer": _prop("Filter by customer ID"),
            "starting_after": _prop("Cursor for pagination"),
        },
        "required": [],
    },
    "get_payment_intent": {
        "description": "Get a payment intent by ID",
        "properties": {"payment_intent_id": _prop("Payment Intent ID (pi_...)")},
        "required": ["payment_intent_id"],
    },
    "create_payment_intent": {
        "description": "Create a payment intent (amount in cents, e.g. 1000 = $10.00)",
        "properties": {
            "amount": _prop("Amount in cents (e.g. 2500 = $25.00)", "integer"),
            "currency": _prop("Currency code (default: usd)"),
            "customer": _prop("Customer ID to charge"),
            "description": _prop("Payment description"),
            "payment_method_types": _prop("Comma-separated types (default: card)"),
        },
        "required": ["amount"],
    },
    # Invoices
    "list_invoices": {
        "description": "List invoices",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "customer": _prop("Filter by customer ID"),
            "status": _prop("Filter: draft, open, paid, uncollectible, void"),
            "starting_after": _prop("Cursor for pagination"),
        },
        "required": [],
    },
    "get_invoice": {
        "description": "Get an invoice by ID",
        "properties": {"invoice_id": _prop("Invoice ID (in_...)")},
        "required": ["invoice_id"],
    },
    "create_invoice": {
        "description": "Create a draft invoice for a customer",
        "properties": {
            "customer": _prop("Customer ID"),
            "description": _prop("Invoice description"),
            "auto_advance": _prop("Auto-finalize (default: true)", "boolean"),
            "collection_method": _prop("charge_automatically or send_invoice"),
        },
        "required": ["customer"],
    },
    "send_invoice": {
        "description": "Finalize and send an invoice to the customer",
        "properties": {"invoice_id": _prop("Invoice ID")},
        "required": ["invoice_id"],
    },
    "void_invoice": {
        "description": "Void an open invoice",
        "properties": {"invoice_id": _prop("Invoice ID")},
        "required": ["invoice_id"],
    },
    "create_invoice_item": {
        "description": "Add a line item to a draft invoice",
        "properties": {
            "customer": _prop("Customer ID"),
            "invoice": _prop("Invoice ID (optional — if omitted, added to next invoice)"),
            "amount": _prop("Amount in cents", "integer"),
            "currency": _prop("Currency code (default: usd)"),
            "description": _prop("Line item description"),
        },
        "required": ["customer"],
    },
    # Products & Prices
    "list_products": {
        "description": "List Stripe products",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "active": _prop("Filter: true or false", "boolean"),
        },
        "required": [],
    },
    "get_product": {
        "description": "Get a Stripe product by ID",
        "properties": {"product_id": _prop("Product ID (prod_...)")},
        "required": ["product_id"],
    },
    "list_prices": {
        "description": "List prices (optionally filter by product)",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "product": _prop("Filter by product ID"),
            "active": _prop("Filter: true or false", "boolean"),
        },
        "required": [],
    },
    # Charges
    "list_charges": {
        "description": "List charges (completed payments)",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "customer": _prop("Filter by customer ID"),
            "starting_after": _prop("Cursor for pagination"),
        },
        "required": [],
    },
    "get_charge": {
        "description": "Get a charge by ID",
        "properties": {"charge_id": _prop("Charge ID (ch_...)")},
        "required": ["charge_id"],
    },
    # Refunds
    "create_refund": {
        "description": "Create a refund for a payment",
        "properties": {
            "payment_intent": _prop("Payment Intent ID to refund"),
            "charge": _prop("Charge ID to refund (alternative to payment_intent)"),
            "amount": _prop("Partial refund amount in cents (omit for full refund)", "integer"),
            "reason": _prop("Reason: duplicate, fraudulent, or requested_by_customer"),
        },
        "required": [],
    },
    "list_refunds": {
        "description": "List refunds",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "payment_intent": _prop("Filter by payment intent ID"),
            "charge": _prop("Filter by charge ID"),
        },
        "required": [],
    },
    # Subscriptions
    "list_subscriptions": {
        "description": "List subscriptions",
        "properties": {
            "limit": _prop("Max results (1-100, default: 10)", "integer"),
            "customer": _prop("Filter by customer ID"),
            "status": _prop("Filter: active, past_due, canceled, incomplete, trialing, all"),
        },
        "required": [],
    },
    "get_subscription": {
        "description": "Get a subscription by ID",
        "properties": {"subscription_id": _prop("Subscription ID (sub_...)")},
        "required": ["subscription_id"],
    },
}

_HANDLERS = {
    "get_balance": _get_balance,
    "list_customers": _list_customers,
    "get_customer": _get_customer,
    "create_customer": _create_customer,
    "search_customers": _search_customers,
    "list_payment_intents": _list_payment_intents,
    "get_payment_intent": _get_payment_intent,
    "create_payment_intent": _create_payment_intent,
    "list_invoices": _list_invoices,
    "get_invoice": _get_invoice,
    "create_invoice": _create_invoice,
    "send_invoice": _send_invoice,
    "void_invoice": _void_invoice,
    "create_invoice_item": _create_invoice_item,
    "list_products": _list_products,
    "get_product": _get_product,
    "list_prices": _list_prices,
    "list_charges": _list_charges,
    "get_charge": _get_charge,
    "create_refund": _create_refund,
    "list_refunds": _list_refunds,
    "list_subscriptions": _list_subscriptions,
    "get_subscription": _get_subscription,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build MCP tool definition."""
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
