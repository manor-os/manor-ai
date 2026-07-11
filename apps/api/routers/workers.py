"""Worker HTTP API — registration, heartbeat, lease lifecycle, admin.

Two audiences hit this router:

  * **Operators** (auth = User JWT): create / pause / resume / revoke
    workers via the admin endpoints. The register endpoint mints a
    one-time worker secret which they paste into their external
    worker's config.

  * **Workers** (auth = Bearer worker_secret + ``Manor-Worker-Id``
    header): self-deregister, rotate secret, heartbeat (the hot path),
    and report lease outcomes.

Internal workers never touch this router — they reach the dispatcher
through ``packages.core.workers.internal.tick_one_internal_worker``
inside the Celery process.

Endpoint groups:
  POST   /api/v1/workers/register                 admin (User JWT)
  GET    /api/v1/workers                          admin
  GET    /api/v1/workers/{id}                     admin
  POST   /api/v1/workers/{id}/pause               admin
  POST   /api/v1/workers/{id}/resume              admin
  POST   /api/v1/workers/{id}/revoke              admin
  POST   /api/v1/workers/{id}/browser-mcp         admin
  GET    /api/v1/workers/{id}/activity            admin

  POST   /api/v1/workers/heartbeat                worker
  POST   /api/v1/workers/me/deregister            worker
  POST   /api/v1/workers/me/rotate-secret         worker
  POST   /api/v1/workers/leases/{id}/complete     worker
  POST   /api/v1/workers/leases/{id}/fail         worker
  POST   /api/v1/workers/leases/{id}/need-human   worker
  POST   /api/v1/workers/leases/{id}/extend       worker
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, get_current_worker
from packages.core.database import get_db
from packages.core.dispatcher import (
    Dispatcher,
    DispatchError,
    LeaseNotActive,
)
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.user import User
from packages.core.models.worker import (
    Worker,
    WorkLease,
    WorkerActivityLog,
)
from packages.core.workers import (
    INTERNAL_WORKER_KIND,
    register_external_worker,
    rotate_worker_secret,
    update_worker_status,
)


router = APIRouter(prefix="/api/v1/workers", tags=["workers"])




# ── Schemas ────────────────────────────────────────────────────────────

class WorkerCapabilities(BaseModel):
    supported_kinds: list[Literal["llm", "action", "subagent", "code", "sleep", "human"]] = Field(default_factory=list)
    supported_providers: Optional[list[str]] = None
    """None = all providers; explicit empty list = none."""
    supported_capabilities: Optional[list[str]] = None
    """None = all runtime capabilities; explicit empty list = none."""
    max_concurrent_leases: int = Field(default=1, ge=1, le=64)
    max_risk_level: Literal["low", "medium", "high"] = "low"
    uses_manor_credentials: bool = True
    deployment: Literal["local", "remote", "cloud"] = "local"
    protocol_version: int = 1


class RegisterRequest(BaseModel):
    kind: Literal[
        "openclaw",
        "paperclip_bridge",
        "custom_http",
        "shell_script",
        "mcp_reverse",
    ]
    """``internal`` is reserved for the bootstrap path and not allowed here."""
    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    version: Optional[str] = None
    capabilities: WorkerCapabilities
    trust_level: Literal["high", "standard", "low"] = "standard"
    allowed_ips: Optional[list[str]] = None
    monthly_budget_usd: Optional[float] = Field(default=None, ge=0)
    expires_at: Optional[datetime] = None


class RegisterResponse(BaseModel):
    worker_id: str
    worker_secret: str
    """Plaintext secret — shown ONCE. Worker must store it; we only keep
    the bcrypt hash."""
    expires_at: Optional[datetime]
    heartbeat_endpoint: str = "/api/v1/workers/heartbeat"
    next_heartbeat_in_seconds: int = 2


class WorkerResponse(BaseModel):
    id: str
    entity_id: str
    kind: str
    display_name: str
    description: Optional[str]
    version: Optional[str]
    capabilities: dict
    trust_level: str
    status: str
    last_heartbeat_at: Optional[datetime]
    last_seen_ip: Optional[str]
    consecutive_failures: int
    monthly_budget_usd: Optional[float]
    monthly_spent_usd: float
    expires_at: Optional[datetime]
    created_at: datetime
    updated_at: Optional[datetime]


# ── Heartbeat schemas ────────────────────────────────────────────────

class HeartbeatActiveLease(BaseModel):
    lease_id: str
    progress: Optional[float] = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class HeartbeatCompletedLease(BaseModel):
    lease_id: str
    status: Literal["done", "failed"]
    result: Optional[dict] = None
    error: Optional[dict] = None
    cost: Optional[dict] = None
    evidence_refs: Optional[list[str]] = None


class HeartbeatCapacity(BaseModel):
    can_accept_leases: int = Field(default=0, ge=0, le=32)
    filters: Optional[dict] = None


class HeartbeatRequest(BaseModel):
    state: Literal["idle", "busy", "shutting_down"] = "idle"
    version: Optional[str] = None
    timestamp: Optional[datetime] = None
    budget_remaining_usd: Optional[float] = None
    active_leases: list[HeartbeatActiveLease] = Field(default_factory=list)
    completed_since_last: list[HeartbeatCompletedLease] = Field(default_factory=list)
    capacity: HeartbeatCapacity = Field(default_factory=HeartbeatCapacity)
    capabilities: Optional[dict] = None


class LeaseDTO(BaseModel):
    lease_id: str
    step_id: str
    plan_id: str
    workspace_id: Optional[str]
    subscription_id: Optional[str] = None
    service_key: Optional[str] = None
    agent: Optional[dict] = None
    bindings: dict[str, Any] = Field(default_factory=dict)
    kind: str
    provider: Optional[str]
    action_key: Optional[str]
    capability_id: Optional[str] = None
    integration_id: Optional[str]
    params: dict
    expected_input_schema: Optional[dict]
    expected_output_schema: Optional[dict]
    risk_level: str
    lease_until: datetime
    budget_limit_usd: Optional[float]
    execution_mode: str


class HeartbeatInstruction(BaseModel):
    type: str
    payload: Optional[dict] = None


class HeartbeatResponse(BaseModel):
    server_time: datetime
    next_heartbeat_in_seconds: int
    new_leases: list[LeaseDTO] = Field(default_factory=list)
    instructions: list[HeartbeatInstruction] = Field(default_factory=list)


# ── Lease lifecycle schemas ──────────────────────────────────────────

class CompleteLeaseRequest(BaseModel):
    result: Optional[dict] = None
    cost: Optional[dict] = None
    evidence_refs: Optional[list[str]] = None


class FailLeaseRequest(BaseModel):
    error: dict
    will_retry: Optional[bool] = None


class NeedHumanRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)


class ExtendLeaseRequest(BaseModel):
    extra_seconds: float = Field(default=300, gt=0, le=3600)
    progress: Optional[float] = Field(default=None, ge=0, le=1)


class ActivityResponse(BaseModel):
    event: str
    lease_id: Optional[str]
    occurred_at: datetime
    payload_summary: Optional[dict]
    ip: Optional[str]




def _merge_worker_capabilities(
    stored: dict | None,
    reported: dict | None,
    *,
    worker_id: str | None = None,
) -> dict:
    """Merge heartbeat capabilities without erasing just-connected sessions.

    Older local worker builds probe browser sessions only once at startup.
    After a
    user connects a local browser session from Integrations, the API writes the
    connected platform into worker.capabilities immediately; the next heartbeat
    from an older daemon can otherwise shallow-merge an empty browser session
    list over that fresh state.
    """
    merged = dict(stored or {})
    incoming = dict(reported or {})
    for key, value in incoming.items():
        if key != "browser":
            merged[key] = value

    if "browser" not in incoming:
        return merged

    current_browser = (
        dict(merged.get("browser") or {})
        if isinstance(merged.get("browser"), dict)
        else {}
    )
    reported_browser = (
        dict(incoming.get("browser") or {})
        if isinstance(incoming.get("browser"), dict)
        else {}
    )
    browser = dict(current_browser)
    browser.update(reported_browser)

    current_statuses = (
        dict(current_browser.get("session_statuses") or {})
        if isinstance(current_browser.get("session_statuses"), dict)
        else {}
    )
    reported_statuses = (
        dict(reported_browser.get("session_statuses") or {})
        if isinstance(reported_browser.get("session_statuses"), dict)
        else {}
    )
    session_statuses = {**current_statuses, **reported_statuses}
    if session_statuses:
        browser["session_statuses"] = session_statuses

    saved_sessions_authoritative = reported_browser.get("saved_sessions_authoritative") is True
    for key in ("saved_sessions", "active_sessions"):
        reported_sessions = _string_list(reported_browser.get(key))
        if reported_sessions:
            if key == "saved_sessions" and saved_sessions_authoritative:
                # Modern local workers report saved_sessions from the local cookie
                # files on every heartbeat. Treat that as the source of truth
                # so reconnecting after a prior explicit disconnect works.
                browser[key] = reported_sessions
                for platform in reported_sessions:
                    session_statuses[platform] = "connected"
                last_action = (
                    dict(browser.get("last_session_action") or {})
                    if isinstance(browser.get("last_session_action"), dict)
                    else {}
                )
                if (
                    last_action.get("platform") in reported_sessions
                    and last_action.get("status") == "disconnected"
                ):
                    browser["last_session_action"] = {
                        **last_action,
                        "status": "connected",
                    }
            else:
                # For non-authoritative or active-session lists, do not let a
                # stale heartbeat resurrect a platform that a stop_session
                # action has already marked disconnected.
                browser[key] = [
                    platform
                    for platform in reported_sessions
                    if session_statuses.get(platform) != "disconnected"
                ]
            continue
        if key == "saved_sessions" and saved_sessions_authoritative:
            browser[key] = []
            continue
        if key in reported_browser:
            preserved = [
                platform
                for platform in _string_list(current_browser.get(key))
                if session_statuses.get(platform) == "connected"
            ]
            browser[key] = preserved

    disconnected = {
        platform
        for platform, status in session_statuses.items()
        if status == "disconnected"
    }
    if disconnected:
        for key in ("saved_sessions", "active_sessions"):
            if key in browser:
                browser[key] = [
                    platform
                    for platform in _string_list(browser.get(key))
                    if platform not in disconnected
                ]
    if session_statuses:
        browser["session_statuses"] = session_statuses

    gateway = browser.get("gateway")
    if isinstance(gateway, dict):
        gateway = dict(gateway)
        browser["gateway"] = gateway

    merged["browser"] = browser
    return merged


def _sync_worker_runtime_version(
    capabilities: dict | None,
    version: str | None,
    *,
    stored_version: str | None = None,
) -> dict:
    synced = dict(capabilities or {})
    normalized_version = str(version or stored_version or "").strip()
    if not normalized_version:
        return synced
    runtime = synced.get("runtime")
    if isinstance(runtime, dict):
        synced["runtime"] = {**runtime, "version": normalized_version}
    return synced


def _should_merge_heartbeat_capabilities(
    req: HeartbeatRequest,
    *,
    worker_status: str,
) -> bool:
    if not req.capabilities:
        return False
    daemon = req.capabilities.get("daemon")
    if not isinstance(daemon, dict):
        return True
    is_oneshot_probe = (
        daemon.get("running") is False
        and req.capacity.can_accept_leases == 0
        and not req.active_leases
        and not req.completed_since_last
    )
    if worker_status == "active" and is_oneshot_probe:
        return False
    return True


async def _serialize_lease_for_worker(
    db: AsyncSession,
    lease: WorkLease,
    step: ExecutionStep,
) -> LeaseDTO:
    # plan.execution_mode tells the worker whether it should call
    # adapter.simulate_tool or adapter.call_tool.
    from packages.core.models.execution import ExecutionPlan
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == lease.plan_id)
    )).scalar_one()
    subscription_id = lease.subscription_id or step.resolved_subscription_id
    service_key = step.service_key
    agent_ctx: dict[str, Any] | None = None
    bindings: dict[str, Any] = {"tools": [], "skills": []}
    if subscription_id:
        from packages.core.models.skill import AgentSkillBinding, Skill
        from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition

        sub = (
            await db.execute(
                select(AgentSubscription).where(AgentSubscription.id == subscription_id)
            )
        ).scalar_one_or_none()
        if sub:
            service_key = sub.service_key or service_key
            agent = (
                await db.execute(select(Agent).where(Agent.id == sub.agent_id))
            ).scalar_one_or_none()
            if agent:
                agent_ctx = {
                    "id": agent.id,
                    "name": agent.name,
                    "description": agent.description,
                    "system_prompt": agent.system_prompt,
                    "config": agent.config or {},
                    "source": agent.source,
                    "category": agent.category,
                    "subscription_id": sub.id,
                    "service_key": sub.service_key,
                    "custom_prompt": sub.custom_prompt,
                }
                tool_rows = (
                    await db.execute(
                        select(ToolDefinition)
                        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
                        .where(
                            AgentToolBinding.agent_id == agent.id,
                            ToolDefinition.status == "active",
                        )
                    )
                ).scalars().all()
                bindings["tools"] = [
                    {
                        "id": t.id,
                        "name": t.name,
                        "display_name": t.display_name,
                        "description": t.description,
                        "category": t.category,
                    }
                    for t in tool_rows
                ]
                skill_rows = (
                    await db.execute(
                        select(Skill)
                        .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
                        .where(
                            AgentSkillBinding.agent_id == agent.id,
                            AgentSkillBinding.status == "active",
                            Skill.status == "active",
                        )
                    )
                ).scalars().all()
                bindings["skills"] = [
                    {
                        "id": s.id,
                        "slug": s.slug,
                        "name": s.name,
                        "display_name": s.display_name,
                        "description": s.description,
                        "category": s.category,
                    }
                    for s in skill_rows
                ]
    return _build_lease_dto_for_worker(
        lease=lease,
        step=step,
        plan=plan,
        subscription_id=subscription_id,
        service_key=service_key,
        agent_ctx=agent_ctx,
        bindings=bindings,
    )


def _build_lease_dto_for_worker(
    *,
    lease: Any,
    step: Any,
    plan: Any,
    subscription_id: Optional[str] = None,
    service_key: Optional[str] = None,
    agent_ctx: Optional[dict[str, Any]] = None,
    bindings: Optional[dict[str, Any]] = None,
) -> LeaseDTO:
    """Build the heartbeat LeaseDTO shape consumed by external workers.


    Kept separate from DB loading so the AI tool -> local action -> worker
    lease protocol can be regression-tested without a database.
    """
    params = dict(getattr(step, "params", None) or {})
    human_input_response = getattr(step, "human_input_response", None)
    if human_input_response is not None:
        params["human_input_response"] = human_input_response
    resolved_subscription_id = (
        subscription_id
        or getattr(lease, "subscription_id", None)
        or getattr(step, "resolved_subscription_id", None)
    )
    resolved_service_key = service_key or getattr(step, "service_key", None)
    resolved_bindings = bindings if bindings is not None else {"tools": [], "skills": []}
    return LeaseDTO(
        lease_id=lease.id,
        step_id=step.id,
        plan_id=step.plan_id,
        workspace_id=step.workspace_id,
        subscription_id=resolved_subscription_id,
        service_key=resolved_service_key,
        agent=agent_ctx,
        bindings=resolved_bindings,
        kind=step.kind,
        provider=step.provider,
        action_key=step.action_key,
        capability_id=step.capability_id,
        integration_id=step.integration_id,
        params=params,
        expected_input_schema=step.expected_input_schema,
        expected_output_schema=step.expected_output_schema,
        risk_level=step.risk_level,
        lease_until=lease.lease_until,
        budget_limit_usd=(
            float(lease.budget_limit_usd) if lease.budget_limit_usd is not None else None
        ),
        execution_mode=plan.execution_mode,
    )


async def _ensure_lease_belongs_to_worker(
    db: AsyncSession, lease_id: str, worker: Worker,
) -> WorkLease:
    """Authorisation guard for lease lifecycle endpoints — a worker
    can only mutate its own leases."""
    lease = (await db.execute(
        select(WorkLease).where(WorkLease.id == lease_id)
    )).scalar_one_or_none()
    if lease is None:
        raise HTTPException(404, f"lease {lease_id} not found")
    if lease.worker_id != worker.id:
        raise HTTPException(
            403, f"lease {lease_id} not held by worker {worker.id}",
        )
    return lease


# ── Admin endpoints (User auth) ──────────────────────────────────────



@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_worker(
    req: RegisterRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Operator creates an external worker. Returns the secret ONCE —
    the worker must store it (we only keep the bcrypt hash)."""
    worker, secret = await register_external_worker(
        db,
        entity_id=user.entity_id,
        kind=req.kind,
        display_name=req.display_name,
        description=req.description,
        version=req.version,
        capabilities=req.capabilities.model_dump(),
        trust_level=req.trust_level,
        allowed_ips=req.allowed_ips,
        monthly_budget_usd=req.monthly_budget_usd,
        expires_at=req.expires_at,
        created_by_user_id=user.id,
    )
    await db.commit()
    return RegisterResponse(
        worker_id=worker.id,
        worker_secret=secret,
        expires_at=worker.expires_at,
    )


@router.get("", response_model=list[WorkerResponse])
async def list_workers(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Worker).where(Worker.entity_id == user.entity_id)
    if status:
        stmt = stmt.where(Worker.status == status)
    if kind:
        stmt = stmt.where(Worker.kind == kind)
        if kind == "custom_http":
            stmt = stmt.where(Worker.created_by_user_id == user.id)
    else:
        stmt = stmt.where(
            (Worker.kind != "custom_http") | (Worker.created_by_user_id == user.id)
        )
    stmt = stmt.order_by(Worker.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_worker_response(w) for w in rows]



@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker_detail(
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    w = (await db.execute(
        _user_worker_scope(select(Worker), user).where(
            Worker.id == worker_id,
        )
    )).scalar_one_or_none()
    if w is None:
        raise HTTPException(404, "worker not found")
    return _to_worker_response(w)


async def _admin_set_status(
    db: AsyncSession,
    worker_id: str,
    user: User,
    new_status: str,
    *,
    block_internal: bool,
    extra_mutations=None,
) -> WorkerResponse:
    """Shared body for pause / resume / revoke.

    We inline the status flip + activity log write rather than calling
    the registry helper because the registry's two-step
    ``get_worker → mutate → flush`` confuses asyncpg's prepared-statement
    cache when the same session subsequently does a SELECT … RETURNING
    on the activity log row, raising ``MissingGreenlet`` from a sync
    code path. Inlining keeps the whole flow in one async transaction
    and one prepared-statement scope.
    """
    w = (await db.execute(
        _user_worker_scope(select(Worker), user).where(Worker.id == worker_id)
    )).scalar_one_or_none()
    if w is None:
        raise HTTPException(404, "worker not found")
    if block_internal and w.kind == INTERNAL_WORKER_KIND:
        raise HTTPException(409, f"internal workers cannot be {new_status}")

    w.status = new_status
    if extra_mutations:
        extra_mutations(w)

    db.add(WorkerActivityLog(
        worker_id=w.id,
        event=new_status,
        payload_summary={"reason": "admin"},
    ))

    # Capture response BEFORE commit — after commit asyncpg may close
    # the prepared statement cache and any later attribute load on w
    # would need a fresh checkout from the pool, which lands in a
    # sync context here.
    resp = _to_worker_response(w)
    await db.commit()
    return resp


@router.post("/{worker_id}/pause", response_model=WorkerResponse)
async def pause_worker(
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _admin_set_status(
        db, worker_id, user, "paused", block_internal=True,
    )


@router.post("/{worker_id}/resume", response_model=WorkerResponse)
async def resume_worker(
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    def _reset_failures(w: Worker) -> None:
        # Resuming a worker also resets its consecutive_failures so
        # one bad day doesn't permanently shadow a healthy worker.
        w.consecutive_failures = 0

    return await _admin_set_status(
        db, worker_id, user, "active",
        block_internal=False, extra_mutations=_reset_failures,
    )


@router.post("/{worker_id}/revoke", response_model=WorkerResponse)
async def revoke_worker(
    worker_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _admin_set_status(
        db, worker_id, user, "revoked", block_internal=True,
    )


@router.get("/{worker_id}/activity", response_model=list[ActivityResponse])
async def get_worker_activity(
    worker_id: str,
    limit: int = Query(default=50, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Confirm worker belongs to caller's entity before exposing audit.
    owned = (await db.execute(
        _user_worker_scope(select(Worker.id), user).where(Worker.id == worker_id)
    )).scalar_one_or_none()
    if owned is None:
        raise HTTPException(404, "worker not found")

    rows = (await db.execute(
        select(WorkerActivityLog)
        .where(WorkerActivityLog.worker_id == worker_id)
        .order_by(desc(WorkerActivityLog.occurred_at))
        .limit(limit)
    )).scalars().all()

    return [
        ActivityResponse(
            event=r.event,
            lease_id=r.lease_id,
            occurred_at=r.occurred_at,
            payload_summary=r.payload_summary,
            ip=r.ip,
        )
        for r in rows
    ]


# ── Worker self-management (worker auth) ─────────────────────────────

@router.post("/me/deregister", status_code=204)
async def deregister_self(
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    """Worker shutting down cleanly. Soft-disable so admin can review
    activity log; full deletion is admin-only via revoke."""
    if worker.kind == INTERNAL_WORKER_KIND:
        raise HTTPException(409, "internal workers cannot deregister via HTTP")
    await update_worker_status(db, worker.id, "offline", reason="self_deregister")
    await db.commit()


@router.post("/me/rotate-secret")
async def rotate_self_secret(
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    new_secret = await rotate_worker_secret(db, worker.id)
    await db.commit()
    return {
        "worker_id": worker.id,
        "worker_secret": new_secret,
        "expires_at": worker.expires_at,
    }


# ── Heartbeat (the hot path) ─────────────────────────────────────────

@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    req: HeartbeatRequest,
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    """One-shot full lifecycle pass for an external worker:

      1. Drain ``completed_since_last`` — any leases the worker
         finished between ticks land via complete_lease / fail_lease.
      2. Renew ``active_leases`` — extend lease_until + record progress.
      3. Update worker stats (heartbeat_at, ip).
      4. Checkout up to ``capacity.can_accept_leases`` new leases.
      5. Surface any out-of-band instructions (pause / shutdown).
    """
    from packages.core.observability import span
    async with span("worker.heartbeat", attributes={
        "worker.id": worker.id,
        "worker.kind": worker.kind,
        "completed_count": len(req.completed_since_last),
        "active_count": len(req.active_leases),
        "capacity": req.capacity.can_accept_leases,
    }):
        return await _heartbeat_inner(req, worker, db)


async def _heartbeat_inner(
    req: HeartbeatRequest,
    worker: Worker,
    db: AsyncSession,
):
    dispatcher = Dispatcher()
    now = datetime.now(timezone.utc)
    instructions: list[HeartbeatInstruction] = []

    # Phase 1 — drain completions. Each one is independent; tolerate
    # already-terminated leases (worker may have retried over a flaky
    # network and we already processed it).
    for c in req.completed_since_last:
        try:
            if c.status == "done":
                completed_lease = await dispatcher.complete_lease(
                    db, c.lease_id,
                    result=c.result,
                    cost=c.cost,
                    evidence_refs=c.evidence_refs,
                )
            else:
                failed_lease = await dispatcher.fail_lease(
                    db, c.lease_id,
                    error=c.error or {"type": "WorkerReportedFailure", "message": "no detail"},
                )
        except LeaseNotActive:
            # Already terminal — ignore.
            pass
        except DispatchError:
            pass

    # Phase 2 — extend in-flight leases.
    for al in req.active_leases:
        active_row = (await db.execute(
            select(WorkLease, ExecutionStep)
            .join(ExecutionStep, ExecutionStep.id == WorkLease.step_id)
            .where(
                WorkLease.id == al.lease_id,
                WorkLease.worker_id == worker.id,
            )
        )).first()
        if active_row is not None:
            lease_row, step_row = active_row
            if step_row.step_status in {"cancelled", "canceled"}:
                if lease_row.status == "active":
                    lease_row.status = "cancelled"
                    lease_row.error = {
                        "type": "LeaseCancelled",
                        "message": "The plan or step was cancelled while the local CLI task was running.",
                    }
                    step_row.current_lease_id = None
                    db.add(WorkerActivityLog(
                        worker_id=worker.id,
                        event="lease_cancel_requested",
                        lease_id=al.lease_id,
                        payload_summary={"step_id": step_row.id, "step_key": step_row.step_key},
                    ))
                instructions.append(HeartbeatInstruction(
                    type="cancel_lease",
                    payload={"lease_id": al.lease_id, "reason": "step_cancelled"},
                ))
                continue
        try:
            await dispatcher.extend_lease(
                db, al.lease_id, progress=al.progress,
            )
        except (LeaseNotActive, DispatchError):
            pass

    # Phase 3 — worker bookkeeping.
    worker.last_heartbeat_at = now
    if req.version:
        worker.version = req.version
    if worker.status == "pairing":
        worker.status = "active"
        db.add(WorkerActivityLog(
            worker_id=worker.id,
            event="paired",
            payload_summary={"source": "heartbeat"},
            ip=None,
        ))
    if hasattr(worker, "consecutive_failures") and worker.status == "active":
        # Workers that successfully heartbeat have not failed catastrophically.
        # Don't reset counter here — leave that to explicit operator action.
        pass
    # Merge capabilities reported by the daemon into the stored worker record.
    # This keeps DB capabilities in sync with what's actually installed on the
    # machine (Browser Use, coding CLIs, etc.) without requiring a re-pair.
    if _should_merge_heartbeat_capabilities(req, worker_status=worker.status):
        worker.capabilities = _merge_worker_capabilities(
            worker.capabilities,
            req.capabilities,
            worker_id=worker.id,
        )
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(worker, "capabilities")
    worker.capabilities = _sync_worker_runtime_version(
        worker.capabilities,
        req.version,
        stored_version=worker.version,
    )
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(worker, "capabilities")

    # Phase 4 — checkout new leases.
    new_leases: list[LeaseDTO] = []
    if (
        worker.status == "active"
        and req.state != "shutting_down"
        and req.capacity.can_accept_leases > 0
    ):
        leased = await dispatcher.checkout_steps_for_worker(
            db, worker, max_n=req.capacity.can_accept_leases,
        )
        for lease, step in leased:
            new_leases.append(await _serialize_lease_for_worker(db, lease, step))

    # Phase 5 — out-of-band instructions.
    # "pause"    → worker should stop accepting leases but may be resumed later.
    # "shutdown" → worker should terminate completely (deregistered / offline).
    if worker.status == "paused":
        instructions.append(HeartbeatInstruction(
            type="pause", payload={"reason": "paused"},
        ))
    elif worker.status in ("offline", "quarantined"):
        instructions.append(HeartbeatInstruction(
            type="shutdown", payload={"reason": worker.status},
        ))

    await db.commit()

    next_in = int((worker.preferences or {}).get("heartbeat_interval_seconds", 2))
    return HeartbeatResponse(
        server_time=now,
        next_heartbeat_in_seconds=next_in,
        new_leases=new_leases,
        instructions=instructions,
    )


# ── Lease lifecycle (worker auth) ────────────────────────────────────

@router.post("/leases/{lease_id}/complete", status_code=204)
async def lease_complete(
    lease_id: str,
    req: CompleteLeaseRequest,
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_lease_belongs_to_worker(db, lease_id, worker)
    try:
        completed_lease = await Dispatcher().complete_lease(
            db, lease_id,
            result=req.result, cost=req.cost, evidence_refs=req.evidence_refs,
        )
    except LeaseNotActive as exc:
        raise HTTPException(409, str(exc))
    await db.commit()


@router.post("/leases/{lease_id}/fail", status_code=204)
async def lease_fail(
    lease_id: str,
    req: FailLeaseRequest,
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_lease_belongs_to_worker(db, lease_id, worker)
    try:
        failed_lease = await Dispatcher().fail_lease(
            db, lease_id, error=req.error, will_retry=req.will_retry,
        )
    except LeaseNotActive as exc:
        raise HTTPException(409, str(exc))
    await db.commit()


@router.post("/leases/{lease_id}/need-human", status_code=204)
async def lease_need_human(
    lease_id: str,
    req: NeedHumanRequest,
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_lease_belongs_to_worker(db, lease_id, worker)
    try:
        await Dispatcher().lease_needs_human(db, lease_id, prompt=req.prompt)
    except LeaseNotActive as exc:
        raise HTTPException(409, str(exc))
    await db.commit()


@router.post("/leases/{lease_id}/extend")
async def lease_extend(
    lease_id: str,
    req: ExtendLeaseRequest,
    worker: Worker = Depends(get_current_worker),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_lease_belongs_to_worker(db, lease_id, worker)
    try:
        lease = await Dispatcher().extend_lease(
            db, lease_id,
            extra_seconds=req.extra_seconds,
            progress=req.progress,
        )
    except LeaseNotActive as exc:
        raise HTTPException(409, str(exc))
    await db.commit()
    return {
        "lease_id": lease.id,
        "lease_until": lease.lease_until.isoformat(),
        "extended_count": lease.extended_count,
    }
