from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

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
from packages.core.ai.runtime.skill_routing import filter_skills_for_runtime_turn
from packages.core.ai.runtime.skill_routing import (
    external_platform_action_intent,
    is_local_coding_skill,
    local_coding_cli_intent,
    should_route_external_action_to_integration,
    skill_slug_and_name,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.services.skill_bundle import parse_clarifying_questions


SkillSource = Literal["builtin", "entity", "agent_binding", "workspace_operation", "manual"]

RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT = """\
You are an expert skill designer for an AI agent platform. Given a \
natural-language description, produce a single JSON object that fully defines \
a reusable, production-grade skill.

The JSON **must** contain exactly these keys:
  name          - short lowercase-hyphenated identifier (e.g. "weekly-report")
  slug          - same as name (URL-safe, underscores OK)
  display_name  - human-friendly title
  description   - SEE "Rules for description" below
  system_prompt - SEE "Rules for system_prompt" below (the bulk of the skill)
  tools         - list of tool names the skill may call (empty list if none)
  input_schema  - JSON Schema describing the expected user input (use {} if free-text)
  output_format - "text", "json", or "markdown"
  category      - a short category label (e.g. "reporting", "analysis")
  tags          - list of keyword strings
  complexity    - "worker" or "primary". Use "worker" for simple/routine tasks \
(summaries, notifications, data lookups, status updates, reminders, simple reports). \
Use "primary" for complex tasks (multi-step research, creative writing, customer-facing \
communications, analysis requiring judgment, tasks needing many tool calls).

It MAY also include these optional keys (use them when the skill benefits):
  scripts       - object {"filename.py": "<file contents>"} of STANDALONE helper
                  scripts the skill runs. Strongly preferred over inline code for
                  anything non-trivial.
  references    - object {"name.md": "<file contents>"} of longer reference
                  material (style guides, templates, schemas, long examples) that
                  the agent reads ON DEMAND instead of carrying in the prompt.
When either is present the platform packages the skill as a sandbox bundle: the
system_prompt becomes the SKILL.md, the files are mounted next to it, and the
agent reads them with sandbox_read_file and runs scripts with sandbox_exec. Do
NOT list sandbox_* tools yourself — packaging is automatic.

Rules for description (this is what makes the skill get DISCOVERED and chosen):
- Third person, 2-4 sentences. This single field decides whether the agent
  remembers to use the skill, so be explicit.
- State WHAT the skill does AND — crucially — WHEN to use it: the concrete
  situations, the user phrasings, and the trigger keywords that should
  activate it.
- Start with "Use this skill when ...".
  Bad:  "Generates a report."
  Good: "Use this skill when the user asks for a weekly performance report, a
         recap of last week, or 'how did we do this week'. Pulls task and
         revenue metrics, computes deltas vs. the prior week, and produces a
         formatted markdown summary."

Rules for system_prompt (THE most important field — be thorough, never terse):
- This is a complete operating manual for the agent. Aim for 200-300 lines;
  never fewer than ~150. A short prompt is a FAILED skill — real skills are
  detailed and leave no ambiguity about what to do.
- Write it as markdown with these sections, in this order:
    # <Skill Title>
    ## Overview          - what the skill accomplishes and the end result.
    ## When to use       - trigger situations, plus when NOT to use it.
    ## Inputs            - every input, its meaning, and how to handle it when
                           missing or ambiguous (ask the user vs. apply a
                           documented default).
    ## Prerequisites     - data, integrations, permissions, or context the
                           skill assumes are in place first.
    ## Workflow          - the core. 6-15 NUMBERED steps in strict order. Each
                           step MUST specify: the exact action; the exact tool
                           and representative arguments to use; what data to
                           read or compute; the expected intermediate result;
                           and what to do if that step fails. Break complex
                           steps into lettered sub-steps (a, b, c).
    ## Tools             - list each tool the skill uses and exactly how/when
                           to call it, with example arguments.
    ## Worked example    - one realistic end-to-end run: a sample input, the
                           key tool calls it triggers, and the final output.
                           Make it concrete, not abstract.
    ## Edge cases & failure handling - enumerate what goes wrong (empty data,
                           auth/permission errors, rate limits, ambiguous or
                           missing input) and the exact recovery behaviour for
                           each.
    ## Quality bar       - a checklist the output must satisfy before the agent
                           considers the task done.
    ## Output format     - the EXACT structure of every run's final output
                           (headings, fields, ordering). Include a template.
- Be specific and actionable everywhere. Never vague ("analyze the data");
  always concrete ("call manor list_tasks with status=completed for the last
  7 days, group by assignee, and compute each one's completion rate").
- When the skill needs runnable code (data aggregation, computation,
  formatting, report building), PREFER standalone files: put each script in the
  top-level "scripts" object and have the Workflow run it (e.g. "run
  `scripts/build_report.py`"). Scripts may be any length, stdlib only, and must
  document how they read inputs and print outputs. Only fall back to an inline
  ```python ... ``` block for a trivial one-off snippet.
- For long, static reference material (style guides, templates, schemas,
  example outputs), put it in the "references" object and instruct the agent to
  read it on demand (sandbox_read_file) at the step that needs it — do NOT paste
  it inline. This keeps the system_prompt focused (progressive disclosure).

Rules for tools:
- Available tools: bash, read_file, write_file, web_search, web_fetch, \
manor (platform actions), invoke_skill, search_tools.
- Keep the tools list realistic; only include tools the system_prompt actually
  references.

Output **only** the JSON object, no markdown fences, no commentary."""

RUNTIME_SKILL_REVIEW_SYSTEM_PROMPT = (
    "You are a strict skill quality reviewer. Hold skills to a high bar: a "
    "good skill is detailed (200-300 lines), unambiguous, and has a "
    "discovery-ready description. Be specific about every problem you find."
)

RUNTIME_SKILL_PATCH_SYSTEM_PROMPT = """\
You are a skill editor. You will receive the current skill definition as JSON \
and a description of what to change. Output an updated JSON object with **only \
the fields that changed**. Omit unchanged fields entirely.

Rules:
- Preserve the existing style and structure of any text fields you modify.
- If the system_prompt needs editing, include the full updated system_prompt.
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
    skill_id_or_slug: str,
    entity_id: str,
    input_text: str,
    **kwargs: Any,
) -> dict:
    """Invoke a skill through the Runtime skill boundary."""

    from packages.core.services.skill_service import invoke_skill

    return await invoke_skill(
        db,
        skill_id_or_slug,
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


async def runtime_get_skill(db: AsyncSession, skill_id: str) -> Any | None:
    """Load a skill through the Runtime skill lifecycle boundary."""

    from packages.core.services.skill_service import get_skill

    return await get_skill(db, skill_id)


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
    skill: str,
    input_text: str,
    skill_params: Any | None = None,
    runtime_context: Any | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Invoke a skill and format the tool result through Runtime."""

    effective_input_text = str(input_text or "").strip()
    if not effective_input_text:
        effective_input_text = str(getattr(runtime_context, "active_user_message", "") or "").strip()
    effective_input_text = runtime_skill_input_with_params(effective_input_text, skill_params)
    effective_input_text = runtime_input_with_artifact_context(
        effective_input_text,
        runtime_artifact_urls=getattr(runtime_context, "runtime_artifact_urls", None),
        dependency_artifact_urls=getattr(runtime_context, "dependency_artifact_urls", None),
    )

    skip_result = runtime_external_action_skill_skip_result(
        active_user_message=getattr(runtime_context, "active_user_message", None),
        skill=skill,
        manual_skill_selected=bool(getattr(runtime_context, "manual_skill_selected", False)),
    )
    if skip_result is not None:
        return skip_result

    from packages.core.database import async_session

    async with async_session() as db:
        result = await runtime_invoke_skill(
            db,
            skill,
            entity_id,
            effective_input_text,
            agent_id=getattr(runtime_context, "agent_id", None),
            enforce_agent_access=not bool(getattr(runtime_context, "manual_skill_selected", False)),
            user_id=user_id or None,
            workspace_id=getattr(runtime_context, "workspace_id", None),
            conversation_id=conversation_id or getattr(runtime_context, "conversation_id", None),
            task_id=getattr(runtime_context, "task_id", None),
            manual_skill_selected=bool(getattr(runtime_context, "manual_skill_selected", False)),
            legacy_tool_profile=getattr(runtime_context, "legacy_tool_profile", None),
            allowed_tool_names=getattr(runtime_context, "allowed_tool_names", None),
            runtime_envelope=getattr(runtime_context, "runtime_envelope", None),
            metadata=runtime_skill_invocation_metadata(runtime_context),
            model=getattr(runtime_context, "llm_model", None),
        )
    return runtime_format_invoke_skill_result(skill, result)


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
    skill_id: str,
    tool_kwargs: dict[str, Any] | None = None,
) -> str:
    """Return skill discovery details through Runtime without leaking hidden prompts."""

    from packages.core.database import async_session

    async with async_session() as db:
        runtime_descriptors = await runtime_skill_descriptors_from_tool_kwargs(db, tool_kwargs or {})
        if runtime_descriptors is not None:
            key = str(skill_id or "").strip().lower()
            for descriptor in runtime_descriptors:
                if runtime_skill_descriptor_matches(descriptor, key):
                    return runtime_format_skill_descriptor_detail(descriptor)
            return f"Skill '{skill_id}' is not visible in this runtime."
        skill = await runtime_get_skill(db, skill_id)

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
        {"role": "system", "content": RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT},
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
        # Detailed skills run 200-300 lines of system_prompt plus the other
        # JSON fields; a low cap silently truncates them mid-spec.
        max_tokens=8000,
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

    return (
        f"You are testing a skill before it goes live.\n\n"
        f"Skill name: {spec.get('name', 'unknown')}\n"
        f"System prompt:\n{spec.get('system_prompt', '')}\n\n"
        f"Tools available: {spec.get('tools', [])}\n"
        f"Input schema: {json.dumps(spec.get('input_schema', {}))}\n\n"
        f"Simulate running this skill with a realistic sample input. Then evaluate:\n"
        f"1. Are the steps clear and actionable? Would an agent know exactly what to do?\n"
        f"2. Are the right tools listed? Any missing or unnecessary?\n"
        f"3. Is the output format well-defined (with a concrete template)?\n"
        f"4. Are there edge cases that would break the skill? Are they handled?\n"
        f"5. Is the system_prompt specific enough (not vague/generic)?\n"
        f"6. Is the system_prompt detailed enough? It should be ~200-300 lines "
        f"and contain the Overview / When to use / Inputs / Workflow / Tools / "
        f"Worked example / Edge cases / Quality bar / Output format sections. "
        f"A short or skeletal prompt FAILS — it must be expanded.\n"
        f"7. Does the description say BOTH what the skill does AND when to use "
        f"it (trigger situations / keywords)? A bare one-line summary FAILS.\n\n"
        f"If the skill passes all checks, respond with exactly: PASS\n"
        f"If it needs changes, respond with: FAIL followed by a JSON object "
        f"with the COMPLETE corrected skill spec (same keys as before). When "
        f"the problem is thinness, return a fully expanded system_prompt — do "
        f"not just describe what to add."
    )


def runtime_skill_review_messages(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Build Runtime one-shot messages for skill quality review."""

    return [
        {"role": "system", "content": RUNTIME_SKILL_REVIEW_SYSTEM_PROMPT},
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
        # On FAIL the reviewer returns the COMPLETE corrected spec, including a
        # fully expanded system_prompt, so it needs the same headroom.
        max_tokens=8000,
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
        {"role": "system", "content": RUNTIME_SKILL_PATCH_SYSTEM_PROMPT},
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
            ],
            "statuses": ["running", "queued", "pending"],
            "json_equals": {"tool": ["codex_cli", "claude_code"]},
            "stop_reason": "local_coding_dispatched",
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
    if external_platform_action_intent(active_user_message):
        return _runtime_available_skills_section(
            "Skills are intentionally omitted for this turn because the latest "
            "request targets an external platform action. Use `search_tools` "
            "for the relevant Integration/MCP tool instead."
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

    # Intent-scoped narrowing. When a turn's intent lands squarely in a local
    # coding capability domain, we focus the catalog on that umbrella skill.
    # Per-MCP guidance packs (``mcp_*``) remain visible when connected so the
    # model can still consult the selected MCP's pack.
    if local_coding_cli_intent(active_user_message):
        filtered = [
            skill
            for skill in items
            if is_local_coding_skill(*skill_slug_and_name(skill)) or _is_mcp_guidance_pack(skill)
        ]
        if not filtered:
            return [], _runtime_available_skills_section(
                "Skills are intentionally omitted for this turn because no "
                "local coding operations skill is available. Use "
                "`search_tools` for `mcp__codex_cli__check_path`/`run` or "
                "`mcp__claude_code__check_path`/`run` instead."
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
            f"- [{descriptor.slug or descriptor.name}] [{source_label}] "
            f"{descriptor.name or descriptor.slug} - "
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
    skill_key: str,
) -> dict[str, Any]:
    key = str(skill_key or "").strip().lower()
    for descriptor in descriptors or ():
        if runtime_skill_descriptor_matches(descriptor, key):
            payload = runtime_skill_descriptor_dict(descriptor)
            payload["runtime_scoped"] = True
            payload["instructions"] = (
                "Full skill instructions are not exposed by discovery; call invoke_skill to run this skill."
            )
            return payload
    return {
        "error": "Skill not visible in this runtime",
        "skill_id": key,
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
    if isinstance(runtime, dict) and runtime:
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
    """A per-MCP built-in guidance pack uses the ``mcp_<server_key>`` slug convention."""
    slug, _name = skill_slug_and_name(skill)
    return str(slug or "").startswith("mcp_")


def _pack_declared_tool_names(skill) -> tuple[str, ...]:
    """Declared tools for either a DB ``Skill`` (``tools``) or a runtime
    ``SkillDescriptor`` (``declared_tools``)."""
    raw = getattr(skill, "declared_tools", None) or getattr(skill, "tools", None) or ()
    return tuple(str(name) for name in raw if str(name or "").strip())


def _mcp_pack_tools_available(skill, available_prefixes: set[str]) -> bool:
    """True when the pack's MCP is connectable this turn (its tools are available).

    A pack declares its MCP via ``mcp__<server>__*`` tools in ``config.json``.
    If it declares none, it is not gated (shown). Otherwise it is shown only
    when at least one of its servers appears in the available tool surface.
    """
    declared = _mcp_server_prefixes(_pack_declared_tool_names(skill))
    if not declared:
        return True
    return bool(declared & available_prefixes)


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
    omitted = runtime_available_skills_omission_section(
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
    )
    if omitted:
        return omitted

    filtered, empty_message = _filter_skills_for_prompt(
        skills,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
    )
    if empty_message:
        return empty_message

    if available_tool_names is not None and not manual_skill_selected:
        available_prefixes = _mcp_server_prefixes(available_tool_names)
        filtered = [
            skill
            for skill in filtered
            if not _is_mcp_guidance_pack(skill) or _mcp_pack_tools_available(skill, available_prefixes)
        ]

    if not filtered:
        return None

    loaded = {str(name) for name in (loaded_tool_names or ()) if str(name or "").strip()}
    lines = ["## Available Skills"]
    if "invoke_skill" in loaded:
        lines.append("Use `invoke_skill(skill=<slug>, input=<instructions>)` to run these:")
    else:
        lines.append(
            "`invoke_skill` is available in this runtime but its schema may be deferred. "
            "Call `search_tools` for `invoke_skill` first if needed, then run one of these skills:"
        )
    for skill in filtered:
        slug, _name = skill_slug_and_name(skill)
        desc = str(getattr(skill, "description", "") or "")
        short = desc.split(".")[0].strip()
        if len(short) > 120:
            short = short[:117] + "..."
        source_label = _runtime_skill_source_label(skill)
        lines.append(f"- **{slug}** [{source_label}]: {short}")
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
    tools: list[dict[str, Any]]
    allowed_tool_names: frozenset[str] | None = None
    harness: RuntimeHarness | None = None


def _declared_tool_names(skill) -> tuple[str, ...]:
    return tuple(str(tool_name) for tool_name in (getattr(skill, "tools", None) or ()) if str(tool_name or "").strip())


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
    allowed = _runtime_allowed_tool_name_set(allowed_tool_names)
    declared = {str(tool_name) for tool_name in declared_tool_names if str(tool_name or "").strip()}
    if not declared:
        return allowed
    profile = getattr(runtime_envelope, "profile", None)
    if profile in {
        RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
    }:
        return allowed
    expanded = set(allowed or ())
    expanded.update(declared)
    return frozenset(expanded)


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
) -> RuntimeEnvelope | None:
    if runtime_envelope is None:
        return None
    effective_allowed = set(str(tool_name) for tool_name in (allowed_tool_names or ()) if str(tool_name or "").strip())
    if not effective_allowed:
        return runtime_envelope
    effective_tools = set(runtime_envelope.tool_names or ())
    effective_tools.update(skill_tool_names)
    return replace(
        runtime_envelope,
        tool_names=tuple(sorted(effective_tools)),
        allowed_tool_names=tuple(sorted(effective_allowed)),
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
) -> RuntimePromptSkillToolSurface:
    """Prepare the schema-visible tool surface for a prompt skill.

    Internal prompt skill invocations inherit the parent runtime policy scope
    and add the skill's declared tools as the child skill's visible tool surface.
    External customer/channel profiles keep the existing runtime allowlist.
    """

    declared = _declared_tool_names(skill)
    allowed = _prompt_skill_effective_allowed_tools(
        declared,
        allowed_tool_names,
        runtime_envelope,
    )
    skill_tool_names = tuple(tool_name for tool_name in declared if allowed is None or tool_name in allowed)
    if get_schemas_for_names is None:
        from packages.core.ai.runtime.tool_registry import runtime_tool_schemas_for_names

        get_schemas_for_names = runtime_tool_schemas_for_names
    skill_envelope = _prompt_skill_runtime_envelope(
        runtime_envelope,
        allowed_tool_names=allowed,
        skill_tool_names=skill_tool_names,
    )
    return RuntimePromptSkillToolSurface(
        declared_tool_names=declared,
        skill_tool_names=skill_tool_names,
        tools=list(get_schemas_for_names(list(skill_tool_names)) or []),
        allowed_tool_names=allowed,
        harness=RuntimeHarness(skill_envelope) if skill_envelope is not None else None,
    )


def runtime_prompt_skill_tool_schema_resolver(
    *,
    declared_tool_names: Iterable[str],
    allowed_tool_names: Iterable[str] | None = None,
    get_schema: Callable[[str], dict[str, Any] | None] | None = None,
) -> Callable[[str], dict[str, Any] | None]:
    """Build a resolver for skill-declared tools plus parent-visible MCP tools."""

    if get_schema is None:
        from packages.core.ai.runtime.tool_registry import runtime_tool_schema

        get_schema = runtime_tool_schema
    declared = {str(tool_name) for tool_name in declared_tool_names if str(tool_name or "").strip()}
    allowed = _runtime_allowed_tool_name_set(allowed_tool_names)

    def _resolver(name: str) -> dict[str, Any] | None:
        tool_name = str(name or "").strip()
        if not tool_name:
            return None
        if allowed is not None and tool_name not in allowed:
            return None
        if tool_name.startswith("mcp__") or tool_name in declared:
            return get_schema(tool_name)
        return None

    return _resolver


# Document/office skills assemble decks, docs, and sheets — they legitimately
# generate images, but they have no business producing video or audio. Left
# unguarded, a stuck pptx/docx run can flail into generate_file(kind="video")
# with a prompt bled from earlier in the conversation, producing a deck request
# that ends in an unrelated clip. Deny those media kinds outright for these
# skills (slug or built-in alias).
_DOCUMENT_SKILL_SLUGS = frozenset(
    {
        "pptx",
        "presentation",
        "docx",
        "word_document",
        "doc",
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
                "deck, stop and report what is missing — do not switch to an "
                "unrelated media artifact."
            )
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
    legacy_tool_profile: str | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    runtime_envelope: RuntimeEnvelope | None = None,
    skill_slug: str | None = None,
) -> Callable[[str, Any], Awaitable[str]]:
    """Build the registered-tool executor used inside prompt-skill runs."""

    async def _execute(tool_name: str, args: Any) -> str:
        guard = document_skill_media_guard(skill_slug, tool_name, args)
        if guard is not None:
            return guard

        from packages.core.ai.runtime.tool_registry import runtime_execute_tool

        return await runtime_execute_tool(
            tool_name,
            args,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            active_user_message=active_user_message,
            manual_skill_selected=manual_skill_selected,
            legacy_tool_profile=legacy_tool_profile,
            allowed_tool_names=allowed_tool_names,
            runtime_envelope=runtime_envelope,
        )

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
        metadata={
            "category": str(getattr(skill, "category", "") or ""),
            "output_format": str(getattr(skill, "output_format", "") or ""),
        },
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


async def resolve_skill_descriptors(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    surface: ChatSurface,
    invoke_skill_visible: bool,
    profile: RuntimeProfile | None = None,
    allowed_tool_names: set[str] | None = None,
    active_user_message: str | None = None,
    manual_skill_selected: bool = False,
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
    if max_count == 0:
        return []
    try:
        if agent_id:
            from packages.core.services.skill_service import list_skills_for_agent

            skills = await list_skills_for_agent(
                db,
                entity_id,
                agent_id,
                workspace_id=workspace_id,
            )
        else:
            from packages.core.services.skill_service import list_skills

            skills = await list_skills(db, entity_id)
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

    descriptors: list[SkillDescriptor] = []
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
        descriptors.append(
            descriptor_from_skill(
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
        )
        if len(descriptors) >= max_count:
            break
    return descriptors


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
        workspace_id=envelope.workspace_id,
        surface=envelope.surface,
        invoke_skill_visible=visible,
        profile=envelope.profile,
        allowed_tool_names=effective_allowed,
        active_user_message=active_user_message,
        manual_skill_selected=manual_skill_selected,
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
