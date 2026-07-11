"""WeChat Official Account (公众号) MCP module.

Wraps the Customer Service Message API + Media API behind the MCP
``tools/list`` + ``tools/call`` contract. Agents discover tools by name
and call them with structured arguments.

Credentials (passed by the dispatcher as a JSON-encoded ``bearer_token``):
    {
      "app_id": "wx...",
      "app_secret": "...",
      "token": "...",                    # webhook verify token (unused here)
      "encoding_aes_key": "..."          # optional
    }

Token management and signing live in ``WeChatAdapter`` so this module
stays thin. The adapter caches access_token in Redis so every worker
amortises the cgi-bin/token call.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

from packages.core.services.channels.wechat_adapter import (
    WECHAT_API_BASE, WeChatAdapter,
)

logger = logging.getLogger(__name__)


_TOOLS: Dict[str, Dict[str, Any]] = {
    "send_text_message": {
        "description": "Send a text customer-service message to a follower "
                       "(allowed within 48h of their last message).",
        "required": ["to_user", "content"],
        "properties": {
            "to_user": {"type": "string", "description": "Follower OpenID."},
            "content": {"type": "string"},
        },
    },
    "send_image_message": {
        "description": "Send an image customer-service message (requires a prior upload_media).",
        "required": ["to_user", "media_id"],
        "properties": {
            "to_user": {"type": "string"},
            "media_id": {"type": "string"},
        },
    },
    "send_template_message": {
        "description": "Send a pre-approved template message.",
        "required": ["to_user", "template_id", "data"],
        "properties": {
            "to_user": {"type": "string"},
            "template_id": {"type": "string"},
            "url": {"type": "string"},
            "data": {"type": "object",
                     "description": "Template variables, keyed by placeholder name."},
        },
    },
    "upload_media": {
        "description": "Upload a temporary media file (image/voice/video/thumb). "
                       "Returns media_id valid for 3 days.",
        "required": ["media_type", "file_url"],
        "properties": {
            "media_type": {"type": "string",
                           "enum": ["image", "voice", "video", "thumb"]},
            "file_url": {"type": "string",
                         "description": "HTTP(S) URL to fetch the file from."},
        },
    },
    "get_follower_info": {
        "description": "Fetch profile info for a follower by OpenID.",
        "required": ["open_id"],
        "properties": {
            "open_id": {"type": "string"},
            "lang": {"type": "string",
                     "description": "zh_CN | zh_TW | en (default zh_CN)."},
        },
    },
    "list_followers": {
        "description": "List follower OpenIDs (paginated — up to 10 000 per call).",
        "required": [],
        "properties": {
            "next_open_id": {"type": "string",
                             "description": "Pagination cursor returned by a previous call."},
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
        return _error("WeChat credentials malformed.")

    app_id = cfg.get("app_id")
    app_secret = cfg.get("app_secret")
    if not app_id or not app_secret:
        return _error("WeChat OA needs app_id and app_secret. "
                      "Configure in Integrations → WeChat Official Account.")

    adapter = WeChatAdapter(
        app_id=app_id,
        app_secret=app_secret,
        token=cfg.get("token", ""),
        encoding_aes_key=cfg.get("encoding_aes_key"),
    )

    try:
        if name == "send_text_message":
            await adapter.send_text(arguments["to_user"], arguments["content"])
            result = {"success": True, "to_user": arguments["to_user"]}
        elif name == "send_image_message":
            await adapter.send_image(arguments["to_user"], arguments["media_id"])
            result = {"success": True, "to_user": arguments["to_user"]}
        elif name == "send_template_message":
            result = await _send_template(adapter, arguments)
        elif name == "upload_media":
            result = await _upload_media(adapter, arguments)
        elif name == "get_follower_info":
            result = await _get_follower(adapter, arguments)
        elif name == "list_followers":
            result = await _list_followers(adapter, arguments)
        else:
            return _error(f"Unhandled tool: {name}")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("WeChat OA tool %s failed", name)
        return _error(f"{name} failed: {e}")

    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False}


# ── Per-tool implementations ────────────────────────────────────────────────

async def _send_template(adapter: WeChatAdapter, args: Dict[str, Any]) -> Dict[str, Any]:
    token = await adapter.get_access_token()
    url = f"{WECHAT_API_BASE}/message/template/send?access_token={token}"
    payload = {
        "touser": args["to_user"],
        "template_id": args["template_id"],
        "data": _format_template_data(args["data"]),
    }
    if args.get("url"):
        payload["url"] = args["url"]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        data = resp.json()
    _raise_on_errcode(adapter, data, "send_template_message")
    return {"success": True, "msgid": data.get("msgid")}


def _format_template_data(data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """WeChat template data is a dict of {key: {value: str, color?: str}}."""
    out: Dict[str, Dict[str, str]] = {}
    for key, val in data.items():
        if isinstance(val, dict) and "value" in val:
            out[key] = {"value": str(val["value"])}
            if "color" in val:
                out[key]["color"] = str(val["color"])
        else:
            out[key] = {"value": str(val)}
    return out


async def _upload_media(adapter: WeChatAdapter, args: Dict[str, Any]) -> Dict[str, Any]:
    media_type = args["media_type"]
    file_url = args["file_url"]

    # Fetch the file bytes from the caller-provided URL
    async with httpx.AsyncClient(timeout=30) as client:
        file_resp = await client.get(file_url)
        file_resp.raise_for_status()
        body = file_resp.content
        content_type = file_resp.headers.get("Content-Type", "application/octet-stream")

        token = await adapter.get_access_token()
        upload_url = f"{WECHAT_API_BASE}/media/upload?access_token={token}&type={media_type}"
        files = {"media": ("upload", body, content_type)}
        up = await client.post(upload_url, files=files)
        data = up.json()

    _raise_on_errcode(adapter, data, "upload_media")
    return {
        "media_id": data.get("media_id"),
        "type": data.get("type", media_type),
        "created_at": data.get("created_at"),
    }


async def _get_follower(adapter: WeChatAdapter, args: Dict[str, Any]) -> Dict[str, Any]:
    token = await adapter.get_access_token()
    url = (
        f"{WECHAT_API_BASE}/user/info?access_token={token}"
        f"&openid={args['open_id']}"
        f"&lang={args.get('lang') or 'zh_CN'}"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()
    _raise_on_errcode(adapter, data, "get_follower_info")
    return data


async def _list_followers(adapter: WeChatAdapter, args: Dict[str, Any]) -> Dict[str, Any]:
    token = await adapter.get_access_token()
    url = f"{WECHAT_API_BASE}/user/get?access_token={token}"
    if args.get("next_open_id"):
        url += f"&next_openid={args['next_open_id']}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()
    _raise_on_errcode(adapter, data, "list_followers")
    return {
        "total": data.get("total", 0),
        "count": data.get("count", 0),
        "openids": (data.get("data") or {}).get("openid", []),
        "next_open_id": data.get("next_openid", ""),
    }


# ── Shared helpers ──────────────────────────────────────────────────────────

def _raise_on_errcode(
    adapter: WeChatAdapter, data: Dict[str, Any], op: str,
) -> None:
    errcode = data.get("errcode", 0)
    if not errcode:
        return
    errmsg = data.get("errmsg", "unknown error")
    if errcode in (40001, 40014, 42001):
        adapter._invalidate_token_cache()  # noqa: SLF001 — adapter helper
    raise RuntimeError(f"WeChat {op} failed — errcode={errcode} errmsg={errmsg}")


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}
