"""Agent generator — LLM-powered conversational agent creation.

Mirrors the skill generator: a short natural-language request can first be
turned into clarifying questions, then into a fully-formed agent (name,
description, category, tags, and a detailed persona/system_prompt).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.completions import runtime_execute_text_completion
from packages.core.services.skill_bundle import (
    extract_json_object,
    parse_clarifying_questions,
)

logger = logging.getLogger(__name__)

_AGENT_SOURCE = "agent_generator"

AGENT_GENERATION_SYSTEM_PROMPT = """\
You design AI agents for a business automation platform. Given a \
natural-language description, output a single JSON object that fully defines \
the agent.

The JSON **must** contain exactly these keys:
  name          - short human-friendly agent name (e.g. "Support Triage Bot")
  description   - 1-2 sentence summary of what the agent is for
  category      - a short category label (e.g. "Support", "Sales", "Marketing", "Operations")
  tags          - list of short keyword strings
  system_prompt - the agent's full operating instructions / persona (see below)

Rules for system_prompt (the heart of the agent — be thorough, ~120-220 lines):
- Write it as the agent's own operating manual, in the second person
  ("You are ...").
- Use clear markdown sections, in this order:
    ## Role & mission        - who the agent is and the outcome it owns.
    ## Scope                  - what it handles, and explicitly what it does NOT.
    ## Voice & tone           - how it communicates (style, formality, language).
    ## How you work           - step-by-step how it approaches its work, naming
                                which tools/integrations to use when available.
    ## Guardrails             - rules it must never break; when to escalate to a
                                human; actions that need approval; privacy limits.
    ## Edge cases             - ambiguous requests, missing info, failures.
    ## Definition of done     - what a good outcome looks like before it stops.
- Be specific and actionable everywhere; never vague, no placeholder text.

Output **only** the JSON object, no markdown fences, no commentary."""

AGENT_CLARIFY_SYSTEM_PROMPT = """\
You help design AI agents. The user gave a short request for a new agent. \
Before it is built, ask the 1-3 MOST important clarifying questions whose \
answers would materially shape the agent. Focus on:
- Role & scope: what should it own, and what is out of scope?
- Voice & audience: who does it talk to, and in what tone/language?
- Tools & systems: which integrations or data must it use?
- Guardrails: what must it never do; what needs human approval?

Ask only what is genuinely ambiguous — never pad to three. Each question must \
be specific and answerable in a sentence.

Output ONLY the questions, one per line, with no numbering or preamble. If the \
request is already detailed enough to build a strong agent, output exactly: READY"""


AGENT_PATCH_SYSTEM_PROMPT = """\
You are editing an existing AI agent. You will receive the agent's current \
definition as JSON and a requested change. Output a single JSON object with \
**only** the fields that should change — any of: name, description, category, \
tags, system_prompt. Omit unchanged fields entirely.

Rules:
- When you edit system_prompt, output the FULL updated persona (keep the
  existing structure, sections, and voice unless the change asks otherwise).
- Be specific and actionable; never vague, no placeholder text.
- Output **only** the JSON object, no markdown fences, no commentary."""


def _extract_json(raw: str) -> dict:
    """Extract a JSON object from LLM output, tolerating fences and prose.

    Raises ``ValueError`` (not a cryptic ``JSONDecodeError``) when the model
    didn't return a JSON object at all.
    """
    return extract_json_object(raw)


def _normalize_agent_spec(spec: dict[str, Any]) -> dict[str, Any]:
    raw_tags = spec.get("tags")
    tags = [str(x).strip() for x in raw_tags if str(x).strip()] if isinstance(raw_tags, list) else []
    return {
        "name": (str(spec.get("name") or "").strip() or "New Agent"),
        "description": str(spec.get("description") or "").strip(),
        "category": str(spec.get("category") or "").strip(),
        "tags": tags,
        "system_prompt": str(spec.get("system_prompt") or "").strip(),
    }


async def update_agent_via_ai(agent_id: str, change: str, entity_id: str, db: AsyncSession) -> Any:
    """Apply a natural-language change to an existing agent."""
    from packages.core.services.agent_service import get_agent, update_agent

    agent = await get_agent(db, agent_id)
    if not agent or (agent.entity_id and agent.entity_id != entity_id):
        raise ValueError("Agent not found")

    current = json.dumps(
        {
            "name": agent.name,
            "description": agent.description or "",
            "category": agent.category or "",
            "tags": agent.tags or [],
            "system_prompt": agent.system_prompt or "",
        },
        ensure_ascii=False,
        indent=2,
    )
    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": AGENT_PATCH_SYSTEM_PROMPT},
            {"role": "user", "content": f"## Current agent\n\n{current}\n\n## Requested change\n\n{change}"},
        ],
        entity_id=entity_id,
        source=_AGENT_SOURCE,
        temperature=0.3,
        max_tokens=8000,
    )
    raw = completion.content
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response for agent update")
    patch = _extract_json(raw)

    fields: dict[str, Any] = {}
    for key in ("name", "description", "category", "system_prompt"):
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    if isinstance(patch.get("tags"), list):
        fields["tags"] = [str(x).strip() for x in patch["tags"] if str(x).strip()]

    updated = await update_agent(db, agent_id, entity_id, **fields)
    if not updated:
        raise ValueError("Failed to update agent")
    logger.info("AI-updated agent %s for entity %s", agent_id, entity_id)
    return updated


async def agent_clarifying_questions(prompt: str, *, entity_id: str) -> list[str]:
    """Up to 3 clarifying questions for an agent request (empty = ready)."""
    text = (prompt or "").strip()
    if not text:
        return []
    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": AGENT_CLARIFY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Agent request:\n\n{text}"},
        ],
        entity_id=entity_id,
        source=_AGENT_SOURCE,
        temperature=0.3,
        max_tokens=500,
    )
    return parse_clarifying_questions(completion.content or "")


async def generate_agent_streaming(
    prompt: str, entity_id: str, db: AsyncSession,
) -> AsyncIterator[Tuple[str, object]]:
    """Generate an agent, yielding progress as it goes.

    Yields ``("step", label)`` tuples, then a final ``("agent", agent)`` tuple.
    Emitting a step before the (slow) completion keeps the HTTP connection alive
    past Cloudflare's 100s 524 timeout and lets the UI narrate the build.
    """
    from packages.core.services.agent_service import create_agent

    spec: dict[str, Any] | None = None
    async for kind, payload in generate_agent_draft_streaming(prompt, entity_id):
        if kind == "step":
            yield ("step", payload)
        elif kind == "draft":
            spec = payload if isinstance(payload, dict) else None
    if spec is None:
        raise ValueError("Agent generation did not produce a draft")

    yield ("step", "Saving the agent")
    agent = await create_agent(
        db,
        entity_id,
        name=spec["name"],
        description=spec["description"],
        system_prompt=spec["system_prompt"],
        category=spec["category"],
        tags=spec["tags"],
        source="llm-generated",
    )
    logger.info("Generated agent %s (%s) for entity %s", agent.id, agent.name, entity_id)
    yield ("agent", agent)


async def generate_agent_draft_streaming(
    prompt: str, entity_id: str,
) -> AsyncIterator[Tuple[str, object]]:
    """Generate an agent draft without persisting it.

    This powers the AI create review step: the UI can show the generated
    persona and only call ``create_agent`` after the user confirms.
    """
    yield ("step", "Designing the agent")
    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": AGENT_GENERATION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Create an agent for the following:\n\n{prompt}"},
        ],
        entity_id=entity_id,
        source=_AGENT_SOURCE,
        temperature=0.4,
        max_tokens=8000,
    )
    raw = completion.content
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response for agent generation")

    spec = _normalize_agent_spec(_extract_json(raw))
    yield ("step", "Preparing preview")
    yield ("draft", spec)


async def generate_agent(prompt: str, entity_id: str, db: AsyncSession) -> Any:
    """Generate and persist an agent from a natural-language prompt.

    Thin wrapper over :func:`generate_agent_streaming` for callers that only
    want the final agent without progress events.
    """
    agent = None
    async for kind, payload in generate_agent_streaming(prompt, entity_id, db):
        if kind == "agent":
            agent = payload
    if agent is None:
        raise ValueError("Agent generation did not produce an agent")
    return agent
