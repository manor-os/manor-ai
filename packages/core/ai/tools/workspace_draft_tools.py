"""Workspace draft tools — let any chat agent kick off a conversational
workspace creation flow.

When a user says "let's set up another workspace for my YouTube channel"
inside an existing chat, the agent starts a draft and keeps updating it in
the same conversation. The frontend projects the latest draft into its
right-side output panel.
"""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime.workspace_drafts import (
    runtime_continue_workspace_draft_action,
    runtime_start_workspace_draft_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

logger = logging.getLogger(__name__)


START_WORKSPACE_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "start_workspace_draft",
        "description": (
            "Begin a conversational workspace creation flow. Use when the "
            "user expresses intent to create a new workspace (e.g. 'set up "
            "a workspace for my Twitter growth project'). The Workspace is "
            "configured in the current Chat and shown in its output panel. "
            "Returns a draft id for later turns. Do NOT use this for "
            "configuring an existing workspace -- only for creating new ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initial_brief": {
                    "type": "string",
                    "description": (
                        "The user's intent in their own words, e.g. 'a "
                        "workspace for my YouTube channel that helps me "
                        "grow to 100k subscribers'. Optional. Pre-seeds "
                        "the conversation so the assistant doesn't repeat "
                        "the opening question."
                    ),
                },
            },
        },
    },
}


CONTINUE_WORKSPACE_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "continue_workspace_draft",
        "description": (
            "Update an existing Workspace draft in the current Chat. Use the "
            "draft_id returned by start_workspace_draft whenever the user "
            "adds, removes, or changes Workspace configuration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "The active Workspace draft id.",
                },
                "message": {
                    "type": "string",
                    "description": "The requested configuration change.",
                },
            },
            "required": ["draft_id", "message"],
        },
    },
}


async def _start_workspace_draft_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    runtime_context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
    )
    return await runtime_start_workspace_draft_action(
        entity_id=runtime_context.entity_id or entity_id,
        user_id=runtime_context.user_id or user_id,
        initial_brief=kwargs.get("initial_brief"),
        runtime_envelope=runtime_context.runtime_envelope,
    )


async def _continue_workspace_draft_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    return await runtime_continue_workspace_draft_action(
        entity_id=entity_id,
        user_id=user_id,
        draft_id=str(kwargs.get("draft_id") or ""),
        message=str(kwargs.get("message") or ""),
    )


def get_tools():
    return [
        (START_WORKSPACE_DRAFT_SCHEMA, _start_workspace_draft_handler),
        (CONTINUE_WORKSPACE_DRAFT_SCHEMA, _continue_workspace_draft_handler),
    ]
