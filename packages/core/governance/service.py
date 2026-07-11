"""Governance read / write helpers + the Dispatcher hook.

Every mutation goes through ``update_policy`` so every change writes
both the current row and an audit revision atomically. The Dispatcher
calls ``check_step_policy`` per step at lease checkout; on a HITL
decision it pauses the step and posts a chat card via
``packages.core.workspace_chat``.
"""
from __future__ import annotations

import logging
import fnmatch
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.governance.policy import (
    DEFAULT_POLICY,
    PolicyDecision,
    WorkspacePolicy,
    decide,
    policy_auto_approves,
    policy_from_dict,
    policy_to_dict,
)
from packages.core.services.hitl_options import approval_options
from packages.core.models.governance import (
    GovernancePolicy,
    GovernanceRevision,
)

logger = logging.getLogger(__name__)


# ── Read ──────────────────────────────────────────────────────────────

async def get_policy(
    db: AsyncSession, workspace_id: str,
) -> WorkspacePolicy:
    """Return the current policy for a workspace, falling back to
    DEFAULT_POLICY if the operator never customised one."""
    row = (await db.execute(
        select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace_id
        )
    )).scalar_one_or_none()
    if row is None:
        return DEFAULT_POLICY
    return policy_from_dict(row.policy)


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
) -> GovernancePolicy:
    """Upsert the policy + write a revision. Caller commits.

    Raises PolicyError indirectly via policy_from_dict if the given
    policy round-trips to something invalid (defensive — callers pass
    dataclasses, but we re-validate to keep the storage layer honest).
    """
    # Round-trip to catch unsupported shapes before persistence.
    persisted = policy_to_dict(policy_from_dict(policy_to_dict(policy)))

    row = (await db.execute(
        select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace_id
        ).with_for_update()
    )).scalar_one_or_none()
    max_revision = (await db.execute(
        select(func.max(GovernanceRevision.revision)).where(
            GovernanceRevision.workspace_id == workspace_id
        )
    )).scalar_one_or_none() or 0
    next_revision = max((row.revision if row else 0) or 0, max_revision) + 1

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
    return row


async def add_auto_approve_action(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    action_key: str,
    changed_by: Optional[str] = None,
) -> bool:
    """Add ``action_key`` to a workspace's ``auto_approve_actions`` (idempotent).

    Backs the "always allow" approval choice: once the operator picks it, the
    same action_key stops triggering HITL on future steps. Returns True if the
    action was newly added (a policy revision was written), False if it was
    already auto-approved or inputs were missing. Caller commits.
    """
    if not workspace_id or not action_key:
        return False
    from dataclasses import replace

    policy = await get_policy(db, workspace_id)
    if action_key in policy.auto_approve_actions:
        return False
    new_policy = replace(
        policy,
        auto_approve_actions=[*policy.auto_approve_actions, action_key],
    )
    await update_policy(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=new_policy,
        changed_by=changed_by,
        change_summary=f"always-approve action: {action_key}",
    )
    return True


async def add_auto_approve_capability(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    capability_id: str,
    changed_by: Optional[str] = None,
) -> bool:
    """Add ``capability_id`` to workspace ``auto_approve_capabilities``.

    This backs "always allow" for governance approvals that are capability
    scoped rather than action scoped, such as a subagent step with
    ``capability_id=file.write`` and no concrete provider ``action_key``.
    """
    if not workspace_id or not capability_id:
        return False
    from dataclasses import replace

    policy = await get_policy(db, workspace_id)
    if capability_id in policy.auto_approve_capabilities:
        return False
    new_policy = replace(
        policy,
        auto_approve_capabilities=[*policy.auto_approve_capabilities, capability_id],
    )
    await update_policy(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=new_policy,
        changed_by=changed_by,
        change_summary=f"always-approve capability: {capability_id}",
    )
    return True


# ── Dispatcher hook ───────────────────────────────────────────────────

async def check_step_policy(
    db: AsyncSession,
    *,
    workspace_id: Optional[str],
    kind: str,
    action_key: Optional[str],
    risk_level: str,
    capability_id: Optional[str] = None,
    spent_credits_per_kind: Optional[dict[str, int]] = None,
    task_id: Optional[str] = None,
) -> PolicyDecision:
    """Evaluate a step against the workspace's current policy.

    Workspace-less steps (``workspace_id is None``) always pass — they
    only happen for entity-level system tasks the operator can't
    realistically govern at the workspace tier.
    """
    if not workspace_id:
        return PolicyDecision(allowed=True)
    policy = await get_policy(db, workspace_id)
    decision = decide(
        policy,
        kind=kind,
        action_key=action_key,
        risk_level=risk_level,
        capability_id=capability_id,
        spent_credits_per_kind=spent_credits_per_kind,
    )
    if not decision.allowed:
        return decision
    task_decision = await _check_task_runtime_rules(
        db,
        task_id=task_id,
        action_key=action_key,
        capability_id=capability_id,
        risk_level=risk_level,
    )
    return task_decision or decision


async def workspace_policy_auto_approves(
    db: AsyncSession,
    *,
    workspace_id: Optional[str],
    action_key: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> bool:
    """True when the workspace's policy *explicitly* auto-approves this action
    or capability. Used by the dispatcher so a workspace can override a
    capability's intrinsic ``required_approval``. Workspace-less steps return
    False (nothing to opt into)."""
    if not workspace_id:
        return False
    policy = await get_policy(db, workspace_id)
    return policy_auto_approves(
        policy, action_key=action_key, capability_id=capability_id
    )


async def _check_task_runtime_rules(
    db: AsyncSession,
    *,
    task_id: Optional[str],
    action_key: Optional[str],
    risk_level: str,
    capability_id: Optional[str] = None,
) -> PolicyDecision | None:
    """Task-level runtime requirements may add restrictions, never loosen them."""
    if not task_id or (not action_key and not capability_id):
        return None
    from packages.core.models.task import Task

    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        return None
    runtime = ((task.details or {}).get("runtime_context") or {})
    rules = runtime.get("rules") or []
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is False:
            continue
        action_patterns = rule.get("action_patterns") or rule.get("actions") or []
        capability_patterns = rule.get("capability_patterns") or rule.get("capabilities") or []
        if isinstance(action_patterns, str):
            action_patterns = [action_patterns]
        if isinstance(capability_patterns, str):
            capability_patterns = [capability_patterns]
        matched_action = bool(action_key) and any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(action_key or "", pattern)
            for pattern in action_patterns
        )
        matched_capability = bool(capability_id) and any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(capability_id or "", pattern)
            for pattern in capability_patterns
        )
        if not matched_action and not matched_capability:
            continue
        rule_type = str(rule.get("rule_type") or "").strip().lower()
        key = str(rule.get("rule_key") or rule_type or "task_runtime_rule")
        desc = str(rule.get("description") or key)
        subject = action_key or capability_id or "runtime action"
        if rule_type in {"deny", "never_allow", "block", "draft_only"}:
            return PolicyDecision(
                allowed=False,
                reason=f"task runtime rule blocks {subject!r}: {desc}",
                matched_rule=key,
            )
        if rule_type in {"approval_required", "hitl_required", "require_approval"}:
            return PolicyDecision(
                allowed=False,
                pause_for_hitl=True,
                reason=f"task runtime rule requires approval for {subject!r}: {desc}",
                matched_rule=key,
            )
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
) -> None:
    """Best-effort structured chat card prompting approve/reject.

    Posted by the Dispatcher when policy requires HITL. The pending_action is
    the durable resolver; the body is just the human-readable prompt.
    """
    try:
        from packages.core.database import async_session
        from packages.core.workspace_chat import service as chat_service

        body = (
            f"⛔ **Approval needed** — step `{step_key}` "
            f"({kind}/{action_key or capability_id or '—'}) was paused by your governance "
            f"policy (rule: `{matched_rule}`)."
        )
        prompt = reason or (
            f"Approve this step once? {kind}/{action_key or capability_id or 'unknown action'} "
            f"matched governance rule {matched_rule or 'unknown'}."
        )
        pending_action = {
            "kind": "governance_approval",
            "step_id": step_id,
            "plan_id": plan_id,
            "step_key": step_key,
            "prompt": prompt,
            "action": action_key,
            "capability_id": capability_id,
            "tool": kind,
            "matched_rule": matched_rule,
            # "always_approve" lets the operator persist this approval at the
            # workspace layer. Prefer a concrete action_key when available;
            # otherwise persist the capability_id (for subagent/file.write
            # style steps that do not have a provider action).
            "options": approval_options(),
        }
        async with async_session() as db:
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
            await db.commit()
    except Exception:
        logger.warning("HITL card post failed", exc_info=True)
