"""Stateless MCP (JSON-RPC 2.0) dispatcher for a tools-only server.

Streamable HTTP with JSON responses: each POST carries one message (or a batch),
there are no sessions and no server-initiated messages, so any API replica can
serve any request.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from apps.api.mcp.manor_api import ManorApiError
from apps.api.mcp.registry import Tool, ToolContext, ToolError, ToolImage

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "manor", "title": "Manor Workspace Builder", "version": "1.0.0"}
INSTRUCTIONS = (
    "Build and deliver Manor Workspaces. Configure behaviour on a live Workspace "
    "(stage → review → apply operation drafts, workflows, skills, knowledge, "
    "scheduled jobs), read back its Knowledge and tasks, keep "
    "the exported Blueprint file in the repo as the versioned release, prove it with "
    "install_blueprint (simulate) + compare_blueprints, then promote_workspace or "
    "deliver_to_client. Large files move through curl commands returned by "
    "export_blueprint, read_knowledge_document and create_upload."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# The token lacks an OAuth scope the tool needs (the user re-authorizes with it).
INSUFFICIENT_SCOPE = -32003


def _error(message_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _text_result(value: Any, *, is_error: bool = False) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _image_result(image: ToolImage) -> dict:
    content: list[dict] = [{"type": "image", "data": image.data, "mimeType": image.mime_type}]
    if image.text:
        content.append({"type": "text", "text": image.text})
    return {"content": content, "isError": False}


async def _available(tool: Tool, ctx: ToolContext) -> bool:
    return tool.available is None or bool(await tool.available(ctx))


def _granted_scopes(claims: dict) -> set[str]:
    # A token from before scopes existed was granted "mcp" only.
    return set(claims.get("scp") or ["mcp"])


class McpDispatcher:
    def __init__(self, tools: Iterable[Tool]):
        self._tools = {tool.name: tool for tool in tools}

    @property
    def tools(self) -> dict[str, Tool]:
        return self._tools

    async def handle(self, message: Any, ctx: ToolContext) -> Optional[dict]:
        """One JSON-RPC message → its response, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "invalid JSON-RPC message")
        method = message.get("method")
        message_id = message.get("id")
        if not isinstance(method, str):
            # A response to a server request; this server never sends any.
            return None
        if "id" not in message:
            return None  # notifications (initialized, cancelled, …) need no reply
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(message_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            return _result(message_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": [
                tool.descriptor() for tool in self._tools.values() if await _available(tool, ctx)
            ]})
        if method == "tools/call":
            tool = self._tools.get(params.get("name"))
            if tool is not None and not await _available(tool, ctx):
                return _result(message_id, _text_result(f"unknown tool: {params.get('name')}", is_error=True))
            missing = [scope for scope in (tool.scopes if tool else ()) if scope not in _granted_scopes(ctx.claims)]
            if missing:
                return _error(
                    message_id, INSUFFICIENT_SCOPE,
                    f"insufficient_scope: {tool.name} needs {', '.join(missing)}; "
                    "reconnect Manor and allow it",
                )
            return _result(message_id, await self._call(params, ctx))
        return _error(message_id, METHOD_NOT_FOUND, f"method not found: {method}")

    async def _call(self, params: dict, ctx: ToolContext) -> dict:
        tool = self._tools.get(params.get("name"))
        if tool is None:
            return _text_result(f"unknown tool: {params.get('name')}", is_error=True)
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _text_result("arguments must be an object", is_error=True)
        try:
            value = await tool.handler(ctx, arguments)
            return _image_result(value) if isinstance(value, ToolImage) else _text_result(value)
        except ToolError as e:
            return _text_result(str(e), is_error=True)
        except ManorApiError as e:
            return _text_result({"status": e.status_code, "error": e.detail}, is_error=True)
        except Exception:
            logger.exception("MCP tool %s failed", tool.name)
            return _text_result(f"{tool.name} failed with an internal error", is_error=True)
