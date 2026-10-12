"""search_tools must not abort a turn when a usable match exists next to an
unusable provider match (2026-09-11 incident: chrome unpaired hid builtin web_search)."""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

CHROME_SKILL = {
    "kind": "skill",
    "name": "chrome",
    "skill_id": "01SKILLCHROME00000000000000",
    "slug": "chrome",
    "description": "Operate the user's local Chrome browser.",
    "tools": ["mcp__chrome__navigate", "mcp__chrome__click"],
}
WEB_SEARCH = {
    "kind": "tool",
    "name": "web_search",
    "description": "Search the web for current information.",
    "available": True,
    "ready": True,
}
SUPPRESSED = [{"server_key": "chrome", "reason": "not_usable"}]


def _payload(matches):
    return runtime_search_tools_payload(
        matches=matches,
        mode="semantic",
        query="给所有agent配置 Chrome MCP 和搜索工具",
        provider_keys=("chrome",),
        suppressed_mcp=SUPPRESSED,
    )


def test_usable_builtin_match_survives_unusable_provider():
    payload = _payload([CHROME_SKILL, WEB_SEARCH])

    assert "error" not in payload
    assert "stop_parent" not in payload
    assert "terminal_failure" not in payload
    assert payload["unavailable_provider_keys"] == ["chrome"]
    assert [m["name"] for m in payload["matches"]] == ["chrome", "web_search"]
    assert "web_search" in payload["hint"]
    assert "chrome" in payload["hint"]


def test_all_matches_unusable_reports_scoped_nonterminal_discovery():
    payload = _payload([CHROME_SKILL])

    assert payload["ok"] is True
    assert "error" not in payload
    assert "terminal_failure" not in payload
    assert "stop_parent" not in payload
    assert "stop_reason" not in payload
    assert payload["details"]["provider_selection"] == "inferred"
    assert payload["details"]["failure_scope"] == "provider_discovery"
    assert payload["details"]["capability_availability"] == "not_established"
    assert payload["details"]["callable_candidates"] == []


def test_no_provider_keys_is_untouched():
    payload = runtime_search_tools_payload(
        matches=[CHROME_SKILL, WEB_SEARCH],
        mode="semantic",
        query="q",
        provider_keys=(),
        suppressed_mcp=SUPPRESSED,
    )

    assert "error" not in payload
    assert "unavailable_provider_keys" not in payload


def test_skill_without_provider_signal_is_not_counted_as_usable():
    unknown_skill = {"kind": "skill", "name": "mystery", "skill_id": "01SKILLMYSTERY0000000000000"}
    payload = _payload([CHROME_SKILL, unknown_skill])

    assert payload["ok"] is True
    assert "error" not in payload
    assert payload["details"]["callable_candidates"] == []


@pytest.mark.parametrize("mode", ["provider", "exact"])
def test_explicit_provider_failure_retains_requirement_without_stopping_loop(mode):
    payload = runtime_search_tools_payload(
        matches=[], mode=mode, query="", provider_keys=["chrome"],
        suppressed_mcp=SUPPRESSED,
    )
    assert payload["details"]["provider_selection"] == "explicit"
    assert payload["details"]["unavailable_provider_keys"] == ["chrome"]
    assert payload["details"]["callable_candidates"] == []
    assert "does not authorize replacing a provider explicitly required by the user" in payload["hint"]
    assert not {"stop_parent", "terminal_failure", "recommended_next_action"} & payload.keys()


@pytest.mark.parametrize("has_alternative", [False, True])
def test_discovery_scope_survives_model_context_compaction(has_alternative):
    from packages.core.ai.agentic_loop import _compact_search_tools_result_for_context
    from packages.core.ai.runtime.tool_error_contract import model_tool_error_result

    payload = _payload([WEB_SEARCH] if has_alternative else [])
    compact = json.loads(_compact_search_tools_result_for_context(payload, payload["loaded_tools"]))
    assert compact["details"] == payload["details"]
    if not has_alternative:
        assert model_tool_error_result("search_tools", compact, max_chars=12000) is None


def _schema(name, description):
    return name, {"function": {"name": name, "description": description}}


@pytest.mark.parametrize("provider_selection,expected", [
    ("inferred", ["generate_file"]), ("explicit", []),
])
def test_unavailable_provider_only_suppresses_its_own_semantic_candidates(provider_selection, expected):
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    matches, suppressed = runtime_search_tool_candidates(
        tool_schemas=[
            _schema("generate_file", "Generate narration audio"),
            _schema("private_tool", "Generate narration audio"),
            _schema("mcp__speech_vendor__speak", "Generate narration audio"),
        ],
        query="narration audio", provider_keys=["speech_vendor"],
        provider_selection=provider_selection,
        bound_tool_names={"generate_file", "mcp__speech_vendor__speak"},
        usable_providers=frozenset(),
    )
    assert [match["name"] for match in matches] == expected
    assert suppressed == [{"server_key": "speech_vendor", "reason": "not_usable", "matched_tools": []}]


def test_unavailable_inferred_provider_does_not_hide_ready_provider_or_widen_scope():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    matches, suppressed = runtime_search_tool_candidates(
        tool_schemas=[
            _schema("generate_file", "Generate narration audio"),
            _schema("mcp__missing__speak", "Generate narration audio"),
            _schema("mcp__ready__speak", "Generate narration audio"),
            _schema("mcp__unselected__speak", "Generate narration audio"),
        ],
        query="narration audio", provider_keys=["missing", "ready"],
        provider_selection="inferred", usable_providers=frozenset({"ready", "unselected"}),
    )
    assert {match["name"] for match in matches} == {"generate_file", "mcp__ready__speak"}
    assert [item["server_key"] for item in suppressed] == ["missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,query,expected", [
    ("semantic", "narration audio", ["generate_file"]),
    ("semantic", "add Chinese dubbing to the video", []),
    ("provider", "narration audio", []),
])
async def test_handler_provider_inference_is_diagnostic_not_required_dependency(monkeypatch, mode, query, expected):
    import packages.core.database as database
    from packages.core.ai.runtime import tool_search
    from packages.core.ai.runtime.capability_matcher import CapabilitySelection, candidate_id
    from packages.core.services import agent_permission_service

    class Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def usable(*_args, **_kwargs):
        return frozenset()

    def assess_capabilities(*, query, candidates, **_kwargs):
        chosen = {"provider:elevenlabs"}
        if query == "narration audio":
            chosen.add("tool:generate_file")
        return CapabilitySelection("ranked", tuple(
            {"id": candidate_id(item), "assessment": {
                "fit": "unassessed", "source": "lexical_rank", "rank": 0,
                "reason": "relevant to narration",
            }}
            for item in candidates if candidate_id(item) in chosen
        ))

    async def mark_available(matches, *_args):
        # Only native candidates can survive the disconnected-provider filter.
        assert all(not match["name"].startswith("mcp__") for match in matches)
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(agent_permission_service, "resolve_usable_mcp_providers", usable)
    monkeypatch.setattr(tool_search, "rank_runtime_capabilities", assess_capabilities)
    monkeypatch.setattr(tool_search, "runtime_annotate_tool_availability", mark_available)
    schemas = [
        _schema("generate_file", "Generate narration audio"),
        _schema("mcp__elevenlabs__text_to_speech", "Generate narration audio"),
    ]
    payload = json.loads(await tool_search.runtime_execute_search_tools_handler(
        arguments={"mode": mode, "query": query, "provider_key": "elevenlabs" if mode == "provider" else ""},
        entity_id="entity-1", user_id="user-1", tool_schemas=schemas,
        available_tool_names=[name for name, _schema in schemas],
    ))
    assert payload["loaded_tools"] == expected
    assert payload["details"]["provider_selection"] == ("inferred" if mode == "semantic" else "explicit")
    assert payload["details"]["callable_candidates"] == expected
    assert payload["details"]["capability_availability"] == "not_established"
    assert "terminal_failure" not in payload and "stop_parent" not in payload
    assert payload["ok"] is True and "error" not in payload
