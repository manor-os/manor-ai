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
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")

    spec = _TOOLS.get(name, {})
    try:
        token, configured_realm_id = _credentials(bearer_token)
    except ValueError as exc:
        return _error(str(exc))
    if not token:
        return _error("QuickBooks access token is missing. Connect QuickBooks first.")
    if configured_realm_id and _is_blank(arguments.get("realm_id")):
        arguments["realm_id"] = configured_realm_id

    missing = [p for p in spec.get("required", []) if _is_blank(arguments.get(p))]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    for parameter in spec.get("required", []):
        value = arguments.get(parameter)
        if isinstance(value, str):
            arguments[parameter] = value.strip()

    try:
        text = await handler(token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("QuickBooks MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── QBO API client ───────────────────────────────────────────────────────────

def quickbooks_base_url() -> str:
    """Resolve QBO's endpoint without allowing non-production to hit live data."""
    configured = (
        os.getenv("QUICKBOOKS_ENVIRONMENT", "").strip()
        or os.getenv("QBO_ENVIRONMENT", "").strip()
    ).lower()
    if configured:
        if configured in {"sandbox", "test"}:
            return _API_SANDBOX
        if configured in {"production", "prod", "live"}:
            return _API_PROD
        raise ValueError(
            "QUICKBOOKS_ENVIRONMENT (or legacy QBO_ENVIRONMENT) must be "
            "'sandbox' or 'production'."
        )

    manor_env = os.getenv("MANOR_ENV", "").strip().lower()
    return _API_PROD if manor_env in {"production", "prod", "live"} else _API_SANDBOX


async def _api(
    token: str,
    method: str,
    realm_id: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    token = str(token or "").strip()
    if _is_blank(token):
        raise ValueError("QuickBooks access token is missing.")
    base = quickbooks_base_url()
    url = f"{base}/{_path_segment(realm_id)}/{path.lstrip('/')}"
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
    return await _api(token, "GET", realm_id, f"companyinfo/{_path_segment(realm_id)}")


async def _query_customers(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=20)
    where = f" WHERE DisplayName LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    # QBO query fragments are fixed; values pass through _esc and limit is bounded.
    return await _query(token, realm_id, f"SELECT * FROM Customer{where} MAXRESULTS {limit}")  # nosec B608


async def _get_customer(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", args["realm_id"],
        f"customer/{_path_segment(args['customer_id'])}",
    )


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
    limit = _limit(args.get("limit"), default=20)
    conditions = []
    if args.get("customer_id"):
        conditions.append(f"CustomerRef = '{_esc(args['customer_id'])}'")
    if args.get("status"):
        # QBO uses Balance for paid/unpaid: Balance = '0' means paid
        pass  # complex filter, skip for simplicity
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    # QBO query fragments are fixed; values are escaped and LIMIT is bounded.
    query = f"SELECT * FROM Invoice{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}"  # nosec B608
    return await _query(token, realm_id, query)


async def _get_invoice(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", args["realm_id"],
        f"invoice/{_path_segment(args['invoice_id'])}",
    )


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
    return await _api(
        token, "POST", realm_id,
        f"invoice/{_path_segment(invoice_id)}/send", params=params,
    )


async def _void_invoice(token: str, args: Dict) -> str:
    invoice_id = args["invoice_id"]
    body = {"Id": invoice_id, "SyncToken": args["sync_token"]}
    return await _api(
        token, "POST", args["realm_id"],
        f"invoice/{_path_segment(invoice_id)}/void", body=body,
    )


async def _query_payments(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=20)
    conditions = []
    if args.get("customer_id"):
        conditions.append(f"CustomerRef = '{_esc(args['customer_id'])}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    # QBO query fragments are fixed; values are escaped and LIMIT is bounded.
    query = f"SELECT * FROM Payment{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}"  # nosec B608
    return await _query(token, realm_id, query)


async def _get_payment(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", args["realm_id"],
        f"payment/{_path_segment(args['payment_id'])}",
    )


async def _query_items(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=20)
    where = f" WHERE Name LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    return await _query(token, realm_id, f"SELECT * FROM Item{where} MAXRESULTS {limit}")  # nosec B608


async def _query_accounts(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=50)
    return await _query(token, realm_id, f"SELECT * FROM Account MAXRESULTS {limit}")  # nosec B608


async def _query_vendors(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=20)
    where = f" WHERE DisplayName LIKE '%{_esc(args['name'])}%'" if args.get("name") else ""
    return await _query(token, realm_id, f"SELECT * FROM Vendor{where} MAXRESULTS {limit}")  # nosec B608


async def _query_bills(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    limit = _limit(args.get("limit"), default=20)
    conditions = []
    if args.get("vendor_id"):
        conditions.append(f"VendorRef = '{_esc(args['vendor_id'])}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    # QBO query fragments are fixed; values are escaped and LIMIT is bounded.
    query = f"SELECT * FROM Bill{where} ORDERBY MetaData.CreateTime DESC MAXRESULTS {limit}"  # nosec B608
    return await _query(token, realm_id, query)


async def _run_report(token: str, args: Dict) -> str:
    realm_id = args["realm_id"]
    report_name = args["report_name"]
    params: Dict[str, str] = {}
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    return await _api(
        token, "GET", realm_id,
        f"reports/{_path_segment(report_name)}", params=params,
    )


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


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _credentials(value: str) -> tuple[str, str]:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw.startswith("{"):
        return raw, ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("QuickBooks credentials are malformed; reconnect QuickBooks.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("QuickBooks credentials must be a JSON object.")
    token = parsed.get("access_token")
    realm_id = parsed.get("realm_id")
    if token is not None and not isinstance(token, str):
        raise ValueError("QuickBooks access_token must be a string.")
    if realm_id is not None and not isinstance(realm_id, str):
        raise ValueError("QuickBooks realm_id must be a string.")
    return str(token or "").strip(), str(realm_id or "").strip()


def _path_segment(value: Any) -> str:
    return quote(str(value), safe="")


def _limit(value: Any, *, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("limit must be an integer between 1 and 1000")
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit must be an integer between 1 and 1000") from exc
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    return limit


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
    "void_invoice": {
        "description": "Void a QuickBooks invoice using its current SyncToken",
        "properties": {
            "realm_id": _REALM,
            "invoice_id": _prop("Invoice ID"),
            "sync_token": _prop("Current invoice SyncToken"),
        },
        "required": ["realm_id", "invoice_id", "sync_token"],
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
    "void_invoice": _void_invoice,
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
