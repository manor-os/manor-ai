"""A removed Workspace provider cannot be restored by shared bindings or discovery."""
from copy import deepcopy
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.services.workspace_integration_scope import (
    disabled_workspace_providers,
    integration_provider_for_requirement,
    integration_provider_for_tool,
    load_disabled_workspace_providers,
)


def test_workspace_provider_policy_is_canonical_and_does_not_parse_rules():
    policy = {"integration_scope": {"disabled_providers": ["Twitter", "x", " ElevenLabs "]}}
    assert disabled_workspace_providers(policy) == {"twitter_x", "elevenlabs"}
    assert disabled_workspace_providers({"rules": [{"description": "Do not use ElevenLabs"}]}) == set()
    assert integration_provider_for_tool("mcp__x__post") == "twitter_x"
    assert integration_provider_for_tool("search_tools") == ""
    assert integration_provider_for_requirement({"kind": "mcp_configuration", "payload": {"server_slug": "x"}}) == "twitter_x"
    assert integration_provider_for_requirement({"kind": "channel", "payload": {"provider": "x"}}) == ""


async def test_workspace_scope_lookup_rejects_missing_or_foreign_workspace():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: None)))
    with pytest.raises(ValueError, match="unavailable"):
        await load_disabled_workspace_providers(db, workspace_id="workspace", entity_id="entity")
    query = str(db.execute.await_args.args[0])
    assert "workspaces.entity_id" in query and "workspaces.deleted_at IS NULL" in query


async def test_setup_policy_suppresses_only_removed_provider_and_preserves_indexes(monkeypatch):
    from packages.core.services import workspace_readiness as readiness

    requirements = [
        {"kind": "missing_integration", "blocking": True, "payload": {"provider": "elevenlabs"}},
        {"kind": "mcp_configuration", "blocking": True, "payload": {"server_slug": "elevenlabs"}},
        {"kind": "mcp_configuration", "blocking": True, "payload": {"server_slug": "other"}},
    ]
    workspace = SimpleNamespace(
        id="workspace", entity_id="entity",
        operating_model={"integration_scope": {"disabled_providers": ["elevenlabs"]}},
        settings={"_blueprint": {"live_setup_requirements": requirements}, "blocking_setup": {"checks": [
            {"kind": "integration_provider", "provider": "elevenlabs", "blocking": True},
        ]}},
    )
    original = deepcopy(workspace.settings)
    evaluate = AsyncMock(return_value={"ready": False, "reason": "Other binding missing"})
    monkeypatch.setattr(readiness, "_blueprint_install_todo_result", evaluate)
    report = await readiness.evaluate_workspace_blocking_setup(object(), workspace)
    evaluate.assert_awaited_once()
    assert evaluate.await_args.args[2] == requirements[2]
    assert report.details["incomplete_checks"][0]["key"] == "blueprint_install_todo:2"
    assert workspace.settings == original


async def test_disabled_last_setup_check_does_not_leave_install_time_aggregate_blocker():
    from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup

    workspace = SimpleNamespace(
        operating_model={"integration_scope": {"disabled_providers": ["elevenlabs"]}},
        settings={
            "blocking_setup": {"checks": [{"kind": "integration_provider", "provider": "elevenlabs"}]},
            "_blueprint": {"live_setup_requirements": [
                {"kind": "missing_integration", "payload": {"provider": "elevenlabs"}},
                {"kind": "blocking_setup", "payload": {}},
            ]},
        },
    )
    report = await evaluate_workspace_blocking_setup(object(), workspace)
    assert report.details["ready"] is True
    assert report.details["incomplete_checks"] == []


@pytest.mark.parametrize("is_master", [False, True])
async def test_runtime_filters_removed_provider_after_all_scope_overlays(monkeypatch, is_master):
    from packages.core.services import workspace_runtime as runtime
    from packages.core.ai.runtime import tool_registry
    from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
    from packages.core.ai.runtime.tool_visibility import resolve_runtime_tool_surface

    names = {"mcp__elevenlabs__speak", "mcp__other__read", "search_tools"}
    monkeypatch.setattr(runtime, "load_conversation_runtime_context", AsyncMock(return_value={}))
    monkeypatch.setattr(runtime, "_resolve_agent_tool_scope", AsyncMock(return_value=(
        None if is_master else names,
        None if is_master else names - {"search_tools"},
        () if is_master else (RuntimeMCPProviderToolScope("elevenlabs"), RuntimeMCPProviderToolScope("other")),
    )))
    monkeypatch.setattr(runtime, "_agent_has_visible_skills", AsyncMock(return_value=False))
    monkeypatch.setattr(runtime, "_resolve_workspace_operation_tool_scope", AsyncMock(return_value=(
        {"mcp__elevenlabs__speak"}, set(), set(), False, frozenset({"elevenlabs"}),
    )))
    monkeypatch.setattr(tool_registry, "runtime_registered_tool_names", lambda **kwargs: names - {"search_tools"})
    envelope = await runtime.resolve_workspace_runtime(
        None, entity_id="entity", workspace_id="workspace", agent_id=None if is_master else "agent",
        is_master=is_master, include_prompt_context=False,
    )
    # Native master scope stays unrestricted; MCP exposure is independently
    # governed by the filtered provider scope, not by coercing None to a set.
    if is_master:
        assert envelope.bound_tool_names is None
    else:
        assert "mcp__elevenlabs__speak" not in envelope.bound_tool_names
    visible = set(resolve_runtime_tool_surface(
        names, bound_tool_names=envelope.bound_tool_names,
        is_master=envelope.is_master, mcp_allowed_names=envelope.mcp_allowed_names,
        tool_profile=envelope.tool_profile,
    ).visible_tool_names)
    assert "mcp__elevenlabs__speak" not in visible
    assert "mcp__other__read" in visible
    assert envelope.mcp_allowed_names == {"mcp__other__read"}
    assert all(scope.provider != "elevenlabs" for scope in envelope.mcp_provider_scopes)
    # An exclusion must not destroy unrelated providers' dynamic discovery.
    assert envelope.mcp_scope_unrestricted is is_master


async def test_workspace_master_operation_mcp_preserves_owner_chat_baseline(monkeypatch):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import workspace_runtime as runtime

    monkeypatch.setattr(runtime, "load_conversation_runtime_context", AsyncMock(return_value={}))
    monkeypatch.setattr(
        runtime,
        "_resolve_agent_tool_scope",
        AsyncMock(return_value=(None, None, ())),
    )
    monkeypatch.setattr(runtime, "_agent_has_visible_skills", AsyncMock(return_value=False))
    monkeypatch.setattr(
        runtime,
        "_resolve_workspace_operation_tool_scope",
        AsyncMock(return_value=(set(), {"mcp__workspace_provider__read"}, set(), False, frozenset())),
    )

    envelope = await runtime.resolve_workspace_runtime(
        None,
        entity_id="entity",
        user_id="user",
        workspace_id="workspace",
        is_master=True,
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
        include_prompt_context=False,
    )

    assert envelope.mcp_allowed_names is None
    assert envelope.mcp_scope_unrestricted is True


@pytest.mark.parametrize("principal", ["owner", "workspace_member", "delegated", "system", "external"])
async def test_current_workspace_policy_denies_before_all_authorization_strategies(monkeypatch, principal):
    from packages.core.services import workspace_integration_scope as scope
    from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationRequest, PermissionDecision
    from packages.core.services.runtime_authorization.service import DefaultAuthorizationService

    strategy = SimpleNamespace(authorize=AsyncMock(return_value=PermissionDecision.allow()))
    factory = SimpleNamespace(create=MagicMock(return_value=strategy))
    service = DefaultAuthorizationService(runtime_factory=factory, hitl_factory=object())
    monkeypatch.setattr(service, "_workspace_exists", AsyncMock(return_value=True))
    load = AsyncMock(return_value=frozenset({"elevenlabs"}))
    monkeypatch.setattr(scope, "load_disabled_workspace_providers", load)
    request = RuntimeAuthorizationRequest.create(
        entity_id="entity", user_id="user", workspace_id="workspace",
        action_key="", capability_id="", tool_name="mcp__elevenlabs__speak",
        bound_tool_names={"mcp__elevenlabs__speak"}, principal_kind=principal,
    )
    denied = await service.authorize_runtime(object(), request)
    assert not denied.allowed
    assert denied.matched_rule == "permission.workspace.integration_disabled"
    factory.create.assert_not_called()
    load.return_value = frozenset()
    assert (await service.authorize_runtime(object(), request)).allowed
    assert load.await_count == 2  # Re-enable/retry sees durable policy, not a cached run grant.


@pytest.mark.parametrize("mode", ["provider", "exact"])
async def test_search_cannot_load_disabled_provider_from_static_or_dynamic_catalog(monkeypatch, mode):
    from packages.core import database
    from packages.core.ai.runtime import tool_search, tool_discovery
    from packages.core.services import agent_permission_service

    @asynccontextmanager
    async def session():
        yield object()

    async def unchanged(matches, *_args):
        return matches

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(tool_search, "load_disabled_workspace_providers", AsyncMock(return_value=frozenset({"stripe"})))
    monkeypatch.setattr(agent_permission_service, "resolve_usable_mcp_providers", AsyncMock(return_value=frozenset({"stripe"})))
    monkeypatch.setattr(tool_search, "runtime_annotate_tool_availability", unchanged)
    monkeypatch.setattr(tool_discovery, "runtime_server_index", lambda: {"stripe": {"name": "Stripe"}})
    live_loader = AsyncMock(return_value={})
    arguments = {"workspace_id": "workspace", "mode": mode, "kinds": ["mcp_tool"]}
    if mode == "provider":
        arguments["provider_key"] = "stripe"
    else:
        arguments["exact_names"] = ["mcp__stripe__list_customers", "mcp__stripe__future_action"]
    payload = json.loads(await tool_search.runtime_execute_search_tools_handler(
        arguments=arguments, entity_id="entity", user_id="user",
        tool_schemas=[("mcp__stripe__list_customers", {"name": "mcp__stripe__list_customers", "description": "List customers"})],
        available_tool_names=["mcp__stripe__list_customers"], live_mcp_schema_loader=live_loader,
    ))
    assert payload["matches"] == []
    assert payload["loaded_tools"] == []
    live_loader.assert_not_awaited()


async def test_search_fails_closed_when_workspace_policy_cannot_be_read(monkeypatch):
    from packages.core import database
    from packages.core.ai.runtime import tool_search

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(tool_search, "load_disabled_workspace_providers", AsyncMock(side_effect=ValueError("deleted")))
    live_loader = AsyncMock()
    payload = json.loads(await tool_search.runtime_execute_search_tools_handler(
        arguments={"workspace_id": "workspace", "mode": "provider", "provider_key": "stripe"},
        entity_id="entity", user_id="user", tool_schemas=[], available_tool_names=[],
        live_mcp_schema_loader=live_loader,
    ))
    assert payload["error"] == "workspace_integration_scope_unavailable"
    live_loader.assert_not_awaited()


@pytest.mark.parametrize("op", ["operating_model.update", "operating_model.replace", "workspace_model.update", "workspace_model.replace"])
@pytest.mark.parametrize("invalid_scope", [
    None, "invalid", ["invalid"], False,
    {"disabled_providers": None}, {"disabled_providers": "elevenlabs"},
    {"disabled_providers": [None]}, {"disabled_providers": [""]},
    {"disabled_providers": ["elevenlabs", 7]},
])
def test_operating_model_patch_rejects_raw_scope_corruption_before_it_can_reenable_integrations(op, invalid_scope):
    from packages.core.services import workspace_operation_service as operations

    state = operations._sync_operating_model_from_state({
        "operating_model": {}, "integration_scope": {"disabled_providers": ["elevenlabs"]},
    })
    before = deepcopy(state)
    with pytest.raises(ValueError, match="integration_scope|disabled_providers"):
        operations._apply_single_patch(state, {
            "op": op, "payload": {"operating_model": {"integration_scope": invalid_scope}},
        })
    assert state == before


@pytest.mark.parametrize("scope", [{}, {"disabled_providers": []}, {"disabled_providers": ["elevenlabs"]}])
def test_explicit_well_typed_scope_updates_remain_supported(scope):
    from packages.core.services import workspace_operation_service as operations

    result = operations._apply_single_patch({"operating_model": {}}, {
        "op": "operating_model.update", "payload": {"integration_scope": scope},
    })
    assert result["integration_scope"] == scope


@pytest.mark.parametrize("op", ["integration.remove", "integration.enable"])
@pytest.mark.parametrize("target", [
    {"provider": "elevenlabs", "integration_key": "slack"},
    {"provider": [], "integration_key": "elevenlabs"},
    {"provider": "elevenlabs", "integration_key": []},
    {"provider": "elevenlabs", "owner_scope": "workspace", "scope": "agent"},
    {"provider": "elevenlabs", "owner_scope": "agent", "scope": "workspace"},
    {"provider": "elevenlabs", "binding_key": "specific_voice_binding"},
])
def test_integration_patch_rejects_conflicting_aliases_and_narrow_targets(op, target):
    from packages.core.services import workspace_operation_service as operations

    state = operations._sync_operating_model_from_state({
        "operating_model": {}, "integration_scope": {"disabled_providers": ["elevenlabs"]},
    })
    before = deepcopy(state)
    with pytest.raises(ValueError):
        operations._apply_single_patch(state, {"op": op, "payload": target})
    assert state == before


def test_integration_provider_aliases_can_agree_on_one_canonical_target():
    from packages.core.services import workspace_operation_service as operations

    state = operations._apply_single_patch({"operating_model": {}}, {
        "op": "integration.remove", "payload": {"provider": "Twitter", "integration_key": "x"},
    })
    assert state["integration_scope"]["disabled_providers"] == ["twitter_x"]


def test_review_uses_authoritative_policy_diff_instead_of_stale_draft_policy():
    from packages.core.governance import DEFAULT_POLICY, policy_to_dict
    from packages.core.models.workspace import WorkspaceOperationDraft
    from packages.core.services import workspace_operation_service as operations

    policy = policy_to_dict(DEFAULT_POLICY)
    policy["approval"] = {"mode": None, "rules": [{"id": "old-ban", "effect": "deny",
        "selector": {"kind": "action", "action_key": "old.block"}}]}
    before = operations._sync_operating_model_from_state({
        "operating_model": {}, "rules": [], "governance_policy": policy,
    })
    patch = {"op": "operating_model.update", "payload": {"rules": [{
        "rule_key": "new", "description": "Block new action", "rule_type": "never_allow",
        "approval_selectors": [{"kind": "action", "action_key": "new.block"}],
    }]}}
    after = operations._apply_single_patch(deepcopy(before), patch)
    current = deepcopy(before)
    current["governance_policy"]["approval"]["rules"] = [{"id": "live-ban", "effect": "deny",
        "selector": {"kind": "action", "action_key": "live.block"}}]
    current = operations._sync_operating_model_from_state(current)
    draft = WorkspaceOperationDraft(current_state=after, patches=[patch])
    diff = operations._operation_state_diff(current, draft)
    from packages.core.governance.approval_projection import policy_override
    after_policy = policy_override(diff["changes"]["governance_policy"]["after"])
    assert {rule.selector.action_key for rule in after_policy.rules if rule.effect == "deny"} == {"live.block", "new.block"}
    assert operations.operation_review_diff({
        "current_state": after, "patches": [patch], "diff": diff,
    })["removed_hard_blocks"] == []
