"""Construct the same inherited policy used by production, without storage."""
from packages.core.governance.approval_facts import action_facts
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain


def evaluate_runtime_action(action, *, mode="risk_based", rules=()):
    assert action is not None, "A classified action is required"
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"),
        root={"revision": 1, "policy": {"mode": mode, "rules": list(rules)}})
    return evaluate_policy(chain, action_facts(action_key=action.action_key,
        capability_id=action.capability_id, resource_kind=action.resource_kind,
        resource_id=action.resource_id, risk_level=action.risk_level, operation=action.operation,
        **action.policy_effect()))
