"""Microsoft Teams MCP server — in-process MCP for Microsoft Graph
Teams + chat + online-meetings surface.

Scopes used:
  - Team.ReadBasic.All        — list teams the user is in
  - Channel.ReadBasic.All     — list channels in those teams
  - ChannelMessage.Send       — post in channels
  - ChannelMessage.Read.All   — read channel messages
  - Chat.ReadWrite            — DMs / group chats
  - OnlineMeetings.ReadWrite  — schedule + read Teams meeting metadata

Auth: Microsoft Graph access_token (resolved via ``_ms_auth``).

Coverage focuses on the 80% agent uses:
  * Find which teams + channels the user is in
  * Read recent messages, post a message / reply
  * Send DMs (1:1 + group)
  * Spin up a Teams meeting link
What's intentionally NOT here:
  * Meeting recording / transcript download (admin-only scopes,
    different policy implications)
  * Channel/team CRUD (creation requires Group.ReadWrite.All; agents
    almost never need to create teams)
  * Files-tab proxying (use ``onedrive`` instead — Teams files are
    stored in SharePoint and reachable via Graph)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

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
        logger.exception("MS Teams MCP tool %s failed", name)
        return _error(str(exc))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── HTTP client ─────────────────────────────────────────────────────────────

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
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(method, url, headers=headers, json=body, params=params or {})
    if resp.status_code == 401:
        raise RuntimeError("Teams auth failed. Reconnect Microsoft on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"Teams forbidden: {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code in (202, 204):
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Teams API error ({resp.status_code}): {resp.text[:300]}")
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


# ── Tool handlers ───────────────────────────────────────────────────────────

# Teams + channels (read-only navigation)

async def _list_my_teams(token: str, args: Dict) -> str:
    return await _api(token, "GET", "me/joinedTeams")


async def _list_channels(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"teams/{args['team_id']}/channels")


async def _get_channel(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"teams/{args['team_id']}/channels/{args['channel_id']}",
    )


# Channel messages

async def _list_channel_messages(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"teams/{args['team_id']}/channels/{args['channel_id']}/messages",
        params={"$top": min(int(args.get("top") or 25), 50)},
    )


async def _send_channel_message(token: str, args: Dict) -> str:
    body = {
        "body": {
            "contentType": args.get("body_type") or "html",
            "content": args["body"],
        },
    }
    if args.get("subject"):
        body["subject"] = args["subject"]
    return await _api(
        token, "POST",
        f"teams/{args['team_id']}/channels/{args['channel_id']}/messages",
        body=body,
    )


async def _reply_to_channel_message(token: str, args: Dict) -> str:
    body = {
        "body": {
            "contentType": args.get("body_type") or "html",
            "content": args["body"],
        },
    }
    return await _api(
        token, "POST",
        f"teams/{args['team_id']}/channels/{args['channel_id']}/messages/{args['message_id']}/replies",
        body=body,
    )


async def _list_channel_message_replies(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"teams/{args['team_id']}/channels/{args['channel_id']}/messages/{args['message_id']}/replies",
        params={"$top": min(int(args.get("top") or 25), 50)},
    )


# Chats (DMs / group)

async def _list_chats(token: str, args: Dict) -> str:
    params: Dict[str, Any] = {"$top": min(int(args.get("top") or 25), 50)}
    if args.get("filter"):
        params["$filter"] = args["filter"]
    return await _api(token, "GET", "me/chats", params=params)


async def _get_chat(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"chats/{args['chat_id']}",
        params={"$expand": "members"},
    )


async def _list_chat_messages(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"chats/{args['chat_id']}/messages",
        params={"$top": min(int(args.get("top") or 25), 50)},
    )


async def _send_chat_message(token: str, args: Dict) -> str:
    body = {
        "body": {
            "contentType": args.get("body_type") or "html",
            "content": args["body"],
        },
    }
    return await _api(
        token, "POST", f"chats/{args['chat_id']}/messages", body=body,
    )


async def _create_chat(token: str, args: Dict) -> str:
    """Start a new 1:1 or group chat. Manor's user must always be a
    member — Graph adds them implicitly when you POST as them."""
    raw = args["recipients"]
    emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
    chat_type = args.get("chat_type") or ("group" if len(emails) > 1 else "oneOnOne")
    members = [
        {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{e}')",
        }
        for e in emails
    ]
    body: Dict[str, Any] = {"chatType": chat_type, "members": members}
    if args.get("topic") and chat_type == "group":
        body["topic"] = args["topic"]
    return await _api(token, "POST", "chats", body=body)


# Online meetings

async def _create_online_meeting(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"subject": args["subject"]}
    if args.get("start_time"):
        body["startDateTime"] = args["start_time"]
    if args.get("end_time"):
        body["endDateTime"] = args["end_time"]
    if args.get("attendees"):
        raw = args["attendees"]
        emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
        body["participants"] = {
            "attendees": [
                {"upn": e, "role": "attendee"} for e in emails
            ]
        }
    return await _api(token, "POST", "me/onlineMeetings", body=body)


async def _get_online_meeting(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"me/onlineMeetings/{args['meeting_id']}")


# Presence

async def _get_my_presence(token: str, args: Dict) -> str:
    return await _api(token, "GET", "me/presence")


async def _set_my_presence(token: str, args: Dict) -> str:
    body = {
        "sessionId": args.get("session_id") or "manor-mcp",
        "availability": args["availability"],
        "activity": args.get("activity") or args["availability"],
        "expirationDuration": args.get("expiration_duration") or "PT1H",
    }
    return await _api(token, "POST", "me/presence/setPresence", body=body)


# ── Tool definitions ────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_my_teams": {
        "description": "List the teams the authenticated user is in.",
        "properties": {},
        "required": [],
    },
    "list_channels": {
        "description": "List channels of a team.",
        "properties": {"team_id": _prop("Team ID")},
        "required": ["team_id"],
    },
    "get_channel": {
        "description": "Channel metadata by id.",
        "properties": {
            "team_id": _prop("Team ID"),
            "channel_id": _prop("Channel ID"),
        },
        "required": ["team_id", "channel_id"],
    },
    "list_channel_messages": {
        "description": "List recent messages in a channel.",
        "properties": {
            "team_id": _prop("Team ID"),
            "channel_id": _prop("Channel ID"),
            "top": _prop("Max messages (default 25, max 50)", "integer"),
        },
        "required": ["team_id", "channel_id"],
    },
    "send_channel_message": {
        "description": "Post a new message to a channel.",
        "properties": {
            "team_id": _prop("Team ID"),
            "channel_id": _prop("Channel ID"),
            "body": _prop("Message body (HTML or text)"),
            "body_type": _prop("html | text (default html)"),
            "subject": _prop("Optional subject (announcement-style)"),
        },
        "required": ["team_id", "channel_id", "body"],
    },
    "reply_to_channel_message": {
        "description": "Reply to a channel message (creates a thread / continues an existing thread).",
        "properties": {
            "team_id": _prop("Team ID"),
            "channel_id": _prop("Channel ID"),
            "message_id": _prop("Parent message ID"),
            "body": _prop("Reply body"),
            "body_type": _prop("html | text (default html)"),
        },
        "required": ["team_id", "channel_id", "message_id", "body"],
    },
    "list_channel_message_replies": {
        "description": "List replies under a channel message.",
        "properties": {
            "team_id": _prop("Team ID"),
            "channel_id": _prop("Channel ID"),
            "message_id": _prop("Parent message ID"),
            "top": _prop("Max replies (default 25)", "integer"),
        },
        "required": ["team_id", "channel_id", "message_id"],
    },

    "list_chats": {
        "description": "List the user's recent 1:1 / group chats.",
        "properties": {
            "top": _prop("Max chats (default 25, max 50)", "integer"),
            "filter": _prop("OData $filter (rare; use sparingly)"),
        },
        "required": [],
    },
    "get_chat": {
        "description": "Chat metadata + member list.",
        "properties": {"chat_id": _prop("Chat ID")},
        "required": ["chat_id"],
    },
    "list_chat_messages": {
        "description": "List messages from a chat.",
        "properties": {
            "chat_id": _prop("Chat ID"),
            "top": _prop("Max (default 25, max 50)", "integer"),
        },
        "required": ["chat_id"],
    },
    "send_chat_message": {
        "description": "Send a message to an existing chat.",
        "properties": {
            "chat_id": _prop("Chat ID"),
            "body": _prop("Message body"),
            "body_type": _prop("html | text"),
        },
        "required": ["chat_id", "body"],
    },
    "create_chat": {
        "description": (
            "Start a new chat (1:1 if 1 recipient, group otherwise). "
            "Returns the new chat object you can then send_chat_message into."
        ),
        "properties": {
            "recipients": _prop("Email addresses (list or comma-string)"),
            "chat_type": _prop("oneOnOne | group (auto-inferred from recipients count if omitted)"),
            "topic": _prop("Group chat topic (only honored when chat_type=group)"),
        },
        "required": ["recipients"],
    },

    "create_online_meeting": {
        "description": "Spin up a Teams meeting link, optionally with attendees / start window.",
        "properties": {
            "subject": _prop("Meeting subject"),
            "start_time": _prop("Optional ISO 8601 start"),
            "end_time": _prop("Optional ISO 8601 end"),
            "attendees": _prop("Optional invitees — emails (list or comma-string)"),
        },
        "required": ["subject"],
    },
    "get_online_meeting": {
        "description": "Get a Teams meeting (join URL, attendees, recording status).",
        "properties": {"meeting_id": _prop("Meeting ID")},
        "required": ["meeting_id"],
    },

    "get_my_presence": {
        "description": "Get the user's current Teams presence state (Available / Busy / Away …).",
        "properties": {},
        "required": [],
    },
    "set_my_presence": {
        "description": "Set the user's Teams presence (e.g. Busy / DoNotDisturb) for an interval.",
        "properties": {
            "availability": _prop("Available | Busy | DoNotDisturb | BeRightBack | Away | Offline"),
            "activity": _prop("Optional activity (defaults to availability)"),
            "session_id": _prop("Caller-chosen session id (default 'manor-mcp')"),
            "expiration_duration": _prop("ISO 8601 duration (default PT1H)"),
        },
        "required": ["availability"],
    },
}


_HANDLERS = {
    "list_my_teams": _list_my_teams,
    "list_channels": _list_channels,
    "get_channel": _get_channel,
    "list_channel_messages": _list_channel_messages,
    "send_channel_message": _send_channel_message,
    "reply_to_channel_message": _reply_to_channel_message,
    "list_channel_message_replies": _list_channel_message_replies,
    "list_chats": _list_chats,
    "get_chat": _get_chat,
    "list_chat_messages": _list_chat_messages,
    "send_chat_message": _send_chat_message,
    "create_chat": _create_chat,
    "create_online_meeting": _create_online_meeting,
    "get_online_meeting": _get_online_meeting,
    "get_my_presence": _get_my_presence,
    "set_my_presence": _set_my_presence,
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
