"""Microsoft Excel MCP server — in-process MCP for the Workbook API.

Scopes used:
  - Files.ReadWrite     — open + write workbooks in OneDrive
  - Sites.ReadWrite.All — when the workbook lives in SharePoint

Auth: Microsoft Graph access_token (resolved via ``_ms_auth``).

Why this is a separate module from onedrive.py
──────────────────────────────────────────────
OneDrive treats Excel workbooks as opaque files (download / upload).
The dedicated Workbook API at ``/me/drive/items/{id}/workbook/...``
exposes the *live* spreadsheet model — read a range, append a row to
a table, update a single cell, run a calculation — without
download/parse/upload round-trips.

Tool surface mirrors what an agent doing data work actually needs:
  * Inspect: list worksheets, list tables, read used range, read named
    range, read range by A1 address
  * Append + edit: append rows to a table, write a range, update one
    cell, clear a range
  * Compute: trigger a workbook recalc

Out-of-scope (rarely needed for agent use):
  * Charts / pivots / shapes (UI ops)
  * Worksheet protection
  * Data validation rules
  * Formatting (cell colors, fonts) — possible but verbose; add later
    if a real use case demands it
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API = "https://graph.microsoft.com/v1.0"
_MAX_CHARS = 12_000


def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str, arguments: Dict[str, Any], bearer_token: str,
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("MS Excel MCP tool %s failed", name)
        return _error(str(exc))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── HTTP client ─────────────────────────────────────────────────────────────

async def _api(
    token: str, method: str, path: str,
    body: Optional[Dict] = None, params: Optional[Dict] = None,
    persist_changes: bool = True,
) -> str:
    """``persist_changes=True`` sends ``Workbook-Session-Id`` autocreate
    header so writes commit. Set False for read-only ops to skip a
    session creation round-trip."""
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # No Workbook-Session-Id header: for these workbook endpoints Graph
    # auto-persists changes in a transient session. A fabricated session id is
    # rejected ("session not found"), and a real one needs a createSession
    # round-trip we intentionally skip. persist_changes kept for call-site
    # read-vs-write intent / signature stability.
    _ = persist_changes
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, headers=headers, json=body, params=params or {})
    if resp.status_code == 401:
        raise RuntimeError("Excel auth failed. Reconnect Microsoft on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"Excel forbidden: {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code in (202, 204):
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Excel API error ({resp.status_code}): {resp.text[:300]}")
    if not resp.text:
        return json.dumps({"success": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


def _wb(file_id: str) -> str:
    """Workbook root path for a OneDrive item."""
    return f"me/drive/items/{file_id}/workbook"


# ── Tool handlers ───────────────────────────────────────────────────────────

# Worksheets

async def _list_worksheets(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"{_wb(args['file_id'])}/worksheets")


async def _add_worksheet(token: str, args: Dict) -> str:
    return await _api(
        token, "POST", f"{_wb(args['file_id'])}/worksheets/add",
        body={"name": args["name"]} if args.get("name") else None,
    )


async def _delete_worksheet(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}",
    )


async def _rename_worksheet(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}",
        body={"name": args["new_name"]},
    )


# Ranges

async def _read_range(token: str, args: Dict) -> str:
    """Read a range by A1 address (e.g. 'A1:C10'). Returns values + formulas + format."""
    return await _api(
        token, "GET",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}"
        f"/range(address='{args['address']}')",
        persist_changes=False,
    )


async def _read_used_range(token: str, args: Dict) -> str:
    """Read all populated cells on a worksheet — Graph computes the
    bounding rectangle automatically. Cheaper than guessing A1
    addresses when you don't know the data shape."""
    values_only = bool(args.get("values_only", False))
    suffix = "?$select=values" if values_only else ""
    return await _api(
        token, "GET",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}/usedRange{suffix}",
        persist_changes=False,
    )


async def _write_range(token: str, args: Dict) -> str:
    """PATCH a range with a 2D values array (rows × cols).

    The values array shape must match the address bounding box —
    Graph rejects shape mismatches with 400.
    """
    body = {"values": args["values"]}
    if args.get("formulas") is not None:
        body["formulas"] = args["formulas"]
    if args.get("number_format") is not None:
        body["numberFormat"] = args["number_format"]
    return await _api(
        token, "PATCH",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}"
        f"/range(address='{args['address']}')",
        body=body,
    )


async def _update_cell(token: str, args: Dict) -> str:
    """Convenience for the common 'set one cell' case.

    Equivalent to write_range with a 1×1 values array but easier to
    call from agent prompts ("set cell B5 to 42")."""
    return await _api(
        token, "PATCH",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}"
        f"/range(address='{args['address']}')",
        body={"values": [[args["value"]]]},
    )


async def _clear_range(token: str, args: Dict) -> str:
    """Clear contents / formats of a range."""
    apply_to = args.get("apply_to") or "Contents"  # Contents | Formats | All
    return await _api(
        token, "POST",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}"
        f"/range(address='{args['address']}')/clear",
        body={"applyTo": apply_to},
    )


# Tables (best for structured append-rows workflows)

async def _list_tables(token: str, args: Dict) -> str:
    """List Excel tables (the structured-data primitive — name, rows, columns)."""
    return await _api(
        token, "GET", f"{_wb(args['file_id'])}/tables",
        persist_changes=False,
    )


async def _get_table_rows(token: str, args: Dict) -> str:
    """Read a table's data rows."""
    params: Dict[str, Any] = {"$top": min(int(args.get("top") or 100), 1000)}
    return await _api(
        token, "GET",
        f"{_wb(args['file_id'])}/tables/{quote(args['table'])}/rows",
        params=params, persist_changes=False,
    )


async def _add_table_rows(token: str, args: Dict) -> str:
    """Append rows to an Excel table — the most common agent op
    (writing daily reports, logging events, populating a CRM sheet)."""
    body = {"values": args["values"]}
    if args.get("index") is not None:
        body["index"] = int(args["index"])
    return await _api(
        token, "POST",
        f"{_wb(args['file_id'])}/tables/{quote(args['table'])}/rows/add",
        body=body,
    )


async def _create_table(token: str, args: Dict) -> str:
    """Promote a range into a structured table.

    Tables auto-grow when add_table_rows appends — much more agent-
    friendly than writing into a raw range that needs manual bound
    tracking."""
    body = {
        "address": args["address"],
        "hasHeaders": bool(args.get("has_headers", True)),
    }
    return await _api(
        token, "POST",
        f"{_wb(args['file_id'])}/worksheets/{quote(args['worksheet'])}/tables/add",
        body=body,
    )


# Computation

async def _calculate(token: str, args: Dict) -> str:
    """Trigger a workbook recalc. Use sparingly — Graph recalcs
    on most writes anyway. Pass ``calculation_type=Full`` to force
    a full recomputation regardless of dependencies."""
    return await _api(
        token, "POST", f"{_wb(args['file_id'])}/application/calculate",
        body={"calculationType": args.get("calculation_type") or "Recalculate"},
    )


# Named ranges

async def _list_named_items(token: str, args: Dict) -> str:
    """List named ranges (a.k.a. Defined Names)."""
    return await _api(
        token, "GET", f"{_wb(args['file_id'])}/names",
        persist_changes=False,
    )


async def _get_named_item_range(token: str, args: Dict) -> str:
    """Read the range a named item points at."""
    return await _api(
        token, "GET",
        f"{_wb(args['file_id'])}/names/{quote(args['name'])}/range",
        persist_changes=False,
    )


# ── Tool definitions ────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # Worksheets
    "list_worksheets": {
        "description": "List worksheets in a workbook.",
        "properties": {"file_id": _prop("OneDrive item ID of the .xlsx workbook")},
        "required": ["file_id"],
    },
    "add_worksheet": {
        "description": "Add a new worksheet to a workbook.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "name": _prop("Optional sheet name (Excel auto-names if omitted)"),
        },
        "required": ["file_id"],
    },
    "delete_worksheet": {
        "description": "Delete a worksheet by name or id.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
        },
        "required": ["file_id", "worksheet"],
    },
    "rename_worksheet": {
        "description": "Rename a worksheet.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Current worksheet name or id"),
            "new_name": _prop("New name"),
        },
        "required": ["file_id", "worksheet", "new_name"],
    },

    # Ranges
    "read_range": {
        "description": (
            "Read a range by A1 address (e.g. 'A1:C10'). Returns values, "
            "formulas, formats. Use read_used_range when you don't know "
            "the data shape."
        ),
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "address": _prop("A1 range, e.g. 'A1:C10' or 'B5'"),
        },
        "required": ["file_id", "worksheet", "address"],
    },
    "read_used_range": {
        "description": (
            "Read every populated cell on a worksheet — Graph computes "
            "the bounding rectangle. Pass values_only=true to skip "
            "formulas/formats and shrink the response."
        ),
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "values_only": _prop("Return only the values array (default false)", "boolean"),
        },
        "required": ["file_id", "worksheet"],
    },
    "write_range": {
        "description": (
            "Write a 2D values array to a range. The array shape (rows × cols) "
            "must match the address bounding box — Graph 400s otherwise."
        ),
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "address": _prop("A1 range to write into, e.g. 'A1:C3'"),
            "values": _prop(
                "2D array, rows × cols. Values can be strings / numbers / null.",
                "array", items={"type": "array"},
            ),
            "formulas": _prop(
                "Optional 2D array of formulas (Excel re-evaluates them).",
                "array", items={"type": "array"},
            ),
            "number_format": _prop(
                "Optional 2D array of numberFormat strings (e.g. '0.00%').",
                "array", items={"type": "array"},
            ),
        },
        "required": ["file_id", "worksheet", "address", "values"],
    },
    "update_cell": {
        "description": "Convenience: write a single value to one cell (use write_range for bulk).",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "address": _prop("Single cell, e.g. 'B5'"),
            "value": _prop("New cell value (string / number / null)", "string"),
        },
        "required": ["file_id", "worksheet", "address", "value"],
    },
    "clear_range": {
        "description": "Clear contents / formats of a range. apply_to: Contents | Formats | All.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "address": _prop("A1 range to clear"),
            "apply_to": _prop("Contents | Formats | All (default Contents)"),
        },
        "required": ["file_id", "worksheet", "address"],
    },

    # Tables
    "list_tables": {
        "description": "List Excel tables in a workbook.",
        "properties": {"file_id": _prop("Workbook item ID")},
        "required": ["file_id"],
    },
    "get_table_rows": {
        "description": "Read all data rows from a table.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "table": _prop("Table name or id"),
            "top": _prop("Max rows (default 100, max 1000)", "integer"),
        },
        "required": ["file_id", "table"],
    },
    "add_table_rows": {
        "description": (
            "Append rows to a table. Tables auto-grow — best primitive "
            "for agent reporting / logging workflows."
        ),
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "table": _prop("Table name or id"),
            "values": _prop(
                "2D array of new rows (rows × table-cols). Column count must match the table.",
                "array", items={"type": "array"},
            ),
            "index": _prop("Optional 0-based insertion index (default: append at end)", "integer"),
        },
        "required": ["file_id", "table", "values"],
    },
    "create_table": {
        "description": (
            "Promote a range into a structured Excel table. Once a "
            "range becomes a table, add_table_rows can append data "
            "and the table auto-grows."
        ),
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "worksheet": _prop("Worksheet name or id"),
            "address": _prop("Range to promote, e.g. 'A1:D10'"),
            "has_headers": _prop("First row contains column headers (default true)", "boolean"),
        },
        "required": ["file_id", "worksheet", "address"],
    },

    # Computation
    "calculate": {
        "description": "Trigger workbook recalculation.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "calculation_type": _prop("Recalculate | Full | FullRebuild (default Recalculate)"),
        },
        "required": ["file_id"],
    },

    # Named ranges
    "list_named_items": {
        "description": "List named ranges (Defined Names) in a workbook.",
        "properties": {"file_id": _prop("Workbook item ID")},
        "required": ["file_id"],
    },
    "get_named_item_range": {
        "description": "Read the range bound to a named item.",
        "properties": {
            "file_id": _prop("Workbook item ID"),
            "name": _prop("Named item / Defined Name"),
        },
        "required": ["file_id", "name"],
    },
}


_HANDLERS = {
    "list_worksheets": _list_worksheets,
    "add_worksheet": _add_worksheet,
    "delete_worksheet": _delete_worksheet,
    "rename_worksheet": _rename_worksheet,
    "read_range": _read_range,
    "read_used_range": _read_used_range,
    "write_range": _write_range,
    "update_cell": _update_cell,
    "clear_range": _clear_range,
    "list_tables": _list_tables,
    "get_table_rows": _get_table_rows,
    "add_table_rows": _add_table_rows,
    "create_table": _create_table,
    "calculate": _calculate,
    "list_named_items": _list_named_items,
    "get_named_item_range": _get_named_item_range,
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
