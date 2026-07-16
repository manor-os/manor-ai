from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from packages.core.ai.runtime.skill_routing import (
    is_local_coding_skill,
    local_coding_cli_intent,
)
from packages.core.ai.runtime.prompt_tools import runtime_prompt_tool_name

logger = logging.getLogger(__name__)

_MANUAL_SKILL_BASE_TOOL_NAMES = (
    "invoke_skill",
    "generate_file",
    "sandbox_exec",
    "sandbox_read_file",
    "sandbox_write_file",
    "sandbox_save_result",
    "sandbox_destroy",
)


def runtime_message_text_for_intent(message: str | list[dict]) -> str:
    """Compact multimodal user content into text for turn intent decisions."""
    if isinstance(message, str):
        return message
    return " ".join(
        str(part.get("text") or part.get("image_url", {}).get("url", "")[:32] or "")
        for part in message
        if isinstance(part, dict)
    ).strip()


def runtime_manual_skill_context(manual_skill_refs: list[dict] | None) -> str | None:
    if not manual_skill_refs:
        return None
    lines = [
        "## Manual Skill Selection",
        "The user explicitly selected these skills for this turn. They are invoked before the first model response; use their tool results as primary context for the answer.",
    ]
    for skill in manual_skill_refs:
        label = skill.get("display_name") or skill.get("name") or skill.get("slug") or skill.get("id")
        slug = skill.get("slug") or skill.get("id")
        desc = (skill.get("description") or "").strip()
        suffix = f" - {desc[:160]}" if desc else ""
        lines.append(f"- {label} (`{slug}`){suffix}")
    return "\n".join(lines)


def runtime_manual_skill_input(message: str | list[dict]) -> str:
    text = runtime_message_text_for_intent(message).strip()
    return text or "Use the manually selected skill with the current conversation context."


def runtime_parse_manual_skill_ids(manual_skill_ids: str | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (manual_skill_ids or "").split(","):
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def runtime_skill_ref_dict(skill: Any) -> dict:
    return {
        "id": getattr(skill, "id", None),
        "slug": getattr(skill, "slug", None),
        "name": getattr(skill, "name", None),
        "display_name": getattr(skill, "display_name", None),
        "description": getattr(skill, "description", None),
        "category": getattr(skill, "category", None),
        "output_format": getattr(skill, "output_format", None),
        "tags": getattr(skill, "tags", None) or [],
        "source": "builtin" if getattr(skill, "entity_id", None) is None else "entity",
    }


async def runtime_resolve_manual_skill_refs(
    db: Any,
    *,
    entity_id: str,
    agent_id: str | None,
    manual_skill_ids: str | None,
    list_skills_fn: Callable[..., Any] | None = None,
    list_skills_for_agent_fn: Callable[..., Any] | None = None,
) -> tuple[list[dict], list[str]]:
    requested = runtime_parse_manual_skill_ids(manual_skill_ids)
    if not requested:
        return [], []

    if agent_id:
        if list_skills_for_agent_fn is None:
            from packages.core.services.skill_service import list_skills_for_agent
            list_skills_for_agent_fn = list_skills_for_agent
        skills = await list_skills_for_agent_fn(db, entity_id, agent_id)
    else:
        if list_skills_fn is None:
            from packages.core.services.skill_service import list_skills
            list_skills_fn = list_skills
        skills = await list_skills_fn(db, entity_id)

    lookup: dict[str, Any] = {}
    for skill in skills:
        for key in (getattr(skill, "id", None), getattr(skill, "slug", None), getattr(skill, "name", None)):
            if key:
                lookup[str(key)] = skill

    resolved: list[dict] = []
    seen_ids: set[str] = set()
    missing: list[str] = []
    for skill_id in requested:
        skill = lookup.get(skill_id)
        if not skill:
            missing.append(skill_id)
            continue
        ref = runtime_skill_ref_dict(skill)
        ref_id = str(ref.get("id") or ref.get("slug") or ref.get("name") or "")
        if ref_id and ref_id not in seen_ids:
            seen_ids.add(ref_id)
            resolved.append(ref)

    return resolved, missing


def runtime_manual_skill_token_variants(skill: dict) -> list[str]:
    values = [skill.get("slug"), skill.get("name"), skill.get("display_name")]
    tokens: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        tokens.append(f"/{text}")
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text.lower()).strip("-")
        if slug:
            tokens.append(f"/{slug}")
    return list(dict.fromkeys(tokens))


def runtime_strip_manual_skill_tokens(message: str, manual_skill_refs: list[dict]) -> str:
    cleaned = message
    for skill in manual_skill_refs:
        for token in runtime_manual_skill_token_variants(skill):
            escaped = re.escape(token)
            cleaned = re.sub(rf"(^|\s){escaped}(?=\s|$)", " ", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def runtime_message_with_manual_skill_marker(message: str, manual_skill_refs: list[dict]) -> str:
    if not manual_skill_refs:
        return message
    labels = [
        str(skill.get("display_name") or skill.get("name") or skill.get("slug") or skill.get("id"))
        for skill in manual_skill_refs
    ]
    base = runtime_strip_manual_skill_tokens(message, manual_skill_refs)
    marker = f"[Skill: {', '.join(labels)}]"
    return f"{base}\n{marker}".strip() if base else marker


def runtime_manual_skill_forced_tool_calls(
    manual_skill_refs: list[dict] | None,
    message: str | list[dict],
) -> list[dict]:
    if not manual_skill_refs:
        return []
    default_skill_input = runtime_manual_skill_input(message)
    calls: list[dict] = []
    for skill in manual_skill_refs:
        # Prefer slug or name over id so clients can render readable labels.
        skill_ref = skill.get("slug") or skill.get("name") or skill.get("id")
        if not skill_ref:
            continue
        skill_input = str(skill.get("input") or "").strip() or default_skill_input
        calls.append({
            "name": "invoke_skill",
            "arguments": {
                "skill": str(skill_ref),
                "input": skill_input,
            },
        })
    return calls


def runtime_manual_skill_omits_generate_file(
    *,
    manual_skill_refs: list[dict] | None,
    message: str | list[dict],
) -> bool:
    if not manual_skill_refs:
        return False
    message_text = runtime_message_text_for_intent(message)
    has_attached_image_context = (
        "<attached_image_rules>" in message_text
        or "[Image:" in message_text
        or "[Image from KB:" in message_text
    )
    if not has_attached_image_context:
        return False
    for ref in manual_skill_refs:
        category = str(ref.get("category") or "").strip().lower()
        output_format = str(ref.get("output_format") or "").strip().lower()
        tags = {
            str(tag).strip().lower()
            for tag in (ref.get("tags") or [])
            if str(tag or "").strip()
        }
        if (
            output_format == "file"
            or category in {"document-generation", "file-generation"}
            or "document-generation" in tags
            or "file-generation" in tags
        ):
            return True
    return False


def runtime_apply_manual_skill_tool_surface(
    *,
    tools: list[dict],
    allowed_tool_names: set[str],
    manual_skill_refs: list[dict] | None,
    message: str | list[dict],
    get_schema,
    disable_tools: bool = False,
) -> tuple[list[dict], set[str]]:
    """Ensure manual skill turns can invoke skills without leaking conflicting tools."""
    if not manual_skill_refs or disable_tools:
        return tools, allowed_tool_names

    updated_tools = list(tools or [])
    updated_allowed = set(allowed_tool_names or set())
    existing_tool_names = {
        name for name in (runtime_prompt_tool_name(tool) for tool in updated_tools) if name
    }
    omit_generate_file = runtime_manual_skill_omits_generate_file(
        manual_skill_refs=manual_skill_refs,
        message=message,
    )

    if omit_generate_file:
        updated_tools = [
            tool
            for tool in updated_tools
            if runtime_prompt_tool_name(tool) != "generate_file"
        ]
        existing_tool_names.discard("generate_file")
        updated_allowed.discard("generate_file")

    for tool_name in _MANUAL_SKILL_BASE_TOOL_NAMES:
        if tool_name == "generate_file" and omit_generate_file:
            continue
        schema = get_schema(tool_name)
        if schema and tool_name not in existing_tool_names:
            updated_tools.append(schema)
            existing_tool_names.add(tool_name)
        if schema:
            updated_allowed.add(tool_name)

    return updated_tools, updated_allowed


async def runtime_auto_skill_forced_tool_calls(
    ctx: Any,
    message: str | list[dict],
) -> list[dict]:
    if getattr(ctx, "manual_skill_selected", False) or not getattr(ctx, "db", None) or not getattr(ctx, "entity_id", None):
        return []
    if "invoke_skill" not in set(getattr(ctx, "tool_names", None) or []):
        return []
    if not local_coding_cli_intent(getattr(ctx, "active_user_message", None)):
        return []
    try:
        if getattr(ctx, "agent_id", None):
            from packages.core.services.skill_service import list_skills_for_agent
            skills = await list_skills_for_agent(
                ctx.db,
                ctx.entity_id,
                ctx.agent_id,
                workspace_id=getattr(ctx, "workspace_id", None),
            )
        else:
            from packages.core.services.skill_service import list_skills
            skills = await list_skills(ctx.db, ctx.entity_id)
    except Exception:
        logger.debug("Auto skill resolution failed", exc_info=True)
        return []
    for skill in skills:
        slug = getattr(skill, "slug", "") or ""
        name = getattr(skill, "name", "") or getattr(skill, "display_name", "") or ""
        if not is_local_coding_skill(slug, name):
            continue
        skill_ref = slug or name
        if not skill_ref:
            continue
        return [{
            "name": "invoke_skill",
            "arguments": {
                "skill": str(skill_ref),
                "input": runtime_manual_skill_input(message),
            },
        }]
    return []


def runtime_forced_tool_calls_for_turn(
    ctx: Any,
    manual_skill_refs: list[dict] | None,
    message: str | list[dict],
) -> list[dict]:
    manual_calls = runtime_manual_skill_forced_tool_calls(manual_skill_refs, message)
    if manual_calls:
        return manual_calls
    return list(getattr(ctx, "auto_forced_tool_calls", None) or [])
