from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.agentic_loop import _compact_search_tools_result_for_context
from packages.core.ai.runtime.capability_search import (
    _runtime_skill_search_document,
    runtime_merge_capability_matches,
    runtime_search_companion_skill_candidates,
    runtime_search_skill_candidates,
)
from packages.core.ai.runtime.skills import (
    SkillDescriptor,
    _prompt_skill_discoverable_tool_names,
    descriptor_from_skill,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.skill_capability_companion import (
    SkillCapabilityCompanion,
    trusted_skill_capability_companion,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_search import (
    _runtime_partition_suggestion_overflow,
    runtime_execute_search_tools_handler,
    runtime_search_tools_schema,
)
from packages.core.ai.tool_pool import ToolPool


def _skill(
    skill_id: str,
    slug: str,
    description: str,
    *,
    source: str = "entity",
    metadata: dict | None = None,
) -> SkillDescriptor:
    return SkillDescriptor(
        id=skill_id,
        slug=slug,
        name=slug.replace("-", " ").title(),
        description=description,
        source=source,
        metadata={"category": "test", **(metadata or {})},
    )


def _schema(name: str, description: str) -> tuple[str, dict]:
    return (
        name,
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        },
    )


def test_search_tools_schema_covers_tools_mcp_and_skills() -> None:
    schema = runtime_search_tools_schema()["function"]

    assert "MCP actions" in schema["description"]
    assert "reusable Skills" in schema["description"]
    assert schema["parameters"]["properties"]["kinds"]["items"]["enum"] == [
        "tool",
        "mcp_tool",
        "skill",
    ]


def test_unavailable_mcp_suggestion_stays_outside_capability_slot_limit() -> None:
    primary, overflow = _runtime_partition_suggestion_overflow(
        [
            {"kind": "tool", "name": "search_tasks", "description": "Search tasks"},
            {
                "kind": "mcp_tool",
                "name": "mcp__twitter_x__create_tweet",
                "description": "Create a tweet",
                "available": False,
            },
        ],
        frozenset({"twitter_x"}),
    )
    visible = runtime_merge_capability_matches(
        tool_matches=primary,
        skill_matches=[
            {
                "kind": "skill",
                "name": "social-post",
                "description": "Draft a social post",
                "_search_score": 20,
            }
        ],
        query="post a tweet",
        max_results=1,
    )
    visible.extend(overflow)

    assert visible[0]["kind"] == "skill"
    assert visible[1]["name"] == "mcp__twitter_x__create_tweet"
    assert visible[1]["available"] is False


def test_equal_relevance_prefers_skill_contract_before_raw_tool() -> None:
    visible = runtime_merge_capability_matches(
        tool_matches=[
            {
                "kind": "mcp_tool",
                "name": "mcp__gmail__search_messages",
                "description": "Search Gmail messages",
            }
        ],
        skill_matches=[
            {
                "kind": "skill",
                "name": "mcp_gmail",
                "description": "Search Gmail messages",
                "_search_score": 36,
            }
        ],
        query="search gmail messages",
        max_results=1,
    )

    assert visible == [
        {
            "kind": "skill",
            "name": "mcp_gmail",
            "description": "Search Gmail messages",
        }
    ]


def test_skill_search_uses_whole_terms_and_stopwords_for_candidate_recall() -> None:
    cloud = _skill(
        "skill_cloud",
        "cloud-intro",
        "Guide to Manor Cloud features, plans, billing, and Team collaboration.",
        source="builtin",
    )
    gold = _skill(
        "skill_gold",
        "gold-options",
        "Analyze gold futures options data, call walls, put walls, and gamma exposure.",
    )

    gold_matches = runtime_search_skill_candidates(
        skills=[cloud, gold],
        query="Pull options data call wall and put wall for gold last three months",
    )
    cloud_matches = runtime_search_skill_candidates(
        skills=[cloud, gold],
        query="Manor Cloud plans and features",
    )

    assert [match["skill_id"] for match in gold_matches] == ["skill_gold"]
    assert [match["skill_id"] for match in cloud_matches] == ["skill_cloud"]
    assert not runtime_search_skill_candidates(skills=[cloud], query="for call")


def test_skill_search_cache_contains_only_catalog_projection() -> None:
    skill = _skill(
        "skill_cloud",
        "cloud-intro",
        "Guide to Manor Cloud features and plans.",
        source="builtin",
    )
    _runtime_skill_search_document.cache_clear()

    runtime_search_skill_candidates(skills=[skill], query="Manor Cloud")
    first = _runtime_skill_search_document.cache_info()
    runtime_search_skill_candidates(skills=[skill], query="Manor Cloud")
    second = _runtime_skill_search_document.cache_info()

    assert first.misses == 1
    assert second.hits == 1


def test_companion_skill_matches_only_available_bound_capabilities() -> None:
    finance = _skill(
        "skill_finance",
        "finance-ledger",
        "Use the active Workspace Finance Ledger.",
        source="builtin",
        metadata={
            "capability_companion": {
                "tool_names": ["read_finance_ledger", "record_finance_ledger"]
            }
        },
    )
    chrome = _skill(
        "skill_chrome",
        "chrome",
        "Operate the user's paired Chrome extension.",
        source="builtin",
        metadata={
            "capability_companion": {
                "tool_prefixes": ["mcp__chrome__"]
            }
        },
    )

    companions = runtime_search_companion_skill_candidates(
        skills=[finance, chrome],
        tool_matches=[
            {"kind": "tool", "name": "read_finance_ledger", "available": True},
            {
                "kind": "mcp_tool",
                "name": "mcp__chrome__read_page",
                "available": False,
            },
        ],
    )

    assert [match["skill_id"] for match in companions] == ["skill_finance"]
    assert companions[0]["capability_role"] == "companion"
    assert companions[0]["companion_for"] == ["read_finance_ledger"]
    assert companions[0]["load_before_use"] is True


def test_capability_companion_config_is_strict_and_builtin_only() -> None:
    with pytest.raises(ValueError, match="unknown fields: prompt"):
        SkillCapabilityCompanion.from_config(
            {"tool_names": ["read_finance_ledger"], "prompt": "inject me"}
        )

    entity_skill = SimpleNamespace(
        entity_id="entity_1",
        config={
            "source": "builtin",
            "capability_companion": {"tool_names": ["read_finance_ledger"]},
        },
    )
    builtin_skill = SimpleNamespace(
        entity_id=None,
        config={
            "source": "builtin",
            "capability_companion": {"tool_names": ["read_finance_ledger"]},
        },
    )

    assert trusted_skill_capability_companion(entity_skill) is None
    assert trusted_skill_capability_companion(builtin_skill) == (
        SkillCapabilityCompanion(tool_names=("read_finance_ledger",))
    )
def test_integration_companion_is_derived_from_the_route_registry() -> None:
    gmail = SimpleNamespace(
        entity_id=None,
        slug="mcp_gmail",
        name="mcp_gmail",
        config={"source": "builtin"},
    )
    chrome = SimpleNamespace(
        entity_id=None,
        slug="chrome",
        name="chrome",
        config={"source": "builtin"},
    )
    spoofed = SimpleNamespace(
        entity_id="entity_1",
        slug="mcp_gmail",
        name="mcp_gmail",
        config={"source": "builtin"},
    )

    assert trusted_skill_capability_companion(gmail) == (
        SkillCapabilityCompanion(tool_prefixes=("mcp__gmail__",))
    )
    assert trusted_skill_capability_companion(chrome) == (
        SkillCapabilityCompanion(tool_prefixes=("mcp__chrome__",))
    )
    assert trusted_skill_capability_companion(spoofed) is None


def test_integration_descriptor_exports_derived_discovery_and_companion_metadata() -> None:
    skill = SimpleNamespace(
        id="skill_gmail",
        entity_id=None,
        slug="mcp_gmail",
        name="mcp_gmail",
        display_name="Gmail",
        description="Operate Gmail.",
        category="email",
        output_format="guidance",
        tools=["mcp__gmail__list_messages"],
        config={"source": "builtin"},
    )

    descriptor = descriptor_from_skill(skill)

    assert descriptor.metadata["discoverable_provider_keys"] == ("gmail",)
    assert descriptor.metadata["discoverable_tool_prefixes"] == (
        "mcp__gmail__",
    )
    assert descriptor.metadata["capability_companion"] == {
        "tool_prefixes": ["mcp__gmail__"]
    }


def test_integration_skill_discovers_new_provider_tools_without_config_duplication() -> None:
    skill = SimpleNamespace(
        entity_id=None,
        slug="mcp_gmail",
        name="mcp_gmail",
        tools=["mcp__gmail__list_messages"],
        config={"source": "builtin"},
    )

    assert _prompt_skill_discoverable_tool_names(
        skill,
        {
            "mcp__gmail__list_messages",
            "mcp__gmail__future_action",
            "mcp__github__future_action",
        },
        registered_tool_names=(
            "mcp__gmail__list_messages",
            "mcp__gmail__future_action",
            "mcp__github__future_action",
        ),
    ) == ("mcp__gmail__future_action",)


@pytest.mark.asyncio
async def test_exact_tool_search_adds_its_companion_skill_without_using_a_slot() -> None:
    finance = _skill(
        "skill_finance",
        "finance-ledger",
        "Use the active Workspace Finance Ledger.",
        source="builtin",
        metadata={
            "capability_companion": {
                "tool_names": ["read_finance_ledger", "record_finance_ledger"]
            }
        },
    )

    async def load_skills():
        return [finance]

    output = await runtime_execute_search_tools_handler(
        arguments={"query": "select:read_finance_ledger", "max_results": 1},
        tool_schemas=[
            _schema("read_finance_ledger", "Read the active finance ledger"),
            _schema("invoke_skill", "Invoke a reusable Skill"),
        ],
        available_tool_names=("read_finance_ledger", "invoke_skill"),
        skill_descriptor_loader=load_skills,
    )
    payload = json.loads(output)

    assert [match["kind"] for match in payload["matches"]] == ["tool", "skill"]
    assert payload["matches"][1]["skill_id"] == "skill_finance"
    assert payload["matches"][1]["capability_role"] == "companion"
    assert payload["matches"][1]["companion_for"] == ["read_finance_ledger"]
    assert payload["matches"][1]["load_before_use"] is True
    assert payload["loaded_tools"] == ["read_finance_ledger", "invoke_skill"]


@pytest.mark.asyncio
async def test_exact_mcp_search_adds_its_derived_integration_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gmail = descriptor_from_skill(
        SimpleNamespace(
            id="skill_gmail",
            entity_id=None,
            slug="mcp_gmail",
            name="mcp_gmail",
            display_name="Gmail",
            description="Operate Gmail.",
            category="email",
            output_format="guidance",
            tools=["mcp__gmail__list_messages"],
            config={"source": "builtin"},
        )
    )

    async def load_skills():
        return [gmail]

    async def mark_available(matches, _entity_id, _user_id):
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability",
        mark_available,
    )

    output = await runtime_execute_search_tools_handler(
        arguments={"query": "select:mcp__gmail__list_messages", "max_results": 1},
        tool_schemas=[
            _schema("mcp__gmail__list_messages", "List Gmail messages"),
            _schema("invoke_skill", "Invoke a reusable Skill"),
        ],
        available_tool_names=("mcp__gmail__list_messages", "invoke_skill"),
        skill_descriptor_loader=load_skills,
    )
    payload = json.loads(output)

    assert [match["kind"] for match in payload["matches"]] == [
        "mcp_tool",
        "skill",
    ]
    assert payload["matches"][1]["skill_id"] == "skill_gmail"
    assert payload["matches"][1]["companion_for"] == [
        "mcp__gmail__list_messages"
    ]
    assert payload["loaded_tools"] == [
        "mcp__gmail__list_messages",
        "invoke_skill",
    ]


@pytest.mark.asyncio
async def test_search_tools_returns_typed_skill_and_loads_only_invoke_schema() -> None:
    cloud = _skill(
        "skill_cloud",
        "cloud-intro",
        "Guide to Manor Cloud features, plans, billing, and settings.",
        source="builtin",
    )

    async def load_skills():
        return [cloud]

    output = await runtime_execute_search_tools_handler(
        arguments={
            "query": "Manor Cloud plans",
            "kinds": ["skill"],
        },
        tool_schemas=[
            _schema("search_tools", "Search capabilities"),
            _schema("invoke_skill", "Invoke a reusable Skill"),
        ],
        available_tool_names=("search_tools", "invoke_skill"),
        total_tool_count=2,
        skill_descriptor_loader=load_skills,
    )
    payload = json.loads(output)

    assert payload["matches"] == [
        {
            "kind": "skill",
            "name": "cloud-intro",
            "skill_id": "skill_cloud",
            "slug": "cloud-intro",
            "display_name": "Cloud Intro",
            "description": "Guide to Manor Cloud features, plans, billing, and settings.",
            "source": "builtin",
            "available": True,
            "invoke_with": "invoke_skill",
            "category": "test",
        }
    ]
    assert payload["loaded_tools"] == ["invoke_skill"]
    assert "system_prompt" not in output


@pytest.mark.asyncio
async def test_default_search_merges_typed_tool_mcp_and_skill_candidates() -> None:
    gold = _skill(
        "skill_gold",
        "gold-options",
        "Analyze gold futures options, call walls, put walls, and gamma exposure.",
    )

    async def load_skills():
        return [gold]

    output = await runtime_execute_search_tools_handler(
        arguments={"query": "gold options", "max_results": 5},
        tool_schemas=[
            _schema("gold_market_data", "Fetch gold options market data"),
            _schema("mcp__broker__gold_chain", "Read gold options chains"),
            _schema("invoke_skill", "Invoke a reusable Skill"),
        ],
        available_tool_names=(
            "gold_market_data",
            "mcp__broker__gold_chain",
            "invoke_skill",
        ),
        skill_descriptor_loader=load_skills,
    )
    payload = json.loads(output)

    kinds = {match["kind"] for match in payload["matches"]}
    assert kinds == {"tool", "mcp_tool", "skill"}
    assert "gold_market_data" in payload["loaded_tools"]
    assert "invoke_skill" in payload["loaded_tools"]
    assert "gold-options" not in payload["loaded_tools"]


@pytest.mark.asyncio
async def test_tool_pool_search_handler_loads_runtime_skill_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = _skill(
        "skill_cloud",
        "cloud-intro",
        "Guide to Manor Cloud plans and settings.",
        source="builtin",
    )
    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            entity_id="entity_1",
            user_id="user_1",
        ),
        profile=RuntimeProfile.OWNER_COPILOT,
        entity_id="entity_1",
        user_id="user_1",
        tool_names=("search_tools", "invoke_skill"),
        allowed_tool_names=("search_tools", "invoke_skill"),
    )

    async def resolve_catalog(_db, kwargs):
        assert kwargs["_runtime_envelope_from_context"] is envelope
        return [cloud]

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        "packages.core.ai.runtime.skills.runtime_searchable_skill_descriptors_from_tool_kwargs",
        resolve_catalog,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: SessionContext(),
    )
    pool = ToolPool()
    invoke_name, invoke_schema = _schema("invoke_skill", "Invoke a reusable Skill")
    pool.register(invoke_name, invoke_schema, lambda **_kwargs: "ok")
    pool._register_search_tools()

    output = await pool.get("search_tools")["handler"](
        query="Manor Cloud",
        kinds=["skill"],
        _runtime_envelope_from_context=envelope,
        _allowed_tool_names_from_context={"search_tools", "invoke_skill"},
    )
    payload = json.loads(output)

    assert [match["skill_id"] for match in payload["matches"]] == ["skill_cloud"]
    assert payload["loaded_tools"] == ["invoke_skill"]


@pytest.mark.asyncio
async def test_skill_projection_cache_cannot_widen_a_later_actor_catalog() -> None:
    catalogs = [
        [_skill("skill_a", "tenant-a-only", "Tenant A private forecasting workflow.")],
        [_skill("skill_b", "tenant-b-only", "Tenant B private forecasting workflow.")],
    ]

    async def load_skills():
        return catalogs.pop(0)

    common = {
        "arguments": {"query": "private forecasting", "kinds": ["skill"]},
        "tool_schemas": [_schema("invoke_skill", "Invoke a reusable Skill")],
        "available_tool_names": ("invoke_skill",),
        "skill_descriptor_loader": load_skills,
    }
    first = json.loads(await runtime_execute_search_tools_handler(**common))
    second = json.loads(await runtime_execute_search_tools_handler(**common))

    assert [match["skill_id"] for match in first["matches"]] == ["skill_a"]
    assert [match["skill_id"] for match in second["matches"]] == ["skill_b"]
    assert "skill_a" not in json.dumps(second)


@pytest.mark.asyncio
async def test_skill_discovery_failure_is_visible_and_loads_no_skill_tool() -> None:
    async def load_skills():
        raise RuntimeError("private database detail")

    output = await runtime_execute_search_tools_handler(
        arguments={"query": "forecasting", "kinds": ["skill"]},
        tool_schemas=[_schema("invoke_skill", "Invoke a reusable Skill")],
        available_tool_names=("invoke_skill",),
        skill_descriptor_loader=load_skills,
    )
    payload = json.loads(output)

    assert payload["matches"] == []
    assert payload["loaded_tools"] == []
    assert "could not be searched" in payload["skill_discovery_error"]
    assert "private database detail" not in output


def test_search_tools_compaction_keeps_skill_description_and_not_instructions() -> None:
    compact = json.loads(
        _compact_search_tools_result_for_context(
            {
                "query": "Manor Cloud plans",
                "matches": [
                    {
                        "kind": "skill",
                        "name": "cloud-intro",
                        "skill_id": "skill_cloud",
                        "slug": "cloud-intro",
                        "display_name": "Manor Cloud Guide",
                        "description": "Guide to Manor Cloud plans and settings.",
                        "source": "builtin",
                        "capability_role": "companion",
                        "companion_for": ["mcp__chrome__read_page"],
                        "load_before_use": True,
                        "system_prompt": "must not survive",
                    }
                ],
            },
            ["invoke_skill"],
        )
    )

    assert compact["matched_tools"] == []
    assert compact["loaded_tools"] == ["invoke_skill"]
    assert compact["matched_skills"][0]["skill_id"] == "skill_cloud"
    assert compact["matched_skills"][0]["capability_role"] == "companion"
    assert compact["matched_skills"][0]["companion_for"] == [
        "mcp__chrome__read_page"
    ]
    assert compact["matched_skills"][0]["load_before_use"] is True
    assert "Guide to Manor Cloud" in compact["matched_skills"][0]["description"]
    assert "must not survive" not in json.dumps(compact)


def test_invoke_skill_remains_directly_registered() -> None:
    pool = ToolPool()
    pool.initialize()

    schema = pool.get_schema("invoke_skill")
    assert schema is not None
    assert schema["function"]["name"] == "invoke_skill"
