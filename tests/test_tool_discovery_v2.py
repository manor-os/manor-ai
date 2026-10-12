"""Tests for Tool Discovery v2 — server-first two-level search.

See docs/superpowers/specs/2026-07-25-tool-discovery-v2-design.md (A1, A2)
and docs/superpowers/plans/2026-07-25-tool-discovery-v2-search.md.

Fixture conventions (client, _register_owner) mirror tests/test_manor_mcp_admin.py.
Pure-function tests (server index / scorer) need no client fixture at all —
no module-level cloud parametrize is required here because these tests
never go through DEPLOYMENT_MODE-gated HTTP routers;
`resolve_usable_mcp_providers` is called directly against the DB session.

This file ships in OSS, so it must not import cloud-only modules
(e.g. packages.core.models.admin) or reference the platform-admin gate —
that coverage lives in tests/test_manor_mcp_admin.py (OSS-excluded)
instead, alongside the platform-admin fixtures it already has.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict[str, str], str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": f"{username} Co",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, data["user_id"], me.json()["entity_id"]


def test_server_index_builds_from_catalog_and_schemas():
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    index = runtime_server_index()
    assert "manor_mcp_calendar" in index
    entry = index["manor_mcp_calendar"]
    assert entry["name"]  # from _MCP_CATALOG
    assert entry["description"]
    assert isinstance(entry["tool_count"], int) and entry["tool_count"] > 0
    # every key present in _SERVER_TOOL_SCHEMAS appears, even if not in catalog
    from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS
    for key in _SERVER_TOOL_SCHEMAS:
        assert key in index


def test_server_query_score_matches_name_description_aliases():
    from packages.core.ai.runtime.tool_discovery import (
        runtime_server_index,
        runtime_server_query_score,
    )

    index = runtime_server_index()
    cal = index["manor_mcp_calendar"]
    assert runtime_server_query_score(cal, "calendar booking") > 0
    assert runtime_server_query_score(cal, "quantum entanglement") == 0


@pytest.mark.asyncio
async def test_usable_providers_first_party_in_unconnected_out(client: AsyncClient):
    """OSS-safe slice of the usable-providers coverage: a first-party
    Manor MCP is usable, an unconnected external provider is not. The
    platform-admin gate variant of this test lives in
    tests/test_manor_mcp_admin.py (OSS-excluded)."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_usable")
    import packages.core.database as dbmod
    from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS
    from packages.core.services.agent_permission_service import (
        resolve_usable_mcp_providers,
    )

    async with dbmod.async_session() as db:
        usable = await resolve_usable_mcp_providers(
            db, user_id=user_id, entity_id=entity_id,
            provider_keys=list(_SERVER_TOOL_SCHEMAS.keys()),
        )
    assert "manor_mcp_calendar" in usable   # first-party prefix
    assert "twitter_x" not in usable        # not connected


def _schema(name, desc):
    return (name, {"function": {"name": name, "description": desc}})


def _synthetic_pool():
    return [
        _schema("mcp__twitter_x__post_tweet", "Post a tweet."),
        _schema("mcp__facebook__create_post", "Create a Facebook post."),
        _schema("mcp__manor_mcp_calendar__get_calendar_settings", "Calendar settings."),
        # A second tool on the same provider — needed so browse_server's
        # "bypass the per-provider dedup" behavior (Task 4) is actually
        # observable: with only one calendar tool, browse and plain keyword
        # fallthrough would coincidentally return the same single result.
        _schema("mcp__manor_mcp_calendar__list_booking_links", "List booking links."),
        _schema("web_search", "Search the web."),
    ]


def test_prefilter_excludes_unusable_providers_from_matches():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    results, suppressed = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="post tweet",
        usable_providers=frozenset({"facebook", "manor_mcp_calendar"}),
    )
    names = [r["name"] for r in results]
    assert all(not n.startswith("mcp__twitter_x__") for n in names)


def test_select_of_unusable_provider_still_returns_the_manifest():
    """Structured exact names remain visible for availability annotation."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="",
        exact_names=["mcp__twitter_x__post_tweet"],
        usable_providers=frozenset({"facebook", "manor_mcp_calendar"}),
    )
    assert any(r["name"] == "mcp__twitter_x__post_tweet" for r in results)


def test_prefilter_none_means_v1_behavior():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    with_filter, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(), query="post tweet",
        usable_providers=None,
    )
    baseline, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(), query="post tweet",
    )
    assert [r["name"] for r in with_filter] == [r["name"] for r in baseline]


def test_server_metadata_never_routes_provider_from_query_text():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    # Even catalog names/descriptions must not become a keyword-routing path.
    # Provider discovery is owned by the worker-model matcher.
    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="calendar settings",
        server_index=runtime_server_index(),
    )
    assert not any(result["name"].startswith("mcp__") for result in results)


def test_structured_provider_search_lists_one_providers_tools():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="",
        provider_keys=["manor_mcp_calendar"],
        max_results=8,
        server_index=runtime_server_index(),
    )
    assert results
    assert all(r["name"].startswith("mcp__manor_mcp_calendar__") for r in results)
    # Provider mode bypasses per-provider semantic dedup — both tools appear.
    assert len(results) == 2


def test_provider_search_requires_exact_provider_key_not_skill_slug():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    pool = [
        _schema("mcp__gmail__list_messages", "Search Gmail messages."),
        _schema("mcp__gmail__get_message", "Read one Gmail message."),
    ]
    server_index = {
        "gmail": {
            "key": "gmail",
            "name": "Gmail",
            "description": "Read and send Gmail.",
            "aliases": ("google mail",),
            "tool_count": 2,
        }
    }

    results, suppressed = runtime_search_tool_candidates(
        tool_schemas=pool,
        query="",
        provider_keys=["mcp_gmail"],
        usable_providers=frozenset({"gmail"}),
        server_index=server_index,
    )

    assert results == []
    assert suppressed == [{
        "server_key": "mcp_gmail",
        "reason": "unknown_server",
        "similar_servers": [],
    }]


def test_provider_search_unknown_key_is_typed_without_fuzzy_guessing():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    results, suppressed = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="",
        provider_keys=["manor_calendar"],
        server_index=runtime_server_index(),
    )
    assert results == []
    assert suppressed and suppressed[0]["reason"] == "unknown_server"
    assert suppressed[0]["server_key"] == "manor_calendar"
    assert suppressed[0]["similar_servers"] == []


def test_empty_matches_hint_is_reason_aware_for_unknown_server():
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[],
        mode="provider",
        query="",
        provider_keys=["manor_calendar"],
        suppressed_mcp=[{
            "server_key": "manor_calendar",
            "reason": "unknown_server",
            "similar_servers": ["manor_mcp_calendar"],
        }],
    )
    assert "Unknown server key 'manor_calendar'" in payload["hint"]
    assert "manor_mcp_calendar" in payload["hint"]
    assert "intent" not in payload["hint"]


def test_empty_matches_hint_is_reason_aware_for_not_usable():
    """Review #4: not_usable must point at connecting the integration, not
    the generic 'doesn't match your request' intent-suppression wording."""
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[],
        mode="provider",
        query="",
        provider_keys=["twitter_x"],
        suppressed_mcp=[{
            "server_key": "twitter_x",
            "reason": "not_usable",
            "matched_tools": [],
        }],
    )
    assert "Connect the integration under Settings" in payload["hint"]
    assert "intent" not in payload["hint"]


def test_empty_matches_hint_stays_generic_for_ordinary_intent_suppression():
    """Unaffected v1 case: ordinary keyword-search intent suppression keeps
    its existing generic wording."""
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[],
        query="post a tweet",
        suppressed_mcp=[{
            "server_key": "twitter_x",
            "reason": "outside_active_user_intent",
            "matched_tools": ["mcp__twitter_x__post_tweet"],
        }],
    )
    assert "do not match the user's current request" in payload["hint"]


def test_payload_gains_servers_section():
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[{"name": "mcp__twitter_x__post_tweet", "available": True}],
        query="post tweet",
        servers=[{"key": "twitter_x", "name": "Twitter/X", "matched_tools": 1,
                  "top_tools": ["mcp__twitter_x__post_tweet"]}],
    )
    assert payload["servers"][0]["key"] == "twitter_x"
    assert payload["matches"]  # existing keys untouched
    assert "loaded_tools" in payload


@pytest.mark.parametrize(
    ("query", "provider_keys", "exact_names", "expected", "expectation"),
    [
        ("", (), ("search_tasks",), "search_tasks", "exact"),
        (
            "",
            (),
            ("query_scheduled_jobs",),
            "query_scheduled_jobs",
            "exact",
        ),
        (
            "scheduled automations with errors",
            (),
            (),
            "query_scheduled_jobs",
            "top",
        ),
        (
            "tasks filtered by status priority and deadline",
            (),
            (),
            "search_tasks",
            "top",
        ),
        ("", (), ("query_entity_agents",), "query_entity_agents", "exact"),
        (
            "agents by categories owners and deployment count",
            (),
            (),
            "query_entity_agents",
            "top",
        ),
        ("", (), ("search_documents",), "search_documents", "exact"),
        (
            "knowledge documents by file type source size and created date",
            (),
            (),
            "search_documents",
            "top",
        ),
        (
            "",
            (),
            ("list_workflow_definitions",),
            "list_workflow_definitions",
            "exact",
        ),
        (
            "workflow definitions by status category version and creator",
            (),
            (),
            "list_workflow_definitions",
            "top",
        ),
        (
            "",
            (),
            ("list_workflow_runs",),
            "list_workflow_runs",
            "exact",
        ),
        (
            "workflow run history by status trigger and attempt number",
            (),
            (),
            "list_workflow_runs",
            "top",
        ),
        ("", (), ("find_team_members",), "find_team_members", "exact"),
        (
            "team members by workspace role and last login",
            (),
            (),
            "find_team_members",
            "top",
        ),
        ("", (), ("query_ledger",), "query_ledger", "exact"),
        (
            "ledger entries with multiple filters and date range",
            (),
            (),
            "query_ledger",
            "top",
        ),
        ("", ("gmail",), (), "mcp__gmail__", "prefix"),
        (
            "",
            (),
            ("mcp__gmail__list_messages",),
            "mcp__gmail__list_messages",
            "exact",
        ),
        (
            "",
            (),
            ("mcp__google_calendar__freebusy_query",),
            "mcp__google_calendar__freebusy_query",
            "exact",
        ),
    ],
    ids=(
        "exact-task",
        "exact-automation",
        "semantic-automation",
        "semantic-task",
        "exact-agent",
        "semantic-agent-multi-filter",
        "exact-documents",
        "semantic-documents-multi-filter",
        "exact-workflow-definitions",
        "semantic-workflow-definitions",
        "exact-workflow-runs",
        "semantic-workflow-runs",
        "exact-team",
        "semantic-team",
        "exact-ledger",
        "semantic-ledger",
        "skill-slug-gmail",
        "exact-gmail-tool",
        "semantic-google-calendar",
    ),
)
def test_real_catalog_query_matrix(
    query: str,
    provider_keys: tuple[str, ...],
    exact_names: tuple[str, ...],
    expected: str,
    expectation: str,
) -> None:
    """Keep exact, semantic, and Skill-provider discovery paths accurate."""
    from packages.core.ai.runtime.tool_discovery import (
        runtime_mcp_provider_from_tool_name,
        runtime_server_index,
    )
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    tool_schemas = pool.registered_tool_schemas()
    usable_providers = frozenset(filter(None, (
        runtime_mcp_provider_from_tool_name(name)
        for name in pool._tools
    )))
    matches, _suppressed = runtime_search_tool_candidates(
        tool_schemas=tool_schemas,
        query=query,
        provider_keys=provider_keys,
        exact_names=exact_names,
        max_results=5,
        usable_providers=usable_providers,
        server_index=runtime_server_index(),
    )
    names = [match["name"] for match in matches]

    if expectation == "exact":
        assert names == [expected]
    elif expectation == "prefix":
        assert names and all(name.startswith(expected) for name in names)
    else:
        assert names and names[0] == expected


def test_exact_selector_miss_explains_non_fuzzy_recovery_path():
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[],
        mode="exact",
        query="",
        total_tool_count=100,
    )

    assert "do not use semantic matching" in payload["hint"]
    assert "semantic mode" in payload["hint"]


@pytest.mark.asyncio
async def test_handler_end_to_end_structured_provider_is_the_default(client: AsyncClient, caplog):
    """Real-path integration test through the structured provider handler.
    the same call shape tool_pool.py's
    _register_search_tools uses (arguments dict, not direct query/max_results
    kwargs — the plan's pseudocode signature was wrong on this point, see
    ToolPool._register_search_tools ~packages/core/ai/tool_pool.py:170-187),
    against the REAL registered tool schemas (not a synthetic pool), so this
    It verifies provider readiness and servers[] through the real entry point."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_e2e")
    import json

    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    caplog.set_level("INFO", logger="packages.core.ai.runtime.tool_search")
    pool = ToolPool()
    pool.initialize()

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={
            "mode": "provider",
            "provider_key": "manor_mcp_calendar",
            "max_results": 5,
        },
        user_id=user_id,
        entity_id=entity_id,
        tool_schemas=pool.registered_tool_schemas(),
        available_tool_names=pool._tools.keys(),
        total_tool_count=len(pool._tools),
    ))

    assert "servers" in result
    assert result["matches"], result
    top = result["matches"][0]["name"]
    assert top.startswith("mcp__manor_mcp_calendar__")
    # unconnected providers absent from matches
    assert all(
        not m["name"].startswith("mcp__twitter_x__") for m in result["matches"]
    )
    assert "performance" not in result  # telemetry consumes no model tokens
    performance_records = [
        record.message
        for record in caplog.records
        if record.message.startswith("tool_discovery.performance")
    ]
    assert performance_records
    assert "'version': 'v3_structured_provider'" in performance_records[-1]


def test_resolve_usable_mcp_providers_requires_provider_keys():
    """Review #1: provider_keys has no default anymore — the old fallback
    imported packages.core.ai.tools.mcp_builtin directly from this
    non-runtime service file, which is exactly the tool-to-tool import
    boundary test_production_tool_to_tool_imports_stay_runtime_owned
    enforces. Calling without it must fail loudly (TypeError), not
    silently reach for the catalog again."""
    import inspect

    from packages.core.services.agent_permission_service import (
        resolve_usable_mcp_providers,
    )

    sig = inspect.signature(resolve_usable_mcp_providers)
    assert sig.parameters["provider_keys"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_resolve_usable_mcp_providers_loads_account_registry_once(
    monkeypatch,
):
    """External providers share one actor-scoped registry snapshot; special
    providers keep their canonical decision path without concurrent use of
    the same AsyncSession."""
    from types import SimpleNamespace

    from packages.core.services import agent_permission_service as service
    from packages.core.services import integration_availability_cache
    from packages.core.services.integration_account_service import RuntimeIntegrationRegistry

    registry = RuntimeIntegrationRegistry(user_id="user-1", entity_id="entity-1", integrations=())
    registry_loads: list[tuple[str, ...]] = []
    decisions: list[tuple[str, object | None]] = []

    # The decisions are under test here, not their per-actor snapshot.
    async def no_cache(*_args):
        return None

    monkeypatch.setattr(integration_availability_cache, "current_versions", no_cache)

    monkeypatch.setattr(
        service,
        "provider_requires_integration_account_registry",
        lambda provider: provider in {"gmail", "slack"},
    )

    async def load_registry(_db, **kwargs):
        registry_loads.append(tuple(kwargs["provider_keys"]))
        return registry

    async def decide(_db, **kwargs):
        decisions.append((kwargs["provider"], kwargs.get("integration_registry")))
        return SimpleNamespace(allowed=kwargs["provider"] != "slack")

    monkeypatch.setattr(service, "load_runtime_integration_registry", load_registry)
    monkeypatch.setattr(service, "can_use_integration", decide)

    usable = await service.resolve_usable_mcp_providers(
        object(),
        user_id="user-1",
        entity_id="entity-1",
        provider_keys=["gmail", "slack", "manor_mcp_calendar", "gmail"],
    )

    assert registry_loads == [("gmail", "slack")]
    assert decisions == [
        ("gmail", registry),
        ("slack", registry),
        ("manor_mcp_calendar", registry),
    ]
    assert usable == frozenset({"gmail", "manor_mcp_calendar"})


@pytest.mark.asyncio
async def test_handler_loads_provider_scope_and_skills_in_parallel(monkeypatch):
    import asyncio
    import json

    import packages.core.database as database
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.services import agent_permission_service

    started: set[str] = set()
    both_started = asyncio.Event()

    async def rendezvous(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def usable(_db, **_kwargs):
        await rendezvous("providers")
        return frozenset()

    async def skills():
        await rendezvous("skills")
        return []

    monkeypatch.setattr(database, "async_session", SessionContext)
    monkeypatch.setattr(
        agent_permission_service,
        "resolve_usable_mcp_providers",
        usable,
    )

    payload = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "calendar"},
        entity_id="entity-1",
        user_id="user-1",
        tool_schemas=(),
        available_tool_names=(),
        skill_descriptor_loader=skills,
    ))

    assert started == {"providers", "skills"}
    assert payload["matches"] == []


@pytest.mark.asyncio
async def test_ranked_provider_surfaces_unconnected_provider(
    client: AsyncClient,
    monkeypatch,
):
    """A worker-model provider ID reaches the existing setup-card path."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_suggest")
    import json

    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.runtime import tool_search
    from packages.core.ai.runtime.capability_matcher import CapabilitySelection
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()

    def match_twitter(**_kwargs):
        return CapabilitySelection("ranked", ({"id": "provider:twitter_x", "assessment": {
            "fit": "direct", "source": "lexical_rank", "rank": 0, "reason": "publishing candidate",
            "covers": ["publish update"], "gaps": ["connection required"],
        }},))

    monkeypatch.setattr(
        tool_search,
        "rank_runtime_capabilities",
        match_twitter,
    )

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={
            "mode": "semantic",
            "query": "publish this update to my social account",
            "max_results": 5,
        },
        user_id=user_id,
        entity_id=entity_id,
        tool_schemas=pool.registered_tool_schemas(),
        available_tool_names=pool._tools.keys(),
        total_tool_count=len(pool._tools),
    ))

    loaded = result.get("loaded_tools") or []
    assert not any(name.startswith("mcp__twitter_x__") for name in loaded)
    available_matches = [
        m for m in result.get("matches") or [] if m.get("available") is not False
    ]
    assert not any(
        m["name"].startswith("mcp__twitter_x__") for m in available_matches
    )
    assert result["details"]["failure_scope"] == "provider_discovery"
    assert result["details"]["provider_selection"] == "inferred"
    assert result["details"]["unavailable_provider_keys"] == ["twitter_x"]
    assert "stop_parent" not in result and "terminal_failure" not in result
    assert result["setup_url"] == "/integrations?provider=twitter_x"


@pytest.mark.asyncio
async def test_handler_exact_name_of_unusable_provider_surfaces_unavailable_mcp(
    client: AsyncClient,
) -> None:
    """At handler level, an exact name of an unconnected
    provider's exact tool name -> the tool is present in unavailable_mcp
    with the connect hint (v1-parity), not silently dropped to 'No tools
    matched'."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_select_unusable")
    import json

    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()

    result = json.loads(await runtime_execute_search_tools_handler(
        # The real catalog's twitter_x create-tweet tool is
        # mcp__twitter_x__create_tweet (NOT post_tweet — that name only
        # exists in this file's synthetic test pools, confirmed directly
        # against the real ToolPool catalog before writing this test).
        arguments={
            "mode": "exact",
            "exact_names": ["mcp__twitter_x__create_tweet"],
            "max_results": 5,
        },
        user_id=user_id,
        entity_id=entity_id,
        tool_schemas=pool.registered_tool_schemas(),
        available_tool_names=pool._tools.keys(),
        total_tool_count=len(pool._tools),
    ))

    assert not any(
        name == "mcp__twitter_x__create_tweet" for name in (result.get("loaded_tools") or [])
    )
    assert result["ok"] is True
    assert "error" not in result
    assert result["details"]["failure_scope"] == "provider_discovery"
    assert result["details"]["unavailable_provider_keys"] == ["twitter_x"]
    assert result["setup_url"] == "/integrations?provider=twitter_x"


@pytest.mark.asyncio
async def test_handler_provider_search_reports_shown_vs_total_tool_count(
    client: AsyncClient,
) -> None:
    """Provider search lets the model
    see it's only looking at a slice of a bigger server, not just the
    tools that happened to fit in max_results. servers[] gains
    total_tools (the server's real tool_count from runtime_server_index)
    alongside matched_tools (the shown count)."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_browse_total")
    import json

    from packages.core.ai.runtime.tool_discovery import runtime_server_index
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    real_total = runtime_server_index()["manor_mcp_calendar"]["tool_count"]

    result = json.loads(await runtime_execute_search_tools_handler(
        # max_results below the real total so shown < total is observable.
        arguments={
            "mode": "provider",
            "provider_key": "manor_mcp_calendar",
            "max_results": 2,
        },
        user_id=user_id,
        entity_id=entity_id,
        tool_schemas=pool.registered_tool_schemas(),
        available_tool_names=pool._tools.keys(),
        total_tool_count=len(pool._tools),
    ))

    servers = {s["key"]: s for s in result.get("servers") or []}
    assert "manor_mcp_calendar" in servers
    entry = servers["manor_mcp_calendar"]
    assert entry["total_tools"] == real_total
    assert entry["matched_tools"] < entry["total_tools"]


@pytest.mark.asyncio
async def test_handler_v2_discovers_bound_provider_without_static_schema(
    client: AsyncClient,
    monkeypatch,
) -> None:
    _, user_id, entity_id = await _register_owner(client, "tdv2_semantic_scope")
    import json

    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_bindings import (
        RuntimeMCPProviderToolScopeFactory,
    )
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.constants.runtime_principal import RuntimePrincipalKind
    from packages.core.services import agent_permission_service

    async def usable_providers(_db, **kwargs):
        assert set(kwargs["provider_keys"]) == {"stripe"}
        return frozenset({"stripe"})

    async def annotate(matches, _entity_id, _user_id):
        for match in matches:
            match["available"] = True
        return matches

    public_name = "mcp__stripe__create_future_settlement_mandate"

    async def load_live(provider_keys):
        assert provider_keys == frozenset({"stripe"})
        return {
            "stripe": [
                (
                    public_name,
                    {
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": "Create a future settlement mandate.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                )
            ]
        }

    monkeypatch.setattr(
        agent_permission_service,
        "resolve_usable_mcp_providers",
        usable_providers,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability",
        annotate,
    )
    provider_scopes = RuntimeMCPProviderToolScopeFactory.create({"stripe": None})
    envelope = RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.DELEGATED),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        agent_id="agent-1",
        allowed_tool_names=("search_tools",),
        mcp_provider_scopes=provider_scopes,
    )

    result = json.loads(
        await runtime_execute_search_tools_handler(
            arguments={
                "mode": "provider",
                "provider_key": "stripe",
                "_runtime_envelope_from_context": envelope,
            },
            user_id=user_id,
            entity_id=entity_id,
            tool_schemas=(),
            available_tool_names=(),
            live_mcp_schema_loader=load_live,
        )
    )

    assert result["loaded_tools"] == [public_name]


@pytest.mark.asyncio
async def test_handler_v2_does_not_probe_bound_but_unusable_provider(
    client: AsyncClient,
    monkeypatch,
) -> None:
    _, user_id, entity_id = await _register_owner(client, "tdv2_unusable_scope")
    import json

    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_bindings import (
        RuntimeMCPProviderToolScopeFactory,
    )
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.constants.runtime_principal import RuntimePrincipalKind
    from packages.core.services import agent_permission_service

    async def usable_providers(_db, **_kwargs):
        return frozenset()

    async def load_live(_provider_keys):
        raise AssertionError("an unusable provider must not receive tools/list")

    monkeypatch.setattr(
        agent_permission_service,
        "resolve_usable_mcp_providers",
        usable_providers,
    )
    envelope = RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.DELEGATED),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        agent_id="agent-1",
        allowed_tool_names=("search_tools",),
        mcp_provider_scopes=RuntimeMCPProviderToolScopeFactory.create(
            {"stripe": None}
        ),
    )

    result = json.loads(
        await runtime_execute_search_tools_handler(
            arguments={
                "mode": "provider",
                "provider_key": "stripe",
                "_runtime_envelope_from_context": envelope,
            },
            user_id=user_id,
            entity_id=entity_id,
            tool_schemas=(),
            available_tool_names=(),
            live_mcp_schema_loader=load_live,
        )
    )

    assert result["loaded_tools"] == []
