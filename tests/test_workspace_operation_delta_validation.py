"""Workspace operations validate the change, not the whole stored state.

A Workspace can already hold validation errors (R2-4: a Blueprint install left
``agent_mappings[*].service_key`` pointing at services that
``operating_model.services`` never declared). Every apply entry point must
reject only the errors a patch introduces; errors the Workspace already had
are reported back as ``pre_existing_issues`` and as warnings, never dropped.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations


class _NoDb:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("these state checks do not need a database")


def _state_with_undeclared_services():
    """The R2-4 shape: two mappings reference services that were never declared."""
    return {
        "operating_model": {},
        "services": [{"key": "video_studio.direction", "name": "Video Director"}],
        "agent_mappings": [
            {"service_key": "video_studio.direction", "strategy": "create_custom"},
            {"service_key": "product_video.exploration", "strategy": "create_custom"},
            {"service_key": "product_video.planning", "strategy": "create_custom"},
        ],
        "capability_bindings": [],
        "skill_bindings": [],
        "knowledge_policy": {},
        "heartbeat_policy": {"enabled": False, "cadence": "daily"},
        "governance_policy": {},
        "budget_policy": {"monthly_budget_credits": 100},
        "immediate_tasks": [],
        "channel_config": {},
        "rules": [{"rule_key": "kept", "description": "kept"}],
    }


def _draft(current, *patches):
    state = deepcopy(current)
    for patch in patches:
        state = operations._apply_single_patch(state, patch)
    return WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace",
        created_by_user_id=None, status="open", base_revision=0,
        current_state=state, patches=list(patches),
    )


async def _validate(current, *patches):
    return await operations.validate_operation_draft(
        _NoDb(), _draft(current, *patches), current_state=current,
    )


@pytest.mark.asyncio
async def test_the_existing_state_really_has_the_r2_4_errors():
    current = _state_with_undeclared_services()
    unchanged = await operations.validate_operation_draft(
        _NoDb(), _draft(current), current_state=current,
    )
    paths = sorted(error["path"] for error in unchanged["errors"])
    # The no-op draft is still refused for making no change, but the stored
    # errors are not attributed to it.
    assert [error["code"] for error in unchanged["errors"]] == ["no_changes"]
    assert sorted(issue["path"] for issue in unchanged["pre_existing_issues"]) == [
        "agent_mappings[1].service_key", "agent_mappings[2].service_key",
    ]
    assert paths == ["patches"]


@pytest.mark.asyncio
async def test_valid_patch_applies_with_pre_existing_errors_as_warnings():
    current = _state_with_undeclared_services()
    validation = await _validate(
        current, {"op": "budget.update", "payload": {"monthly_budget_credits": 200}},
    )
    assert validation["valid"] is True
    assert validation["errors"] == []
    issues = validation["pre_existing_issues"]
    assert sorted(issue["path"] for issue in issues) == [
        "agent_mappings[1].service_key", "agent_mappings[2].service_key",
    ]
    assert all("product_video." in issue["message"] for issue in issues)
    # Surfaced where every review card already reads: never silently ignored.
    surfaced = [w for w in validation["warnings"] if w.get("code") == "pre_existing_issue"]
    assert sorted(w["path"] for w in surfaced) == sorted(issue["path"] for issue in issues)
    assert all(w["message"].startswith("Existing issue") for w in surfaced)


@pytest.mark.asyncio
async def test_patch_introducing_an_error_is_rejected_with_its_path():
    current = _state_with_undeclared_services()
    validation = await _validate(current, {"op": "agent_mapping.upsert", "payload": {
        "service_key": "product_video.capture", "strategy": "create_custom",
    }})
    assert validation["valid"] is False
    assert [(error["path"], error["message"]) for error in validation["errors"]] == [(
        "agent_mappings[3].service_key",
        "agent mapping references unknown service 'product_video.capture'",
    )]
    assert len(validation["pre_existing_issues"]) == 2


@pytest.mark.asyncio
async def test_a_second_copy_of_an_existing_error_is_still_new():
    current = _state_with_undeclared_services()
    current["agent_mappings"].append({"service_key": "", "strategy": "create_custom", "note": "old"})
    validation = await _validate(current, {"op": "agent_mapping.replace", "payload": {"mappings": [
        *current["agent_mappings"], {"service_key": "", "strategy": "create_custom", "note": "new"},
    ]}})
    # Two identical "requires service_key" problems after, one before: one is new.
    new = [error for error in validation["errors"] if error["message"] == "agent mapping requires service_key"]
    assert len(new) == 1 and new[0]["path"] == "agent_mappings[4].service_key"
    assert validation["valid"] is False


@pytest.mark.asyncio
async def test_removing_an_earlier_row_does_not_turn_existing_errors_into_new_ones():
    current = _state_with_undeclared_services()
    current["services"].append({"key": "video_studio.quality", "name": "Quality"})
    current["agent_mappings"].insert(0, {"service_key": "video_studio.quality", "strategy": "create_custom"})
    validation = await _validate(current, {"op": "agent_mapping.remove", "payload": {
        "service_key": "video_studio.quality",
    }})
    # Indexes shift by one; identity is the problem, not its array position.
    assert validation["errors"] == []
    assert sorted(issue["path"] for issue in validation["pre_existing_issues"]) == [
        "agent_mappings[1].service_key", "agent_mappings[2].service_key",
    ]


@pytest.mark.asyncio
async def test_a_patch_that_repairs_the_state_reports_no_pre_existing_issue():
    current = _state_with_undeclared_services()
    validation = await _validate(current, *[
        {"op": "service.upsert", "payload": {"key": key, "name": key}}
        for key in ("product_video.exploration", "product_video.planning")
    ])
    assert validation["valid"] is True
    assert validation["errors"] == validation["pre_existing_issues"] == []


def _duplicate_state():
    state = _state_with_undeclared_services()
    state["services"] = [{"key": key} for key in ("a", "b", "c")]
    state["agent_mappings"] = [
        {"service_key": "a", "strategy": "create_custom", "n": 1},
        {"service_key": "a", "strategy": "create_custom", "n": 2},  # inherited duplicate on a
        {"service_key": "b", "strategy": "create_custom", "n": 3},
    ]
    return state


@pytest.mark.asyncio
async def test_fixing_one_duplicate_cannot_launder_a_new_one_with_the_same_message():
    current = _duplicate_state()
    validation = await _validate(current, {"op": "agent_mapping.replace", "payload": {"mappings": [
        {"service_key": "a", "strategy": "create_custom", "n": 1},
        {"service_key": "b", "strategy": "create_custom", "n": 3},
        {"service_key": "b", "strategy": "create_custom", "n": 4},  # NEW duplicate on b
    ]}})
    assert validation["valid"] is False
    assert [(e["path"], e["message"]) for e in validation["errors"]] == [
        ("agent_mappings[2].service_key", "only one Agent may be mapped to each service_key"),
    ]
    assert validation["pre_existing_issues"] == []


@pytest.mark.asyncio
async def test_untouched_inherited_duplicate_is_still_inherited():
    current = _duplicate_state()
    validation = await _validate(current, {"op": "budget.update", "payload": {"monthly_budget_credits": 5}})
    assert validation["valid"] is True
    assert [issue["path"] for issue in validation["pre_existing_issues"]] == ["agent_mappings[1].service_key"]


class _AgentDb:
    """Answers only the Agent lookup the mapping checks make."""

    def __init__(self, *agents):
        self.agents = list(agents)

    async def execute(self, *_args, **_kwargs):
        rows = self.agents
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _agent(agent_id, entity_id):
    return SimpleNamespace(id=agent_id, entity_id=entity_id, is_template=False)


def _foreign_state():
    state = _state_with_undeclared_services()
    state["services"] = [{"key": "a"}, {"key": "b"}]
    state["agent_mappings"] = [{"service_key": "a", "agent_id": "foreign-1"}]
    return state


@pytest.mark.asyncio
async def test_swapping_one_foreign_entity_agent_for_another_is_rejected():
    current = _foreign_state()
    draft = _draft(current, {"op": "agent_mapping.upsert", "payload": {
        "service_key": "a", "agent_id": "foreign-2",
    }})
    validation = await operations.validate_operation_draft(
        _AgentDb(_agent("foreign-1", "other"), _agent("foreign-2", "other")), draft, current_state=current,
    )
    assert validation["valid"] is False
    assert [(e["path"], e["code"]) for e in validation["errors"]] == [
        ("agent_mappings[0].agent_id", "foreign_entity_agent"),
    ]


@pytest.mark.asyncio
async def test_tenant_boundary_errors_are_never_inherited():
    current = _foreign_state()
    draft = _draft(current, {"op": "budget.update", "payload": {"monthly_budget_credits": 5}})
    validation = await operations.validate_operation_draft(
        _AgentDb(_agent("foreign-1", "other")), draft, current_state=current,
    )
    # An unrelated edit cannot ride along while another tenant's Agent is deployed here.
    assert validation["valid"] is False
    assert [e["code"] for e in validation["errors"]] == ["foreign_entity_agent"]
    assert validation["pre_existing_issues"] == []


def test_never_inheritable_codes_cover_the_access_and_tenant_boundaries():
    from packages.core.services.workspace_operation_validation import NEVER_INHERITED_ERROR_CODES

    assert {
        "foreign_entity_agent", "unavailable_workflow",
        "unavailable_app_target", "unavailable_channel_config", "task_scoped_binding_unsupported",
        "rule_implied_governance", "invalid_patch_shape", "duplicate_service_change",
    } <= NEVER_INHERITED_ERROR_CODES


def test_validation_error_names_the_first_paths_for_the_model():
    exc = operations.OperationValidationError({"valid": False, "errors": [
        {"path": f"agent_mappings[{index}].service_key", "message": f"problem {index}"}
        for index in range(5)
    ]})
    text = str(exc)
    assert text.startswith("workspace operation draft has validation errors")
    assert "agent_mappings[0].service_key: problem 0" in text
    assert "agent_mappings[2].service_key: problem 2" in text
    assert "agent_mappings[4]" not in text and "2 more" in text
    assert exc.validation["errors"][4]["message"] == "problem 4"


def test_validation_error_without_paths_keeps_its_plain_message():
    assert str(operations.OperationValidationError({"errors": []})) == (
        "workspace operation draft has validation errors"
    )


# The 2026-10-28 benchmark shape: a per-person discount limit drafted as a block
# on words no runtime call carries. It was shown as enforced and gated nothing.
_LIMIT_AS_BLOCK = {
    "rule_key": "discount_cap", "rule_type": "block",
    "description": "单人优惠金额不超过 6 元", "action_patterns": ["promote", "discount", "coupon"],
}


@pytest.mark.asyncio
async def test_rule_on_actions_no_call_carries_is_rejected_and_never_marked_enforced():
    current = _state_with_undeclared_services()
    validation = await _validate(current, {"op": "rule.add", "payload": dict(_LIMIT_AS_BLOCK)})

    assert validation["valid"] is False
    [error] = [e for e in validation["errors"] if e.get("code") == "rule_action_not_gateable"]
    assert "promote, discount, coupon" in error["message"]
    added = _draft(current, {"op": "rule.add", "payload": dict(_LIMIT_AS_BLOCK)}).current_state["rules"][-1]
    assert "runtime_enforced" not in added


@pytest.mark.asyncio
async def test_rule_on_a_declared_action_is_enforced_and_valid():
    current = _state_with_undeclared_services()
    rule = {"rule_key": "review_posts", "rule_type": "approval_required",
            "description": "发布前审批", "action_patterns": ["social_post.publish"]}
    validation = await _validate(current, {"op": "rule.add", "payload": rule})

    assert not [e for e in validation["errors"] if e.get("code") == "rule_action_not_gateable"]
    added = _draft(current, {"op": "rule.add", "payload": rule}).current_state["rules"][-1]
    assert added["runtime_enforced"] is True


@pytest.mark.asyncio
async def test_a_stored_rule_that_gates_nothing_does_not_block_unrelated_changes():
    current = _state_with_undeclared_services()
    current["rules"] = [dict(_LIMIT_AS_BLOCK)]
    validation = await _validate(
        current, {"op": "budget.update", "payload": {"monthly_budget_credits": 200}},
    )
    assert validation["valid"] is True
    assert "rules[0].action_patterns" in {issue["path"] for issue in validation["pre_existing_issues"]}
