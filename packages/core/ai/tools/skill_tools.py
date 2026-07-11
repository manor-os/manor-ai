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
    skill: str = "",
    input: str = "",
    params=None,
    conversation_id: str = "",
    user_id: str = "",
    **kwargs,
):
    """Invoke a skill by name or slug."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    return await runtime_invoke_skill_action(
        entity_id=entity_id,
        skill=skill,
        input_text=input,
        skill_params=params,
        runtime_context=runtime_context,
        user_id=user_id or None,
        conversation_id=conversation_id or runtime_context.conversation_id,
    )


async def _draft_skill_handler(
    entity_id: str = "", name: str = "", description: str = "", **kwargs,
):
    """Return clarifying questions for a skill request (does not create it)."""
    return await runtime_draft_skill_action(
        entity_id=entity_id,
        name=name,
        description=description,
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
                        "Invoke a reusable skill by name. Skills are pre-built prompt+tool "
                        "chains or sandboxed script workflows for specialized tasks like "
                        "'write_email', 'research_topic', document generation, or complex "
                        "file editing. Use this when a matching entry appears in Available "
                        "Skills; use generate_document_file only for direct conversion of "
                        "already-supplied text/Markdown into a simple document. For external "
                        "social platform operations, invoke a subscribed social operations "
                        "skill when one is available; otherwise use search_tools to load the "
                        "relevant Integration/MCP tool. Content-writing skills are for "
                        "drafts, blogs, articles, or an explicitly requested style."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string", "description": "Skill name or slug to invoke"},
                            "input": {"type": "string", "description": "Input text/instructions for the skill"},
                            "params": {
                                "type": "object",
                                "description": (
                                    "Structured options for the selected skill. Use this for fixed composer/runtime "
                                    "settings instead of encoding them in natural language."
                                ),
                                "additionalProperties": True,
                            },
                        },
                        "required": ["skill", "input"],
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
