from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import capability_matcher as matcher
from packages.core.ai.runtime.capability_matcher import capability_candidate_page, parse_capability_selection
from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler


def schema(name, description):
    return name, {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {}},
    }}


def decision(key, fit="direct"):
    return {"id": key, "fit": fit, "reason": "Retrieves current public information",
            "covers": ["public information retrieval"], "gaps": ["latest publication must be verified"]}


def test_catalog_paging_covers_zero_lexical_matches_and_all_kinds():
    candidates = [{"kind": kind, "name": f"item-{i}", "description": "English description"}
                  for kind in ("tool", "skill", "provider") for i in range(80)]
    seen = set()
    cursor = 0
    while True:
        page, cursor = capability_candidate_page(candidates, query="搜索最新发布", cursor=cursor)
        assert len(page) <= matcher.MAX_CANDIDATES
        assert all(item["id"] not in seen for item in page)
        seen.update(item["id"] for item in page)
        if cursor is None:
            break
    assert len(seen) == 240


@pytest.mark.parametrize("bad", [
    {**decision("tool:outside")},
    {**decision("tool:web_search"), "allowed": True},
    {**decision("tool:web_search"), "fit": "completed"},
])
def test_model_cannot_mint_host_availability_or_unknown_ids(bad):
    with pytest.raises(ValueError):
        parse_capability_selection(json.dumps({"matches": [bad]}), [{"id": "tool:web_search"}])


@pytest.mark.asyncio
async def test_retrieval_query_shortlists_retrieval_tools_and_shows_skill_limits(monkeypatch):
    """Ranking is a shortlist: the model reads each description and decides.

    The Skill's own limitation is shown so the model can reject it; no model
    is called during discovery.
    """
    names = [schema("web_search", "Search the public web for current posts and news"),
             schema("web_fetch", "Read a public page"), schema("invoke_skill", "Run a Skill")]
    skills = [SimpleNamespace(id=name, slug=name, name=name, source="entity", metadata={},
                              description=description) for name, description in (
        ("pptx", "Create PowerPoint and post-process presentations"),
        ("script", "Write Instagram reel scripts; does not retrieve current publications"),
    )]

    async def no_model(*args, **kwargs):
        raise AssertionError("discovery must not call a model")

    async def load_skills():
        return skills

    async def available(matches, *_args):
        return [{**item, "available": True} for item in matches]

    monkeypatch.setattr(matcher, "runtime_execute_text_completion", no_model)
    monkeypatch.setattr("packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability", available)
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "semantic", "query": "search latest Instagram posts and reels"},
        tool_schemas=names, available_tool_names=[name for name, _ in names], skill_descriptor_loader=load_skills,
    ))
    assert "web_search" in result["loaded_tools"]
    assert result["discovery"]["assessment_status"] == "ranked"
    assert result["discovery"]["action_executed"] is False
    script = next((m for m in result["matches"] if m.get("name") == "script"), None)
    assert script is None or "does not retrieve" in script["description"]
    assert all(not item["availability"]["execution_verified"] for item in result["matches"])


@pytest.mark.asyncio
async def test_matcher_failure_is_not_no_match_evidence(monkeypatch):
    async def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(matcher, "runtime_execute_text_completion", fail)
    result = await matcher.assess_runtime_capabilities(
        query="search", candidates=[{"kind": "tool", "name": "web_search", "description": "Search"}],
    )
    assert result.status == "unassessed"


@pytest.mark.asyncio
async def test_no_keyword_hit_keeps_zero_keyword_candidates_for_parent(monkeypatch):
    async def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(matcher, "runtime_execute_text_completion", fail)
    names = [schema("create_scheduled_job", "Create recurring Workspace work")]
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "do it by itself at dawn", "kinds": ["tool"]},
        tool_schemas=names, available_tool_names=["create_scheduled_job"],
    ))
    assert result["discovery"]["assessment_status"] == "unmatched"
    assert result["discovery"]["candidates"][0]["name"] == "create_scheduled_job"
    assert result["discovery"]["capability_availability"] == "not_established"


@pytest.mark.asyncio
async def test_manor_actions_are_found_by_manor_search_not_search_tools(monkeypatch):
    from packages.core.ai.tools.manor_tool import MANOR_SCHEMA, _manor_handler

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "list scheduled jobs automations", "kinds": ["tool"]},
        tool_schemas=[("manor", MANOR_SCHEMA)], available_tool_names=["manor"],
    ))
    assert not any(str(match.get("catalog_id") or "").startswith("manor_action:") for match in result["matches"])
    assert "manor(action='search')" in result["discovery"]["manor_platform"]
    manor = json.loads(await _manor_handler(action="search", query="list scheduled jobs automations"))
    assert manor["matches"][0]["call"] == {"tool": "manor", "arguments": {"action": "list_scheduled_jobs"}}


@pytest.mark.asyncio
async def test_exact_composite_lookup_does_not_invoke_matcher(monkeypatch):
    from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
    from unittest.mock import AsyncMock

    complete = AsyncMock(side_effect=AssertionError("Exact lookup is deterministic"))
    monkeypatch.setattr(matcher, "runtime_execute_text_completion", complete)
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "exact_names": ["manor.list_scheduled_jobs"], "kinds": ["tool"]},
        tool_schemas=[("manor", MANOR_SCHEMA)], available_tool_names=["manor"],
    ))
    assert result["loaded_tools"] == ["manor"]
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_cannot_expand_current_tool_scope(monkeypatch):
    names = [schema("web_search", "Public search"), schema("create_scheduled_job", "Create schedules")]
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "create schedules automation", "kinds": ["tool"], "_allowed_tool_names_from_context": ["web_search"]},
        tool_schemas=names, available_tool_names=[name for name, _ in names],
    ))
    assert result["loaded_tools"] == []
    assert "create_scheduled_job" not in json.dumps(result)


def test_disconnected_provider_is_scoped_data_not_global_failure():
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    result = runtime_search_tools_payload(
        matches=[], query="latest Instagram posts", provider_keys=["apify"],
        suppressed_mcp=[{"server_key": "apify", "reason": "not_usable"}],
    )
    assert result["ok"] is True
    assert "error" not in result
    assert result["details"]["capability_availability"] == "not_established"
    assert "callable_alternatives" not in result["details"]
