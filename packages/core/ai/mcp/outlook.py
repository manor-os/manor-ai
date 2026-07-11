"""Outlook Mail MCP server — in-process MCP for Microsoft Graph /me/messages.

Scopes used (request the subset your deployment needs):
  - Mail.Read         — read messages + folders
  - Mail.ReadWrite    — flag / move / mark read / labels
  - Mail.Send         — send / reply / forward

Auth: Microsoft Graph access_token (resolved + refreshed via
``_ms_auth.get_ms_access_token``).

Tool surface mirrors gmail.py so an agent can swap providers without
re-learning. Calling conventions stay close to Microsoft Graph
(``message_id`` is the Graph id, not the legacy MAPI Outlook id).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://graph.microsoft.com/v1.0"
_MAX_CHARS = 12_000


# ── MCP Protocol ─────────────────────────────────────────────────────────────

_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Messages: read ────────────────────────────────────────────────────
    "list_messages": {
        "description": (
            "List Outlook mail messages. Use $filter for inbox-style "
            "queries: ``isRead eq false``, ``from/emailAddress/address eq 'x@y.com'``, "
            "``receivedDateTime ge 2026-04-01``."
        ),
        "required": [],
        "properties": {
            "filter": {"type": "string", "description": "OData $filter expression"},
            "search": {"type": "string", "description": "Microsoft Search KQL — alternative to filter for full-text"},
            "folder_id": {"type": "string", "description": "Restrict to a folder (default: inbox)"},
            "top": {"type": "integer", "description": "Max results (default 25, max 1000)"},
            "select": {"type": "string", "description": "Comma-separated fields to return (default: id,subject,from,receivedDateTime,isRead,bodyPreview)"},
        },
    },
    "get_message": {
        "description": "Fetch a single message by Graph id (headers + body).",
        "required": ["message_id"],
        "properties": {
            "message_id": {"type": "string"},
            "select": {"type": "string", "description": "Comma-separated fields to return"},
        },
    },
    # ── Messages: write ───────────────────────────────────────────────────
    "send_message": {
        "description": "Send a new email from the authenticated user.",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string", "description": "Recipient email (or comma-separated)"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "HTML or plain text body"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "body_type": {"type": "string", "enum": ["text", "html"], "description": "Default 'html'"},
        },
    },
    "reply_to_message": {
        "description": "Reply to a single message (Graph creates the threaded reply for you).",
        "required": ["message_id", "body"],
        "properties": {
            "message_id": {"type": "string"},
            "body": {"type": "string"},
            "reply_all": {"type": "boolean", "description": "Reply to all recipients (default: false)"},
        },
    },
    "forward_message": {
        "description": "Forward a message to one or more recipients.",
        "required": ["message_id", "to"],
        "properties": {
            "message_id": {"type": "string"},
            "to": {"type": "string", "description": "Comma-separated recipient emails"},
            "comment": {"type": "string", "description": "Optional note prepended to the forward"},
        },
    },
    # ── Drafts ────────────────────────────────────────────────────────────
    "create_draft": {
        "description": "Create an unsent draft for human review (use update_draft / send_draft to refine + send).",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "body_type": {"type": "string", "enum": ["text", "html"]},
        },
    },
    "update_draft": {
        "description": "Replace draft fields (subject / body / recipients).",
        "required": ["draft_id"],
        "properties": {
            "draft_id": {"type": "string"},
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "body_type": {"type": "string", "enum": ["text", "html"]},
        },
    },
    "send_draft": {
        "description": "Send a previously-prepared draft.",
        "required": ["draft_id"],
        "properties": {"draft_id": {"type": "string"}},
    },
    "delete_draft": {
        "description": "Permanently delete a draft.",
        "required": ["draft_id"],
        "properties": {"draft_id": {"type": "string"}},
    },
    # ── Status ────────────────────────────────────────────────────────────
    "mark_read": {
        "description": "Mark a message as read.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "mark_unread": {
        "description": "Mark a message as unread.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "flag_message": {
        "description": "Flag (or unflag) a message for follow-up. ``status`` = flagged | complete | notFlagged.",
        "required": ["message_id"],
        "properties": {
            "message_id": {"type": "string"},
            "status": {"type": "string", "enum": ["flagged", "complete", "notFlagged"], "description": "Default 'flagged'"},
        },
    },
    "categorize": {
        "description": "Apply category labels to a message (Outlook's coloured-tag system).",
        "required": ["message_id", "categories"],
        "properties": {
            "message_id": {"type": "string"},
            "categories": {"type": "string", "description": "Comma-separated category names"},
        },
    },
    "move_message": {
        "description": "Move a message to a different folder. Use a folder id from list_folders or a well-known name (Inbox / Archive / DeletedItems / JunkEmail / SentItems / Drafts).",
        "required": ["message_id", "destination_folder_id"],
        "properties": {
            "message_id": {"type": "string"},
            "destination_folder_id": {"type": "string"},
        },
    },
    "delete_message": {
        "description": "Move a message to Deleted Items (recoverable for ~30 days from Outlook clients).",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    # ── Folders ───────────────────────────────────────────────────────────
    "list_folders": {
        "description": "List the user's mail folders (Inbox, Sent Items, Drafts, custom folders).",
        "required": [],
        "properties": {
            "top": {"type": "integer", "description": "Max results (default 50)"},
        },
    },
    "create_folder": {
        "description": "Create a new mail folder under the user's root (or under another folder if parent_folder_id is set).",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "parent_folder_id": {"type": "string"},
        },
    },
    # ── Attachments ───────────────────────────────────────────────────────
    "list_attachments": {
        "description": "List attachments on a message (id, name, contentType, size, isInline).",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "download_attachment": {
        "description": (
            "Fetch a single file attachment as base64. The Graph response "
            "embeds ``contentBytes`` for fileAttachment types — for "
            "itemAttachments (forwarded emails / events) the response "
            "is a structured object instead."
        ),
        "required": ["message_id", "attachment_id"],
        "properties": {
            "message_id": {"type": "string"},
            "attachment_id": {"type": "string"},
        },
    },
    # ── User profile ──────────────────────────────────────────────────────
    "get_profile": {
        "description": "Get the authenticated user's mail profile (mail address, display name).",
        "required": [],
        "properties": {},
    },
}


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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Outlook MCP tool %s failed", name)
        return _error(str(exc))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── Microsoft Graph client ──────────────────────────────────────────────────

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
            method, url, headers=headers, json=body, params=params or {},
        )

    if resp.status_code == 401:
        raise RuntimeError("Outlook auth failed. Reconnect Microsoft on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"Outlook forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code in (202, 204):
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Outlook API error ({resp.status_code}): {resp.text[:300]}")

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


def _to_recipients(value: Any) -> List[Dict[str, Dict[str, str]]]:
    """Normalise a `to/cc/bcc` value into Graph's ``[{emailAddress: {address}}]``
    list shape. Accepts a single email, comma-separated string, or list."""
    if not value:
        return []
    if isinstance(value, list):
        emails = [str(e).strip() for e in value if e]
    else:
        emails = [e.strip() for e in str(value).split(",") if e.strip()]
    return [{"emailAddress": {"address": e}} for e in emails]


def _build_message(args: Dict[str, Any]) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "subject": args["subject"],
        "body": {
            "contentType": (args.get("body_type") or "html").lower(),
            "content": args["body"],
        },
        "toRecipients": _to_recipients(args["to"]),
    }
    if args.get("cc"):
        msg["ccRecipients"] = _to_recipients(args["cc"])
    if args.get("bcc"):
        msg["bccRecipients"] = _to_recipients(args["bcc"])
    return msg


# ── Tool handlers ───────────────────────────────────────────────────────────

# Messages: read

async def _list_messages(token: str, args: Dict) -> str:
    folder_id = args.get("folder_id") or "inbox"
    params: Dict[str, Any] = {
        "$top": min(int(args.get("top") or 25), 1000),
        "$select": args.get("select") or "id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview,hasAttachments",
        "$orderby": "receivedDateTime DESC",
    }
    if args.get("filter"):
        params["$filter"] = args["filter"]
    if args.get("search"):
        # $search is mutually exclusive with both $filter and $orderby on Graph.
        params.pop("$filter", None)
        params.pop("$orderby", None)
        params["$search"] = f'"{args["search"]}"'
    return await _api(
        token, "GET", f"me/mailFolders/{folder_id}/messages", params=params,
    )


async def _get_message(token: str, args: Dict) -> str:
    params: Dict[str, Any] = {}
    if args.get("select"):
        params["$select"] = args["select"]
    return await _api(token, "GET", f"me/messages/{args['message_id']}", params=params)


# Messages: write

async def _send_message(token: str, args: Dict) -> str:
    body = {"message": _build_message(args), "saveToSentItems": True}
    return await _api(token, "POST", "me/sendMail", body=body)


async def _reply_to_message(token: str, args: Dict) -> str:
    endpoint = "replyAll" if args.get("reply_all") else "reply"
    return await _api(
        token, "POST", f"me/messages/{args['message_id']}/{endpoint}",
        body={"comment": args["body"]},
    )


async def _forward_message(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"toRecipients": _to_recipients(args["to"])}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return await _api(
        token, "POST", f"me/messages/{args['message_id']}/forward", body=body,
    )


# Drafts

async def _create_draft(token: str, args: Dict) -> str:
    return await _api(token, "POST", "me/messages", body=_build_message(args))


async def _update_draft(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    if args.get("subject"):
        body["subject"] = args["subject"]
    if args.get("body"):
        body["body"] = {
            "contentType": (args.get("body_type") or "html").lower(),
            "content": args["body"],
        }
    if args.get("to"):
        body["toRecipients"] = _to_recipients(args["to"])
    if args.get("cc"):
        body["ccRecipients"] = _to_recipients(args["cc"])
    if args.get("bcc"):
        body["bccRecipients"] = _to_recipients(args["bcc"])
    if not body:
        return "No fields to update — pass to / subject / body / cc / bcc"
    return await _api(token, "PATCH", f"me/messages/{args['draft_id']}", body=body)


async def _send_draft(token: str, args: Dict) -> str:
    return await _api(token, "POST", f"me/messages/{args['draft_id']}/send")


async def _delete_draft(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"me/messages/{args['draft_id']}")


# Status

async def _mark_read(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH", f"me/messages/{args['message_id']}",
        body={"isRead": True},
    )


async def _mark_unread(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH", f"me/messages/{args['message_id']}",
        body={"isRead": False},
    )


async def _flag_message(token: str, args: Dict) -> str:
    status = (args.get("status") or "flagged").strip()
    if status not in ("flagged", "complete", "notFlagged"):
        return "status must be flagged | complete | notFlagged"
    return await _api(
        token, "PATCH", f"me/messages/{args['message_id']}",
        body={"flag": {"flagStatus": status}},
    )


async def _categorize(token: str, args: Dict) -> str:
    raw = args["categories"]
    cats = raw if isinstance(raw, list) else [c.strip() for c in str(raw).split(",") if c.strip()]
    return await _api(
        token, "PATCH", f"me/messages/{args['message_id']}",
        body={"categories": cats},
    )


async def _move_message(token: str, args: Dict) -> str:
    return await _api(
        token, "POST", f"me/messages/{args['message_id']}/move",
        body={"destinationId": args["destination_folder_id"]},
    )


async def _delete_message(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"me/messages/{args['message_id']}")


# Folders

async def _list_folders(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", "me/mailFolders",
        params={"$top": min(int(args.get("top") or 50), 1000)},
    )


async def _create_folder(token: str, args: Dict) -> str:
    body = {"displayName": args["name"]}
    parent = args.get("parent_folder_id")
    path = (
        f"me/mailFolders/{parent}/childFolders" if parent
        else "me/mailFolders"
    )
    return await _api(token, "POST", path, body=body)


# Attachments

async def _list_attachments(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"me/messages/{args['message_id']}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    )


async def _download_attachment(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"me/messages/{args['message_id']}/attachments/{args['attachment_id']}",
    )


# User profile

async def _get_profile(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", "me",
        params={"$select": "id,displayName,mail,userPrincipalName,jobTitle,preferredLanguage"},
    )


_HANDLERS = {
    "list_messages": _list_messages,
    "get_message": _get_message,
    "send_message": _send_message,
    "reply_to_message": _reply_to_message,
    "forward_message": _forward_message,
    "create_draft": _create_draft,
    "update_draft": _update_draft,
    "send_draft": _send_draft,
    "delete_draft": _delete_draft,
    "mark_read": _mark_read,
    "mark_unread": _mark_unread,
    "flag_message": _flag_message,
    "categorize": _categorize,
    "move_message": _move_message,
    "delete_message": _delete_message,
    "list_folders": _list_folders,
    "create_folder": _create_folder,
    "list_attachments": _list_attachments,
    "download_attachment": _download_attachment,
    "get_profile": _get_profile,
}


# ── Schema helpers ──────────────────────────────────────────────────────────

def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "required": spec.get("required", []),
            "properties": spec.get("properties", {}),
        },
    }
