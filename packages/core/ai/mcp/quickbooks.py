"""
QuickBooks Online MCP server — in-process MCP for QBO REST API.

Auth: Bearer token = QuickBooks OAuth2 access_token (from entity integration config).
Requires realm_id (company ID) stored in credentials alongside the access_token.

Tools follow mcp__quickbooks__{tool_name} naming via the MCP tool pool.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# QBO uses sandbox vs production base URLs
_API_PROD = "https://quickbooks.api.intuit.com/v3/company"
_API_SANDBOX = "https://sandbox-quickbooks.api.intuit.com/v3/company"
_MAX_CHARS = 12_000


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

    # bearer_token here is the access_token. We need realm_id too.
    # The auth resolver gives us just the token. realm_id must come from arguments
    # or we fetch it from the integration config via a helper.
    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("QuickBooks MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── QBO API client ───────────────────────────────────────────────────────────

def _base_url() -> str:
    """Use sandbox in dev, production otherwise."""
    env = os.getenv("QBO_ENVIRONMENT", "production").lower()
    return _API_SANDBOX if env == "sandbox" else _API_PROD


async def _api(
    token: str,
    method: str,
    realm_id: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    base = _base_url()
    url = f"{base}/{realm_id}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if method in ("POST",) and body is not None:
        headers["Content-Type"] = "application/json"

    # QBO needs an explicit minorversion for a stable response schema; apply
    # it to every method (not just GET).
    params = {**(params or {}), "minorversion": "65"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers, params=params)
        elif method == "POST":
            resp = await client.post(url, headers=headers, params=params, json=body or {})
        else:
            resp = await client.request(method, url, headers=headers, params=params, json=body)

    if resp.status_code == 401:
        raise RuntimeError("QuickBooks authentication failed. Reconnect QuickBooks on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"QuickBooks forbidden: {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code == 429:
        raise RuntimeError("QuickBooks rate limit exceeded. Please wait and try again.")
    if not resp.is_success:
        raise RuntimeError(f"QuickBooks API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


async def _query(token: str, realm_id: str, sql: str) -> str:
    """Run a QBO query (SQL-like syntax)."""
    return await _api(token, "GET", realm_id, "query", params={"query": sql})


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def _get_company_info(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    return await _api(token, "GET", realm_id, f"companyinfo/{realm_id}")


async def _query_customers(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    where = f" WHERE DisplayName LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    return await _query(token, realm_id, f"SELECT * FROM Customer{where} MAXRESULTS {limit}")


async def _get_customer(token: str, args: Dict) -> str:
    return await _api(token, "GET", args["realm_id"], f"customer/{args['customer_id']}")


async def _create_customer(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"DisplayName": args["display_name"]}
    if args.get("email"):
        body["PrimaryEmailAddr"] = {"Address": args["email"]}
    if args.get("phone"):
        body["PrimaryPhone"] = {"FreeFormNumber": args["phone"]}
    if args.get("company_name"):
        body["CompanyName"] = args["company_name"]
    return await _api(token, "POST", args["realm_id"], "customer", body)


async def _query_invoices(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    conditions = []
    if args.get("customer_id"):
        conditions.append(f"CustomerRef = '{_esc(args['customer_id'])}'")
    if args.get("status"):
        # QBO uses Balance for paid/unpaid: Balance = '0' means paid
        pass  # complex filter, skip for simplicity
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return await _query(token, realm_id, f"SELECT * FROM Invoice{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}")


async def _get_invoice(token: str, args: Dict) -> str:
    return await _api(token, "GET", args["realm_id"], f"invoice/{args['invoice_id']}")


async def _create_invoice(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {
        "CustomerRef": {"value": args["customer_id"]},
    }
    lines = []
    if args.get("line_description") and args.get("line_amount"):
        lines.append({
            "Amount": float(args["line_amount"]),
            "DetailType": "SalesItemLineDetail",
            "Description": args["line_description"],
            "SalesItemLineDetail": {"Qty": 1, "UnitPrice": float(args["line_amount"])},
        })
    if lines:
        body["Line"] = lines
    if args.get("due_date"):
        body["DueDate"] = args["due_date"]
    return await _api(token, "POST", args["realm_id"], "invoice", body)


async def _send_invoice(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    invoice_id = args["invoice_id"]
    email = args.get("email", "")
    params = {"sendTo": email} if email else None
    return await _api(token, "POST", realm_id, f"invoice/{invoice_id}/send", params=params)


async def _query_payments(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    conditions = []
    if args.get("customer_id"):
        conditions.append(f"CustomerRef = '{_esc(args['customer_id'])}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return await _query(token, realm_id, f"SELECT * FROM Payment{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}")


async def _get_payment(token: str, args: Dict) -> str:
    return await _api(token, "GET", args["realm_id"], f"payment/{args['payment_id']}")


async def _query_items(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    where = f" WHERE Name LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    return await _query(token, realm_id, f"SELECT * FROM Item{where} MAXRESULTS {limit}")


async def _query_accounts(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 50), 1, 1000)
    return await _query(token, realm_id, f"SELECT * FROM Account MAXRESULTS {limit}")


async def _query_vendors(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    where = f" WHERE DisplayName LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    return await _query(token, realm_id, f"SELECT * FROM Vendor{where} MAXRESULTS {limit}")


async def _query_bills(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _clamp(args.get("limit", 20), 1, 1000)
    conditions = []
    if args.get("vendor_id"):
        conditions.append(f"VendorRef = '{_esc(args['vendor_id'])}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return await _query(token, realm_id, f"SELECT * FROM Bill{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}")


async def _run_report(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    report_name = args["report_name"]
    params: Dict[str, str] = {}
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    return await _api(token, "GET", realm_id, f"reports/{report_name}", params=params)


async def _custom_query(token: str, args: Dict) -> str:
    """Run a raw QBO SQL query."""
    return await _query(token, args["realm_id"], args["sql"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _esc(value: str) -> str:
    """Escape a string for use in QBO SQL LIKE / WHERE clauses.

    QBO escapes a literal single quote by doubling it (''), not with a
    backslash; backslash is only for the LIKE wildcards % and _.
    """
    return str(value).replace("'", "''").replace("%", "\\%").replace("_", "\\_")


def _clamp(value, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_REALM = _prop("QuickBooks Company/Realm ID (numeric string)")

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_company_info": {
        "description": "Get QuickBooks company info (name, address, fiscal year, etc.)",
        "properties": {"realm_id": _REALM},
        "required": ["realm_id"],
    },
    # Customers
    "query_customers": {
        "description": "Search/list QuickBooks customers",
        "properties": {
            "realm_id": _REALM,
            "name": _prop("Filter by display name (partial match)"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    "get_customer": {
        "description": "Get a QuickBooks customer by ID",
        "properties": {"realm_id": _REALM, "customer_id": _prop("Customer ID")},
        "required": ["realm_id", "customer_id"],
    },
    "create_customer": {
        "description": "Create a new QuickBooks customer",
        "properties": {
            "realm_id": _REALM,
            "display_name": _prop("Customer display name (must be unique)"),
            "email": _prop("Customer email"),
            "phone": _prop("Customer phone"),
            "company_name": _prop("Company name"),
        },
        "required": ["realm_id", "display_name"],
    },
    # Invoices
    "query_invoices": {
        "description": "Search/list QuickBooks invoices",
        "properties": {
            "realm_id": _REALM,
            "customer_id": _prop("Filter by customer ID"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    "get_invoice": {
        "description": "Get a QuickBooks invoice by ID",
        "properties": {"realm_id": _REALM, "invoice_id": _prop("Invoice ID")},
        "required": ["realm_id", "invoice_id"],
    },
    "create_invoice": {
        "description": "Create a QuickBooks invoice",
        "properties": {
            "realm_id": _REALM,
            "customer_id": _prop("Customer ID"),
            "line_description": _prop("Line item description"),
            "line_amount": _prop("Line item amount (e.g. 150.00)", "number"),
            "due_date": _prop("Due date (YYYY-MM-DD)"),
        },
        "required": ["realm_id", "customer_id"],
    },
    "send_invoice": {
        "description": "Email an invoice to the customer",
        "properties": {
            "realm_id": _REALM,
            "invoice_id": _prop("Invoice ID"),
            "email": _prop("Override recipient email (optional)"),
        },
        "required": ["realm_id", "invoice_id"],
    },
    # Payments
    "query_payments": {
        "description": "Search/list QuickBooks payments received",
        "properties": {
            "realm_id": _REALM,
            "customer_id": _prop("Filter by customer ID"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    "get_payment": {
        "description": "Get a QuickBooks payment by ID",
        "properties": {"realm_id": _REALM, "payment_id": _prop("Payment ID")},
        "required": ["realm_id", "payment_id"],
    },
    # Items
    "query_items": {
        "description": "Search/list QuickBooks items (products/services)",
        "properties": {
            "realm_id": _REALM,
            "name": _prop("Filter by item name (partial match)"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    # Chart of Accounts
    "query_accounts": {
        "description": "List QuickBooks chart of accounts",
        "properties": {
            "realm_id": _REALM,
            "limit": _prop("Max results (default: 50)", "integer"),
        },
        "required": ["realm_id"],
    },
    # Vendors
    "query_vendors": {
        "description": "Search/list QuickBooks vendors",
        "properties": {
            "realm_id": _REALM,
            "name": _prop("Filter by vendor name (partial match)"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    # Bills
    "query_bills": {
        "description": "Search/list QuickBooks bills (accounts payable)",
        "properties": {
            "realm_id": _REALM,
            "vendor_id": _prop("Filter by vendor ID"),
            "limit": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["realm_id"],
    },
    # Reports
    "run_report": {
        "description": "Run a QuickBooks financial report (ProfitAndLoss, BalanceSheet, CashFlow, etc.)",
        "properties": {
            "realm_id": _REALM,
            "report_name": _prop("Report name: ProfitAndLoss, BalanceSheet, CashFlow, TrialBalance, GeneralLedger, AgedReceivables, AgedPayables"),
            "start_date": _prop("Start date (YYYY-MM-DD)"),
            "end_date": _prop("End date (YYYY-MM-DD)"),
        },
        "required": ["realm_id", "report_name"],
    },
    # Custom query
    "custom_query": {
        "description": "Run a raw QBO SQL query (e.g. SELECT * FROM Employee MAXRESULTS 10)",
        "properties": {
            "realm_id": _REALM,
            "sql": _prop("QBO SQL query"),
        },
        "required": ["realm_id", "sql"],
    },
}

_HANDLERS = {
    "get_company_info": _get_company_info,
    "query_customers": _query_customers,
    "get_customer": _get_customer,
    "create_customer": _create_customer,
    "query_invoices": _query_invoices,
    "get_invoice": _get_invoice,
    "create_invoice": _create_invoice,
    "send_invoice": _send_invoice,
    "query_payments": _query_payments,
    "get_payment": _get_payment,
    "query_items": _query_items,
    "query_accounts": _query_accounts,
    "query_vendors": _query_vendors,
    "query_bills": _query_bills,
    "run_report": _run_report,
    "custom_query": _custom_query,
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
