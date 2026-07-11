"""System/utility tools — time, entity info."""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime import (
    runtime_get_current_time_action,
    runtime_get_entity_info_action,
    runtime_tool_call_context_from_kwargs,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

GET_CURRENT_TIME_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Get the current date and time in UTC and the user's local timezone.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

GET_ENTITY_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_entity_info",
        "description": "Get basic information about the current entity (organization name, settings).",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _get_current_time(entity_id: str, **kwargs: Any) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = kwargs.get("user_id") or runtime_context.user_id
    return await runtime_get_current_time_action(user_id=user_id if isinstance(user_id, str) else None)


async def _get_entity_info(entity_id: str, **kwargs: Any) -> str:
    return await runtime_get_entity_info_action(entity_id=entity_id)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (GET_CURRENT_TIME_SCHEMA, _get_current_time),
        (GET_ENTITY_INFO_SCHEMA, _get_entity_info),
    ]
