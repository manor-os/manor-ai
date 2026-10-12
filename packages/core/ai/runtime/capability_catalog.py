"""The built-in capability catalogs discovery ranks, built from the repository.

search_tools ranks native tools, MCP providers and built-in Skills; where
``manor`` is in scope the Manor platform (its actions and Manor's own MCP
servers) is left to ``manor(action='search')`` instead. The registration
contract test ranks every declared example against the catalog that lists it;
the discovery search report re-ranks real queries against them.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime.capability_search_metadata import skill_search_metadata

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    fields = {}
    for line in text.split("---", 2)[1].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


@lru_cache(maxsize=1)
def builtin_skills() -> tuple[tuple[str, dict, dict], ...]:
    """(dir, frontmatter, config) for every built-in Skill."""
    skills = []
    for directory in sorted(SKILLS_ROOT.iterdir()):
        manifest = directory / "SKILL.md"
        if not manifest.is_file():
            continue
        config_path = directory / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        skills.append((directory.name, _frontmatter(manifest.read_text(encoding="utf-8")), config))
    return tuple(skills)


@lru_cache(maxsize=2)
def builtin_capability_catalog(manor_in_scope: bool = True) -> tuple[dict, ...]:
    """The candidates search_tools ranks, built from the repository alone.

    With ``manor`` in scope (every Manor chat) the Manor platform is searched
    by manor instead; without it, Manor's own MCP servers stay listed here.
    """
    from packages.core.ai.runtime.capability_search import runtime_skill_manifest
    from packages.core.ai.runtime.integration_provider_matcher import runtime_integration_provider_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index
    from packages.core.ai.runtime.tool_search import runtime_tool_manifest
    from packages.core.ai.runtime.manor_capability_catalog import (
        EMBEDDED_MCP_PROVIDERS,
        MANOR_PLATFORM_MCP_PROVIDERS,
    )
    from packages.core.ai.tool_pool import tool_pool
    from packages.core.services.mcp_seed import _MCP_CATALOG

    if not tool_pool._initialized:
        tool_pool.initialize()
    schemas = dict(tool_pool.registered_tool_schemas())
    candidates = [
        {**runtime_tool_manifest(name, schema),
         "description": str((schema.get("function") or {}).get("description") or "")}
        for name, schema in schemas.items() if not name.startswith("mcp__")
    ]
    candidates += [
        {"kind": "provider", "name": provider.provider_key, "description": provider.description}
        for provider in runtime_integration_provider_candidates(
            server_index=runtime_server_index(), tool_schemas=list(schemas.items()),
            provider_keys=[row[0] for row in _MCP_CATALOG],
        )
        if provider.provider_key not in EMBEDDED_MCP_PROVIDERS
        and not (manor_in_scope and provider.provider_key in MANOR_PLATFORM_MCP_PROVIDERS)
    ]
    for directory, frontmatter, config in builtin_skills():
        search = skill_search_metadata(config)
        candidates.append(runtime_skill_manifest(SimpleNamespace(
            id=directory, slug=frontmatter.get("name") or directory,
            name=config.get("display_name") or frontmatter.get("name") or directory,
            description=frontmatter.get("description", ""), source="builtin",
            metadata={"search_terms": search["terms"]}, declared_tools=(),
        )))
    return tuple(candidates)


def builtin_manor_catalog(*, in_workspace: bool) -> tuple[dict, ...]:
    """The candidates manor(action='search') ranks (every Manor MCP server listed)."""
    from packages.core.ai.runtime.manor_capability_catalog import manor_capability_catalog

    return tuple(manor_capability_catalog(in_workspace=in_workspace))
