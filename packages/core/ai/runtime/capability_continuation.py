"""Bounded, non-authorizing capability identities for the next chat turn."""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

CAPABILITY_HINT_VERSION = 1
CAPABILITY_HINT_LIMIT = 12
CAPABILITY_SCHEMA_CHAR_BUDGET = 24_000
_SCOPE_KEYS = (
    "conversation_id",
    "entity_id",
    "user_id",
    "agent_id",
    "workspace_id",
)


def runtime_capability_scope(**values: Any) -> dict[str, str | None]:
    """Build the durable identity boundary for one chat capability record."""
    return {
        key: (str(values[key]) if values.get(key) is not None else None)
        for key in _SCOPE_KEYS
    }


def runtime_known_capability_hints(
    state: Any,
    *,
    scope: dict[str, str | None],
    limit: int = CAPABILITY_HINT_LIMIT,
) -> list[dict[str, str]]:
    """Read same-scope identities only; a hint is never a grant or schema."""
    if not isinstance(state, dict) or state.get("version") != CAPABILITY_HINT_VERSION:
        return []
    if state.get("scope") != runtime_capability_scope(**scope):
        return []
    hints = state.get("hints")
    if not isinstance(hints, list):
        return []
    accepted: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in hints:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not name or source not in {"used", "loaded"} or name in seen:
            continue
        item = {"name": name, "source": source}
        observed_at = raw.get("observed_at")
        if isinstance(observed_at, str) and observed_at:
            item["observed_at"] = observed_at
        provider = raw.get("provider")
        if isinstance(provider, str) and provider:
            item["provider"] = provider
        accepted.append(item)
        seen.add(name)
        if len(accepted) >= max(0, limit):
            break
    return accepted


def runtime_capability_hint_state(
    *,
    scope: dict[str, str | None] | None,
    hints: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Serialize a small credential-free identity set into result metadata."""
    normalized_scope = runtime_capability_scope(**(scope or {}))
    items: list[dict[str, str]] = []
    positions: dict[str, int] = {}
    for raw in hints:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not name or source not in {"used", "loaded"}:
            continue
        item = {"name": name, "source": source}
        for key in ("observed_at", "provider"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                item[key] = value
        old = positions.get(name)
        if old is not None:
            # A direct call is more useful than a discovery-only hint while
            # retaining the original observation time when a schema is reused.
            if source == "used" and items[old].get("source") != "used":
                items[old] = item
            continue
        positions[name] = len(items)
        items.append(item)
    # Keep recent actual use ahead of schema-only candidates. Apply the bound
    # after ranking so a broad discovery cannot evict the working capability.
    def observation_time(item: dict[str, str]) -> datetime:
        try:
            observed = datetime.fromisoformat(item.get("observed_at", ""))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)

    items.sort(key=observation_time, reverse=True)
    items.sort(key=lambda item: item["source"] != "used")
    return {
        "version": CAPABILITY_HINT_VERSION,
        "scope": normalized_scope,
        "hints": items[:CAPABILITY_HINT_LIMIT],
    }


async def runtime_restore_known_capability_schemas(
    hints: Iterable[dict[str, Any]],
    *,
    blocked_tool_names: Iterable[str] = (),
    prepare_exact: Callable[[tuple[str, ...]], Awaitable[Iterable[str]]] | None,
    schema_for_actor: Callable[[str], Awaitable[dict[str, Any] | None]],
    schema_char_budget: int | None = CAPABILITY_SCHEMA_CHAR_BUDGET,
    limit: int = CAPABILITY_HINT_LIMIT,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Prepare exact current identities and return only current schemas.

    ``prepare_exact`` belongs to the existing scoped discovery path. It must
    enforce current Agent/provider/account scope and may establish a run-local
    dynamic grant. This function intentionally cannot do semantic matching.
    """
    blocked = {str(name).strip() for name in blocked_tool_names if str(name or "").strip()}
    names = tuple(dict.fromkeys(
        str(item.get("name") or "").strip()
        for item in hints
        if isinstance(item, dict) and str(item.get("name") or "").strip() not in blocked
    ))[:max(0, limit)]
    if not names or prepare_exact is None:
        return [], ()
    try:
        prepared = {
            str(name).strip() for name in await prepare_exact(names)
            if str(name or "").strip()
        }
    except Exception:
        # A stale provider must not remove unrelated known capabilities. Retry
        # each exact identity so current scope can reject only the failing one.
        prepared = set()
        for name in names:
            try:
                prepared.update(
                    str(value).strip() for value in await prepare_exact((name,))
                    if str(value or "").strip()
                )
            except Exception:
                continue
    schemas: list[dict[str, Any]] = []
    restored: list[str] = []
    chars = 0
    for name in names:
        if name not in prepared:
            continue
        try:
            schema = await schema_for_actor(name)
        except Exception:
            continue
        function = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(function, dict) or str(function.get("name") or "") != name:
            continue
        schema_chars = len(json.dumps(schema, ensure_ascii=False, sort_keys=True))
        if schema_char_budget is not None and chars + schema_chars > schema_char_budget:
            continue
        schemas.append(copy.deepcopy(schema))
        restored.append(name)
        chars += schema_chars
    return schemas, tuple(restored)


async def runtime_restore_gateway_tools(
    names: Iterable[str | dict[str, Any]],
    *,
    envelope: Any,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    active_user_message: str | None,
    limit: int = CAPABILITY_HINT_LIMIT,
    schema_char_budget: int | None = CAPABILITY_SCHEMA_CHAR_BUDGET,
    refused: set[str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Make tools an earlier turn or wake loaded callable again, re-authorized.

    Only names are restored, never authority: each one goes through exact
    discovery under ``envelope`` (the current run's scope), which rebuilds
    the run-local discovered grants on that envelope. A name the current
    scope no longer allows is dropped. Shared by chat (names from message
    metadata) and the harness (names from a run's checkpointed evidence).

    ``refused`` collects the names discovery answered without loading: the
    current scope said no. A name whose discovery failed (an exception or an
    error payload) is not refused; it may still load on a later attempt.
    """
    hints = [
        item if isinstance(item, dict) else {"name": item}
        for item in names or ()
        if isinstance(item, (str, dict))
    ]
    if not hints or not entity_id or not user_id or envelope is None:
        return [], ()
    from packages.core.ai.runtime.tool_discovery import SEARCH_TOOLS_HARD_CAP
    from packages.core.ai.runtime.tool_registry import (
        runtime_discover_tools,
        runtime_tool_schema_for_actor,
    )

    discovered_matches: dict[str, dict] = {}

    async def prepare_exact(batch_names: tuple[str, ...]) -> set[str]:
        prepared: set[str] = set()
        # Persisted hints can exceed one exact-discovery request's bound.
        # Keep discovery's existing scope and size checks for every batch.
        for offset in range(0, len(batch_names), SEARCH_TOOLS_HARD_CAP):
            batch = batch_names[offset:offset + SEARCH_TOOLS_HARD_CAP]
            raw = await runtime_discover_tools(
                entity_id=entity_id,
                user_id=user_id,
                arguments={
                    "mode": "exact",
                    "exact_names": list(batch),
                    "kinds": ["tool", "mcp_tool"],
                    "max_results": len(batch),
                    "_agent_id_from_context": agent_id,
                    "_active_user_message_from_context": active_user_message,
                    "_runtime_envelope_from_context": envelope,
                    "_allowed_tool_names_from_context": envelope.effective_allowed_tool_names(),
                    "workspace_id": workspace_id,
                    "conversation_id": conversation_id,
                },
            )
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                logger.warning("Capability restoration rejected: %s", payload["error"])
                continue
            for match in payload.get("matches") or []:
                if isinstance(match, dict) and isinstance(match.get("name"), str):
                    discovered_matches[match["name"]] = match
            loaded = {
                str(name).strip()
                for name in (payload.get("loaded_tools") or [])
                if str(name or "").strip()
            }
            if refused is not None:
                refused.update(name for name in batch if name not in loaded)
            prepared.update(loaded)
        return prepared

    async def schema_for_actor(name: str) -> dict | None:
        schema = await runtime_tool_schema_for_actor(
            name, entity_id=entity_id, user_id=user_id,
        )
        match = discovered_matches.get(name)
        if isinstance(schema, dict) and isinstance(match, dict):
            from packages.core.ai.runtime.tool_discovery import (
                runtime_apply_integration_account_options_to_schema,
            )

            return runtime_apply_integration_account_options_to_schema(
                schema,
                match.get("account_options"),
                requires_explicit_account=bool(match.get("requires_explicit_account")),
            )
        return schema

    return await runtime_restore_known_capability_schemas(
        hints,
        blocked_tool_names=getattr(envelope, "blocked_tool_names", ()),
        prepare_exact=prepare_exact,
        schema_for_actor=schema_for_actor,
        schema_char_budget=schema_char_budget,
        limit=limit,
    )
