"""
Google Drive MCP server — in-process MCP for Google Drive API v3.

Scopes used:
  - https://www.googleapis.com/auth/drive (full access)
  - or https://www.googleapis.com/auth/drive.readonly (read-only)
  - or https://www.googleapis.com/auth/drive.file (files created by app)

Auth: Google OAuth access_token (from entity integration config, auto-refreshed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/drive/v3"
_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
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

    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Google Drive MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Google Drive API client ──────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            method, url, headers=headers,
            json=body, params=params or {},
        )

    if resp.status_code == 401:
        raise RuntimeError("Google Drive auth failed. Reconnect Google on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"Google Drive forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code == 204:
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Google Drive API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


async def _api_raw(token: str, url: str) -> str:
    """Download raw file content (for export/download)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 401:
        raise RuntimeError("Google Drive auth failed. Reconnect Google on the Integration page.")
    if not resp.is_success:
        raise RuntimeError(f"Google Drive API error ({resp.status_code}): {resp.text[:300]}")

    text = resp.text
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + f"\n… (truncated, {len(text)} total chars)"
    return text


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _q_escape(value: str) -> str:
    """Escape a value for a Drive query string literal. Drive has no
    parameterization, so an unescaped ``'`` breaks the query (or injects);
    escape backslash first, then the single quote."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


async def _list_files(token: str, args: Dict) -> str:
    """List files and folders in Google Drive."""
    params: Dict[str, Any] = {
        "pageSize": min(int(args.get("max_results") or 20), 100),
        "fields": "files(id,name,mimeType,modifiedTime,size,parents,webViewLink),nextPageToken",
        "orderBy": "modifiedTime desc",
    }
    q_parts = []
    if args.get("query"):
        q_parts.append(f"fullText contains '{_q_escape(args['query'])}'")
    if args.get("folder_id"):
        q_parts.append(f"'{_q_escape(args['folder_id'])}' in parents")
    if args.get("mime_type"):
        q_parts.append(f"mimeType = '{_q_escape(args['mime_type'])}'")
    if not args.get("include_trashed"):
        q_parts.append("trashed = false")
    if q_parts:
        params["q"] = " and ".join(q_parts)

    return await _api(token, "GET", "files", params=params)


async def _get_file(token: str, args: Dict) -> str:
    """Get file metadata."""
    fields = "id,name,mimeType,modifiedTime,createdTime,size,parents,webViewLink,description,owners"
    return await _api(token, "GET", f"files/{args['file_id']}", params={"fields": fields})


async def _read_file(token: str, args: Dict) -> str:
    """Read file content. For Google Docs/Sheets/Slides, exports as text. For others, downloads."""
    file_id = args["file_id"]

    # First get metadata to determine type
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_API}/files/{file_id}",
            params={"fields": "mimeType,name"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if not resp.is_success:
        return f"Failed to get file info: {resp.status_code}"
    meta = resp.json()
    mime = meta.get("mimeType", "")

    # Google Workspace files need export
    export_map = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    if mime in export_map:
        export_mime = args.get("export_format") or export_map[mime]
        url = f"{_API}/files/{file_id}/export?mimeType={quote(export_mime)}"
        return await _api_raw(token, url)
    else:
        url = f"{_API}/files/{file_id}?alt=media"
        return await _api_raw(token, url)


async def _search_files(token: str, args: Dict) -> str:
    """Search files by name or content."""
    query = args["query"]
    params: Dict[str, Any] = {
        "q": f"fullText contains '{_q_escape(query)}' and trashed = false",
        "pageSize": min(int(args.get("max_results") or 20), 100),
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink),nextPageToken",
        "orderBy": "modifiedTime desc",
    }
    return await _api(token, "GET", "files", params=params)


async def _create_file(token: str, args: Dict) -> str:
    """Create a new file (metadata only — for Google Docs, Sheets, etc.)."""
    body: Dict[str, Any] = {"name": args["name"]}
    if args.get("mime_type"):
        body["mimeType"] = args["mime_type"]
    if args.get("folder_id"):
        body["parents"] = [args["folder_id"]]
    if args.get("description"):
        body["description"] = args["description"]
    return await _api(token, "POST", "files", body)


async def _create_folder(token: str, args: Dict) -> str:
    """Create a new folder."""
    body: Dict[str, Any] = {
        "name": args["name"],
        "mimeType": "application/vnd.google-apps.folder",
    }
    if args.get("parent_folder_id"):
        body["parents"] = [args["parent_folder_id"]]
    return await _api(token, "POST", "files", body)


async def _move_file(token: str, args: Dict) -> str:
    """Move a file to a different folder."""
    file_id = args["file_id"]
    new_parent = args["folder_id"]

    # Get current parents
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_API}/files/{file_id}",
            params={"fields": "parents"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if not resp.is_success:
        return f"Failed to get file parents: {resp.status_code}"
    current_parents = ",".join(resp.json().get("parents", []))

    return await _api(
        token, "PATCH", f"files/{file_id}",
        params={"addParents": new_parent, "removeParents": current_parents},
    )


async def _rename_file(token: str, args: Dict) -> str:
    """Rename a file."""
    return await _api(token, "PATCH", f"files/{args['file_id']}", {"name": args["new_name"]})


async def _delete_file(token: str, args: Dict) -> str:
    """Move a file to trash."""
    return await _api(token, "PATCH", f"files/{args['file_id']}", {"trashed": True})


async def _share_file(token: str, args: Dict) -> str:
    """Share a file with a user or make it public."""
    file_id = args["file_id"]
    role = args.get("role") or "reader"  # reader, writer, commenter

    body: Dict[str, Any] = {"role": role}
    if args.get("email"):
        body["type"] = "user"
        body["emailAddress"] = args["email"]
    else:
        body["type"] = "anyone"

    return await _api(token, "POST", f"files/{file_id}/permissions", body)


async def _get_about(token: str, args: Dict) -> str:
    """Get Drive storage info and user details."""
    return await _api(token, "GET", "about", params={"fields": "user,storageQuota"})


async def _copy_file(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    if args.get("name"):
        body["name"] = args["name"]
    if args.get("folder_id"):
        body["parents"] = [args["folder_id"]]
    return await _api(token, "POST", f"files/{args['file_id']}/copy", body=body)


async def _restore_file(token: str, args: Dict) -> str:
    """Pull a file out of Trash."""
    return await _api(token, "PATCH", f"files/{args['file_id']}", {"trashed": False})


async def _delete_file_permanent(token: str, args: Dict) -> str:
    """HARD-delete (skip trash). Irreversible — use delete_file for the
    trash-then-restore safety net."""
    return await _api(token, "DELETE", f"files/{args['file_id']}")


async def _empty_trash(token: str, args: Dict) -> str:
    """Permanently delete every file in the user's Trash."""
    return await _api(token, "DELETE", "files/trash")


# ── Permissions ─────────────────────────────────────────────────────────────

async def _list_permissions(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"files/{args['file_id']}/permissions",
        params={"fields": "permissions(id,type,role,emailAddress,displayName,domain,allowFileDiscovery)"},
    )


async def _update_permission(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"role": args["role"]}
    return await _api(
        token, "PATCH",
        f"files/{args['file_id']}/permissions/{args['permission_id']}",
        body=body,
    )


async def _delete_permission(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"files/{args['file_id']}/permissions/{args['permission_id']}",
    )


# ── Revisions ───────────────────────────────────────────────────────────────

async def _list_revisions(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"files/{args['file_id']}/revisions",
        params={"fields": "revisions(id,modifiedTime,lastModifyingUser,size,keepForever)"},
    )


async def _get_revision(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"files/{args['file_id']}/revisions/{args['revision_id']}",
    )


async def _delete_revision(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"files/{args['file_id']}/revisions/{args['revision_id']}",
    )


# ── Comments ────────────────────────────────────────────────────────────────

_COMMENT_FIELDS = "id,content,htmlContent,createdTime,modifiedTime,resolved,author,quotedFileContent,replies"


async def _list_comments(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"files/{args['file_id']}/comments",
        params={"fields": f"comments({_COMMENT_FIELDS})"},
    )


async def _create_comment(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"content": args["content"]}
    if args.get("quoted_text"):
        body["quotedFileContent"] = {"value": args["quoted_text"]}
    return await _api(
        token, "POST", f"files/{args['file_id']}/comments",
        body=body, params={"fields": _COMMENT_FIELDS},
    )


async def _resolve_comment(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH",
        f"files/{args['file_id']}/comments/{args['comment_id']}",
        body={"resolved": True},
        params={"fields": _COMMENT_FIELDS},
    )


async def _delete_comment(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"files/{args['file_id']}/comments/{args['comment_id']}",
    )


async def _create_reply(token: str, args: Dict) -> str:
    return await _api(
        token, "POST",
        f"files/{args['file_id']}/comments/{args['comment_id']}/replies",
        body={"content": args["content"]},
        params={"fields": "id,content,createdTime,author"},
    )


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_files": {
        "description": "List files and folders in Google Drive",
        "properties": {
            "folder_id": _prop("Folder ID to list contents of (default: root)"),
            "query": _prop("Search query to filter files by content"),
            "mime_type": _prop("Filter by MIME type (e.g. application/vnd.google-apps.document)"),
            "max_results": _prop("Max results (default: 20, max: 100)", "integer"),
            "include_trashed": _prop("Include trashed files (default: false)", "boolean"),
        },
        "required": [],
    },
    "get_file": {
        "description": "Get file metadata (name, type, size, link, etc.)",
        "properties": {
            "file_id": _prop("Google Drive file ID"),
        },
        "required": ["file_id"],
    },
    "read_file": {
        "description": "Read file content. Google Docs export as text, Sheets as CSV, others download raw",
        "properties": {
            "file_id": _prop("Google Drive file ID"),
            "export_format": _prop("Export MIME type override (e.g. text/plain, text/csv, application/pdf)"),
        },
        "required": ["file_id"],
    },
    "search_files": {
        "description": "Search files by name or content in Google Drive",
        "properties": {
            "query": _prop("Search query"),
            "max_results": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["query"],
    },
    "create_file": {
        "description": "Create a new file (Google Doc, Sheet, etc.) — metadata only",
        "properties": {
            "name": _prop("File name"),
            "mime_type": _prop("MIME type (e.g. application/vnd.google-apps.document for Google Doc, application/vnd.google-apps.spreadsheet for Sheet)"),
            "folder_id": _prop("Parent folder ID"),
            "description": _prop("File description"),
        },
        "required": ["name"],
    },
    "create_folder": {
        "description": "Create a new folder in Google Drive",
        "properties": {
            "name": _prop("Folder name"),
            "parent_folder_id": _prop("Parent folder ID (default: root)"),
        },
        "required": ["name"],
    },
    "move_file": {
        "description": "Move a file to a different folder",
        "properties": {
            "file_id": _prop("File ID to move"),
            "folder_id": _prop("Destination folder ID"),
        },
        "required": ["file_id", "folder_id"],
    },
    "rename_file": {
        "description": "Rename a file in Google Drive",
        "properties": {
            "file_id": _prop("File ID to rename"),
            "new_name": _prop("New file name"),
        },
        "required": ["file_id", "new_name"],
    },
    "delete_file": {
        "description": "Move a file to trash in Google Drive",
        "properties": {
            "file_id": _prop("File ID to trash"),
        },
        "required": ["file_id"],
    },
    "share_file": {
        "description": "Share a file — grant access to a user by email or make it public",
        "properties": {
            "file_id": _prop("File ID to share"),
            "email": _prop("Email address to share with (omit for public link)"),
            "role": _prop("Permission role: reader, writer, or commenter (default: reader)"),
        },
        "required": ["file_id"],
    },
    "get_about": {
        "description": "Get Google Drive storage info and authenticated user details",
        "properties": {},
        "required": [],
    },
    "copy_file": {
        "description": (
            "Duplicate a file. Useful for templating: copy a Doc / Sheet "
            "and rename / refile in one step."
        ),
        "properties": {
            "file_id": _prop("Source file ID"),
            "name": _prop("Optional new name (defaults to 'Copy of …')"),
            "folder_id": _prop("Optional destination folder"),
        },
        "required": ["file_id"],
    },
    "restore_file": {
        "description": "Restore a file from Trash (un-trash).",
        "properties": {"file_id": _prop("File ID")},
        "required": ["file_id"],
    },
    "delete_file_permanent": {
        "description": (
            "Permanently delete a file (skip Trash). NOT reversible — "
            "use delete_file for the safe two-stage path."
        ),
        "properties": {"file_id": _prop("File ID")},
        "required": ["file_id"],
    },
    "empty_trash": {
        "description": "Permanently delete every file in the user's Trash. NOT reversible.",
        "properties": {},
        "required": [],
    },
    # ── Permissions ──
    "list_permissions": {
        "description": "List all permissions on a file (who has access at what role).",
        "properties": {"file_id": _prop("File ID")},
        "required": ["file_id"],
    },
    "update_permission": {
        "description": "Change a single permission's role on a file.",
        "properties": {
            "file_id": _prop("File ID"),
            "permission_id": _prop("Permission ID from list_permissions"),
            "role": _prop("New role: reader | writer | commenter | owner"),
        },
        "required": ["file_id", "permission_id", "role"],
    },
    "delete_permission": {
        "description": "Revoke a single permission from a file.",
        "properties": {
            "file_id": _prop("File ID"),
            "permission_id": _prop("Permission ID from list_permissions"),
        },
        "required": ["file_id", "permission_id"],
    },
    # ── Revisions ──
    "list_revisions": {
        "description": "List the version history of a Drive file.",
        "properties": {"file_id": _prop("File ID")},
        "required": ["file_id"],
    },
    "get_revision": {
        "description": "Fetch metadata for a single file revision.",
        "properties": {
            "file_id": _prop("File ID"),
            "revision_id": _prop("Revision ID from list_revisions"),
        },
        "required": ["file_id", "revision_id"],
    },
    "delete_revision": {
        "description": (
            "Delete a single revision. The current revision can't be "
            "deleted (only superseded by a newer one)."
        ),
        "properties": {
            "file_id": _prop("File ID"),
            "revision_id": _prop("Revision ID"),
        },
        "required": ["file_id", "revision_id"],
    },
    # ── Comments + replies ──
    "list_comments": {
        "description": "List comments on a Drive file (Doc / Sheet / Slide).",
        "properties": {"file_id": _prop("File ID")},
        "required": ["file_id"],
    },
    "create_comment": {
        "description": (
            "Add a comment to a file. Pass quoted_text to anchor the "
            "comment to a specific quote (Docs/Sheets only)."
        ),
        "properties": {
            "file_id": _prop("File ID"),
            "content": _prop("Comment body (Markdown OK)"),
            "quoted_text": _prop("Optional anchor text"),
        },
        "required": ["file_id", "content"],
    },
    "resolve_comment": {
        "description": "Mark a comment thread as resolved.",
        "properties": {
            "file_id": _prop("File ID"),
            "comment_id": _prop("Comment ID"),
        },
        "required": ["file_id", "comment_id"],
    },
    "delete_comment": {
        "description": "Delete a comment (and its replies).",
        "properties": {
            "file_id": _prop("File ID"),
            "comment_id": _prop("Comment ID"),
        },
        "required": ["file_id", "comment_id"],
    },
    "create_reply": {
        "description": "Reply to an existing comment thread.",
        "properties": {
            "file_id": _prop("File ID"),
            "comment_id": _prop("Comment ID to reply under"),
            "content": _prop("Reply body"),
        },
        "required": ["file_id", "comment_id", "content"],
    },
}

_HANDLERS = {
    "list_files": _list_files,
    "get_file": _get_file,
    "read_file": _read_file,
    "search_files": _search_files,
    "create_file": _create_file,
    "create_folder": _create_folder,
    "move_file": _move_file,
    "rename_file": _rename_file,
    "delete_file": _delete_file,
    "share_file": _share_file,
    "get_about": _get_about,
    "copy_file": _copy_file,
    "restore_file": _restore_file,
    "delete_file_permanent": _delete_file_permanent,
    "empty_trash": _empty_trash,
    "list_permissions": _list_permissions,
    "update_permission": _update_permission,
    "delete_permission": _delete_permission,
    "list_revisions": _list_revisions,
    "get_revision": _get_revision,
    "delete_revision": _delete_revision,
    "list_comments": _list_comments,
    "create_comment": _create_comment,
    "resolve_comment": _resolve_comment,
    "delete_comment": _delete_comment,
    "create_reply": _create_reply,
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
