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
from packages.core.governance.policy import policy_auto_approves
from packages.core.governance.service import (
    add_auto_approve_action,
    add_auto_approve_capability,
    check_step_policy,
    get_policy,
    list_revisions,
    update_policy,
    workspace_policy_auto_approves,
)

__all__ = [
    "DEFAULT_POLICY",
    "PolicyDecision",
    "PolicyError",
    "WorkspacePolicy",
    "policy_from_dict",
    "policy_to_dict",
    "policy_auto_approves",
    "add_auto_approve_action",
    "add_auto_approve_capability",
    "check_step_policy",
    "get_policy",
    "list_revisions",
    "update_policy",
    "workspace_policy_auto_approves",
]
