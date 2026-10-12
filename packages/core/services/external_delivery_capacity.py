"""Process-local provider admission shared by durable event and notification queues."""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession


def external_delivery_semaphore(db: AsyncSession | None = None) -> asyncio.Semaphore:
    # Match the actual session's business admission policy. The event outbox
    # owns its sessions through the default factory; notifications may use a
    # separate worker engine with a different budget.
    if db is None:
        from packages.core.database import engine
        bind = engine
    else:
        bind = db.get_bind()
    bind = getattr(bind, "sync_engine", bind)
    runtime = getattr(getattr(bind, "engine", bind), "_manor_database_runtime", None)
    policy = runtime.policy if runtime is not None else None
    limit = min(2, max(1, policy.limit - 1)) if policy is not None else 2
    loop = asyncio.get_running_loop()
    gates = getattr(loop, "_manor_external_delivery_capacity", None)
    if gates is None:
        gates = {}
        loop._manor_external_delivery_capacity = gates
    if policy not in gates:
        gates[policy] = asyncio.Semaphore(limit)
    return gates[policy]
