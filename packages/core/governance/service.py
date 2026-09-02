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
import fnmatch
from typing import Optional

from sqlalchemy import desc, event, func, select
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
from packages.core.governance.approval_scope import approval_scope_key
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
    """Return the current policy for a workspace, falling back to
    DEFAULT_POLICY if the operator never customised one."""
    policy_cache = GovernancePolicyCacheFactory.create_default()
    cached = await policy_cache.get_current(workspace_id)
    if cached is not None:
        return policy_from_dict(cached.policy)

    row = (await db.execute(
        select(GovernancePolicy.policy, GovernancePolicy.revision).where(
            GovernancePolicy.workspace_id == workspace_id
        )
    )).one_or_none()
    if row is None:
        snapshot = GovernancePolicySnapshot(
            workspace_id=workspace_id,
            revision=0,
            policy=policy_to_dict(DEFAULT_POLICY),
        )
    else:
        snapshot = GovernancePolicySnapshot(
            workspace_id=workspace_id,
            revision=int(row.revision or 0),
            policy=dict(row.policy or {}),
        )
    await policy_cache.store_if_current(snapshot)
    return policy_from_dict(snapshot.policy)


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
    return row


async def add_auto_approve_action(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    action_key: str,
    resource_id: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> bool:
    """Add an action scope to a workspace's ``auto_approve_actions``.

    Backs the "always allow" approval choice: once the operator picks it, the
    same action scope stops triggering HITL on future steps. ``resource_id``
    narrows the grant when the caller wants a single concrete object. Returns
    True if the scope was newly added (a policy revision was written), False
    if it was already auto-approved or inputs were missing. Caller commits.
    """
    scope_key = approval_scope_key(action_key, resource_id)
    if not workspace_id or not scope_key:
        return False
    from dataclasses import replace

    policy = await get_policy(db, workspace_id)
    if scope_key in policy.auto_approve_actions:
        return False
    new_policy = replace(
        policy,
        auto_approve_actions=[*policy.auto_approve_actions, scope_key],
    )
    await update_policy(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=new_policy,
        changed_by=changed_by,
        change_summary=f"always-approve action: {scope_key}",
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


async def remove_auto_approve_action(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    action_key: str,
    resource_id: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> bool:
    """Remove ``action_key`` from a workspace's ``auto_approve_actions``.

    Backs the standing-grant revoke surface (Settings → Approval
    automation). Returns True if the action was present and removed (a
    policy revision was written), False if it was not granted or inputs
    were missing. Caller commits.
    """
    scope_key = approval_scope_key(action_key, resource_id)
    if not workspace_id or not scope_key:
        return False
    from dataclasses import replace

    policy = await get_policy(db, workspace_id)
    if scope_key not in policy.auto_approve_actions:
        return False
    new_policy = replace(
        policy,
        auto_approve_actions=[
            key for key in policy.auto_approve_actions if key != scope_key
        ],
    )
    await update_policy(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=new_policy,
        changed_by=changed_by,
        change_summary=f"revoke always-approve action: {scope_key}",
    )
    return True


async def remove_auto_approve_capability(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    capability_id: str,
    changed_by: Optional[str] = None,
) -> bool:
    """Remove ``capability_id`` from workspace ``auto_approve_capabilities``.

    Mirror of ``remove_auto_approve_action`` for capability-scoped
    standing grants. Returns True if removed, False otherwise. Caller
    commits.
    """
    if not workspace_id or not capability_id:
        return False
    from dataclasses import replace

    policy = await get_policy(db, workspace_id)
    if capability_id not in policy.auto_approve_capabilities:
        return False
    new_policy = replace(
        policy,
        auto_approve_capabilities=[
            cid for cid in policy.auto_approve_capabilities if cid != capability_id
        ],
    )
    await update_policy(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=new_policy,
        changed_by=changed_by,
        change_summary=f"revoke always-approve capability: {capability_id}",
    )
    return True


# ── Dispatcher hook ───────────────────────────────────────────────────

async def check_step_policy(
    db: AsyncSession,
    *,
    workspace_id: Optional[str],
    kind: str,
    action_key: Optional[str],
    resource_id: Optional[str] = None,
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
        resource_id=resource_id,
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
    resource_id: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> bool:
    """True when the workspace's policy *explicitly* auto-approves this scope
    or capability. Used by the dispatcher so a workspace can override a
    capability's intrinsic ``required_approval``. Workspace-less steps return
    False (nothing to opt into)."""
    if not workspace_id:
        return False
    policy = await get_policy(db, workspace_id)
    return policy_auto_approves(
        policy,
        action_key=action_key,
        resource_id=resource_id,
        capability_id=capability_id,
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
