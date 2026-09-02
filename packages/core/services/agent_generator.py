"""Agent generator — LLM-powered conversational agent creation.

Mirrors the skill generator: a short natural-language request can first be
turned into clarifying questions, then into a fully-formed agent (name,
description, category, tags, and a detailed persona/system_prompt).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, AsyncIterator, Coroutine, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.completions import runtime_execute_text_completion
from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.services.agent_capability_catalog import (
    AgentCapabilitySelectionError,
)
from packages.core.services.skill_bundle import (
    extract_json_object,
    parse_clarifying_questions,
)

logger = logging.getLogger(__name__)

_AGENT_SOURCE = "agent_generator"

_CAPABILITY_SCAN_MAX_ITEMS = 120
_CAPABILITY_SCAN_MAX_CHARS = 48_000
_CAPABILITY_SCAN_MAX_CHUNKS = 12
# Capability selection favors deterministic completion over burst latency.
# A single request may span many catalog chunks. Keep one provider call active
# per Factory plan; failed batches also cancel and drain all queued siblings.
_CAPABILITY_SCAN_CONCURRENCY = 1
_CAPABILITY_REVIEW_MAX_ITEMS = AGENT_CAPABILITY_SELECTION_LIMIT * 2
_CAPABILITY_REVIEW_MAX_CHARS = 96_000
_CAPABILITY_REDUCTION_SELECTION_LIMIT = AGENT_CAPABILITY_SELECTION_LIMIT
_CAPABILITY_COMPLETION_MAX_ATTEMPTS = 2
_CAPABILITY_PROVIDER_CALL_BUDGET = 48


class AgentCapabilitySelectionPhase(StrEnum):
    SCAN = "semantic_scan"
    REDUCTION = "least_privilege_reduction"
    FINAL_REVIEW = "final_review"


@dataclass
class AgentCapabilitySelectionBudget:
    max_provider_calls: int
    scan_concurrency: int
    provider_calls: int = 0

    def reserve(self, phase: AgentCapabilitySelectionPhase) -> None:
        if self.provider_calls >= self.max_provider_calls:
            raise AgentCapabilitySelectionError(
                "capability selection exceeded its bounded provider-call budget "
                f"during {phase.value}"
            )
        self.provider_calls += 1


class AgentCapabilitySelectionPolicyFactory:
    """Build one hard-bounded semantic-selection policy per Agent request."""

    @staticmethod
    def create(*, scan_chunk_count: int) -> AgentCapabilitySelectionBudget:
        if scan_chunk_count > _CAPABILITY_SCAN_MAX_CHUNKS:
            raise AgentCapabilitySelectionError(
                "capability catalog exceeds the bounded semantic scan capacity"
            )
        return AgentCapabilitySelectionBudget(
            max_provider_calls=_CAPABILITY_PROVIDER_CALL_BUDGET,
            scan_concurrency=max(1, _CAPABILITY_SCAN_CONCURRENCY),
        )


async def _gather_capability_batch(
    *operations: Coroutine[Any, Any, list[str]],
) -> list[list[str]]:
    """A failed or cancelled selection cannot leave provider work behind."""
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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

AGENT_CAPABILITY_SCAN_SYSTEM_PROMPT = f"""\
You retrieve plausible runtime capabilities for one AI agent from one chunk of
an actor-scoped catalog.
This is a semantic classification task, not keyword matching. Optimize this phase
for high recall. Every catalog chunk is scanned separately before a
final least-privilege review.

Return exactly one JSON object with this shape:
{{"capability_ids": ["exact catalog id", "..."]}}

Rules:
- Infer the actions the agent may genuinely need from the complete request and
  generated agent summary. Include plausible candidates from this chunk so the
  final reviewer can compare overlapping Skills, BusinessCapabilities, tools,
  and MCP actions.
- Copy ids exactly from the supplied catalog. Never invent, shorten, translate,
  or rewrite an id. Return [] when this chunk contains no relevant candidate.
- Include both a focused Skill or BusinessCapability and a plausible exact
  low-level action when the final reviewer needs to compare their coverage.
- An MCP candidate without an action suffix represents the complete server and
  grants all of its listed actions. Select it when the role needs that MCP;
  execution-time instructions and approvals still govern which actions it uses.
- Never include action-level write, delete, send, purchase, or publish
  candidates unless the request genuinely requires that effect.
- Respect explicit exclusions and guardrails more strongly than general role
  descriptions.
- setup_required is a valid selection that records connection intent; it is not
  a reason to substitute an unrelated capability.
- Return at most {AGENT_CAPABILITY_SELECTION_LIMIT} ids.

Output only JSON. No prose or markdown fences."""

_AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT_TEMPLATE = """\
You perform the final least-privilege review for one AI agent. The supplied
shortlist was produced by semantic scans over every chunk of the actor-scoped
capability catalog; this is semantic classification, not keyword matching.

Return exactly one JSON object with this shape:
{"capability_ids": ["exact shortlist id", "..."]}

Rules:
- Infer the actions the agent must perform from the complete request and the
  generated agent summary. Select the smallest sufficient capability set.
- Copy ids exactly from the supplied shortlist. Never invent, shorten,
  translate, rewrite, or restore an id that is not in the shortlist.
- Prefer one focused Skill or BusinessCapability over many low-level tools when
  it covers the requested workflow.
- Treat Skills or MCP providers that perform the same role as alternatives.
  Select one ready option; if none are ready, retain only the single best setup
  intent. Select multiple overlapping alternatives only when the request
  explicitly requires multiple providers or accounts.
- For action-level MCP candidates, select only the exact actions the role needs.
  An MCP candidate without an action suffix represents the complete server;
  select it when the role needs that integration, even if it will use only a
  subset of the listed actions.
- Remove speculative candidates that are not required by the request.
- Respect explicit exclusions and guardrails more strongly than general role
  descriptions.
- setup_required is a valid selection that records connection intent; it is not
  a reason to substitute an unrelated capability.
- Return at most __CAPABILITY_LIMIT__ ids.

Output only JSON. No prose or markdown fences."""
# This is agent prompt prose, never submitted to a database.
AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT = _AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT_TEMPLATE.replace(  # nosec B608
    "__CAPABILITY_LIMIT__", str(AGENT_CAPABILITY_SELECTION_LIMIT),
)

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


def _normalize_agent_spec(
    spec: dict[str, Any],
    *,
    capability_catalog=None,
) -> dict[str, Any]:
    raw_tags = spec.get("tags")
    tags = [str(x).strip() for x in raw_tags if str(x).strip()] if isinstance(raw_tags, list) else []
    raw_capability_ids = spec.get("capability_ids")
    capability_ids = (
        [str(value).strip() for value in raw_capability_ids if str(value).strip()]
        if isinstance(raw_capability_ids, list)
        else []
    )
    plan = capability_catalog.resolve(capability_ids) if capability_catalog is not None else None
    return {
        "name": (str(spec.get("name") or "").strip() or "New Agent"),
        "description": str(spec.get("description") or "").strip(),
        "category": str(spec.get("category") or "").strip(),
        "tags": tags,
        "system_prompt": str(spec.get("system_prompt") or "").strip(),
        "capability_ids": (
            list(plan.selected_catalog_ids) if plan is not None else []
        ),
        "capability_plan_status": (
            plan.status.value if plan is not None else "ready"
        ),
        "capability_setup_required": (
            list(plan.setup_required) if plan is not None else []
        ),
    }


async def _match_agent_capabilities(
    *,
    prompt: str,
    spec: dict[str, Any],
    entity_id: str,
    capability_catalog,
):
    """Scan the complete catalog in bounded rounds, then resolve exact ids.

    Keeping this short structured decision separate from the long persona
    generation prevents ``capability_ids`` from being omitted when a verbose
    system prompt approaches the generation token limit. Chunk scans improve
    recall without trusting model-created names; the final review can only
    choose exact ids from the actor-scoped shortlist.
    """
    payload = capability_catalog.prompt_payload()
    if not payload:
        return capability_catalog.resolve([])
    chunks = _chunk_capability_payload(payload)
    budget = AgentCapabilitySelectionPolicyFactory.create(
        scan_chunk_count=len(chunks)
    )

    request_context = _agent_capability_request_context(prompt=prompt, spec=spec)
    semaphore = asyncio.Semaphore(budget.scan_concurrency)

    async def scan_chunk(index: int, chunk: list[dict[str, Any]]) -> list[str]:
        allowed_ids = {
            str(item.get("id") or "").strip()
            for item in chunk
            if str(item.get("id") or "").strip()
        }
        messages = [
            {"role": "system", "content": AGENT_CAPABILITY_SCAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{request_context}\n\n"
                    f"## Semantic scan chunk {index + 1} of {len(chunks)}\n\n"
                    f"{_compact_json(chunk)}"
                ),
            },
        ]
        async with semaphore:
            return await _complete_capability_ids(
                messages=messages,
                entity_id=entity_id,
                allowed_ids=allowed_ids,
                phase=f"semantic scan chunk {index + 1}",
                phase_kind=AgentCapabilitySelectionPhase.SCAN,
                budget=budget,
            )

    scan_results = await _gather_capability_batch(*(
        scan_chunk(index, chunk) for index, chunk in enumerate(chunks)
    ))
    recalled_ids = {
        catalog_id
        for chunk_ids in scan_results
        for catalog_id in chunk_ids
    }
    shortlist = [
        item for item in payload if str(item.get("id") or "") in recalled_ids
    ]
    if not shortlist:
        logger.info(
            "AI Agent semantic capability scan chunks=%s catalog=%s shortlist=0",
            len(chunks),
            len(payload),
        )
        return capability_catalog.resolve([])

    shortlist = await _reduce_capability_shortlist(
        shortlist=shortlist,
        request_context=request_context,
        entity_id=entity_id,
        semaphore=semaphore,
        budget=budget,
    )
    shortlist_ids = _capability_ids(shortlist)
    final_ids = await _complete_capability_ids(
        messages=[
            {"role": "system", "content": AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{request_context}\n\n"
                    "## Semantically recalled capability shortlist\n\n"
                    f"{_compact_json(shortlist)}"
                ),
            },
        ],
        entity_id=entity_id,
        allowed_ids=shortlist_ids,
        phase="final capability review",
        phase_kind=AgentCapabilitySelectionPhase.FINAL_REVIEW,
        budget=budget,
    )
    plan = capability_catalog.resolve(final_ids)
    logger.info(
        "AI Agent semantic capability match chunks=%s catalog=%s shortlist=%s "
        "status=%s selected=%s",
        len(chunks),
        len(payload),
        len(shortlist),
        plan.status.value,
        list(plan.selected_catalog_ids),
    )
    return plan


async def match_agent_capabilities(
    *,
    prompt: str,
    spec: dict[str, Any],
    entity_id: str,
    capability_catalog,
):
    """Public semantic matcher shared by Agent and Workspace creation."""

    return await _match_agent_capabilities(
        prompt=prompt,
        spec=spec,
        entity_id=entity_id,
        capability_catalog=capability_catalog,
    )


def _agent_capability_request_context(*, prompt: str, spec: dict[str, Any]) -> str:
    return (
        "## Complete agent request\n\n"
        f"{prompt}\n\n"
        "## Generated agent summary\n\n"
        f"Name: {str(spec.get('name') or '').strip()}\n"
        f"Description: {str(spec.get('description') or '').strip()}\n"
        f"Category: {str(spec.get('category') or '').strip()}"
    )


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _chunk_capability_payload(
    payload: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    max_chars: int | None = None,
) -> list[list[dict[str, Any]]]:
    """Split the complete catalog without dropping or duplicating candidates."""

    item_limit = max_items or _CAPABILITY_SCAN_MAX_ITEMS
    char_limit = max_chars or _CAPABILITY_SCAN_MAX_CHARS

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 2
    for item in payload:
        item_chars = len(_compact_json(item)) + (1 if current else 0)
        if current and (
            len(current) >= item_limit
            or current_chars + item_chars > char_limit
        ):
            chunks.append(current)
            current = []
            current_chars = 2
            item_chars = len(_compact_json(item))
        current.append(item)
        current_chars += item_chars
    if current:
        chunks.append(current)
    return chunks


def _capability_ids(payload: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("id") or "").strip()
        for item in payload
        if str(item.get("id") or "").strip()
    }


async def _reduce_capability_shortlist(
    *,
    shortlist: list[dict[str, Any]],
    request_context: str,
    entity_id: str,
    semaphore: asyncio.Semaphore,
    budget: AgentCapabilitySelectionBudget,
) -> list[dict[str, Any]]:
    """Hierarchically reduce a recalled catalog without one unbounded prompt."""

    round_number = 0
    current = shortlist
    while True:
        chunks = _chunk_capability_payload(
            current,
            max_items=_CAPABILITY_REVIEW_MAX_ITEMS,
            max_chars=_CAPABILITY_REVIEW_MAX_CHARS,
        )
        if len(chunks) <= 1:
            return current
        round_number += 1

        async def reduce_chunk(index: int, chunk: list[dict[str, Any]]) -> list[str]:
            messages = [
                {"role": "system", "content": AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"{request_context}\n\n"
                        f"## Least-privilege reduction round {round_number}, "
                        f"chunk {index + 1} of {len(chunks)}\n\n"
                        f"Return at most {_CAPABILITY_REDUCTION_SELECTION_LIMIT} ids "
                        "from this chunk.\n\n"
                        f"{_compact_json(chunk)}"
                    ),
                },
            ]
            async with semaphore:
                return await _complete_capability_ids(
                    messages=messages,
                    entity_id=entity_id,
                    allowed_ids=_capability_ids(chunk),
                    phase=f"capability reduction round {round_number} chunk {index + 1}",
                    phase_kind=AgentCapabilitySelectionPhase.REDUCTION,
                    budget=budget,
                    selection_limit=_CAPABILITY_REDUCTION_SELECTION_LIMIT,
                )

        selected_by_chunk = await _gather_capability_batch(*(
            reduce_chunk(index, chunk) for index, chunk in enumerate(chunks)
        ))
        selected_ids = {
            catalog_id
            for chunk_ids in selected_by_chunk
            for catalog_id in chunk_ids
        }
        next_shortlist = [
            item for item in current if str(item.get("id") or "") in selected_ids
        ]
        if not next_shortlist:
            return []
        if len(next_shortlist) >= len(current):
            raise AgentCapabilitySelectionError(
                "capability shortlist could not be reduced within the bounded review prompt"
            )
        current = next_shortlist


async def _complete_capability_ids(
    *,
    messages: list[dict[str, str]],
    entity_id: str,
    allowed_ids: set[str],
    phase: str,
    phase_kind: AgentCapabilitySelectionPhase,
    budget: AgentCapabilitySelectionBudget,
    selection_limit: int = AGENT_CAPABILITY_SELECTION_LIMIT,
) -> list[str]:
    """Complete and validate one bounded semantic selection, retrying once."""

    last_error: Exception | None = None
    for attempt in range(_CAPABILITY_COMPLETION_MAX_ATTEMPTS):
        attempt_messages = list(messages)
        if last_error is not None:
            attempt_messages.append({
                "role": "user",
                "content": (
                    f"Your previous {phase} response was invalid: {last_error}. "
                    "Retry once. Return only the required JSON object and copy "
                    "ids exactly from the supplied candidates."
                ),
            })
        budget.reserve(phase_kind)
        completion = await runtime_execute_text_completion(
            attempt_messages,
            entity_id=entity_id,
            source=_AGENT_SOURCE,
            temperature=0.1,
            # Exact MCP ids can be long. Leave enough output room for the
            # catalog's supported selection maximum.
            max_tokens=8000,
        )
        try:
            raw = completion.content
            if not raw or not raw.strip():
                raise ValueError("LLM returned empty capability selection")
            selection = _extract_json(raw)
            raw_ids = selection.get("capability_ids")
            if not isinstance(raw_ids, list):
                raise ValueError(
                    "LLM capability selection must contain capability_ids as a list"
                )
            selected_ids: list[str] = []
            for value in raw_ids:
                catalog_id = str(value or "").strip()
                if catalog_id and catalog_id not in selected_ids:
                    selected_ids.append(catalog_id)
            if len(selected_ids) > selection_limit:
                raise AgentCapabilitySelectionError(
                    f"{phase} selected more than {selection_limit} capability ids"
                )
            unknown = [
                catalog_id for catalog_id in selected_ids
                if catalog_id not in allowed_ids
            ]
            if unknown:
                raise AgentCapabilitySelectionError(
                    f"{phase} returned unknown or inaccessible capability ids: "
                    + ", ".join(unknown)
                )
            return selected_ids
        except (ValueError, TypeError) as exc:
            last_error = exc
            if attempt + 1 >= _CAPABILITY_COMPLETION_MAX_ATTEMPTS:
                raise
            logger.warning(
                "AI Agent %s invalid response; retrying once: %s",
                phase,
                exc,
            )
    if last_error is None:
        raise RuntimeError("agent generation failed without a provider error")
    raise last_error


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
    prompt: str,
    entity_id: str,
    db: AsyncSession,
    *,
    user_id: str = "",
) -> AsyncIterator[Tuple[str, object]]:
    """Generate an agent, yielding progress as it goes.

    Yields ``("step", label)`` tuples, then a final ``("agent", agent)`` tuple.
    Emitting a step before the (slow) completion keeps the HTTP connection alive
    past Cloudflare's 100s 524 timeout and lets the UI narrate the build.
    """
    from packages.core.services.agent_service import create_agent

    spec: dict[str, Any] | None = None
    async for kind, payload in generate_agent_draft_streaming(
        prompt,
        entity_id,
        db=db,
        user_id=user_id,
    ):
        if kind == "step":
            yield ("step", payload)
        elif kind == "draft":
            spec = payload if isinstance(payload, dict) else None
    if spec is None:
        raise ValueError("Agent generation did not produce a draft")

    yield ("step", "Saving the agent")
    config: dict[str, Any] = {}
    selected_capability_ids = list(spec.get("capability_ids") or [])
    if selected_capability_ids:
        config["capability_selection"] = {
            "version": 1,
            "catalog_ids": selected_capability_ids,
            "source": "agent_ai",
        }
    agent = await create_agent(
        db,
        entity_id,
        name=spec["name"],
        description=spec["description"],
        system_prompt=spec["system_prompt"],
        category=spec["category"],
        tags=spec["tags"],
        config=config,
        source="llm-generated",
        owner_user_id=user_id or None,
    )
    if selected_capability_ids:
        from packages.core.services.agent_capability_catalog import (
            AgentCapabilityCatalogFactory,
            materialize_agent_capability_plan,
        )

        catalog = await AgentCapabilityCatalogFactory.create(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
        plan = catalog.resolve(selected_capability_ids)
        await materialize_agent_capability_plan(
            db,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent.id,
            plan=plan,
        )
        logger.info(
            "AI Agent capability plan agent=%s status=%s selected=%s",
            agent.id,
            plan.status.value,
            list(plan.selected_catalog_ids),
        )
    logger.info("Generated agent %s (%s) for entity %s", agent.id, agent.name, entity_id)
    yield ("agent", agent)


async def generate_agent_draft_streaming(
    prompt: str,
    entity_id: str,
    *,
    db: AsyncSession | None = None,
    user_id: str = "",
) -> AsyncIterator[Tuple[str, object]]:
    """Generate an agent draft without persisting it.

    This powers the AI create review step: the UI can show the generated
    persona and only call ``create_agent`` after the user confirms.
    """
    yield ("step", "Designing the agent")
    capability_catalog = None
    if db is not None and user_id:
        from packages.core.services.agent_capability_catalog import (
            AgentCapabilityCatalogFactory,
        )

        capability_catalog = await AgentCapabilityCatalogFactory.create(
            db,
            entity_id=entity_id,
            user_id=user_id,
        )
    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": AGENT_GENERATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Create an agent for the following:\n\n{prompt}",
            },
        ],
        entity_id=entity_id,
        source=_AGENT_SOURCE,
        temperature=0.4,
        max_tokens=8000,
    )
    raw = completion.content
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response for agent generation")

    raw_spec = _extract_json(raw)
    if capability_catalog is not None:
        yield ("step", "Matching capabilities")
        plan = await _match_agent_capabilities(
            prompt=prompt,
            spec=raw_spec,
            entity_id=entity_id,
            capability_catalog=capability_catalog,
        )
        raw_spec["capability_ids"] = list(plan.selected_catalog_ids)
    spec = _normalize_agent_spec(raw_spec, capability_catalog=capability_catalog)
    yield ("step", "Preparing preview")
    yield ("draft", spec)


async def generate_agent(
    prompt: str,
    entity_id: str,
    db: AsyncSession,
    *,
    user_id: str = "",
) -> Any:
    """Generate and persist an agent from a natural-language prompt.

    Thin wrapper over :func:`generate_agent_streaming` for callers that only
    want the final agent without progress events.
    """
    agent = None
    async for kind, payload in generate_agent_streaming(
        prompt,
        entity_id,
        db,
        user_id=user_id,
    ):
        if kind == "agent":
            agent = payload
    if agent is None:
        raise ValueError("Agent generation did not produce an agent")
    return agent
