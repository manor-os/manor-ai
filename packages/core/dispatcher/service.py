"""Dispatcher core — atomic step→worker matchmaker + lease lifecycle.

Concurrency model: one ``checkout_steps_for_worker`` call per worker
heartbeat. ``SELECT … FOR UPDATE SKIP LOCKED`` plus the partial unique
index ``uq_one_active_lease_per_step`` together guarantee that two
worker heartbeats arriving at the same instant never see the same
runnable step.

Capability matching:
  * worker.capabilities.supported_kinds       must include step.kind
  * worker.capabilities.supported_capabilities
                                               optional Runtime capability
                                               allowlist for step.capability_id
  * worker.capabilities.supported_providers   either null (all) or
                                               must include step.provider
  * worker.capabilities.max_risk_level        must be >= step.risk_level
  * step.service_key                           must map to a subscription
                                               that's bound to this
                                               worker via subscription_workers
  * agent_mcp_bindings.allowed_tools           must include step.action_key
                                               (deferred — checked in M3.5
                                               when integrated with PlanExecutor)

Step state transitions managed here:
  pending          → running         (checkout)
  running          → done            (complete_lease)
  running          → failed          (fail_lease, attempts exhausted)
  running          → pending         (fail_lease, retry remaining;
                                       fail_lease puts step back in queue)
  running          → waiting_human   (lease_needs_human)
  running          → pending         (expire_leases, retry remaining)
  running          → failed          (expire_leases, attempts exhausted)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    runtime_capability_id_for_action_key,
    runtime_planner_action_binding_for,
    runtime_planner_action_specs_from_tools_cached,
)
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.validation import (
    SchemaError,
    output_schema_is_advisory,
    validate_step_input,
    validate_step_output,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import (
    SubscriptionWorker,
    Worker,
    WorkerActivityLog,
    WorkLease,
)
from packages.core.plans.refs import ReferenceError as PlanReferenceError
from packages.core.plans.refs import resolve_refs

logger = logging.getLogger(__name__)


_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_DEFAULT_LEASE_TTL = timedelta(seconds=300)


class DispatchError(Exception):
    """Generic dispatcher failure."""


class NoMatchingSteps(DispatchError):
    """No runnable steps matched the worker's capabilities."""


class LeaseNotActive(DispatchError):
    """The lease being completed/failed/extended isn't in 'active' state."""


def _counts_towards_worker_quarantine(error: dict | None) -> bool:
    """Return whether a failure indicates an unhealthy worker runtime.

    Local diagnostics are expected operator/setup feedback: missing browser
    permissions, not logged in, or Chrome inspection disabled. Those should
    fail the step and surface to the UI, but they should not quarantine the
    whole local worker.
    """
    if not isinstance(error, dict):
        return True
    err_type = str(error.get("type") or "")
    err_code = str(error.get("code") or "")
    if err_type == "LocalDiagnosticFailed" or err_code == "local_diagnostic_failed":
        return False
    return True


def _validation_failure_debug(result: Any) -> dict[str, Any]:
    try:
        preview = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        preview = str(result)

    debug: dict[str, Any] = {"result_preview": preview[:1200]}
    if isinstance(result, dict):
        debug["result_keys"] = sorted(str(key) for key in result.keys())[:40]
    return debug


def _target_worker_id_for_plan(plan: ExecutionPlan) -> str | None:
    """Return an explicit worker routing constraint for ad-hoc local actions."""
    for container in (plan.dispatcher_state, plan.plan_dag):
        if isinstance(container, dict):
            target = container.get("target_worker_id")
            if target:
                return str(target)
    return None


def _plan_source(plan: ExecutionPlan) -> str:
    for container in (plan.dispatcher_state, plan.plan_dag):
        if isinstance(container, dict):
            source = container.get("source")
            if source:
                return str(source)
    return ""


def _has_governance_approval_once(step: ExecutionStep) -> bool:
    params = dict(step.params or {})
    approval = params.get("_governance_approval")
    if not isinstance(approval, dict):
        return False
    if approval.get("status") != "approved" or approval.get("consumed_at"):
        return False
    return _governance_approval_matches_step(step, approval)


def _consume_governance_approval_once(step: ExecutionStep) -> bool:
    params = dict(step.params or {})
    approval = params.get("_governance_approval")
    if not isinstance(approval, dict):
        return False
    if approval.get("status") != "approved" or approval.get("consumed_at"):
        return False
    if not _governance_approval_matches_step(step, approval):
        return False
    approval = {
        **approval,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
        "consumed_for_step_id": step.id,
    }
    params["_governance_approval"] = approval
    step.params = params
    step.error = None
    step.human_input_prompt = None
    return True


def _step_runtime_capability_id(step: ExecutionStep) -> str | None:
    materialized = str(getattr(step, "capability_id", None) or "").strip()
    if materialized:
        return materialized
    return runtime_capability_id_for_action_key(
        getattr(step, "action_key", None),
        provider=getattr(step, "provider", None),
    )


async def _hydrate_runtime_action_binding_schemas(
    db: AsyncSession,
    step: ExecutionStep,
) -> bool:
    """Attach provider action schemas from Runtime binding catalog when absent."""

    if step.kind != "action":
        return False
    if step.expected_input_schema and step.expected_output_schema:
        return False
    provider = str(step.provider or "").strip()
    action_key = str(step.action_key or "").strip()
    if not provider or not action_key:
        return False

    from packages.core.models.mcp import MCPServer

    tools_cached = (
        await db.execute(
            select(MCPServer.tools_cached).where(
                MCPServer.server_key == provider,
                MCPServer.status == "active",
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not tools_cached:
        return False

    action_specs = runtime_planner_action_specs_from_tools_cached(tools_cached)
    binding = runtime_planner_action_binding_for(
        provider=provider,
        action_key=action_key,
        provider_actions={provider: list(action_specs.keys())},
        provider_action_specs={provider: action_specs},
    )
    if binding is None:
        return False

    hydrated = False
    if binding.input_schema is not None and not step.expected_input_schema:
        step.expected_input_schema = binding.input_schema
        hydrated = True
    if binding.output_schema is not None and not step.expected_output_schema:
        step.expected_output_schema = binding.output_schema
        hydrated = True
    return hydrated


async def _agent_allows_action_step(
    db: AsyncSession,
    *,
    agent_id: str | None,
    provider: str | None,
    action_key: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    """Check the resolved service agent owns the MCP action being leased."""

    provider_key = str(provider or "").strip()
    action = str(action_key or "").strip()
    if provider_key == "browser_setup":
        return True, None
    if not agent_id or not provider_key or not action:
        return False, {
            "type": "ActionBindingDenied",
            "message": "action step is missing resolved agent, provider, or action_key",
            "provider": provider_key,
            "action_key": action,
        }

    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    row = (
        await db.execute(
            select(AgentMCPBinding, MCPServer)
            .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
            .where(
                AgentMCPBinding.agent_id == agent_id,
                AgentMCPBinding.status == "active",
                MCPServer.server_key == provider_key,
                MCPServer.status == "active",
            )
            .limit(1)
        )
    ).first()
    from packages.core.services.agent_permission_service import resolve_agent_direct_mcp_actions

    direct_actions = await resolve_agent_direct_mcp_actions(db, agent_id)
    if action in direct_actions.get(provider_key, set()):
        return True, None

    if not row:
        return False, {
            "type": "ActionBindingDenied",
            "message": (
                f"agent {agent_id} is not bound to MCP provider "
                f"{provider_key!r}"
            ),
            "provider": provider_key,
            "action_key": action,
            "agent_id": agent_id,
        }

    binding, server = row
    allowed_tools = (
        binding.allowed_tools
        if binding.allowed_tools is not None
        else server.default_allowed_tools
    )
    if allowed_tools is None:
        return True, None

    allowed = {str(name) for name in allowed_tools if str(name or "").strip()}
    qualified = f"mcp__{provider_key}__{action}"
    if action in allowed or qualified in allowed:
        return True, None

    return False, {
        "type": "ActionBindingDenied",
        "message": (
            f"agent {agent_id} is not allowed to execute "
            f"{provider_key}.{action}"
        ),
        "provider": provider_key,
        "action_key": action,
        "agent_id": agent_id,
        "allowed_tools": sorted(allowed),
    }


def _worker_supported_capability_ids(capabilities: dict[str, Any]) -> set[str] | None:
    """Return explicit worker Runtime capability allowlist, if declared."""
    raw = (
        capabilities.get("supported_capabilities")
        or capabilities.get("runtime_capabilities")
        or capabilities.get("capability_ids")
    )
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in raw if str(value or "").strip()}


def _governance_approval_matches_step(step: ExecutionStep, approval: dict[str, Any]) -> bool:
    approved_step_id = str(approval.get("step_id") or "")
    if approved_step_id and approved_step_id != str(getattr(step, "id", "") or ""):
        return False
    approved_action = str(approval.get("action_key") or "")
    approved_capability = str(approval.get("capability_id") or "")
    if approved_action and approved_action != str(getattr(step, "action_key", None) or ""):
        return False
    if approved_capability and approved_capability != str(_step_runtime_capability_id(step) or ""):
        return False
    return bool(approved_step_id or approved_action or approved_capability)


def _step_intrinsic_approval_reason(step: ExecutionStep, capability_id: str | None) -> tuple[str, str] | None:
    """Return (matched_rule, reason) when the step itself requires HITL."""
    if getattr(step, "requires_approval", False):
        subject = step.action_key or capability_id or step.step_key
        return (
            "step.requires_approval",
            f"Step {step.step_key!r} requires operator approval before dispatching {subject!r}.",
        )
    if (step.risk_level or "low") == "high":
        subject = step.action_key or capability_id or step.step_key
        return (
            "step.high_risk",
            f"Step {step.step_key!r} is high risk and needs one-time operator approval before dispatching {subject!r}.",
        )
    return None


# ── Public API ────────────────────────────────────────────────────────


class Dispatcher:
    """Stateless service object — one per process.

    Methods take a DB session (caller controls transaction boundaries).
    Concurrency is handled at the SQL layer via FOR UPDATE SKIP LOCKED;
    no in-process locking required.
    """

    def __init__(self, *, lease_ttl: timedelta = _DEFAULT_LEASE_TTL):
        self._lease_ttl = lease_ttl

    # ── Checkout (the hot path) ───────────────────────────────────────

    async def checkout_steps_for_worker(
        self,
        db: AsyncSession,
        worker: Worker,
        *,
        max_n: int = 1,
        plan_id: Optional[str] = None,
    ) -> list[tuple[WorkLease, ExecutionStep]]:
        """Atomically claim up to ``max_n`` runnable steps for this worker.

        ``plan_id`` filter is for testing / scoped runs; production
        callers leave it None and the worker picks across all plans.

        Returns a list of (lease, step) tuples. Caller commits.
        """
        from packages.core.observability import span

        async with span(
            "dispatcher.checkout",
            attributes={
                "worker.id": worker.id,
                "worker.kind": worker.kind,
                "max_n": max_n,
                "plan_id": plan_id,
            },
        ) as _sp:
            return await self._checkout_steps_for_worker_inner(
                db,
                worker,
                max_n=max_n,
                plan_id=plan_id,
                _sp=_sp,
            )

    async def _checkout_steps_for_worker_inner(
        self,
        db: AsyncSession,
        worker: Worker,
        *,
        max_n: int,
        plan_id: Optional[str],
        _sp: Any = None,
    ) -> list[tuple[WorkLease, ExecutionStep]]:
        if worker.status != "active":
            return []

        capabilities = worker.capabilities or {}
        supported_kinds = capabilities.get("supported_kinds") or []
        if not supported_kinds:
            return []

        # ── Worker-level budget gate ──
        # Workspace gate happens per-step inside the loop because one
        # checkout can pull steps across multiple workspaces.
        from packages.core.budget import check_worker_budget

        worker_ok, worker_reason = check_worker_budget(worker)
        if not worker_ok:
            logger.info("dispatcher: worker %s skipped — %s", worker.id, worker_reason)
            return []

        max_risk = _RISK_RANK.get(capabilities.get("max_risk_level") or "low", 0)
        allowed_risk_levels = [
            level for level, rank in _RISK_RANK.items()
            if rank <= max_risk
        ]
        supported_providers = capabilities.get("supported_providers")
        # supported_providers=None means "any provider"; missing key
        # treated the same; explicit empty list means "no providers".
        supported_capabilities = _worker_supported_capability_ids(capabilities)
        # supported_capabilities=None means "any Runtime capability"; an
        # explicit empty list means this worker accepts no capability-scoped
        # leases.

        # Subquery: subscription_ids this worker is bound to.
        bound_subs = select(SubscriptionWorker.subscription_id).where(
            SubscriptionWorker.worker_id == worker.id,
        )

        # Pre-build the candidate query. Dependency check is in Python
        # below — keeping the SQL simpler and easier to audit.
        stmt = (
            select(ExecutionStep, ExecutionPlan)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
            .where(
                ExecutionStep.step_status == "pending",
                ExecutionStep.entity_id == worker.entity_id,
                ExecutionStep.kind.in_(supported_kinds),
                ExecutionStep.risk_level.in_(allowed_risk_levels),
                ExecutionStep.attempt_count < ExecutionStep.max_attempts,
                ExecutionPlan.status.in_(("draft", "running")),
                # No active lease for this step (the partial unique index
                # is the hard guarantee; this filter is the soft one
                # that lets the DB skip already-leased rows fast).
                ~exists().where(
                    and_(
                        WorkLease.step_id == ExecutionStep.id,
                        WorkLease.status == "active",
                    )
                ),
            )
            .order_by(ExecutionStep.created_at.asc())
            .limit(max_n * 4)  # room to skip un-mappable rows
            .with_for_update(skip_locked=True)
        )
        dispatcher_target_worker_id = ExecutionPlan.dispatcher_state["target_worker_id"].astext
        plan_target_worker_id = ExecutionPlan.plan_dag["target_worker_id"].astext
        stmt = stmt.where(
            or_(dispatcher_target_worker_id.is_(None), dispatcher_target_worker_id == worker.id),
            or_(plan_target_worker_id.is_(None), plan_target_worker_id == worker.id),
        )
        if supported_providers is not None:
            provider_filters = [
                ExecutionStep.provider == "browser_setup",
                ExecutionStep.provider.in_(supported_providers),
            ]
            stmt = stmt.where(
                or_(
                    ExecutionStep.kind != "action",
                    *provider_filters,
                )
            )
        if plan_id is not None:
            stmt = stmt.where(ExecutionStep.plan_id == plan_id)

        rows = (await db.execute(stmt)).all()
        if not rows:
            return []

        # Resolve subscription bindings once.
        bound_sub_ids = set((await db.execute(bound_subs)).scalars().all())

        leased: list[tuple[WorkLease, ExecutionStep]] = []
        for step, plan in rows:
            if len(leased) >= max_n:
                break
            now = datetime.now(timezone.utc)
            local_worker_plan = _plan_source(plan) == "local_worker"

            if worker.kind == "internal" and (local_worker_plan or step.provider == "browser_setup"):
                continue

            target_worker_id = _target_worker_id_for_plan(plan)
            if target_worker_id and target_worker_id != worker.id:
                continue

            from packages.core.services.retry_policy import (
                apply_retry_policy_to_step,
                resolve_retry_policy_for_step,
                step_retry_ready,
            )

            retry_policy = await resolve_retry_policy_for_step(db, step, plan=plan)
            apply_retry_policy_to_step(step, retry_policy)
            if not step_retry_ready(step, now=now):
                continue
            if step.attempt_count >= step.max_attempts:
                step.step_status = "failed"
                step.error = {
                    "type": "RetryPolicyExhausted",
                    "message": "attempt_count reached max_attempts before checkout",
                    "retry_policy": {
                        "source": retry_policy.source,
                        "strategy": retry_policy.strategy,
                        "max_attempts": retry_policy.max_attempts,
                    },
                }
                step.finished_at = now
                continue

            # Capability check beyond what SQL can express.
            step_risk = _RISK_RANK.get(step.risk_level or "low", 0)
            if step_risk > max_risk:
                continue

            # ── Workspace pause gate ──
            if step.workspace_id:
                from packages.core.models.workspace import Workspace as _Ws

                _ws = (await db.execute(select(_Ws.status).where(_Ws.id == step.workspace_id))).scalar_one_or_none()
                if _ws and _ws != "active":
                    continue

            # ── Workspace-level budget gate ──
            # Re-checked per step because one checkout can cross many
            # workspaces. Cheap (single SELECT) and avoids leasing a
            # step we'd refuse to bill for anyway.
            from packages.core.budget import check_workspace_budget

            ws_ok, ws_reason = await check_workspace_budget(db, step.workspace_id)
            if not ws_ok:
                logger.info("dispatcher: skipping step %s — %s", step.step_key, ws_reason)
                continue

            step_capability_id = _step_runtime_capability_id(step)
            if step_capability_id and not getattr(step, "capability_id", None):
                step.capability_id = step_capability_id
            if supported_capabilities is not None and not local_worker_plan:
                if not step_capability_id or step_capability_id not in supported_capabilities:
                    continue

            # ── Step-level approval gate ──
            # Planner can mark a specific step as approval-gated, and high-risk
            # steps keep the same safety posture without blocking the whole plan
            # at creation time. Approval is one-use and scoped to this step.
            if not _has_governance_approval_once(step):
                intrinsic_approval = _step_intrinsic_approval_reason(
                    step,
                    step_capability_id,
                )
                # A workspace that EXPLICITLY auto-approves this capability/action
                # overrides the capability-level intrinsic requires_approval — the
                # operator opted in. Fall through to the governance policy gate
                # below, which re-confirms allow and still enforces deny / risk /
                # budget. (Without this, file.write & other required_approval
                # capabilities pause even when the workspace policy grants them,
                # because this intrinsic gate runs before the policy gate.)
                if intrinsic_approval is not None and step.workspace_id:
                    from packages.core.governance.service import (
                        workspace_policy_auto_approves,
                    )
                    if await workspace_policy_auto_approves(
                        db,
                        workspace_id=step.workspace_id,
                        action_key=step.action_key,
                        capability_id=step_capability_id,
                    ):
                        intrinsic_approval = None
                if intrinsic_approval is not None:
                    matched_rule, reason = intrinsic_approval
                    if step.workspace_id:
                        from packages.core.governance.service import post_hitl_card

                        step.step_status = "waiting_human"
                        step.human_input_prompt = reason
                        step.current_lease_id = None
                        step.error = {
                            "type": "StepApprovalRequired",
                            "message": reason,
                            "matched_rule": matched_rule,
                            "capability_id": step_capability_id,
                        }
                        await post_hitl_card(
                            entity_id=step.entity_id,
                            workspace_id=step.workspace_id,
                            plan_id=step.plan_id,
                            step_id=step.id,
                            step_key=step.step_key,
                            kind=step.kind,
                            action_key=step.action_key,
                            capability_id=step_capability_id,
                            matched_rule=matched_rule,
                            reason=reason,
                        )
                    else:
                        step.step_status = "failed"
                        step.error = {
                            "type": "StepApprovalUnavailable",
                            "message": reason,
                            "matched_rule": matched_rule,
                            "capability_id": step_capability_id,
                        }
                        step.finished_at = now
                    logger.info(
                        "dispatcher: step %s requires approval (%s) — %s",
                        step.step_key, matched_rule, reason,
                    )
                    continue

            # ── Governance policy gate ──
            # Hard-deny → step.failed with policy reason; HITL-required →
            # waiting_human + structured workspace-chat pending_action.
            # A user approval writes a one-use bypass into step.params; the
            # dispatcher consumes it immediately before lease creation.
            if not _has_governance_approval_once(step):
                from packages.core.budget import get_workspace_spent_credits_per_kind
                from packages.core.governance import check_step_policy
                from packages.core.governance.service import post_hitl_card
                spent_credits_per_kind = (
                    await get_workspace_spent_credits_per_kind(db, step.workspace_id)
                    if step.workspace_id else None
                )
                decision = await check_step_policy(
                    db,
                    workspace_id=step.workspace_id,
                    kind=step.kind,
                    action_key=step.action_key,
                    risk_level=step.risk_level or "low",
                    capability_id=step_capability_id,
                    spent_credits_per_kind=spent_credits_per_kind,
                    task_id=plan.task_id,
                )
                if not decision.allowed:
                    if decision.pause_for_hitl and step.workspace_id:
                        step.step_status = "waiting_human"
                        step.human_input_prompt = decision.reason
                        step.current_lease_id = None
                        step.error = {
                            "type": "GovernancePolicyHITL",
                            "message": decision.reason,
                            "matched_rule": decision.matched_rule,
                            "capability_id": step_capability_id,
                        }
                        await post_hitl_card(
                            entity_id=step.entity_id,
                            workspace_id=step.workspace_id,
                            plan_id=step.plan_id,
                            step_id=step.id,
                            step_key=step.step_key,
                            kind=step.kind,
                            action_key=step.action_key,
                            capability_id=step_capability_id,
                            matched_rule=decision.matched_rule,
                            reason=decision.reason,
                        )
                    else:
                        step.step_status = "failed"
                        step.error = {
                            "type": "GovernancePolicy",
                            "message": decision.reason,
                            "matched_rule": decision.matched_rule,
                            "capability_id": step_capability_id,
                        }
                        step.finished_at = datetime.now(timezone.utc)
                    logger.info(
                        "dispatcher: step %s blocked by policy (%s) — %s",
                        step.step_key, decision.matched_rule, decision.reason,
                    )
                    continue

            # Subscription gating — for kinds that need an owner.
            if step.kind in ("llm", "action", "subagent", "code"):
                ws_sub = await _resolve_subscription_for_step(db, step)
                if ws_sub is not None:
                    if ws_sub.id not in bound_sub_ids:
                        # Worker not bound to this subscription — skip.
                        continue
                    step.resolved_subscription_id = ws_sub.id
                    step.resolved_agent_id = ws_sub.agent_id
                    if step.kind == "action":
                        allowed_action, action_error = await _agent_allows_action_step(
                            db,
                            agent_id=ws_sub.agent_id,
                            provider=step.provider,
                            action_key=step.action_key,
                        )
                        if not allowed_action:
                            step.step_status = "failed"
                            step.error = action_error or {
                                "type": "ActionBindingDenied",
                                "message": "agent action binding denied",
                            }
                            step.finished_at = now
                            step.current_lease_id = None
                            continue
                elif step.service_key or step.workspace_id:
                    # Plan/workspace-routed steps must resolve to a
                    # subscription before they can be leased.
                    continue

            # Dependency check — if any dep failed/cancelled, mark
            # the step skipped instead of leasing.
            dep_status = await _check_dependency_status(db, step)
            if dep_status == "blocked_by_failure":
                step.step_status = "skipped"
                step.finished_at = now
                continue
            if dep_status == "waiting":
                continue

            # Make the leased payload self-contained. PlanExecutor also
            # resolves refs, but worker heartbeat can reach the dispatcher
            # before the next executor cycle. Resolve here too so downstream
            # workers never receive raw `${{ steps.* }}` placeholders.
            if _contains_step_ref(step.params):
                try:
                    prior_results = await _collect_done_step_results(db, step)
                    step.params = resolve_refs(step.params or {}, prior_results)
                except PlanReferenceError as exc:
                    step.step_status = "failed"
                    step.error = {
                        "type": "ReferenceError",
                        "message": str(exc),
                    }
                    step.finished_at = now
                    continue

            await _hydrate_runtime_action_binding_schemas(db, step)

            # Optional input-schema validation, before the lease goes out.
            try:
                validate_step_input(step, step.params or {})
            except SchemaError as exc:
                step.step_status = "failed"
                step.error = {
                    "type": "InputSchemaError",
                    "message": str(exc),
                    "errors": exc.errors,
                }
                step.finished_at = now
                continue

            _consume_governance_approval_once(step)

            lease = WorkLease(
                id=generate_ulid(),
                step_id=step.id,
                plan_id=step.plan_id,
                entity_id=step.entity_id,
                workspace_id=step.workspace_id,
                worker_id=worker.id,
                subscription_id=step.resolved_subscription_id,
                lease_until=now + self._lease_ttl,
                status="active",
            )
            db.add(lease)

            # Step state transition.
            step.step_status = "running"
            step.current_lease_id = lease.id
            step.attempt_count = (step.attempt_count or 0) + 1
            if step.started_at is None:
                step.started_at = now

            # Activity log for audit.
            db.add(
                WorkerActivityLog(
                    worker_id=worker.id,
                    event="lease_grant",
                    lease_id=lease.id,
                    payload_summary={
                        "step_key": step.step_key,
                        "kind": step.kind,
                        "capability_id": _step_runtime_capability_id(step),
                        "plan_id": step.plan_id,
                    },
                )
            )

            leased.append((lease, step))

        if leased:
            worker.last_heartbeat_at = datetime.now(timezone.utc)

        if _sp is not None:
            try:
                _sp.set_attribute("leases.granted", len(leased))
            except Exception:
                pass

        await db.flush()
        return leased

    # ── Completion ────────────────────────────────────────────────────

    async def complete_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        result: Optional[dict] = None,
        cost: Optional[dict] = None,
        evidence_refs: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> WorkLease:
        """Worker reports success. Validates output against step schema,
        marks lease + step done, releases credential subleases. Caller commits."""
        from packages.core.observability import span

        async with span(
            "dispatcher.complete_lease",
            attributes={
                "lease.id": lease_id,
                "cost.usd": (cost or {}).get("usd"),
            },
        ):
            return await self._complete_lease_inner(
                db,
                lease_id,
                result=result,
                cost=cost,
                evidence_refs=evidence_refs,
                metadata=metadata,
            )

    async def _complete_lease_inner(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        result: Optional[dict],
        cost: Optional[dict],
        evidence_refs: Optional[list[str]],
        metadata: Optional[dict],
    ) -> WorkLease:
        lease = await self._get_active_lease(db, lease_id)
        step = await self._get_step(db, lease.step_id)
        now = datetime.now(timezone.utc)

        if result is not None:
            result = coerce_step_output_for_schema(step.expected_output_schema, result)
            try:
                validate_step_output(step, result)
            except SchemaError as exc:
                if output_schema_is_advisory(step.kind):
                    # Free-form agent output (llm/subagent): the Planner-guessed
                    # output schema is advisory, not a contract. Accept the real
                    # output instead of dead-failing the step (which otherwise
                    # burns 3 retries and strands the whole plan).
                    logger.warning(
                        "[dispatcher] step %s kind=%s output failed its (advisory) "
                        "schema; accepting real output anyway: %s",
                        step.step_key, step.kind, exc,
                    )
                else:
                    # Structured kinds: schema is a hard contract — fail.
                    lease.result = result if isinstance(result, dict) else {"value": result}
                    if cost:
                        lease.cost = cost
                    return await self.fail_lease(
                        db,
                        lease_id,
                        error={
                            "type": "OutputSchemaError",
                            "message": str(exc),
                            "errors": exc.errors,
                            **_validation_failure_debug(result),
                        },
                    )

        # Lease side
        lease.status = "completed"
        lease.result = result
        if cost:
            lease.cost = cost
        if evidence_refs:
            lease.evidence_refs = list(evidence_refs)

        # Step side — mirror the result so PlanExecutor can pick it
        # up without joining work_leases on every cycle.
        step.step_status = "done"
        step.result = result if isinstance(result, dict) else ({"value": result} if result is not None else None)
        step.finished_at = now
        if cost:
            step.cost = cost
        if evidence_refs:
            step.evidence_refs = list(evidence_refs)
        step.error = None
        step.current_lease_id = None  # lease is terminal

        # Sublease cleanup is M3.5 (when integrated with the Vault).
        # For now, just zero out the index — real revoke happens in the
        # next iteration when CredentialService.release_sublease lands.
        lease.credential_leases = []

        activity_summary = {
            "step_key": step.step_key,
            "duration_s": _duration(step),
            "capability_id": _step_runtime_capability_id(step),
        }
        if metadata:
            activity_summary["metadata"] = metadata
        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_complete",
                lease_id=lease.id,
                payload_summary=activity_summary,
            )
        )
        worker = (await db.execute(select(Worker).where(Worker.id == lease.worker_id))).scalar_one_or_none()
        if worker is not None and (worker.consecutive_failures or 0) > 0:
            worker.consecutive_failures = 0
        await db.flush()

        # ── Cost rollup (M8) — runs in the same transaction so the
        # workspace.monthly_spent_usd lands atomically with the lease
        # state transition. The accumulator may emit a chat alert if
        # we just crossed 80% / 100% thresholds.
        try:
            from packages.core.budget import accumulate_step_cost

            await accumulate_step_cost(db, step, lease)
        except Exception:
            logger.warning(
                "dispatcher: budget accumulation failed for step %s",
                step.id,
                exc_info=True,
            )

        await _safe_chat_step_done(step, cost=cost)
        dur = _duration(step)
        step_label = step.step_key.replace("_", " ").title()
        result_preview = ""
        if step.result:
            text = step.result.get("text") or step.result.get("value") or ""
            if isinstance(text, str) and text.strip():
                result_preview = text.strip()[:300]
        log_content = (
            f"✓ **{step_label}** completed"
            + (f" in {dur:.1f}s" if dur else "")
            + (f"\n\n{result_preview}" if result_preview else "")
        )
        log_meta_extra = {"duration_s": dur, "cost": cost}
        if isinstance(metadata, dict):
            if metadata.get("runtime") is not None:
                log_meta_extra["runtime"] = metadata["runtime"]
                if metadata.get("runtime_event_summary") is not None:
                    log_meta_extra["runtime_event_summary"] = metadata["runtime_event_summary"]
            else:
                log_meta_extra["worker_metadata"] = metadata
        await _safe_task_log(
            step,
            "step_completed",
            log_content,
            metadata=_step_log_meta(step, lease, **log_meta_extra),
        )
        await _safe_signal_workflow_done(step, result)
        return lease

    async def fail_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        error: dict,
        will_retry: Optional[bool] = None,
    ) -> WorkLease:
        """Worker reports failure. Step retries if attempts remain.

        ``will_retry`` overrides the auto-retry decision (used by the
        worker when it knows the failure is permanent — bad credentials,
        unsupported action, etc.).
        """
        lease = await self._get_active_lease(db, lease_id)
        step = await self._get_step(db, lease.step_id)
        now = datetime.now(timezone.utc)

        from packages.core.services.retry_policy import (
            apply_retry_policy_to_step,
            error_with_retry_policy,
            human_prompt_for_exhausted_step,
            resolve_retry_policy_for_step,
        )

        retry_policy = await resolve_retry_policy_for_step(db, step)
        apply_retry_policy_to_step(step, retry_policy)

        retry = will_retry if will_retry is not None else step.attempt_count < step.max_attempts
        stored_error, next_retry_at = error_with_retry_policy(
            error,
            policy=retry_policy,
            now=now,
            attempt_count=step.attempt_count,
        )

        lease.status = "failed"
        lease.error = stored_error
        lease.credential_leases = []

        if retry:
            step.step_status = "pending"  # back in the queue
            step.error = stored_error  # surface the most recent error
            step.current_lease_id = None
        elif retry_policy.auto_human_on_exhausted:
            step.step_status = "waiting_human"
            step.error = stored_error
            step.human_input_prompt = human_prompt_for_exhausted_step(step, stored_error, retry_policy)
            step.finished_at = None
            step.current_lease_id = None
        else:
            step.step_status = "failed"
            step.error = stored_error
            step.finished_at = now
            step.current_lease_id = None

        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_fail",
                lease_id=lease.id,
                payload_summary={
                    "step_key": step.step_key,
                    "capability_id": _step_runtime_capability_id(step),
                    "will_retry": retry,
                    "error_type": stored_error.get("type"),
                },
            )
        )
        await _safe_chat_step_failed(step, error=stored_error, will_retry=retry)
        err_type = stored_error.get("type", "unknown")
        err_msg = stored_error.get("message", "")[:200]
        await _safe_task_log(
            step,
            "step_failed" if not retry else "step_retrying",
            f"✗ Step '{step.step_key}' failed: {err_type}"
            + (" — retrying" if retry else "")
            + (f"\n{err_msg}" if err_msg else ""),
            metadata=_step_log_meta(
                step,
                lease,
                error=stored_error,
                will_retry=retry,
                next_retry_at=next_retry_at.isoformat() if next_retry_at else None,
                retry_policy={
                    "source": retry_policy.source,
                    "strategy": retry_policy.strategy,
                    "max_attempts": retry_policy.max_attempts,
                    "auto_human_on_exhausted": retry_policy.auto_human_on_exhausted,
                },
            ),
        )
        if not retry and retry_policy.auto_human_on_exhausted:
            await _safe_chat_step_needs_human(step, prompt=step.human_input_prompt or "")
            await _safe_task_log(
                step,
                "step_needs_human",
                f"⏸ Step '{step.step_key}' needs human guidance after retry exhaustion.",
                metadata=_step_log_meta(
                    step,
                    lease,
                    error=stored_error,
                    prompt=step.human_input_prompt,
                    hitl={
                        "kind": "human_input",
                        "fields": [{"name": "response", "type": "textarea", "required": True}],
                    },
                ),
            )
            await _safe_task_event(
                step,
                "task.hitl_requested",
                {
                    "plan_id": step.plan_id,
                    "step_ids": [step.id],
                    "lease_id": lease.id,
                    "worker_id": lease.worker_id,
                    "prompt": step.human_input_prompt,
                    "reason": "retry_policy_exhausted",
                },
            )
        # Only signal Temporal on TERMINAL failure — retries stay
        # invisible to the workflow (the next worker pickup is just
        # another attempt at the same step from the workflow's POV).
        if not retry and step.step_status == "failed":
            await _safe_signal_workflow_failed(step, stored_error)

        # Quarantine workers that fail repeatedly — keeps a buggy
        # external worker from holding the queue hostage.
        worker = (await db.execute(select(Worker).where(Worker.id == lease.worker_id))).scalar_one_or_none()
        if worker is not None and _counts_towards_worker_quarantine(stored_error):
            worker.consecutive_failures = (worker.consecutive_failures or 0) + 1
            if worker.kind != "internal" and worker.consecutive_failures >= 5:
                worker.status = "quarantined"
                db.add(
                    WorkerActivityLog(
                        worker_id=worker.id,
                        event="quarantine",
                        payload_summary={"reason": "consecutive_failures >= 5"},
                    )
                )

        await db.flush()
        return lease

    async def extend_lease(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        extra_seconds: Optional[float] = None,
        progress: Optional[float] = None,
    ) -> WorkLease:
        """Worker is still alive — bump ``lease_until``.

        ``extra_seconds`` defaults to the dispatcher's default TTL.
        Increments ``heartbeat_count`` so we can audit chatty workers.
        Caller commits.
        """
        lease = await self._get_active_lease(db, lease_id)
        delta = timedelta(seconds=extra_seconds) if extra_seconds else self._lease_ttl
        lease.lease_until = datetime.now(timezone.utc) + delta
        lease.heartbeat_count = (lease.heartbeat_count or 0) + 1
        lease.last_heartbeat_at = datetime.now(timezone.utc)
        lease.extended_count = (lease.extended_count or 0) + 1
        if progress is not None:
            lease.progress = max(0.0, min(1.0, float(progress)))
        await db.flush()
        return lease

    async def lease_needs_human(
        self,
        db: AsyncSession,
        lease_id: str,
        *,
        prompt: str,
        pending_action: Optional[dict] = None,
    ) -> WorkLease:
        """Worker hit a CAPTCHA / approval / 2FA wall. Lease pauses;
        step → waiting_human; chat surfaces an interactive prompt.

        ``pending_action`` is the optional generic structured payload
        (``{kind, options, …}``) defined in
        packages/core/ai/pending_action.py. When provided, the chat
        message renders the kind-specific button card (sign-in,
        question form, confirm) instead of a free-form text input.
        ``prompt`` is still used for log lines + step.human_input_prompt
        so existing UIs that read that field continue to work.
        """
        lease = await self._get_active_lease(db, lease_id)
        step = await self._get_step(db, lease.step_id)

        lease.status = "needs_human"
        lease.credential_leases = []  # release while waiting

        step.step_status = "waiting_human"
        step.human_input_prompt = prompt
        step.current_lease_id = None

        db.add(
            WorkerActivityLog(
                worker_id=lease.worker_id,
                event="lease_needs_human",
                lease_id=lease.id,
                payload_summary={
                    "step_key": step.step_key,
                    "capability_id": _step_runtime_capability_id(step),
                    "pending_action_kind": (pending_action or {}).get("kind"),
                },
            )
        )
        await db.flush()
        await _safe_chat_step_needs_human(
            step,
            prompt=prompt,
            pending_action=pending_action,
        )
        await _safe_task_log(
            step,
            "step_needs_human",
            f"⏸ Step '{step.step_key}' waiting for human input: {prompt[:200]}",
            metadata=_step_log_meta(
                step,
                lease,
                prompt=prompt,
                hitl={
                    "kind": "human_input",
                    "fields": [{"name": "response", "type": "textarea", "required": True}],
                },
            ),
        )
        await _safe_task_event(
            step,
            "task.hitl_requested",
            {
                "plan_id": step.plan_id,
                "step_ids": [step.id],
                "lease_id": lease.id,
                "worker_id": lease.worker_id,
                "prompt": prompt,
                "task_status": "in_progress",
            },
        )
        return lease

    # ── Periodic sweep ───────────────────────────────────────────────

    async def expire_leases(
        self,
        db: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Reclaim leases past lease_until. Caller commits.

        Each expired lease bumps its step back to pending (if attempts
        remain) or failed (if exhausted). Returns count expired.
        """
        now = now or datetime.now(timezone.utc)
        rows = list(
            (
                await db.execute(
                    select(WorkLease)
                    .where(
                        WorkLease.status == "active",
                        WorkLease.lease_until < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for lease in rows:
            lease.status = "expired"
            lease.credential_leases = []

            step = await self._get_step(db, lease.step_id)
            if step is None:
                continue

            from packages.core.services.retry_policy import (
                apply_retry_policy_to_step,
                error_with_retry_policy,
                human_prompt_for_exhausted_step,
                resolve_retry_policy_for_step,
            )

            retry_policy = await resolve_retry_policy_for_step(db, step)
            apply_retry_policy_to_step(step, retry_policy)
            expired_error, next_retry_at = error_with_retry_policy(
                {"type": "lease_expired", "message": "lease expired before completion"},
                policy=retry_policy,
                now=now,
                attempt_count=step.attempt_count,
            )

            if step.attempt_count < step.max_attempts:
                step.step_status = "pending"
                step.error = expired_error
                step.current_lease_id = None
            elif retry_policy.auto_human_on_exhausted:
                step.step_status = "waiting_human"
                step.error = expired_error
                step.human_input_prompt = human_prompt_for_exhausted_step(step, expired_error, retry_policy)
                step.current_lease_id = None
            else:
                step.step_status = "failed"
                step.error = {"type": "lease_expired", "message": "all attempts expired", **expired_error}
                step.finished_at = now
                step.current_lease_id = None

            db.add(
                WorkerActivityLog(
                    worker_id=lease.worker_id,
                    event="lease_expire",
                    lease_id=lease.id,
                    payload_summary={
                        "step_key": step.step_key,
                        "attempts_remaining": step.max_attempts - step.attempt_count,
                        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                    },
                )
            )

        await db.flush()
        return len(rows)

    # ── Internals ────────────────────────────────────────────────────

    async def _get_active_lease(
        self,
        db: AsyncSession,
        lease_id: str,
    ) -> WorkLease:
        lease = (await db.execute(select(WorkLease).where(WorkLease.id == lease_id))).scalar_one_or_none()
        if lease is None:
            raise DispatchError(f"lease {lease_id} not found")
        if lease.status != "active":
            raise LeaseNotActive(f"lease {lease_id} is {lease.status!r}, not active")
        return lease

    async def _get_step(
        self,
        db: AsyncSession,
        step_id: str,
    ) -> ExecutionStep:
        step = (await db.execute(select(ExecutionStep).where(ExecutionStep.id == step_id))).scalar_one_or_none()
        if step is None:
            raise DispatchError(f"step {step_id} not found")
        return step


# ── Module helpers ────────────────────────────────────────────────────


def _contains_step_ref(value: Any) -> bool:
    if isinstance(value, str):
        return "${{" in value and "steps." in value
    if isinstance(value, dict):
        return any(_contains_step_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_step_ref(v) for v in value)
    return False


async def _collect_done_step_results(
    db: AsyncSession,
    step: ExecutionStep,
) -> dict[str, Any]:
    rows = list(
        (
            await db.execute(
                select(ExecutionStep.step_key, ExecutionStep.result).where(
                    ExecutionStep.plan_id == step.plan_id,
                    ExecutionStep.step_status == "done",
                    ExecutionStep.result.is_not(None),
                )
            )
        ).all()
    )
    return {step_key: result for step_key, result in rows}


async def _resolve_subscription_for_step(
    db: AsyncSession,
    step: ExecutionStep,
) -> Optional[Any]:
    """Find the active subscription for a routed step.

    The caller caches both ``resolved_subscription_id`` and
    ``resolved_agent_id`` so downstream worker context uses the workspace-bound
    agent persona, not a default entity context.
    """
    from packages.core.models.workspace import AgentSubscription

    if step.resolved_subscription_id:
        conditions = [
            AgentSubscription.id == step.resolved_subscription_id,
            AgentSubscription.status == "active",
        ]
        if step.workspace_id:
            conditions.append(AgentSubscription.workspace_id == step.workspace_id)
        sub = (
            await db.execute(
                select(AgentSubscription)
                .where(*conditions)
                .limit(1)
            )
        ).scalar_one_or_none()
        if sub:
            return sub

    if not step.service_key or not step.workspace_id:
        return None

    sub = (
        await db.execute(
            select(AgentSubscription)
            .where(
                AgentSubscription.workspace_id == step.workspace_id,
                AgentSubscription.service_key == step.service_key,
                AgentSubscription.status == "active",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return sub


async def _check_dependency_status(
    db: AsyncSession,
    step: ExecutionStep,
) -> str:
    """Inspect ``step.depends_on`` against the current sibling steps.

    Returns:
      'ready'              all deps done — step can be leased
      'waiting'            some dep still pending/running — skip this cycle
      'blocked_by_failure' a dep failed/cancelled/skipped — caller marks
                            this step skipped
    """
    deps = step.depends_on or []
    if not deps:
        return "ready"

    rows = list(
        (
            await db.execute(
                select(ExecutionStep.step_key, ExecutionStep.step_status).where(
                    ExecutionStep.plan_id == step.plan_id,
                    ExecutionStep.step_key.in_(deps),
                )
            )
        ).all()
    )
    by_key = {k: s for k, s in rows}

    for dep in deps:
        s = by_key.get(dep)
        if s is None:
            # Plan invariant violated — depends_on references a step
            # that doesn't exist. Treat as blocked so the cohort can
            # still terminate (won't loop forever waiting).
            return "blocked_by_failure"
        if s in {"failed", "cancelled", "skipped"}:
            return "blocked_by_failure"
        if s != "done":
            return "waiting"
    return "ready"


def _duration(step: ExecutionStep) -> Optional[float]:
    if step.started_at and step.finished_at:
        return (step.finished_at - step.started_at).total_seconds()
    return None


def _iso_or_none(value) -> Optional[str]:
    return value.isoformat() if value else None


def _step_log_meta(
    step: ExecutionStep,
    lease: WorkLease | None = None,
    *,
    error: dict | None = None,
    **extra,
) -> dict:
    """Standard observability envelope for task logs emitted from steps."""
    err = error or step.error or {}
    meta = {
        "plan_id": step.plan_id,
        "step_id": step.id,
        "step_key": step.step_key,
        "kind": step.kind,
        "service_key": getattr(step, "service_key", None),
        "agent_id": getattr(step, "resolved_agent_id", None),
        "agent_subscription_id": getattr(step, "resolved_subscription_id", None),
        "provider": getattr(step, "provider", None),
        "action_key": getattr(step, "action_key", None),
        "capability_id": _step_runtime_capability_id(step),
        "lease_id": lease.id if lease else step.current_lease_id,
        "worker_id": lease.worker_id if lease else None,
        "attempt_count": step.attempt_count,
        "retry_count": step.attempt_count,
        "max_attempts": step.max_attempts,
        "lease_until": _iso_or_none(lease.lease_until if lease else None),
        "error_type": err.get("type") if isinstance(err, dict) else None,
        "error_message": err.get("message") if isinstance(err, dict) else None,
        "error": err or None,
    }
    meta.update(extra)
    return {key: value for key, value in meta.items() if value is not None}


async def _enrich_step_log_author_meta(db: AsyncSession, step: ExecutionStep, metadata: dict | None) -> dict:
    """Attach the user-facing step owner to dispatcher-authored task logs."""
    meta = dict(metadata or {})
    if getattr(step, "service_key", None):
        meta.setdefault("service_key", step.service_key)
    if getattr(step, "resolved_agent_id", None):
        meta.setdefault("agent_id", step.resolved_agent_id)
    if getattr(step, "resolved_subscription_id", None):
        meta.setdefault("agent_subscription_id", step.resolved_subscription_id)

    if not meta.get("agent_name") and step.resolved_subscription_id:
        try:
            from packages.core.models.workspace import AgentSubscription

            sub = (
                await db.execute(
                    select(AgentSubscription).where(
                        AgentSubscription.id == step.resolved_subscription_id,
                    )
                )
            ).scalar_one_or_none()
            if sub:
                meta.setdefault("agent_id", sub.agent_id)
                meta["agent_name"] = sub.name or sub.service_key or step.service_key
        except Exception:
            logger.debug("dispatcher: failed to enrich task log author from subscription", exc_info=True)

    return {key: value for key, value in meta.items() if value is not None}


# ── Task log + Chat notifications ────────────────────────────────────
# These run AFTER db.flush() but inside the caller's transaction. Each
# notifier opens its own session and commits independently, so a chat
# write failure can't roll back the lease state. Best-effort by design.


async def _safe_task_log(step: ExecutionStep, log_type: str, content: str, metadata: dict | None = None) -> None:
    """Write a TaskLog entry for plan step events. Best-effort."""
    try:
        from packages.core.database import async_session as _session
        from packages.core.services.task_service import add_task_log
        from packages.core.models.execution import ExecutionPlan

        async with _session() as db:
            plan = (
                await db.execute(select(ExecutionPlan.task_id).where(ExecutionPlan.id == step.plan_id))
            ).scalar_one_or_none()
            if plan:
                enriched_metadata = await _enrich_step_log_author_meta(db, step, metadata)
                created_by = enriched_metadata.get("agent_name") or enriched_metadata.get("agent_id") or "system"
                await add_task_log(db, plan, log_type, content, created_by=created_by, metadata=enriched_metadata)
                await db.commit()
    except Exception:
        logger.warning("dispatcher: task log write failed for step %s", step.id, exc_info=True)


async def _safe_task_event(step: ExecutionStep, event_type: str, payload: dict) -> None:
    """Emit task domain events for step-level dispatcher transitions."""
    try:
        from packages.core.database import async_session as _session
        from packages.core.models.execution import ExecutionPlan
        from packages.core.services import event_emitter

        async with _session() as db:
            plan = (
                await db.execute(select(ExecutionPlan).where(ExecutionPlan.id == step.plan_id))
            ).scalar_one_or_none()
            if not plan or not plan.task_id:
                return
            event_payload = {
                **payload,
                "task_id": plan.task_id,
                "plan_id": plan.id,
            }
            event_emitter.emit(
                step.entity_id,
                event_type,
                source="dispatcher",
                payload=event_payload,
            )
    except Exception:
        logger.warning("dispatcher: task event emit failed for step %s", step.id, exc_info=True)


async def _safe_chat_step_done(
    step: ExecutionStep,
    *,
    cost: Optional[dict] = None,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify
        from packages.core.database import async_session as _session

        # Resolve agent name from subscription
        agent_name = None
        if step.resolved_subscription_id:
            try:
                from packages.core.models.workspace import AgentSubscription

                async with _session() as _db:
                    sub = (
                        await _db.execute(
                            select(AgentSubscription).where(
                                AgentSubscription.id == step.resolved_subscription_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if sub:
                        agent_name = sub.name or sub.service_key
            except Exception:
                pass  # best-effort — don't block on agent lookup failure

        # Extract result summary from step
        result_summary = None
        if step.result:
            result_summary = chat_notify.summarize_result_for_chat(step.result, max_chars=2000)

        description = getattr(step, "description", None) or step.step_key.replace("_", " ").title()
        duration = _duration(step)
        cost_usd = (cost or {}).get("usd")

        await chat_notify.notify_step_done(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            kind=step.kind,
            description=description,
            duration_seconds=duration,
            cost_usd=cost_usd,
            subscription_id=step.resolved_subscription_id,
            agent_name=agent_name,
            result_summary=result_summary,
            result=step.result,
        )
    except Exception:
        logger.warning("dispatcher: chat notify (done) failed", exc_info=True)


async def _safe_chat_step_failed(
    step: ExecutionStep,
    *,
    error: dict,
    will_retry: bool,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify

        await chat_notify.notify_step_failed(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            error=error,
            will_retry=will_retry,
            subscription_id=step.resolved_subscription_id,
        )
    except Exception:
        logger.warning("dispatcher: chat notify (failed) failed", exc_info=True)


async def _safe_chat_step_needs_human(
    step: ExecutionStep,
    *,
    prompt: str,
    pending_action: Optional[dict] = None,
) -> None:
    if not step.workspace_id:
        return
    try:
        from packages.core.workspace_chat import notifiers as chat_notify

        await chat_notify.notify_step_needs_human(
            entity_id=step.entity_id,
            workspace_id=step.workspace_id,
            plan_id=step.plan_id,
            step_id=step.id,
            step_key=step.step_key,
            prompt=prompt,
            subscription_id=step.resolved_subscription_id,
            pending_action=pending_action,
        )
    except Exception:
        logger.warning("dispatcher: chat notify (needs_human) failed", exc_info=True)


# ── Workflow signaling (M3.8 — Temporal opt-in) ──────────────────────
# These no-op when TEMPORAL_ENABLED=false, so the legacy Celery path
# is unaffected. When enabled, the Dispatcher pushes step lifecycle
# updates to the per-plan workflow which is awaiting them via
# wait_condition. Imports are deferred to keep temporalio out of the
# import graph for non-Temporal deployments.


async def _safe_signal_workflow_done(
    step: ExecutionStep,
    result: Optional[dict],
) -> None:
    try:
        from packages.core.temporal_app import signal_step_completed

        await signal_step_completed(step.plan_id, step.step_key, result)
    except Exception:
        logger.debug("dispatcher: workflow signal (done) skipped", exc_info=True)


async def _safe_signal_workflow_failed(
    step: ExecutionStep,
    error: dict,
) -> None:
    try:
        from packages.core.temporal_app import signal_step_failed

        await signal_step_failed(step.plan_id, step.step_key, error)
    except Exception:
        logger.debug("dispatcher: workflow signal (failed) skipped", exc_info=True)
