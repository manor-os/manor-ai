"""
MCP (Model Context Protocol) tool integration.

Discovers and registers tools from external MCP servers.
Tool naming: mcp__{server_name}__{tool_name}
All MCP tools are deferred by default.
"""
from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# MCP config from env or file
MCP_SERVERS_ENV = "MCP_SERVERS"  # JSON string: [{"name": "x", "url": "http://...", "token": "..."}]


def _load_mcp_config() -> list[dict]:
    """Load MCP server configurations."""
    raw = os.getenv(MCP_SERVERS_ENV, "").strip()
    if not raw:
        return []
    try:
        servers = json.loads(raw)
        if isinstance(servers, list):
            return servers
    except json.JSONDecodeError:
        logger.warning("Invalid MCP_SERVERS JSON: %s", raw[:100])
    return []


async def _call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict,
    token: str | None = None,
) -> str:
    """Call a tool on an MCP server via HTTP."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{server_url.rstrip('/')}/tools/{tool_name}",
                headers=headers,
                json={"arguments": arguments},
            )
            if resp.status_code == 200:
                data = resp.json()
                return json.dumps(data.get("result", data), ensure_ascii=False)
            return json.dumps({"error": f"MCP server returned {resp.status_code}: {resp.text[:500]}"})
    except Exception as e:
        return json.dumps({"error": f"MCP call failed: {e}"})


async def _discover_mcp_tools(server_url: str, token: str | None = None) -> list[dict]:
    """Discover available tools from an MCP server."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{server_url.rstrip('/')}/tools",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("tools", data) if isinstance(data, (dict, list)) else []
    except Exception as e:
        logger.warning("Failed to discover MCP tools from %s: %s", server_url, e)
    return []


async def _mcp_tool_handler(
    entity_id: str = "", *, server_url: str, tool_name: str, token: str | None, **kwargs: Any,
) -> str:
    """Handler for a single MCP tool, used with functools.partial."""
    return await _call_mcp_tool(server_url, tool_name, kwargs, token)


def get_mcp_tools() -> list[tuple[dict, Any]]:
    """
    Load MCP server configs and register their tools.

    Each MCP tool becomes: mcp__{server_name}__{tool_name}
    All MCP tools are deferred.
    """
    servers = _load_mcp_config()
    if not servers:
        return []

    tools: list[tuple[dict, Any]] = []

    for server in servers:
        server_name = server.get("name", "unknown")
        server_url = server.get("url", "")
        server_token = server.get("token")

        if not server_url:
            continue

        # Static tool definitions from config (no async discovery at import time)
        static_tools = server.get("tools", [])

        for tool_def in static_tools:
            original_name = tool_def.get("name", "")
            if not original_name:
                continue

            mcp_name = f"mcp__{server_name}__{original_name}"
            description = tool_def.get("description", f"MCP tool: {original_name} from {server_name}")
            parameters = tool_def.get("parameters", {"type": "object", "properties": {}})

            schema = {
                "type": "function",
                "function": {
                    "name": mcp_name,
                    "description": f"[MCP:{server_name}] {description}",
                    "parameters": parameters,
                },
            }

            handler = functools.partial(
                _mcp_tool_handler,
                server_url=server_url,
                tool_name=original_name,
                token=server_token,
            )
            tools.append((schema, handler))

        logger.info("MCP server '%s': %d tools registered", server_name, len(static_tools))

    return tools


def get_tools():
    """Standard tool registration interface."""
    return get_mcp_tools()
