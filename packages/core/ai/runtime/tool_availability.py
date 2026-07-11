from __future__ import annotations

import json

from packages.core.ai.runtime.tool_discovery import (
    runtime_apply_mcp_availability,
    runtime_mark_match_available,
    runtime_mark_mcp_match_unavailable,
    runtime_mcp_provider_from_tool_name,
    runtime_sort_available_matches,
)


async def runtime_annotate_tool_availability(
    matches: list[dict],
    entity_id: str,
    user_id: str,
) -> list[dict]:
    """Attach runtime availability metadata to search results.

    Non-MCP tools are always callable from this layer's perspective. MCP tools
    fail closed unless the current entity/user can prove connected credentials.
    """
    if not entity_id:
        for match in matches:
            runtime_mark_mcp_match_unavailable(
                match,
                "entity_id is required for MCP tool discovery.",
            )
        return matches

    try:
        from packages.core.database import async_session
        from packages.core.models.mcp import MCPServer
        from packages.core.services.agent_permission_service import can_use_integration
        from packages.core.services.integration_service import coming_soon_servers
        from sqlalchemy import select
    except Exception:
        for match in matches:
            runtime_mark_mcp_match_unavailable(match, "availability check failed")
        return matches

    provider_status: dict[str, dict] = {}
    provider_metadata: dict[str, dict] = {}

    async with async_session() as db:
        providers = sorted({
            provider
            for match in matches
            if (
                provider := runtime_mcp_provider_from_tool_name(
                    str(match.get("name") or "")
                )
            )
        })
        if providers:
            rows = list((await db.execute(
                select(MCPServer).where(MCPServer.server_key.in_(providers))
            )).scalars().all())
            coming_soon = coming_soon_servers()
            provider_metadata = {
                row.server_key: {
                    "name": row.name,
                    "auth_type": row.auth_type,
                    "transport": row.transport,
                    "endpoint": row.endpoint,
                    "coming_soon": row.server_key in coming_soon,
                }
                for row in rows
            }

        for match in matches:
            name = str(match.get("name") or "")
            if not name.startswith("mcp__"):
                runtime_mark_match_available(match)
                continue
            provider = runtime_mcp_provider_from_tool_name(name)
            if not provider:
                runtime_mark_match_available(match)
                continue
            metadata = provider_metadata.get(provider, {})
            if provider not in provider_status:
                try:
                    decision = await can_use_integration(
                        db,
                        user_id=user_id or "",
                        entity_id=entity_id,
                        provider=provider,
                        allow_env_fallback=False,
                    )
                    provider_status[provider] = {
                        "available": decision.allowed,
                        "reason": decision.reason,
                        "scope": decision.scope,
                    }
                except Exception:
                    provider_status[provider] = {
                        "available": False,
                        "reason": "availability check failed",
                        "scope": "none",
                    }
            runtime_apply_mcp_availability(
                match,
                provider=provider,
                metadata=metadata,
                status=provider_status[provider],
            )

    return runtime_sort_available_matches(matches)


async def runtime_blocked_mcp_call_result(
    name: str,
    entity_id: str,
    user_id: str,
) -> str:
    """Fail closed before any MCP provider code runs."""
    parts = name.split("__", 2)
    if len(parts) < 3:
        return json.dumps({"error": f"Malformed MCP tool name: {name}"})
    _, provider, tool_name = parts
    if not entity_id:
        return json.dumps({
            "server": provider,
            "tool": tool_name,
            "error": "credentials_unavailable",
            "reason": "entity_id is required for MCP tool calls.",
            "scope": "none",
        })

    try:
        from packages.core.database import async_session
        from packages.core.services.agent_permission_service import can_use_integration

        async with async_session() as db:
            decision = await can_use_integration(
                db,
                user_id=user_id or "",
                entity_id=entity_id,
                provider=provider,
                allow_env_fallback=False,
            )
    except Exception:
        return json.dumps({
            "server": provider,
            "tool": tool_name,
            "error": "credentials_unavailable",
            "reason": "MCP credential availability check failed; refusing to call an unverified MCP tool.",
            "scope": "none",
        })

    if decision.allowed:
        return ""

    return json.dumps({
        "server": provider,
        "tool": tool_name,
        "error": "credentials_unavailable",
        "reason": decision.reason,
        "scope": decision.scope,
        "suggested_tool": "generate_file" if tool_name == "generate_video" else None,
    })
