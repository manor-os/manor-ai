"""Build the Strategist LLM prompt + parse the response into a Proposal.

The shape mirrors what the Planner uses (system prompt = stable rules
+ workspace context blocks; user prompt = the trigger + the JSON
schema). One repair retry on validation failure.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_execute_strategist_completion,
    runtime_strategist_system_prompt,
    runtime_strategist_user_prompt,
    runtime_validation_retry_user_prompt,
)
from packages.core.strategist.context import StrategistContext
from packages.core.strategist.proposal import Proposal

logger = logging.getLogger(__name__)


PLANNER_VERSION = "v0.1-demo-a"

STRATEGIST_PROMPT_SKILL_SLUG = "strategist_system_prompt"
"""If a Skill row with this slug exists for the workspace's entity, its
``system_prompt`` body replaces the hardcoded preamble. The dynamic
parts (services list, JSON schema) are still appended in code so an
edited skill body can't break the contract."""


# ── LLM call orchestration ───────────────────────────────────────────

async def generate_proposal(
    ctx: StrategistContext,
    *,
    review_id: str,
    db: Optional[AsyncSession] = None,
) -> Proposal:
    """Single Claude call → validated Proposal, with one repair retry."""
    override = (ctx.strategist_template or {}).get("system_prompt_override")
    if isinstance(override, str) and override.strip():
        preamble = override
    else:
        preamble = await _load_skill_preamble(db, ctx.workspace.entity_id)
    system_prompt = runtime_strategist_system_prompt(ctx, preamble=preamble)
    user_prompt = runtime_strategist_user_prompt(ctx, review_id=review_id)

    raw = await _safe_runtime_completion(ctx, system_prompt, user_prompt)

    if raw is None:
        # No LLM available — produce a minimal "no new tasks" proposal
        # so downstream code paths stay exercised in CI / dev.
        logger.warning("Strategist: LLM unavailable, returning empty proposal")
        return _fallback_proposal(ctx, review_id=review_id)

    parsed = _parse_proposal(raw, review_id=review_id)
    if parsed is not None:
        return parsed

    repair = runtime_validation_retry_user_prompt(
        user_prompt,
        "Return ONLY a valid JSON Proposal matching the schema above. No prose, no markdown.",
    )
    raw2 = await _safe_runtime_completion(ctx, system_prompt, repair)
    if raw2:
        parsed2 = _parse_proposal(raw2, review_id=review_id)
        if parsed2 is not None:
            return parsed2

    logger.error("Strategist: produced invalid Proposal after one retry")
    return _fallback_proposal(ctx, review_id=review_id)


async def _safe_runtime_completion(
    ctx: StrategistContext,
    system_prompt: str,
    user_prompt: str,
) -> Optional[str]:
    try:
        workspace = getattr(ctx, "workspace", None)
        completion = await runtime_execute_strategist_completion(
            system_prompt,
            user_prompt,
            entity_id=getattr(workspace, "entity_id", None),
            workspace_id=getattr(workspace, "id", None),
        )
        return completion.content
    except Exception as exc:
        logger.warning("Strategist LLM call failed: %s", exc)
        return None


def _parse_proposal(text: str, *, review_id: str) -> Optional[Proposal]:
    if not text:
        return None
    candidate = _strip_code_fence(text).strip()
    try:
        # Inject review_id if the LLM omitted it (very common).
        data = json.loads(candidate)
        if isinstance(data, dict):
            data.setdefault("review_id", review_id)
        return Proposal.model_validate(data)
    except (ValidationError, ValueError) as exc:
        logger.debug("Strategist parse failed: %s", exc)
        return None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ── Prompt building ───────────────────────────────────────────────────


async def _load_skill_preamble(
    db: Optional[AsyncSession], entity_id: Optional[str],
) -> Optional[str]:
    """Look up a per-entity Skill that overrides the system prompt.

    Returns the skill body (with ``{workspace_name}`` available as a
    format token) or None if no row exists / db isn't supplied.
    """
    if db is None or not entity_id:
        return None
    try:
        from packages.core.services.skill_service import get_skill_by_slug
        skill = await get_skill_by_slug(db, STRATEGIST_PROMPT_SKILL_SLUG, entity_id)
    except Exception:
        logger.debug("Strategist skill lookup failed", exc_info=True)
        return None
    if skill and (skill.system_prompt or "").strip():
        return skill.system_prompt
    return None


# ── Fallback ──────────────────────────────────────────────────────────

def _fallback_proposal(ctx: StrategistContext, *, review_id: str) -> Proposal:
    """Empty proposal for dev/CI when no LLM is reachable."""
    return Proposal(
        review_id=review_id,
        summary=(
            f"[fallback] Strategist could not reach the LLM for review "
            f"{review_id}. No tasks proposed."
        ),
        tasks=[],
        notes="LLM unavailable — propose tasks manually or configure a provider API key.",
    )
