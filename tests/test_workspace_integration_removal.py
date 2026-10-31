"""Workspace integration removal changes executable scope, not shared accounts."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations


class _NoDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("this contract must not query or mutate shared resources")


def _state() -> dict:
    return operations._sync_operating_model_from_state({
        "operating_model": {},
        "services": [{"key": "tutorial_production"}],
        "capability_bindings": [],
        "integration_scope": {},
        "governance_policy": {},
        "rules": [],
    })


def _patch(state: dict, op: str, payload: dict) -> dict:
    return operations._apply_single_patch(deepcopy(state), {"op": op, "payload": payload})


def _draft(state: dict, patches: list[dict]) -> WorkspaceOperationDraft:
    return WorkspaceOperationDraft(
        id="draft", workspace_id="workspace", entity_id="entity",
        status="open", base_revision=2, current_state=state, patches=patches,
    )


@pytest.mark.parametrize("field", ["provider", "integration_key"])
def test_integration_remove_sets_canonical_workspace_policy_and_preserves_bindings(field):
    before = _state()
    before["integration_scope"] = {"disabled_providers": ["slack"]}
    before["capability_bindings"] = [{"binding_key": "voice", "provider": "elevenlabs"}]
    before = operations._sync_operating_model_from_state(before)

    after = _patch(before, "integration.remove", {field: " ElevenLabs "})

    assert after["integration_scope"] == {
        "disabled_providers": ["elevenlabs", "slack"],
    }
    assert after["operating_model"]["integration_scope"] == after["integration_scope"]
    assert after["capability_bindings"] == before["capability_bindings"]
    assert before["integration_scope"]["disabled_providers"] == ["slack"]
    diff = operations._state_diff(before, after)
    assert "integration_scope" in diff["changed_keys"]
    review = operations.operation_review_hitl_payload({
        "id": "draft", "diff": diff, "current_state": after, "patches": [],
    })
    assert review["diff"]["disabled_integrations"] == ["elevenlabs"]
    assert "elevenlabs" in review["why"]


def test_integration_enable_restores_only_the_explicit_provider():
    before = _patch(_state(), "integration.remove", {"provider": "elevenlabs"})
    before = _patch(before, "integration.remove", {"provider": "slack"})
    after = _patch(before, "integration.enable", {"integration_key": "ElevenLabs"})
    assert after["integration_scope"]["disabled_providers"] == ["slack"]
    with pytest.raises(ValueError, match="not disabled"):
        _patch(after, "integration.enable", {"provider": "elevenlabs"})


@pytest.mark.parametrize("op", ["operating_model.update", "operating_model.replace"])
@pytest.mark.parametrize("model", [None, [], "invalid"])
def test_malformed_whole_model_cannot_clear_disabled_integration_policy(op, model):
    before = _patch(_state(), "integration.remove", {"provider": "elevenlabs"})
    with pytest.raises(ValueError, match="operating_model must be an object"):
        _patch(before, op, {"operating_model": model})
    assert before["integration_scope"]["disabled_providers"] == ["elevenlabs"]


@pytest.mark.parametrize("payload", [{}, {"provider": []}, {"provider": ""}])
def test_integration_remove_requires_one_explicit_provider(payload):
    with pytest.raises(ValueError, match="requires.*provider"):
        _patch(_state(), "integration.remove", payload)


@pytest.mark.parametrize("field", ["service_key", "agent_id", "agent_subscription_id", "task_id"])
def test_workspace_integration_remove_rejects_a_narrower_target(field):
    with pytest.raises(ValueError, match="workspace-wide"):
        _patch(_state(), "integration.remove", {"provider": "elevenlabs", field: "foreign"})


@pytest.mark.parametrize("payload", [
    {},
    {"owner_scope": "workspace_agent", "capability_type": "mcp", "integration_key": "elevenlabs"},
    {"binding_key": "missing"},
])
def test_capability_remove_cannot_report_success_without_an_exact_existing_binding(payload):
    with pytest.raises(ValueError, match="binding_key|integration.remove"):
        _patch(_state(), "capability_binding.remove", payload)


def test_exact_capability_remove_keeps_other_bindings():
    before = _state()
    before["capability_bindings"] = [{"binding_key": "voice"}, {"binding_key": "publish"}]
    after = _patch(before, "capability_binding.remove", {"binding_key": "voice"})
    assert after["capability_bindings"] == [{"binding_key": "publish"}]


@pytest.mark.asyncio
async def test_repeated_integration_removal_is_a_noop_not_an_applyable_change():
    before = _patch(_state(), "integration.remove", {"provider": "elevenlabs"})
    after = _patch(before, "integration.remove", {"provider": "elevenlabs"})
    draft = _draft(after, [{"op": "integration.remove", "payload": {"provider": "elevenlabs"}}])

    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)

    assert validation["valid"] is False
    assert any(error.get("code") == "no_changes" and error["path"] == "patches" for error in validation["errors"])


@pytest.mark.asyncio
async def test_noop_apply_does_not_advance_revision_or_mark_draft_applied(monkeypatch):
    state = _state()
    draft = _draft(state, [{"op": "integration_scope.update", "payload": {}}])
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=2)
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(return_value=state))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())

    with pytest.raises(operations.OperationValidationError):
        await operations.apply_operation_draft(
            _NoDb(), "draft", "entity", "workspace", user_confirmation=True,
        )

    assert workspace.operation_revision == 2
    assert draft.status == "open"
    assert draft.applied_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    {"automations": [{"key": "daily_tutorial", "enabled": False}]},
    {"primary_work": "Produce tutorials without voiceover"},
])
async def test_real_operating_model_changes_are_not_rejected_as_noops(update):
    before = _state()
    after = _patch(before, "operating_model.update", update)
    draft = _draft(after, [{"op": "operating_model.update", "payload": update}])
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    assert validation["valid"] is True, validation
    assert operations._state_diff(before, after)["changed_keys"]


def test_strategist_declared_inventory_ignores_disabled_legacy_flags():
    from packages.core.strategist.context import _workspace_declared_provider_keys

    workspace = SimpleNamespace(
        settings={"flagged_integrations": [{"provider": "ElevenLabs"}, {"provider": "slack"}]},
        operating_model={"integration_scope": {"disabled_providers": ["elevenlabs"]}},
    )
    assert _workspace_declared_provider_keys(workspace, []) == {"slack"}


@pytest.mark.asyncio
async def test_strategist_fallback_cannot_reintroduce_a_disabled_connected_account(monkeypatch):
    from packages.core.strategist import context

    workspace = SimpleNamespace(
        id="workspace", entity_id="entity", settings={},
        operating_model={"integration_scope": {"disabled_providers": ["elevenlabs"]}},
    )
    for name in (
        "_list_subscriptions", "_agents_by_id", "_installed_workspace_flows", "_active_goals",
        "_recent_tasks", "_recent_plans", "_recent_activity", "_open_proposed_tasks",
        "_open_proposed_items", "_recent_proposal_outcomes", "_gather_memory",
        "_gather_operating_memory", "_knowledge_nets", "_governance_policy",
        "_calibration_stats", "_recent_runtime_evidence", "_learning_candidates", "_workspace_evaluation",
        "_declined_human_requests", "_open_tasks",
    ):
        monkeypatch.setattr(context, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(context, "_active_provider_keys", AsyncMock(return_value={"elevenlabs", "slack"}))
    monkeypatch.setattr(
        "packages.core.strategist.execution_snapshot.gather_execution_snapshot",
        AsyncMock(return_value={}),
    )
    readiness = SimpleNamespace(
        configured_channels=[], missing_channel_requirements=[], missing_setup_keys=[], as_dict=lambda: {},
    )
    readiness_check = AsyncMock(return_value=readiness)
    monkeypatch.setattr(context, "check_workspace_readiness", readiness_check)

    result = await context.gather_context(_NoDb(), workspace)

    assert result.configured_integrations == ["slack"]
    assert readiness_check.call_args.kwargs["active_provider_keys"] == {"slack"}


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_goal_collection_cannot_call_a_workspace_disabled_provider(monkeypatch, automatic):
    from packages.core.goals import measurement
    from packages.core.services import workspace_integration_scope

    goal = SimpleNamespace(
        id="goal", entity_id="entity", workspace_id="workspace", status="active",
        measurement_source={"provider": "ElevenLabs"}, metric_key="metric", stat_id=None,
        baseline_value=0, current_value=0, target_value=100, deadline=None, created_at=None,
    )
    workspace = SimpleNamespace(status="active", heartbeat_enabled=True, deleted_at=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: goal),
        SimpleNamespace(scalar_one_or_none=lambda: workspace),
    ]))
    policy = AsyncMock(return_value=frozenset({"elevenlabs"}))
    monkeypatch.setattr(workspace_integration_scope, "load_disabled_workspace_providers", policy)
    monkeypatch.setattr(measurement, "_maybe_simulate", AsyncMock(return_value=None))
    resolve_account = AsyncMock(side_effect=AssertionError("disabled provider must not resolve credentials"))
    monkeypatch.setattr(measurement, "_resolve_integration", resolve_account)

    with pytest.raises(measurement.MeasurementError, match="disabled in this workspace"):
        await measurement.measure_goal("goal", db, require_autonomous_runtime=automatic)

    policy.assert_awaited_once_with(db, workspace_id="workspace", entity_id="entity")
    resolve_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_stat_collection_cannot_call_a_workspace_disabled_provider(monkeypatch):
    from packages.core.services import workspace_integration_scope
    from packages.core.stats import service

    stat = SimpleNamespace(
        workspace_id="workspace", entity_id="entity",
        collector_config={"integration_key": "twitter_x", "metric_key": "follower_count"},
    )
    monkeypatch.setattr(service, "get_stat_integration_spec", lambda _: SimpleNamespace(provider_key="twitter_x"))
    policy = AsyncMock(return_value=frozenset({"twitter_x"}))
    monkeypatch.setattr(workspace_integration_scope, "load_disabled_workspace_providers", policy)
    db = _NoDb()

    with pytest.raises(service.StatError, match="disabled in this workspace"):
        await service._collect_integration(db, stat)

    policy.assert_awaited_once_with(db, workspace_id="workspace", entity_id="entity")


_INVENTED_SCOPE = {
    "rules": "unchanged", "scope": "current_workspace_only", "automations": "unchanged",
    "credentials": "unchanged", "remove_bindings": ["elevenlabs"],
    "other_workspaces": "unchanged", "preserve_integrations": ["notion"],
    "remove_required_connections": ["elevenlabs"],
}


@pytest.mark.parametrize("op", ["integration_scope.update", "integration.update"])
@pytest.mark.parametrize("extra", [_INVENTED_SCOPE, {"account_mode": "default"}, {"remove_bindings": []}])
def test_scope_updates_reject_invented_instructions_without_mutation(op, extra):
    state = _patch(_state(), "integration.remove", {"provider": "slack"})
    before = deepcopy(state)
    with pytest.raises(ValueError, match="unsupported fields") as exc:
        operations._apply_single_patch(state, {"op": op, "payload": extra})
    assert '"op":"integration.remove"' in str(exc.value)
    assert state == before


@pytest.mark.parametrize("op", ["operating_model.update", "operating_model.replace", "workspace_model.update", "workspace_model.replace"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_bulk_model_update_cannot_smuggle_invented_scope_fields(op, wrapped):
    state = _patch(_state(), "integration.remove", {"provider": "slack"})
    before = deepcopy(state)
    payload = {"integration_scope": {"disabled_providers": ["elevenlabs"], **_INVENTED_SCOPE}}
    if wrapped:
        payload = {"operating_model": payload}
    with pytest.raises(ValueError, match="unsupported fields"):
        operations._apply_single_patch(state, {"op": op, "payload": payload})
    assert state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_persisted_fake_scope_draft_is_invalid_before_approval_or_apply(monkeypatch, nested):
    from packages.core.ai.runtime.workspace_operation_actions import _operation_review_or_needs_fix

    before = _state()
    after = deepcopy(before)
    container = after["operating_model"] if nested else after
    container["integration_scope"] = {"disabled_providers": [], **_INVENTED_SCOPE}
    draft = _draft(after, [{"op": "integration_scope.update", "payload": _INVENTED_SCOPE}])
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    assert validation["valid"] is False
    assert any(error.get("code") == "invalid_integration_scope" for error in validation["errors"])
    review = _operation_review_or_needs_fix({"id": draft.id, "validation": validation}, prompt="Apply?", content={})
    assert review["status"] == "needs_fix"
    assert "approval_card" not in review
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=2)
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(return_value=before))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    with pytest.raises(operations.OperationValidationError):
        await operations.apply_operation_draft(_NoDb(), draft.id, "entity", "workspace", user_confirmation=True)
    assert workspace.operation_revision == 2 and draft.status == "open"


@pytest.mark.parametrize("op", ["integration.remove", "integration.enable"])
@pytest.mark.parametrize("extra", [{"preserve_integrations": ["notion"]}, {"credentials": "unchanged"}, {"service_key": ""}])
def test_provider_operation_rejects_unknown_fields_even_when_other_fields_are_valid(op, extra):
    state = _patch(_state(), "integration.remove", {"provider": "elevenlabs"})
    before = deepcopy(state)
    with pytest.raises(ValueError, match="workspace-wide") as exc:
        operations._apply_single_patch(state, {"op": op, "payload": {"provider": "elevenlabs", **extra}})
    assert '"payload":{"provider":"elevenlabs"}' in str(exc.value)
    assert state == before


@pytest.mark.parametrize("op", ["integration_scope.update", "integration.remove", "operating_model.update"])
@pytest.mark.parametrize("payload", [[], "unchanged", False])
def test_malformed_patch_payload_is_not_silently_converted_to_empty_config(op, payload):
    with pytest.raises(ValueError, match="payload must be an object"):
        operations._apply_single_patch(_state(), {"op": op, "payload": payload})


@pytest.mark.asyncio
@pytest.mark.parametrize("before_scope,after_scope", [
    ({}, {"disabled_providers": []}),
    ({"disabled_providers": []}, {}),
    ({"disabled_providers": ["Twitter", "slack"]}, {"disabled_providers": ["slack", "x"]}),
])
async def test_equivalent_scope_representations_do_not_create_a_fake_effect(before_scope, after_scope):
    before = _state()
    before["integration_scope"] = before_scope
    before = operations._sync_operating_model_from_state(before)
    after = _patch(before, "operating_model.update", {"integration_scope": after_scope})
    draft = _draft(after, [{"op": "operating_model.update", "payload": {"integration_scope": after_scope}}])
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    assert validation["valid"] is False
    assert any(error.get("code") == "no_changes" for error in validation["errors"])


def test_both_agent_schemas_advertise_exact_executable_removal_shape():
    from packages.core.ai.tools.workspace_agent_tools import WORKSPACE_AGENT_SCHEMA, WORKSPACE_OPERATION_SCHEMA

    descriptions = [
        WORKSPACE_AGENT_SCHEMA["function"]["parameters"]["properties"]["params"]["description"],
        WORKSPACE_OPERATION_SCHEMA["function"]["parameters"]["properties"]["patches"]["description"],
    ]
    for description in descriptions:
        assert '"patches":[{"op":"integration.remove","payload":{"provider":"elevenlabs"}}]' in description
        assert "The draft already names the Workspace; omit scope" in description
        assert "remove_bindings" in description and "invalid" in description
        assert "integration.enable alone" in description
        assert "do not add capability bindings" in description
