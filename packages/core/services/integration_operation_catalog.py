"""Normalize MCP tool schemas for human-configured Workflow connectors.

Built-in integrations expose their live ``list_tools()`` contract. Remote MCP
servers persist the same contract in ``mcp_servers.tools_cached``. This module
turns both shapes into one safe, credential-free operation catalog for the web
Workflow editor.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPFactory,
    OfficialRemoteMCPProvider,
    OfficialRemoteMCPToolNameFactory,
)

logger = logging.getLogger(__name__)


# Nango is an OAuth/token transport, not a provider-specific webhook or MCP
# implementation. Keep this small capability contract next to the operation
# catalog so API cards and workflow tooling cannot infer unsupported behavior
# from a raw Nango proxy.
NANGO_PROVIDER_CAPABILITIES: dict[str, dict[str, Any]] = {
    "gmail": {
        "provider_config_key": "gmail",
        "provider": "gmail",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    },
    "google_calendar": {
        "provider_config_key": "google_calendar",
        "provider": "google-calendar",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    },
    "google_drive": {
        "provider_config_key": "google_drive",
        "provider": "google-drive",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    },
    "twitter": {
        "provider_config_key": "twitter",
        "provider": "twitter",
        "mode": "oauth_saas",
        "operations": {"read": True, "write": True, "inbound": False},
    },
    "outlook": {
        "provider_config_key": "outlook",
        "provider": "outlook",
        "mode": "bidirectional_chat",
        "operations": {"read": True, "write": True, "inbound": True},
    },
    "whatsapp": {
        "provider_config_key": "whatsapp",
        "provider": "whatsapp",
        "mode": "bidirectional_chat",
        "operations": {"read": True, "write": True, "inbound": True},
    },
}


def nango_provider_capability(provider_config_key: str) -> dict[str, Any] | None:
    """Return a defensive copy of the declared Nango provider contract."""
    key = str(provider_config_key or "").strip().lower()
    record = NANGO_PROVIDER_CAPABILITIES.get(key)
    if record is None:
        return None
    return {
        **record,
        "operations": dict(record.get("operations") or {}),
    }


class MCPServerKind(StrEnum):
    MANAGED = "managed"
    CUSTOM = "custom"


class MCPServerKindFactory:
    @staticmethod
    def from_server(server_key: object, transport: object) -> MCPServerKind:
        """Classify server ownership independently from its wire transport."""
        from packages.core.services.mcp_seed import is_managed_mcp_server_key

        if str(transport or "").strip().lower() == "builtin":
            return MCPServerKind.MANAGED
        if is_managed_mcp_server_key(server_key):
            return MCPServerKind.MANAGED
        return MCPServerKind.CUSTOM


_READ_PREFIXES = {
    "check",
    "describe",
    "download",
    "fetch",
    "find",
    "get",
    "inspect",
    "list",
    "lookup",
    "query",
    "read",
    "search",
    "status",
    "verify",
}
_DESTRUCTIVE_TOKENS = {
    "cancel",
    "clear",
    "delete",
    "disable",
    "disconnect",
    "remove",
    "revoke",
    "trash",
    "unpublish",
    "void",
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
    "account",
    "artifact",
    "attachment",
    "branch",
    "calendar",
    "caption",
    "card",
    "channel",
    "chat",
    "comment",
    "contact",
    "customer",
    "draft",
    "event",
    "file",
    "folder",
    "group",
    "invoice",
    "issue",
    "label",
    "meeting",
    "message",
    "order",
    "page",
    "payment",
    "playlist",
    "post",
    "product",
    "profile",
    "range",
    "repository",
    "sheet",
    "subscription",
    "thread",
    "transaction",
    "user",
    "video",
    "workbook",
    "worksheet",
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


def _output_schema(raw: dict[str, Any]) -> dict[str, Any] | None:
    for key in (
        "outputSchema",
        "output_schema",
        "resultSchema",
        "result_schema",
        "returns",
    ):
        schema = raw.get(key)
        if isinstance(schema, dict):
            return dict(schema)
    return None


def _operation_name(raw: dict[str, Any], server_key: str) -> str:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    name = str(function.get("name") or raw.get("name") or raw.get("tool") or "").strip()
    prefix = f"mcp__{server_key}__"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _operation_tool_name(
    raw: dict[str, Any],
    server_key: str,
    operation_name: str,
) -> str:
    explicit = str(raw.get("tool_name") or "").strip()
    if explicit:
        return explicit
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    function_name = str(function.get("name") or "").strip()
    if function_name.startswith("mcp__"):
        return function_name
    try:
        provider = OfficialRemoteMCPProvider(server_key)
    except ValueError:
        safe_server = server_key.replace("-", "_").replace(".", "_")
        safe_action = operation_name.replace("-", "_").replace(".", "_")
        return f"mcp__{safe_server}__{safe_action}"
    return OfficialRemoteMCPToolNameFactory.create(provider, operation_name)


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


def _operation_effect(server_key: str, name: str, raw: dict[str, Any]) -> MCPActionEffect:
    try:
        provider = OfficialRemoteMCPProvider(server_key)
    except ValueError:
        pass
    else:
        annotations = raw.get("annotations")
        return OfficialRemoteMCPFactory.resolve_action_effect(
            provider,
            name,
            annotations=annotations if isinstance(annotations, dict) else None,
        )

    explicit = str(raw.get("effect") or "").strip().lower()
    try:
        return MCPActionEffect(explicit)
    except ValueError:
        pass
    annotations = raw.get("annotations")
    if isinstance(annotations, dict):
        if annotations.get("destructiveHint") is True:
            return MCPActionEffect.DESTRUCTIVE
        if annotations.get("readOnlyHint") is True:
            return MCPActionEffect.READ

    tokens = set(name.lower().replace("-", "_").split("_"))
    if tokens & _DESTRUCTIVE_TOKENS:
        return MCPActionEffect.DESTRUCTIVE
    first = next(iter(name.lower().replace("-", "_").split("_")), "")
    return MCPActionEffect.READ if first in _READ_PREFIXES else MCPActionEffect.WRITE


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
        output_schema = _output_schema(raw)
        operations.append(
            {
                "name": name,
                "tool_name": _operation_tool_name(raw, server_key, name),
                "label": _humanize(name),
                "resource": _operation_resource(name, schema),
                "description": _operation_description(raw),
                "effect": _operation_effect(server_key, name, raw).value,
                "input_schema": schema,
                **({"output_schema": output_schema} if output_schema is not None else {}),
                **(
                    {
                        "account_ids": [
                            str(account_id)
                            for account_id in raw.get("account_ids", [])
                            if str(account_id or "").strip()
                        ]
                    }
                    if raw.get("account_ids")
                    else {}
                ),
                **(
                    {
                        "account_options": [
                            dict(option)
                            for option in raw.get("account_options", [])
                            if isinstance(option, dict)
                        ]
                    }
                    if raw.get("account_options")
                    else {}
                ),
                **(
                    {
                        "account_input_schemas": {
                            str(account_id): _input_schema({"inputSchema": schema})
                            for account_id, schema in raw.get(
                                "account_input_schemas", {}
                            ).items()
                            if str(account_id or "").strip()
                            and isinstance(schema, dict)
                        }
                    }
                    if isinstance(raw.get("account_input_schemas"), dict)
                    and raw.get("account_input_schemas")
                    else {}
                ),
                **(
                    {"requires_explicit_account": True}
                    if raw.get("requires_explicit_account") is True
                    else {}
                ),
                **(
                    {
                        "account_output_schemas": {
                            str(account_id): dict(schema)
                            for account_id, schema in raw.get(
                                "account_output_schemas", {}
                            ).items()
                            if str(account_id or "").strip()
                            and isinstance(schema, dict)
                        }
                    }
                    if isinstance(raw.get("account_output_schemas"), dict)
                    and raw.get("account_output_schemas")
                    else {}
                ),
                **(
                    {"supports_all_accounts": raw["supports_all_accounts"]}
                    if isinstance(raw.get("supports_all_accounts"), bool)
                    else {}
                ),
            }
        )
    return sorted(operations, key=lambda item: (item["resource"], item["label"]))


def integration_operation_catalog(
    *,
    server_key: str,
    transport: str,
    tools_cached: Any,
) -> tuple[list[dict[str, Any]], str]:
    """Load one server's current operation schemas without reading secrets."""
    from packages.core.services.mcp_account_tool_catalog import MCPToolCatalogSource

    if str(transport or "").lower() == "builtin":
        from packages.core.ai.mcp import get_module

        module = get_module(server_key)
        if module is not None:
            return (
                normalize_mcp_operations(server_key, module.list_tools()),
                MCPToolCatalogSource.BUILTIN.value,
            )
        # A stale cache must never advertise executable operations when the
        # in-process implementation has disappeared or was disabled.
        return [], "unavailable"

    return (
        normalize_mcp_operations(server_key, _iter_cached_tools(tools_cached)),
        MCPToolCatalogSource.CACHE.value,
    )


async def actor_integration_operation_catalog(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    server_key: str,
    transport: str,
    endpoint: str | None,
    tools_cached: Any,
) -> tuple[list[dict[str, Any]], str]:
    if str(transport or "").lower() == "http":
        try:
            OfficialRemoteMCPProvider(server_key)
        except ValueError:
            pass
        else:
            from packages.core.services.integration_account_service import load_runtime_integration_registry
            from packages.core.services.mcp_account_tool_catalog import actor_mcp_tool_cache

            try:
                registry = await load_runtime_integration_registry(
                    db,
                    user_id=user_id,
                    entity_id=entity_id,
                    provider_keys=[server_key],
                )
                account_cache = await actor_mcp_tool_cache(
                    db,
                    provider=server_key,
                    registry=registry,
                    endpoint=endpoint,
                )
            except Exception:
                logger.warning(
                    "Account-scoped MCP catalog load failed for %s; using fallback",
                    server_key,
                    exc_info=True,
                )
                account_cache = None
            if account_cache:
                operations = normalize_mcp_operations(
                    server_key, _iter_cached_tools(account_cache)
                )
                if operations:
                    from packages.core.services.mcp_account_tool_catalog import (
                        MCPToolCatalogSource,
                    )

                    return operations, MCPToolCatalogSource.ACCOUNT_DISCOVERY.value
    return integration_operation_catalog(
        server_key=server_key,
        transport=transport,
        tools_cached=tools_cached,
    )


__all__ = [
    "MCPServerKind",
    "MCPServerKindFactory",
    "actor_integration_operation_catalog",
    "NANGO_PROVIDER_CAPABILITIES",
    "integration_operation_catalog",
    "normalize_mcp_operations",
    "nango_provider_capability",
]
