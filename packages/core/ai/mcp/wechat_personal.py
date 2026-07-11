"""Personal WeChat bot MCP module.

Talks to the multi-session WeChat Personal runner. Every connected
Integration owns a runner ``session_id``; all calls must be addressed
through that session:

    GET  /sessions/{sid}/status
    GET  /sessions/{sid}/qr.png
    POST /sessions/{sid}/messages  {kind, target, body}

iLink personal-account bots are reply-only: outbound sends need a
recent inbound ``context_token`` for the target. The runner caches
those by peer id and returns HTTP 409 when no reply context exists.

Credentials (JSON blob delivered as ``bearer_token``):
    {
      "runner_url":    "https://wechat-bot.internal",
      "bearer_token":  "shared-secret-or-empty",
      "session_id":    "runner-session-id",
      "group_id":      "default group id for group scope (optional)"
    }
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_groups": {
        "description": "List recently-seen WeChat group peers the bot can reply to.",
        "required": [],
        "properties": {},
    },
    "list_contacts": {
        "description": "List recently-seen 1:1 WeChat peers the bot can reply to.",
        "required": [],
        "properties": {},
    },
    "send_group_message": {
        "description": "Reply with text to a WeChat group that messaged the bot recently.",
        "required": ["group_id", "content"],
        "properties": {
            "group_id": {"type": "string"},
            "content": {"type": "string"},
        },
    },
    "send_direct_message": {
        "description": "Reply with text to a WeChat contact that messaged the bot recently.",
        "required": ["contact_id", "content"],
        "properties": {
            "contact_id": {"type": "string"},
            "content": {"type": "string"},
        },
    },
    "get_bot_status": {
        "description": "Get this WeChat runner session status (online / QR pending / offline).",
        "required": [],
        "properties": {},
    },
    "get_qr_code": {
        "description": "Get a fresh QR code URL so the user can (re)scan to log in.",
        "required": [],
        "properties": {},
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
        return _error("WeChat personal credentials malformed.")

    runner_url = (cfg.get("runner_url") or "").rstrip("/")
    if not runner_url:
        return _error("Personal WeChat bot needs a runner_url. "
                      "Configure in Integrations → WeChat (Personal).")

    runner_token = cfg.get("bearer_token") or ""
    session_id = (cfg.get("session_id") or "").strip()
    if not session_id:
        return _error(
            "Personal WeChat bot credentials are missing session_id. "
            "Open Integrations -> WeChat (Personal), scan the ClawBot QR, "
            "and finish the connection again."
        )

    session_prefix = f"/sessions/{session_id}"

    try:
        if name == "list_groups":
            status = await _get_json(runner_url, f"{session_prefix}/status", runner_token)
            result = {
                "groups": [],
                "known_peers": status.get("known_peers") or [],
                "note": (
                    "The iLink runner cannot enumerate all WeChat groups. "
                    "Use a group peer id from an inbound message once that group "
                    "has messaged the bot."
                ),
            }
        elif name == "list_contacts":
            status = await _get_json(runner_url, f"{session_prefix}/status", runner_token)
            result = {
                "contacts": [
                    {"id": peer, "name": peer, "source": "recent_inbound"}
                    for peer in status.get("known_peers") or []
                ],
                "note": (
                    "iLink only exposes recently-seen peers that can be replied to; "
                    "it does not provide a full contact book."
                ),
            }
        elif name == "get_bot_status":
            result = await _get_json(runner_url, f"{session_prefix}/status", runner_token)
        elif name == "get_qr_code":
            result = {
                "session_id": session_id,
                "qr_path": f"{session_prefix}/qr.png",
                "qr_url": f"{runner_url}{session_prefix}/qr.png",
            }
        elif name == "send_group_message":
            result = await _post_json(
                runner_url, f"{session_prefix}/messages", runner_token,
                {"kind": "group", "target": arguments["group_id"],
                 "body": arguments["content"]},
            )
        elif name == "send_direct_message":
            result = await _post_json(
                runner_url, f"{session_prefix}/messages", runner_token,
                {"kind": "direct", "target": arguments["contact_id"],
                 "body": arguments["content"]},
            )
        else:
            return _error(f"Unhandled tool: {name}")
    except httpx.TimeoutException:
        return _error(f"WeChat bot runner timed out at {runner_url}.")
    except httpx.ConnectError:
        return _error(f"Cannot reach WeChat bot runner at {runner_url}. "
                      f"Check the runner is online and runner_url is correct.")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("WeChat personal tool %s failed", name)
        return _error(f"{name} failed: {e}")

    return {"content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False)}],
            "isError": False}


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _headers(token: str) -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _get_json(base: str, path: str, token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{base}{path}", headers=_headers(token))
        _raise_on_bad_status(resp)
        return resp.json()


async def _post_json(
    base: str, path: str, token: str, body: Dict[str, Any],
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base}{path}",
            headers={**_headers(token), "Content-Type": "application/json"},
            json=body,
        )
        _raise_on_bad_status(resp)
        return resp.json()


def _raise_on_bad_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise RuntimeError(
            "WeChat bot runner rejected the bearer token — check runner configuration."
        )
    if resp.status_code == 404:
        raise RuntimeError(
            f"WeChat runner has no session for {resp.request.url}. "
            "The runner may have restarted; reconnect WeChat (Personal) and scan the ClawBot QR again."
        )
    if resp.status_code == 409:
        raise RuntimeError(
            "WeChat iLink rejected the send because there is no recent reply context for this peer. "
            "Ask the contact or group to message the bot first, then reply."
        )
    if resp.status_code == 410:
        raise RuntimeError(
            "WeChat runner returned 410 for a legacy endpoint. Upgrade the backend/runner pair "
            "so both use /sessions/{session_id}/..."
        )
    if resp.status_code == 503:
        raise RuntimeError(
            "WeChat session is not online. Open Integrations -> WeChat (Personal) and scan the ClawBot QR."
        )
    if not resp.is_success:
        raise RuntimeError(
            f"WeChat bot runner error {resp.status_code}: {resp.text[:200]}"
        )


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}
