from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.completions import RuntimeTextCompletionResult
from packages.core.ai.runtime.integration_provider_matcher import (
    RuntimeIntegrationProviderCandidate,
    parse_runtime_integration_provider_matches,
    resolve_runtime_integration_providers,
    runtime_integration_provider_candidates,
)
from packages.core.ai.runtime.sources import RUNTIME_INTERNAL_WORKER_SOURCE


def _candidate(provider_key: str, description: str = "") -> RuntimeIntegrationProviderCandidate:
    return RuntimeIntegrationProviderCandidate(
        provider_key=provider_key,
        name=provider_key.replace("_", " ").title(),
        description=description,
    )


def test_provider_candidates_use_catalog_metadata_without_alias_lists() -> None:
    candidates = runtime_integration_provider_candidates(
        server_index={
            "future_crm_v9": {
                "name": "Future CRM",
                "description": "Customer relationship records and outreach",
                "aliases": ("sales words must not be copied",),
            }
        },
        tool_schemas=[],
        provider_keys=["future_crm_v9"],
    )

    assert candidates == (
        RuntimeIntegrationProviderCandidate(
            provider_key="future_crm_v9",
            name="Future CRM",
            description="Customer relationship records and outreach",
        ),
    )
    assert "sales words must not be copied" not in candidates[0].description


def test_provider_match_parser_requires_exact_catalog_ids() -> None:
    candidates = (_candidate("future_crm_v9"),)

    with pytest.raises(ValueError, match="outside the catalog"):
        parse_runtime_integration_provider_matches(
            '{"matches":[{"provider_key":"invented_crm","confidence":0.99}]}',
            candidates=candidates,
        )


def test_provider_match_parser_drops_low_confidence_without_guessing() -> None:
    candidates = (_candidate("gmail"), _candidate("linkedin"))

    matches = parse_runtime_integration_provider_matches(
        json.dumps({
            "matches": [
                {"provider_key": "gmail", "confidence": 0.95},
                {"provider_key": "linkedin", "confidence": 0.4},
            ]
        }),
        candidates=candidates,
    )

    assert [(match.provider_key, match.confidence) for match in matches] == [
        ("gmail", 0.95)
    ]


@pytest.mark.asyncio
async def test_provider_matcher_uses_worker_model_and_structured_catalog(monkeypatch) -> None:
    from packages.core.ai.runtime import integration_provider_matcher as matcher

    captured = {}

    async def fake_completion(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return RuntimeTextCompletionResult(
            content=json.dumps({
                "matches": [
                    {"provider_key": "future_crm_v9", "confidence": 0.93}
                ]
            }),
            usage={},
        )

    monkeypatch.setattr(matcher, "runtime_execute_text_completion", fake_completion)
    matches = await resolve_runtime_integration_providers(
        intent_text="sync Emmanuel profile to the customer relationship system",
        candidates=(
            _candidate(
                "future_crm_v9",
                "Customer relationship records and outreach",
            ),
        ),
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        conversation_id="conversation-1",
    )

    assert [match.provider_key for match in matches] == ["future_crm_v9"]
    assert captured["kwargs"]["source"] == RUNTIME_INTERNAL_WORKER_SOURCE
    assert captured["kwargs"]["reasoning_effort"] == "none"
    assert captured["kwargs"]["temperature"] == 0.0
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}
    assert '"provider_key": "future_crm_v9"' in captured["messages"][1]["content"]


@pytest.mark.asyncio
async def test_provider_matcher_failure_returns_no_route(monkeypatch) -> None:
    from packages.core.ai.runtime import integration_provider_matcher as matcher

    async def broken_completion(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(matcher, "runtime_execute_text_completion", broken_completion)

    assert await resolve_runtime_integration_providers(
        intent_text="sync the customer record",
        candidates=(_candidate("future_crm_v9"),),
    ) == ()


@pytest.mark.asyncio
async def test_tool_search_routes_through_the_ranked_provider(
    monkeypatch,
) -> None:
    from packages.core.ai.runtime import tool_discovery, tool_search
    from packages.core.ai.runtime.capability_matcher import CapabilitySelection

    tool_name = "mcp__future_crm_v9__upsert_contact"
    schema = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": "Create or update a customer record",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    monkeypatch.setattr(tool_discovery, "runtime_server_index", lambda: {
        "future_crm_v9": {
            "name": "Future CRM",
            "description": "Customer relationship records and outreach",
        },
    })
    captured = {}

    def assess(**kwargs):
        captured.update(kwargs)
        return CapabilitySelection("ranked", ({
            "id": "provider:future_crm_v9",
            "assessment": {"fit": "direct", "source": "lexical_rank", "rank": 0,
                           "reason": "customer records", "covers": ["sync contact"], "gaps": []},
        },))

    async def available(matches, *_args):
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(tool_search, "rank_runtime_capabilities", assess)
    monkeypatch.setattr(tool_search, "runtime_annotate_tool_availability", available)

    result = json.loads(await tool_search.runtime_execute_search_tools_handler(
        arguments={"mode": "semantic", "query": "sync Emmanuel profile to the customer relationship system"},
        tool_schemas=((tool_name, schema),), available_tool_names=(tool_name,),
    ))

    assert captured["query"] == "sync Emmanuel profile to the customer relationship system"
    assert [candidate["name"] for candidate in captured["candidates"]
            if candidate["kind"] == "provider"] == ["future_crm_v9"]
    assert [match["name"] for match in result["matches"]] == [tool_name]
