"""Review and apply must agree on the types their runtime consumers accept."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.workspace_operation_actions import _operation_review_or_needs_fix
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations
from packages.core.services.workspace_operation_validation import validate_operation_domain_values


def _base_state():
    return {
        "operating_model": {},
        "services": [{"key": "content", "name": "Content"}],
        "agent_mappings": [],
        "capability_bindings": [],
        "skill_bindings": [],
        "knowledge_policy": {},
        "heartbeat_policy": {"enabled": False, "cadence": "daily"},
        "governance_policy": {},
        "budget_policy": {"monthly_budget_credits": 100},
        "immediate_tasks": [],
        "channel_config": {},
    }


_INVALID_PATCHES = [
    ("budget.update", {"monthly_budget_credits": "500"}, "budget_policy.monthly_budget_credits"),
    ("budget.update", {"monthly_budget_usd": True}, "budget_policy.monthly_budget_usd"),
    ("budget.update", {"monthly_budget_credits": float("inf")}, "budget_policy.monthly_budget_credits"),
    ("budget.update", {"monthly_budget_credits": 10 ** 400}, "budget_policy.monthly_budget_credits"),
    ("budget.update", {"auto_pause_on_budget": "false"}, "budget_policy.auto_pause_on_budget"),
    ("heartbeat.update", {"enabled": "false"}, "heartbeat_policy.enabled"),
    ("heartbeat.update", {"enabled": True, "cadence": "every Monday"}, "heartbeat_policy.cadence"),
    ("knowledge.update", {"citation_required": "false"}, "knowledge_policy.citation_required"),
    ("knowledge.update", {"strict_mode": 1}, "knowledge_policy.strict_mode"),
    ("knowledge.update", {"retrieval_mode": {}}, "knowledge_policy.retrieval_mode"),
    ("task.prepare", {"title": "Make the video", "priority": "urgent"}, "immediate_tasks[0].priority"),
    ("task.prepare", {"title": "Make the video", "priority": 0}, "immediate_tasks[0].priority"),
    ("task.prepare", {"title": "Make the video", "runtime_context": "User constraints"}, "immediate_tasks[0].runtime_context"),
    ("task.prepare", {"title": "Make the video", "delegate_service_keys": "content"}, "immediate_tasks[0].delegate_service_keys"),
    ("governance.update", {"max_risk_level": "urgent"}, "governance_policy"),
    ("governance.update", {"max_risk_level": []}, "governance_policy"),
    ("channel.upsert", {"channel_type": "telegram"}, "channel_config.channels[0].channel_config_id"),
    ("channel.upsert", {"channel_type": "webchat", "linked_service_key": "unknown"}, "channel_config.channels[0].linked_service_key"),
    ("channel.upsert", {"channel_type": "webchat", "linked_service_key": "content"}, "channel_config.channels[0].linked_service_key"),
    ("channel.upsert", {"channel_type": "webchat", "login_required": "false"}, "channel_config.channels[0].login_required"),
    ("channel.upsert", {"channel_type": "webchat", "agent_subscription_id": "foreign-sub"}, "channel_config.channels[0].agent_subscription_id"),
    ("channel.upsert", {"channel_type": "webchat", "agent_id": "foreign-agent"}, "channel_config.channels[0].agent_id"),
]


class _NoDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("these primitive checks do not need a database")


@pytest.mark.parametrize("field", ["hitl_required_actions", "never_allow_actions", "auto_approve_actions",
    "hitl_required_capabilities", "never_allow_capabilities", "auto_approve_capabilities", "approval"])
def test_generic_governance_patch_rejects_approval_before_draft_creation(field):
    with pytest.raises(ValueError, match="Execution.*approvals"):
        operations._apply_single_patch(_base_state(), {
            "op": "governance.update", "payload": {field: []},
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("binding", [
    {"capability_type": "mcp", "integration_key": "slack"},
    {"capability_type": "tool", "tool_name": "mcp__slack__send_message"},
])
async def test_workspace_chat_integration_add_requires_existing_connection(monkeypatch, connected, binding):
    from packages.core.services import integration_resolution

    patch = {"op": "capability_binding.upsert", "payload": {"binding": {
        "owner_scope": "workspace_agent", **binding,
    }}}
    current = _base_state()
    state = operations._apply_single_patch(deepcopy(current), patch)
    draft = WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace",
        created_by_user_id="user", status="open", base_revision=0,
        current_state=state, patches=[patch],
    )
    readiness = AsyncMock(return_value={
        "slack": SimpleNamespace(ready=connected),
    })
    monkeypatch.setattr(integration_resolution, "integration_provider_readiness", readiness)

    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=current, user_id="user",
    )

    assert validation["valid"] is connected
    assert any(error.get("code") == "integration_not_connected" for error in validation["errors"]) is not connected
    readiness.assert_awaited_once()


def test_workspace_chat_integration_remove_and_enable_change_runtime_scope():
    state = _base_state()
    removed = operations._apply_single_patch(state, {
        "op": "integration.remove", "payload": {"provider": "slack"},
    })
    assert removed["integration_scope"]["disabled_providers"] == ["slack"]
    enabled = operations._apply_single_patch(removed, {
        "op": "integration.enable", "payload": {"provider": "slack"},
    })
    assert enabled["integration_scope"]["disabled_providers"] == []


def _draft(op, payload):
    patch = {"op": op, "payload": payload}
    state = operations._apply_single_patch(deepcopy(_base_state()), patch)
    return WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace", status="open",
        base_revision=0, current_state=state, patches=[patch],
    )


def _task_tool_draft(task_id: str, tool_name: str = "generate_file"):
    return _draft("capability_binding.upsert", {"binding": {
        "owner_scope": "task",
        "task_id": task_id,
        "capability_type": "tool",
        "tool_name": tool_name,
    }})


def _model_task_binding_draft(op: str, binding: dict):
    model = {"capability_bindings": [binding]}
    if op.endswith(".replace"):
        model["services"] = _base_state()["services"]
    return _draft(op, {"operating_model": model})


@pytest.mark.asyncio
async def test_task_tool_binding_requires_reviewed_agent_configuration():
    validation = await operations.validate_operation_draft(
        _NoDb(), _task_tool_draft("task-local"), current_state=_base_state(),
    )
    assert validation["valid"] is False
    assert any(
        error.get("code") == "task_scoped_binding_unsupported"
        and "Agent's configuration" in error["message"]
        for error in validation["errors"]
    )


@pytest.mark.parametrize("op", [
    "operating_model.update", "workspace_model.update",
    "operating_model.replace", "workspace_model.replace",
])
@pytest.mark.asyncio
async def test_whole_model_routes_reject_task_tool_binding(op):
    validation = await operations.validate_operation_draft(
        _NoDb(), _model_task_binding_draft(op, {
            "owner_scope": "task", "task_id": "task-local",
            "capability_type": "tool", "tool_name": "generate_file",
        }), current_state=_base_state(),
    )
    assert validation["valid"] is False
    assert any(error.get("code") == "task_scoped_binding_unsupported" for error in validation["errors"])


@pytest.mark.parametrize("op", [
    "operating_model.update", "workspace_model.update",
    "operating_model.replace", "workspace_model.replace",
])
@pytest.mark.asyncio
async def test_whole_model_routes_reject_task_capability_binding(op):
    validation = await operations.validate_operation_draft(
        _NoDb(), _model_task_binding_draft(op, {
            "owner_scope": "task", "task_id": "task-local",
            "capability_type": "capability", "capability_id": "file.write",
        }), current_state=_base_state(),
    )
    assert validation["valid"] is False
    assert any(error.get("code") == "task_scoped_binding_unsupported" for error in validation["errors"])


@pytest.mark.asyncio
async def test_task_skill_binding_requires_reviewed_agent_configuration():
    draft = _draft("skill_binding.upsert", {"binding": {
        "owner_scope": "task", "task_id": "task-local", "skill_id": "skill-local",
    }})
    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=_base_state(),
    )
    assert validation["valid"] is False
    assert any(
        error.get("code") == "task_scoped_binding_unsupported"
        and error["path"] == "skill_bindings[0].owner_scope"
        for error in validation["errors"]
    )


@pytest.mark.asyncio
async def test_service_tool_binding_remains_available_for_review():
    draft = _draft("capability_binding.upsert", {"binding": {
        "owner_scope": "service", "owner_service_key": "content",
        "capability_type": "tool", "tool_name": "generate_file",
    }})
    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=_base_state(),
    )
    assert validation["valid"] is True, validation["errors"]


@pytest.mark.asyncio
async def test_disabled_task_binding_is_not_a_new_execution_grant():
    draft = _draft("capability_binding.upsert", {"binding": {
        "owner_scope": "task", "task_id": "task-local",
        "capability_type": "tool", "tool_name": "generate_file", "enabled": False,
    }})
    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=_base_state(),
    )
    assert validation["valid"] is True, validation["errors"]


@pytest.mark.asyncio
async def test_unrelated_draft_does_not_recheck_legacy_task_tool_binding():
    legacy_binding = {
        "owner_scope": "task", "task_id": "old-task",
        "capability_type": "tool", "tool_name": "retired_tool", "description": "Old label",
    }
    current = _base_state()
    current["capability_bindings"] = [legacy_binding]
    draft = _draft("service_role.upsert", {"service": {"key": "other", "name": "Other"}})
    draft.current_state["capability_bindings"] = [{**legacy_binding, "description": "New label"}]

    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=current,
    )
    assert validation["valid"] is True, validation["errors"]


@pytest.mark.asyncio
async def test_explicit_task_scope_does_not_inherit_unscoped_legacy_grant():
    current = _base_state()
    current["capability_bindings"] = [{
        "task_id": "task-foreign", "capability_type": "tool", "tool_name": "generate_file",
    }]
    draft = _task_tool_draft("task-foreign")

    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=current,
    )
    assert validation["valid"] is False
    assert any(error.get("code") == "task_scoped_binding_unsupported" for error in validation["errors"])


@pytest.mark.asyncio
async def test_task_tool_binding_rechecks_unsupported_grant_at_apply(monkeypatch):
    draft = _task_tool_draft("task-local")

    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=SimpleNamespace(operation_revision=0)))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(return_value=_base_state()))
    preview = AsyncMock(side_effect=AssertionError("invalid grant must stop before materialization"))
    monkeypatch.setattr(operations, "preview_operation_diff", preview)

    with pytest.raises(operations.OperationValidationError) as raised:
        await operations.apply_operation_draft(
            _NoDb(), "draft", "entity", "workspace",
            user_id="user", user_confirmation=True,
        )
    assert any(error.get("code") == "task_scoped_binding_unsupported" for error in raised.value.validation["errors"])
    assert draft.status == "open"
    preview.assert_not_awaited()


@pytest.mark.parametrize("op,payload,path", _INVALID_PATCHES)
@pytest.mark.asyncio
async def test_invalid_domain_values_return_field_errors_instead_of_review(op, payload, path):
    draft = _draft(op, payload)
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=_base_state())
    assert validation["valid"] is False
    assert any(error["path"] == path for error in validation["errors"])

    result = _operation_review_or_needs_fix(
        operations.draft_to_dict(draft), prompt="Apply?", content={},
    )
    assert result["status"] == "needs_fix"
    assert "__hitl__" not in result


@pytest.mark.parametrize("op,payload,path", _INVALID_PATCHES)
@pytest.mark.asyncio
async def test_apply_rejects_invalid_legacy_draft_before_materialization(monkeypatch, op, payload, path):
    draft = _draft(op, payload)
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=SimpleNamespace(operation_revision=0)))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(return_value=_base_state()))
    preview = AsyncMock(side_effect=AssertionError("invalid configuration must stop before materialization"))
    monkeypatch.setattr(operations, "preview_operation_diff", preview)

    with pytest.raises(operations.OperationValidationError) as raised:
        await operations.apply_operation_draft(
            _NoDb(), "draft", "entity", "workspace", user_id="user", user_confirmation=True,
        )
    assert any(error["path"] == path for error in raised.value.validation["errors"])
    assert draft.status == "open"
    preview.assert_not_awaited()


def test_valid_domain_values_include_disabled_flags_and_real_cron():
    state = _base_state()
    state.update({
        "budget_policy": {"monthly_budget_credits": 500, "auto_pause_on_budget": False},
        "heartbeat_policy": {"enabled": True, "cadence": "0 9 * * 1-5"},
        "knowledge_policy": {"citation_required": False, "strict_mode": True, "retrieval_mode": "strict"},
        "governance_policy": {"approval": {"mode": None, "rules": [{
            "id": "review-publish", "effect": "ask", "selector": {
                "kind": "action", "action_key": "social_post.publish",
            },
        }]}},
        "immediate_tasks": [{"title": "Make the video", "priority": 4, "runtime_context": {}, "delegate_service_keys": ["content"]}],
        "agent_mappings": [{"service_key": "content", "agent_id": "agent"}],
        "channel_config": {"channels": [{"channel_type": "telegram", "channel_config_id": "existing-config", "linked_service_key": "content", "login_required": False}]},
    })
    assert validate_operation_domain_values(state, changed_keys=set(state)) == []


def test_unrelated_legacy_values_do_not_block_configuration_edits():
    state = _base_state()
    state["heartbeat_policy"]["enabled"] = "false"
    state["budget_policy"]["monthly_budget_credits"] = "500"
    state["channel_config"] = {"channels": [{"channel_type": "telegram"}]}
    assert validate_operation_domain_values(state, changed_keys={"services"}) == []


@pytest.mark.parametrize("config_values", [
    None,
    {"status": "inactive"},
    {"workspace_id": "another-workspace"},
    {"entity_id": "another-entity"},
    {"owner_user_id": "another-user"},
    {"owner_user_id": None, "workspace_id": None},
])
@pytest.mark.asyncio
async def test_unavailable_channel_account_is_rejected_before_review(db_session, config_values):
    if config_values is not None:
        fields = dict(id="channel", entity_id="entity", workspace_id=None, owner_user_id="user", channel_type="telegram", provider="telegram", status="active")
        fields.update(config_values)
        db_session.add(ChannelConfig(**fields))
        await db_session.flush()
    draft = _draft("channel.upsert", {"channel_type": "telegram", "channel_config_id": "channel"})
    draft.created_by_user_id = "user"
    validation = await operations.validate_operation_draft(db_session, draft, current_state=_base_state())
    assert validation["valid"] is False
    assert any(error["path"] == "channel_config.channels[0].channel_config_id" for error in validation["errors"])
    result = _operation_review_or_needs_fix(operations.draft_to_dict(draft), prompt="Apply?", content={})
    assert result["status"] == "needs_fix"
    assert "__hitl__" not in result


@pytest.mark.parametrize("owner_id,workspace_id", [("user", None), ("user", "workspace"), (None, "workspace")])
@pytest.mark.asyncio
async def test_channel_reference_matches_workspace_api_access_boundary(db_session, owner_id, workspace_id):
    db_session.add(ChannelConfig(id="channel", entity_id="entity", workspace_id=workspace_id, owner_user_id=owner_id, channel_type="telegram", provider="telegram", status="active"))
    await db_session.flush()
    draft = _draft("channel.upsert", {"channel_type": "telegram", "channel_config_id": "channel"})
    draft.created_by_user_id = "user"
    validation = await operations.validate_operation_draft(db_session, draft, current_state=_base_state())
    assert validation["valid"] is True, validation
    if owner_id:
        other_actor = await operations.validate_operation_draft(db_session, draft, current_state=_base_state(), user_id="another-user")
        assert other_actor["valid"] is False


@pytest.mark.asyncio
async def test_selected_channel_type_and_provider_cannot_rewrite_account(db_session):
    db_session.add(ChannelConfig(id="channel", entity_id="entity", owner_user_id="user", channel_type="email", provider="smtp", status="active"))
    await db_session.flush()
    draft = _draft("channel.upsert", {"channel_type": "telegram", "provider": "telegram", "channel_config_id": "channel"})
    validation = await operations.validate_operation_draft(db_session, draft, current_state=_base_state(), user_id="user")
    assert {error["path"] for error in validation["errors"]} >= {"channel_config.channels[0].channel_type", "channel_config.channels[0].provider"}


@pytest.mark.asyncio
async def test_channel_materialization_preserves_credential_owner_and_provider(db_session):
    config = ChannelConfig(id="channel", entity_id="entity", owner_user_id="user", channel_type="email", provider="smtp", status="active")
    db_session.add(config)
    await db_session.flush()
    workspace = SimpleNamespace(id="workspace", entity_id="entity")
    state = {"channels": [{"channel_config_id": "channel", "channel_type": "email", "name": "Email Channel"}]}
    created = await operations._sync_channels_from_operation_state(db_session, workspace, state)
    binding = (await db_session.execute(select(Channel).where(Channel.workspace_id == "workspace"))).scalar_one()
    assert created["created_bindings"] == 1
    assert binding.user_id == "user"
    assert config.provider == "smtp"
    binding.user_id = "old-owner"
    await db_session.flush()
    updated = await operations._sync_channels_from_operation_state(db_session, workspace, state)
    assert updated["updated_bindings"] == 1
    assert binding.user_id == "user"
    assert config.provider == "smtp"


@pytest.mark.asyncio
async def test_legacy_channel_type_alias_reaches_materialization(db_session):
    state = {"primary_external_channel": {"type": "webchat", "name": "Public Chat"}}
    result = await operations._sync_channels_from_operation_state(
        db_session, SimpleNamespace(id="workspace", entity_id="entity"), state,
    )
    assert result["created_configs"] == result["created_bindings"] == 1
    assert result["skipped"] == 0
