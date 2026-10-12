"""Every discoverable capability can be found by the words people use for it.

Discovery ranks the English catalogs locally (capability_ranker): search_tools
ranks native tools, MCP providers and built-in Skills; manor(action='search')
ranks the Manor platform - Manor actions, Workspace actions and operations,
and Manor's own MCP servers. Each entry declares search metadata
(capability_search_metadata) whose every example query finds it in the top
FINDABLE_WITHIN results of the catalog that lists it. A new capability that
pushes an existing one out of reach fails here, naming both.
"""
from __future__ import annotations


import pytest

from packages.core.ai.runtime.capability_catalog import (
    builtin_capability_catalog as builtin_catalog,
    builtin_manor_catalog,
    builtin_skills,
)
from packages.core.ai.runtime.capability_matcher import candidate_id
from packages.core.ai.runtime.capability_ranker import query_is_english, rank_runtime_capabilities
from packages.core.ai.runtime.capability_search_metadata import (
    FINDABLE_WITHIN,
    MIN_EXAMPLES,
    capability_search_metadata,
    skill_search_metadata,
)

def _declared() -> dict[str, dict[str, list[str]]]:
    """Every catalog id with its declared terms and examples."""
    declared = dict(capability_search_metadata())
    for directory, _frontmatter_fields, config in builtin_skills():
        declared[f"skill:{directory}"] = skill_search_metadata(config)
    return declared


_MANOR_KINDS = ("manor_action:", "workspace_action:", "workspace_operation:", "manor_mcp:")
_ADMIN_PREFIX = "manor_mcp:manor_mcp_admin."


def _all_catalog_ids() -> set[str]:
    return {candidate_id(item) for item in (
        *builtin_catalog(manor_in_scope=True), *builtin_catalog(manor_in_scope=False),
        *builtin_manor_catalog(in_workspace=True), *builtin_manor_catalog(in_workspace=False),
    )}


def _catalog_listing(key: str) -> tuple[dict, ...]:
    """The catalog an id is found in: manor's own, or search_tools' as Manor chats see it."""
    if key.startswith(_MANOR_KINDS):
        inside = builtin_manor_catalog(in_workspace=True)
        listing = inside if key in {candidate_id(item) for item in inside} else builtin_manor_catalog(in_workspace=False)
        # Platform admin is listed only to platform admins; everyone else
        # searches without it.
        if not key.startswith(_ADMIN_PREFIX):
            listing = tuple(item for item in listing if item.get("provider") != "manor_mcp_admin")
        return listing
    listed = builtin_catalog(manor_in_scope=True)
    # Manor's MCP servers are listed by search_tools only where manor is not in scope.
    return listed if key in {candidate_id(item) for item in listed} else builtin_catalog(manor_in_scope=False)


def test_every_catalog_entry_declares_search_metadata():
    declared = _declared()
    missing = [
        key for key in sorted(_all_catalog_ids())
        if len((declared.get(key) or {}).get("examples") or ()) < MIN_EXAMPLES
    ]
    assert not missing, (
        f"{len(missing)} capabilities lack {MIN_EXAMPLES}+ search examples. Tools, Manor operations and "
        "MCP providers declare them in packages/core/ai/runtime/capability_search_metadata.json; Skills "
        f"in config.json under 'search'. Missing: {missing[:30]}"
    )


def test_search_metadata_is_english_and_names_real_capabilities():
    stale = sorted(set(capability_search_metadata()) - _all_catalog_ids())
    assert not stale, f"Search metadata for capabilities that no longer exist: {stale}"
    not_english = [
        (key, text) for key, entry in _declared().items()
        for text in entry.get("examples", ())
        # A pasted identifier would find a tool by its name, not by how
        # people ask for it.
        if not query_is_english(text) or "_" in text
    ] + [
        (key, term) for key, entry in _declared().items() for term in entry.get("terms", ())
        if not term.strip() or not all(ch.isascii() for ch in term if ch.isalpha())
    ]
    assert not not_english, (
        f"Search metadata must be English keywords people would use, not identifiers: {not_english[:20]}"
    )


def _same_integration(key: str) -> set[str]:
    """An MCP provider and its mcp_<provider> guidance Skill reach one integration."""
    if key.startswith("provider:"):
        return {key, f"skill:mcp_{key.split(':', 1)[1]}"}
    if key.startswith("skill:mcp_"):
        return {key, f"provider:{key.split('skill:mcp_', 1)[1]}"}
    return {key}


def example_misses() -> list[str]:
    misses = []
    for key, entry in sorted(_declared().items()):
        accepted = _same_integration(key)
        catalog = _catalog_listing(key)
        for example in entry.get("examples", ()):
            ranked = [match["id"] for match in rank_runtime_capabilities(
                query=example, candidates=catalog, limit=FINDABLE_WITHIN,
            ).matches]
            if not accepted & set(ranked):
                misses.append(f"{key}: {example!r} -> {ranked[:3]}")
    return misses


def test_every_example_finds_its_capability():
    misses = example_misses()
    assert not misses, (
        f"{len(misses)} example queries do not find their capability in the top {FINDABLE_WITHIN}. "
        "Add the missing words to its terms or description, or make the example specific:\n"
        + "\n".join(misses[:60])
    )
