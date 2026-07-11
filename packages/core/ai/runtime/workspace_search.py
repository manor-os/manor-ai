from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def runtime_workspace_search(
    *,
    entity_id: str = "",
    workspace_id: str = "",
    **kwargs: Any,
) -> str:
    """Execute workspace-scoped search through the Runtime boundary."""

    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
        runtime_tool_call_context_is_external_customer,
        runtime_tool_call_context_is_public_customer,
    )

    context = runtime_tool_call_context_from_kwargs(kwargs)
    workspace_id = str(workspace_id or context.workspace_id or "").strip()
    if not workspace_id:
        return json.dumps({"error": "No workspace context — this tool only works inside a workspace chat."})

    query = kwargs.get("query", "")
    category = kwargs.get("category")
    status = kwargs.get("status")
    external_customer = runtime_tool_call_context_is_external_customer(kwargs)
    public_customer = runtime_tool_call_context_is_public_customer(kwargs)

    if external_customer:
        requested_category = str(category or "all").strip().lower()
        if requested_category not in {"", "all", "knowledge"}:
            return (
                "No customer-visible results found. External customer chats can only "
                "search workspace knowledge that has been explicitly marked client-visible."
            )
        category = "knowledge"

    try:
        from packages.core.database import async_session
        from packages.core.workspace_chat.context import workspace_search

        async with async_session() as db:
            return await workspace_search(
                db,
                workspace_id,
                entity_id,
                query=query,
                category=category,
                status=status,
                external_client=external_customer,
                public_agent_client=public_customer,
            )
    except Exception as exc:
        logger.warning("workspace_search failed: %s", exc, exc_info=True)
        return json.dumps({"error": f"Search failed: {exc}"})
