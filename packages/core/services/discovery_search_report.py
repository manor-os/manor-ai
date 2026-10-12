"""How capability searches turned out in real conversations - from data already kept.

Nothing is written for this. Every tool call, search_tools included, already
lands in ``tool_call_logs`` with its arguments, so each search's query is
there. Ranking is local and deterministic, so the report re-ranks each query
against the current built-in catalog, and reads the model's next tool calls
in the same conversation to see whether the search found what it needed:

- used_result: the next non-discovery call was among the re-ranked results
  (also when the model searched again first: this query now finds it);
- searched_again: another search came first and found something this
  query's results do not contain;
- used_other: the model used something the results did not contain;
- used_baseline: the next call was an always-loaded tool (bash, read_file,
  web_search...) - the search led nowhere, but whether a better capability
  existed needs a look at the query, so these stay out of the found rate;
- no_tool_call: nothing followed within the follow window;
- query_not_english: rejected for rewriting (decided from the query itself).

Re-ranking against today's catalog asks whether past real queries find their
capability now, so a report run after a metadata fix shows its effect. The
catalog is the built-in one plus each tenant's own Skills (invoke_skill
names a Skill by its row id); it can differ slightly from what a user saw
when a tenant's Skills or connections changed since. Misses pair the query with the tool eventually
used: candidate example queries for capability_search_metadata.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill
from packages.core.models.usage import ToolCallLog

FOLLOW_WINDOW = timedelta(minutes=15)
RESULTS_CONSIDERED = 5


def _search_query(row: ToolCallLog) -> str | None:
    """The semantic query of a search_tools / manor search call, else None."""
    args = row.tool_args if isinstance(row.tool_args, dict) else {}
    if row.tool_name == "search_tools":
        if str(args.get("mode") or "semantic") != "semantic":
            return None
        return str(args.get("query") or "").strip() or " ; ".join(_queries(row)) or None
    if row.tool_name == "manor" and args.get("action") == "search":
        return str(args.get("query") or "").strip() or None
    return None


def _queries(row: ToolCallLog) -> tuple[str, ...]:
    """A multi-step search's per-step queries (search_tools ``queries``)."""
    args = row.tool_args if isinstance(row.tool_args, dict) else {}
    queries = args.get("queries") if row.tool_name == "search_tools" else None
    return tuple(str(q).strip() for q in queries if str(q).strip()) if isinstance(queries, list) else ()


def _called(row: ToolCallLog) -> tuple[str, dict]:
    """The capability actually run; call_tool rows name their target."""
    args = row.tool_args if isinstance(row.tool_args, dict) else {}
    if row.tool_name == "call_tool":
        inner = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
        return str(args.get("name") or "call_tool"), inner
    return row.tool_name, args


def _is_discovery(row: ToolCallLog) -> bool:
    return _search_query(row) is not None or row.tool_name in {"search_tools", "acquire_capabilities"}


def _invoked_skill(args: dict) -> str:
    return str(args.get("skill") or args.get("skill_id") or args.get("slug") or "")


def _workspace_action(args: dict) -> str:
    """manor(action='workspace') carries the Workspace action in params."""
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    return str(params.get("action") or "")


def _workspace_patch_ops(args: dict) -> set[str]:
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    inner = params.get("params") if isinstance(params.get("params"), dict) else params
    return {str(patch.get("op")) for patch in inner.get("patches") or () if isinstance(patch, dict)}


def _found(name: str, args: dict, results: Iterable[str], names: dict[str, str],
           skill_aliases: dict[str, set[str]]) -> bool:
    for key in results:
        kind, _, ident = key.partition(":")
        if kind == "tool" and ident == name:
            return True
        if kind == "tool" and name == "manor" and args.get("action") == "workspace" \
                and ident == f"workspace_{_workspace_action(args)}":
            return True
        # A Manor action named like a tool is listed as that tool.
        if kind in {"manor_action", "tool"} and name == "manor" and args.get("action") == ident:
            return True
        if kind == "provider" and name.startswith(f"mcp__{ident}__"):
            return True
        # manor(action='search') lists Workspace actions/operations and Manor's MCP tools.
        if kind == "workspace_action" and name == "manor" and _workspace_action(args) == ident:
            return True
        if kind == "workspace_operation" and name == "manor" and ident in _workspace_patch_ops(args):
            return True
        if kind == "manor_mcp" and name == "mcp__{}__{}".format(*ident.split(".", 1)):
            return True
        if kind == "skill" and name == "invoke_skill":
            invoked = _invoked_skill(args)
            if {invoked, *skill_aliases.get(invoked, ())} & {ident, names.get(key, "")}:
                return True
    return False


def _label(row: ToolCallLog, skill_aliases: dict[str, set[str]]) -> str:
    """What a miss eventually used, specific enough to act on."""
    name, args = _called(row)
    if name == "manor" and args.get("action") == "workspace":
        return f"manor:workspace.{_workspace_action(args)}"
    if name == "manor":
        return f"manor:{args.get('action') or ''}"
    if name == "invoke_skill":
        invoked = _invoked_skill(args)
        return f"invoke_skill:{min(skill_aliases.get(invoked) or {invoked})}"
    return name


async def _skills(db: AsyncSession, rows: Iterable[ToolCallLog]) -> tuple[dict[str, set[str]], dict[str, list[dict]]]:
    """Slug aliases of invoked Skill row ids, and each tenant's own Skills as candidates."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.capability_search import runtime_skill_manifest
    from packages.core.ai.runtime.capability_search_metadata import skill_search_metadata

    rows = list(rows)
    invoked = {_invoked_skill(_called(row)[1]) for row in rows if _called(row)[0] == "invoke_skill"}
    entities = {row.entity_id for row in rows if _search_query(row) is not None and row.entity_id}
    found = (await db.execute(select(Skill).where(
        Skill.id.in_(invoked - {""}) | Skill.entity_id.in_(entities)
    ))).scalars().all() if invoked or entities else []
    aliases: dict[str, set[str]] = {}
    tenant: dict[str, list[dict]] = defaultdict(list)
    for skill in found:
        aliases[skill.id] = {value for value in (skill.slug, skill.name) if value}
        if skill.entity_id in entities and skill.status == "active":
            tenant[skill.entity_id].append(runtime_skill_manifest(SimpleNamespace(
                id=skill.id, slug=skill.slug, name=skill.display_name or skill.name,
                description=skill.description or "", category=skill.category, source="entity",
                metadata={"search_terms": skill_search_metadata(skill.config)["terms"]},
                declared_tools=tuple(skill.tools or ()),
            )))
    return aliases, tenant


async def discovery_search_report(
    db: AsyncSession, *, since: datetime, until: datetime | None = None,
    entity_id: str | None = None, max_misses: int = 50, catalog: Iterable[dict] | None = None,
    manor_catalog: Iterable[dict] | None = None,
) -> dict[str, Any]:
    """Read-only; scans tool_call_logs for the window (index: created_at)."""
    from packages.core.ai.runtime.capability_catalog import builtin_capability_catalog, builtin_manor_catalog
    from packages.core.ai.runtime.capability_matcher import candidate_id
    from packages.core.ai.runtime.capability_ranker import query_is_english, rank_runtime_capabilities
    from packages.core.ai.runtime.tool_discovery import SEARCH_TOOLS_HARD_CAP, SEARCH_TOOLS_RESULTS_PER_QUERY
    from packages.core.ai.runtime.tool_visibility import (
        ALWAYS_LOADED, MASTER_ALWAYS_LOADED, WORKSPACE_AGENT_ALWAYS_LOADED,
    )

    baseline = (ALWAYS_LOADED | MASTER_ALWAYS_LOADED | WORKSPACE_AGENT_ALWAYS_LOADED) - {
        "manor", "invoke_skill", "call_tool", "search_tools",
    }
    catalog = tuple(catalog if catalog is not None else builtin_capability_catalog())
    # manor(action='search') ranks the Manor platform's own catalog (the
    # Workspace view, its superset).
    manor_catalog = builtin_manor_catalog(in_workspace=True) if manor_catalog is None else tuple(manor_catalog)
    filters = [ToolCallLog.created_at >= since, ToolCallLog.conversation_id.is_not(None)]
    if until is not None:
        filters.append(ToolCallLog.created_at < until + FOLLOW_WINDOW)
    if entity_id:
        filters.append(ToolCallLog.entity_id == entity_id)
    by_conversation: dict[str, list[ToolCallLog]] = defaultdict(list)
    for row in (await db.execute(
        select(ToolCallLog).where(*filters).order_by(ToolCallLog.conversation_id, ToolCallLog.created_at)
    )).scalars():
        by_conversation[row.conversation_id].append(row)
    skill_aliases, tenant_skills = await _skills(
        db, (row for rows in by_conversation.values() for row in rows),
    )
    names = {candidate_id(item): str(item.get("name") or "")
             for item in (*catalog, *(item for items in tenant_skills.values() for item in items))}

    outcomes: Counter = Counter()
    misses = []
    searches = 0
    for rows in by_conversation.values():
        for index, row in enumerate(rows):
            query = _search_query(row)
            if query is None or (until is not None and row.created_at >= until):
                continue
            searches += 1
            queries = _queries(row)
            if not all(query_is_english(text) for text in queries or (query,)):
                outcomes["query_not_english"] += 1
                continue
            results = [match["id"] for match in rank_runtime_capabilities(
                query=query,
                candidates=manor_catalog if row.tool_name == "manor" else (*catalog, *tenant_skills.get(row.entity_id, ())),
                # As many as search_tools shows by default.
                limit=min(SEARCH_TOOLS_HARD_CAP, max(RESULTS_CONSIDERED, SEARCH_TOOLS_RESULTS_PER_QUERY * len(queries))),
                queries=queries,
            ).matches]
            following = [
                later for later in rows[index + 1:]
                if later.created_at - row.created_at <= FOLLOW_WINDOW
            ]
            if not following:
                outcomes["no_tool_call"] += 1
                continue
            used = next((later for later in following if not _is_discovery(later)), None)
            if used is not None and _found(*_called(used), results, names, skill_aliases):
                outcomes["used_result"] += 1
            elif _is_discovery(following[0]):
                outcomes["searched_again"] += 1
                misses.append({"query": query, "outcome": "searched_again",
                               "eventually_used": _label(used, skill_aliases) if used is not None else None,
                               "results": results})
            elif _called(used)[0] in baseline:
                outcomes["used_baseline"] += 1
                misses.append({"query": query, "outcome": "used_baseline",
                               "eventually_used": _label(following[0], skill_aliases),
                               "results": results})
            else:
                outcomes["used_other"] += 1
                misses.append({"query": query, "outcome": "used_other",
                               "eventually_used": _label(following[0], skill_aliases),
                               "results": results})

    followed = outcomes["used_result"] + outcomes["searched_again"] + outcomes["used_other"]
    return {
        "since": since.isoformat(), "until": until.isoformat() if until else None,
        "searches": searches,
        "outcomes": dict(outcomes),
        "found_rate": round(outcomes["used_result"] / followed, 3) if followed else None,
        "misses": misses[:max_misses],
    }
