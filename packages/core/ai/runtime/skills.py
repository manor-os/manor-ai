from __future__ import annotations

import json
from enum import StrEnum
from uuid import UUID
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from packages.core.ai.runtime.capabilities import (
    allowed_tools_for_profile,
    capabilities_for_tool_names,
)
from packages.core.ai.runtime.artifacts import runtime_input_with_artifact_context
from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.harness import RuntimeHarness
from packages.core.ai.runtime.middleware import apply_runtime_middleware
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.sources import RUNTIME_SKILL_GENERATOR_SOURCE
from packages.core.ai.terminal_stops import LOCAL_CODING_DISPATCHED_STOP_REASON
from packages.core.ai.runtime.skill_routing import filter_skills_for_runtime_turn
from packages.core.ai.runtime.skill_routing import (
    external_platform_action_intent,
    is_chrome_skill,
    is_integration_child_skill,
    is_integration_parent_skill,
    is_linkedin_platform_skill,
    is_linkedin_route_skill,
    is_local_coding_skill,
    is_presentation_skill,
    is_social_platform_skill,
    is_social_platform_route_skill,
    is_youtube_platform_skill,
    is_youtube_route_skill,
    local_coding_cli_intent,
    linkedin_platform_operation_intent,
    named_integration_operation_intent,
    presentation_artifact_intent,
    presentation_fresh_creation_intent,
    runtime_approval_resume_intent,
    social_platform_action_intent,
    should_route_external_action_to_integration,
    skill_slug_and_name,
    youtube_platform_action_intent,
)
from packages.core.ai.runtime.integration_skill_registry import (
    integration_skill_route_for_message,
)
from packages.core.ai.runtime.skill_invocation_policy import (
    retain_required_skill_invocation_policies,
    render_skill_invocation_policy,
    trusted_skill_invocation_policy,
)
from packages.core.ai.runtime.chrome_routing import detect_chrome_local_browser_route
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.services.skill_bundle import parse_clarifying_questions


SkillSource = Literal["builtin", "entity", "agent_binding", "workspace_operation", "manual"]

_SKILL_CREATOR_ROOT = (
    Path(__file__).resolve().parents[1] / "skills" / "skill-creator"
)
_SKILL_CREATOR_GENERATION_CONTRACT = (
    _SKILL_CREATOR_ROOT / "references" / "generation-contract.md"
)


@lru_cache(maxsize=1)
def runtime_skill_generation_contract() -> str:
    """Load the repository-controlled Skill Creator contract."""

    try:
        contract = _SKILL_CREATOR_GENERATION_CONTRACT.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "Packaged Skill Creator generation contract is unavailable"
        ) from exc
    if not contract:
        raise RuntimeError("Packaged Skill Creator generation contract is empty")
    return contract


def _system_prompt_with_skill_creation_contract(base_prompt: str) -> str:
    return (
        f"{base_prompt.strip()}\n\n"
        "## Packaged Skill Creator Contract\n\n"
        f"{runtime_skill_generation_contract()}"
    )


RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT = """\
You are an expert skill designer for an AI agent platform. Given a \
natural-language description, produce a single JSON object that fully defines \
a reusable skill at production quality.

Follow the packaged Skill Creator contract below as the authoritative schema and
quality standard. Prefer progressive disclosure and semantic trigger guidance.
Do not force an arbitrary size such as 200-300 lines; include only instructions
that improve reliable execution.

Output only the JSON object, with no Markdown fences or surrounding prose."""

RUNTIME_SKILL_REVIEW_SYSTEM_PROMPT = (
    "You are a strict skill quality reviewer. Hold skills to a high bar: a "
    "good skill is focused, executable, safe, and has a discovery-ready "
    "description. Do not treat 200-300 lines as a quality target. Be specific "
    "about every problem you find and follow the packaged contract below. "
    "When you return a refined spec, return the COMPLETE spec — including "
    "the unchanged scripts and references objects — never a partial one."
)

RUNTIME_SKILL_PATCH_SYSTEM_PROMPT = """\
You are a skill editor. You will receive the current skill definition as JSON \
and a description of what to change. Output an updated JSON object with **only \
the fields that changed**. Omit unchanged fields entirely.

Rules:
- Preserve the existing style and structure of any text fields you modify.
- If the system_prompt needs editing, include the full updated system_prompt.
- Preserve identity unless the requested change explicitly includes a rename.
- Keep the Skill concise and use progressive disclosure where appropriate.
- Follow the packaged Skill Creator contract below.
- Output **only** the JSON patch object, no markdown fences, no commentary."""

RUNTIME_SKILL_CLARIFY_SYSTEM_PROMPT = """\
You help design AI agent skills. The user gave a short request for a new skill. \
Before it is built, ask the 1-3 MOST important clarifying questions — the ones \
whose answers would materially change the skill. Focus on:
- Scope & triggers: exactly when should the skill run (and not run)?
- Inputs: what does it need, and where does that data come from?
- Output: the exact format / destination of the result.
- Tools & integrations: which systems must it touch?
- Constraints: tone, length, approvals, edge cases that matter.

Ask only what is genuinely ambiguous — never pad to three. Each question must be \
specific and answerable in a sentence.

Output ONLY the questions, one per line, with no numbering, bullets, or \
preamble. If the request is already detailed enough to build a strong skill, \
output exactly: READY"""


async def runtime_invoke_skill(
    db: AsyncSession,
    skill_id: str,
    entity_id: str,
    input_text: str,
    **kwargs: Any,
) -> dict:
    """Invoke a skill through the Runtime skill boundary."""

    from packages.core.services.skill_service import invoke_skill

    return await invoke_skill(
        db,
        skill_id,
        entity_id,
        input_text,
        **kwargs,
    )


async def runtime_list_skills(
    db: AsyncSession,
    entity_id: str,
    *,
    category: str | None = None,
) -> list[Any]:
    """List skills through the Runtime skill lifecycle boundary."""

    from packages.core.services.skill_service import list_skills

    return await list_skills(db, entity_id, category=category or None)


async def runtime_get_skill(
    db: AsyncSession,
    skill_id: str,
    *,
    entity_id: str | None = None,
) -> Any | None:
    """Load a skill through the Runtime skill lifecycle boundary."""

    from packages.core.services.skill_service import get_skill

    skill = await get_skill(db, skill_id)
    if skill is None or getattr(skill, "status", "active") != "active":
        return None
    if entity_id is not None and getattr(skill, "entity_id", None) not in {
        None,
        entity_id,
    }:
        return None
    return skill


async def runtime_generate_skill(
    db: AsyncSession,
    *,
    entity_id: str,
    prompt: str,
    category: str | None = None,
    tags: Any = None,
) -> Any:
    """Generate and persist a skill through the Runtime lifecycle boundary."""

    from packages.core.services.skill_generator import generate_skill

    return await generate_skill(
        prompt=prompt,
        entity_id=entity_id,
        db=db,
        category=category,
        tags=tags if tags is not None else [],
    )


async def runtime_update_skill(
    db: AsyncSession,
    *,
    entity_id: str,
    skill_id: str,
    change_description: str,
) -> Any:
    """Patch a skill through the Runtime lifecycle boundary."""

    from packages.core.services.skill_generator import update_skill

    return await update_skill(skill_id, change_description, entity_id, db)


async def runtime_delete_skill(
    db: AsyncSession,
    *,
    entity_id: str,
    skill_id: str,
) -> bool:
    """Delete a skill through the Runtime lifecycle boundary."""

    from packages.core.services.skill_service import delete_skill

    return await delete_skill(db, skill_id, entity_id)


async def runtime_invoke_skill_action(
    *,
    entity_id: str,
    skill_id: str | None = None,
    skill: str | None = None,
    input_text: str = "",
    skill_params: Any | None = None,
    runtime_context: Any | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Invoke a skill and format the tool result through Runtime."""

    # The public invoke_skill handler historically used ``skill=`` while the
    # runtime helper was renamed to ``skill_id``. Accept both at this boundary
    # so registered-tool dispatch remains backwards compatible.
    skill_id = str(skill_id or skill or "").strip()

    manually_selected_ids = {
        str(value).strip().lower()
        for value in (getattr(runtime_context, "manual_skill_ids", None) or ())
        if str(value or "").strip()
    }
    manually_selected_slugs = {
        str(value).strip().lower()
        for value in (getattr(runtime_context, "manual_skill_slugs", None) or ())
        if str(value or "").strip()
    }
    manually_selected_refs = manually_selected_ids | manually_selected_slugs
    if (
        bool(getattr(runtime_context, "manual_skill_selected", False))
        and manually_selected_refs
        and str(skill_id or "").strip().lower() not in manually_selected_refs
    ):
        return f"Error invoking skill '{skill_id}': skill was not selected for this turn"

    active_user_message = str(
        getattr(runtime_context, "active_user_message", "") or ""
    ).strip()
    effective_input_text = str(input_text or "").strip()
    if not effective_input_text:
        effective_input_text = active_user_message

    # Reject an accidental writing-skill route before touching the database.
    # Besides avoiding unnecessary work, this keeps the safety handoff
    # deterministic when the workspace database is unavailable: external
    # publish/send requests must yield to the integration tools regardless of
    # whether the named skill can be resolved.
    # Opaque UUID/ULID references need to be resolved first: the row may be a
    # legitimate platform route even though its identifier is not recognizable
    # by the routing classifier. Human-readable slugs can be rejected eagerly.
    opaque_skill_reference = False
    try:
        UUID(skill_id)
        opaque_skill_reference = True
    except (ValueError, AttributeError, TypeError):
        try:
            ULID.from_str(skill_id)
            opaque_skill_reference = True
        except (ValueError, AttributeError, TypeError):
            pass
    if not opaque_skill_reference:
        early_skip_result = runtime_external_action_skill_skip_result(
            active_user_message=active_user_message,
            skill=skill_id,
            manual_skill_selected=bool(
                getattr(runtime_context, "manual_skill_selected", False)
            ),
        )
        if early_skip_result is not None:
            return early_skip_result

    from packages.core.database import async_session
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_tool_context_args
    from packages.core.services.skill_service import get_skill, get_skill_by_slug

    runtime_tool_context = runtime_workflow_tool_context_args({
        "workflow_run_id": getattr(runtime_context, "workflow_run_id", None),
        "workflow_lineage_root_run_id": getattr(
            runtime_context, "workflow_lineage_root_run_id", None
        ),
        "workflow_project_id": getattr(runtime_context, "workflow_project_id", None),
        "workflow_action_grant_id": getattr(runtime_context, "workflow_action_grant_id", None),
        "workflow_step_id": getattr(runtime_context, "workflow_step_id", None),
        "workflow_scene_id": getattr(runtime_context, "workflow_scene_id", None),
        "workflow_batch_capture": getattr(runtime_context, "workflow_batch_capture", None),
        "approved_plan_version": getattr(runtime_context, "approved_plan_version", None),
    })
    if getattr(runtime_context, "runtime_run_id", None):
        runtime_tool_context.update(
            {
                "_runtime_run_id_from_context": runtime_context.runtime_run_id,
                "_runtime_tool_call_id_from_context": runtime_context.runtime_tool_call_id,
                "_runtime_tool_attempt_from_context": runtime_context.runtime_tool_attempt,
            }
        )

    async with async_session() as db:
        skill_row = await get_skill(db, skill_id)
        if skill_row is None:
            skill_row = await get_skill_by_slug(db, skill_id, entity_id)
        if (
            skill_row is None
            or getattr(skill_row, "status", None) != "active"
            or getattr(skill_row, "entity_id", None) not in {None, entity_id}
        ):
            return f"Error invoking skill '{skill_id}': Skill not found"
        skill_key = str(skill_row.slug or skill_row.name or skill_row.id)
        skill_params = runtime_skill_params_for_active_turn(
            skill_key,
            skill_params,
            active_user_message=active_user_message,
        )
        effective_input_text = runtime_skill_input_with_params(effective_input_text, skill_params)
        effective_input_text = runtime_input_with_artifact_context(
            effective_input_text,
            runtime_artifact_urls=getattr(runtime_context, "runtime_artifact_urls", None),
            dependency_artifact_urls=getattr(runtime_context, "dependency_artifact_urls", None),
        )
        skip_result = runtime_external_action_skill_skip_result(
            active_user_message=getattr(runtime_context, "active_user_message", None),
            skill=skill_key,
            manual_skill_selected=bool(
                getattr(runtime_context, "manual_skill_selected", False)
            ),
        )
        if skip_result is not None:
            return skip_result
        skill_model = await runtime_manual_skill_execution_model(
            db,
            runtime_context=runtime_context,
        )
        result = await runtime_invoke_skill(
            db,
            skill_id,
            entity_id,
            effective_input_text,
            agent_id=getattr(runtime_context, "agent_id", None),
            enforce_agent_access=not bool(getattr(runtime_context, "manual_skill_selected", False)),
            user_id=user_id or None,
            workspace_id=getattr(runtime_context, "workspace_id", None),
            conversation_id=conversation_id or getattr(runtime_context, "conversation_id", None),
            task_id=getattr(runtime_context, "task_id", None),
            manual_skill_selected=bool(getattr(runtime_context, "manual_skill_selected", False)),
            tool_profile=getattr(runtime_context, "tool_profile", None),
            allowed_tool_names=getattr(runtime_context, "allowed_tool_names", None),
            runtime_envelope=getattr(runtime_context, "runtime_envelope", None),
            metadata=runtime_skill_invocation_metadata(runtime_context),
            model=skill_model,
            active_user_message=getattr(runtime_context, "active_user_message", None),
            runtime_tool_context=runtime_tool_context,
        )
    from packages.core.ai.runtime.nested_usage import runtime_record_nested_usage
    from packages.core.ai.runtime.control import is_runtime_tool_suspension

    if is_runtime_tool_suspension(result):
        return result

    runtime_record_nested_usage(result.get("usage") if isinstance(result, dict) else None)
    if runtime_manual_skill_result_stops_parent(runtime_context, result):
        result = {
            **result,
            "stop_parent": True,
            "stop_reason": result.get("stop_reason") or "manual_skill_completed",
            "replace_visible_text": True,
        }
    return runtime_format_invoke_skill_result(skill_key, result)


async def runtime_manual_skill_execution_model(
    db: Any,
    *,
    runtime_context: Any | None,
) -> str | None:
    """Use dedicated high-capability models for bounded Research/Slides Skills.

    The model and native BYOK credential are resolved as one route. This keeps
    a Slides turn from inheriting a cheap Primary model from another provider,
    which otherwise fails before PowerPoint authoring even starts.
    """

    current_model = str(getattr(runtime_context, "llm_model", "") or "").strip()
    envelope = getattr(runtime_context, "runtime_envelope", None)
    metadata = getattr(envelope, "metadata", None)
    plan = metadata.get("turn_execution_plan") if isinstance(metadata, dict) else None
    dedicated_research = (
        str((metadata or {}).get("chat_mode") or "").strip().lower()
        == "research"
    )
    dedicated_slides = (
        str((metadata or {}).get("chat_mode") or "").strip().lower()
        == "slides"
    )
    bounded_manual_research = bool(
        bool(getattr(runtime_context, "manual_skill_selected", False))
        and isinstance(plan, dict)
        and plan.get("tool_catalog_mode") == "web_research"
    )
    if not (dedicated_research or bounded_manual_research or dedicated_slides):
        return current_model or None

    try:
        from packages.core.services.model_gateway import (
            detect_provider_from_key,
            provider_for_model,
        )
        from packages.core.services.model_settings import (
            get_model_settings_cached,
            is_model_disabled,
            presentation_model,
            research_model,
        )

        settings = await get_model_settings_cached(db)
        candidate = (
            presentation_model(settings)
            if dedicated_slides
            else research_model(settings)
        )
        if is_model_disabled(settings, "primary", candidate):
            return current_model or None

        raw_llm_metadata = getattr(runtime_context, "llm_metadata", None)
        byok_key = (
            raw_llm_metadata.get("llm_api_key")
            or raw_llm_metadata.get("api_key")
            or raw_llm_metadata.get("_resolved_api_key")
            if isinstance(raw_llm_metadata, dict)
            else None
        )
        if byok_key:
            key_provider = detect_provider_from_key(str(byok_key))
            candidate_provider = provider_for_model(candidate)
            current_provider = provider_for_model(current_model)
            if key_provider and key_provider != "openrouter":
                if candidate_provider == key_provider:
                    return candidate
                if current_model and current_provider == key_provider:
                    return current_model
                # No compatible route is available. Keep the current model so
                # the lower routing layer emits its explicit provider mismatch
                # instead of silently sending a native key to the wrong model.
                return current_model or None
            if current_model and candidate_provider != current_provider:
                return current_model
        return candidate
    except Exception:
        return current_model or None


def runtime_manual_skill_result_stops_parent(
    runtime_context: Any | None,
    result: Any,
) -> bool:
    """Stop duplicate parent research after a selected web Skill succeeds."""

    if not bool(getattr(runtime_context, "manual_skill_selected", False)):
        return False
    if not isinstance(result, dict) or result.get("error") or result.get("sandbox_id"):
        return False
    if not str(result.get("content") or "").strip():
        return False
    envelope = getattr(runtime_context, "runtime_envelope", None)
    metadata = getattr(envelope, "metadata", None)
    plan = metadata.get("turn_execution_plan") if isinstance(metadata, dict) else None
    return bool(
        isinstance(plan, dict)
        and plan.get("tool_catalog_mode") == "web_research"
    )


def runtime_skill_input_with_params(input_text: str, skill_params: Any | None) -> str:
    """Attach structured skill params without asking models to hand-roll JSON."""

    if not isinstance(skill_params, dict) or not skill_params:
        return input_text
    text = str(input_text or "").strip()
    payload: dict[str, Any]
    if text:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            payload = dict(parsed)
        else:
            payload = {"prompt": text}
    else:
        payload = {}
    existing_params = payload.get("params")
    merged_params = dict(existing_params) if isinstance(existing_params, dict) else {}
    merged_params.update(skill_params)
    payload["params"] = merged_params
    return json.dumps(payload, ensure_ascii=False, indent=2)


def runtime_skill_params_for_active_turn(
    skill: str,
    skill_params: Any | None,
    *,
    active_user_message: str | None,
) -> Any | None:
    """Drop stale PPTX resume identity from an explicit fresh-deck request."""

    if not isinstance(skill_params, dict):
        return skill_params
    if not (
        is_presentation_skill(str(skill or ""), str(skill or ""))
        and presentation_fresh_creation_intent(active_user_message)
    ):
        return skill_params
    return {
        key: value
        for key, value in skill_params.items()
        if key not in {"sandbox_id", "project", "project_path"}
    }


async def runtime_create_skill_action(
    *,
    entity_id: str,
    name: str,
    description: str = "",
    category: str | None = None,
    tags: Any = None,
) -> str:
    """Create a skill from tool input through the Runtime lifecycle boundary."""

    from packages.core.database import async_session

    prompt = f"{name}: {description}" if description else name
    async with async_session() as db:
        skill = await runtime_generate_skill(
            db,
            prompt=prompt,
            entity_id=entity_id,
            category=category,
            tags=tags if tags is not None else [],
        )
        await db.commit()
    return (
        f"Created skill '{skill.name}' (id={skill.id})\n"
        f"Description: {skill.description or 'N/A'}\n"
        f"Tools: {', '.join(skill.tools) if skill.tools else 'none'}\n"
        f"Category: {skill.category or 'N/A'}"
    )


async def runtime_list_skills_action(
    *,
    entity_id: str,
    category: str | None = None,
    tool_kwargs: dict[str, Any] | None = None,
) -> str:
    """List runtime-visible or entity skills through the Runtime boundary."""

    from packages.core.database import async_session

    async with async_session() as db:
        runtime_descriptors = await runtime_skill_descriptors_from_tool_kwargs(db, tool_kwargs or {})
        if runtime_descriptors is not None:
            return runtime_format_skill_descriptor_list(
                runtime_descriptors,
                category=category or None,
            )
        skills = await runtime_list_skills(db, entity_id, category=category or None)

    if not skills:
        return "No skills found."

    lines = [f"Found {len(skills)} skill(s):\n"]
    for skill in skills:
        source = "platform" if not skill.entity_id else "custom"
        lines.append(
            f"- [{skill.slug or skill.name}] {skill.display_name or skill.name} — "
            f"{skill.description or 'No description'} ({source})"
        )
    return "\n".join(lines)


async def runtime_update_skill_action(
    *,
    entity_id: str,
    skill_id: str,
    change_description: str,
) -> str:
    """Patch a skill from tool input through Runtime."""

    if not change_description:
        return "Error: change_description is required."

    from packages.core.database import async_session

    async with async_session() as db:
        skill = await runtime_update_skill(
            db,
            skill_id=skill_id,
            change_description=change_description,
            entity_id=entity_id,
        )
        await db.commit()
    return f"Updated skill '{skill.name}' (v{skill.version})"


async def runtime_delete_skill_action(
    *,
    entity_id: str,
    skill_id: str,
) -> str:
    """Delete a custom skill from tool input through Runtime."""

    from packages.core.database import async_session

    async with async_session() as db:
        deleted = await runtime_delete_skill(db, skill_id=skill_id, entity_id=entity_id)

    if deleted:
        return f"Deleted skill '{skill_id}'."
    return f"Skill '{skill_id}' not found or cannot be deleted (platform skill)."


async def runtime_get_skill_details_action(
    *,
    entity_id: str,
    skill_id: str,
    tool_kwargs: dict[str, Any] | None = None,
) -> str:
    """Return skill discovery details through Runtime without leaking hidden prompts."""

    from packages.core.database import async_session

    async with async_session() as db:
        runtime_descriptors = await runtime_skill_descriptors_from_tool_kwargs(db, tool_kwargs or {})
        if runtime_descriptors is not None:
            resolved_id = str(skill_id or "").strip()
            for descriptor in runtime_descriptors:
                if runtime_skill_descriptor_id_matches(descriptor, resolved_id):
                    return runtime_format_skill_descriptor_detail(descriptor)
            return f"Skill '{skill_id}' is not visible in this runtime."
        skill = await runtime_get_skill(db, skill_id, entity_id=entity_id)

    if not skill:
        return f"Skill '{skill_id}' not found."

    return (
        f"Skill: {skill.display_name or skill.name}\n"
        f"ID: {skill.id}\n"
        f"Slug: {skill.slug}\n"
        f"Description: {skill.description or 'N/A'}\n"
        f"Category: {skill.category or 'N/A'}\n"
        f"Tools: {', '.join(skill.tools) if skill.tools else 'none'}\n"
        f"Version: {skill.version}\n"
        f"Output: {skill.output_format}\n\n"
        f"--- System Prompt ---\n{skill.system_prompt}"
    )


def runtime_skill_generation_user_message(
    user_prompt: str,
    *,
    category: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Build the Runtime-owned user message for skill generation."""

    parts = [f"Create a skill for the following task:\n\n{user_prompt}"]
    if category:
        parts.append(f"\nPreferred category: {category}")
    if tags:
        parts.append(f"\nSuggested tags: {', '.join(tags)}")
    return "\n".join(parts)


def runtime_skill_generation_messages(
    user_prompt: str,
    *,
    category: str | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for initial skill generation."""

    return [
        {
            "role": "system",
            "content": _system_prompt_with_skill_creation_contract(
                RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT
            ),
        },
        {
            "role": "user",
            "content": runtime_skill_generation_user_message(
                user_prompt,
                category=category,
                tags=tags,
            ),
        },
    ]


async def runtime_execute_skill_generation_completion(
    user_prompt: str,
    *,
    entity_id: str,
    category: str | None = None,
    tags: list[str] | None = None,
) -> RuntimeTextCompletionResult:
    """Execute initial skill generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_skill_generation_messages(
            user_prompt,
            category=category,
            tags=tags,
        ),
        entity_id=entity_id,
        source=RUNTIME_SKILL_GENERATOR_SOURCE,
        temperature=0.4,
        # A bundle spec carries the main instructions plus complete scripts
        # and references file contents; a low cap silently truncates the
        # JSON mid-spec.
        max_tokens=16000,
    )


def runtime_skill_clarify_messages(prompt: str) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for skill clarifying questions."""

    return [
        {"role": "system", "content": RUNTIME_SKILL_CLARIFY_SYSTEM_PROMPT},
        {"role": "user", "content": f"Skill request:\n\n{prompt}"},
    ]


async def runtime_execute_skill_clarify_completion(
    prompt: str,
    *,
    entity_id: str,
) -> RuntimeTextCompletionResult:
    """Execute the clarifying-questions step with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_skill_clarify_messages(prompt),
        entity_id=entity_id,
        source=RUNTIME_SKILL_GENERATOR_SOURCE,
        temperature=0.3,
        max_tokens=500,
    )


async def runtime_skill_clarifying_questions(
    prompt: str,
    *,
    entity_id: str,
) -> list[str]:
    """Return up to 3 clarifying questions for a skill request.

    Empty list means the request is already specific enough to build. This is
    the structured entry point used by the REST/UI conversational create flow;
    ``runtime_draft_skill_action`` formats it for the agent tool surface.
    """
    text = (prompt or "").strip()
    if not text:
        return []
    completion = await runtime_execute_skill_clarify_completion(text, entity_id=entity_id)
    return parse_clarifying_questions(completion.content or "")


async def runtime_draft_skill_action(
    *,
    entity_id: str,
    name: str = "",
    description: str = "",
) -> str:
    """Return clarifying questions for a skill request (does NOT create it).

    The agent should call this before ``create_skill`` when the request is
    vague, relay the questions to the user, then call ``create_skill`` with the
    answers folded into the description.
    """
    prompt = f"{name}: {description}" if description else (name or description)
    prompt = prompt.strip()
    if not prompt:
        return "Provide a name and/or description for the skill you want to create."

    questions = await runtime_skill_clarifying_questions(prompt, entity_id=entity_id)
    if not questions:
        return "The request is specific enough to build. Call create_skill now with this name and description."
    lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return (
        "Ask the user these clarifying questions before creating the skill, then "
        "call create_skill with their answers folded into the description:\n\n"
        f"{lines}"
    )


def runtime_skill_review_prompt(spec: dict[str, Any]) -> str:
    """Build the Runtime-owned skill review simulation prompt."""

    scripts = spec.get("scripts") or {}
    references = spec.get("references") or {}
    bundled = sorted(
        [f"scripts/{name}" for name in scripts] + [f"references/{name}" for name in references]
    )
    return (
        f"You are testing a skill before it goes live.\n\n"
        f"Skill name: {spec.get('name', 'unknown')}\n"
        f"System prompt (the SKILL.md body):\n{spec.get('system_prompt', '')}\n\n"
        f"Bundled files: {bundled or 'none'}\n"
        f"Tools available: {spec.get('tools', [])}\n"
        f"Input schema: {json.dumps(spec.get('input_schema', {}))}\n\n"
        f"Simulate running this skill with a realistic sample input. Then evaluate:\n"
        f"1. Are the steps clear and actionable? Would an agent know exactly what to do?\n"
        f"2. Are the right tools listed? Any missing or unnecessary?\n"
        f"3. Is the output format well-defined (with a concrete template)?\n"
        f"4. Are there edge cases that would break the skill? Are they handled?\n"
        f"5. Is the system_prompt specific enough (not vague/generic)?\n"
        f"6. Is the main prompt concise while still complete, with detailed "
        f"static knowledge moved into directly linked references?\n"
        f"7. Is every bundled file explicitly pointed to from the system_prompt "
        f"(e.g. 'read `references/style-guide.md`', 'run "
        f"`scripts/build_report.py`')? An unmentioned file is dead weight.\n"
        f"8. Does the description say both what the skill does and the semantic "
        f"situations in which it should be used, including important near-misses?\n"
        f"9. Are side effects, approvals, authorization, and failure states handled "
        f"without surprising the user?\n\n"
        f"If the skill passes all checks, respond with exactly: PASS\n"
        f"If it needs changes, respond with: FAIL followed by a JSON object "
        f"with the complete corrected skill spec using the same keys — "
        f"including the unchanged scripts and references objects. Return "
        f"the correction itself rather than commentary about what to change."
    )


def runtime_skill_review_messages(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for skill quality review."""

    return [
        {
            "role": "system",
            "content": _system_prompt_with_skill_creation_contract(
                RUNTIME_SKILL_REVIEW_SYSTEM_PROMPT
            ),
        },
        {"role": "user", "content": runtime_skill_review_prompt(spec)},
    ]


async def runtime_execute_skill_review_completion(
    spec: dict[str, Any],
    *,
    entity_id: str,
) -> RuntimeTextCompletionResult:
    """Execute skill quality review with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_skill_review_messages(spec),
        entity_id=entity_id,
        source=RUNTIME_SKILL_GENERATOR_SOURCE,
        temperature=0.3,
        # On FAIL the reviewer returns the COMPLETE corrected spec, including
        # the scripts and references objects, so it needs the same headroom as
        # generation.
        max_tokens=16000,
    )


def runtime_skill_patch_current_definition(skill: Any) -> str:
    """Serialize the current skill definition for Runtime-owned patch prompts."""

    return json.dumps(
        {
            "name": skill.name,
            "slug": skill.slug,
            "display_name": skill.display_name,
            "description": skill.description,
            "system_prompt": skill.system_prompt,
            "tools": skill.tools or [],
            "input_schema": skill.input_schema or {},
            "output_format": skill.output_format,
            "category": skill.category,
            "tags": skill.tags or [],
        },
        indent=2,
    )


def runtime_skill_patch_user_message(skill: Any, change_description: str) -> str:
    """Build the Runtime-owned user message for patching an existing skill."""

    current_definition = runtime_skill_patch_current_definition(skill)
    return f"## Current skill definition\n\n{current_definition}\n\n## Requested change\n\n{change_description}"


def runtime_skill_patch_messages(
    skill: Any,
    change_description: str,
) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for skill patch generation."""

    return [
        {
            "role": "system",
            "content": _system_prompt_with_skill_creation_contract(
                RUNTIME_SKILL_PATCH_SYSTEM_PROMPT
            ),
        },
        {"role": "user", "content": runtime_skill_patch_user_message(skill, change_description)},
    ]


async def runtime_execute_skill_patch_completion(
    skill: Any,
    change_description: str,
    *,
    entity_id: str,
) -> RuntimeTextCompletionResult:
    """Execute skill patch generation with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_skill_patch_messages(skill, change_description),
        entity_id=entity_id,
        source=RUNTIME_SKILL_GENERATOR_SOURCE,
        temperature=0.3,
        # A patch may rewrite the full detailed system_prompt, not just a field.
        max_tokens=6000,
    )


_LOCAL_CODING_TERMINAL_TOOL_RESULT_POLICY: dict[str, Any] = {
    "terminal_tool_results": [
        {
            "tool_names": [
                "mcp__codex_cli__run",
                "mcp__codex_cli__review",
                "mcp__claude_code__run",
                "mcp__claude_code__review",
                "mcp__gemini_cli__run",
                "mcp__cursor_cli__run",
                "mcp__aider__run",
                "mcp__continue_cli__run",
            ],
            "statuses": ["running", "queued", "pending"],
            "json_equals": {
                "tool": [
                    "codex_cli",
                    "claude_code",
                    "gemini_cli",
                    "cursor",
                    "cursor_cli",
                    "aider",
                    "continue",
                    "continue_cli",
                ]
            },
            "stop_reason": LOCAL_CODING_DISPATCHED_STOP_REASON,
            "stop_parent": True,
            "replace_visible_text": True,
            "notice": {
                "en": (
                    "The remote coding task has been dispatched. Please wait; "
                    "status, errors, and resulting changes will update in the run card above."
                ),
                "zh": "已派发远程 coding 任务，请稍后。运行状态、错误信息和完成后的改动会在上方卡片中更新。",
            },
        },
    ],
}


@dataclass(frozen=True)
class SkillDescriptor:
    id: str
    slug: str
    name: str
    description: str = ""
    source: SkillSource = "entity"
    allowed_surfaces: tuple[ChatSurface, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    declared_tools: tuple[str, ...] = ()
    visibility_reason: str | None = None
    metadata: dict = field(default_factory=dict)


def skill_descriptor_to_trace_dict(descriptor: SkillDescriptor) -> dict:
    """Serialize the prompt-visible skill descriptor without full instructions."""
    return {
        "id": descriptor.id,
        "slug": descriptor.slug,
        "name": descriptor.name,
        "description": descriptor.description,
        "source": descriptor.source,
        "allowed_surfaces": tuple(surface.value for surface in descriptor.allowed_surfaces),
        "required_capabilities": tuple(descriptor.required_capabilities),
        "declared_tools": tuple(descriptor.declared_tools),
        "visibility_reason": descriptor.visibility_reason,
        "metadata": dict(descriptor.metadata or {}),
    }


def skill_descriptors_to_trace_dict(
    descriptors: Iterable[SkillDescriptor],
) -> tuple[dict, ...]:
    return tuple(skill_descriptor_to_trace_dict(descriptor) for descriptor in descriptors)


def _runtime_available_skills_section(message: str) -> str:
    return f"## Available Skills\n{message}"


def runtime_available_skills_omission_section(
    *,
    active_user_message: str | None,
    manual_skill_selected: bool = False,
) -> str | None:
    """Return the runtime-owned skill omission message for this turn."""
    if manual_skill_selected:
        return None
    chrome_local_route = (
        None
        if runtime_approval_resume_intent(active_user_message)
        else detect_chrome_local_browser_route(active_user_message)
    )
    if (
        external_platform_action_intent(active_user_message)
        and not youtube_platform_action_intent(active_user_message)
        and not linkedin_platform_operation_intent(active_user_message)
        and not named_integration_operation_intent(active_user_message)
        and not social_platform_action_intent(active_user_message)
        and not chrome_local_route
    ):
        return _runtime_available_skills_section(
            "Optional Skills are not offered for this turn because the latest "
            "request targets an external platform action. Use `search_tools` "
            "for the relevant Integration/MCP tool."
        )
    return None


def _filter_skills_for_prompt(
    skills: Iterable,
    *,
    active_user_message: str | None,
    manual_skill_selected: bool = False,
) -> tuple[list, str | None]:
    items = list(skills or [])
    if manual_skill_selected:
        return items, None

    # Intent-scoped narrowing. When a turn's intent lands squarely in an
    # umbrella capability domain (local Chrome / local coding), we focus the
    # catalog on that umbrella skill. Per-MCP guidance packs (``mcp_*``) are
    # NOT mutually exclusive with the umbrella, though — they combine: the
    # umbrella skill drives the task, and if it reaches for a connected MCP
    # the model can still consult that MCP's pack to learn how to use it. So
    # each branch keeps the umbrella skill *and* any ``mcp_*`` packs (the
    # latter stay availability-gated downstream, so only connected MCPs
    # actually surface).
    chrome_local_route = (
        None
        if runtime_approval_resume_intent(active_user_message)
        else detect_chrome_local_browser_route(active_user_message)
    )
    if chrome_local_route:
        selected = [
            skill
            for skill in items
            if is_chrome_skill(*skill_slug_and_name(skill))
            or _is_mcp_guidance_pack(skill)
            or (
                youtube_platform_action_intent(active_user_message)
                and is_youtube_platform_skill(*skill_slug_and_name(skill))
            )
            or (
                linkedin_platform_operation_intent(active_user_message)
                and is_linkedin_platform_skill(*skill_slug_and_name(skill))
            )
            or (
                social_platform_action_intent(active_user_message)
                and is_social_platform_skill(*skill_slug_and_name(skill))
            )
            or (
                named_integration_operation_intent(active_user_message)
                and is_integration_parent_skill(
                    integration_skill_route_for_message(active_user_message),
                    *skill_slug_and_name(skill),
                )
            )
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "No Chrome runtime skill is available. Stop and report that the "
                "local Chrome runtime skill or setup is unavailable; do not use "
                "generic web tools."
            )
        return filtered, None

    if youtube_platform_action_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_youtube_route_skill(*skill_slug_and_name(skill))
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "No internal YouTube platform route is available. Report that "
                "platform-youtube and its verified mcp_youtube or Chrome child "
                "routes are unavailable; do not recommend a Marketplace install."
            )
        return filtered, None

    if linkedin_platform_operation_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_linkedin_route_skill(*skill_slug_and_name(skill))
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "No internal LinkedIn platform route is available. Report that "
                "platform-linkedin and its verified mcp_linkedin or Chrome child "
                "routes are unavailable; do not recommend a Marketplace install."
            )
        return filtered, None

    if social_platform_action_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_social_platform_route_skill(
                active_user_message, *skill_slug_and_name(skill)
            )
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "Optional Skills are not offered for this external platform action. No internal "
                "social-platform route is available. Report that "
                "platform-social and its verified platform MCP or Chrome child "
                "routes are unavailable; use `search_tools` to discover a verified "
                "Integration/MCP route when one is connected; do not recommend a Marketplace install."
            )
        return filtered, None

    if presentation_artifact_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_presentation_skill(*skill_slug_and_name(skill))
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "No built-in PPTX workflow is available. Report that the PPTX "
                "skill is unavailable; do not route the presentation request "
                "through platform-development or a generic coding provider."
            )
        return filtered, None

    if local_coding_cli_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_local_coding_skill(*skill_slug_and_name(skill))
            or str(skill_slug_and_name(skill)[0] or "").startswith(("mcp_", "mcp-"))
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            return filtered, _runtime_available_skills_section(
                "No internal development route is available. Report that "
                "platform-development and its paired local coding children are "
                "unavailable; do not recommend a Marketplace install."
            )
        return filtered, None

    if named_integration_operation_intent(active_user_message):
        route = integration_skill_route_for_message(active_user_message)
        selected = [
            skill
            for skill in items
            if is_integration_parent_skill(route, *skill_slug_and_name(skill))
            or is_integration_child_skill(route, *skill_slug_and_name(skill))
            or (
                route is not None
                and route.chrome_fallback
                and is_chrome_skill(*skill_slug_and_name(skill))
            )
        ]
        filtered = retain_required_skill_invocation_policies(items, selected)
        if not selected:
            provider = route.provider_key if route else "requested"
            return filtered, _runtime_available_skills_section(
                f"No internal executable Skill route is available for {provider}. "
                "Report the missing Integration capability or connection; do not "
                "recommend a Marketplace install or invent MCP tools."
            )
        return filtered, None

    return items, None


def runtime_skill_descriptor_key_values(descriptor: Any) -> set[str]:
    return {
        str(value).strip().lower()
        for value in (
            getattr(descriptor, "id", ""),
            getattr(descriptor, "slug", ""),
            getattr(descriptor, "name", ""),
        )
        if str(value or "").strip()
    }


def runtime_skill_descriptor_matches(descriptor: Any, key: str) -> bool:
    return str(key or "").strip().lower() in runtime_skill_descriptor_key_values(descriptor)


def runtime_skill_descriptor_id_matches(descriptor: Any, skill_id: str) -> bool:
    """Match a runtime Skill descriptor strictly by its stable database ID."""

    return str(getattr(descriptor, "id", "") or "").strip() == str(skill_id or "").strip()


def runtime_skill_source_for_skill(
    skill: Any,
    *,
    agent_id: str | None = None,
    agent_bound_skill_ids: Iterable[str] | None = None,
    workspace_operation_skill_ids: Iterable[str] | None = None,
) -> SkillSource:
    """Classify why a skill is visible in the current runtime catalog."""

    if getattr(skill, "entity_id", None) is None:
        return "builtin"
    skill_id = str(getattr(skill, "id", "") or "")
    workspace_bound = {str(value) for value in (workspace_operation_skill_ids or ()) if str(value or "").strip()}
    if skill_id and skill_id in workspace_bound:
        return "workspace_operation"
    agent_bound = {str(value) for value in (agent_bound_skill_ids or ()) if str(value or "").strip()}
    if agent_id and skill_id and skill_id in agent_bound:
        return "agent_binding"
    return "entity"


def runtime_skill_descriptor_dict(descriptor: Any) -> dict[str, Any]:
    meta = dict(getattr(descriptor, "metadata", {}) or {})
    return {
        "id": getattr(descriptor, "id", ""),
        "name": getattr(descriptor, "name", ""),
        "slug": getattr(descriptor, "slug", ""),
        "description": getattr(descriptor, "description", ""),
        "category": meta.get("category"),
        "output_format": meta.get("output_format"),
        "source": getattr(descriptor, "source", None),
        "declared_tools": list(getattr(descriptor, "declared_tools", ()) or ()),
        "required_capabilities": list(getattr(descriptor, "required_capabilities", ()) or ()),
        "visibility_reason": getattr(descriptor, "visibility_reason", None),
    }


def _runtime_skill_source_label(descriptor: Any) -> str:
    source = str(getattr(descriptor, "source", "") or "").strip()
    if not source and hasattr(descriptor, "entity_id"):
        source = "builtin" if getattr(descriptor, "entity_id", None) is None else "entity"
    labels = {
        "builtin": "built-in",
        "entity": "entity",
        "agent_binding": "agent",
        "workspace_operation": "workspace",
        "manual": "manual",
    }
    return labels.get(source, source or "skill")


def runtime_format_skill_descriptor_list(
    descriptors: Iterable[Any],
    *,
    category: str | None = None,
) -> str:
    items = list(descriptors or [])
    category_key = str(category or "").strip().lower()
    if category_key:
        items = [
            descriptor
            for descriptor in items
            if str((getattr(descriptor, "metadata", {}) or {}).get("category") or "").strip().lower() == category_key
        ]
    if not items:
        return "No skills found."
    lines = [f"Found {len(items)} runtime-visible skill(s):\n"]
    for descriptor in items:
        meta = getattr(descriptor, "metadata", {}) or {}
        category_label = str(meta.get("category") or "").strip()
        suffix = f" ({category_label})" if category_label else ""
        source_label = _runtime_skill_source_label(descriptor)
        lines.append(
            f"- [{descriptor.id}] [{source_label}] "
            f"{descriptor.name or descriptor.slug} (`{descriptor.slug}`) - "
            f"{descriptor.description or 'No description'}{suffix}"
        )
    lines.append("\nFull skill instructions are loaded only by `invoke_skill`.")
    return "\n".join(lines)


def runtime_format_skill_descriptor_detail(descriptor: Any) -> str:
    meta = getattr(descriptor, "metadata", {}) or {}
    category = str(meta.get("category") or "N/A")
    output_format = str(meta.get("output_format") or "N/A")
    tools = ", ".join(getattr(descriptor, "declared_tools", ()) or ()) or "none"
    capabilities = ", ".join(getattr(descriptor, "required_capabilities", ()) or ()) or "none"
    return (
        f"Skill: {descriptor.name or descriptor.slug}\n"
        f"ID: {descriptor.id}\n"
        f"Slug: {descriptor.slug}\n"
        f"Description: {descriptor.description or 'N/A'}\n"
        f"Category: {category}\n"
        f"Tools visible in this runtime: {tools}\n"
        f"Required capabilities: {capabilities}\n"
        f"Output: {output_format}\n\n"
        "Full skill instructions are not exposed by discovery; call "
        "`invoke_skill` to run the skill when it is appropriate for this runtime."
    )


def runtime_skill_descriptor_list_payload(
    descriptors: Iterable[Any],
    *,
    category: str | None = None,
) -> dict[str, Any]:
    items = list(descriptors or [])
    category_key = str(category or "").strip().lower()
    if category_key:
        items = [
            descriptor
            for descriptor in items
            if str((getattr(descriptor, "metadata", {}) or {}).get("category") or "").strip().lower() == category_key
        ]
    return {
        "skills": [runtime_skill_descriptor_dict(descriptor) for descriptor in items],
        "count": len(items),
        "runtime_scoped": True,
        "instructions": "Full skill instructions are loaded only by invoke_skill.",
    }


def runtime_skill_descriptor_detail_payload(
    descriptors: Iterable[Any],
    *,
    skill_id: str,
) -> dict[str, Any]:
    resolved_id = str(skill_id or "").strip()
    for descriptor in descriptors or ():
        if runtime_skill_descriptor_id_matches(descriptor, resolved_id):
            payload = runtime_skill_descriptor_dict(descriptor)
            payload["runtime_scoped"] = True
            payload["instructions"] = (
                "Full skill instructions are not exposed by discovery; call invoke_skill to run this skill."
            )
            return payload
    return {
        "error": "Skill not visible in this runtime",
        "skill_id": resolved_id,
        "runtime_scoped": True,
    }


def runtime_external_action_skill_skip_result(
    *,
    active_user_message: str | None,
    skill: str,
    manual_skill_selected: bool,
) -> str | None:
    if not should_route_external_action_to_integration(
        active_user_message=active_user_message,
        skill=skill,
        manual_skill_selected=manual_skill_selected,
    ):
        return None
    import json

    return json.dumps(
        {
            "status": "skipped",
            "reason": "external_platform_action",
            "message": (
                "This turn asks for an external platform workflow. "
                "Do not invoke a writing skill as the primary route; call "
                "search_tools for publish/send actions, or create a durable "
                "draft bundle with write_file/generate_file for copy/image "
                "requests. Use a skill only if the user manually selected it "
                "or explicitly named it."
            ),
            "suggested_next_tool": "search_tools",
            "suggested_next_tools": ["write_file", "generate_file", "search_tools"],
        },
        ensure_ascii=False,
    )


def runtime_format_invoke_skill_result(skill: str, result: dict) -> str:
    import json

    chrome_outcome = result.get("chrome_outcome")
    if isinstance(chrome_outcome, dict):
        payload = dict(chrome_outcome)
        payload.setdefault("skill", result.get("skill") or skill)
        for key in ("stop_parent", "stop_reason", "notice_key", "replace_visible_text", "control"):
            if key in result:
                payload[key] = result.get(key)
        return json.dumps(payload, ensure_ascii=False)

    stop_reason = result.get("stop_reason")
    if result.get("stop_parent"):
        return json.dumps(
            {
                "status": "terminal",
                "skill": result.get("skill") or skill,
                "content": result.get("content") or "",
                "stop_parent": True,
                "stop_reason": stop_reason or "skill_terminal",
                "notice_key": result.get("notice_key"),
                "replace_visible_text": result.get("replace_visible_text", True),
                "control": result.get("control") or {},
            },
            ensure_ascii=False,
        )
    if stop_reason == "credit_exhausted":
        return json.dumps(
            {
                "status": "failed",
                "stop_reason": "credit_exhausted",
                "error": result.get("error") or result.get("content") or "Credits exhausted",
                "limit_detail": result.get("limit_detail"),
            },
            ensure_ascii=False,
        )
    if result.get("error"):
        detail = str(result.get("content") or "").strip()
        if detail and detail != str(result["error"]):
            return f"Error invoking skill '{skill}': {result['error']}\n\n{detail}"
        return f"Error invoking skill '{skill}': {result['error']}"
    if stop_reason == "error":
        return f"Error invoking skill '{skill}': {result.get('content') or 'unknown error'}"
    return result.get("content") or ""


def runtime_skill_invocation_metadata(runtime_context: Any | None) -> dict[str, Any] | None:
    """Return parent runtime metadata that prompt-skill child loops must inherit."""

    metadata: dict[str, Any] = {}
    envelope = getattr(runtime_context, "runtime_envelope", None)
    raw_metadata = getattr(envelope, "metadata", None)
    if isinstance(raw_metadata, dict):
        metadata.update(
            {
                key: value
                for key, value in raw_metadata.items()
                if key not in {"disable_tools", "forced_tool_calls", "legacy_path"}
                and not str(key).startswith("runtime_")
            }
        )
    raw_llm_metadata = getattr(runtime_context, "llm_metadata", None)
    if isinstance(raw_llm_metadata, dict):
        metadata.update(raw_llm_metadata)
    return metadata or None


def runtime_terminal_tool_result_policy_for_skill(skill) -> dict[str, Any] | None:
    """Return terminal tool-result policy for a prompt skill invocation."""

    config = getattr(skill, "config", None) or {}
    runtime = config.get("runtime") if isinstance(config, dict) else None
    if isinstance(runtime, dict) and runtime.get("terminal_tool_results"):
        return runtime
    if is_local_coding_skill(*skill_slug_and_name(skill)):
        return _LOCAL_CODING_TERMINAL_TOOL_RESULT_POLICY
    return None


async def runtime_skill_descriptors_from_tool_kwargs(
    db: AsyncSession | None,
    kwargs: dict[str, Any],
    *,
    limit: int = 50,
) -> list[SkillDescriptor] | None:
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    if runtime_context.runtime_envelope is None:
        return None
    return await resolve_skill_descriptors_for_envelope(
        db,
        runtime_context.runtime_envelope,
        allowed_tool_names=runtime_context.allowed_tool_names,
        active_user_message=runtime_context.active_user_message,
        manual_skill_selected=runtime_context.manual_skill_selected,
        limit=limit,
    )


def _mcp_server_prefixes(tool_names: Iterable[str] | None) -> set[str]:
    """Collapse ``mcp__<server>__<tool>`` names to their ``mcp__<server>__`` prefix."""
    prefixes: set[str] = set()
    for raw in tool_names or ():
        parts = str(raw or "").split("__")
        if len(parts) >= 3 and parts[0] == "mcp" and parts[1]:
            prefixes.add(f"mcp__{parts[1]}__")
    return prefixes


def _is_mcp_guidance_pack(skill) -> bool:
    """A per-MCP built-in guidance pack uses an ``mcp_*`` or ``mcp-*`` slug."""
    slug, _name = skill_slug_and_name(skill)
    return str(slug or "").startswith(("mcp_", "mcp-"))


def _pack_declared_tool_names(skill) -> tuple[str, ...]:
    """Declared tools for either a DB ``Skill`` (``tools``) or a runtime
    ``SkillDescriptor`` (``declared_tools``)."""
    raw = getattr(skill, "declared_tools", None) or getattr(skill, "tools", None) or ()
    return tuple(str(name) for name in raw if str(name or "").strip())


def _mcp_pack_tools_available(skill, available_prefixes: set[str]) -> bool:
    """True when the pack's MCP is connectable this turn (its tools are available).

    A pack declares its MCP via concrete ``mcp__<server>__*`` tools or, for a
    discovery-only vendor surface, ``discoverable_tool_prefixes``. If it
    declares neither, it is not gated (shown). Otherwise it is shown only when
    at least one of its servers appears in the available tool surface.
    """
    declared = _mcp_server_prefixes(_pack_declared_tool_names(skill))
    metadata = getattr(skill, "metadata", None)
    config = getattr(skill, "config", None)
    discovery_config = config if isinstance(config, dict) else metadata
    discoverable = {
        str(prefix).strip()
        for prefix in (
            (discovery_config or {}).get("discoverable_tool_prefixes") or ()
        )
        if str(prefix or "").strip().startswith("mcp__")
    }
    required = declared | discoverable
    if not required:
        return True
    return bool(required & available_prefixes)


def render_runtime_available_skills_section(
    skills: Iterable,
    *,
    active_user_message: str | None,
    manual_skill_selected: bool = False,
    loaded_tool_names: Iterable[str] | None = None,
    available_tool_names: Iterable[str] | None = None,
) -> str | None:
    """Render prompt-visible skill descriptors without loading full instructions.

    ``available_tool_names`` is the connectable tool surface for the turn
    (loaded ∪ allowed). When provided, per-MCP guidance packs (``mcp_*`` slugs)
    are listed only if their MCP's tools are available — so a pack never shows
    for an MCP the agent has not connected. When ``None``, no MCP gating is
    applied (backward-compatible).
    """
    items = list(skills or [])
    routing_message = runtime_available_skills_omission_section(
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
    )
    if routing_message:
        filtered = retain_required_skill_invocation_policies(items, ())
        if not filtered:
            return routing_message
    else:
        filtered, routing_message = _filter_skills_for_prompt(
            items,
            active_user_message=active_user_message,
            manual_skill_selected=manual_skill_selected,
        )
        if routing_message and not filtered:
            return routing_message

    if available_tool_names is not None and not manual_skill_selected:
        available_prefixes = _mcp_server_prefixes(available_tool_names)
        filtered = [
            skill
            for skill in filtered
            if not _is_mcp_guidance_pack(skill) or _mcp_pack_tools_available(skill, available_prefixes)
        ]

    if not filtered:
        return routing_message

    loaded = {str(name) for name in (loaded_tool_names or ()) if str(name or "").strip()}
    lines = ["## Available Skills"]
    if "invoke_skill" in loaded:
        lines.append("Use `invoke_skill(skill=<slug>, input=<instructions>)` to run these:")
    else:
        lines.append(
            "`invoke_skill` is available in this runtime but its schema may be deferred. "
            "Call `search_tools` for `invoke_skill` first if needed, then run one of these skills:"
        )
    invocation_policy_lines = []
    if not manual_skill_selected:
        for skill in filtered:
            policy = trusted_skill_invocation_policy(skill)
            if policy is None:
                continue
            skill_id = str(getattr(skill, "id", "") or "")
            invocation_policy_lines.append(
                render_skill_invocation_policy(
                    skill_id=skill_id,
                    policy=policy,
                )
            )
    if invocation_policy_lines:
        lines.append("### Required Skill Invocation Policies")
        lines.extend(invocation_policy_lines)
    if routing_message:
        _heading, _separator, routing_body = routing_message.partition("\n")
        lines.append("### Runtime Routing Constraint")
        lines.append(
            "This constraint applies only to optional/domain Skills and does "
            "not override any applicable Required Skill Invocation Policy above."
        )
        lines.append(routing_body or routing_message)
    for skill in filtered:
        slug, _name = skill_slug_and_name(skill)
        desc = str(getattr(skill, "description", "") or "")
        short = desc.split(".")[0].strip()
        if len(short) > 120:
            short = short[:117] + "..."
        source_label = _runtime_skill_source_label(skill)
        skill_id = str(getattr(skill, "id", "") or "")
        lines.append(f"- **{skill_id}** (`{slug}`) [{source_label}]: {short}")
    return "\n".join(lines)


def with_skill_descriptors(
    envelope: RuntimeEnvelope,
    descriptors: Iterable[SkillDescriptor],
) -> RuntimeEnvelope:
    """Return an envelope annotated with prompt-visible skill descriptors."""
    serialized = skill_descriptors_to_trace_dict(descriptors)
    metadata = dict(envelope.metadata or {})
    metadata["runtime_skill_descriptors"] = serialized
    return replace(
        envelope,
        skill_descriptors=serialized,
        metadata=metadata,
    )


@dataclass
class RuntimeSkillMiddleware:
    """Async middleware that resolves prompt-visible skill descriptors."""

    db: AsyncSession | None
    invoke_skill_visible: bool | None = None
    allowed_tool_names: Iterable[str] | None = None
    active_user_message: str | None = None
    manual_skill_selected: bool = False
    limit: int = 8
    name: str = "skill"
    resolved_descriptors: list[SkillDescriptor] = field(default_factory=list, init=False)

    async def apply(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        descriptors = await resolve_skill_descriptors_for_envelope(
            self.db,
            envelope,
            invoke_skill_visible=self.invoke_skill_visible,
            allowed_tool_names=self.allowed_tool_names,
            active_user_message=self.active_user_message,
            manual_skill_selected=self.manual_skill_selected,
            limit=self.limit,
        )
        self.resolved_descriptors = list(descriptors)
        return with_skill_descriptors(envelope, descriptors)


@dataclass(frozen=True)
class RuntimePromptSkillToolSurface:
    """Runtime-owned tool surface for one prompt-skill invocation."""

    declared_tool_names: tuple[str, ...]
    skill_tool_names: tuple[str, ...]
    discoverable_tool_names: tuple[str, ...]
    tools: list[dict[str, Any]]
    allowed_tool_names: frozenset[str] | None = None
    harness: RuntimeHarness | None = None


def _declared_tool_names(skill) -> tuple[str, ...]:
    return tuple(str(tool_name) for tool_name in (getattr(skill, "tools", None) or ()) if str(tool_name or "").strip())


def _skill_has_bundle_extra_files(skill) -> bool:
    config = getattr(skill, "config", None) or {}
    return isinstance(config, dict) and isinstance(config.get("extra_files"), dict) and bool(config.get("extra_files"))


def _prompt_skill_declared_tool_names(skill) -> tuple[str, ...]:
    declared = list(_declared_tool_names(skill))
    if _skill_has_bundle_extra_files(skill):
        for tool_name in ("read_file", "list_files"):
            if tool_name not in declared:
                declared.append(tool_name)
    return tuple(declared)


def _prompt_skill_discoverable_tool_names(
    skill,
    allowed_tool_names: Iterable[str] | None,
    *,
    runtime_envelope: RuntimeEnvelope | None = None,
    registered_tool_names: Iterable[str] = (),
) -> tuple[str, ...]:
    config = getattr(skill, "config", None) or {}
    if not isinstance(config, dict):
        return ()
    prefixes = tuple(
        str(prefix).strip()
        for prefix in (config.get("discoverable_tool_prefixes") or ())
        if str(prefix or "").strip()
    )
    if not prefixes:
        return ()
    provider_keys = {
        str(provider).strip()
        for provider in (config.get("discoverable_provider_keys") or ())
        if str(provider or "").strip()
    }
    declared = set(_prompt_skill_declared_tool_names(skill))
    candidates = {
        str(tool_name).strip()
        for tool_name in registered_tool_names
        if str(tool_name or "").strip()
    }
    allowed = _runtime_allowed_tool_name_set(allowed_tool_names)
    if allowed is not None:
        candidates &= set(allowed)
    if provider_keys:
        from packages.core.ai.runtime.tool_discovery import runtime_mcp_provider_from_tool_name

        candidates = {
            tool_name
            for tool_name in candidates
            if runtime_mcp_provider_from_tool_name(tool_name) in provider_keys
        }
    return tuple(sorted({
        tool_name
        for tool_name in candidates
        if tool_name not in declared and tool_name.startswith(prefixes)
    }))


def _runtime_allowed_tool_name_set(
    allowed_tool_names: Iterable[str] | None,
) -> frozenset[str] | None:
    if allowed_tool_names is None:
        return None
    return frozenset(str(tool_name) for tool_name in allowed_tool_names if str(tool_name or "").strip())


def _prompt_skill_effective_allowed_tools(
    declared_tool_names: Iterable[str],
    allowed_tool_names: Iterable[str] | None,
    runtime_envelope: RuntimeEnvelope | None = None,
) -> frozenset[str] | None:
    # A Skill describes how to use tools; it is never an authorization grant.
    # ``None`` means the parent runtime is intentionally unrestricted.  Any
    # concrete allowlist, including an empty one, must pass through unchanged
    # so the child cannot gain a declared tool that its parent could not use.
    return _runtime_allowed_tool_name_set(allowed_tool_names)


def _runtime_visible_declared_tools(
    declared_tools: Iterable[str],
    *,
    profile: RuntimeProfile | None = None,
    profile_allowed_tools: Iterable[str] | None = None,
    runtime_allowed_tools: Iterable[str] | None = None,
) -> set[str]:
    visible = {str(tool_name) for tool_name in declared_tools if str(tool_name or "").strip()}
    if profile_allowed_tools is not None:
        visible &= {str(tool_name) for tool_name in profile_allowed_tools if str(tool_name or "").strip()}
    if (
        profile
        in {
            RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
            RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
        }
        and runtime_allowed_tools is not None
    ):
        visible &= {str(tool_name) for tool_name in runtime_allowed_tools if str(tool_name or "").strip()}
    return visible


def _prompt_skill_runtime_envelope(
    runtime_envelope: RuntimeEnvelope | None,
    *,
    allowed_tool_names: Iterable[str] | None,
    skill_tool_names: Iterable[str],
    discovery_policy: dict[str, tuple[str, ...]] | None = None,
) -> RuntimeEnvelope | None:
    if runtime_envelope is None:
        return None
    effective_allowed = set(str(tool_name) for tool_name in (allowed_tool_names or ()) if str(tool_name or "").strip())
    if not effective_allowed:
        return runtime_envelope
    effective_tools = (
        set(skill_tool_names)
        if discovery_policy is not None
        else set(runtime_envelope.tool_names or ()) | set(skill_tool_names)
    )
    # Chrome Skill child loops continue the same Browser Group/Harness run.
    # Keep the state in the isolated child envelope; never mutate the parent.
    metadata = deepcopy(runtime_envelope.metadata)
    if discovery_policy is not None:
        metadata["prompt_skill_tool_discovery"] = {
            key: list(values)
            for key, values in discovery_policy.items()
        }
    return replace(
        runtime_envelope,
        tool_names=tuple(sorted(effective_tools)),
        allowed_tool_names=tuple(sorted(effective_allowed)),
        metadata=metadata,
    )


def _skill_allowed_surface_values(skill) -> set[str]:
    config = getattr(skill, "config", None) or {}
    if not isinstance(config, dict):
        return set()
    raw = config.get("allowed_surfaces") or config.get("public_allowed_surfaces") or config.get("runtime_surfaces")
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return {str(value).strip() for value in values if str(value or "").strip()}


def runtime_skill_allowed_on_surface(skill, surface: ChatSurface | str | None) -> bool:
    """Return whether a skill may be exposed/invoked on this runtime surface."""

    surface_value = getattr(surface, "value", surface)
    surface_name = str(surface_value or "").strip()
    if not surface_name:
        return True
    if surface_name not in {
        ChatSurface.PUBLIC_CUSTOMER_CHAT.value,
        ChatSurface.EXTERNAL_CHANNEL_CHAT.value,
    }:
        return True
    return surface_name in _skill_allowed_surface_values(skill)


def runtime_prepare_prompt_skill_tool_surface(
    skill,
    *,
    allowed_tool_names: Iterable[str] | None = None,
    runtime_envelope: RuntimeEnvelope | None = None,
    get_schemas_for_names: Callable[[list[str]], list[dict[str, Any]]] | None = None,
    get_registered_tool_names: Callable[[], Iterable[str]] | None = None,
) -> RuntimePromptSkillToolSurface:
    """Prepare the schema-visible tool surface for a prompt skill.

    Prompt skill invocations inherit the parent runtime policy scope.  Declared
    tools describe what the Skill can use inside that scope; they never expand
    the Agent's authorization.
    """

    declared = _prompt_skill_declared_tool_names(skill)
    parent_allowed = _prompt_skill_effective_allowed_tools(
        declared,
        allowed_tool_names,
        runtime_envelope,
    )
    skill_tool_names = tuple(
        tool_name
        for tool_name in declared
        if parent_allowed is None or tool_name in parent_allowed
    )
    if get_registered_tool_names is None:
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names

        get_registered_tool_names = runtime_registered_tool_names
    discoverable = _prompt_skill_discoverable_tool_names(
        skill,
        allowed_tool_names,
        runtime_envelope=runtime_envelope,
        registered_tool_names=get_registered_tool_names(),
    )
    config = getattr(skill, "config", None) or {}
    discovery_policy = None
    if discoverable or (isinstance(config, dict) and config.get("discoverable_tool_prefixes")):
        discovery_policy = {
            "provider_keys": tuple(
                str(value).strip()
                for value in (config.get("discoverable_provider_keys") or ())
                if str(value or "").strip()
            ),
            "tool_prefixes": tuple(
                str(value).strip()
                for value in (config.get("discoverable_tool_prefixes") or ())
                if str(value or "").strip()
            ),
        }
        allowed = frozenset((*skill_tool_names, *discoverable))
    else:
        allowed = parent_allowed
    if get_schemas_for_names is None:
        from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_names

        get_schemas_for_names = runtime_tool_schemas_for_names
    skill_envelope = _prompt_skill_runtime_envelope(
        runtime_envelope,
        allowed_tool_names=allowed,
        skill_tool_names=skill_tool_names,
        discovery_policy=discovery_policy,
    )
    return RuntimePromptSkillToolSurface(
        declared_tool_names=declared,
        skill_tool_names=skill_tool_names,
        discoverable_tool_names=discoverable,
        tools=list(get_schemas_for_names(list(skill_tool_names)) or []),
        allowed_tool_names=allowed,
        harness=RuntimeHarness(skill_envelope) if skill_envelope is not None else None,
    )


def runtime_prompt_skill_tool_schema_resolver(
    *,
    declared_tool_names: Iterable[str],
    allowed_tool_names: Iterable[str] | None = None,
    get_schema: Callable[[str], dict[str, Any] | None] | None = None,
    required_arguments_by_tool: Mapping[str, Iterable[str]] | None = None,
) -> Callable[[str], dict[str, Any] | None]:
    """Build a resolver for skill-declared tools plus parent-visible MCP tools."""

    if get_schema is None:
        from packages.core.ai.runtime.tool_registry import runtime_tool_schema

        get_schema = runtime_tool_schema
    declared = {str(tool_name) for tool_name in declared_tool_names if str(tool_name or "").strip()}
    allowed = _runtime_allowed_tool_name_set(allowed_tool_names)
    required_arguments = {
        str(tool_name): {
            str(argument)
            for argument in arguments
            if str(argument or "").strip()
        }
        for tool_name, arguments in dict(required_arguments_by_tool or {}).items()
    }

    def _resolver(name: str) -> dict[str, Any] | None:
        tool_name = str(name or "").strip()
        if not tool_name:
            return None
        if allowed is not None and tool_name not in allowed:
            return None
        if tool_name.startswith("mcp__") or tool_name in declared:
            schema = get_schema(tool_name)
            additions = required_arguments.get(tool_name) or set()
            if schema is None or not additions:
                return schema
            resolved = deepcopy(schema)
            function = resolved.get("function") if isinstance(resolved, dict) else None
            parameters = function.get("parameters") if isinstance(function, dict) else None
            if not isinstance(parameters, dict):
                return resolved
            current = parameters.get("required")
            required = [
                str(value)
                for value in (current if isinstance(current, list) else [])
                if str(value or "").strip()
            ]
            for argument in sorted(additions):
                if argument not in required:
                    required.append(argument)
            parameters["required"] = required
            return resolved
        return None

    return _resolver


# Document/office skills assemble decks, docs, PDFs, and sheets - they
# legitimately generate images, but they have no business producing video or
# audio. Left unguarded, a stuck document run can flail into
# generate_file(kind="video") with a prompt bled from earlier in the
# conversation, producing a document request that ends in an unrelated clip.
# Deny those media kinds outright for these skills (slug or built-in alias).
_DOCUMENT_SKILL_SLUGS = frozenset(
    {
        "pptx",
        "presentation",
        "docx",
        "word_document",
        "doc",
        "pdf",
        "xlsx",
        "spreadsheet",
    }
)
_DOCUMENT_SKILL_BLOCKED_MEDIA_KINDS = frozenset({"video", "audio"})


def document_skill_media_guard(skill_slug: str | None, tool_name: str, args: Any) -> str | None:
    """Refuse video/audio generation from a document/office skill run.

    Returns a tool-result string to short-circuit the call, or None to allow it.
    """
    if str(tool_name or "").strip() != "generate_file":
        return None
    slug = str(skill_slug or "").strip().lower()
    if slug not in _DOCUMENT_SKILL_SLUGS:
        return None
    tool_args = args if isinstance(args, dict) else {}
    kind = str(tool_args.get("kind") or "").strip().lower()
    if kind not in _DOCUMENT_SKILL_BLOCKED_MEDIA_KINDS:
        return None
    return json.dumps(
        {
            "error": (
                f"generate_file(kind='{kind}') is not allowed inside the "
                f"'{slug}' skill. This skill produces documents; it must not "
                "generate video or audio. If you are stuck assembling the "
                "document, stop and report what is missing — do not switch to an "
                "unrelated media artifact."
            )
        },
        ensure_ascii=False,
    )


class RuntimePromptSkillExecutionContractKind(StrEnum):
    STICKMAN_VIDEO = "stickman_video"


_STICKMAN_PROMPT_SKILL_SLUGS = frozenset({
    "stickman-video-creator",
    "stickman_video_creator",
})
_STICKMAN_PROMPT_EXECUTION_CONTRACT: dict[str, Any] = {
    "kind": RuntimePromptSkillExecutionContractKind.STICKMAN_VIDEO.value,
    "guidance": (
        "The authoritative Workflow artifact prefix for this run is "
        "{run_artifact_prefix}. Keep every run-owned derivative inside it."
    ),
    "artifact_scope": {
        "prefix_directory": "runs",
        "lineage_context_keys": [
            "_workflow_lineage_root_run_id_from_context",
            "_workflow_run_id_from_context",
        ],
        "generated_directories": [
            "audio", "final", "images", "qa", "subtitles", "technical", "video",
        ],
        "path_arguments": [
            "audio_path", "cues_name", "directory", "filename", "input_path",
            "manifest_name", "media_path", "name", "output_dir", "output_name",
            "path", "subtitle_path", "timeline_name", "transcript_path",
        ],
        "defaultable_arguments": [
            "filename", "name", "output_dir", "output_name", "path",
        ],
        "default_directories_by_tool": {
            "align_subtitles": "technical",
            "build_narration_timeline": "technical",
            "compose_video_timeline": "final",
            "generate_file": "technical",
            "generate_image": "images",
            "generate_video": "video",
            "merge_videos": "video",
            "normalize_audio_loudness": "audio/normalized",
            "render_frame_samples": "qa",
            "still_to_video": "video",
            "write_file": "technical",
        },
        "default_directory_rules": [
            {"tool": "generate_file", "when": {"kind": "audio"}, "directory": "audio"},
        ],
    },
    "required_output_rules": [
        {
            "tool": "generate_file",
            "when": {"kind": "audio", "purpose": "narration"},
            "output_arguments": ["output_name", "name", "filename"],
            "directory": "audio",
            "code": "stickman_run_output_name_required",
            "error": (
                "{tool_name} requires an explicit stable name inside "
                "{run_artifact_prefix}/{directory}. Retry this call with the "
                "manifest segment or scene number in the filename; unnamed "
                "provider defaults are not valid Stickman Workflow artifacts."
            ),
        },
        {
            "tool": "generate_video",
            "output_arguments": ["output_name", "name", "filename"],
            "directory": "video",
            "code": "stickman_run_output_name_required",
            "error": (
                "{tool_name} requires an explicit stable name inside "
                "{run_artifact_prefix}/{directory}. Retry this call with the "
                "manifest segment or scene number in the filename; unnamed "
                "provider defaults are not valid Stickman Workflow artifacts."
            ),
        },
    ],
    "prerequisites": [
        {
            "before_tool": "generate_video",
            "path": "technical/narration-timeline.json",
            "limit": 10000,
            "max_chars": 200000,
            "required_non_empty_array_groups": [
                ["cues", "subtitle_cues"],
                "audio_tracks",
            ],
            "code": "stickman_narration_timeline_required",
            "invalid_json_reason": "narration-timeline.json is not valid JSON",
            "invalid_content_reason": (
                "narration-timeline.json has no measured cues or audio tracks"
            ),
            "error": (
                "generate_video is blocked until build_narration_timeline "
                "succeeds for {required_path}: {reason}. Build the timeline "
                "from every normalized narration segment, then retry."
            ),
        },
    ],
    "result_receipts": [
        {
            "after_tool": "generate_image",
            "when": {
                "reused_workspace_asset": True,
                "workspace_asset_key": "stickman_character",
            },
            "source_fields": ["image_url", "result_url"],
            "path": "technical/person-reference-url.txt",
            "max_chars": 4096,
            "code": "workspace_identity_receipt_write_failed",
            "error": (
                "The reusable Workspace Stickman was resolved, but its "
                "run-scoped durable person-reference receipt could not be "
                "written and read back. Stop before paid media calls."
            ),
            "result_fields": {
                "durable_person_reference_path": "{receipt_path}",
                "durable_person_reference_verified": True,
            },
        },
    ],
}


class RuntimePromptSkillExecutionContractFactory:
    """Resolve configured contracts with typed built-in compatibility defaults."""

    @staticmethod
    def create(
        *,
        skill: Any | None = None,
        skill_slug: str | None = None,
        configured_contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(configured_contract, Mapping) and configured_contract:
            return deepcopy(dict(configured_contract))
        slug = str(
            skill_slug
            or getattr(skill, "slug", "")
            or getattr(skill, "name", "")
            or ""
        ).strip().lower()
        if slug in _STICKMAN_PROMPT_SKILL_SLUGS:
            return deepcopy(_STICKMAN_PROMPT_EXECUTION_CONTRACT)
        return {}


def runtime_prompt_skill_execution_contract(skill: Any) -> dict[str, Any]:
    """Return the configured or typed built-in prompt execution contract."""

    config = getattr(skill, "config", None)
    runtime = config.get("runtime") if isinstance(config, dict) else None
    configured = (
        runtime.get("prompt_skill_execution")
        if isinstance(runtime, dict)
        else None
    )
    return RuntimePromptSkillExecutionContractFactory.create(
        skill=skill,
        configured_contract=configured,
    )


def _execution_contract_artifact_prefix(
    contract: Mapping[str, Any] | None,
    runtime_tool_context: Mapping[str, Any] | None,
) -> str | None:
    scope = dict((contract or {}).get("artifact_scope") or {})
    if not scope:
        return None
    context = dict(runtime_tool_context or {})
    context_keys = scope.get("lineage_context_keys") or [
        "_workflow_lineage_root_run_id_from_context",
        "_workflow_run_id_from_context",
    ]
    lineage_root = next(
        (
            str(context.get(str(key)) or "").strip()
            for key in context_keys
            if str(context.get(str(key)) or "").strip()
        ),
        "",
    )
    if not lineage_root:
        return None
    prefix_directory = str(scope.get("prefix_directory") or "runs").strip("/")
    return f"{prefix_directory}/{lineage_root}"


def _execution_contract_text(value: Any, **tokens: Any) -> str:
    text = str(value or "")
    for key, token in tokens.items():
        text = text.replace("{" + key + "}", str(token))
    return text


def _execution_contract_matches(
    values: Mapping[str, Any],
    conditions: Mapping[str, Any] | None,
) -> bool:
    for key, expected in dict(conditions or {}).items():
        actual = values.get(str(key))
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def runtime_prompt_skill_execution_guidance(
    contract: Mapping[str, Any] | None,
    runtime_tool_context: Mapping[str, Any] | None,
) -> str | None:
    """Render optional Skill-owned guidance for the resolved artifact scope."""

    run_prefix = _execution_contract_artifact_prefix(
        contract,
        runtime_tool_context,
    )
    guidance = str((contract or {}).get("guidance") or "").strip()
    if not run_prefix or not guidance:
        return None
    return _execution_contract_text(
        guidance,
        run_artifact_prefix=run_prefix,
    )


def runtime_prompt_skill_required_arguments(
    contract: Mapping[str, Any] | None,
) -> dict[str, set[str]]:
    """Return declarative required tool arguments for schema projection."""

    configured = (contract or {}).get("required_arguments_by_tool") or {}
    if not isinstance(configured, dict):
        return {}
    return {
        str(tool_name): {
            str(argument)
            for argument in arguments
            if str(argument or "").strip()
        }
        for tool_name, arguments in configured.items()
        if isinstance(arguments, list)
    }


def _scope_execution_contract_artifact_path(
    value: str,
    *,
    run_prefix: str,
    generated_directories: set[str],
    simple_default_dir: str | None = None,
) -> str:
    original = str(value or "")
    normalized = original.replace("\\", "/").strip()
    if not normalized or "://" in normalized or normalized.startswith("data:"):
        return original
    if normalized == run_prefix or normalized.startswith(f"{run_prefix}/"):
        return normalized

    prefix_directory, run_root = run_prefix.split("/", 1)
    run_marker = f"/{prefix_directory}/"
    if normalized.startswith(f"{prefix_directory}/"):
        parts = normalized.split("/")
        suffix = "/".join(parts[2:])
        return run_prefix if not suffix else f"{run_prefix}/{suffix}"
    if run_marker in normalized:
        base, remainder = normalized.split(run_marker, 1)
        suffix = "/".join(remainder.split("/")[1:])
        scoped = f"{base}/{prefix_directory}/{run_root}"
        return scoped if not suffix else f"{scoped}/{suffix}"

    for artifact_dir in sorted(generated_directories):
        if normalized == artifact_dir or normalized.startswith(f"{artifact_dir}/"):
            return f"{run_prefix}/{normalized}"
        marker = f"/{artifact_dir}/"
        if marker in normalized:
            base, suffix = normalized.split(marker, 1)
            return f"{base}/{run_prefix}/{artifact_dir}/{suffix}"

    if "/" not in normalized and simple_default_dir:
        return f"{run_prefix}/{simple_default_dir}/{normalized}"
    return original


def _scope_execution_contract_artifact_args(
    *,
    contract: Mapping[str, Any] | None,
    tool_name: str,
    args: Mapping[str, Any],
    runtime_tool_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scope = dict((contract or {}).get("artifact_scope") or {})
    run_prefix = _execution_contract_artifact_prefix(
        contract,
        runtime_tool_context,
    )
    copied = deepcopy(dict(args))
    if not scope or run_prefix is None:
        return copied

    generated_directories = {
        str(value).strip("/")
        for value in scope.get("generated_directories") or []
        if str(value or "").strip("/")
    }
    path_arguments = {
        str(value)
        for value in scope.get("path_arguments") or []
        if str(value or "").strip()
    }
    default_dir = str(
        dict(scope.get("default_directories_by_tool") or {}).get(tool_name)
        or ""
    ).strip("/") or None
    for rule in scope.get("default_directory_rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("tool") or "").strip() != tool_name:
            continue
        if _execution_contract_matches(copied, rule.get("when")):
            default_dir = str(rule.get("directory") or "").strip("/") or None
            break

    defaultable_arguments = {
        str(value)
        for value in scope.get("defaultable_arguments")
        or ["filename", "name", "output_dir", "output_name", "path"]
    }

    def _visit(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {
                item_key: _visit(item_value, item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [_visit(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key not in path_arguments and not str(key or "").endswith("_path"):
            return value
        return _scope_execution_contract_artifact_path(
            value,
            run_prefix=run_prefix,
            generated_directories=generated_directories,
            simple_default_dir=default_dir if key in defaultable_arguments else None,
        )

    return _visit(copied)


def _execution_contract_required_output_guard(
    *,
    contract: Mapping[str, Any] | None,
    tool_name: str,
    args: Mapping[str, Any],
    runtime_tool_context: Mapping[str, Any] | None,
) -> str | None:
    run_prefix = _execution_contract_artifact_prefix(
        contract,
        runtime_tool_context,
    )
    if run_prefix is None:
        return None
    for rule in (contract or {}).get("required_output_rules") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("tool") or "").strip() != tool_name:
            continue
        if not _execution_contract_matches(args, rule.get("when")):
            continue
        output_arguments = rule.get("output_arguments") or [
            "output_name",
            "name",
            "filename",
        ]
        if any(str(args.get(str(key)) or "").strip() for key in output_arguments):
            continue
        directory = str(rule.get("directory") or "").strip("/")
        error = _execution_contract_text(
            rule.get("error"),
            tool_name=tool_name,
            run_artifact_prefix=run_prefix,
            directory=directory,
        )
        return json.dumps(
            {
                "status": "blocked",
                "code": str(rule.get("code") or "run_output_name_required"),
                "error": error,
                "run_artifact_prefix": run_prefix,
            },
            ensure_ascii=False,
        )
    return None


def _execution_contract_json_value(
    payload: Mapping[str, Any],
    dotted_path: str,
) -> Any:
    value: Any = payload
    for part in str(dotted_path).split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _execution_contract_prerequisite_error(
    *,
    rule: Mapping[str, Any],
    run_prefix: str,
    required_path: str,
    read_result: str,
) -> str | None:
    invalid_reason = str(
        rule.get("invalid_json_reason") or "the receipt is not valid JSON"
    )
    try:
        read_payload = json.loads(read_result)
    except (TypeError, ValueError):
        read_payload = None
        reason = str(rule.get("unreadable_reason") or "the receipt could not be read")
    else:
        reason = ""

    receipt_payload: Any = None
    if isinstance(read_payload, dict):
        if read_payload.get("error"):
            reason = str(read_payload.get("error"))
        else:
            try:
                receipt_payload = json.loads(str(read_payload.get("content") or ""))
            except (TypeError, ValueError):
                reason = invalid_reason
    elif not reason:
        reason = str(
            rule.get("unstructured_reason")
            or "the receipt response was not structured JSON"
        )

    if isinstance(receipt_payload, dict):
        valid = True
        for group in rule.get("required_non_empty_array_groups") or []:
            alternatives = group if isinstance(group, list) else [group]
            if not any(
                isinstance(
                    _execution_contract_json_value(receipt_payload, str(path)),
                    list,
                )
                and bool(_execution_contract_json_value(receipt_payload, str(path)))
                for path in alternatives
            ):
                valid = False
                break
        if valid:
            return None
        reason = str(
            rule.get("invalid_content_reason")
            or "the receipt is missing required content"
        )

    error = _execution_contract_text(
        rule.get("error"),
        reason=reason,
        required_path=required_path,
        run_artifact_prefix=run_prefix,
    )
    return json.dumps(
        {
            "status": "blocked",
            "code": str(rule.get("code") or "required_receipt_missing"),
            "error": error,
            "run_artifact_prefix": run_prefix,
            "required_path": required_path,
        },
        ensure_ascii=False,
    )


def runtime_prompt_skill_registered_tool_executor(
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    runtime_envelope: RuntimeEnvelope | None = None,
    skill_slug: str | None = None,
    runtime_tool_context: Mapping[str, Any] | None = None,
    execution_contract: Mapping[str, Any] | None = None,
) -> Callable[[str, Any], Awaitable[str]]:
    """Build the registered-tool executor used inside prompt-skill runs."""

    completed_prerequisites: set[int] = set()
    verified_result_receipts: dict[str, tuple[str, int]] = {}
    contract = RuntimePromptSkillExecutionContractFactory.create(
        skill_slug=skill_slug,
        configured_contract=execution_contract,
    )

    async def _execute(tool_name: str, args: Any) -> str:
        guard = document_skill_media_guard(skill_slug, tool_name, args)
        if guard is not None:
            return guard

        values = dict(args) if isinstance(args, dict) else {}
        guard = _execution_contract_required_output_guard(
            contract=contract,
            tool_name=tool_name,
            args=values,
            runtime_tool_context=runtime_tool_context,
        )
        if guard is not None:
            return guard

        from packages.core.ai.runtime.tool_registry import runtime_execute_tool

        tool_args = _scope_execution_contract_artifact_args(
            contract=contract,
            tool_name=tool_name,
            args=values,
            runtime_tool_context=runtime_tool_context,
        )
        context_args = {
            key: value
            for key, value in dict(runtime_tool_context or {}).items()
            if key in RUNTIME_TOOL_CONTEXT_KEYS
        }
        tool_args.update(context_args)

        if tool_name == "write_file":
            receipt_path = str(tool_args.get("path") or "").strip()
            receipt_content = str(tool_args.get("content") or "")
            verified_receipt = verified_result_receipts.get(receipt_path)
            if verified_receipt is not None and verified_receipt[0] == receipt_content:
                read_result = await runtime_execute_tool(
                    "read_file",
                    {
                        "path": receipt_path,
                        "max_chars": verified_receipt[1],
                        **context_args,
                    },
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    active_user_message=active_user_message,
                    manual_skill_selected=manual_skill_selected,
                    tool_profile=tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    runtime_envelope=runtime_envelope,
                )
                try:
                    existing_receipt = json.loads(read_result)
                except (TypeError, ValueError):
                    existing_receipt = None
                if (
                    isinstance(existing_receipt, dict)
                    and str(existing_receipt.get("content") or "") == receipt_content
                ):
                    return json.dumps(
                        {
                            "written": False,
                            "idempotent": True,
                            "unchanged": True,
                            "path": receipt_path,
                        },
                        ensure_ascii=False,
                    )

        run_prefix = _execution_contract_artifact_prefix(
            contract,
            runtime_tool_context,
        )
        for index, rule in enumerate(contract.get("prerequisites") or []):
            if not isinstance(rule, dict) or index in completed_prerequisites:
                continue
            if str(rule.get("before_tool") or "").strip() != tool_name:
                continue
            if not _execution_contract_matches(values, rule.get("when")):
                continue
            if run_prefix is None:
                continue
            relative_path = str(rule.get("path") or "").strip("/")
            required_path = f"{run_prefix}/{relative_path}"
            prerequisite_result = await runtime_execute_tool(
                "read_file",
                {
                    "path": required_path,
                    "limit": int(rule.get("limit") or 10000),
                    "max_chars": int(rule.get("max_chars") or 200000),
                    **context_args,
                },
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                active_user_message=active_user_message,
                manual_skill_selected=manual_skill_selected,
                tool_profile=tool_profile,
                allowed_tool_names=allowed_tool_names,
                runtime_envelope=runtime_envelope,
            )
            prerequisite_error = _execution_contract_prerequisite_error(
                rule=rule,
                run_prefix=run_prefix,
                required_path=required_path,
                read_result=prerequisite_result,
            )
            if prerequisite_error is not None:
                return prerequisite_error
            completed_prerequisites.add(index)

        result = await runtime_execute_tool(
            tool_name,
            tool_args,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            active_user_message=active_user_message,
            manual_skill_selected=manual_skill_selected,
            tool_profile=tool_profile,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
        )

        for rule in contract.get("result_receipts") or []:
            if not isinstance(rule, dict):
                continue
            if str(rule.get("after_tool") or "").strip() != tool_name:
                continue
            if run_prefix is None:
                continue
            try:
                result_payload = json.loads(result)
            except (TypeError, ValueError):
                continue
            if not isinstance(result_payload, dict):
                continue
            if not _execution_contract_matches(result_payload, rule.get("when")):
                continue
            source_value = next(
                (
                    str(result_payload.get(str(field)) or "").strip()
                    for field in rule.get("source_fields") or []
                    if str(result_payload.get(str(field)) or "").strip()
                ),
                "",
            )
            if not source_value:
                continue
            relative_path = str(rule.get("path") or "").strip("/")
            receipt_path = f"{run_prefix}/{relative_path}"
            receipt_max_chars = int(rule.get("max_chars") or 4096)
            read_result = await runtime_execute_tool(
                "read_file",
                {
                    "path": receipt_path,
                    "max_chars": receipt_max_chars,
                    **context_args,
                },
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                active_user_message=active_user_message,
                manual_skill_selected=manual_skill_selected,
                tool_profile=tool_profile,
                allowed_tool_names=allowed_tool_names,
                runtime_envelope=runtime_envelope,
            )
            try:
                receipt_payload = json.loads(read_result)
            except (TypeError, ValueError):
                receipt_payload = None
            verified = bool(
                isinstance(receipt_payload, dict)
                and str(receipt_payload.get("content") or "").strip()
                == source_value
            )
            if not verified:
                await runtime_execute_tool(
                    "write_file",
                    {"path": receipt_path, "content": source_value, **context_args},
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    active_user_message=active_user_message,
                    manual_skill_selected=manual_skill_selected,
                    tool_profile=tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    runtime_envelope=runtime_envelope,
                )
                read_result = await runtime_execute_tool(
                    "read_file",
                    {
                        "path": receipt_path,
                        "max_chars": receipt_max_chars,
                        **context_args,
                    },
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    active_user_message=active_user_message,
                    manual_skill_selected=manual_skill_selected,
                    tool_profile=tool_profile,
                    allowed_tool_names=allowed_tool_names,
                    runtime_envelope=runtime_envelope,
                )
                try:
                    receipt_payload = json.loads(read_result)
                except (TypeError, ValueError):
                    receipt_payload = None
                verified = bool(
                    isinstance(receipt_payload, dict)
                    and str(receipt_payload.get("content") or "").strip()
                    == source_value
                )
            if not verified:
                error = _execution_contract_text(
                    rule.get("error"),
                    receipt_path=receipt_path,
                    run_artifact_prefix=run_prefix,
                )
                return json.dumps(
                    {
                        "status": "blocked",
                        "code": str(
                            rule.get("code") or "result_receipt_write_failed"
                        ),
                        "error": error,
                        "run_artifact_prefix": run_prefix,
                        "required_path": receipt_path,
                    },
                    ensure_ascii=False,
                )
            verified_result_receipts[receipt_path] = (
                source_value,
                receipt_max_chars,
            )
            for result_field, configured_value in dict(
                rule.get("result_fields") or {}
            ).items():
                result_payload[str(result_field)] = (
                    _execution_contract_text(
                        configured_value,
                        receipt_path=receipt_path,
                        run_artifact_prefix=run_prefix,
                    )
                    if isinstance(configured_value, str)
                    else configured_value
                )
            result = json.dumps(result_payload, ensure_ascii=False)
        return result

    return _execute


def runtime_prompt_skill_bundle_tool_result(
    *,
    harness: RuntimeHarness | None,
    tool_name: str,
    arguments: dict[str, Any],
    bundle_handler: Callable[[dict[str, Any]], str | None],
) -> str | None:
    """Serve prompt-skill bundle files without bypassing runtime policy/events."""

    if harness is not None:
        decision = harness.check_tool_call(tool_name, arguments)
        if not decision.allowed:
            return decision.to_tool_result()
    result = bundle_handler(arguments)
    if result is None:
        return None
    if harness is not None:
        harness.record_event("tool_start", tool_name=tool_name)
        harness.record_event("tool_end", tool_name=tool_name)
    return result


def runtime_prompt_skill_tool_executor(
    *,
    harness: RuntimeHarness | None,
    execute_tool: Callable[[str, Any], Awaitable[str]],
    read_bundle_file: Callable[[dict[str, Any]], str | None] | None = None,
    list_bundle_files: Callable[[dict[str, Any]], str | None] | None = None,
) -> Callable[[str, Any], Awaitable[str]]:
    """Build the nested tool executor used by prompt skills."""

    async def _executor(name: str, args: Any) -> str:
        tool_name = str(name or "").strip()
        tool_args = args if isinstance(args, dict) else {}
        if tool_name == "read_file" and read_bundle_file is not None:
            bundle_result = runtime_prompt_skill_bundle_tool_result(
                harness=harness,
                tool_name=tool_name,
                arguments=tool_args,
                bundle_handler=read_bundle_file,
            )
            if bundle_result is not None:
                return bundle_result
        if tool_name == "list_files" and list_bundle_files is not None:
            bundle_result = runtime_prompt_skill_bundle_tool_result(
                harness=harness,
                tool_name=tool_name,
                arguments=tool_args,
                bundle_handler=list_bundle_files,
            )
            if bundle_result is not None:
                return bundle_result
        return await execute_tool(tool_name, args)

    return _executor


def descriptor_from_skill(
    skill,
    *,
    source: SkillSource = "entity",
    reason: str | None = None,
    surface: ChatSurface | None = None,
    profile: RuntimeProfile | None = None,
    visible_declared_tools: tuple[str, ...] | None = None,
) -> SkillDescriptor:
    declared_tools = visible_declared_tools
    if declared_tools is None:
        declared_tools = _declared_tool_names(skill)
    required_capabilities = tuple(
        capability.id
        for capability in capabilities_for_tool_names(
            set(declared_tools),
            profile=profile,
        )
    )
    metadata = {
        "category": str(getattr(skill, "category", "") or ""),
        "output_format": str(getattr(skill, "output_format", "") or ""),
    }
    config = getattr(skill, "config", None)
    if isinstance(config, dict):
        for key in ("discoverable_provider_keys", "discoverable_tool_prefixes"):
            values = tuple(
                str(value).strip()
                for value in (config.get(key) or ())
                if str(value or "").strip()
            )
            if values:
                metadata[key] = values
    invocation_policy = trusted_skill_invocation_policy(skill, source=source)
    if invocation_policy is not None:
        metadata["invocation_policy"] = invocation_policy.to_dict()

    return SkillDescriptor(
        id=str(getattr(skill, "id", "") or ""),
        slug=str(getattr(skill, "slug", "") or getattr(skill, "name", "") or ""),
        name=str(getattr(skill, "display_name", "") or getattr(skill, "name", "") or getattr(skill, "slug", "") or ""),
        description=str(getattr(skill, "description", "") or ""),
        source=source,
        allowed_surfaces=(surface,) if surface else (),
        required_capabilities=required_capabilities,
        declared_tools=declared_tools,
        visibility_reason=reason,
        metadata=metadata,
    )


async def runtime_agent_bound_skill_ids(
    db: AsyncSession | None,
    agent_id: str | None,
) -> set[str]:
    if not db or not agent_id:
        return set()
    from sqlalchemy import select

    from packages.core.models.skill import AgentSkillBinding

    result = await db.execute(
        select(AgentSkillBinding.skill_id).where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.status == "active",
        )
    )
    return {str(skill_id) for skill_id in result.scalars().all() if str(skill_id or "").strip()}


async def _runtime_agent_bound_skill_ids(
    db: AsyncSession | None,
    agent_id: str | None,
) -> set[str]:
    return await runtime_agent_bound_skill_ids(db, agent_id)


async def runtime_agent_has_surface_bound_skill(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    agent_id: str | None,
    surface: ChatSurface,
    profile: RuntimeProfile | None = None,
    allowed_tool_names: Iterable[str] | None = None,
) -> bool:
    """Return whether an agent has a directly bound skill usable on a surface."""

    if not db or not entity_id or not agent_id:
        return False
    try:
        from packages.core.services.skill_service import list_agent_skill_bindings

        skills = await list_agent_skill_bindings(db, agent_id, entity_id)
    except Exception:
        return False
    profile_allowed_tools = allowed_tools_for_profile(profile) if profile else None
    runtime_allowed_tools = (
        {str(tool_name) for tool_name in allowed_tool_names if str(tool_name or "").strip()}
        if allowed_tool_names is not None
        else None
    )
    for skill in skills:
        if not runtime_skill_allowed_on_surface(skill, surface):
            continue
        declared_tools = _declared_tool_names(skill)
        visible_declared_tools = _runtime_visible_declared_tools(
            declared_tools,
            profile=profile,
            profile_allowed_tools=profile_allowed_tools,
            runtime_allowed_tools=runtime_allowed_tools,
        )
        if declared_tools and not visible_declared_tools:
            continue
        return True
    return False


def runtime_filter_skills_for_installed_ledgers(
    skills: Iterable[Any],
    installed_contract_ids: Iterable[str],
) -> list[Any]:
    """Keep repository Ledger Skills beside their installed contract only.

    ``ledger_contracts`` is trusted only on built-in Skills. Entity and
    Marketplace Skills cannot hide themselves behind Workspace configuration
    or turn their config into runtime policy.
    """

    installed = {
        str(contract_id).strip()
        for contract_id in installed_contract_ids
        if str(contract_id or "").strip()
    }
    filtered: list[Any] = []
    for skill in skills:
        config = getattr(skill, "config", None)
        required = (
            config.get("ledger_contracts")
            if (
                getattr(skill, "entity_id", "missing") is None
                and isinstance(config, dict)
                and config.get("source") == "builtin"
            )
            else None
        )
        if not required:
            filtered.append(skill)
            continue
        if not isinstance(required, list):
            continue
        required_ids = {
            str(contract_id).strip()
            for contract_id in required
            if str(contract_id or "").strip()
        }
        if required_ids and required_ids.issubset(installed):
            filtered.append(skill)
    return filtered


def _runtime_skills_need_installed_ledger_contracts(
    skills: Iterable[Any],
) -> bool:
    """Return whether runtime Skill filtering needs Workspace Ledger state."""

    for skill in skills:
        config = getattr(skill, "config", None)
        if (
            getattr(skill, "entity_id", "missing") is None
            and isinstance(config, dict)
            and config.get("source") == "builtin"
            and bool(config.get("ledger_contracts"))
        ):
            return True
    return False


async def _runtime_workspace_ledger_contract_ids(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    workspace_id: str | None,
) -> set[str]:
    if not db or not entity_id or not workspace_id:
        return set()

    from sqlalchemy import select

    from packages.core.models.workspace import Workspace
    from packages.core.services.ledger_query_service import (
        workspace_queryable_ledger_configs,
    )

    settings = (await db.execute(
        select(Workspace.settings).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    return set(workspace_queryable_ledger_configs(settings))


async def runtime_skill_is_eligible(
    db: AsyncSession,
    skill: Any,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None = None,
    enforce_user_access: bool = False,
    readable_skill_ids: set[str] | None = None,
    installed_ledger_contract_ids: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Enforce the shared runtime admission policy for one Skill."""

    skill_entity_id = getattr(skill, "entity_id", None)
    if (
        getattr(skill, "status", None) != "active"
        or skill_entity_id not in {None, entity_id}
    ):
        return False, "skill_not_found"

    if enforce_user_access and skill_entity_id is not None:
        if not user_id:
            return False, "skill_not_allowed"
        skill_id = str(getattr(skill, "id", "") or "")
        if readable_skill_ids is not None:
            if skill_id not in readable_skill_ids:
                return False, "skill_not_allowed"
        else:
            from packages.core.models.permission import Capability, ResourceType
            from packages.core.services.resource_access import (
                ResourceDescriptor,
                user_can_access_resource,
            )

            if not await user_can_access_resource(
                db,
                descriptor=ResourceDescriptor.from_row(skill, ResourceType.SKILL),
                entity_id=entity_id,
                user_id=user_id,
                capability=Capability.VIEW,
            ):
                return False, "skill_not_allowed"

    if _runtime_skills_need_installed_ledger_contracts((skill,)):
        installed = (
            {
                str(contract_id).strip()
                for contract_id in installed_ledger_contract_ids
                if str(contract_id or "").strip()
            }
            if installed_ledger_contract_ids is not None
            else await _runtime_workspace_ledger_contract_ids(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )
        )
        if not runtime_filter_skills_for_installed_ledgers((skill,), installed):
            return False, "skill_workspace_contract_missing"

    return True, ""


async def resolve_skill_descriptors(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    surface: ChatSurface,
    invoke_skill_visible: bool,
    agent_subscription_id: str | None = None,
    profile: RuntimeProfile | None = None,
    allowed_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    user_id: str | None = None,
    enforce_user_access: bool = False,
    limit: int = 8,
) -> list[SkillDescriptor]:
    """Resolve lightweight skill descriptors for a runtime surface.

    The prompt builder still owns rendering today; this resolver provides the
    common model for the next migration step and enforces the "name/description
    first, full instructions on invoke" boundary.
    """

    if not db or not entity_id or not invoke_skill_visible:
        return []
    max_count = max(limit, 0)
    try:
        if agent_id:
            from packages.core.services.skill_service import list_skills_for_agent

            skills = await list_skills_for_agent(
                db,
                entity_id,
                agent_id,
                workspace_id=workspace_id,
                **(
                    {"agent_subscription_id": agent_subscription_id}
                    if agent_subscription_id
                    else {}
                ),
            )
        else:
            from packages.core.services.skill_service import list_skills

            skills = await list_skills(db, entity_id)
        installed_ledger_contract_ids: set[str] | None = None
        if _runtime_skills_need_installed_ledger_contracts(skills):
            installed_ledger_contract_ids = await _runtime_workspace_ledger_contract_ids(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
            )
        readable_skill_ids: set[str] | None = None
        if enforce_user_access:
            from packages.core.models.permission import ResourceType
            from packages.core.services.resource_access import (
                ResourceDescriptor,
                readable_resource_ids,
            )

            entity_skills = [
                skill for skill in skills
                if getattr(skill, "entity_id", None) is not None
            ]
            readable_skill_ids = (
                await readable_resource_ids(
                    db,
                    descriptors=[
                        ResourceDescriptor.from_row(skill, ResourceType.SKILL)
                        for skill in entity_skills
                    ],
                    entity_id=entity_id,
                    user_id=user_id,
                )
                if user_id and entity_skills
                else set()
            )
        eligible_skills: list[Any] = []
        for skill in skills:
            eligible, _reason = await runtime_skill_is_eligible(
                db,
                skill,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                enforce_user_access=enforce_user_access,
                readable_skill_ids=readable_skill_ids,
                installed_ledger_contract_ids=installed_ledger_contract_ids,
            )
            if eligible:
                eligible_skills.append(skill)
        skills = eligible_skills
    except Exception:
        return []

    skills = filter_skills_for_runtime_turn(
        skills,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
    )

    public_customer_surface = surface == ChatSurface.PUBLIC_CUSTOMER_CHAT
    try:
        agent_bound_skill_ids = await runtime_agent_bound_skill_ids(db, agent_id)
    except Exception:
        agent_bound_skill_ids = set()
    if public_customer_surface:
        if not agent_id:
            return []
        skills = [skill for skill in skills if str(getattr(skill, "id", "") or "") in agent_bound_skill_ids]
    profile_allowed_tools = allowed_tools_for_profile(profile) if profile else None
    runtime_allowed_tools = (
        {str(tool_name) for tool_name in allowed_tool_names if str(tool_name or "").strip()}
        if allowed_tool_names is not None
        else None
    )

    required_descriptors: list[SkillDescriptor] = []
    bound_descriptors: list[SkillDescriptor] = []
    ordinary_descriptors: list[SkillDescriptor] = []
    for skill in skills:
        if not runtime_skill_allowed_on_surface(skill, surface):
            continue
        declared_tools = _declared_tool_names(skill)
        visible_declared_tools = _runtime_visible_declared_tools(
            declared_tools,
            profile=profile,
            profile_allowed_tools=profile_allowed_tools,
            runtime_allowed_tools=runtime_allowed_tools,
        )
        if declared_tools and not visible_declared_tools:
            continue
        descriptor = descriptor_from_skill(
            skill,
            source=runtime_skill_source_for_skill(
                skill,
                agent_id=agent_id,
                agent_bound_skill_ids=agent_bound_skill_ids,
            ),
            surface=surface,
            profile=profile,
            visible_declared_tools=tuple(sorted(visible_declared_tools)),
            reason=f"visible on {surface.value}",
        )
        if (
            not manual_skill_selected
            and trusted_skill_invocation_policy(skill) is not None
        ):
            required_descriptors.append(descriptor)
        elif not manual_skill_selected and descriptor.source == "agent_binding":
            # An explicit agent binding is stronger routing intent than a
            # generally visible entity or built-in catalog entry. Keep bound
            # skills inside the prompt budget even when many built-ins share
            # the same creation timestamp and appear first in query order.
            bound_descriptors.append(descriptor)
        elif len(ordinary_descriptors) < max_count:
            ordinary_descriptors.append(descriptor)

    # ``limit`` is a soft prompt-catalog budget, not permission to violate a
    # required-before-answer policy. Required descriptors are always retained;
    # ordinary descriptors fill whatever portion of the budget remains.
    bound_slots = max(max_count - len(required_descriptors), 0)
    selected_bound = bound_descriptors[:bound_slots]
    ordinary_slots = max(
        max_count - len(required_descriptors) - len(selected_bound),
        0,
    )
    return required_descriptors + selected_bound + ordinary_descriptors[:ordinary_slots]


def invoke_skill_visible_for_runtime(
    *,
    tool_names: Iterable[str] | None = None,
    allowed_tool_names: Iterable[str] | None = None,
) -> bool:
    """Return whether this runtime can expose skill descriptors to the model."""

    def _values(names: Iterable[str] | None) -> tuple[str, ...]:
        if not names:
            return ()
        if isinstance(names, str):
            return tuple(part.strip() for part in names.split(",") if part.strip())
        return tuple(str(name) for name in names if str(name or "").strip())

    visible_tools = {
        str(tool_name)
        for tool_name in _values(tool_names) + _values(allowed_tool_names)
        if str(tool_name or "").strip()
    }
    return "invoke_skill" in visible_tools


async def resolve_skill_descriptors_for_envelope(
    db: AsyncSession | None,
    envelope: RuntimeEnvelope | None,
    *,
    invoke_skill_visible: bool | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
    limit: int = 8,
) -> list[SkillDescriptor]:
    """Resolve descriptors from the RuntimeEnvelope instead of entrypoint args.

    This is the runtime-facing skill middleware boundary: entrypoints provide an
    envelope, while skill visibility derives from the envelope's surface,
    principal-bound agent/workspace ids, profile, and resolved tool surface.
    """
    if envelope is None:
        return []
    effective_allowed = (
        {str(tool_name) for tool_name in allowed_tool_names if str(tool_name or "").strip()}
        if allowed_tool_names is not None
        else set(envelope.allowed_tool_names or ())
    )
    visible = (
        invoke_skill_visible
        if invoke_skill_visible is not None
        else invoke_skill_visible_for_runtime(
            tool_names=envelope.tool_names,
            allowed_tool_names=effective_allowed,
        )
    )
    return await resolve_skill_descriptors(
        db,
        entity_id=envelope.entity_id,
        agent_id=envelope.agent_id,
        agent_subscription_id=(
            envelope.metadata.get("agent_subscription_id")
            if isinstance(envelope.metadata, dict)
            else None
        ),
        workspace_id=envelope.workspace_id,
        surface=envelope.surface,
        invoke_skill_visible=visible,
        profile=envelope.profile,
        allowed_tool_names=effective_allowed,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
        user_id=envelope.user_id,
        enforce_user_access=bool(
            manual_skill_selected
            or (envelope.user_id and not envelope.agent_id)
        ),
        limit=limit,
    )


async def populate_runtime_skill_descriptors(
    db: AsyncSession | None,
    ctx,
    envelope: RuntimeEnvelope | None = None,
    *,
    limit: int = 8,
) -> list[SkillDescriptor]:
    """Populate ``ctx.runtime_skill_descriptors`` from the runtime envelope."""
    runtime_envelope = envelope or getattr(ctx, "runtime_envelope", None)
    if runtime_envelope is None:
        setattr(ctx, "runtime_skill_descriptors", [])
        return []
    allowed_tool_names = set(getattr(ctx, "allowed_tool_names", None) or ())
    middleware = RuntimeSkillMiddleware(
        db=db,
        invoke_skill_visible=invoke_skill_visible_for_runtime(
            tool_names=getattr(ctx, "tool_names", None),
            allowed_tool_names=allowed_tool_names,
        ),
        allowed_tool_names=allowed_tool_names,
        active_user_message=getattr(ctx, "active_user_message", None),
        manual_skill_selected=bool(getattr(ctx, "manual_skill_selected", False)),
        limit=limit,
    )
    runtime_envelope = await apply_runtime_middleware(runtime_envelope, (middleware,))
    descriptors = list(middleware.resolved_descriptors)
    setattr(ctx, "runtime_skill_descriptors", descriptors)
    setattr(ctx, "runtime_envelope", runtime_envelope)
    return descriptors


async def runtime_available_skills_section_for_envelope(
    db: AsyncSession | None,
    *,
    envelope: RuntimeEnvelope,
    tools: list[dict],
    allowed_tool_names: Iterable[str] | None,
    entity_id: str | None,
    active_user_message: str | None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    runtime_profile: str | None = None,
    runtime_surface: str | None = None,
    mode: str = "full",
) -> tuple[str | None, RuntimeEnvelope]:
    """Render the runtime-owned Available Skills section for an envelope."""
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.ai.runtime.prompt_sections import available_skills_section
    from packages.core.ai.runtime.prompt_tools import runtime_set_tools_for_prompt_context

    ctx = ChatContext(
        db=db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        runtime_profile=runtime_profile,
        runtime_surface=runtime_surface or envelope.surface.value,
        runtime_profile_name=envelope.profile.value,
        runtime_envelope=envelope,
        active_user_message=active_user_message,
        mode=mode,
    )
    await ctx.resolve()
    runtime_set_tools_for_prompt_context(
        ctx,
        tools=tools,
        allowed_tool_names=(set(allowed_tool_names) if allowed_tool_names is not None else None),
    )
    await populate_runtime_skill_descriptors(db, ctx, envelope)
    return await available_skills_section(ctx), ctx.runtime_envelope
