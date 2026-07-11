"""Celery worker runtime helpers.

Celery uses a prefork pool — each worker is a forked process. Inside a
single worker, every task wraps its coroutine in ``asyncio.run(...)``
which creates a **fresh** event loop per invocation. That's fine for
stateless code, but SQLAlchemy's async engine caches connections from
the PRIOR loop; the next task tries to reuse them and asyncpg blows up:

    RuntimeError: Event loop is closed

Fix: dispose the engine's pool at the start AND end of every task so
each task only ever uses connections bound to its own (current) event
loop. ``engine.dispose()`` is async, so we have to run it inside the
wrapper coroutine — we can't call it before ``asyncio.run()``.

Use ``run_in_worker(coro)`` from every Celery task that awaits DB work.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable


def run_in_worker(coro: Awaitable[Any]) -> Any:
    """Run an async coroutine from a synchronous Celery worker safely.

    Wraps the coroutine in an outer async function that disposes the
    module-level SQLAlchemy engine's pool before and after the work.
    That invalidates any connections held over from a previous task's
    event loop (which is now closed) and cleans up any we created
    before the current loop closes.
    """
    async def _wrapped() -> Any:
        from packages.core.database import engine
        # Detach stale pooled connections from earlier task loops without
        # awaiting asyncpg close calls on the new loop.
        await engine.dispose(close=False)
        try:
            return await coro
        finally:
            # Close anything we opened before the loop is torn down.
            await engine.dispose()

    return asyncio.run(_wrapped())
