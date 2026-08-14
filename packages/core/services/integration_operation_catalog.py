"""Normalize MCP tool schemas for human-configured Workflow connectors.

Built-in integrations expose their live ``list_tools()`` contract. Remote MCP
servers persist the same contract in ``mcp_servers.tools_cached``. This module
turns both shapes into one safe, credential-free operation catalog for the web
Workflow editor.
"""
from __future__ import annotations

from typing import Any, Iterable


_READ_PREFIXES = {
    "check", "describe", "download", "fetch", "find", "get", "inspect",
    "list", "lookup", "query", "read", "search", "status", "verify",
}
_DESTRUCTIVE_TOKENS = {
    "cancel", "clear", "delete", "disable", "disconnect", "remove",
    "revoke", "trash", "unpublish",
}
_RESOURCE_ARGUMENTS = (
    ("pull_number", "Pull request"),
    ("issue_number", "Issue"),
    ("message_id", "Message"),
    ("message_ids", "Message"),
    ("draft_id", "Draft"),
    ("thread_id", "Thread"),
    ("comment_id", "Comment"),
    ("attachment_id", "Attachment"),
    ("file_id", "File"),
    ("folder_id", "Folder"),
    ("calendar_id", "Calendar"),
    ("event_id", "Event"),
    ("channel_id", "Channel"),
    ("chat_id", "Chat"),
    ("video_id", "Video"),
    ("playlist_id", "Playlist"),
    ("invoice_id", "Invoice"),
    ("order_id", "Order"),
    ("product_id", "Product"),
    ("customer_id", "Customer"),
    ("contact_id", "Contact"),
)
_RESOURCE_PHRASES = (
    ("pull_request", "Pull request"),
    ("check_run", "Check run"),
    ("workflow_run", "Workflow run"),
    ("named_range", "Named range"),
)
_RESOURCE_TOKENS = {
    "account", "artifact", "attachment", "branch", "calendar", "caption",
    "card", "channel", "chat", "comment", "contact", "customer", "draft",
    "event", "file", "folder", "group", "invoice", "issue", "label",
    "meeting", "message", "order", "page", "payment", "playlist", "post",
    "product", "profile", "range", "repository", "sheet", "subscription",
    "thread", "transaction", "user", "video", "workbook", "worksheet",
}


def _humanize(value: str) -> str:
    return " ".join(part for part in value.replace("-", "_").split("_") if part).capitalize()


def _iter_cached_tools(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for key in ("tools", "items", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                yield from nested
                return
        for key, item in value.items():
            if isinstance(item, dict):
                spec = dict(item)
                spec.setdefault("name", key)
                yield spec
            elif isinstance(item, str):
                yield {"name": key, "description": item}
        return
    if isinstance(value, (list, tuple)):
        yield from value


def _input_schema(raw: dict[str, Any]) -> dict[str, Any]:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    schema = (
        raw.get("inputSchema")
        or raw.get("input_schema")
        or raw.get("parameters")
        or function.get("parameters")
        or {"type": "object", "properties": {}}
    )
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = dict(schema)
    normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    if not isinstance(normalized.get("required"), list):
        normalized["required"] = []
    return normalized


def _operation_name(raw: dict[str, Any], server_key: str) -> str:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    name = str(function.get("name") or raw.get("name") or raw.get("tool") or "").strip()
    prefix = f"mcp__{server_key}__"
    return name[len(prefix):] if name.startswith(prefix) else name


def _operation_description(raw: dict[str, Any]) -> str:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    return str(function.get("description") or raw.get("description") or "").strip()


def _operation_resource(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties") or {}
    for argument, resource in _RESOURCE_ARGUMENTS:
        if argument in properties:
            return resource

    normalized = name.lower().replace("-", "_")
    for phrase, resource in _RESOURCE_PHRASES:
        if phrase in normalized:
            return resource
    for token in normalized.split("_"):
        singular = token[:-1] if token.endswith("s") and len(token) > 3 else token
        if singular == "repo":
            return "Repository"
        if singular in _RESOURCE_TOKENS:
            return _humanize(singular)
    return "General"


def _operation_effect(name: str) -> str:
    tokens = set(name.lower().replace("-", "_").split("_"))
    if tokens & _DESTRUCTIVE_TOKENS:
        return "destructive"
    first = next(iter(name.lower().replace("-", "_").split("_")), "")
    return "read" if first in _READ_PREFIXES else "write"


def normalize_mcp_operations(
    server_key: str,
    raw_tools: Iterable[Any],
) -> list[dict[str, Any]]:
    """Return a stable, UI-ready operation list from MCP tool definitions."""
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = _operation_name(raw, server_key)
        if not name or name in seen:
            continue
        seen.add(name)
        schema = _input_schema(raw)
        operations.append({
            "name": name,
            "label": _humanize(name),
            "resource": _operation_resource(name, schema),
            "description": _operation_description(raw),
            "effect": _operation_effect(name),
            "input_schema": schema,
        })
    return sorted(operations, key=lambda item: (item["resource"], item["label"]))


def integration_operation_catalog(
    *,
    server_key: str,
    transport: str,
    tools_cached: Any,
) -> tuple[list[dict[str, Any]], str]:
    """Load one server's current operation schemas without reading secrets."""
    if str(transport or "").lower() == "builtin":
        from packages.core.ai.mcp import get_module

        module = get_module(server_key)
        if module is not None:
            return normalize_mcp_operations(server_key, module.list_tools()), "builtin"

    return normalize_mcp_operations(server_key, _iter_cached_tools(tools_cached)), "cache"


__all__ = ["integration_operation_catalog", "normalize_mcp_operations"]
