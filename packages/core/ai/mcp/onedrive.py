"""OneDrive MCP server — in-process MCP for Microsoft Graph /me/drive.

Scopes used:
  - Files.Read           — read-only access
  - Files.ReadWrite      — full file CRUD
  - Files.ReadWrite.All  — also reach SharePoint sites you have access to
  - Sites.ReadWrite.All  — only when an agent needs Sites/Lists beyond the
                           personal drive

Auth: Microsoft Graph access_token (resolved via ``_ms_auth``).

Tool surface mirrors google_drive.py — list / get / read / search /
create / move / rename / share / permissions / revisions / copy.
Excludes long-tail features (versions/history beyond simple list,
SharePoint deep-linking) — agents in Tier 1 use cases don't need them.
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


# ── MCP Protocol ─────────────────────────────────────────────────────────────

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
        logger.exception("OneDrive MCP tool %s failed", name)
        return _error(str(exc))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── Microsoft Graph client ──────────────────────────────────────────────────

async def _api(
    token: str, method: str, path: str,
    body: Optional[Dict] = None, params: Optional[Dict] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, url, headers=headers, json=body, params=params or {},
        )
    if resp.status_code == 401:
        raise RuntimeError("OneDrive auth failed. Reconnect Microsoft on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"OneDrive forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code in (202, 204):
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"OneDrive API error ({resp.status_code}): {resp.text[:300]}")
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


async def _api_raw(
    token: str, path: str,
    *, headers_extra: Optional[Dict[str, str]] = None,
) -> str:
    """Download raw bytes (returned as text — base64 encode upstream if binary)."""
    headers = {"Authorization": f"Bearer {token}"}
    if headers_extra:
        headers.update(headers_extra)
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    if not resp.is_success:
        raise RuntimeError(f"OneDrive download error ({resp.status_code}): {resp.text[:300]}")
    text = resp.text
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + f"\n… (truncated, {len(text)} total chars)"
    return text


async def _api_upload_text(
    token: str, path: str, content: str,
    *, content_type: str = "text/plain",
) -> str:
    """PUT raw bytes to a file path. Used by ``upload_text_file``."""
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.put(url, headers=headers, content=content.encode("utf-8"))
    if not resp.is_success:
        raise RuntimeError(f"OneDrive upload error ({resp.status_code}): {resp.text[:300]}")
    try:
        return json.dumps(resp.json(), ensure_ascii=False, indent=2, default=str)
    except Exception:
        return resp.text[:_MAX_CHARS]


# ── Tool handlers ───────────────────────────────────────────────────────────

# Browse + read

async def _list_files(token: str, args: Dict) -> str:
    """List children of a folder. Default: drive root. Pass folder_id
    or path (path takes precedence)."""
    path = args.get("path")
    folder_id = args.get("folder_id")
    if path:
        endpoint = f"me/drive/root:/{quote(path.strip('/'))}:/children"
    elif folder_id:
        endpoint = f"me/drive/items/{folder_id}/children"
    else:
        endpoint = "me/drive/root/children"
    params: Dict[str, Any] = {
        "$top": min(int(args.get("top") or 50), 200),
        "$orderby": args.get("order_by") or "lastModifiedDateTime DESC",
    }
    if args.get("select"):
        params["$select"] = args["select"]
    return await _api(token, "GET", endpoint, params=params)


async def _get_file(token: str, args: Dict) -> str:
    """Item metadata by id."""
    return await _api(token, "GET", f"me/drive/items/{args['file_id']}")


async def _get_file_by_path(token: str, args: Dict) -> str:
    """Item metadata resolved by path (e.g. ``Documents/Reports/2026.docx``)."""
    return await _api(
        token, "GET", f"me/drive/root:/{quote(args['path'].strip('/'))}",
    )


async def _read_file(token: str, args: Dict) -> str:
    """Download text content of a file. For Office docs (Word / Excel /
    PowerPoint) use the dedicated MCPs (ms_excel) or convert via the
    ``format`` query param (e.g. ``format=pdf`` returns a PDF render).

    Plain text / Markdown / source code files come back as-is."""
    fid = args["file_id"]
    fmt = args.get("format")
    suffix = f"?format={fmt}" if fmt else ""
    return await _api_raw(token, f"me/drive/items/{fid}/content{suffix}")


async def _search_files(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"me/drive/root/search(q='{quote(args['query'])}')",
        params={"$top": min(int(args.get("top") or 25), 100)},
    )


# Create / modify

async def _upload_text_file(token: str, args: Dict) -> str:
    """Upload a small text file (≤4 MB). For larger / binary files
    use Manor's general file-upload tools — Graph's resumable-upload
    flow isn't worth wrapping here."""
    name = args["name"]
    content = args.get("content") or ""
    folder_id = args.get("folder_id")
    if folder_id:
        path = f"me/drive/items/{folder_id}:/{quote(name)}:/content"
    else:
        path = f"me/drive/root:/{quote(name)}:/content"
    return await _api_upload_text(
        token, path, content,
        content_type=args.get("content_type") or "text/plain",
    )


async def _create_folder(token: str, args: Dict) -> str:
    parent_id = args.get("parent_folder_id")
    body = {
        "name": args["name"],
        "folder": {},
        "@microsoft.graph.conflictBehavior": args.get("conflict_behavior") or "rename",
    }
    if parent_id:
        path = f"me/drive/items/{parent_id}/children"
    else:
        path = "me/drive/root/children"
    return await _api(token, "POST", path, body=body)


async def _copy_file(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    if args.get("name"):
        body["name"] = args["name"]
    if args.get("destination_folder_id"):
        body["parentReference"] = {"id": args["destination_folder_id"]}
    return await _api(token, "POST", f"me/drive/items/{args['file_id']}/copy", body=body)


async def _move_file(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH", f"me/drive/items/{args['file_id']}",
        body={"parentReference": {"id": args["destination_folder_id"]}},
    )


async def _rename_file(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH", f"me/drive/items/{args['file_id']}",
        body={"name": args["new_name"]},
    )


async def _delete_file(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"me/drive/items/{args['file_id']}")


# Permissions / sharing

async def _create_share_link(token: str, args: Dict) -> str:
    """Mint a view / edit link. ``type`` = view | edit | embed.
    ``scope`` = anonymous | organization. Anonymous links may be
    blocked by tenant policy."""
    body: Dict[str, Any] = {
        "type": args.get("type") or "view",
        "scope": args.get("scope") or "anonymous",
    }
    if args.get("expires_at"):
        body["expirationDateTime"] = args["expires_at"]
    return await _api(
        token, "POST", f"me/drive/items/{args['file_id']}/createLink", body=body,
    )


async def _invite(token: str, args: Dict) -> str:
    """Grant per-user access (more granular than createLink)."""
    raw = args["recipients"]
    emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
    body = {
        "recipients": [{"email": e} for e in emails],
        "roles": [args.get("role") or "read"],
        "requireSignIn": bool(args.get("require_signin", True)),
        "sendInvitation": bool(args.get("send_invitation", True)),
    }
    if args.get("message"):
        body["message"] = args["message"]
    return await _api(
        token, "POST", f"me/drive/items/{args['file_id']}/invite", body=body,
    )


async def _list_permissions(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"me/drive/items/{args['file_id']}/permissions")


async def _delete_permission(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"me/drive/items/{args['file_id']}/permissions/{args['permission_id']}",
    )


# Versions / drive info

async def _list_versions(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"me/drive/items/{args['file_id']}/versions")


async def _restore_version(token: str, args: Dict) -> str:
    return await _api(
        token, "POST",
        f"me/drive/items/{args['file_id']}/versions/{args['version_id']}/restoreVersion",
    )


async def _get_drive_info(token: str, args: Dict) -> str:
    """Quota, owner, drive type."""
    return await _api(token, "GET", "me/drive")


async def _get_recent_files(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", "me/drive/recent",
        params={"$top": min(int(args.get("top") or 25), 100)},
    )


async def _get_shared_with_me(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", "me/drive/sharedWithMe",
        params={"$top": min(int(args.get("top") or 25), 100)},
    )


# ── Tool definitions ────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # Browse + read
    "list_files": {
        "description": "List children of a OneDrive folder. Default: drive root. Pass folder_id, or path like 'Documents/Reports'.",
        "properties": {
            "folder_id": _prop("Item ID of the folder (mutually exclusive with path)"),
            "path": _prop("Folder path relative to drive root, e.g. 'Documents/Reports'"),
            "top": _prop("Max results (default 50, max 200)", "integer"),
            "select": _prop("Comma-separated $select fields"),
            "order_by": _prop("OData $orderby (default 'lastModifiedDateTime DESC')"),
        },
        "required": [],
    },
    "get_file": {
        "description": "Get a OneDrive item (file/folder) metadata by id.",
        "properties": {"file_id": _prop("Drive item ID")},
        "required": ["file_id"],
    },
    "get_file_by_path": {
        "description": "Resolve a OneDrive item by its path (e.g. 'Documents/Reports/2026.docx').",
        "properties": {"path": _prop("Path relative to drive root")},
        "required": ["path"],
    },
    "read_file": {
        "description": (
            "Download a file's content as text. For Office docs use the "
            "ms_excel module or pass format=pdf for a PDF render. "
            "Plain text / Markdown / code files come back as-is."
        ),
        "properties": {
            "file_id": _prop("Drive item ID"),
            "format": _prop("Optional Graph format hint, e.g. 'pdf' to get a PDF render"),
        },
        "required": ["file_id"],
    },
    "search_files": {
        "description": "Full-text search across the user's OneDrive.",
        "properties": {
            "query": _prop("Search term"),
            "top": _prop("Max results (default 25, max 100)", "integer"),
        },
        "required": ["query"],
    },

    # Create / modify
    "upload_text_file": {
        "description": "Upload a small (≤4MB) text/UTF-8 file. For larger or binary uploads, route through the Manor file-upload pipeline.",
        "properties": {
            "name": _prop("Filename incl. extension"),
            "content": _prop("File content as UTF-8 text"),
            "folder_id": _prop("Optional parent folder id (default: drive root)"),
            "content_type": _prop("MIME type (default text/plain)"),
        },
        "required": ["name", "content"],
    },
    "create_folder": {
        "description": "Create a new folder.",
        "properties": {
            "name": _prop("Folder name"),
            "parent_folder_id": _prop("Parent folder id (default: drive root)"),
            "conflict_behavior": _prop("rename | replace | fail (default: rename)"),
        },
        "required": ["name"],
    },
    "copy_file": {
        "description": "Duplicate a file or folder. Optional rename + new parent.",
        "properties": {
            "file_id": _prop("Source item ID"),
            "name": _prop("Optional new name (defaults to original)"),
            "destination_folder_id": _prop("Optional destination folder id"),
        },
        "required": ["file_id"],
    },
    "move_file": {
        "description": "Move an item to a different folder.",
        "properties": {
            "file_id": _prop("Item ID"),
            "destination_folder_id": _prop("Target folder id"),
        },
        "required": ["file_id", "destination_folder_id"],
    },
    "rename_file": {
        "description": "Rename a file or folder.",
        "properties": {
            "file_id": _prop("Item ID"),
            "new_name": _prop("New name"),
        },
        "required": ["file_id", "new_name"],
    },
    "delete_file": {
        "description": "Move a OneDrive item to recycle bin (recoverable from web UI).",
        "properties": {"file_id": _prop("Item ID")},
        "required": ["file_id"],
    },

    # Permissions / sharing
    "create_share_link": {
        "description": "Mint a view / edit / embed share link. Anonymous links may be blocked by tenant policy.",
        "properties": {
            "file_id": _prop("Item ID"),
            "type": _prop("view | edit | embed (default: view)"),
            "scope": _prop("anonymous | organization (default: anonymous)"),
            "expires_at": _prop("Optional ISO 8601 expiry"),
        },
        "required": ["file_id"],
    },
    "invite": {
        "description": "Grant per-user access by email (more granular than create_share_link).",
        "properties": {
            "file_id": _prop("Item ID"),
            "recipients": _prop("Email addresses (list, or comma-separated string)"),
            "role": _prop("read | write (default: read)"),
            "require_signin": _prop("Recipients must sign in (default: true)", "boolean"),
            "send_invitation": _prop("Email the recipients a notification (default: true)", "boolean"),
            "message": _prop("Optional invitation message"),
        },
        "required": ["file_id", "recipients"],
    },
    "list_permissions": {
        "description": "List all permissions on a OneDrive item.",
        "properties": {"file_id": _prop("Item ID")},
        "required": ["file_id"],
    },
    "delete_permission": {
        "description": "Revoke a permission entry.",
        "properties": {
            "file_id": _prop("Item ID"),
            "permission_id": _prop("Permission ID from list_permissions"),
        },
        "required": ["file_id", "permission_id"],
    },

    # Versions
    "list_versions": {
        "description": "List a file's version history.",
        "properties": {"file_id": _prop("Item ID")},
        "required": ["file_id"],
    },
    "restore_version": {
        "description": "Restore a file to a prior version.",
        "properties": {
            "file_id": _prop("Item ID"),
            "version_id": _prop("Version ID from list_versions"),
        },
        "required": ["file_id", "version_id"],
    },

    # Drive info
    "get_drive_info": {
        "description": "Get the user's drive metadata (quota, owner, drive type).",
        "properties": {},
        "required": [],
    },
    "get_recent_files": {
        "description": "List the user's recently used files (across drives).",
        "properties": {"top": _prop("Max (default 25, max 100)", "integer")},
        "required": [],
    },
    "get_shared_with_me": {
        "description": "List items other people have shared with the user.",
        "properties": {"top": _prop("Max (default 25, max 100)", "integer")},
        "required": [],
    },
}


_HANDLERS = {
    "list_files": _list_files,
    "get_file": _get_file,
    "get_file_by_path": _get_file_by_path,
    "read_file": _read_file,
    "search_files": _search_files,
    "upload_text_file": _upload_text_file,
    "create_folder": _create_folder,
    "copy_file": _copy_file,
    "move_file": _move_file,
    "rename_file": _rename_file,
    "delete_file": _delete_file,
    "create_share_link": _create_share_link,
    "invite": _invite,
    "list_permissions": _list_permissions,
    "delete_permission": _delete_permission,
    "list_versions": _list_versions,
    "restore_version": _restore_version,
    "get_drive_info": _get_drive_info,
    "get_recent_files": _get_recent_files,
    "get_shared_with_me": _get_shared_with_me,
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
