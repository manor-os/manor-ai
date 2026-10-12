"""Workspace inspection must reach the same hard read gate through every tool surface."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
    RuntimeAuthorizationAccess,
)


def _call(surface, params):
    if surface == "workspace_operation":
        return surface, params
    wrapped = {"action": "operation", "params": params}
    if surface == "workspace_agent":
        return surface, wrapped
    return "manor", {"action": "workspace", "params": wrapped}


class _SessionContext:
    async def __aenter__(self):
        return SimpleNamespace(commit=AsyncMock())

    async def __aexit__(self, *_args):
        return False


_SURFACES = ("workspace_operation", "workspace_agent", "manor")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("params", [
    {"action": "get_current"},
    {"action": "preview_diff", "draft_id": "draft"},
    {"action": "validate_draft", "draft_id": "draft"},
    {"op": "get"},
    {"operation": "preview", "draft_id": "draft"},
])
async def test_operation_reads_cross_the_real_runtime_hard_authorization_gate(monkeypatch, surface, params):
    name, arguments = _call(surface, params)
    permission = AsyncMock(return_value=PermissionDecision.allow(AuthorizationRule.WORKSPACE_READ))
    from packages.core.governance.approvals import ApprovalDecision, ApprovalOutcome

    approval = AsyncMock(return_value=ApprovalDecision(ApprovalOutcome.ALLOW))
    monkeypatch.setattr("packages.core.database.async_session", _SessionContext)
    monkeypatch.setattr("packages.core.services.runtime_authorization.authorize_runtime_action", permission)
    monkeypatch.setattr("packages.core.governance.approvals.resolve_approval", approval)
    monkeypatch.setattr("packages.core.budget.get_workspace_spent_credits_per_kind", AsyncMock(return_value={}))

    result = await guard_runtime_tool_action(
        name=name, arguments=arguments, entity_id="entity", user_id="user",
        workspace_id="workspace", conversation_id="conversation",
    )

    assert result is None
    permission.assert_awaited_once()
    checked = permission.call_args.kwargs
    assert checked["access"] is RuntimeAuthorizationAccess.READ
    assert checked["capability_id"] == "workspace.governance"
    assert checked["action_key"] == "tool.workspace_operation.read"
    assert checked["workspace_id"] == "workspace"
    assert checked["entity_id"] == "entity"
    assert checked["user_id"] == "user"
    assert checked["tool_name"] == name
    approval.assert_awaited_once()
    assert approval.await_args.kwargs["subject"].impact == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _SURFACES)
async def test_operation_reads_still_fail_when_hard_permission_is_revoked(monkeypatch, surface):
    name, arguments = _call(surface, {"action": "get_current"})
    permission = AsyncMock(return_value=PermissionDecision.deny("workspace access revoked", AuthorizationRule.DENIED))
    monkeypatch.setattr("packages.core.database.async_session", _SessionContext)
    monkeypatch.setattr("packages.core.services.runtime_authorization.authorize_runtime_action", permission)

    result = await guard_runtime_tool_action(
        name=name, arguments=arguments, entity_id="entity", user_id="user",
        workspace_id="workspace", conversation_id="conversation",
    )

    assert json.loads(result)["error"] == "blocked_by_permission"
    permission.assert_awaited_once()


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(("params", "action_key"), [
    ({"action": "create_draft"}, "workspace.operation.draft"),
    ({"op": "create"}, "workspace.operation.draft"),
    ({"action": "patch_draft", "draft_id": "draft"}, "workspace.operation.draft"),
    ({"operation": "approve", "draft_id": "draft"}, "workspace.operation.apply"),
    ({"action": "apply_draft", "op": "get_current", "draft_id": "draft"}, "workspace.operation.apply"),
    ({"action": "discard_draft", "draft_id": "draft"}, "workspace.operation.discard"),
])
def test_operation_writes_keep_their_action_gate_even_through_aliases(surface, params, action_key):
    name, arguments = _call(surface, params)
    classification = classify_runtime_tool(name, arguments)
    assert classification.effect is RuntimeToolEffect.ACTION
    assert classification.action.action_key == action_key
    assert classification.authorization.access is RuntimeAuthorizationAccess.ACTION


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("params", [{}, {"action": "unknown"}, {"op": "unknown"}, {"action": "unknown", "op": "get_current"}])
async def test_unknown_operation_leaf_fails_before_any_authorization_or_approval(monkeypatch, surface, params):
    name, arguments = _call(surface, params)
    session = AsyncMock(side_effect=AssertionError("unknown leaves must fail before opening authorization state"))
    monkeypatch.setattr("packages.core.database.async_session", session)

    result = await guard_runtime_tool_action(
        name=name, arguments=arguments, entity_id="entity", user_id="user",
        workspace_id="workspace", conversation_id="conversation",
    )

    classification = classify_runtime_tool(name, arguments)
    assert classification.effect is RuntimeToolEffect.UNKNOWN
    assert json.loads(result)["matched_rule"] == "permission.tool.classification"
    session.assert_not_called()
