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
    runtime_execute_search_tools_handler,
    runtime_search_tool_candidates,
    runtime_search_tools_schema,
)
from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload
from packages.core.ai.tool_pool import ToolPool


@pytest.fixture(autouse=True)
def _default_account_policy_for_fixture_ids(monkeypatch):
    """Hand-built envelopes use the fixture entity "entity", which has no stored account.

    Give it the default (permissive) account policy so discovery's policy check
    sees a real chain; real seeded accounts keep using the stored policy.
    """
    from packages.core.governance import approval_policy_service
    from packages.core.governance.approval_policy import ApprovalScope

    real_load = approval_policy_service.load_policy_chain

    async def load(db, *, entity_id, workspace_id=None, user_id=None, room_id=None):
        if entity_id == "entity":
            return approval_policy_service.build_policy_chain(
                scope=ApprovalScope(kind="account", id=entity_id, entity_id=entity_id),
                root=approval_policy_service.account_defaults(),
            )
        return await real_load(db, entity_id=entity_id, workspace_id=workspace_id,
                               user_id=user_id, room_id=room_id)

    monkeypatch.setattr(approval_policy_service, "load_policy_chain", load)


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


def _semantic_selection(monkeypatch, *ids):
    """Fix the ranked shortlist so these tests cover the assembly after it."""
    from packages.core.ai.runtime.capability_matcher import CapabilitySelection

    def select(**_kwargs):
        return CapabilitySelection("ranked", tuple({"id": key, "assessment": {
            "source": "lexical_rank", "fit": "unassessed", "rank": rank, "reason": "test selection",
        }} for rank, key in enumerate(ids)))

    monkeypatch.setattr("packages.core.ai.runtime.tool_search.rank_runtime_capabilities", select)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chrome", "gmail"])
@pytest.mark.parametrize("mode", ["semantic", "exact", "outside_provider"])
@pytest.mark.parametrize("authorized", [True, False])
async def test_child_discovery_obeys_its_provider_contract(monkeypatch, provider, mode, authorized):
    from packages.core.ai.runtime import integration_provider_matcher

    wanted = f"mcp__{provider}__documentation"
    other = "mcp__stripe__documentation"
    names = [wanted, other, "read_file", "search_tools"]
    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.GLOBAL_OWNER_CHAT, entity_id="entity", user_id="owner",
        ),
        profile=RuntimeProfile.OWNER_COPILOT, entity_id="entity", user_id="owner",
        tool_names=tuple(names[2:]), allowed_tool_names=tuple(names if authorized else names[1:]),
        metadata={"prompt_skill_tool_discovery": {
            "provider_keys": [provider], "tool_prefixes": [f"mcp__{provider}__"],
        }},
    )

    async def no_provider_rerouting(**kwargs):
        raise AssertionError("An already selected single-provider Skill must not reroute")

    async def available(matches, *_args):
        return [{**match, "available": True} for match in matches]

    extra_discovery_calls = []

    async def live_loader(providers):
        extra_discovery_calls.append(providers)
        return {}

    async def skill_loader():
        extra_discovery_calls.append("skills")
        return []

    monkeypatch.setattr(integration_provider_matcher, "resolve_runtime_integration_providers", no_provider_rerouting)
    monkeypatch.setattr("packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability", available)
    request = {"mode": mode, "query": "documentation for file uploads"}
    if mode == "exact":
        request["exact_names"] = names
    elif mode == "outside_provider":
        request.update(mode="provider", provider_key="stripe")
    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={**request, "_runtime_envelope_from_context": envelope},
        tool_schemas=[_schema(name, "Read documentation for file uploads") for name in names],
        available_tool_names=names, skill_descriptor_loader=skill_loader,
        live_mcp_schema_loader=live_loader,
    ))
    found = authorized and mode != "outside_provider"
    assert result["loaded_tools"] == ([wanted] if found else [])
    assert {item["name"] for item in result["matches"]} == ({wanted} if found else set())
    assert extra_discovery_calls == []


def test_search_tools_schema_covers_tools_mcp_and_skills() -> None:
    schema = runtime_search_tools_schema()["function"]

    assert "MCP actions" in schema["description"]
    assert "reusable Skills" in schema["description"]
    assert schema["parameters"]["properties"]["kinds"]["items"]["enum"] == [
        "tool",
        "mcp_tool",
        "skill",
    ]
    assert schema["parameters"]["properties"]["mode"]["enum"] == [
        "semantic",
        "provider",
        "exact",
    ]
    rendered = json.dumps(schema)
    assert "browse_server:" not in rendered
    assert "select:" not in rendered


@pytest.mark.asyncio
async def test_exact_search_without_limit_returns_every_requested_visible_tool() -> None:
    names = [
        "search_documents", "list_documents", "ws_attach_knowledge",
        "inspect_file_engine", "ws_read_source", "glob_files",
        "read_file", "patch_file",
    ]
    common = {
        "tool_schemas": [_schema(name, f"Use {name}") for name in names],
        "available_tool_names": names,
    }

    result = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "exact_names": names},
        **common,
    ))
    assert result["loaded_tools"] == names

    limited = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "exact_names": names, "max_results": 3},
        **common,
    ))
    assert limited["loaded_tools"] == names[:3]


@pytest.mark.parametrize("query,expected", [
    ("tour", "create_showing"),
    ("book a tour", "create_showing"),
    ("available units", "list_available_units"),
    ("maintenance request", "create_maintenance_request"),
])
def test_provider_search_ranks_real_pms_operations_within_result_limit(query, expected):
    from packages.core.ai.mcp import manor_pms

    schemas = [_schema(f"mcp__manor_pms__{tool['name']}", tool["description"]) for tool in manor_pms.list_tools()]
    matches, suppressed = runtime_search_tool_candidates(
        tool_schemas=schemas, query=query, provider_keys=["manor_pms"], max_results=5,
        usable_providers=frozenset({"manor_pms"}),
    )
    assert suppressed == []
    assert len(matches) <= 5
    assert f"mcp__manor_pms__{expected}" in {match["name"] for match in matches}


@pytest.mark.parametrize("query,expected", [
    ("list applications rental applications PMS", "list_applications"),
    ("list inspections property inspection PMS", "list_inspections"),
    ("inspection unit walkthrough PMS manor", "list_inspections"),
    ("list notifications PMS", "list_notices"),
    ("list messages communication records PMS", "list_messages"),
    ("message thread tenant communication log PMS manor", "list_messages"),
])
def test_actual_browser_pms_module_queries_find_the_list_tool(query, expected):
    from packages.core.ai.mcp import manor_pms

    schemas = [_schema(f"mcp__manor_pms__{tool['name']}", tool["description"]) for tool in manor_pms.list_tools()]
    matches, suppressed = runtime_search_tool_candidates(
        # Provider ranking reserves half its pool for first-party matches;
        # the runtime searches a larger pool before showing five results.
        tool_schemas=schemas, query=query, provider_keys=["manor_pms"], max_results=10,
        usable_providers=frozenset({"manor_pms"}),
    )
    assert suppressed == []
    assert f"mcp__manor_pms__{expected}" in {match["name"] for match in matches[:5]}


def test_provider_query_ranking_does_not_widen_account_or_tool_scope():
    schemas = [
        _schema("mcp__manor_pms__create_showing", "Record a property tour"),
        _schema("mcp__manor_pms__list_units", "List available units"),
        _schema("mcp__other__create_showing", "Record a property tour"),
    ]
    matches, _ = runtime_search_tool_candidates(
        tool_schemas=schemas, query="tour", provider_keys=["manor_pms"],
        bound_tool_names={"mcp__manor_pms__list_units"},
        usable_providers=frozenset({"manor_pms"}),
    )
    assert [match["name"] for match in matches] == ["mcp__manor_pms__list_units"]
    unavailable, suppressed = runtime_search_tool_candidates(
        tool_schemas=schemas, query="tour", provider_keys=["manor_pms"],
        usable_providers=frozenset(),
    )
    assert unavailable == []
    assert suppressed[0]["reason"] == "not_usable"
    without_provider, _ = runtime_search_tool_candidates(tool_schemas=schemas, query="manor pms tour")
    assert without_provider == []


def test_unknown_provider_is_not_reported_as_disconnected_integration() -> None:
    matches, suppressed = runtime_search_tool_candidates(
        tool_schemas=[_schema("workspace_search", "Search the current workspace")],
        query="",
        provider_keys=["workspace"],
        usable_providers=frozenset(),
    )

    assert matches == []
    assert suppressed == [{
        "server_key": "workspace",
        "reason": "unknown_server",
        "similar_servers": [],
    }]
    payload = runtime_search_tools_payload(
        matches=matches,
        mode="provider",
        query="",
        provider_keys=["workspace"],
        suppressed_mcp=suppressed,
    )
    assert "Unknown server key 'workspace'" in payload["hint"]
    assert "integration_setup" not in payload
    assert "stop_parent" not in payload


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
    spoofed = SimpleNamespace(
        entity_id="entity_1",
        slug="mcp_gmail",
        name="mcp_gmail",
        config={"source": "builtin"},
    )

    assert trusted_skill_capability_companion(gmail) == (
        SkillCapabilityCompanion(tool_prefixes=("mcp__gmail__",))
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
        arguments={
            "mode": "exact",
            "exact_names": ["read_finance_ledger"],
            "max_results": 1,
        },
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
@pytest.mark.parametrize(
    "arguments",
    [
        {
            "mode": "exact",
            "exact_names": ["mcp__gmail__list_messages"],
            "max_results": 1,
        },
        {"mode": "provider", "provider_key": "gmail", "max_results": 1},
    ],
)
async def test_exact_mcp_search_adds_its_derived_integration_skill(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict,
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
        arguments=arguments,
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
@pytest.mark.oss_regression
async def test_provider_companion_keeps_core_tools_callable_when_skill_wins_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trusted companion adds bounded tools outside the search-result slots."""
    from packages.core.ai.runtime import integration_provider_matcher
    from packages.core.ai.runtime.integration_provider_matcher import (
        RuntimeIntegrationProviderMatch,
    )

    browser = SkillDescriptor(
        id="skill_fixture_browser",
        slug="fixture-browser",
        name="Fixture Browser",
        description="Operate a paired browser fixture.",
        source="builtin",
        declared_tools=(
            "mcp__fixture_browser__read_page",
            "mcp__fixture_browser__open_or_reuse",
            "mcp__fixture_browser__fill_or_select",
            "mcp__fixture_browser__upload",
        ),
        metadata={
            "category": "browser",
            "capability_companion": {"tool_prefixes": ["mcp__fixture_browser__"]},
        },
    )

    async def load_skills():
        return [browser]

    async def match_browser(**_kwargs):
        return (RuntimeIntegrationProviderMatch(provider_key="fixture_browser", confidence=0.99),)

    _semantic_selection(monkeypatch, "provider:fixture_browser", "skill:skill_fixture_browser")

    async def mark_available(matches, _entity_id, _user_id):
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(
        integration_provider_matcher,
        "resolve_runtime_integration_providers",
        match_browser,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability",
        mark_available,
    )

    payload = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": "paired browser", "max_results": 1},
        tool_schemas=[
            _schema("mcp__fixture_browser__read_page", "Read the current fixture page"),
            _schema("mcp__fixture_browser__open_or_reuse", "Open a fixture page"),
            _schema("mcp__fixture_browser__fill_or_select", "Fill a fixture form"),
            _schema("mcp__fixture_browser__upload", "Upload through the fixture browser"),
            _schema("invoke_skill", "Invoke a reusable Skill"),
        ],
        available_tool_names=(
            "mcp__fixture_browser__read_page",
            "mcp__fixture_browser__open_or_reuse",
            "mcp__fixture_browser__fill_or_select",
            "mcp__fixture_browser__upload",
            "invoke_skill",
        ),
        skill_descriptor_loader=load_skills,
    ))

    assert payload["matches"][0]["kind"] == "skill"
    assert payload["loaded_tools"] == [
        "mcp__fixture_browser__read_page",
        "mcp__fixture_browser__open_or_reuse",
        "mcp__fixture_browser__fill_or_select",
        "mcp__fixture_browser__upload",
        "invoke_skill",
    ]
    assert payload["servers"] == [{
        "key": "fixture_browser",
        "name": "fixture_browser",
        "matched_tools": 4,
        "top_tools": [
            "mcp__fixture_browser__read_page",
            "mcp__fixture_browser__open_or_reuse",
            "mcp__fixture_browser__fill_or_select",
        ],
        "total_tools": None,
    }]




@pytest.mark.asyncio
async def test_provider_without_query_loads_chrome_companion_not_alphabetical_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome = SkillDescriptor(
        id="skill_chrome",
        slug="chrome",
        name="Chrome",
        description="Operate Chrome.",
        source="builtin",
        declared_tools=(
            "mcp__chrome__read_page",
            "mcp__chrome__open_or_reuse",
            "mcp__chrome__fill_or_select",
            "mcp__chrome__upload",
        ),
        metadata={
            "category": "browser",
            "capability_companion": {"tool_prefixes": ["mcp__chrome__"]},
        },
    )

    async def load_skills():
        return [chrome]

    async def mark_available(matches, _entity_id, _user_id):
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability",
        mark_available,
    )
    names = (
        "mcp__chrome__aaa",
        "mcp__chrome__bbb",
        "mcp__chrome__ccc",
        "mcp__chrome__ddd",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__open_or_reuse",
        "mcp__chrome__read_page",
        "mcp__chrome__upload",
    )
    payload = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "provider", "provider_key": "chrome", "max_results": 1},
        tool_schemas=[_schema(name, name) for name in names],
        available_tool_names=names,
        skill_descriptor_loader=load_skills,
    ))

    assert payload["loaded_tools"] == [
        "mcp__chrome__read_page",
        "mcp__chrome__open_or_reuse",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__upload",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chrome", "gmail"])
async def test_semantic_provider_companion_precedes_lexically_stronger_unrelated_skills(monkeypatch, provider):
    from packages.core.ai.runtime import integration_provider_matcher
    from packages.core.ai.runtime.integration_provider_matcher import RuntimeIntegrationProviderMatch

    tool_name = f"mcp__{provider}__read_page"
    companion = _skill("provider_skill", provider, "Operate the connected account.", source="builtin", metadata={
        "capability_companion": {"tool_prefixes": [f"mcp__{provider}__"]},
    })
    unrelated = _skill("unrelated", "video-post", "publish video post on X twitter attaching user provided video caption")

    async def load_skills():
        return [unrelated, companion]

    async def match_provider(**_kwargs):
        return (RuntimeIntegrationProviderMatch(provider_key=provider, confidence=0.99),)

    _semantic_selection(monkeypatch, f"provider:{provider}")

    async def mark_available(matches, _entity_id, _user_id):
        return [{**match, "available": True} for match in matches]

    monkeypatch.setattr(integration_provider_matcher, "resolve_runtime_integration_providers", match_provider)
    monkeypatch.setattr("packages.core.ai.runtime.tool_search.runtime_annotate_tool_availability", mark_available)
    payload = json.loads(await runtime_execute_search_tools_handler(
        arguments={"query": f"Use {provider} to publish a video post on X twitter attaching the user provided video and caption", "max_results": 8},
        tool_schemas=[_schema(tool_name, "Read current page"), _schema("invoke_skill", "Invoke skill")],
        available_tool_names=(tool_name, "invoke_skill"), skill_descriptor_loader=load_skills,
    ))

    assert payload["matches"][0]["skill_id"] == "provider_skill"
    assert payload["matches"][0]["load_before_use"] is True
    compact = json.loads(_compact_search_tools_result_for_context(payload, payload["loaded_tools"]))
    assert compact["provider_keys"] == [provider]
    assert compact["provider_resolution"] == payload["provider_resolution"]


def test_successful_search_catalog_is_not_a_public_tool_failure():
    from packages.core.ai.runtime.output_policy import runtime_public_tool_result

    payload = runtime_search_tools_payload(matches=[{
        "kind": "mcp_tool", "name": "mcp__chrome__read_page", "available": True,
        "description": "Read the page, including error messages and retry controls.",
    }], query="Use Chrome")

    assert json.loads(runtime_public_tool_result(json.dumps(payload))) == payload


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

    assert [{key: value for key, value in match.items() if key not in {"tools", "assessment", "availability"}}
            for match in payload["matches"]] == [
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
async def test_default_search_merges_typed_tool_mcp_and_skill_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.runtime import integration_provider_matcher
    from packages.core.ai.runtime.integration_provider_matcher import (
        RuntimeIntegrationProviderMatch,
    )

    gold = _skill(
        "skill_gold",
        "gold-options",
        "Analyze gold futures options, call walls, put walls, and gamma exposure.",
    )

    async def load_skills():
        return [gold]

    async def match_broker(**_kwargs):
        return (
            RuntimeIntegrationProviderMatch(
                provider_key="broker",
                confidence=0.96,
            ),
        )

    _semantic_selection(monkeypatch, "provider:broker", "tool:gold_market_data", "skill:skill_gold")

    monkeypatch.setattr(
        integration_provider_matcher,
        "resolve_runtime_integration_providers",
        match_broker,
    )

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
def test_untrusted_skill_cannot_impersonate_chrome_companion_core_tools():
    from packages.core.ai.runtime.capability_search import runtime_companion_tool_names

    forged = SkillDescriptor(
        id="custom-skill", slug="custom-chrome", name="Chrome helper",
        description="Operate Chrome.", source="entity",
        declared_tools=("mcp__chrome__read_page", "mcp__chrome__click_element"),
        metadata={"capability_companion": {"tool_prefixes": ["mcp__chrome__"]}},
    )
    assert runtime_companion_tool_names(
        skills=[forged], matched_tool_names=["mcp__chrome__read_page"],
        available_tool_names=["mcp__chrome__read_page", "mcp__chrome__click_element"],
    ) == []


def _scoped_envelope(allowed: tuple[str, ...], *, blocked: tuple[str, ...] = ()) -> RuntimeEnvelope:
    return RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.GLOBAL_OWNER_CHAT, entity_id="entity", user_id="owner",
        ),
        profile=RuntimeProfile.OWNER_COPILOT, entity_id="entity", user_id="owner",
        tool_names=allowed, allowed_tool_names=allowed, blocked_tool_names=blocked,
    )


async def _exact_search(envelope: RuntimeEnvelope, names: list[str]) -> dict:
    registry = ["read_file", "search_tools", "generate_file", "patch_file", "list_files"]
    return json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "exact_names": names, "kinds": ["tool"],
                   "_runtime_envelope_from_context": envelope},
        tool_schemas=[_schema(name, f"Use {name}") for name in registry],
        available_tool_names=registry,
    ))


@pytest.mark.asyncio
async def test_exact_search_reports_a_registered_tool_outside_this_agents_scope() -> None:
    envelope = _scoped_envelope(("read_file", "search_tools", "acquire_capabilities"))

    result = await _exact_search(envelope, ["generate_file", "read_file", "no_such_tool"])

    assert result["loaded_tools"] == ["read_file"]
    [entry] = result["not_in_scope"]
    assert entry["name"] == "generate_file"
    assert entry["reason"] == "exists_not_bound_to_this_agent"
    assert entry["acquire_with"] == {
        "tool": "acquire_capabilities",
        "arguments": {"action": "prepare", "catalog_ids": ["tool:generate_file"]},
    }
    assert entry["otherwise"]
    # Information only: nothing out of scope is loaded or granted.
    assert "generate_file" not in result["loaded_tools"]
    assert "generate_file" not in envelope.discovered_tool_grants.tool_names
    assert "generate_file exists but is not bound to this agent" in result["hint"]
    assert "acquire_capabilities" in result["hint"]


@pytest.mark.asyncio
async def test_exact_search_offers_acquisition_only_when_this_run_can_acquire() -> None:
    result = await _exact_search(_scoped_envelope(("read_file", "search_tools")), ["generate_file"])

    assert result["matches"] == []
    [entry] = result["not_in_scope"]
    assert entry["name"] == "generate_file"
    assert "acquire_with" not in entry
    assert "acquire_capabilities" not in result["hint"]
    assert "Ask the user" in result["hint"]


@pytest.mark.asyncio
async def test_exact_search_never_offers_a_policy_blocked_tool_as_acquirable() -> None:
    envelope = _scoped_envelope(
        ("read_file", "search_tools", "acquire_capabilities", "generate_file"),
        blocked=("generate_file",),
    )

    result = await _exact_search(envelope, ["generate_file"])

    [entry] = result["not_in_scope"]
    assert entry["reason"] == "blocked_by_policy"
    assert "acquire_with" not in entry
    assert "acquire_capabilities" not in result["hint"]


@pytest.mark.asyncio
async def test_exact_search_does_not_report_unknown_or_in_scope_names() -> None:
    envelope = _scoped_envelope(("read_file", "search_tools", "acquire_capabilities"))

    result = await _exact_search(envelope, ["read_file", "no_such_tool"])

    assert "not_in_scope" not in result


@pytest.mark.asyncio
async def test_model_context_compaction_keeps_not_in_scope() -> None:
    result = await _exact_search(
        _scoped_envelope(("read_file", "search_tools", "acquire_capabilities")), ["generate_file"],
    )

    compact = json.loads(_compact_search_tools_result_for_context(result, []))

    assert compact["not_in_scope"] == result["not_in_scope"]
    assert compact["hint"] == result["hint"]


async def _skill_readiness_search(monkeypatch, *, connected: frozenset[str] | Exception) -> dict:
    from packages.core.services import agent_permission_service

    async def usable_providers(*_args, **_kwargs):
        if isinstance(connected, Exception):
            raise connected
        return connected

    monkeypatch.setattr(agent_permission_service, "resolve_usable_mcp_providers", usable_providers)
    skills = [
        SkillDescriptor(id="s-notion", slug="mcp_notion", name="Notion Pages", description="Notion pages",
                        declared_tools=("mcp__notion__search", "mcp__notion__get_page"),
                        metadata={"trusted_builtin": True, "discoverable_provider_keys": ["notion"],
                                  "discoverable_tool_prefixes": ["mcp__notion__"]}),
        SkillDescriptor(id="s-files", slug="file-maker", name="File Maker", description="Make files",
                        declared_tools=("generate_file", "patch_file")),
        SkillDescriptor(id="s-mixed", slug="mixed-reader", name="Mixed Reader", description="Read and write",
                        declared_tools=("read_file", "generate_file")),
        SkillDescriptor(id="s-read", slug="file-reader", name="File Reader", description="Read files",
                        declared_tools=("read_file",)),
        SkillDescriptor(id="s-guide", slug="plain-guide", name="Plain Guide", description="Guidance"),
    ]

    async def skill_loader():
        return skills

    registry = ["read_file", "search_tools", "invoke_skill", "generate_file", "patch_file",
                "mcp__notion__search", "mcp__notion__get_page"]
    envelope = _scoped_envelope(("read_file", "search_tools", "invoke_skill"))
    return json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "kinds": ["skill"],
                   "exact_names": ["mcp_notion", "file-maker", "mixed-reader", "file-reader", "plain-guide"],
                   "_runtime_envelope_from_context": envelope},
        entity_id="entity", user_id="owner",
        tool_schemas=[_schema(name, f"Use {name}") for name in registry],
        available_tool_names=registry,
        skill_descriptor_loader=skill_loader,
    ))


@pytest.mark.asyncio
async def test_skill_matches_report_dependency_readiness_against_the_callers_scope(monkeypatch) -> None:
    result = await _skill_readiness_search(monkeypatch, connected=frozenset())
    by_slug = {match["slug"]: match for match in result["matches"]}

    notion = by_slug["mcp_notion"]["availability"]
    assert notion["state"] == "setup_required"
    assert notion["setup_url"] == "/integrations?provider=notion"
    assert notion["missing_tools"] == ["mcp__notion__get_page", "mcp__notion__search"]

    files = by_slug["file-maker"]["availability"]
    assert files["state"] == "declared_tools_out_of_scope"
    assert files["missing_tools"] == ["generate_file", "patch_file"]

    mixed = by_slug["mixed-reader"]["availability"]
    assert mixed["state"] == "invocable_under_policy"
    assert mixed["missing_tools"] == ["generate_file"]

    for slug in ("file-reader", "plain-guide"):
        assert by_slug[slug]["availability"]["state"] == "invocable_under_policy"
        assert "missing_tools" not in by_slug[slug]["availability"]

    # Not hidden, only ordered after every invocable Skill.
    states = [match["availability"]["state"] for match in result["matches"]]
    first_blocked = states.index(next(s for s in states if s != "invocable_under_policy"))
    assert all(state != "invocable_under_policy" for state in states[first_blocked:])
    assert len(result["matches"]) == 5


@pytest.mark.asyncio
async def test_connected_mcp_skill_stays_invocable(monkeypatch) -> None:
    result = await _skill_readiness_search(monkeypatch, connected=frozenset({"notion"}))
    notion = next(match for match in result["matches"] if match["slug"] == "mcp_notion")

    assert notion["availability"]["state"] == "invocable_under_policy"
    assert "missing_tools" not in notion["availability"]


@pytest.mark.asyncio
async def test_skill_readiness_fails_open_when_provider_readiness_is_unknown(monkeypatch) -> None:
    result = await _skill_readiness_search(monkeypatch, connected=RuntimeError("registry down"))
    notion = next(match for match in result["matches"] if match["slug"] == "mcp_notion")

    assert notion["availability"]["state"] == "invocable_under_policy"


def test_skill_readiness_counts_only_work_tools() -> None:
    from packages.core.ai.runtime.skills import runtime_skill_executability, runtime_skill_from_descriptor
    from packages.core.ai.runtime.tool_search import runtime_skill_readiness

    def check(declared):
        descriptor = SkillDescriptor(id="s", slug="s", name="S", declared_tools=declared)
        executability = runtime_skill_executability(
            runtime_skill_from_descriptor(descriptor),
            allowed_tool_names={"invoke_skill", "search_tools"}, registered_tool_names=declared,
        )
        return runtime_skill_readiness(executability, connected_providers=lambda _provider: None)

    readiness = check(("invoke_skill", "search_tools", "generate_file"))
    assert readiness["state"] == "declared_tools_out_of_scope"
    assert readiness["missing_tools"] == ["generate_file"]
    assert check(("invoke_skill", "search_tools")) is None

def test_not_in_scope_hint_keeps_the_suppression_hint() -> None:
    entry = {"name": "generate_file", "reason": "exists_not_bound_to_this_agent",
             "otherwise": "Ask the user to add it to this agent's configuration."}
    suppressed = [{"server_key": "gmail", "reason": "not_usable"}]

    result = runtime_search_tools_payload(
        matches=[], query="", mode="exact", suppressed_mcp=suppressed, not_in_scope=[entry],
    )
    plain = runtime_search_tools_payload(matches=[], query="", mode="exact", suppressed_mcp=suppressed)

    assert result["hint"].startswith(plain["hint"])
    assert "generate_file exists but is not bound to this agent" in result["hint"]


async def _approval_workspace(db_session, monkeypatch):
    from approval_settings_test_utils import initialize_test_approval_owners

    from packages.core import database
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Discovery WS",
                          status="active", settings={}, operating_model={})
    db_session.add_all([
        User(id=entity_id, entity_id=entity_id, email=f"{entity_id}@discovery.example",
             password_hash="test-only", role="owner", status="active"),
        UserMembership(user_id=entity_id, entity_id=entity_id, role="owner", status="active"),
        workspace,
    ])
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=entity_id)

    class SharedSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(database, "async_session", SharedSession)
    return workspace


def _workspace_envelope(workspace, allowed: tuple[str, ...]) -> RuntimeEnvelope:
    return RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.WORKSPACE_CHAT, entity_id=workspace.entity_id, user_id=workspace.entity_id,
        ),
        profile=RuntimeProfile.OWNER_COPILOT, entity_id=workspace.entity_id, user_id=workspace.entity_id,
        workspace_id=workspace.id, tool_names=allowed, allowed_tool_names=allowed,
    )


@pytest.mark.asyncio
async def test_approval_deny_rule_blocks_acquisition_offer(db_session, monkeypatch) -> None:
    from approval_settings_test_utils import action_rule, set_workspace_test_approval

    from packages.core.governance import approval_policy_service

    workspace = await _approval_workspace(db_session, monkeypatch)
    await set_workspace_test_approval(
        db_session, workspace_id=workspace.id, entity_id=workspace.entity_id, actor_id=workspace.entity_id,
        user_rules=[action_rule("workspace.file.create", effect="deny")],
    )
    loads = []
    real_load = approval_policy_service.load_policy_chain

    async def counting_load(*args, **kwargs):
        loads.append(kwargs.get("workspace_id"))
        return await real_load(*args, **kwargs)

    monkeypatch.setattr(approval_policy_service, "load_policy_chain", counting_load)

    result = await _exact_search(
        _workspace_envelope(workspace, ("read_file", "search_tools", "acquire_capabilities")),
        ["generate_file", "patch_file"],
    )

    by_name = {entry["name"]: entry for entry in result["not_in_scope"]}
    assert by_name["generate_file"]["reason"] == "blocked_by_policy"
    assert "acquire_with" not in by_name["generate_file"]
    # Only the denied action is blocked; an allowed one stays acquirable.
    assert by_name["patch_file"]["reason"] == "exists_not_bound_to_this_agent"
    assert by_name["patch_file"]["acquire_with"]["arguments"]["catalog_ids"] == ["tool:patch_file"]
    assert loads == [workspace.id]  # loaded once per search call


@pytest.mark.asyncio
async def test_approval_ask_rule_still_offers_acquisition(db_session, monkeypatch) -> None:
    from approval_settings_test_utils import action_rule, set_workspace_test_approval

    workspace = await _approval_workspace(db_session, monkeypatch)
    await set_workspace_test_approval(
        db_session, workspace_id=workspace.id, entity_id=workspace.entity_id, actor_id=workspace.entity_id,
        user_rules=[action_rule("workspace.file.create", effect="ask")],
    )

    result = await _exact_search(
        _workspace_envelope(workspace, ("read_file", "search_tools", "acquire_capabilities")),
        ["generate_file"],
    )

    [entry] = result["not_in_scope"]
    assert entry["reason"] == "exists_not_bound_to_this_agent"
    assert "acquire_with" in entry


@pytest.mark.parametrize("profile", ["EXTERNAL_CUSTOMER_SAFE", "EXTERNAL_CHANNEL_SAFE"])
@pytest.mark.asyncio
async def test_external_profiles_get_no_not_in_scope(profile) -> None:
    from dataclasses import replace

    envelope = replace(
        _scoped_envelope(("read_file", "search_tools", "acquire_capabilities")),
        profile=getattr(RuntimeProfile, profile),
    )

    result = await _exact_search(envelope, ["generate_file"])

    assert "not_in_scope" not in result
    assert "generate_file" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unreadable_workspace_policy_withholds_acquisition_without_claiming_a_block(monkeypatch) -> None:
    from dataclasses import replace

    from packages.core.ai.runtime import tool_search
    from packages.core.governance import approval_policy_service

    async def broken_policy(*_args, **_kwargs):
        raise RuntimeError("policy store down")

    async def no_disabled_providers(*_args, **_kwargs):
        return frozenset()

    monkeypatch.setattr(approval_policy_service, "load_policy_chain", broken_policy)
    monkeypatch.setattr(tool_search, "load_disabled_workspace_providers", no_disabled_providers)
    envelope = replace(
        _scoped_envelope(("read_file", "search_tools", "acquire_capabilities")), workspace_id="ws-1",
    )

    result = await _exact_search(envelope, ["generate_file"])

    [entry] = result["not_in_scope"]
    assert entry["reason"] == "exists_not_bound_to_this_agent"
    assert "acquire_with" not in entry


async def _exact_search_with_registry(envelope: RuntimeEnvelope, names: list[str], registry: list[str]) -> dict:
    return json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "exact_names": names, "kinds": ["tool"],
                   "_runtime_envelope_from_context": envelope},
        tool_schemas=[_schema(name, f"Use {name}") for name in registry],
        available_tool_names=registry,
    ))


@pytest.mark.asyncio
async def test_out_of_scope_manor_action_points_to_the_manor_tool() -> None:
    registry = ["read_file", "search_tools", "manor", "get_goal_status", "generate_file"]
    envelope = _scoped_envelope(("read_file", "search_tools", "manor", "acquire_capabilities"))

    result = await _exact_search_with_registry(envelope, ["get_goal_status", "generate_file"], registry)

    by_name = {entry["name"]: entry for entry in result["not_in_scope"]}
    goal = by_name["get_goal_status"]
    assert goal["reason"] == "available_via_manor"
    assert goal["call_with"] == {"tool": "manor", "arguments": {"action": "get_goal_status"}}
    assert "acquire_with" not in goal
    assert "manor(action='get_goal_status')" in result["hint"]
    assert "acquire_with" in by_name["generate_file"]


@pytest.mark.asyncio
async def test_manor_action_without_manor_in_scope_is_acquirable() -> None:
    registry = ["read_file", "search_tools", "manor", "get_goal_status"]
    envelope = _scoped_envelope(("read_file", "search_tools", "acquire_capabilities"))

    result = await _exact_search_with_registry(envelope, ["get_goal_status"], registry)

    [entry] = result["not_in_scope"]
    assert entry["reason"] == "exists_not_bound_to_this_agent"
    assert entry["acquire_with"]["arguments"]["catalog_ids"] == ["tool:get_goal_status"]


@pytest.mark.parametrize("name,bindable", [
    ("generate_file", True), ("ws_attach_knowledge", False), ("mcp__gmail__send_message", False), ("", False),
])
def test_catalog_bindable_rule_is_shared(name, bindable):
    from packages.core.ai.runtime.tool_visibility import runtime_tool_is_catalog_bindable

    assert runtime_tool_is_catalog_bindable(name) is bindable


@pytest.mark.asyncio
async def test_workspace_tool_outside_scope_is_not_offered_for_acquisition() -> None:
    registry = ["read_file", "search_tools", "ws_attach_knowledge"]
    envelope = _scoped_envelope(("read_file", "search_tools", "acquire_capabilities"))

    result = await _exact_search_with_registry(envelope, ["ws_attach_knowledge"], registry)

    [entry] = result["not_in_scope"]
    assert "acquire_with" not in entry


@pytest.mark.asyncio
async def test_manor_route_inside_a_workspace_uses_the_workspace_action(monkeypatch) -> None:
    from dataclasses import replace

    from packages.core.ai.runtime import tool_search

    async def no_disabled_providers(*_args, **_kwargs):
        return frozenset()

    monkeypatch.setattr(tool_search, "load_disabled_workspace_providers", no_disabled_providers)
    registry = ["read_file", "search_tools", "manor", "get_goal_status"]
    envelope = replace(_scoped_envelope(("read_file", "search_tools", "manor")), workspace_id="ws-1")

    result = await _exact_search_with_registry(envelope, ["get_goal_status"], registry)

    [entry] = result["not_in_scope"]
    assert entry["reason"] == "available_via_manor"
    assert entry["call_with"]["arguments"]["action"] == "workspace"
    assert entry["call_with"]["arguments"]["params"]["action"] == "get_goal_status"



@pytest.mark.asyncio
async def test_account_deny_rule_blocks_acquisition_without_a_workspace(db_session, monkeypatch) -> None:
    from approval_settings_test_utils import action_rule, initialize_test_account_approval

    from packages.core import database
    from packages.core.models.base import generate_ulid

    entity_id = generate_ulid()
    actor = await initialize_test_account_approval(
        db_session, entity_id=entity_id, user_rules=[action_rule("workspace.file.create", effect="deny")],
    )

    class SharedSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(database, "async_session", SharedSession)
    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.GLOBAL_OWNER_CHAT, entity_id=entity_id, user_id=actor.id,
        ),
        profile=RuntimeProfile.OWNER_COPILOT, entity_id=entity_id, user_id=actor.id,
        tool_names=("read_file", "search_tools"), allowed_tool_names=("read_file", "search_tools"),
    )

    result = await _exact_search(envelope, ["generate_file", "patch_file"])

    by_name = {entry["name"]: entry for entry in result["not_in_scope"]}
    assert by_name["generate_file"]["reason"] == "blocked_by_policy"
    assert by_name["patch_file"]["reason"] == "exists_not_bound_to_this_agent"


@pytest.mark.asyncio
async def test_workspace_risk_ceiling_blocks_acquisition_offer(db_session, monkeypatch) -> None:
    from packages.core.governance.policy import WorkspacePolicy
    from packages.core.governance.service import update_policy

    workspace = await _approval_workspace(db_session, monkeypatch)
    await update_policy(db_session, entity_id=workspace.entity_id, workspace_id=workspace.id,
                        policy=WorkspacePolicy(max_risk_level="low"), changed_by=workspace.entity_id)

    result = await _exact_search(
        _workspace_envelope(workspace, ("read_file", "search_tools", "acquire_capabilities")),
        ["generate_file", "list_files"],
    )

    by_name = {entry["name"]: entry for entry in result["not_in_scope"]}
    # generate_file is a medium-risk action above the Workspace's low ceiling.
    assert by_name["generate_file"]["reason"] == "blocked_by_policy"
    assert "acquire_with" not in by_name["generate_file"]
    assert "acquire_with" in by_name["list_files"]


@pytest.mark.parametrize("tool,expected", [
    ("read_file", {"impact": "read", "operation": "read", "risk_level": "low"}),
    ("generate_file", {"impact": None, "operation": "modify", "risk_level": "medium"}),
])
def test_policy_action_for_classification_matches_the_execution_gate(tool, expected) -> None:
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool,
        runtime_policy_action_for_classification,
    )

    action, impact = runtime_policy_action_for_classification(classify_runtime_tool(tool, {}))

    assert {"impact": impact, "operation": action.operation, "risk_level": action.risk_level} == expected


def test_manor_routes_prefer_the_manor_action_outside_shortcuts() -> None:
    from packages.core.ai.runtime.tool_search import _runtime_manor_routes

    routes = _runtime_manor_routes(in_scope=True, in_workspace=True)

    assert routes["create_task"] == {"tool": "manor", "arguments": {"action": "create_task"}}
    assert routes["get_goal_status"]["arguments"]["action"] == "workspace"


def test_partially_unconnected_skill_stays_invocable_with_a_setup_link() -> None:
    from packages.core.ai.runtime.skills import RuntimeSkillExecutability
    from packages.core.ai.runtime.tool_search import runtime_skill_readiness

    executability = RuntimeSkillExecutability(
        work_tools=("read_file", "mcp__notion__search"),
        executable_tools=("read_file", "mcp__notion__search"), missing_tools=(),
    )

    readiness = runtime_skill_readiness(
        executability, connected_providers=lambda provider: False if provider == "notion" else None,
    )

    assert readiness["state"] == "invocable_under_policy"
    assert readiness["missing_tools"] == ["mcp__notion__search"]
    assert readiness["setup_url"] == "/integrations?provider=notion"


def test_executability_ignores_declared_tools_that_are_not_registered() -> None:
    from packages.core.ai.runtime.skills import runtime_skill_executability, runtime_skill_from_descriptor

    descriptor = SkillDescriptor(id="s", slug="s", name="S", declared_tools=("ghost_tool",))

    result = runtime_skill_executability(
        runtime_skill_from_descriptor(descriptor),
        allowed_tool_names={"ghost_tool", "invoke_skill"}, registered_tool_names=("read_file",),
    )

    assert not result.executable
    assert result.missing_tools == ("ghost_tool",)


def test_skill_without_work_tools_skips_the_tool_surface(monkeypatch) -> None:
    from packages.core.ai.runtime import skills

    def no_surface(*_args, **_kwargs):
        raise AssertionError("a Skill without work tools needs no tool surface")

    monkeypatch.setattr(skills, "runtime_prepare_prompt_skill_tool_surface", no_surface)
    descriptor = SkillDescriptor(id="s", slug="s", name="S", declared_tools=("invoke_skill", "search_tools"))

    result = skills.runtime_skill_executability(
        skills.runtime_skill_from_descriptor(descriptor), allowed_tool_names={"invoke_skill"},
    )

    assert result.executable and result.work_tools == ()
