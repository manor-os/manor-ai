"""Recheck current hard boundaries before applying a Proposal card decision."""
from __future__ import annotations

from packages.core.constants.approvals import HitlType
from packages.core.constants.proposal import ProposalItemKind
from packages.core.governance.approval_policy import ApprovalScope
from packages.core.governance.approval_policy_service import (
    ApprovalPolicyAccessError, ApprovalPolicyIntegrityError, _load,
)
from packages.core.models.workspace import Workspace


class ProposalApprovalBlocked(ValueError):
    pass


async def lock_proposal_approval_boundary(db, *, entity_id: str, workspace_id: str):
    """Match settings writes: account -> Workspace -> policy, before Message."""
    try:
        await _load(db, ApprovalScope(kind="workspace", id=workspace_id, entity_id=entity_id), lock=True)
    except (ApprovalPolicyAccessError, ApprovalPolicyIntegrityError) as exc:
        raise ProposalApprovalBlocked(str(exc)) from exc
    workspace = await db.get(Workspace, workspace_id)
    from packages.core.services.workspace_autonomy import workspace_allows_manual_work

    if not workspace_allows_manual_work(workspace):
        raise ProposalApprovalBlocked("Workspace is unavailable for proposal execution")


async def require_current_proposal_policy(db, *, record, items, actor_id, actor_kind="user"):
    """A new human click satisfies ask, never a current deny or outer limit.

    Check the whole selected cohort before its first mutation. The non-task
    lifecycle also invokes this before each item, since Flow dispatch may
    commit its receipt and release the previous transaction's policy locks.
    """
    if not items:
        return
    if actor_kind == "user":
        await lock_proposal_approval_boundary(db, entity_id=record.entity_id, workspace_id=record.workspace_id)
    from packages.core.budget import get_workspace_spent_credits_per_kind
    from packages.core.governance.service import check_step_policy
    from packages.core.constants.runtime_principal import RuntimePrincipalKind
    from packages.core.services.runtime_authorization import authorize_hitl_action, authorize_runtime_action

    spent = await get_workspace_spent_credits_per_kind(db, record.workspace_id)
    for item in items:
        if item.entity_id != record.entity_id or item.workspace_id != record.workspace_id or item.proposal_id != record.id:
            raise ProposalApprovalBlocked("Proposal item scope changed")
        is_task = item.kind == ProposalItemKind.TASK
        capability = "workflow.run" if item.kind == ProposalItemKind.WORKFLOW_RUN else None
        if actor_kind == "user":
            authority = await authorize_hitl_action(db, entity_id=record.entity_id,
                workspace_id=record.workspace_id, by_user_id=actor_id,
                action_key=item.action_key, capability_id=capability, hitl_type=HitlType.AUTHORIZE.value)
        elif actor_kind == "system":
            authority = await authorize_runtime_action(db, entity_id=record.entity_id,
                workspace_id=record.workspace_id, user_id=None, action_key=item.action_key,
                capability_id=capability, access="action", principal_kind=RuntimePrincipalKind.SYSTEM_WORKER)
        else:
            raise ProposalApprovalBlocked("Unknown Proposal approval actor")
        if not authority.allowed:
            raise ProposalApprovalBlocked(authority.reason or "Proposal approval permission denied")
        decision = await check_step_policy(db, entity_id=record.entity_id,
            workspace_id=record.workspace_id, user_id=actor_id, kind="action",
            action_key=item.action_key, capability_id=capability, risk_level=item.risk_level,
            resource_kind="proposal" if is_task else "proposal_item",
            resource_id=record.id if is_task else item.id,
            task_id=(item.payload or {}).get("task_id") if is_task else None,
            spent_credits_per_kind=spent)
        if not decision.allowed and not decision.pause_for_hitl:
            raise ProposalApprovalBlocked(decision.reason or "Proposal is blocked by current policy")
        if decision.pause_for_hitl and actor_kind != "user":
            raise ProposalApprovalBlocked("Current Proposal policy requires a human decision")
