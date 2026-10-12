"""Internal memory skill runner: worker routing, bounded input and cached decisions.

Callers authorize and load current records. Cache stores decisions, never
authority: selection IDs are checked again against the current input snapshot.
Persistence remains in the existing scoped memory services.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import OrderedDict
import time
import hashlib
import json
import logging
import math
from pathlib import Path
from uuid import uuid4

from packages.core.cache import cache
from packages.core.memory.cache import read_memory_text
from packages.core.memory.policy import content_fingerprint

logger = logging.getLogger(__name__)
SKILL_PATH = Path(__file__).parent / "skills" / "memory-management" / "SKILL.md"
JUDGMENT_CACHE_TTL = 15 * 60
EXTRACTION_CACHE_TTL = 7 * 24 * 60 * 60
WORKER_TIMEOUT = 40
SELECTION_TIMEOUT = 8
MAX_INPUT_CHARS = 100_000
_LOCAL_DECISIONS: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_INFLIGHT: set[str] = set()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def _store_decision(key: str, decision: dict, operation: str, *, expires_at=None) -> None:
    """Selection expires after 15 minutes; unchanged extractions slide 7 days.

    The absolute deadline travels with short-lived judgments, so promoting a
    Redis hit into another process cannot restart its 15-minute lifetime.
    """
    now = time.time()
    if operation != "select" or expires_at is None:
        expires_at = now + (JUDGMENT_CACHE_TTL if operation == "select" else EXTRACTION_CACHE_TTL)
    remaining = max(0, expires_at - now)
    value = {"result": deepcopy(decision), "expires_at": expires_at}
    # Write through even on a local hit: another process must see the renewed
    # Redis lifetime, and a recovered Redis can be repopulated from local cache.
    await cache.set(key, value, ttl=max(1, math.ceil(remaining)))
    _LOCAL_DECISIONS[key] = (time.monotonic() + remaining, value)
    _LOCAL_DECISIONS.move_to_end(key)
    while len(_LOCAL_DECISIONS) > 128:
        _LOCAL_DECISIONS.popitem(last=False)


def _validate(operation: str, data: object, payload: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Memory worker must return an object")
    if operation == "select":
        ids = data.get("ids")
        allowed = {item["id"] for item in payload["records"]}
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in allowed for i in ids):
            raise ValueError("Memory worker selected an unknown record")
        return {"ids": list(dict.fromkeys(ids))[: payload.get("limit", 100)]}
    key = "entries" if operation == "extract" else "memories"
    entries = data.get(key)
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("Invalid memory extraction collection")
    seen, valid = set(), []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Invalid memory extraction item")
        body = item.get("body" if key == "entries" else "content")
        kind = item.get("scope" if key == "entries" else "type")
        kinds = (
            {"fact", "preference", "decision", "guidance", "learning"}
            if key == "entries"
            else {"fact", "preference", "context", "instruction"}
        )
        if kind not in kinds or not isinstance(body, str) or not body.strip() or len(body) > 4000:
            raise ValueError("Invalid memory content or category")
        if key == "entries":
            confidence = item.get("confidence")
            if (
                not isinstance(confidence, (float, int))
                or isinstance(confidence, bool)
                or not 0.3 <= confidence <= 0.8
                or not isinstance(item.get("title"), str)
                or not 1 <= len(item["title"].strip()) <= 200
                or not isinstance(item.get("source_message_id"), str)
                or item["source_message_id"] not in payload.get("source_message_ids", [])
            ):
                raise ValueError("Invalid memory evidence")
            tags = item.get("tags", [])
            if (
                not isinstance(tags, list)
                or len(tags) > 20
                or any(not isinstance(t, str) or len(t) > 100 for t in tags)
            ):
                raise ValueError("Invalid memory tags")
            entry = {k: item[k] for k in ("scope", "title", "body", "source_message_id", "confidence")}
            entry["tags"] = tags
        else:
            importance = item.get("importance", 5)
            if type(importance) is not int or not 1 <= importance <= 10:
                raise ValueError("Invalid memory importance")
            entry = {"type": kind, "content": body, "importance": importance}
        fingerprint = (kind, content_fingerprint(body))
        if fingerprint not in seen:
            seen.add(fingerprint)
            valid.append(entry)
    return {key: valid}


async def run_memory_skill(
    operation: str,
    payload: dict,
    *,
    entity_id: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    from packages.core.ai.runtime.completions import (
        runtime_execute_text_completion,
        runtime_resolve_text_completion_route,
    )
    from packages.core.ai.runtime.sources import RUNTIME_INTERNAL_WORKER_SOURCE

    if operation not in {"extract", "extract_legacy", "select"}:
        raise ValueError("Unknown memory operation")
    serialized = json.dumps({**payload, "operation": operation}, ensure_ascii=False)
    if len(serialized) > MAX_INPUT_CHARS:
        raise ValueError("Memory input exceeds worker budget")
    skill = read_memory_text(str(SKILL_PATH))
    if not skill:
        raise ValueError("Memory management skill is missing")
    model, metadata, byok = await runtime_resolve_text_completion_route(
        entity_id=entity_id,
        user_id=user_id,
        source=RUNTIME_INTERNAL_WORKER_SOURCE,
        model=None,
        metadata=None,
    )
    key = f"memory:{'judgment' if operation == 'select' else 'extraction'}:v2:" + _digest(
        {
            "scope": [entity_id, workspace_id, user_id, agent_id],
            "skill": _digest(skill),
            "operation": operation,
            "payload": payload,
            "reasoning_effort": "none",
            # Hash route configuration, never store/log credentials in cache keys.
            "route": _digest([model, metadata]),
        }
    )
    async def read_cached(*, shared_only=False):
        local = _LOCAL_DECISIONS.get(key) if not shared_only else None
        cached = local[1] if local and local[0] > time.monotonic() else await cache.get(key)
        if cached is None:
            return None
        try:
            if not isinstance(cached, dict) or not isinstance(cached.get("expires_at"), (int, float)):
                raise ValueError("Invalid memory cache deadline")
            if not math.isfinite(cached["expires_at"]) or cached["expires_at"] <= time.time():
                raise ValueError("Expired memory cache")
            validated = _validate(operation, cached.get("result"), payload)
        except ValueError:
            _LOCAL_DECISIONS.pop(key, None)
            await cache.delete(key)
        else:
            await _store_decision(key, validated, operation, expires_at=cached["expires_at"])
            logger.info("[memory-manager] operation=%s cache=hit", operation)
            return deepcopy(validated)
        return None

    cached = await read_cached()
    if cached is not None:
        return cached
    if key in _INFLIGHT:
        raise RuntimeError("Memory decision is already running")
    _INFLIGHT.add(key)
    token = uuid4().hex
    timeout = SELECTION_TIMEOUT if operation == "select" else WORKER_TIMEOUT
    lease = None
    try:
        lease = await cache.acquire_lease(key + ":lease", token, ttl=timeout + 10)
        if lease is False:
            # Reads use bounded fallback; extraction retries without a bookmark.
            raise RuntimeError("Memory decision is already running")
        # Another process may have finished between the initial lookup and lock.
        cached = await read_cached(shared_only=True)
        if cached is not None:
            return cached
        from packages.core.ai.runtime.billing import runtime_llm_billing_context

        # Chat can suppress automatic billing while settling its own provider
        # calls. Memory uses an independent worker call and must not inherit that
        # suppression (or add its separately billed usage to the parent again).
        async with runtime_llm_billing_context(
            entity_id, user_id=user_id, agent_id=agent_id, workspace_id=workspace_id,
            source=RUNTIME_INTERNAL_WORKER_SOURCE, byok=bool(byok),
        ):
            result = await asyncio.wait_for(
                runtime_execute_text_completion(
                    [{"role": "system", "content": skill}, {"role": "user", "content": serialized}],
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    source=RUNTIME_INTERNAL_WORKER_SOURCE,
                    model=model,
                    metadata=metadata,
                    temperature=0,
                    max_tokens=2500,
                    # Keep the bounded JSON budget available for the result.
                    # Reasoning-enabled defaults can consume it before the
                    # worker finishes even a small extraction batch.
                    reasoning_effort="none",
                    response_format={"type": "json_object"},
                ),
                timeout=timeout,
            )
        if result.usage.get("error"):
            raise ValueError("Memory worker failed")
        content = result.content.strip()
        if content.startswith("```json\n") and content.endswith("```"):
            content = content[8:-3]
        validated = _validate(operation, json.loads(content), payload)
        await _store_decision(key, validated, operation)
        logger.info("[memory-manager] operation=%s cache=miss", operation)
        return validated
    finally:
        _INFLIGHT.discard(key)
        if lease:
            await cache.release_lease(key + ":lease", token)


async def select_memory_records(records: list[dict], *, query: str | None, entity_id: str,
                                workspace_id=None, user_id=None, agent_id=None) -> list[dict]:
    """One relevance policy for authorized DB rows and complete MD sources.

    Read current records before calling: cached IDs never resurrect a deleted,
    expired, or newly inaccessible record. Small snapshots use no worker call.
    """
    if not entity_id or not query or sum(len(r["content"]) for r in records) <= 8000:
        return records
    try:
        result = await run_memory_skill(
            "select", {"query": query, "records": records},
            entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, agent_id=agent_id,
        )
    except Exception:
        logger.warning("[memory-manager] selection unavailable; retaining bounded memory")
        return records
    by_id = {r["id"]: r for r in records}
    return [by_id[i] for i in result["ids"]]


def prompt_memory_selection_candidates(ctx, sections: list[dict]) -> list[dict]:
    """Optional memory sections chosen per message, or [] when all of them fit.

    Core files (AGENT/WORKSPACE/STATE/FILES.md) are always kept. When the rest
    exceed the hot-memory size, which of them reach the prompt depends on the
    current message.
    """
    optional = [
        s
        for s in sections
        if s["category"] == "memory_files"
        and s.get("source", "").startswith(("workspace:", "workspace_agent:", "agent_files:"))
        and not s["key"].endswith(("AGENT.md", "WORKSPACE.md", "STATE.md", "FILES.md"))
    ]
    # Small hot memory needs no additional model call.
    if not ctx.entity_id or not ctx.active_user_message or sum(len(s["content"]) for s in optional) <= 8000:
        return []
    return optional


async def select_prompt_memories(ctx, sections: list[dict]) -> list[dict]:
    """Filter only optional memory; rules/identity/other prompt sections survive."""
    optional = prompt_memory_selection_candidates(ctx, sections)
    if not optional:
        return sections
    # Supply complete records: a preview could hide the relevant qualification.
    # Oversized snapshots fail before a model call and use the bounded fallback.
    records = [{"id": s["source"], "content": s["content"], "revision": s["source_digest"]} for s in optional]
    result = await select_memory_records(
        records, query=ctx.active_user_message, entity_id=ctx.entity_id,
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, agent_id=ctx.agent_id,
    )
    eligible = {s["source"] for s in optional}
    selected = {r["id"] for r in result}
    return [
        {**s, "content": "", "policy": "deferred_by_memory_selection"} if s.get("source") in eligible - selected else s
        for s in sections
    ]
