"""Skill generator — LLM-powered skill creation and patching."""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_execute_skill_generation_completion,
    runtime_execute_skill_patch_completion,
    runtime_execute_skill_review_completion,
)
from packages.core.models.skill import Skill
from packages.core.services.skill_bundle import assemble_skill_bundle, extract_json_object

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    """Extract a JSON object from LLM output, tolerating fences and prose.

    Raises ``ValueError`` (not a cryptic ``JSONDecodeError``) when the model
    didn't return a JSON object at all.
    """
    return extract_json_object(raw)


def _bump_version(version: str) -> str:
    """Increment the patch component of a semver string."""
    try:
        parts = version.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        return version


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_skill_streaming(
    prompt: str,
    entity_id: str,
    db: AsyncSession,
    *,
    category: Optional[str] = None,
    tags: Optional[list[str]] = None,
    config_overrides: Optional[dict] = None,
) -> AsyncIterator[Tuple[str, object]]:
    """Generate a skill, yielding progress as it goes.

    Yields ``("step", label)`` tuples narrating what the AI is doing, then a
    final ``("skill", skill)`` tuple with the created :class:`Skill`. Surfacing
    progress keeps the HTTP connection alive (avoiding Cloudflare's 100s 524
    timeout) and lets the UI show the build steps like the chat tool trace.
    """
    from packages.core.services.skill_service import create_skill

    # ── Step 1: Draft initial skill spec ──
    yield ("step", "Drafting the skill")
    completion = await runtime_execute_skill_generation_completion(
        prompt,
        entity_id=entity_id,
        category=category,
        tags=tags,
    )
    raw = completion.content
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response for skill generation")

    spec = _extract_json(raw)

    # ── Step 2: Review pass ──
    # One refine round keeps creation responsive — the detailed generation
    # prompt already produces a strong draft, and extra rounds (each a full
    # ~8k-token completion) made "Building…" feel stuck.
    yield ("step", "Reviewing and refining")
    attempt = 0
    for attempt in range(1):
        review = await runtime_execute_skill_review_completion(
            spec,
            entity_id=entity_id,
        )
        test_result = review.content

        if not test_result:
            break

        test_text = test_result.strip()

        # ── Step 3: Check if it passes ──
        if test_text.upper().startswith("PASS"):
            logger.info("Skill '%s' passed review on attempt %d", spec.get("name"), attempt + 1)
            break

        # ── Step 4: Refine based on feedback ──
        # The reviewer may return prose instead of a clean refined spec; that's
        # fine — keep the (already strong) draft rather than aborting the whole
        # generation over an unparseable review.
        try:
            refined = _extract_json(test_text)
        except ValueError:
            refined = None
        if refined and refined.get("system_prompt"):
            logger.info("Skill '%s' refined on attempt %d", spec.get("name"), attempt + 1)
            spec = refined
        else:
            # Couldn't parse refined spec — keep current and move on
            logger.debug("Could not parse refined spec on attempt %d, keeping current", attempt + 1)
            break

    # ── Step 5: Package + save the final skill ──
    # When the spec produced standalone scripts/references, this turns it into a
    # sandbox bundle (SKILL.md + files) that runs through the same executor as
    # builtin skills; otherwise it stays a prompt skill.
    yield ("step", "Packaging and saving")
    base_config = {
        "source": "llm-generated",
        "complexity": spec.get("complexity", "primary"),
        "review_rounds": min(attempt + 1, 3),
    }
    if config_overrides:
        base_config.update(dict(config_overrides))
    tools, config = assemble_skill_bundle(spec, base_config)

    skill = await create_skill(
        db,
        entity_id=entity_id,
        name=spec.get("name", "unnamed-skill"),
        system_prompt=spec.get("system_prompt", prompt),
        slug=spec.get("slug"),
        display_name=spec.get("display_name"),
        description=spec.get("description"),
        tools=tools,
        input_schema=spec.get("input_schema", {}),
        output_format=spec.get("output_format", "text"),
        category=spec.get("category") or category,
        tags=spec.get("tags") or tags or [],
        config=config,
    )

    logger.info("Generated skill %s (%s) for entity %s", skill.id, skill.name, entity_id)
    yield ("skill", skill)


async def generate_skill(
    prompt: str,
    entity_id: str,
    db: AsyncSession,
    *,
    category: Optional[str] = None,
    tags: Optional[list[str]] = None,
    config_overrides: Optional[dict] = None,
) -> Skill:
    """Generate a skill using the draft → review → package flow.

    Thin wrapper over :func:`generate_skill_streaming` for callers that just
    want the final :class:`Skill` without progress events.
    """
    skill: Optional[Skill] = None
    async for kind, payload in generate_skill_streaming(
        prompt,
        entity_id,
        db,
        category=category,
        tags=tags,
        config_overrides=config_overrides,
    ):
        if kind == "skill":
            skill = payload  # type: ignore[assignment]
    if skill is None:
        raise ValueError("Skill generation did not produce a skill")
    return skill


async def update_skill(
    skill_id: str,
    change_description: str,
    entity_id: str,
    db: AsyncSession,
) -> Skill:
    """Patch an existing Skill by describing the desired change in natural language.

    Sends the current skill definition + change description to the LLM,
    which returns a JSON patch of only the affected fields. The version
    is automatically bumped.

    Returns the updated :class:`Skill`.
    """
    from packages.core.services.skill_service import get_skill
    from packages.core.services import skill_service

    existing = await get_skill(db, skill_id)
    if not existing:
        raise ValueError(f"Skill not found: {skill_id}")
    if existing.entity_id != entity_id:
        raise PermissionError(f"Skill {skill_id} does not belong to entity {entity_id}")

    completion = await runtime_execute_skill_patch_completion(
        existing,
        change_description,
        entity_id=entity_id,
    )
    raw = completion.content

    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response for skill update")

    patch = _extract_json(raw)

    # Bump version
    new_version = _bump_version(existing.version or "1.0.0")
    patch["version"] = new_version

    updated = await skill_service.update_skill(
        db, skill_id, entity_id, **patch,
    )

    if not updated:
        raise ValueError(f"Failed to update skill {skill_id}")

    logger.info("Patched skill %s to v%s for entity %s", skill_id, new_version, entity_id)
    return updated
