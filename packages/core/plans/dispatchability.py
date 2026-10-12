"""Planning-time answer to "can the dispatcher actually lease this plan?".

The dispatcher matches a PENDING step to a worker with three lookups: the
active ``AgentSubscription`` for ``(entity, workspace, service_key)``, an
ACTIVE ``Worker`` bound to it through ``subscription_workers`` whose
``capabilities.supported_kinds`` carry the step kind, and — for action
steps — the agent's MCP binding for ``provider.action_key``.  A plan that
fails any of them used to be accepted and persisted, then skipped on every
dispatcher pass until the undispatchable watchdog parked it on a human.

``plan_dispatch_preflight`` runs the same three lookups while the plan is
still a candidate, so the planner rejects it as a ``CapabilityError`` before
a credit is spent.  It bootstraps the entity's built-in worker first (the
same idempotent call the internal worker makes before its first checkout),
which is why a fresh workspace passes: every active subscription is bound to
that worker and it advertises every leaseable kind.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import WorkerStatus
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import AgentSubscription
from packages.core.plans.schema import Plan
from packages.core.services.agent_action_binding import agent_action_binding_allowed
from packages.core.workers.registry import ensure_internal_worker

logger = logging.getLogger(__name__)

#: Step kinds a Worker executes through a lease.  The others (sleep / human /
#: parallel_fanout / gather) are executor-owned and never need a worker.
PREFLIGHT_STEP_KINDS = frozenset({"llm", "subagent", "action"})


async def plan_dispatch_preflight(
    db: AsyncSession,
    *,
    plan: Plan,
    task: Any,
) -> list[str]:
    """Return one plain-sentence error per step the dispatcher could not lease.

    Empty list means every ``llm`` / ``subagent`` / ``action`` step resolves
    to an active subscription, an active bound worker that supports its
    kind, and (for actions) an agent binding that allows the action.  Tasks
    outside a workspace have no subscriptions to route through and are not
    checked.
    """
    workspace_id = getattr(task, "workspace_id", None)
    if not workspace_id:
        return []
    workspace_id = str(workspace_id)
    entity_id = str(getattr(task, "entity_id", "") or "")

    await ensure_internal_worker(db, entity_id)

    errors: list[str] = []
    subscriptions: dict[str, AgentSubscription | None] = {}
    bound_workers: dict[str, list[Worker]] = {}
    for step in plan.steps:
        if step.kind not in PREFLIGHT_STEP_KINDS or not step.service_key:
            continue
        service_key = str(step.service_key)
        if service_key not in subscriptions:
            subscriptions[service_key] = await _active_subscription(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                service_key=service_key,
            )
        subscription = subscriptions[service_key]
        if subscription is None:
            errors.append(
                f"step {step.key}: service '{service_key}' has no active "
                "subscription in this workspace"
            )
            continue

        if subscription.id not in bound_workers:
            bound_workers[subscription.id] = await _active_bound_workers(db, subscription.id)
        if not any(
            _worker_supports_kind(worker, step.kind)
            for worker in bound_workers[subscription.id]
        ):
            errors.append(
                f"step {step.key}: no active worker is bound to service "
                f"'{service_key}' for kind '{step.kind}'"
            )
            continue

        if step.kind == "action":
            allowed, error = await agent_action_binding_allowed(
                db,
                agent_id=subscription.agent_id,
                provider=step.provider,
                action_key=step.action_key,
            )
            if not allowed:
                message = str((error or {}).get("message") or "action binding denied")
                errors.append(f"step {step.key}: {message}")

    if errors:
        logger.info(
            "Planner dispatch preflight rejected task %s: %s",
            getattr(task, "id", None),
            "; ".join(errors),
        )
    return errors


async def _active_subscription(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    service_key: str,
) -> AgentSubscription | None:
    """The dispatcher's subscription lookup, scoped to the task's entity.

    Ambiguous (duplicate) active subscriptions are rejected earlier by the
    planner's context gathering, so ``limit(1)`` cannot hide a second row.
    """
    return (await db.execute(
        select(AgentSubscription)
        .where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == service_key,
            AgentSubscription.status == "active",
        )
        .limit(1)
    )).scalar_one_or_none()


async def _active_bound_workers(db: AsyncSession, subscription_id: str) -> list[Worker]:
    """Every ACTIVE worker bound to the subscription — the dispatcher's
    ``bound_subs`` subquery seen from the subscription side."""
    return list((await db.execute(
        select(Worker)
        .join(SubscriptionWorker, SubscriptionWorker.worker_id == Worker.id)
        .where(
            SubscriptionWorker.subscription_id == subscription_id,
            Worker.status == WorkerStatus.ACTIVE.value,
        )
    )).scalars().all())


def _worker_supports_kind(worker: Worker, kind: str) -> bool:
    capabilities = worker.capabilities if isinstance(worker.capabilities, dict) else {}
    supported = capabilities.get("supported_kinds") or []
    return kind in {str(value) for value in supported}
