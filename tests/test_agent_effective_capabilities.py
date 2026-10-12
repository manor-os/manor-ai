"""One scheduled-service capability projection; never a configuration grant."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.planning import runtime_planner_capability_catalog
from packages.core.ai.runtime.strategist import (
    runtime_strategist_services_block,
    runtime_strategist_task_capabilities_block,
)
from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
from packages.core.plans.planner import _Context, _require_task_capabilities, TaskCapabilityGapError
from packages.core.services.agent_effective_capabilities import (
    AgentEffectiveCapabilityFactory,
    EffectiveAgentCapabilities,
    _filter_service_provider_actions_to_runtime_scope,
)


def _subscription(key, agent_id="agent"):
    return SimpleNamespace(id=f"sub-{key}", service_key=key, agent_id=agent_id, config={})


@pytest.fixture
def projection_runtime(monkeypatch):
    from packages.core.ai import runtime
    from packages.core.services import agent_effective_capabilities as projection
    from packages.core.services import workspace_runtime
    from packages.core.services.agent_capability_catalog import AgentCapabilityCatalogFactory

    scopes = {}
    calls = []

    async def resolve(_db, **kwargs):
        calls.append(kwargs)
        key = kwargs["agent_subscription_id"]
        tools, providers = scopes[key]
        return SimpleNamespace(
            workspace_id=kwargs["workspace_id"], task_id=kwargs["task_id"],
            thread_ref_kind=None, thread_ref_id=None, is_master=False,
            bound_tool_names=set(tools), mcp_allowed_names=set(),
            mcp_provider_scopes=providers, mcp_scope_unrestricted=False,
            tool_profile="workspace_agent", tool_scope_errors=(),
        )

    def prepare(_request, **kwargs):
        return SimpleNamespace(
            allowed_tool_names=set(kwargs["bound_tool_names"]), blocked_tool_names=set(),
        )

    def no_catalog(*_args, **_kwargs):
        pytest.fail("effective scope must not seed/mutate the configuration catalog")

    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", resolve)
    monkeypatch.setattr(runtime, "runtime_prepare_agent_tool_surface_for_turn", prepare)
    monkeypatch.setattr(runtime, "runtime_request_for_chat_turn", lambda **kwargs: kwargs)
    monkeypatch.setattr(AgentCapabilityCatalogFactory, "create", no_catalog)
    provider_lookup = AsyncMock(return_value=({}, {}))
    monkeypatch.setattr(projection, "_compute_service_provider_actions", provider_lookup)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    agent = SimpleNamespace(id="agent", name="Shared worker", status="active")
    return SimpleNamespace(
        db=db, scopes=scopes, calls=calls, providers=provider_lookup,
        agents={"agent": agent}, resolve=resolve,
    )


async def _project(state, subscriptions):
    return await AgentEffectiveCapabilityFactory.create(
        state.db, entity_id="entity", user_id="actor", workspace_id="workspace",
        subscriptions=subscriptions, agents_by_id=state.agents, task_id="task",
    )


@pytest.mark.asyncio
async def test_same_agent_different_subscriptions_do_not_share_capabilities(projection_runtime):
    state = projection_runtime
    state.scopes.update({"sub-reader": ({"web_search"}, ()), "sub-writer": ({"generate_file"}, ())})
    scopes = await _project(state, [_subscription("reader"), _subscription("writer")])

    assert "web.safe_search" in scopes["reader"].capability_ids
    assert "file.write" not in scopes["reader"].capability_ids
    assert "file.write" in scopes["writer"].capability_ids
    assert "web.safe_search" not in scopes["writer"].capability_ids
    assert {call["agent_subscription_id"] for call in state.calls} == {"sub-reader", "sub-writer"}
    assert all(call["user_id"] == "actor" and call["task_id"] == "task" for call in state.calls)
    assert all(call["runtime_surface"] == "scheduled_agent_run" for call in state.calls)


@pytest.mark.asyncio
async def test_revoked_tool_is_removed_on_next_projection(projection_runtime):
    state = projection_runtime
    state.scopes["sub-reader"] = ({"web_search"}, ())
    first = await _project(state, [_subscription("reader")])
    state.scopes["sub-reader"] = (set(), ())
    second = await _project(state, [_subscription("reader")])
    assert "web.safe_search" in first["reader"].capability_ids
    assert "web.safe_search" not in second["reader"].capability_ids
    assert len(state.calls) == 2


@pytest.mark.asyncio
async def test_missing_agent_never_looks_available(projection_runtime):
    state = projection_runtime
    scopes = await _project(state, [_subscription("missing", "other-agent")])
    assert not scopes["missing"].available
    assert scopes["missing"].capability_ids == ()
    assert scopes["missing"].prompt_dict()["scope_status"] == "unavailable"
    assert state.calls == []


@pytest.mark.asyncio
async def test_runtime_resolution_failure_is_not_an_available_scope(projection_runtime, monkeypatch):
    from packages.core.services import workspace_runtime
    state = projection_runtime
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", AsyncMock(side_effect=RuntimeError("read failed")))
    with pytest.raises(RuntimeError, match="read failed"):
        await _project(state, [_subscription("reader")])


@pytest.mark.asyncio
async def test_binding_read_error_does_not_advertise_implicit_tools(projection_runtime, monkeypatch):
    from packages.core.services import workspace_runtime
    state = projection_runtime
    state.scopes["sub-reader"] = ({"web_search"}, ())

    async def failed_scope(*args, **kwargs):
        resolved = await state.resolve(*args, **kwargs)
        resolved.tool_scope_errors = ("agent_tool_bindings",)
        return resolved

    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", failed_scope)
    scopes = await _project(state, [_subscription("reader")])
    assert not scopes["reader"].available
    assert scopes["reader"].capability_ids == ()
    assert scopes["reader"].tool_names == ()
    assert scopes["reader"].errors == ("agent_tool_bindings",)


@pytest.mark.asyncio
async def test_runtime_scope_error_projection_preserves_legacy_helper_shape(monkeypatch):
    from packages.core.ai.runtime.tool_bindings import RuntimeAgentToolScope
    from packages.core.services import workspace_runtime
    monkeypatch.setattr(workspace_runtime, "runtime_agent_tool_scope", AsyncMock(return_value=RuntimeAgentToolScope(
        bound_tool_names=frozenset(), mcp_allowed_names=frozenset(),
        errors=("agent_mcp_scope",),
    )))
    errors = []
    result = await workspace_runtime._resolve_agent_tool_scope(
        object(), agent_id="agent", is_master=False, errors=errors,
    )
    assert result == (set(), set(), ())
    assert errors == ["agent_mcp_scope"]
    assert await workspace_runtime._resolve_agent_tool_scope(
        object(), agent_id="agent", is_master=False,
    ) == result


@pytest.mark.asyncio
async def test_foreign_agent_is_not_inspected_or_exposed(projection_runtime):
    state = projection_runtime
    state.agents["agent"].entity_id = "another-entity"
    scopes = await _project(state, [_subscription("reader")])
    assert not scopes["reader"].available
    assert scopes["reader"].agent_name == ""
    assert state.calls == []
    assert state.providers.call_args.kwargs["agents_by_id"] == {}


@pytest.mark.asyncio
async def test_skill_capability_requires_executable_invoke_skill(projection_runtime):
    state = projection_runtime
    skill = SimpleNamespace(slug="transcribe", name="Transcribe", description="Read media")
    state.db.execute.return_value = SimpleNamespace(all=lambda: [("agent", skill)])
    state.scopes["sub-reader"] = (set(), ())
    unbound = await _project(state, [_subscription("reader")])
    assert unbound["reader"].skills[0]["slug"] == "transcribe"
    assert "skill.invoke" not in unbound["reader"].capability_ids
    state.scopes["sub-reader"] = ({"invoke_skill"}, ())
    bound = await _project(state, [_subscription("reader")])
    assert bound["reader"].skills[0]["slug"] == "transcribe"
    assert "skill.invoke" in bound["reader"].capability_ids


@pytest.mark.asyncio
async def test_mcp_actions_preserve_per_service_scope_and_schema(projection_runtime):
    state = projection_runtime
    state.scopes.update({
        "sub-reader": (set(), (RuntimeMCPProviderToolScope("demo", frozenset({"read"})),)),
        "sub-writer": (set(), (RuntimeMCPProviderToolScope("demo", frozenset({"write"})),)),
    })
    state.providers.return_value = (
        {key: {"demo": ["read", "write"]} for key in ("reader", "writer")},
        {key: {"demo": {"read": {"description": "Read"}, "write": {"description": "Write"}}} for key in ("reader", "writer")},
    )
    scopes = await _project(state, [_subscription("reader"), _subscription("writer")])
    assert scopes["reader"].provider_actions == {"demo": ["read"]}
    assert scopes["writer"].provider_actions == {"demo": ["write"]}
    assert scopes["reader"].provider_action_specs == {"demo": {"read": {"description": "Read"}}}


def test_explicit_block_cannot_be_reintroduced_by_provider_wildcard():
    actions, specs = _filter_service_provider_actions_to_runtime_scope(
        {"demo": ["read", "write"]}, {"demo": {"read": {}, "write": {}}},
        exact_tool_names={"mcp__demo__write"},
        provider_scopes=(RuntimeMCPProviderToolScope("demo", None),),
        blocked_tool_names=frozenset({"mcp__demo__write"}),
    )
    assert actions == {"demo": ["read"]}
    assert specs == {"demo": {"read": {}}}


def test_strategist_only_describes_projected_service_capabilities():
    sub = _subscription("reader")
    scope = EffectiveAgentCapabilities(
        "reader", sub.id, sub.agent_id, "Reader",
        capabilities=runtime_planner_capability_catalog(platform_tools=["web_search"]),
    )
    ctx = SimpleNamespace(
        subscriptions=[sub], allowed_service_keys=["reader"],
        agents_by_id={"agent": SimpleNamespace(name="Reader", config={}, category="custom", tags=[], system_prompt="Read")},
        service_capabilities={"reader": scope},
    )
    block = runtime_strategist_task_capabilities_block(ctx)
    assert "web.safe_search" in block
    assert "file.write" not in block
    assert '"scope_status": "resolved"' in runtime_strategist_services_block(ctx)
    ctx.service_capabilities = {}
    assert "web.safe_search" not in runtime_strategist_task_capabilities_block(ctx)
    assert '"scope_status":"unknown"' in runtime_strategist_services_block(ctx)


def test_planner_does_not_fall_back_to_union_when_effective_scope_is_empty():
    sub = _subscription("reader")
    ctx = _Context(
        workspace=None, subscriptions=[sub], agents_by_id={"agent": SimpleNamespace(name="Reader")},
        allowed_service_keys={"reader"}, provider_actions={},
        service_tool_names={"reader": ["web_search"]}, service_capabilities={},
    )
    task = SimpleNamespace(id="task", details={"runtime_context": {"required_capabilities": ["web.safe_search"]}})
    with pytest.raises(TaskCapabilityGapError) as exc:
        _require_task_capabilities(task, ctx)
    assert exc.value.affected_agents == ({
        "service_key": "reader", "subscription_id": "sub-reader",
        "agent_id": "agent", "agent_name": "Reader",
    },)


@pytest.mark.asyncio
async def test_duplicate_service_keys_are_not_silently_merged(projection_runtime):
    with pytest.raises(ValueError, match="unique service keys"):
        await _project(projection_runtime, [_subscription("reader"), _subscription("reader")])
