"""Worker CRUD + auth + subscription binding.

Uses the ``bcrypt`` library directly rather than ``passlib.hash.bcrypt``
because passlib 1.7's bcrypt-4.x compatibility shim is broken on
Python 3.13+ (bcrypt removed the legacy ``__about__`` attribute).
``secrets.token_urlsafe(32)`` is well under bcrypt's 72-byte limit, so
no pre-hash needed.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import AgentSubscription
from packages.core.models.worker import (
    SubscriptionWorker,
    Worker,
    WorkerActivityLog,
)

logger = logging.getLogger(__name__)


INTERNAL_WORKER_KIND = "internal"
"""Reserved kind for the always-on, in-process worker."""


DEFAULT_INTERNAL_CAPABILITIES: dict[str, Any] = {
    "supported_kinds": ["llm", "action", "subagent", "sleep", "human"],
    "supported_providers": None,  # null = "all" — internal can call anything
    "max_concurrent_leases": 4,
    "max_risk_level": "high",
    "uses_manor_credentials": True,
    "deployment": "local",
    "protocol_version": 1,
}


# ── Internal worker bootstrap ─────────────────────────────────────────

async def ensure_internal_worker(
    db: AsyncSession, entity_id: str,
) -> Worker:
    """Get-or-create the entity's always-on internal worker. Caller commits.

    Idempotent. Bound to every active subscription on creation; later
    subscriptions get auto-bound by ``bind_subscription`` when they're
    introduced (the subscription_workers row is added on first dispatch
    if missing — handled by the dispatcher's matchmaker)."""
    existing = (await db.execute(
        select(Worker).where(
            Worker.entity_id == entity_id,
            Worker.kind == INTERNAL_WORKER_KIND,
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        if existing.status != "active":
            existing.status = "active"
        await _bind_active_subscriptions_to_worker(db, entity_id, existing.id)
        return existing

    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind=INTERNAL_WORKER_KIND,
        display_name="Manor Built-in",
        description=(
            "Always-on in-process worker. Wraps AgenticLoop + the "
            "MCP integration adapters. Created automatically per "
            "entity on first need; cannot be deleted."
        ),
        capabilities=dict(DEFAULT_INTERNAL_CAPABILITIES),
        secret_hash=None,           # internal doesn't authenticate over HTTP
        trust_level="high",
        status="active",
    )
    db.add(worker)
    await db.flush()

    # Auto-bind to every existing active subscription in the entity so
    # the dispatcher has a default executor immediately.
    await _bind_active_subscriptions_to_worker(db, entity_id, worker.id)

    db.add(WorkerActivityLog(
        worker_id=worker.id,
        event="register",
        payload_summary={"kind": INTERNAL_WORKER_KIND, "auto": True},
    ))
    await db.flush()
    return worker


async def _bind_active_subscriptions_to_worker(
    db: AsyncSession,
    entity_id: str,
    worker_id: str,
) -> None:
    """Ensure the internal worker can execute every active subscription."""
    subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    for sub in subs:
        await bind_subscription(db, sub.id, worker_id, is_preferred=True)


# ── External worker registration ──────────────────────────────────────

async def register_external_worker(
    db: AsyncSession,
    *,
    entity_id: str,
    kind: str,
    display_name: str,
    capabilities: dict,
    description: Optional[str] = None,
    version: Optional[str] = None,
    trust_level: str = "standard",
    allowed_ips: Optional[list[str]] = None,
    monthly_budget_usd: Optional[float] = None,
    expires_at: Optional[datetime] = None,
    created_by_user_id: Optional[str] = None,
    secret_ttl_days: int = 90,
) -> tuple[Worker, str]:
    """Register a new external worker. Returns (worker, plaintext_secret).

    The plaintext secret is only returned once — the caller (HTTP
    register endpoint) must hand it back to the operator. We store
    only the bcrypt hash. Caller commits.
    """
    if kind == INTERNAL_WORKER_KIND:
        raise ValueError("internal workers are bootstrapped via ensure_internal_worker")

    secret = secrets.token_urlsafe(32)
    secret_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()

    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind=kind,
        display_name=display_name,
        description=description,
        version=version,
        capabilities=capabilities,
        secret_hash=secret_hash,
        trust_level=trust_level,
        allowed_ips=allowed_ips,
        monthly_budget_usd=monthly_budget_usd,
        status="active",
        last_rotated_at=datetime.now(timezone.utc),
        created_by_user_id=created_by_user_id,
        expires_at=expires_at or (
            datetime.now(timezone.utc) + timedelta(days=secret_ttl_days)
        ),
    )
    db.add(worker)
    await db.flush()

    db.add(WorkerActivityLog(
        worker_id=worker.id,
        event="register",
        payload_summary={
            "kind": kind, "trust_level": trust_level,
            "expires_at": worker.expires_at.isoformat() if worker.expires_at else None,
        },
    ))
    await db.flush()

    return worker, secret


async def rotate_worker_secret(
    db: AsyncSession, worker_id: str, *, ttl_days: int = 90,
) -> str:
    """Issue a fresh secret for a worker. Returns plaintext (once)."""
    worker = (await db.execute(
        select(Worker).where(Worker.id == worker_id)
    )).scalar_one_or_none()
    if worker is None:
        raise ValueError(f"worker {worker_id} not found")
    if worker.kind == INTERNAL_WORKER_KIND:
        raise ValueError("internal workers don't have a secret to rotate")

    secret = secrets.token_urlsafe(32)
    worker.secret_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()
    worker.last_rotated_at = datetime.now(timezone.utc)
    worker.expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    db.add(WorkerActivityLog(
        worker_id=worker.id,
        event="rotate_secret",
        payload_summary={"new_expiry": worker.expires_at.isoformat()},
    ))
    await db.flush()
    return secret


def verify_worker_secret(worker: Worker, presented_secret: str) -> bool:
    """Constant-time check of a presented secret against the stored hash.

    Returns False for internal workers (no secret) — those cross the
    in-process boundary, never the HTTP one. Returns False for revoked
    workers so they can't reach the API at all.

    Paused and quarantined workers are allowed through so the heartbeat
    endpoint can return a ``pause`` instruction — workers shouldn't have
    to guess at why their hb is failing; they should hear "you're paused,
    back off" explicitly.
    """
    if not worker.secret_hash:
        return False
    if worker.status == "revoked":
        return False
    if worker.expires_at and worker.expires_at < datetime.now(timezone.utc):
        return False
    try:
        return bcrypt.checkpw(
            presented_secret.encode(), worker.secret_hash.encode(),
        )
    except Exception:
        return False


# ── Subscription binding ──────────────────────────────────────────────

async def bind_subscription(
    db: AsyncSession,
    subscription_id: str,
    worker_id: str,
    *,
    priority: int = 100,
    is_preferred: bool = False,
) -> SubscriptionWorker:
    """Idempotent (subscription_id, worker_id) binding. Caller commits."""
    existing = (await db.execute(
        select(SubscriptionWorker).where(
            SubscriptionWorker.subscription_id == subscription_id,
            SubscriptionWorker.worker_id == worker_id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.priority = priority
        existing.is_preferred = is_preferred
        await db.flush()
        return existing

    row = SubscriptionWorker(
        subscription_id=subscription_id,
        worker_id=worker_id,
        priority=priority,
        is_preferred=is_preferred,
    )
    db.add(row)
    await db.flush()
    return row


async def list_workers_for_subscription(
    db: AsyncSession, subscription_id: str,
) -> list[Worker]:
    """All active workers bound to a subscription, ordered by
    (is_preferred desc, priority asc)."""
    rows = (await db.execute(
        select(Worker, SubscriptionWorker)
        .join(SubscriptionWorker, SubscriptionWorker.worker_id == Worker.id)
        .where(
            SubscriptionWorker.subscription_id == subscription_id,
            Worker.status == "active",
        )
        .order_by(desc(SubscriptionWorker.is_preferred), SubscriptionWorker.priority)
    )).all()
    return [w for (w, _) in rows]


# ── Lookups + state ─────────────────────────────────────────────────

async def get_worker(db: AsyncSession, worker_id: str) -> Optional[Worker]:
    return (await db.execute(
        select(Worker).where(Worker.id == worker_id)
    )).scalar_one_or_none()


async def update_worker_status(
    db: AsyncSession, worker_id: str, status: str,
    *, reason: Optional[str] = None,
) -> Optional[Worker]:
    """Pause / resume / quarantine / revoke. Logs to worker_activity_log."""
    worker = await get_worker(db, worker_id)
    if worker is None:
        return None
    if status not in {"pairing", "active", "paused", "offline", "revoked", "quarantined"}:
        raise ValueError(f"unknown worker status {status!r}")

    worker.status = status
    db.add(WorkerActivityLog(
        worker_id=worker.id,
        event=status,
        payload_summary={"reason": reason} if reason else None,
    ))
    await db.flush()
    return worker
