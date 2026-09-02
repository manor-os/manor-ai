"""Guild-bounded Discord MCP tools for a user-owned Server installation."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import quote

import httpx

from packages.core.ai.mcp._http import mcp_err, mcp_ok


_DISCORD_API = "https://discord.com/api/v10"
_TIMEOUT = 20.0

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_connection_info": {
        "description": "Get the Discord App and Server connected to this Manor account.",
        "required": [],
        "properties": {},
    },
    "list_channels": {
        "description": "List channels in the connected Discord Server.",
        "required": [],
        "properties": {},
    },
    "send_message": {
        "description": "Send a text message to a channel in the connected Discord Server.",
        "required": ["channel_id", "content"],
        "properties": {
            "channel_id": {"type": "string"},
            "content": {"type": "string"},
        },
    },
    "add_reaction": {
        "description": "Add a reaction to a message in the connected Discord Server.",
        "required": ["channel_id", "message_id", "emoji"],
        "properties": {
            "channel_id": {"type": "string"},
            "message_id": {"type": "string"},
            "emoji": {"type": "string"},
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
                "required": spec["required"],
                "properties": spec["properties"],
            },
        }
        for name, spec in _TOOLS.items()
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if spec is None:
        return mcp_err(f"Unknown Discord tool: {name}")

    missing = [
        field
        for field in spec["required"]
        if arguments.get(field) in (None, "")
    ]
    if missing:
        return mcp_err(f"Missing required params: {', '.join(missing)}")

    credentials = _decode_credentials(bearer_token)
    if credentials is None:
        return mcp_err("Discord credentials are unavailable for this connection.")
    bot_token, application_id, guild_id = credentials
    headers = {"Authorization": f"Bot {bot_token}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if name == "get_connection_info":
                guild = await _request_json(
                    client,
                    "GET",
                    f"/guilds/{guild_id}",
                    headers=headers,
                )
                return mcp_ok({
                    "application_id": application_id,
                    "guild_id": guild_id,
                    "guild_name": guild.get("name") if isinstance(guild, dict) else None,
                })

            if name == "list_channels":
                channels = await _request_json(
                    client,
                    "GET",
                    f"/guilds/{guild_id}/channels",
                    headers=headers,
                )
                return mcp_ok(channels)

            channel_id = str(arguments["channel_id"])
            channel = await _request_json(
                client,
                "GET",
                f"/channels/{channel_id}",
                headers=headers,
            )
            if not isinstance(channel, dict) or str(channel.get("guild_id") or "") != guild_id:
                return mcp_err(
                    "The requested channel is outside the connected Discord Server."
                )

            if name == "send_message":
                message = await _request_json(
                    client,
                    "POST",
                    f"/channels/{channel_id}/messages",
                    headers=headers,
                    json_body={"content": str(arguments["content"])},
                )
                return mcp_ok(message)

            emoji = quote(str(arguments["emoji"]), safe="")
            await _request_json(
                client,
                "PUT",
                (
                    f"/channels/{channel_id}/messages/{arguments['message_id']}"
                    f"/reactions/{emoji}/@me"
                ),
                headers=headers,
            )
            return mcp_ok({"status": "reacted"})
    except httpx.HTTPStatusError as exc:
        return mcp_err(f"Discord API returned HTTP {exc.response.status_code}.")
    except httpx.RequestError:
        return mcp_err("Discord API is currently unreachable.")

    return mcp_err(f"Unhandled Discord tool: {name}")


def _decode_credentials(token: str) -> tuple[str, str, str] | None:
    try:
        payload = json.loads(token)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    values = tuple(
        str(payload.get(key) or "").strip()
        for key in ("bot_token", "application_id", "guild_id")
    )
    if not all(values):
        return None
    return values


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
) -> Any:
    response = await client.request(
        method,
        f"{_DISCORD_API}{path}",
        headers=headers,
        json=json_body,
    )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()
