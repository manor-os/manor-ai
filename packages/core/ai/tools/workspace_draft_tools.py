"""Workspace draft tools — let any chat agent kick off a conversational
workspace creation flow.

When a user says "let's set up another workspace for my YouTube channel"
inside an existing chat, the agent calls ``start_workspace_draft`` with
the brief. The tool returns a ``draft_id`` and a deep link the frontend
can render as a CTA. The actual conversational creation continues in the
draft chat at /workspaces/new?draft=<id>; this tool only opens the door.
"""
from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime.workspace_drafts import runtime_start_workspace_draft_action

logger = logging.getLogger(__name__)


START_WORKSPACE_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "start_workspace_draft",
        "description": (
            "Begin a conversational workspace creation flow. Use when the "
            "user expresses intent to create a new workspace (e.g. 'set up "
            "a workspace for my Twitter growth project'). The user finishes "
            "the creation in a dedicated draft chat at "
            "/workspaces/new?draft=<id>. Returns the draft id and the deep "
            "link URL the UI should surface as a CTA. Do NOT use this for "
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


async def _start_workspace_draft_handler(
    entity_id: str = "", user_id: str = "", **kwargs: Any,
) -> str:
    return await runtime_start_workspace_draft_action(
        entity_id=entity_id,
        user_id=user_id,
        initial_brief=kwargs.get("initial_brief"),
    )


def get_tools():
    return [
        (START_WORKSPACE_DRAFT_SCHEMA, _start_workspace_draft_handler),
    ]
