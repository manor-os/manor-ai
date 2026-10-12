"""Workspace governance — operator-controlled rules of engagement.

Two pieces:

  policy.py    Validated dataclass for the policy JSONB shape +
               default ("permissive") policy used when none has been
               saved yet.

  service.py   Read / write helpers (always paired with a revision row)
               plus ``check_step_policy`` which the Dispatcher calls
               per step at lease checkout.
"""
from packages.core.governance.policy import (
    DEFAULT_POLICY,
    PolicyDecision,
    PolicyError,
    WorkspacePolicy,
    policy_from_dict,
    policy_to_dict,
)
from packages.core.governance.policy_cache import GovernancePolicyCacheUnavailable
from packages.core.governance.service import (
    add_auto_approve_action,
    add_auto_approve_capability,
    automated_run_always_allowed,
    check_step_policy,
    get_policy,
    list_revisions,
    remove_auto_approve_action,
    remove_auto_approve_capability,
    update_policy,
    workspace_policy_auto_approves,
)


from packages.core.governance.approvals import (
    ApprovalConsumptionError,
    ApprovalDecision,
    ApprovalOrigin,
    ApprovalSubject,
    claim_approval_consumption,
    consume_approval,
    count_open_requests,
    deny_approval,
    grant_approval,
    resolve_approval,
    resolve_origin_requests,
)
from packages.core.constants.approvals import ApprovalConsumeOutcome, ApprovalOutcome

__all__ = [
    "ApprovalConsumeOutcome", "ApprovalConsumptionError", "ApprovalDecision",
    "ApprovalOrigin", "ApprovalOutcome", "ApprovalSubject",
    "resolve_approval", "grant_approval", "deny_approval",
    "claim_approval_consumption", "consume_approval",
    "resolve_origin_requests", "count_open_requests",
    "DEFAULT_POLICY",
    "PolicyDecision",
    "PolicyError",
    "WorkspacePolicy",
    "GovernancePolicyCacheUnavailable",
    "policy_from_dict",
    "policy_to_dict",
    "add_auto_approve_action",
    "add_auto_approve_capability",
    "automated_run_always_allowed",
    "remove_auto_approve_action",
    "remove_auto_approve_capability",
    "check_step_policy",
    "get_policy",
    "list_revisions",
    "update_policy",
    "workspace_policy_auto_approves",
]
