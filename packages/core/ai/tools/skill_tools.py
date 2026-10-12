"""Skill tools — let agents invoke, create, list, and manage skills."""
from __future__ import annotations


from packages.core.ai.runtime.skills import (
    runtime_create_skill_action,
    runtime_delete_skill_action,
    runtime_draft_skill_action,
    runtime_get_skill_details_action,
    runtime_invoke_skill_action,
    runtime_list_skills_action,
    runtime_update_skill_action,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


async def _invoke_skill_handler(
    entity_id: str = "",
    skill_id: str = "",
    input: str = "",
    params=None,
    conversation_id: str = "",
    user_id: str = "",
    bundle_file: str | None = None,
    char_offset: int | None = None,
    **kwargs,
):
    """Invoke a runtime-visible skill by ID.

    ``skill`` remains accepted through ``kwargs`` for callers using the
    pre-runtime tool shape; new schema-generated calls use ``skill_id``.
    ``bundle_file`` loads one bundled file of an instructions-only skill.
    """
    resolved_skill_id = skill_id or str(kwargs.get("skill") or "")
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    # An empty bundle_file is an omitted one: models often send "" for unused
    # optional strings, and that must not turn a normal invocation into a read.
    bundle_file = str(bundle_file or "").strip() or None
    bundle_kwargs = (
        {"bundle_file": bundle_file, "bundle_char_offset": char_offset}
        if bundle_file
        else {}
    )
    return await runtime_invoke_skill_action(
        entity_id=entity_id,
        skill_id=resolved_skill_id,
        input_text=input,
        skill_params=params,
        runtime_context=runtime_context,
        user_id=user_id or None,
        conversation_id=conversation_id or runtime_context.conversation_id,
        **bundle_kwargs,
    )


async def _draft_skill_handler(
    entity_id: str = "", name: str = "", description: str = "", **kwargs,
):
    """Return clarifying questions for a skill request (does not create it)."""
    return await runtime_draft_skill_action(
        entity_id=entity_id,
        name=name,
        description=description,
        user_id=kwargs.get("user_id") or None,
    )


async def _create_skill_handler(
    entity_id: str = "", name: str = "", description: str = "", **kwargs,
):
    """Create a new skill from a natural-language description via LLM."""
    return await runtime_create_skill_action(
        entity_id=entity_id,
        name=name,
        description=description,
        category=kwargs.get("category"),
        tags=kwargs.get("tags", []),
        user_id=kwargs.get("user_id") or None,
    )


async def _list_skills_handler(entity_id: str = "", category: str = "", **kwargs):
    """List available skills."""
    return await runtime_list_skills_action(
        entity_id=entity_id,
        category=category or None,
        tool_kwargs=kwargs,
    )


async def _update_skill_handler(
    entity_id: str = "", skill_id: str = "", change_description: str = "", **kwargs,
):
    """Update an existing skill by describing what to change."""
    return await runtime_update_skill_action(
        entity_id=entity_id,
        skill_id=skill_id,
        change_description=change_description,
        user_id=kwargs.get("user_id") or None,
    )


async def _delete_skill_handler(entity_id: str = "", skill_id: str = "", **kwargs):
    """Delete a custom skill."""
    return await runtime_delete_skill_action(entity_id=entity_id, skill_id=skill_id)


async def _get_skill_handler(entity_id: str = "", skill_id: str = "", **kwargs):
    """Get full details of a skill."""
    return await runtime_get_skill_details_action(skill_id=skill_id, tool_kwargs=kwargs)


def get_tools():
    return [
        (
            {
                "type": "function",
                "function": {
                    "name": "invoke_skill",
                    "description": (
                        "Run a skill by ID when search_tools surfaces a "
                        "matching Skill or one was explicitly selected. Supplied "
                        "text/Markdown to a simple document: generate_file(kind='document'). "
                        "Social platform actions: a subscribed social operations skill, "
                        "else the Integration/MCP tool. Content-writing skills are for "
                        "drafts, blogs, articles, or a requested style."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {
                                "type": "string",
                                "description": "Skill ID (or legacy `skill`).",
                            },
                            "skill": {
                                "type": "string",
                                "description": "Legacy alias.",
                            },
                            "input": {"type": "string", "description": "Input text or instructions"},
                            "params": {
                                "type": "object",
                                "description": "Fixed composer/runtime settings, not prose.",
                                "additionalProperties": True,
                            },
                            "bundle_file": {
                                "type": "string",
                                "description": "Instructions-only skill's file, e.g. references/x.md.",
                            },
                            "char_offset": {
                                "type": "integer",
                                "description": "Resume offset.",
                            },
                        },
                        # No top-level anyOf/oneOf/allOf: Anthropic rejects such
                        # input_schema with HTTP 400 ("input_schema does not support
                        # oneOf, allOf, or anyOf at the top level"). The skill_id /
                        # skill alternative is enforced by the handler instead.
                        "required": ["input"],
                    },
                },
            },
            _invoke_skill_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "draft_skill",
                    "description": (
                        "Get clarifying questions for a new skill BEFORE creating it. "
                        "Call this first whenever the user's request is vague or "
                        "high-level: it returns the 1-3 most important questions (or "
                        "says the request is ready). Ask the user those questions, "
                        "then call create_skill with their answers folded into the "
                        "description. Does not create anything."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Proposed skill name"},
                            "description": {"type": "string", "description": "The user's request / what the skill should do"},
                        },
                        "required": ["description"],
                    },
                },
            },
            _draft_skill_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "create_skill",
                    "description": (
                        "Create a new reusable skill from a description. Uses AI to "
                        "generate a detailed, structured skill (SKILL.md-style system "
                        "prompt, optional standalone scripts, tools, input schema). "
                        "If the request is vague, call draft_skill first and fold the "
                        "user's answers into the description so the generated skill is "
                        "accurate."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Name for the skill (e.g. 'weekly-report-generator')"},
                            "description": {"type": "string", "description": "What the skill should do — be specific about steps, tools, and output format"},
                            "category": {"type": "string", "description": "Category (e.g. 'reporting', 'analysis', 'communication')"},
                        },
                        "required": ["name", "description"],
                    },
                },
            },
            _create_skill_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "List all available skills (platform + custom). Optionally filter by category.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "description": "Filter by category (optional)"},
                        },
                    },
                },
            },
            _list_skills_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "get_skill_details",
                    "description": (
                        "Get runtime-visible skill details. Discovery returns "
                        "descriptor metadata; full instructions are loaded only "
                        "when invoke_skill runs the skill."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "Skill ID to look up"},
                        },
                        "required": ["skill_id"],
                    },
                },
            },
            _get_skill_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "update_skill",
                    "description": "Update an existing skill by describing what to change. AI will patch only the affected parts.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "Skill ID to update"},
                            "change_description": {"type": "string", "description": "What to change (e.g. 'add email delivery step', 'change output to PDF')"},
                        },
                        "required": ["skill_id", "change_description"],
                    },
                },
            },
            _update_skill_handler,
        ),
        (
            {
                "type": "function",
                "function": {
                    "name": "delete_skill",
                    "description": "Delete a custom skill. Platform skills cannot be deleted.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string", "description": "Skill ID to delete"},
                        },
                        "required": ["skill_id"],
                    },
                },
            },
            _delete_skill_handler,
        ),
    ]
