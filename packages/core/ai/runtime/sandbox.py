from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


RUNTIME_SANDBOX_CONTEXT_PREFIX = "sandbox:ctx:"
RUNTIME_SANDBOX_CONTEXT_TTL = 3600
RUNTIME_SANDBOX_IDLE_THRESHOLD = 30


async def runtime_save_sandbox_context(conversation_id: str, ctx: dict[str, Any]) -> None:
    """Persist Runtime-owned sandbox conversation context."""

    if not conversation_id:
        return
    try:
        from packages.core.cache import cache

        await cache.set(
            f"{RUNTIME_SANDBOX_CONTEXT_PREFIX}{conversation_id}",
            ctx,
            ttl=RUNTIME_SANDBOX_CONTEXT_TTL,
        )
    except Exception as exc:
        logger.debug("[runtime.sandbox] context save failed: %s", exc)


async def runtime_load_sandbox_context(conversation_id: str) -> dict[str, Any] | None:
    """Load Runtime-owned sandbox conversation context."""

    if not conversation_id:
        return None
    try:
        from packages.core.cache import cache

        return await cache.get(f"{RUNTIME_SANDBOX_CONTEXT_PREFIX}{conversation_id}")
    except Exception:
        return None


async def runtime_delete_sandbox_context(conversation_id: str) -> None:
    """Delete Runtime-owned sandbox conversation context."""

    if not conversation_id:
        return
    try:
        from packages.core.cache import cache

        await cache.delete(f"{RUNTIME_SANDBOX_CONTEXT_PREFIX}{conversation_id}")
    except Exception:
        pass


async def runtime_init_sandbox_context(
    conversation_id: str,
    sandbox_id: str,
    skill_id: str,
) -> dict[str, Any]:
    """Initialize Runtime-owned sandbox conversation context."""

    ctx: dict[str, Any] = {
        "sandbox_id": sandbox_id,
        "skill_id": skill_id,
        "created_at": time.time(),
        "exec_history": [],
    }
    await runtime_save_sandbox_context(conversation_id, ctx)
    return ctx
