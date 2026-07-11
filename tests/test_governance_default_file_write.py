"""file.write is auto-approved by DEFAULT so workspaces stop prompting for
file writes — while a raw ``WorkspacePolicy()`` stays "no opinion" (empty)."""

from __future__ import annotations

import asyncio

from packages.core.governance.policy import DEFAULT_POLICY, WorkspacePolicy, policy_auto_approves
from packages.core.governance.presets import apply_preset


def test_default_policy_auto_approves_file_write():
    assert policy_auto_approves(DEFAULT_POLICY, capability_id="file.write") is True


def test_raw_policy_still_no_opinion():
    from packages.core.governance.policy import WorkspacePolicy

    assert policy_auto_approves(WorkspacePolicy(), capability_id="file.write") is False


def test_get_policy_falls_back_to_file_write_granting_default():
    """A workspace with no persisted row gets DEFAULT_POLICY, which grants
    file.write — so the dispatcher's intrinsic gate is overridden by default."""
    import packages.core.governance.service as gov

    class _FakeResult:
        def scalar_one_or_none(self):
            return None

    class _FakeDB:
        async def execute(self, *args, **kwargs):
            return _FakeResult()

    policy = asyncio.run(gov.get_policy(_FakeDB(), "ws-no-row"))
    assert policy_auto_approves(policy, capability_id="file.write") is True


def test_standard_preset_grants_file_write():
    out = apply_preset(WorkspacePolicy(), "standard")
    assert policy_auto_approves(out, capability_id="file.write") is True


def test_aggressive_preset_grants_file_write():
    out = apply_preset(WorkspacePolicy(), "aggressive")
    assert policy_auto_approves(out, capability_id="file.write") is True


def test_safe_preset_does_not_grant_file_write():
    base = WorkspacePolicy(auto_approve_capabilities=["file.write"])
    out = apply_preset(base, "safe")
    assert policy_auto_approves(out, capability_id="file.write") is False


# ── manor.composite default-grant (composite gateway: workspace doc/knowledge) ──


def test_default_policy_auto_approves_manor_composite():
    from packages.core.governance.policy import DEFAULT_POLICY, policy_auto_approves

    assert policy_auto_approves(DEFAULT_POLICY, capability_id="manor.composite") is True


def test_standard_preset_grants_manor_composite():
    from packages.core.governance.policy import WorkspacePolicy, policy_auto_approves
    from packages.core.governance.presets import apply_preset

    out = apply_preset(WorkspacePolicy(), "standard")
    assert policy_auto_approves(out, capability_id="manor.composite") is True
    # file.write still granted too (both default caps)
    assert policy_auto_approves(out, capability_id="file.write") is True


def test_aggressive_preset_grants_manor_composite():
    from packages.core.governance.policy import WorkspacePolicy, policy_auto_approves
    from packages.core.governance.presets import apply_preset

    out = apply_preset(WorkspacePolicy(), "aggressive")
    assert policy_auto_approves(out, capability_id="manor.composite") is True


def test_safe_preset_strips_manor_composite():
    from packages.core.governance.policy import WorkspacePolicy, policy_auto_approves
    from packages.core.governance.presets import apply_preset

    base = WorkspacePolicy(auto_approve_capabilities=["file.write", "manor.composite"])
    out = apply_preset(base, "safe")
    assert policy_auto_approves(out, capability_id="manor.composite") is False
    assert policy_auto_approves(out, capability_id="file.write") is False
