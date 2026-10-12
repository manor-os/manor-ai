"""Deterministic smoke for the single approval evaluator and hard constraints.

Run with: uv run python -m packages.core.governance._smoke
Storage, CAS, owner binding and migration integration are covered by
``tests/test_approval_policy_settings.py``.
"""
from __future__ import annotations

from packages.core.governance.approval_policy import (
    ActionFacts, ApprovalPolicyBinding, ApprovalPolicyOverride, ApprovalRule,
    ApprovalScope, evaluate_policy,
)
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.policy import (
    PolicyError, WorkspacePolicy, decide, policy_from_dict, policy_to_dict,
)


def main() -> None:
    rules = [
        ApprovalRule(id="ban", effect="deny", selector={"kind": "action", "action_key": "billing.refund"}),
        ApprovalRule(id="send", effect="ask", selector={"kind": "action", "action_key": "email.send"}),
    ]
    policy = WorkspacePolicy(
        approval=ApprovalPolicyBinding(root_entity_id="account", policy=ApprovalPolicyOverride(rules=rules)),
        max_risk_level="medium", budget_caps_per_kind={"action": 100},
    )
    assert policy_from_dict(policy_to_dict(policy)) == policy
    chain = build_policy_chain(
        scope=ApprovalScope(kind="workspace", id="workspace", entity_id="account"),
        root={"revision": 1, "policy": {"mode": "risk_based", "rules": []}},
        local={"revision": 1, **policy.approval.model_dump()},
    )
    for action, impact, expected in (
        ("billing.refund", "external", "deny"),
        ("email.send", "external", "ask"),
        ("file.edit", "reversible", "allow"),
        ("custom.operation", "unknown", "ask"),
    ):
        assert evaluate_policy(chain, ActionFacts(action_key=action, impact=impact)).outcome == expected
    assert not decide(policy, kind="action", action_key="email.send", risk_level="high").allowed
    assert not decide(policy, kind="action", action_key="email.send", risk_level="low",
                      spent_credits_per_kind={"action": 100}).allowed
    for invalid in ({"max_risk_level": "extreme"}, {"auto_approve_actions": ["*"]}, {"approval": True}):
        try:
            policy_from_dict(invalid)
        except PolicyError:
            pass
        else:
            raise AssertionError(f"Invalid policy accepted: {invalid}")
    print("SMOKE OK: unified evaluator, strict decoding, risk and budget boundaries")


if __name__ == "__main__":
    main()
