"""Telegram Bot MCP module.

Wraps the Bot API (via ``TelegramAdapter``) so agents can send messages,
photos, and documents to any chat_id the bot can reach. Inbound side is
handled by the webhook router; this module is send-side only.

Credentials (JSON blob delivered as ``bearer_token``):
    {
      "bot_token":        "123456:ABC-DEF...",
      "default_chat_id":  "-100..."  (optional fallback when no chat_id given)
    }

Chunking: Telegram caps text messages at 4096 characters. This module
splits on paragraph boundaries (and falls back to hard slicing) so long
agent responses arrive as a series of messages rather than silently
truncated. Captions on photos/documents stay within the 1024-char limit
by truncating with an ellipsis — too long to split across media.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from packages.core.services.channels.telegram_adapter import TelegramAdapter

logger = logging.getLogger(__name__)


_MAX_TEXT = 4096
_MAX_CAPTION = 1024


_TOOLS: Dict[str, Dict[str, Any]] = {
    "send_message": {
        "description": "Send a text message to a Telegram chat. Long text is "
                       "auto-split into 4096-char chunks.",
        "required": ["content"],
        "properties": {
            "chat_id": {"type": "string",
                        "description": "Chat ID. Defaults to default_chat_id."},
            "content": {"type": "string"},
            "parse_mode": {"type": "string",
                           "enum": ["Markdown", "MarkdownV2", "HTML"]},
            "disable_notification": {"type": "boolean"},
            "reply_to_message_id": {"type": "integer"},
        },
    },
    "send_photo": {
        "description": "Send a photo via URL or file_id.",
        "required": ["photo"],
        "properties": {
            "chat_id": {"type": "string"},
            "photo": {"type": "string",
                      "description": "HTTPS URL or existing file_id."},
            "caption": {"type": "string", "description": "Caption ≤1024 chars."},
        },
    },
    "send_document": {
        "description": "Send a document (file) via URL or file_id.",
        "required": ["document"],
        "properties": {
            "chat_id": {"type": "string"},
            "document": {"type": "string"},
            "caption": {"type": "string"},
        },
    },
    "get_me": {
        "description": "Verify the bot token by fetching bot profile info.",
        "required": [],
        "properties": {},
    },
    "answer_callback_query": {
        "description": "Acknowledge a callback query from an inline-keyboard tap.",
        "required": ["callback_query_id"],
        "properties": {
            "callback_query_id": {"type": "string"},
            "text": {"type": "string"},
            "show_alert": {"type": "boolean"},
        },
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "required": spec.get("required", []),
                "properties": spec.get("properties", {}),
            },
        }
        for name, spec in _TOOLS.items()
    ]


async def call_tool(
    name: str, arguments: Dict[str, Any], bearer_token: str,
) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if not spec:
        return _error(f"Unknown tool: {name}")

    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        cfg = json.loads(bearer_token) if bearer_token else {}
    except Exception:
        return _error("Telegram credentials malformed.")

    bot_token = cfg.get("bot_token")
    if not bot_token:
        return _error("Telegram not configured — missing bot_token.")

    chat_id = arguments.get("chat_id") or cfg.get("default_chat_id")
    if name not in ("get_me",) and not chat_id:
        return _error("No chat_id provided and no default_chat_id configured.")

    adapter = TelegramAdapter(bot_token=bot_token)

    try:
        if name == "send_message":
            result = await _send_text_chunks(adapter, chat_id, arguments)
        elif name == "send_photo":
            result = await adapter.send_photo(
                chat_id, arguments["photo"],
                caption=_clip(arguments.get("caption", ""), _MAX_CAPTION),
            )
        elif name == "send_document":
            result = await adapter.send_document(
                chat_id, arguments["document"],
                caption=_clip(arguments.get("caption", ""), _MAX_CAPTION),
            )
        elif name == "get_me":
            result = await adapter.get_me()
        elif name == "answer_callback_query":
            result = await adapter.answer_callback(
                arguments["callback_query_id"],
                text=arguments.get("text"),
                show_alert=bool(arguments.get("show_alert", False)),
            )
        else:
            return _error(f"Unhandled tool: {name}")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("Telegram tool %s failed", name)
        return _error(f"{name} failed: {e}")

    return {"content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": False}


# ── Chunking helper ─────────────────────────────────────────────────────────

async def _send_text_chunks(
    adapter: TelegramAdapter, chat_id: str, args: Dict[str, Any],
) -> Dict[str, Any]:
    chunks = _split_for_telegram(args["content"])
    kwargs: Dict[str, Any] = {}
    if args.get("parse_mode"):
        kwargs["parse_mode"] = args["parse_mode"]
    if args.get("disable_notification"):
        kwargs["disable_notification"] = True
    # Only the first chunk attaches to the original message to keep the
    # reply-thread root consistent.
    first_reply_to = args.get("reply_to_message_id")

    sent: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        call_kwargs = dict(kwargs)
        if i == 0 and first_reply_to is not None:
            call_kwargs["reply_to_message_id"] = first_reply_to
        resp = await adapter.send_message(chat_id, chunk, **call_kwargs)
        sent.append(resp)
    return {
        "chunks": len(sent),
        "chat_id": chat_id,
        "message_ids": [m.get("message_id") for m in sent if isinstance(m, dict)],
    }


def _split_for_telegram(text: str) -> List[str]:
    if len(text) <= _MAX_TEXT:
        return [text]
    out: List[str] = []
    buf = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not buf else buf + "\n\n" + paragraph
        if len(candidate) <= _MAX_TEXT:
            buf = candidate
            continue
        # flush buf
        if buf:
            out.append(buf)
            buf = ""
        # paragraph itself may still exceed — hard-slice
        while len(paragraph) > _MAX_TEXT:
            out.append(paragraph[:_MAX_TEXT])
            paragraph = paragraph[_MAX_TEXT:]
        buf = paragraph
    if buf:
        out.append(buf)
    return out


def _clip(s: str, limit: int) -> str:
    if not s or len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}
