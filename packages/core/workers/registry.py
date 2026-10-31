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
from typing import Optional

import bcrypt
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    WorkerStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.worker import (
    SubscriptionWorker,
    Worker,
    WorkerActivityLog,
)
from packages.core.workers.protocol import (
    require_current_worker_protocol,
)

logger = logging.getLogger(__name__)


INTERNAL_WORKER_KIND = "internal"
"""Kind of the retired in-process Plan Step worker; its rows never authenticate externally."""

STALE_EXTERNAL_WORKER_SECONDS = 300


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
        raise ValueError("the internal worker kind is reserved")
    require_current_worker_protocol(capabilities)

    secret = secrets.token_urlsafe(32)
    secret_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()

    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind=kind,
        display_name=display_name,
        description=description,
        version=version,
        capabilities=dict(capabilities),
        secret_hash=secret_hash,
        trust_level=trust_level,
        allowed_ips=allowed_ips,
        monthly_budget_usd=monthly_budget_usd,
        status=WorkerStatus.ACTIVE.value,
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


def _worker_secret_is_usable(worker: Worker) -> bool:
    return bool(
        worker.secret_hash
        and worker.status != WorkerStatus.REVOKED
        and (not worker.expires_at or worker.expires_at >= datetime.now(timezone.utc))
    )


def _verify_secret_hash(secret_hash: str, presented_secret: str) -> bool:
    try:
        return bcrypt.checkpw(presented_secret.encode(), secret_hash.encode())
    except (ValueError, TypeError):
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
            Worker.status == WorkerStatus.ACTIVE,
        )
        .order_by(desc(SubscriptionWorker.is_preferred), SubscriptionWorker.priority)
    )).all()
    return [w for (w, _) in rows]


# ── Lookups + state ─────────────────────────────────────────────────

async def get_worker(db: AsyncSession, worker_id: str, *, fresh: bool = False) -> Optional[Worker]:
    return (await db.execute(
        select(Worker).where(Worker.id == worker_id)
        .execution_options(populate_existing=fresh)
    )).scalar_one_or_none()


async def update_worker_status(
    db: AsyncSession, worker_id: str, status: str,
    *, reason: Optional[str] = None,
) -> Optional[Worker]:
    """Pause / resume / quarantine / revoke. Logs to worker_activity_log."""
    worker = await get_worker(db, worker_id)
    if worker is None:
        return None
    if status not in WorkerStatus.values():
        raise ValueError(f"unknown worker status {status!r}")

    worker.status = status
    db.add(WorkerActivityLog(
        worker_id=worker.id,
        event=status,
        payload_summary={"reason": reason} if reason else None,
    ))
    await db.flush()
    return worker


async def mark_stale_external_workers_offline(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_EXTERNAL_WORKER_SECONDS,
    limit: int = 100,
) -> int:
    """Project missing external heartbeats into an inspectable offline state."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(seconds=max(int(stale_after_seconds), 1))
    workers = list((await db.execute(
        select(Worker)
        .where(
            Worker.kind != INTERNAL_WORKER_KIND,
            Worker.status == WorkerStatus.ACTIVE.value,
            or_(
                Worker.last_heartbeat_at < cutoff,
                and_(
                    Worker.last_heartbeat_at.is_(None),
                    Worker.created_at < cutoff,
                ),
            ),
        )
        .order_by(Worker.last_heartbeat_at.asc().nullsfirst(), Worker.id.asc())
        .limit(max(int(limit), 1))
        .with_for_update(skip_locked=True)
    )).scalars().all())
    for worker in workers:
        worker.status = WorkerStatus.OFFLINE.value
        db.add(WorkerActivityLog(
            worker_id=worker.id,
            event=WorkerStatus.OFFLINE.value,
            payload_summary={"reason": "heartbeat_stale"},
        ))
    await db.flush()
    return len(workers)
