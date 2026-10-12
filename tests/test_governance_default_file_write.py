"""Default file behavior derives from the account mode, never a missing row."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import ApprovalPolicyIntegrityError, build_policy_chain
from packages.core.governance.approval_projection import policy_override
from packages.core.governance.policy import DEFAULT_POLICY, WorkspacePolicy
from packages.core.governance.presets import apply_preset


@pytest.mark.parametrize("mode,action,outcome", [
    ("risk_based", "workspace.file.modify", "allow"),
    ("risk_based", "workspace.file.delete", "ask"),
    ("confirm_changes", "workspace.file.modify", "ask"),
    ("automatic", "workspace.file.modify", "allow"),
    # The chosen mode decides even when the effect is unnamed.
    ("automatic", "provider.unknown", "allow"),
    ("confirm_changes", "provider.unknown", "ask"),
])
def test_account_mode_controls_file_operations(mode, action, outcome):
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": mode, "rules": []}})
    assert evaluate_policy(chain, action_facts(action_key=action, resource_kind="file", risk_level="low")).outcome == outcome


def test_raw_policy_has_no_implicit_capability_grants():
    assert DEFAULT_POLICY.approval is None
    assert WorkspacePolicy().approval is None


@pytest.mark.asyncio
async def test_missing_persisted_policy_fails_closed(monkeypatch):
    from packages.core.governance import service
    cache = SimpleNamespace(get_current=AsyncMock(return_value=None))
    monkeypatch.setattr(service.GovernancePolicyCacheFactory, "create_default", lambda: cache)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: None)))
    with pytest.raises(ApprovalPolicyIntegrityError, match="missing"):
        await service.get_policy(db, "ws-no-row")


@pytest.mark.parametrize("preset,mode", [("follow_default", None), ("risk_based", "risk_based"), ("confirm_changes", "confirm_changes")])
def test_install_mode_choices_preserve_explicit_rules(preset, mode):
    base = WorkspacePolicy(approval=ApprovalPolicyOverride(mode="automatic", rules=[{
        "id": "review", "effect": "ask", "selector": {"kind": "action", "action_key": "workspace.file.modify"},
    }]))
    out = apply_preset(base, preset)
    assert policy_override(out).mode == mode
    assert policy_override(out).rules == policy_override(base).rules
    assert policy_override(base).mode == "automatic"


@pytest.mark.parametrize("preset", ["standard", "aggressive", "safe", "no_default_grants"])
def test_retired_install_presets_require_current_client(preset):
    with pytest.raises(KeyError, match="refresh the client"):
        apply_preset(WorkspacePolicy(), preset)
