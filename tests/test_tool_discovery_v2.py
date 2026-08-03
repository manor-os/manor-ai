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
    """Review #4: v1 let an explicit select: of an unconnected provider's
    tool through, then downstream availability annotation marked it
    unavailable -> unavailable_mcp + connect hint. The A1 pre-filter's
    hard `continue` in the select: branch regressed this to a bare
    'No tools matched' with the flag on. select: is an explicit request
    for one specific tool by name — annotation (which runs after this
    pure function, at the handler level) is the right place to gate
    availability, not silent exclusion here. browse_server:'s hard gate
    is intentionally different and stays as-is (browsing implies "show me
    this whole server", where a not_usable answer is meaningful; select:
    is "load this one exact tool," where v1 always showed it, unavailable
    or not)."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="select:mcp__twitter_x__post_tweet",
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


def test_server_score_orders_provider_groups():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    # server_index passed → a server whose name/description matches the query
    # outranks one that only has a weak tool-name hit
    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="calendar settings",
        server_index=runtime_server_index(),
    )
    assert results and results[0]["name"].startswith("mcp__manor_mcp_calendar__")


def test_browse_server_lists_one_servers_tools():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    # server_index is required to activate the browse_server: branch at all
    # (it's gated on server_index is not None so flag-off behavior is
    # byte-identical to v1 — see the tool_search.py docstring/commit message).
    results, _ = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="browse_server:manor_mcp_calendar",
        max_results=8,
        server_index=runtime_server_index(),
    )
    assert results
    assert all(r["name"].startswith("mcp__manor_mcp_calendar__") for r in results)
    # browse_server bypasses the per-provider dedup that plain keyword
    # search applies — BOTH of this server's tools appear, not just the
    # single top-scored one a normal search would surface.
    assert len(results) == 2


def test_browse_server_unknown_key_reports_close_matches():
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates
    from packages.core.ai.runtime.tool_discovery import runtime_server_index

    results, suppressed = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="browse_server:manor_calendar",  # wrong key
        server_index=runtime_server_index(),
    )
    assert results == []
    assert suppressed and suppressed[0]["reason"] == "unknown_server"
    assert suppressed[0]["server_key"] == "manor_calendar"
    # Review #5: the close-matches field is named similar_servers (holds
    # provider keys), not matched_tools (which elsewhere holds tool names).
    assert suppressed[0]["similar_servers"] == ["manor_mcp_calendar"]


def test_empty_matches_hint_is_reason_aware_for_unknown_server():
    """Review #4: the zero-matches hint must not blame generic intent
    suppression for a browse_server: unknown_server result."""
    from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload

    payload = runtime_search_tools_payload(
        matches=[],
        query="browse_server:manor_calendar",
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
        query="browse_server:twitter_x",
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


def test_browse_server_gated_off_when_server_index_absent():
    """Flag-off contract: without server_index, 'browse_server:' is not a
    special convention at all. Same "wrong key" query as the unknown-key
    test above, but with server_index omitted — the browse-specific
    unknown_server suppression shape must never appear; the query just
    falls through to plain keyword scoring instead."""
    from packages.core.ai.runtime.tool_search import runtime_search_tool_candidates

    _, suppressed = runtime_search_tool_candidates(
        tool_schemas=_synthetic_pool(),
        query="browse_server:manor_calendar",
    )
    assert not any(s.get("reason") == "unknown_server" for s in suppressed)


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


async def _set_flag(db, key: str, *, enabled: bool) -> None:
    """Canonical test-side flag setter — same pattern used by
    tests/test_proposal_items.py, tests/test_strategist_decision_loop_e2e.py,
    etc.: upsert the FeatureFlag row directly (there is no service-level
    set_flag helper; create_flag()/set_default() exist but this direct
    upsert is what every other test in this repo actually uses), then bump
    the in-process eval cache."""
    from sqlalchemy import select

    from packages.core.models.feature_flag import FeatureFlag
    from packages.core.services import feature_flags as feature_flags_service

    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(key=key, description="test", default_enabled=enabled))
    else:
        flag.default_enabled = enabled
    await db.commit()
    feature_flags_service._bump_cache()


@pytest.mark.asyncio
async def test_handler_end_to_end_with_flag_on(client: AsyncClient):
    """Real-path integration test: flag on via the real feature_flags DB
    row + the real handler, invoked with the same call shape tool_pool.py's
    _register_search_tools uses (arguments dict, not direct query/max_results
    kwargs — the plan's pseudocode signature was wrong on this point, see
    ToolPool._register_search_tools ~packages/core/ai/tool_pool.py:170-187),
    against the REAL registered tool schemas (not a synthetic pool), so this
    is the honest end-to-end check that flag -> prefilter -> server-score ->
    servers[] all thread through the real entry point."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_e2e")
    import json

    import packages.core.database as dbmod
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    pool = ToolPool()
    pool.initialize()

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "calendar settings", "max_results": 5},
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
async def test_suggestion_channel_surfaces_unconnected_strong_match(client: AsyncClient):
    """Review #3 (spec §A1 suggestion channel): flag on, twitter_x
    unconnected, query strongly names it ('post a tweet' -> 'tweet' alias).
    Before this fix, the A1 pre-filter silently dropped twitter_x entirely
    -> 'No tools matched', strictly worse than v1. After the fix: zero
    twitter tools survive into loaded_tools/available matches, but
    twitter_x shows up in unavailable_mcp with a connect hint — reusing
    the existing availability-annotation + unavailable_mcp mechanism
    end to end through the real handler, real tool pool."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_suggest")
    import json

    import packages.core.database as dbmod
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    pool = ToolPool()
    pool.initialize()

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "post a tweet", "max_results": 5},
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
    unavailable = result.get("unavailable_mcp") or []
    twitter_unavailable = [
        u for u in unavailable if u.get("server_key") == "twitter_x"
    ]
    assert twitter_unavailable, result
    assert "hint" in result
    assert "Connect the integration" in result["hint"]


@pytest.mark.asyncio
async def test_handler_select_of_unusable_provider_surfaces_unavailable_mcp(
    client: AsyncClient,
) -> None:
    """Review #4, handler level: flag on, select: of an unconnected
    provider's exact tool name -> the tool is present in unavailable_mcp
    with the connect hint (v1-parity), not silently dropped to 'No tools
    matched'."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_select_unusable")
    import json

    import packages.core.database as dbmod
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    pool = ToolPool()
    pool.initialize()

    result = json.loads(await runtime_execute_search_tools_handler(
        # The real catalog's twitter_x create-tweet tool is
        # mcp__twitter_x__create_tweet (NOT post_tweet — that name only
        # exists in this file's synthetic test pools, confirmed directly
        # against the real ToolPool catalog before writing this test).
        arguments={"query": "select:mcp__twitter_x__create_tweet", "max_results": 5},
        user_id=user_id,
        entity_id=entity_id,
        tool_schemas=pool.registered_tool_schemas(),
        available_tool_names=pool._tools.keys(),
        total_tool_count=len(pool._tools),
    ))

    assert not any(
        name == "mcp__twitter_x__create_tweet" for name in (result.get("loaded_tools") or [])
    )
    unavailable = result.get("unavailable_mcp") or []
    twitter_unavailable = [
        u for u in unavailable if u.get("name") == "mcp__twitter_x__create_tweet"
    ]
    assert twitter_unavailable, result
    assert "Connect the integration" in result.get("hint", "")


@pytest.mark.asyncio
async def test_handler_browse_server_reports_shown_vs_total_tool_count(
    client: AsyncClient,
) -> None:
    """Minor #8 (folded in): browse_server:'s response should let the model
    see it's only looking at a slice of a bigger server, not just the
    tools that happened to fit in max_results. servers[] gains
    total_tools (the server's real tool_count from runtime_server_index)
    alongside matched_tools (the shown count)."""
    _, user_id, entity_id = await _register_owner(client, "tdv2_browse_total")
    import json

    import packages.core.database as dbmod
    from packages.core.ai.runtime.tool_discovery import runtime_server_index
    from packages.core.ai.runtime.tool_search import (
        runtime_execute_search_tools_handler,
    )
    from packages.core.ai.tool_pool import ToolPool

    async with dbmod.async_session() as db:
        await _set_flag(db, "tool_discovery_v2", enabled=True)

    pool = ToolPool()
    pool.initialize()
    real_total = runtime_server_index()["manor_mcp_calendar"]["tool_count"]

    result = json.loads(await runtime_execute_search_tools_handler(
        # max_results below the real total so shown < total is observable.
        arguments={"query": "browse_server:manor_mcp_calendar", "max_results": 2},
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
