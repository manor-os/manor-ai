"""Governance read / write helpers + the Dispatcher hook.

Every mutation goes through ``update_policy`` so every change writes
both the current row and an audit revision atomically. The Dispatcher
calls ``check_step_policy`` per step at lease checkout; on a HITL
decision it pauses the step and posts a chat card via
``packages.core.workspace_chat``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from typing import Any, Optional

from sqlalchemy import desc, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance.policy import (
    PolicyDecision,
    WorkspacePolicy,
    decide,
    policy_from_dict,
    policy_to_dict,
)
from packages.core.constants.approvals import HitlType
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.services.hitl_options import decision_options_for_hitl
from packages.core.models.governance import (
    GovernancePolicy,
    GovernanceRevision,
)
from packages.core.governance.policy_cache import (
    GovernancePolicyCache,
    GovernancePolicyCacheFactory,
    GovernancePolicyCacheUnavailable,
    GovernancePolicyMutationFence,
    GovernancePolicySnapshot,
)

logger = logging.getLogger(__name__)

_POLICY_CACHE_MUTATIONS_KEY = "governance_policy_cache_mutations"
_POLICY_CACHE_LISTENERS_KEY = "governance_policy_cache_listeners"


def _schedule_cache_task(loop, callback, *args) -> None:
    if loop.is_closed():
        return

    try:
        if asyncio.get_running_loop() is loop:
            loop.create_task(callback(*args))
            return
    except RuntimeError:
        pass

    def schedule() -> None:
        loop.create_task(callback(*args))

    if loop.is_running():
        loop.call_soon_threadsafe(schedule)


def _publish_policy_cache_after_commit(session) -> None:
    if session.in_nested_transaction():
        return
    mutations = session.info.pop(_POLICY_CACHE_MUTATIONS_KEY, [])
    for loop, policy_cache, fence, snapshot, _owner in mutations:
        _schedule_cache_task(
            loop,
            policy_cache.commit_mutation,
            fence,
            snapshot,
        )


def _release_policy_cache_after_rollback(session) -> None:
    if session.in_nested_transaction():
        return
    rolled_back = session.info.pop(_POLICY_CACHE_MUTATIONS_KEY, [])
    for loop, policy_cache, fence, _snapshot, _owner in rolled_back:
        _schedule_cache_task(loop, policy_cache.rollback_mutation, fence)


def _release_policy_cache_after_soft_rollback(
    session,
    previous_transaction,
) -> None:
    if not previous_transaction.nested:
        return
    mutations = session.info.get(_POLICY_CACHE_MUTATIONS_KEY, [])
    rolled_back = [
        item
        for item in mutations
        if _transaction_descends_from(item[4], previous_transaction)
    ]
    session.info[_POLICY_CACHE_MUTATIONS_KEY] = [
        item for item in mutations if item not in rolled_back
    ]
    for loop, policy_cache, fence, _snapshot, _owner in rolled_back:
        _schedule_cache_task(loop, policy_cache.rollback_mutation, fence)


def _transaction_descends_from(transaction, ancestor) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _register_policy_cache_mutation(
    db: AsyncSession,
    *,
    policy_cache: GovernancePolicyCache,
    fence: GovernancePolicyMutationFence,
    snapshot: GovernancePolicySnapshot,
) -> None:
    sync_session = db.sync_session
    owner = sync_session.get_nested_transaction()
    sync_session.info.setdefault(_POLICY_CACHE_MUTATIONS_KEY, []).append(
        (asyncio.get_running_loop(), policy_cache, fence, snapshot, owner)
    )
    if sync_session.info.get(_POLICY_CACHE_LISTENERS_KEY):
        return
    event.listen(sync_session, "after_commit", _publish_policy_cache_after_commit)
    event.listen(sync_session, "after_rollback", _release_policy_cache_after_rollback)
    event.listen(
        sync_session,
        "after_soft_rollback",
        _release_policy_cache_after_soft_rollback,
    )
    sync_session.info[_POLICY_CACHE_LISTENERS_KEY] = True


# ── Read ──────────────────────────────────────────────────────────────

async def get_policy(
    db: AsyncSession, workspace_id: str,
) -> WorkspacePolicy:
    """Read an initialized Workspace binding; missing configuration fails closed."""
    policy_cache = GovernancePolicyCacheFactory.create_default()
    cached = await policy_cache.get_current(workspace_id)
    if cached is not None:
        from packages.core.governance.approval_policy import ApprovalPolicyBinding
        policy = policy_from_dict(cached.policy)
        if not isinstance(policy.approval, ApprovalPolicyBinding):
            from packages.core.governance.approval_policy_service import ApprovalPolicyIntegrityError
            raise ApprovalPolicyIntegrityError("Workspace approval binding is missing")
        return policy

    row = (await db.execute(
        select(GovernancePolicy.policy, GovernancePolicy.revision).where(
            GovernancePolicy.workspace_id == workspace_id
        )
    )).one_or_none()
    if row is None:
        from packages.core.governance.approval_policy_service import ApprovalPolicyIntegrityError
        raise ApprovalPolicyIntegrityError("Workspace approval configuration is missing")
    snapshot = GovernancePolicySnapshot(
        workspace_id=workspace_id, revision=int(row.revision or 0), policy=dict(row.policy or {}),
    )
    await policy_cache.store_if_current(snapshot)
    policy = policy_from_dict(snapshot.policy)
    from packages.core.governance.approval_policy import ApprovalPolicyBinding
    if not isinstance(policy.approval, ApprovalPolicyBinding):
        from packages.core.governance.approval_policy_service import ApprovalPolicyIntegrityError
        raise ApprovalPolicyIntegrityError("Workspace approval binding is missing")
    return policy


async def workflow_run_is_executable(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    workflow_run_id: str | None,
    statuses: set[str] | None = None,
) -> bool:
    """Validate delegated execution against the current durable Workflow origin.

    A started Workflow owns its tool execution; human decisions belong to its
    explicit wait nodes. This is not a substitute for tool permissions, hard
    governance rules, or credit checks.
    """
    from packages.core.models.workflow import WorkflowRun

    if not entity_id or not user_id or not workflow_run_id:
        return False
    run = await db.get(WorkflowRun, workflow_run_id, populate_existing=True)
    if not (
        run is not None
        and run.entity_id == entity_id
        and (run.workspace_id or None) == (workspace_id or None)
        and run.started_by == user_id
        and run.status in (statuses or {"running"})
    ):
        return False
    if not workspace_id:
        return True

    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace
    from packages.core.permissions import resolve_effective_user_role_name
    from packages.core.services.workspace_access import user_can_read_workspace_by_identity
    from packages.core.services.workspace_autonomy import workspace_allows_configured_work
    from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
    from packages.core.workspaces import is_sandbox_workspace

    actor = await db.get(User, user_id, populate_existing=True)
    workspace = await db.get(Workspace, workspace_id, populate_existing=True)
    actor_role = await resolve_effective_user_role_name(
        db,
        user_id=user_id,
        entity_id=entity_id,
        legacy_role=(actor.role if actor is not None and actor.entity_id == entity_id else None),
    )
    if (
        actor is None
        or actor.status != "active"
        or actor.deleted_at is not None
        or not actor_role
        or workspace is None
        or workspace.entity_id != entity_id
        or not workspace_allows_configured_work(workspace)
        or is_sandbox_workspace(workspace)
    ):
        return False
    if not await user_can_read_workspace_by_identity(
        db,
        workspace=workspace,
        entity_id=entity_id,
        user_id=user_id,
        role=actor_role,
    ):
        return False
    setup_status = await evaluate_workspace_blocking_setup(db, workspace)
    return setup_status is None or not setup_status.blocks_work


async def automated_run_always_allowed(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    workflow_run_id: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Run identity is not consent; actions use the unified policy gate."""
    return False


async def scheduled_automation_origin_is_valid(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
    execution_type: str | None = None,
) -> bool:
    """Verify origin identity without granting consent to any child action."""
    return await _scheduled_automation_origin_matches(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
        execution_type=execution_type,
    )


async def scheduled_workflow_admission_is_valid(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
    workflow_id: str,
    binding_id: str | None = None,
) -> bool:
    """Validate one enabled scheduled Workflow deployment before it starts.

    This is an admission check only. Once a child WorkflowRun has been
    persisted, its durable identity controls continuation even if the schedule
    is later disabled.
    """
    from packages.core.constants.execution import ScheduledRunStatus

    origin = await _load_scheduled_automation_origin(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
        execution_type="workflow",
    )
    return bool(
        origin is not None
        and origin[0].enabled
        and origin[1].status == ScheduledRunStatus.RUNNING.value
        and _scheduled_workflow_target_matches(
            origin[0], workflow_id=workflow_id, binding_id=binding_id,
        )
    )


async def scheduled_workflow_origin_is_valid(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
    workflow_id: str,
    binding_id: str | None = None,
) -> bool:
    """Validate immutable scheduled Workflow provenance for continuation."""
    origin = await _load_scheduled_automation_origin(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
        execution_type="workflow",
    )
    return bool(
        origin is not None
        and _scheduled_workflow_target_matches(
            origin[0], workflow_id=workflow_id, binding_id=binding_id,
        )
    )


def _scheduled_workflow_target_matches(
    job: Any,
    *,
    workflow_id: str,
    binding_id: str | None,
) -> bool:
    target = job.execution_target if isinstance(job.execution_target, dict) else {}
    configured_workflow_id = str(target.get("workflow_id") or job.goal_id or "")
    if configured_workflow_id != str(workflow_id):
        return False
    return not binding_id or str(target.get("binding_id") or "") == str(binding_id)


async def _scheduled_automation_origin_matches(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
    execution_type: str | None = None,
) -> bool:
    from packages.core.constants.execution import ScheduledRunStatus

    origin = await _load_scheduled_automation_origin(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
        execution_type=execution_type,
    )
    return bool(
        origin is not None
        and origin[1].status == ScheduledRunStatus.RUNNING.value
    )


async def _load_scheduled_automation_origin(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
    execution_type: str | None = None,
) -> tuple[Any, Any] | None:
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    job_key = str(scheduled_job_id or "").strip()
    run_key = str(scheduled_run_id or "").strip()
    if not job_key or not run_key:
        return None
    conditions = [
        ScheduledJob.job_id == job_key,
        ScheduledJob.entity_id == str(entity_id),
        ScheduledJob.workspace_id == str(workspace_id),
    ]
    if execution_type:
        conditions.append(ScheduledJob.execution_type == execution_type)
    job = (
        await db.execute(select(ScheduledJob).where(*conditions))
    ).scalar_one_or_none()
    if job is None:
        return None
    scheduled_run = await db.get(ScheduledJobRun, run_key)
    if scheduled_run is None or scheduled_run.job_id != job.job_id:
        return None
    return job, scheduled_run


async def _scheduled_task_origin_matches(
    db: AsyncSession,
    *,
    task,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str,
    scheduled_run_id: str,
) -> bool:
    from packages.core.constants.execution import (
        ScheduledDispatchKind,
        ScheduledRunStatus,
    )

    origin = await _load_scheduled_automation_origin(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
    )
    if origin is None:
        return False
    _job, scheduled_run = origin
    result = scheduled_run.result if isinstance(scheduled_run.result, dict) else {}
    return bool(
        scheduled_run.status == ScheduledRunStatus.RUNNING.value
        and result.get("scheduled_child_kind")
        == ScheduledDispatchKind.AGENT_TASK.value
        and str(result.get("scheduled_child_id") or "") == str(task.id)
    )


async def _scheduled_strategist_task_origin_matches(
    db: AsyncSession,
    *,
    task,
    entity_id: str,
    workspace_id: str,
    scheduled_job_id: str | None,
    scheduled_run_id: str | None,
) -> bool:
    from packages.core.constants.execution import ScheduledRunStatus

    review_id = str((task.details or {}).get("strategist_review_id") or "").strip()
    if (
        not review_id
        or task.creator_id is not None
        or task.task_type != "ai_generated"
    ):
        return False
    origin = await _load_scheduled_automation_origin(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        scheduled_run_id=scheduled_run_id,
        execution_type="strategist_review",
    )
    if origin is None:
        return False
    _job, scheduled_run = origin
    if scheduled_run.status not in {
        ScheduledRunStatus.SUCCESS.value,
        ScheduledRunStatus.COMPLETED.value,
    }:
        return False
    result = scheduled_run.result if isinstance(scheduled_run.result, dict) else {}
    return str(result.get("review_id") or "") == review_id


async def list_revisions(
    db: AsyncSession, workspace_id: str, *, limit: int = 50,
) -> list[GovernanceRevision]:
    """Audit log — newest first."""
    return list((await db.execute(
        select(GovernanceRevision)
        .where(GovernanceRevision.workspace_id == workspace_id)
        .order_by(desc(GovernanceRevision.revision))
        .limit(limit)
    )).scalars().all())


# ── Write ─────────────────────────────────────────────────────────────

async def update_policy(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    policy: WorkspacePolicy,
    changed_by: Optional[str] = None,
    change_summary: Optional[str] = None,
    preserve_automated_run_consent: bool = False,
) -> GovernancePolicy:
    """Upsert the policy + write a revision. Caller commits.

    Raises PolicyError indirectly via policy_from_dict if the given
    policy round-trips to something invalid (defensive — callers pass
    dataclasses, but we re-validate to keep the storage layer honest).
    Source-rule callers preserve live user-owned rules while holding the
    Workspace and policy locks so a stale draft cannot overwrite settings.
    """
    # Round-trip to catch unsupported shapes before persistence.
    persisted = policy_to_dict(policy_from_dict(policy_to_dict(policy)))

    from packages.core.governance.approval_policy import ApprovalPolicyBinding, ApprovalPolicyOverride
    from packages.core.models.workspace import Workspace
    # All local writers lock Workspace before its policy row. Unified settings
    # additionally lock Entity first; source writers never need that root lock.
    workspace = (await db.execute(select(Workspace).where(
        Workspace.id == workspace_id, Workspace.entity_id == entity_id,
    ).with_for_update())).scalar_one_or_none()
    if workspace is None:
        raise ValueError("Workspace approval owner does not match")
    row = (await db.execute(
        select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace_id,
            GovernancePolicy.entity_id == entity_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if isinstance(policy.approval, ApprovalPolicyBinding):
        if policy.approval.root_entity_id != entity_id:
            raise ValueError("Workspace approval root does not match")
    elif isinstance(policy.approval, ApprovalPolicyOverride):
        persisted["approval"] = ApprovalPolicyBinding(
            root_entity_id=entity_id, policy=policy.approval,
        ).model_dump(mode="json")
    elif row is not None and isinstance((row.policy or {}).get("approval"), dict):
        persisted["approval"] = row.policy["approval"]
    else:
        persisted["approval"] = ApprovalPolicyBinding(
            root_entity_id=entity_id, policy=ApprovalPolicyOverride(),
        ).model_dump(mode="json")
    max_revision = (await db.execute(
        select(func.max(GovernanceRevision.revision)).where(
            GovernanceRevision.workspace_id == workspace_id
        )
    )).scalar_one_or_none() or 0
    next_revision = max((row.revision if row else 0) or 0, max_revision) + 1

    policy_cache = GovernancePolicyCacheFactory.create_default()
    fence = await policy_cache.begin_mutation(workspace_id)
    if not fence.active:
        raise GovernancePolicyCacheUnavailable(workspace_id)

    if row is None:
        row = GovernancePolicy(
            workspace_id=workspace_id,
            entity_id=entity_id,
            policy=persisted,
            revision=next_revision,
            updated_by=changed_by,
        )
        db.add(row)
    else:
        row.policy = persisted
        row.revision = next_revision
        row.updated_by = changed_by

    _register_policy_cache_mutation(
        db,
        policy_cache=policy_cache,
        fence=fence,
        snapshot=GovernancePolicySnapshot(
            workspace_id=workspace_id,
            revision=next_revision,
            policy=persisted,
        ),
    )

    db.add(GovernanceRevision(
        workspace_id=workspace_id,
        revision=next_revision,
        policy=persisted,
        change_summary=(change_summary or "")[:500] or None,
        changed_by=changed_by,
    ))
    try:
        from packages.core.workspace_chat.context import invalidate
        invalidate(workspace_id)
    except Exception:
        pass
    await db.flush()
    # Pending policy asks in this Workspace are re-decided after commit.
    from packages.core.governance.approval_policy import ApprovalScope
    from packages.core.governance.approval_policy_reevaluation import stage_pending_approval_reevaluation
    await stage_pending_approval_reevaluation(
        db, scope=ApprovalScope(kind="workspace", id=workspace_id, entity_id=entity_id),
        revision=next_revision, actor_id=changed_by,
    )
    return row


async def add_auto_approve_action(db, **kwargs):
    raise ValueError("Retired approval write; use approval settings with expected_version")


async def add_auto_approve_capability(db, **kwargs):
    raise ValueError("Retired approval write; use an exact action rule")


async def remove_auto_approve_action(db, **kwargs):
    raise ValueError("Retired approval write; use approval settings with expected_version")


async def remove_auto_approve_capability(db, **kwargs):
    raise ValueError("Retired approval write; use approval settings with expected_version")


async def check_step_policy(
    db: AsyncSession, *, workspace_id: Optional[str], kind: str,
    action_key: Optional[str], resource_id: Optional[str] = None, risk_level: str,
    capability_id: Optional[str] = None,
    spent_credits_per_kind: Optional[dict[str, int]] = None,
    task_id: Optional[str] = None, resource_ids: tuple[str, ...] = (),
    entity_id: str | None = None, user_id: str | None = None,
    room_id: str | None = None, resource_kind: str | None = None,
    account_id: str | None = None, operation: str | None = None,
    impact: str | None = None, category: str | None = None, gate: str | None = None,
    workflow_run_id: str | None = None, facts=None, aliases: tuple[str, ...] = (),
    restrict_only_keys: tuple[str, ...] = (), conversation_id: str | None = None,
) -> PolicyDecision:
    """Check independent constraints and the same current inherited consent policy.

    ``facts`` may carry the caller's already-built ``ActionFacts``, or
    ``EXEMPT`` for a subject with no effect of its own: then only the hard
    constraints run and consent is not evaluated.
    """
    from packages.core.governance.approval_facts import EXEMPT, action_facts
    from packages.core.governance.approval_policy import evaluate_policy
    from packages.core.governance.approval_policy_service import (
        ApprovalPolicyIntegrityError, load_policy_chain,
    )
    if workflow_run_id:
        if not await workflow_run_is_executable(db, entity_id=entity_id,
                workspace_id=workspace_id, user_id=user_id, workflow_run_id=workflow_run_id):
            return PolicyDecision(False, reason="Workflow execution is no longer valid",
                matched_rule="workflow.origin_not_executable")
    if workspace_id:
        from packages.core.models.workspace import Workspace
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or (entity_id and str(workspace.entity_id) != str(entity_id)):
            return PolicyDecision(False, reason="Workspace approval owner does not match", matched_rule="approval.owner")
        entity_id = str(workspace.entity_id)
        policy = await get_policy(db, workspace_id)
        constraints = decide(policy, kind=kind, action_key=action_key,
            resource_id=resource_id, risk_level=risk_level, capability_id=capability_id,
            spent_credits_per_kind=spent_credits_per_kind)
        if not constraints.allowed:
            return constraints
    if facts is EXEMPT:
        return PolicyDecision(True, reason="approval.no_effect", matched_rule="approval.no_effect",
            explanation={"outcome": "allow", "reason_code": "approval.no_effect", "source": "constraint",
                         "reason_params": {"kind": kind}})
    if not entity_id:
        return PolicyDecision(False, reason="Approval scope is missing", matched_rule="approval.scope")
    try:
        chain = await load_policy_chain(db, entity_id=entity_id,
            workspace_id=workspace_id, user_id=user_id, room_id=room_id)
        facts = facts or action_facts(
            action_key=action_key, capability_id=capability_id, resource_kind=resource_kind,
            resource_id=resource_id, resource_ids=resource_ids, account_id=account_id,
            operation=operation, risk_level=risk_level, impact=impact, category=category,
            gate=gate, aliases=aliases, restrict_only_keys=restrict_only_keys,
        )
        evaluation = evaluate_policy(chain, facts)
    except ApprovalPolicyIntegrityError as exc:
        return PolicyDecision(False, reason=str(exc), matched_rule="approval.configuration")
    decision = PolicyDecision(
        allowed=evaluation.outcome == "allow", pause_for_hitl=evaluation.outcome == "ask",
        reason=evaluation.reason_code, matched_rule=evaluation.rule_id or evaluation.reason_code,
        explanation=evaluation.model_dump(mode="json"), facts=facts,
    )
    if evaluation.outcome == "deny":
        return decision
    task_decision = await _check_task_runtime_rules(db, task_id=task_id,
        action_key=action_key, capability_id=capability_id, risk_level=risk_level, facts=facts)
    if task_decision is None:
        return await _apply_conversation_holds(db, decision, conversation_id=conversation_id, facts=facts)
    if not task_decision.pause_for_hitl:
        if task_decision.allowed:
            return await _apply_conversation_holds(
                db, replace(task_decision, facts=facts), conversation_id=conversation_id, facts=facts,
            )
        return replace(task_decision, facts=facts)
    # A task restriction adds a requirement; it must not hide an account/local
    # requirement when checking whether an earlier exact grant still covers it.
    policy_targets = decision.explanation.get("target_decisions") or [decision.explanation]
    task_targets = task_decision.explanation.get("target_decisions") or [task_decision.explanation]
    return replace(task_decision, facts=facts, explanation={**task_decision.explanation,
        "target_decisions": [*policy_targets, *task_targets]})


async def _apply_conversation_holds(
    db: AsyncSession, decision: PolicyDecision, *, conversation_id: str | None, facts,
) -> PolicyDecision:
    """Turn an allow into an ask when the user held this kind of action in this conversation.

    Holds only tighten: an ask or a deny is returned unchanged.
    """
    if not decision.allowed or not conversation_id or facts is None:
        return decision
    from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
    from packages.core.governance.approval_policy_service import build_policy_chain
    from packages.core.governance.conversation_holds import conversation_hold_rules

    rules = await conversation_hold_rules(db, conversation_id)
    if not rules:
        return decision
    scope = ApprovalScope(kind="account", id=conversation_id, entity_id=conversation_id)
    chain = build_policy_chain(scope=scope, root={"revision": 1, "policy": {
        "mode": "automatic", "rules": [rule.model_dump() for rule in rules],
    }})
    result = evaluate_policy(chain, facts)
    if result.outcome != "ask":
        return decision
    held = [{**item.model_dump(mode="json"), "source": "constraint",
             "scope": {"kind": "conversation", "id": conversation_id},
             "reason_code": "approval.conversation_hold"}
            for item in result.target_decisions if item.source == "rule"]
    return PolicyDecision(
        False, True, "approval.conversation_hold", held[0]["rule_id"],
        {**held[0], "outcome": "ask", "target_decisions": held}, facts=facts,
    )


async def workspace_policy_auto_approves(
    db: AsyncSession, *, workspace_id: Optional[str], action_key: Optional[str] = None,
    resource_id: Optional[str] = None, capability_id: Optional[str] = None,
) -> bool:
    if not workspace_id:
        return False
    decision = await check_step_policy(db, workspace_id=workspace_id, kind="action",
        action_key=action_key, resource_id=resource_id, capability_id=capability_id,
        risk_level="medium")
    return decision.allowed


async def _check_task_runtime_rules(
    db: AsyncSession, *, task_id: Optional[str], action_key: Optional[str],
    risk_level: str, capability_id: Optional[str] = None, facts=None,
) -> PolicyDecision | None:
    """Task source rules add restrictions through the same typed evaluator."""
    if not task_id:
        return None
    from packages.core.models.task import Task
    from packages.core.governance.approval_facts import action_facts
    from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
    from packages.core.governance.approval_policy_service import build_policy_chain
    from packages.core.governance.approval_projection import compile_workspace_rules

    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        return None
    runtime = ((task.details or {}).get("runtime_context") or {})
    sources = runtime.get("rules") or []
    if not isinstance(sources, list):
        return PolicyDecision(False, reason="Task approval constraints are malformed", matched_rule="task.approval.configuration")
    try:
        rules = compile_workspace_rules([rule for rule in sources if isinstance(rule, dict) and rule.get("enabled", True) is not False])
    except ValueError:
        return PolicyDecision(False, reason="Task approval constraints need migration to typed selectors", matched_rule="task.approval.configuration")
    facts = facts or action_facts(action_key=action_key, capability_id=capability_id, risk_level=risk_level)
    asks = []
    for rule in rules:
        if rule.effect == "allow":
            continue  # A Task cannot loosen its account or owner policy.
        scope = ApprovalScope(kind="account", id=task_id, entity_id=str(getattr(task, "entity_id", None) or task_id))
        chain = build_policy_chain(scope=scope, root={"revision": 1,
            "policy": {"mode": "automatic", "rules": [rule.model_dump()]}})
        result = evaluate_policy(chain, facts)
        matches = [{**item.model_dump(mode="json"), "source": "constraint",
            "scope": {"kind": "task", "id": task_id}, "rule_id": rule.source_id,
            "reason_code": "task.rule"} for item in result.target_decisions if item.source == "rule"]
        if result.outcome == "deny":
            return PolicyDecision(False, reason=f"Task rule {rule.source_id!r} blocks this action",
                matched_rule=rule.source_id, explanation={"outcome": "deny", "target_decisions": matches})
        asks.extend(item for item in matches if item["outcome"] == "ask")
    if asks:
        return PolicyDecision(False, True, "Task rules require review", asks[0]["rule_id"],
            {**asks[0], "target_decisions": asks})
    return None


async def post_hitl_card(
    *,
    entity_id: str,
    workspace_id: str,
    plan_id: str,
    step_id: str,
    step_key: str,
    kind: str,
    action_key: Optional[str],
    matched_rule: Optional[str],
    reason: Optional[str] = None,
    capability_id: Optional[str] = None,
    approval_request_id: Optional[str] = None,
    hitl_type: str = HitlType.AUTHORIZE.value,
    payload: Optional[dict] = None,
    task_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> None:
    """Best-effort structured chat card for a paused step.

    Posted by the Dispatcher when the approval gate answers needs_human. The
    pending_action is the durable resolver; the body is just the
    human-readable prompt.

    ``approval_request_id`` links the card to the unified ``HitlRequest``
    so the resolver grants THAT request (which the dispatcher consumes when
    the lease goes out). One card per open request: if an unresolved card for
    the same request already exists, no duplicate is posted.

    ``hitl_type``/``payload`` are the request row's own values, carried onto
    the card so the client can render by type instead of guessing from a
    prose prompt. They are the reason this function exists in its current
    shape: the record layer has known since ``hitl_type`` shipped that a
    re-gated failed step is an ``error``, not a fresh "may I?", but nothing
    put that on the card — so the operator kept seeing "this step needs your
    approval" while the real failure (their local worker was offline) stayed
    invisible. An ``error`` card therefore renders the failure and offers
    retry/cancel, never approve/always/reject.

    ``task_id`` is what the card deep-links to. The step and plan ids alone
    are not addressable in the web app; ``/tasks/{task_id}`` is.

    Pass ``db`` to post the card INSIDE the caller's transaction (caller
    commits): the card, the HitlRequest, and the step's waiting_human
    transition then land atomically. Without it (own session + commit), a
    caller crash after the post strands a card whose request id was never
    committed — an unresolvable orphan.
    """
    try:
        from packages.core.database import async_session
        from packages.core.workspace_chat import service as chat_service

        if approval_request_id:
            from packages.core.models.task import Message

            dedup_stmt = select(Message.id).where(
                Message.pending_action["approval_request_id"].as_string()
                == approval_request_id,
                Message.resolved_at.is_(None),
            ).limit(1)
            if db is not None:
                existing = (await db.execute(dedup_stmt)).scalar_one_or_none()
            else:
                async with async_session() as check_db:
                    existing = (await check_db.execute(dedup_stmt)).scalar_one_or_none()
            if existing is not None:
                return

        card_payload = dict(payload or {})
        if hitl_type == HitlType.ERROR.value:
            # The step already ran and failed. Say what broke and what to do
            # about it — never "approval needed", which is what buried the
            # real cause during the incident.
            what_happened = (
                str(card_payload.get("what_happened") or "").strip()
                or "The step failed."
            )
            action_to_take = str(card_payload.get("action_to_take") or "").strip()
            body = f"⚠️ **Action needed** — {what_happened}"
            if action_to_take:
                body = f"{body} {action_to_take}"
            prompt = what_happened
            # Retry / cancel, not approve / always / reject.
            options = decision_options_for_hitl(hitl_type)
        elif hitl_type == HitlType.REVIEW.value:
            body = f"📄 **Review needed** — step `{step_key}` is waiting for your feedback."
            prompt = reason or f"Review the material produced by step `{step_key}`."
            options = decision_options_for_hitl(hitl_type)
        else:
            body = (
                f"⛔ **Approval needed** — step `{step_key}` "
                f"({kind}/{action_key or capability_id or '—'}) was paused by your governance "
                f"policy (rule: `{matched_rule}`)."
            )
            prompt = reason or (
                f"Approve this step once? {kind}/{action_key or capability_id or 'unknown action'} "
                f"matched governance rule {matched_rule or 'unknown'}."
            )
            # Authorize keeps the standing-grant option. Review is handled in
            # the branch above and can never grant a future diff.
            options = decision_options_for_hitl(hitl_type)
        pending_action = {
            "kind": PendingActionKind.GOVERNANCE_APPROVAL.value,
            "step_id": step_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "step_key": step_key,
            "prompt": prompt,
            "action": action_key,
            "capability_id": capability_id,
            "tool": kind,
            "matched_rule": matched_rule,
            "approval_request_id": approval_request_id,
            # What kind of human involvement this is, and the copy that
            # answers what / why / what-to-do. Straight off the
            # HitlRequest row — the card must not re-derive them, or the
            # row and the screen can disagree again.
            "hitl_type": hitl_type,
            "payload": card_payload,
            "options": options,
        }
        if hitl_type == HitlType.REVIEW.value:
            # WorkflowApprovalReview deliberately consumes a top-level
            # ``review`` value. Keep the canonical payload intact for the
            # resolver/audit trail, while projecting the material under
            # review into the shared card shape used by task and workflow
            # reviews. Without this adapter a typed review request falls
            # through to the generic approval summary and the operator
            # cannot inspect the file/diff they are deciding on.
            pending_action["review"] = card_payload.get("diff", card_payload)
            pending_action["review_title"] = str(
                card_payload.get("title")
                or card_payload.get("review_title")
                or f"Review {step_key}"
            )
        if db is not None:
            await chat_service.post_message(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                body=body,
                message_kind="hitl_request",
                author_kind="system",
                thread_ref_kind="plan",
                thread_ref_id=plan_id,
                refs=[
                    {"type": "plan", "id": plan_id},
                    {"type": "step", "id": step_id},
                ],
                pending_action=pending_action,
            )
            await db.flush()
        else:
            async with async_session() as own_db:
                await chat_service.post_message(
                    own_db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    body=body,
                    message_kind="hitl_request",
                    author_kind="system",
                    thread_ref_kind="plan",
                    thread_ref_id=plan_id,
                    refs=[
                        {"type": "plan", "id": plan_id},
                        {"type": "step", "id": step_id},
                    ],
                    pending_action=pending_action,
                )
                await own_db.commit()
    except Exception:
        logger.warning("HITL card post failed", exc_info=True)


async def resolve_stale_hitl_cards(
    db: AsyncSession,
    *,
    plan_id: Optional[str] = None,
    step_ids: Optional[list[str]] = None,
    task_id: Optional[str] = None,
    reason: str = "origin_terminal",
    choice: str = "expired",
    pending_action_kinds: Optional[list[str]] = None,
    for_update: bool = False,
) -> int:
    """Mark matching unresolved HITL chat cards resolved when their origin is gone.

    The companion of ``approvals.resolve_origin_requests``: when a plan/step
    reaches a terminal state, the HitlRequest is expired there and the
    card that rendered it is closed here — otherwise the card lingers
    unresolved forever ("no longer attached to a waiting step") and inflates
    the sidebar pending-action badge. Returns how many cards were closed.
    """
    if not plan_id and not step_ids and not task_id:
        return 0
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from packages.core.models.task import Message

    action_kinds = pending_action_kinds or [
        PendingActionKind.GOVERNANCE_APPROVAL.value,
    ]
    conds = []
    if plan_id:
        conds.append(Message.pending_action["plan_id"].as_string() == plan_id)
    if step_ids:
        conds.append(Message.pending_action["step_id"].as_string().in_(list(step_ids)))
    if task_id:
        conds.append(Message.pending_action["task_id"].as_string() == task_id)
    query = select(Message).where(
        Message.pending_action.isnot(None),
        Message.pending_action["kind"].as_string().in_(action_kinds),
        Message.resolved_at.is_(None),
        or_(*conds),
    )
    if for_update:
        query = query.with_for_update()
    rows = (await db.execute(query)).scalars().all()
    now = datetime.now(timezone.utc)
    for msg in rows:
        msg.resolved_at = now
        msg.resolution = {"choice": choice, "reason": reason}
    if rows:
        logger.info(
            "governance: resolved %d stale HITL card(s) (%s)", len(rows), reason,
        )
    return len(rows)
