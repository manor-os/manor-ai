"""Workspace operation patch ``governance_policy.update`` must be supported.

Agents/UI emit ``governance_policy.update`` patches to adjust a workspace's
governance policy (never-allow / HITL / risk ceiling). The dispatcher had no
handler for it, so it fell through to
``ValueError: unsupported workspace operation patch: governance_policy.update``
(seen in production API logs). It must merge into the governance policy and
sync into the operating model, mirroring budget_policy.update / knowledge.update.
"""

from __future__ import annotations

import pytest

from packages.core.services.workspace_operation_service import _apply_single_patch


def test_governance_policy_update_merges_and_syncs():
    state = {
        "operating_model": {},
        "governance_policy": {"never_allow_actions": ["file.delete"]},
    }
    patch = {
        "op": "governance_policy.update",
        "governance_policy": {"hitl_required_actions": ["external.post"]},
    }

    out = _apply_single_patch(state, patch)

    # merged, not replaced
    assert out["governance_policy"]["never_allow_actions"] == ["file.delete"]
    assert out["governance_policy"]["hitl_required_actions"] == ["external.post"]
    # synced into the operating model under `governance`
    assert out["operating_model"]["governance"]["hitl_required_actions"] == ["external.post"]


def test_governance_update_alias_accepts_flat_payload():
    state = {"operating_model": {}, "governance_policy": {}}
    patch = {"op": "governance.update", "max_risk_level": "low"}

    out = _apply_single_patch(state, patch)

    assert out["governance_policy"]["max_risk_level"] == "low"


def test_unknown_op_still_raises():
    with pytest.raises(ValueError, match="unsupported workspace operation patch"):
        _apply_single_patch({"operating_model": {}}, {"op": "totally.bogus"})
