"""A workspace that EXPLICITLY auto-approves a capability/action overrides a
capability's intrinsic ``required_approval`` (e.g. file.write). This is the
mechanism that lets a workspace stop being prompted for file writes without
weakening the global capability default."""

from __future__ import annotations

import asyncio

import packages.core.governance.service as gov
from packages.core.governance.policy import WorkspacePolicy, policy_auto_approves


# ── pure: policy_auto_approves ────────────────────────────────────────


def test_auto_approves_matching_capability():
    p = WorkspacePolicy(auto_approve_capabilities=["file.write"])
    assert policy_auto_approves(p, capability_id="file.write") is True


def test_auto_approves_matching_action_and_wildcard():
    p = WorkspacePolicy(auto_approve_actions=["x.*"])
    assert policy_auto_approves(p, action_key="x.like") is True


def test_default_allow_is_not_auto_approve():
    # Empty policy: nothing denies file.write, but that is NOT an explicit
    # auto-approval — the intrinsic approval gate must still fire.
    assert policy_auto_approves(WorkspacePolicy(), capability_id="file.write") is False


def test_hitl_or_never_allow_only_is_not_auto_approve():
    p = WorkspacePolicy(
        hitl_required_capabilities=["file.write"],
        never_allow_capabilities=["x.delete"],
    )
    assert policy_auto_approves(p, capability_id="file.write") is False
    assert policy_auto_approves(p, capability_id="x.delete") is False


def test_no_subject_is_not_auto_approve():
    p = WorkspacePolicy(auto_approve_capabilities=["*"])
    assert policy_auto_approves(p, action_key=None, capability_id=None) is False


# ── service: workspace_policy_auto_approves (loads policy) ────────────


def test_workspace_policy_auto_approves_true(monkeypatch):
    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy(auto_approve_capabilities=["file.write"])

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    out = asyncio.run(
        gov.workspace_policy_auto_approves(
            None,
            workspace_id="ws",
            capability_id="file.write",
        )
    )
    assert out is True


def test_workspace_policy_auto_approves_false_without_rule(monkeypatch):
    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy()

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    out = asyncio.run(
        gov.workspace_policy_auto_approves(
            None,
            workspace_id="ws",
            capability_id="file.write",
        )
    )
    assert out is False


def test_workspace_policy_auto_approves_false_without_workspace(monkeypatch):
    # No workspace → nothing to opt into; must not even load a policy.
    monkeypatch.setattr(gov, "get_policy", None)
    out = asyncio.run(
        gov.workspace_policy_auto_approves(
            None,
            workspace_id="",
            capability_id="file.write",
        )
    )
    assert out is False
