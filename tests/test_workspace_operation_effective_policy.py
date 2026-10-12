"""Review, validation and apply must agree on the rule change's real effect."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.governance import policy_to_dict
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.approval_projection import policy_override
from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations
from packages.core.services.workspace_setup_service import _build_governance_policy_from_rules


class _NoDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("this unit must not access shared database state")

    async def flush(self):
        pass


def _rule(key, rule_type):
    return {
        "rule_key": key,
        "rule_type": rule_type,
        "description": key,
        "action_patterns": ["social_post.publish"],
        "capability_patterns": ["external.social"],
        "runtime_enforced": True,
    }


def _user_rule(rule_id, effect, *, action=None, category=None):
    selector = {"kind": "action", "action_key": action} if action else {"kind": "category", "category": category}
    return {"id": rule_id, "effect": effect, "selector": selector, "source_kind": "user", "source_id": None}


def _rules(policy, *, effect=None, source_kind=None):
    return [rule.model_dump() for rule in policy_override(policy).rules
            if (effect is None or rule.effect == effect) and (source_kind is None or rule.source_kind == source_kind)]


def _add_user_rule(policy, rule):
    approval = policy["approval"]
    (approval.get("policy") or approval)["rules"].append(rule)


def _state(*, extra_deny=False):
    rules = [_rule("no_controversial_topics", "deny"), _rule("approval_required_all_posts", "approval_required")]
    if extra_deny:
        rules.append(_rule("still_block_posts", "deny"))
    policy = _build_governance_policy_from_rules(rules, {
        "approval": {"mode": None, "rules": [
            _user_rule("files", "allow", category="files"),
            _user_rule("notes", "allow", action="notes.create"),
            _user_rule("billing", "deny", action="billing.refund"),
        ]},
    })
    return operations._sync_operating_model_from_state({
        "services": [{"key": "social"}],
        "rules": rules,
        "governance_policy": policy_to_dict(policy),
        "operating_model": {},
    })


def _draft(before, *patches):
    after = deepcopy(before)
    for patch in patches:
        after = operations._apply_single_patch(after, patch)
    return WorkspaceOperationDraft(
        id="draft", workspace_id="workspace", entity_id="entity", status="open",
        base_revision=40, current_state=after, patches=list(patches),
    )


def _remove_deny():
    return {"op": "rule.remove", "payload": {"rule_key": "no_controversial_topics"}}


def _publish_decision(policy):
    chain = build_policy_chain(
        scope=ApprovalScope(kind="workspace", id="workspace", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": "risk_based", "rules": []}},
        local={"root_entity_id": "entity", "policy": policy_override(policy).model_dump()},
        local_revision=1,
    )
    evaluation = evaluate_policy(chain, action_facts(action_key="social_post.publish", risk_level="low"))
    return SimpleNamespace(allowed=evaluation.outcome == "allow", pause_for_hitl=evaluation.outcome == "ask")


@pytest.mark.asyncio
async def test_removing_the_deny_clears_stale_conflicts_and_keeps_publication_approval():
    before = _state()
    draft = _draft(before, _remove_deny())
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    diff = await operations.preview_operation_diff(_NoDb(), draft, current_state=before)

    assert validation["valid"] is True
    assert not validation["warnings"]
    policy = diff["changes"]["governance_policy"]["after"]
    assert _publish_decision(before["governance_policy"]).pause_for_hitl is False
    assert _publish_decision(policy).pause_for_hitl is True
    assert _rules(policy, source_kind="user") == _rules(before["governance_policy"], source_kind="user")
    review = operations.operation_review_diff(operations.draft_to_dict(draft))
    assert set(review["removed_hard_blocks"]) == {"social_post.publish"}
    # The creation snapshot remains historical; no validator mutation masks the defect.
    assert draft.current_state["governance_policy"] == before["governance_policy"]


@pytest.mark.asyncio
async def test_a_surviving_deny_remains_in_validation_and_execution():
    before = _state(extra_deny=True)
    draft = _draft(before, _remove_deny())
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    diff = await operations.preview_operation_diff(_NoDb(), draft, current_state=before)

    assert any("still_block_posts" in warning["message"] for warning in validation["warnings"])
    policy = operations._effective_operation_state(before, draft)["governance_policy"]
    assert any(rule["selector"].get("action_key") == "social_post.publish" for rule in _rules(policy, effect="deny"))
    assert _publish_decision(policy).allowed is False
    assert _publish_decision(policy).pause_for_hitl is False
    # Source provenance changes even though another prohibition still wins.
    assert "governance_policy" in diff["changed_keys"]
    assert operations.operation_review_diff(operations.draft_to_dict(draft))["removed_hard_blocks"] == []


@pytest.mark.parametrize("has_before_rules", [False, True])
def test_legacy_single_rule_card_does_not_invent_removal_of_independent_blocks(has_before_rules):
    before = _state()
    draft = _draft(before, _remove_deny())
    # Old cards stored the raw rule diff, before policy resolution was shared.
    draft.diff = operations._state_diff(before, draft.current_state)
    if not has_before_rules:
        draft.diff["changes"]["rules"].pop("before")
    review = operations.operation_review_diff(operations.draft_to_dict(draft))

    # Typed source provenance is evidence even on a card missing its old rules.
    expected = {"social_post.publish"}
    assert set(review["removed_hard_blocks"]) == expected
    assert "billing.refund" not in review["removed_hard_blocks"]


@pytest.mark.asyncio
async def test_fresh_review_does_not_claim_a_gate_already_removed_by_live_governance():
    before = _state()
    draft = _draft(before, _remove_deny())
    live = deepcopy(before)
    live["governance_policy"]["approval"]["rules"] = [
        rule for rule in live["governance_policy"]["approval"]["rules"]
        if rule.get("source_id") != "no_controversial_topics"
    ]
    diff = await operations.preview_operation_diff(_NoDb(), draft, current_state=live)

    assert diff["changed_keys"] == ["rules"]
    assert "governance_policy" not in diff["changes"]
    assert diff["effective_governance_policy"]["before"] == live["governance_policy"]
    assert diff["effective_governance_policy"]["after"] == live["governance_policy"]
    review = operations.operation_review_diff(operations.draft_to_dict(draft))
    assert review["removed_hard_blocks"] == []
    assert "REMOVES hard blocks" not in operations.operation_review_hitl_payload(operations.draft_to_dict(draft))["why"]


@pytest.mark.asyncio
@pytest.mark.parametrize("live_drift", [False, True])
async def test_same_rule_update_cannot_use_unrelated_policy_drift_to_claim_success(live_drift):
    before = _state()
    draft = _draft(before, {
        "op": "rule.update",
        "payload": {"rule_key": "approval_required_all_posts", "rule": {"rule_type": "approval_required"}},
    })
    if live_drift:
        # A separate Governance API write after draft creation does not advance
        # operation_revision. It must not be reverted or counted as this edit.
        _add_user_rule(before["governance_policy"], _user_rule("knowledge", "allow", action="knowledge.read"))
        before["governance_policy"]["max_risk_level"] = "medium"
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    diff = await operations.preview_operation_diff(_NoDb(), draft, current_state=before)

    assert validation["valid"] is False
    assert any(error.get("code") == "no_changes" for error in validation["errors"])
    assert diff["changed_keys"] == []
    assert _user_rule("files", "allow", category="files") in _rules(before["governance_policy"])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_fields", [
    ("runtime_enforced",),
    ("capability_patterns",),
    ("runtime_enforced", "capability_patterns"),
])
async def test_same_legacy_rule_update_cannot_count_derived_metadata_as_a_change(missing_fields):
    before = _state()
    for rule in before["rules"]:
        for field in missing_fields:
            rule.pop(field)
    before = operations._sync_operating_model_from_state(before)
    draft = _draft(before, {"op": "rule.update", "payload": {
        "rule_key": "approval_required_all_posts", "rule": {"rule_type": "approval_required"},
    }})
    validation = await operations.validate_operation_draft(_NoDb(), draft, current_state=before)
    diff = await operations.preview_operation_diff(_NoDb(), draft, current_state=before)

    if "runtime_enforced" in missing_fields:
        assert draft.current_state["rules"] != before["rules"]
    assert any(error.get("code") == "no_changes" for error in validation["errors"])
    assert diff["changed_keys"] == []
    assert diff["effective_governance_policy"]["after"] == before["governance_policy"]


def test_explicit_governance_edit_preserves_unedited_live_keys_and_consent():
    before = _state()
    draft = _draft(before, _remove_deny(), {
        "op": "governance_policy.update", "payload": {"max_risk_level": "medium"},
    })
    live = deepcopy(before)
    _add_user_rule(live["governance_policy"], _user_rule("credentials", "deny", action="credentials.read"))
    _add_user_rule(live["governance_policy"], _user_rule("notes-update", "allow", action="notes.update"))
    live["governance_policy"]["approval"]["mode"] = "automatic"
    effective = operations._effective_operation_state(live, draft)
    policy = effective["governance_policy"]

    assert policy["max_risk_level"] == "medium"
    assert _rules(policy, source_kind="user") == _rules(live["governance_policy"], source_kind="user")
    assert policy_override(policy).mode == "automatic"
    assert effective["operating_model"]["governance"] == policy


def test_explicit_rule_list_replacement_rebuilds_sources_and_preserves_user_rules():
    before = _state()
    after_rules = [before["rules"][1]]
    draft = _draft(before, {"op": "rules.replace", "payload": {"rules": after_rules}})
    policy = operations._effective_operation_state(before, draft)["governance_policy"]

    assert _rules(policy, source_kind="user") == _rules(before["governance_policy"], source_kind="user")
    assert not _rules(policy, effect="deny", source_kind="workspace_rule")
    assert _publish_decision(policy).pause_for_hitl is True


@pytest.mark.parametrize("op", ["operating_model.replace", "operating_model.update"])
def test_operating_model_rule_edit_clears_only_the_removed_rule_hard_block(op):
    before = _state()
    operating_model = deepcopy(before["operating_model"])
    operating_model["rules"] = [before["rules"][1]]
    payload = (
        {"operating_model": operating_model}
        if op == "operating_model.replace"
        else {"operating_model": {"rules": operating_model["rules"]}}
    )
    draft = _draft(before, {
        "op": op,
        "payload": payload,
    })

    policy = operations._effective_operation_state(before, draft)["governance_policy"]

    assert _rules(policy, source_kind="user") == _rules(before["governance_policy"], source_kind="user")
    assert not _rules(policy, effect="deny", source_kind="workspace_rule")
    assert _publish_decision(policy).pause_for_hitl is True


@pytest.mark.parametrize("field,value", [
    ("approval", {"mode": "automatic", "rules": []}),
    ("never_allow_actions", []),
])
def test_direct_approval_edits_require_the_versioned_settings_entry(field, value):
    before = _state()
    with pytest.raises(ValueError, match="Execution & approvals"):
        _draft(before, {"op": "governance_policy.update", "payload": {field: value}})
    assert _publish_decision(before["governance_policy"]).allowed is False


@pytest.mark.parametrize("op", ["operating_model.update", "workspace_model.update"])
def test_operating_model_governance_edit_cannot_hide_a_surviving_rule_gate(op):
    before = _state()
    with pytest.raises(ValueError, match="Execution & approvals"):
        _draft(before, {"op": op, "payload": {"operating_model": {
            "governance": {"approval": {"mode": "automatic", "rules": []}},
        }}})
    assert _publish_decision(before["governance_policy"]).allowed is False


@pytest.mark.asyncio
async def test_operating_model_partial_governance_edit_preserves_unedited_rule_lists():
    before = _state()
    draft = _draft(before, {
        "op": "operating_model.update",
        "payload": {
            "operating_model": {
                "governance": {"max_risk_level": "medium"},
            },
        },
    })

    validation = await operations.validate_operation_draft(
        _NoDb(), draft, current_state=before,
    )
    policy = operations._effective_operation_state(before, draft)["governance_policy"]

    assert validation["valid"] is True
    assert not [
        error for error in validation["errors"]
        if error.get("code") == "rule_implied_governance"
    ]
    assert any(rule["selector"].get("action_key") == "social_post.publish" for rule in _rules(policy, effect="deny"))
    assert policy["max_risk_level"] == "medium"


@pytest.mark.asyncio
async def test_source_rule_edit_removes_only_stale_source_owned_policy():
    before = _state()
    before["rules"] = [before["rules"][1]]
    before["operating_model"]["rules"] = deepcopy(before["rules"])
    draft = _draft(before, {"op": "rules.replace", "payload": {"rules": before["rules"]}})
    policy = operations._effective_operation_state(before, draft)["governance_policy"]
    assert not _rules(policy, effect="deny", source_kind="workspace_rule")
    assert _rules(policy, source_kind="user") == _rules(before["governance_policy"], source_kind="user")
    assert _publish_decision(policy).pause_for_hitl is True


@pytest.mark.asyncio
async def test_apply_uses_locked_live_policy_and_matches_its_validated_preview(monkeypatch):
    before = _state()
    draft = _draft(before, _remove_deny())
    live = deepcopy(before)
    _add_user_rule(live["governance_policy"], _user_rule("credentials", "deny", action="credentials.read"))
    live["governance_policy"]["approval"]["mode"] = "automatic"
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=40,
                                heartbeat_enabled=False, heartbeat_cadence="daily")
    events = []

    async def load_workspace(_db, _workspace_id, _entity_id, *, for_update):
        assert for_update is True
        events.append("workspace_lock")
        return workspace

    async def locked_snapshot(_db, _workspace, *, lock_governance):
        assert lock_governance is True
        events.append("policy_snapshot_lock")
        return live

    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(side_effect=load_workspace))
    snapshot = AsyncMock(side_effect=locked_snapshot)
    monkeypatch.setattr(operations, "_snapshot_workspace_state", snapshot)
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    materialize = AsyncMock(return_value={"task_ids": [], "tasks_created": 0})
    monkeypatch.setattr(operations, "_materialize_immediate_tasks", materialize)
    monkeypatch.setattr("packages.core.services.workspace_runtime.sync_workspace_runtime_schedules", AsyncMock())
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
    monkeypatch.setattr("packages.core.workspace_chat.context.invalidate", lambda *_args: None)

    async def save_policy(_db, **kwargs):
        events.append("policy_write")
        return SimpleNamespace(policy=policy_to_dict(kwargs["policy"]))

    update = AsyncMock(side_effect=save_policy)
    monkeypatch.setattr("packages.core.governance.update_policy", update)
    db = _NoDb()
    result = await operations.apply_operation_draft(
        db, "draft", "entity", "workspace", user_id="user", user_confirmation=True,
        origin_task_id="run-task",
    )
    # Tasks the draft creates are linked to the Task run that applied it.
    assert materialize.call_args.kwargs["origin_task_id"] == "run-task"

    written = policy_to_dict(update.call_args.kwargs["policy"])
    assert written == result["draft"]["diff"]["changes"]["governance_policy"]["after"]
    assert written == workspace.operating_model["governance"]
    assert _rules(written, source_kind="user") == _rules(live["governance_policy"], source_kind="user")
    assert policy_override(written).mode == "automatic"
    assert _publish_decision(written).pause_for_hitl is True
    assert result["workspace_revision"] == 41
    assert draft.status == "applied"
    snapshot.assert_awaited_once_with(db, workspace, lock_governance=True)
    update.assert_awaited_once()
    assert events == ["workspace_lock", "policy_snapshot_lock", "policy_write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_governance", [False, True])
async def test_policy_snapshot_locks_only_the_exact_existing_workspace_policy_when_applying(monkeypatch, lock_governance):
    from sqlalchemy.dialects import postgresql

    state = _state()
    workspace = SimpleNamespace(
        id="workspace", entity_id="entity", operation_revision=40,
        operating_model=state["operating_model"], auto_pause_on_budget=False,
        heartbeat_enabled=False, heartbeat_cadence="daily",
    )
    events = []

    async def execute(statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert ("FOR UPDATE" in sql) is lock_governance
        assert "governance_policies.workspace_id =" in sql
        assert "governance_policies.entity_id =" in sql
        assert set(compiled.params.values()) == {"workspace", "entity"}
        events.append("policy_snapshot")
        return SimpleNamespace(scalar_one_or_none=lambda: state["governance_policy"])

    async def budget(*_args):
        events.append("budget")
        return None

    async def mappings(*_args):
        events.append("agent_mappings")
        return []

    monkeypatch.setattr("packages.core.budget.get_budget_status", budget)
    monkeypatch.setattr("packages.core.services.workspace_service.get_workspace_agent_mappings", mappings)
    snapshot = await operations._snapshot_workspace_state(
        SimpleNamespace(execute=execute), workspace, lock_governance=lock_governance,
    )

    assert snapshot["governance_policy"] == state["governance_policy"]
    assert events == ["policy_snapshot", "budget", "agent_mappings"]


@pytest.mark.asyncio
async def test_rule_noop_apply_does_not_advance_revision(monkeypatch):
    before = _state()
    draft = _draft(before, {"op": "rule.update", "payload": {
        "rule_key": "approval_required_all_posts", "rule": {"rule_type": "approval_required"},
    }})
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=40)
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(return_value=before))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    with pytest.raises(operations.OperationValidationError):
        await operations.apply_operation_draft(_NoDb(), "draft", "entity", "workspace", user_confirmation=True)
    assert workspace.operation_revision == 40
    assert draft.status == "open"


@pytest.mark.asyncio
async def test_concurrent_operation_revision_invalidates_old_approval_before_writes(monkeypatch):
    draft = _draft(_state(), _remove_deny())
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=41)
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(operations, "lock_reusable_resource_lifecycle", AsyncMock())
    snapshot = AsyncMock()
    monkeypatch.setattr(operations, "_snapshot_workspace_state", snapshot)
    with pytest.raises(operations.OperationConflictError, match="stale"):
        await operations.apply_operation_draft(_NoDb(), "draft", "entity", "workspace", user_confirmation=True)
    assert draft.status == "open"
    snapshot.assert_not_awaited()
