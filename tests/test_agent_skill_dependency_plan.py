"""Selected Skills close over exact actor-catalog dependencies, never grants."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services.agent_capability_catalog import (
    AgentCapabilityCandidate as Candidate,
    AgentCapabilityCatalog as Catalog,
    AgentCapabilityCatalogFactory,
    AgentCapabilityKind as Kind,
    AgentCapabilityPlanStatus as Status,
    AgentCapabilitySelectionError,
    materialize_agent_capability_plan,
)


def _tool(name):
    return Candidate(f"tool:{name}", Kind.TOOL, name, name)


def _skill(*tools, skill_id="selected"):
    return Candidate(
        f"skill:{skill_id}", Kind.SKILL, skill_id, skill_id,
        metadata={"declared_tools": tools},
    )


def _mcp(action="read", *, provider="mail", server_level=False, ready=True):
    return Candidate(
        f"mcp:{provider}" if server_level else f"mcp:{provider}:{action}",
        Kind.MCP_ACTION, provider, provider,
        readiness=Status.READY if ready else Status.SETUP_REQUIRED,
        actions=({"name": action, "tool_name": f"mcp__{provider}__{action}"},),
        metadata={
            "server_key": provider,
            **({"all_actions": True} if server_level else {"action": action}),
            "setup_required_reason": "Connect account", "setup_kind": "oauth",
        },
    )


def _catalog(*candidates):
    return Catalog((_tool("invoke_skill"), *candidates))


def test_selected_skill_adds_native_dependency_without_changing_selection():
    catalog = _catalog(_tool("web_search"), _skill("web_search", "web_search"))
    plan = catalog.resolve(["skill:selected"])
    assert plan.selected_catalog_ids == ("skill:selected",)
    assert plan.skill_ids == ("selected",)
    assert plan.tool_names == ("web_search", "invoke_skill")
    assert plan.status is Status.READY
    assert catalog.resolve([]).tool_names == (), "unselected Skills cannot restore revoked tools"


def test_selected_skill_merges_only_exact_mcp_actions_with_explicit_selection():
    catalog = _catalog(
        _mcp("read"), _mcp("send"), _mcp("delete"),
        _skill("mcp__mail__read", "mcp__mail__send"),
    )
    plan = catalog.resolve(["skill:selected", "mcp:mail:read"])
    assert plan.mcp_allowed_tools == {"mail": ("read", "send")}
    assert plan.mcp_server_keys == ("mail",)
    assert plan.tool_names == ("invoke_skill",)
    assert plan.status is Status.READY
    assert catalog.resolve([]).mcp_allowed_tools == {}


def test_server_level_catalog_dependency_stays_exact_unless_explicitly_selected():
    catalog = _catalog(_mcp(server_level=True), _skill("mcp__mail__read"))
    assert catalog.resolve(["skill:selected"]).mcp_allowed_tools == {"mail": ("read",)}
    assert catalog.resolve(["skill:selected", "mcp:mail"]).mcp_allowed_tools == {"mail": None}


@pytest.mark.parametrize("dependency", ["missing_tool", "mcp__mail__delete", "mcp__mail__*"])
def test_missing_dependency_is_not_ready_and_is_never_inferred(dependency):
    catalog = _catalog(_mcp(), _tool("delete"), _skill(dependency))
    plan = catalog.resolve(["skill:selected"])
    assert plan.status is Status.SETUP_REQUIRED
    assert plan.mcp_allowed_tools == {}
    assert plan.tool_names == ("invoke_skill",)
    assert plan.setup_required == ({
        "catalog_id": "skill:selected", "skill_id": "selected", "dependency": dependency,
        "reason": f"Skill dependency is unknown or inaccessible: {dependency}",
        "setup_kind": "skill_dependency_unavailable",
    },)


def test_provider_without_action_catalog_cannot_implicitly_grant_an_action():
    provider = Candidate("mcp:mail", Kind.MCP_ACTION, "mail", "mail")
    plan = _catalog(provider, _skill("mcp__mail__read")).resolve(["skill:selected"])
    assert plan.status is Status.SETUP_REQUIRED
    assert plan.mcp_allowed_tools == {}


def test_unavailable_account_preserves_connection_setup_and_exact_action():
    plan = _catalog(_mcp(ready=False), _skill("mcp__mail__read")).resolve(["skill:selected"])
    assert plan.status is Status.SETUP_REQUIRED
    assert plan.mcp_allowed_tools == {"mail": ("read",)}
    assert plan.setup_required[0]["setup_kind"] == "oauth"


def test_catalog_scope_not_global_mcp_tool_rows_controls_dependencies():
    wrong_provider = Candidate(
        "mcp:other:read", Kind.MCP_ACTION, "other", "Other",
        actions=({"name": "read", "tool_name": "mcp__mail__read"},),
        metadata={"server_key": "other", "action": "read"},
    )
    catalog = _catalog(_tool("mcp__mail__read"), wrong_provider, _skill("mcp__mail__read"))
    plan = catalog.resolve(["skill:selected"])
    assert plan.status is Status.SETUP_REQUIRED
    assert plan.mcp_allowed_tools == {}
    assert plan.tool_names == ("invoke_skill",)


def test_required_tools_for_pending_skills_use_the_same_resolver():
    catalog = _catalog(_tool("web_search"), _mcp())
    plan = catalog.resolve([], required_tools=["web_search", "invoke_skill", "mcp__mail__read"])
    assert plan.selected_catalog_ids == ()
    assert plan.skill_ids == ()
    assert plan.tool_names == ("web_search", "invoke_skill")
    assert plan.mcp_allowed_tools == {"mail": ("read",)}
    assert plan.status is Status.READY
    missing = catalog.resolve([], required_tools=["unknown", "unknown"])
    assert len(missing.setup_required) == 1
    assert missing.setup_required[0]["source"] == "required_tools"
    assert "catalog_id" not in missing.setup_required[0]


def test_implicit_dependency_count_does_not_consume_explicit_selection_limit():
    tools = [f"dependency_{i}" for i in range(201)]
    plan = _catalog(*(_tool(name) for name in tools), _skill(*tools)).resolve(["skill:selected"])
    assert plan.selected_catalog_ids == ("skill:selected",)
    assert len(plan.tool_names) == 202
    assert plan.status is Status.READY


@pytest.mark.asyncio
async def test_materializer_rejects_missing_dependency_before_any_write():
    db = SimpleNamespace(flush=AsyncMock())
    plan = _catalog(_skill("missing")).resolve(["skill:selected"])
    with pytest.raises(AgentCapabilitySelectionError, match="dependencies.*missing"):
        await materialize_agent_capability_plan(
            db, entity_id="entity", user_id="actor", agent_id="agent", plan=plan,
        )
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_materializer_receives_closed_bindings_without_widening_mcp(monkeypatch):
    from packages.core.services import agent_provisioning_service as provisioning

    tools = AsyncMock(return_value=["web_search", "invoke_skill"])
    skills = AsyncMock(return_value=(["selected"], []))
    mcp = AsyncMock(return_value=["mail"])
    monkeypatch.setattr(provisioning, "_bind_tools", tools)
    monkeypatch.setattr(provisioning, "_bind_existing_skills", skills)
    monkeypatch.setattr(provisioning, "_bind_mcp_servers", mcp)
    plan = _catalog(_tool("web_search"), _mcp(), _skill("web_search", "mcp__mail__read")).resolve(["skill:selected"])
    await materialize_agent_capability_plan(
        SimpleNamespace(flush=AsyncMock()), entity_id="entity", user_id="actor", agent_id="agent", plan=plan,
    )
    assert tools.call_args.kwargs["tool_names"] == ["web_search", "invoke_skill"]
    assert skills.call_args.kwargs["requester_user_id"] == "actor"
    assert mcp.call_args.kwargs["allowed_tools_by_server"] == {"mail": ("read",)}
    assert mcp.call_args.kwargs["update_mode"] is provisioning.AgentMCPBindingUpdateMode.MERGE


@pytest.mark.asyncio
async def test_factory_carries_real_skill_tools_for_visible_local_and_platform_skills(monkeypatch):
    from packages.core.services import agent_capability_catalog as module
    from packages.core.services import agent_service, builtin_skill_loader, integration_resolution

    def skill(skill_id, entity):
        return SimpleNamespace(
            id=skill_id, entity_id=entity, name=skill_id, display_name=None, slug=skill_id,
            description="", system_prompt="", tools=["web_search"],
        )

    tool_rows = [SimpleNamespace(id=name, name=name, display_name=name, description="")
                 for name in ("web_search", "invoke_skill")]
    rows = iter([tool_rows, [skill("local", "entity"), skill("private", "entity"), skill("platform", None)], []])

    async def execute(_statement):
        selected = next(rows)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: selected))

    readable = AsyncMock(return_value={"local"})
    monkeypatch.setattr(module, "readable_resource_ids", readable)
    monkeypatch.setattr(module, "_ensure_integration_tool_definitions", AsyncMock(return_value=0))
    monkeypatch.setattr(agent_service, "ensure_runtime_tool_definitions", AsyncMock())
    monkeypatch.setattr(builtin_skill_loader, "seed_builtin_skills", AsyncMock())
    monkeypatch.setattr(integration_resolution, "supported_integration_provider_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(integration_resolution, "integration_provider_readiness", AsyncMock(return_value={}))
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_registered_tool_schemas", lambda: {
        row.name: {"function": {"name": row.name}} for row in tool_rows
    })
    catalog = await AgentCapabilityCatalogFactory.create(
        SimpleNamespace(execute=execute), entity_id="entity", user_id="actor",
    )
    assert readable.call_args.kwargs["user_id"] == "actor"
    assert "skill:private" not in catalog.by_id
    for skill_id in ("local", "platform"):
        assert catalog.by_id[f"skill:{skill_id}"].metadata["declared_tools"] == ("web_search",)
        plan = catalog.resolve([f"skill:{skill_id}"])
        assert plan.tool_names == ("web_search", "invoke_skill")
        assert plan.status is Status.READY
