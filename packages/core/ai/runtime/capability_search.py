from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

from packages.core.ai.runtime.skill_capability_companion import (
    SkillCapabilityCompanion,
)


# A Skill's description is all a caller reads before invoke_skill, so search
# shows it whole. Authors are held to this length when they write it (the
# Agent Skills limit); only text stored before that is cut here.
SKILL_DESCRIPTION_MAX_CHARS = 1024
_SEARCH_TOKEN_RE = re.compile(
    r"[a-z0-9]+(?:[.@][a-z0-9]+)*|[\u4e00-\u9fff]+",
    re.IGNORECASE,
)
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


@lru_cache(maxsize=4096)
def _runtime_capability_search_terms(text: str) -> tuple[str, ...]:
    """Return stable whole-word search terms for a catalog projection.

    This cache contains only normalized public catalog text. Eligibility and
    execution authorization are deliberately resolved before and after it.
    """

    return tuple(
        dict.fromkeys(
            token
            for match in _SEARCH_TOKEN_RE.finditer(str(text or "").casefold())
            if (token := match.group(0).strip(".@"))
            and token not in _SEARCH_STOPWORDS
        )
    )


@lru_cache(maxsize=2048)
def _runtime_skill_search_document(
    skill_id: str,
    slug: str,
    name: str,
    description: str,
    category: str,
    output_format: str,
    declared_tools: tuple[str, ...],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Cache the pure searchable projection, never the visibility decision."""

    identity_terms = frozenset(
        _runtime_capability_search_terms(" ".join((skill_id, slug, name)))
    )
    description_terms = frozenset(
        _runtime_capability_search_terms(
            " ".join((description, category, output_format))
        )
    )
    tool_terms = frozenset(
        _runtime_capability_search_terms(" ".join(declared_tools))
    )
    return identity_terms, description_terms, tool_terms


def _skill_search_fields(skill: Any) -> tuple[str, str, str, str, str, str, tuple[str, ...]]:
    metadata = getattr(skill, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return (
        str(getattr(skill, "id", "") or "").strip(),
        str(getattr(skill, "slug", "") or getattr(skill, "name", "") or "").strip(),
        str(
            getattr(skill, "name", "")
            or getattr(skill, "display_name", "")
            or getattr(skill, "slug", "")
            or ""
        ).strip(),
        str(getattr(skill, "description", "") or "").strip(),
        str(metadata.get("category") or getattr(skill, "category", "") or "").strip(),
        str(
            metadata.get("output_format")
            or getattr(skill, "output_format", "")
            or ""
        ).strip(),
        tuple(
            str(name).strip()
            for name in (
                getattr(skill, "declared_tools", None)
                or getattr(skill, "tools", None)
                or ()
            )
            if str(name or "").strip()
        ),
    )


def runtime_skill_manifest(skill: Any) -> dict[str, Any]:
    """Return the Skill catalog record safe to expose during search, with its whole description."""

    skill_id, slug, name, description, category, _output, declared_tools = (
        _skill_search_fields(skill)
    )
    manifest: dict[str, Any] = {
        "kind": "skill",
        "name": slug or name or skill_id,
        "skill_id": skill_id,
        "slug": slug,
        "display_name": name,
        "description": description[:SKILL_DESCRIPTION_MAX_CHARS],
        "source": str(getattr(skill, "source", "entity") or "entity"),
        "available": True,
        "invoke_with": "invoke_skill",
        "tools": list(declared_tools),
    }
    if category:
        manifest["category"] = category
    metadata = getattr(skill, "metadata", None) or {}
    if isinstance(metadata, dict) and isinstance(metadata.get("required_integrations"), list):
        manifest["required_integrations"] = metadata["required_integrations"]
    if isinstance(metadata, dict) and isinstance(metadata.get("search_terms"), list):
        manifest["search_terms"] = [str(term) for term in metadata["search_terms"]][:12]
    if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
        manifest["description_truncated"] = True
    return manifest


def _runtime_skill_query_score(skill: Any, query: str) -> int:
    skill_id, slug, name, description, category, output_format, declared_tools = (
        _skill_search_fields(skill)
    )
    query_terms = _runtime_capability_search_terms(query)
    if not query_terms:
        return 0
    identity_terms, description_terms, tool_terms = _runtime_skill_search_document(
        skill_id,
        slug,
        name,
        description,
        category,
        output_format,
        declared_tools,
    )
    score = 0
    for term in query_terms:
        if term in identity_terms:
            score += 12
        elif term in description_terms:
            score += 4
        elif term in tool_terms:
            score += 1
    return score


def _runtime_exact_skill_selector_match(skill: Any, selector: str) -> bool:
    selector = str(selector or "").strip().casefold()
    if not selector:
        return False
    skill_id, slug, name, _description, _category, _output, _tools = (
        _skill_search_fields(skill)
    )
    candidates = {
        value.casefold()
        for value in (skill_id, slug, name)
        if value
    }
    return selector in candidates


def runtime_search_skill_candidates(
    *,
    skills: Iterable[Any],
    query: str,
    exact_selectors: Iterable[str] = (),
    max_results: int = 5,
    include_companion_skills: bool = True,
) -> list[dict[str, Any]]:
    """Search already-authorized Skill descriptors by bounded catalog text."""

    items = [
        skill
        for skill in (skills or ())
        if include_companion_skills
        or not isinstance(getattr(skill, "metadata", None), dict)
        or not getattr(skill, "metadata", {}).get("capability_companion")
    ]
    query_text = str(query or "").strip()
    selectors = [
        str(item or "").strip()
        for item in exact_selectors
        if str(item or "").strip()
    ]
    if selectors:
        selected: list[Any] = []
        seen: set[str] = set()
        for selector in selectors:
            for skill in items:
                if not _runtime_exact_skill_selector_match(skill, selector):
                    continue
                skill_id = str(getattr(skill, "id", "") or "")
                if skill_id in seen:
                    continue
                selected.append(skill)
                seen.add(skill_id)
                break
        return [runtime_skill_manifest(skill) for skill in selected[:max_results]]
    if not query_text:
        return []

    scored = [
        (_runtime_skill_query_score(skill, query_text), index, skill)
        for index, skill in enumerate(items)
    ]
    ranked = sorted(
        (item for item in scored if item[0] > 0),
        key=lambda item: (-item[0], item[1]),
    )
    return [
        {
            **runtime_skill_manifest(skill),
            "_search_score": score,
        }
        for score, _index, skill in ranked[:max_results]
    ]


def runtime_search_companion_skill_candidates(
    *,
    skills: Iterable[Any],
    tool_matches: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return ordinary Skills bound to available matched Tool/MCP capabilities."""

    matched_tool_names = tuple(
        dict.fromkeys(
            str(match.get("name") or "").strip()
            for match in tool_matches
            if str(match.get("name") or "").strip()
            and match.get("kind") != "skill"
            and match.get("available") is not False
        )
    )
    if not matched_tool_names:
        return []

    companions: list[dict[str, Any]] = []
    for skill in skills:
        metadata = getattr(skill, "metadata", None)
        raw_binding = (
            metadata.get("capability_companion")
            if isinstance(metadata, dict)
            else None
        )
        if raw_binding is None:
            continue
        try:
            binding = SkillCapabilityCompanion.from_config(raw_binding)
        except ValueError:
            continue
        companion_for = [
            tool_name
            for tool_name in matched_tool_names
            if binding.matches(tool_name)
        ]
        if not companion_for:
            continue
        companions.append(
            {
                **runtime_skill_manifest(skill),
                "capability_role": "companion",
                "companion_for": companion_for,
                "load_before_use": True,
            }
        )
    return companions


def runtime_companion_tool_names(
    *,
    skills: Iterable[Any],
    matched_tool_names: Iterable[str],
    available_tool_names: Iterable[str],
) -> list[str]:
    """Return declared companion tools after an authorized provider was found.

    Descriptors expose companion metadata only after the built-in trust check.
    The returned names are still intersected with the caller's scope-filtered
    schemas; a Skill description cannot expand the executable surface.
    """

    matched = tuple(
        dict.fromkeys(str(name or "").strip() for name in matched_tool_names if str(name or "").strip())
    )
    available = {str(name or "").strip() for name in available_tool_names}
    result: list[str] = []
    selected: set[str] = set()
    for skill in skills:
        if getattr(skill, "source", None) != "builtin":
            continue
        metadata = getattr(skill, "metadata", None)
        raw_binding = (
            metadata.get("capability_companion")
            if isinstance(metadata, dict)
            else None
        )
        if raw_binding is None:
            continue
        try:
            binding = SkillCapabilityCompanion.from_config(raw_binding)
        except ValueError:
            continue
        if not any(binding.matches(name) for name in matched):
            continue
        _id, _slug, _name, _description, _category, _output, declared_tools = (
            _skill_search_fields(skill)
        )
        for name in declared_tools:
            if name in available and binding.matches(name) and name not in selected:
                result.append(name)
                selected.add(name)
    return result


def _runtime_manifest_query_score(match: Mapping[str, Any], query: str) -> int:
    cached_score = match.get("_search_score")
    if isinstance(cached_score, int):
        return cached_score
    identity = " ".join(
        str(match.get(key) or "")
        for key in ("name", "skill_id", "slug", "display_name")
    )
    description = str(match.get("description") or "")
    identity_terms = set(_runtime_capability_search_terms(identity))
    description_terms = set(_runtime_capability_search_terms(description))
    score = 0
    for term in _runtime_capability_search_terms(query):
        if term in identity_terms:
            score += 12
        elif term in description_terms:
            score += 4
    return score


def runtime_merge_capability_matches(
    *,
    tool_matches: Iterable[Mapping[str, Any]],
    skill_matches: Iterable[Mapping[str, Any]],
    query: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Merge Tool/MCP and Skill candidates while preserving typed results."""

    tools = [dict(item) for item in tool_matches]
    skills = [dict(item) for item in skill_matches]
    if not skills:
        return tools[:max_results]
    if not tools:
        selected = skills[:max_results]
    else:
        combined: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, match in enumerate(tools + skills):
            kind = str(match.get("kind") or "tool")
            # Read the reusable operating contract before taking the raw tool
            # route when both are equally relevant. A stronger Tool/MCP score
            # still wins, so this tie-break does not dilute search precision.
            kind_priority = 1 if kind == "skill" else 0
            combined.append(
                (
                    _runtime_manifest_query_score(match, query),
                    kind_priority,
                    -index,
                    match,
                )
            )
        selected = [
            item[3]
            for item in sorted(combined, reverse=True)[:max_results]
        ]
    for match in selected:
        match.pop("_search_score", None)
    return selected
