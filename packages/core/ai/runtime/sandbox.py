from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


RUNTIME_SANDBOX_CONTEXT_PREFIX = "sandbox:ctx:"
RUNTIME_SANDBOX_CONTEXT_TTL = 7 * 24 * 3600
RUNTIME_SANDBOX_IDLE_THRESHOLD = 30

_PERSISTED_PPTX_CONTEXT_KEYS = (
    "pptx_checkpoint_rel_path",
    "pptx_checkpoint_sha256",
    "pptx_checkpoint_size",
    "pptx_checkpoint_saved_at",
    "pptx_checkpoint_progress",
    "pptx_project_path",
    "pptx_snapshot_complete",
)


async def runtime_save_sandbox_context(conversation_id: str, ctx: dict[str, Any]) -> bool:
    """Persist Runtime-owned sandbox conversation context."""

    if not conversation_id:
        return False
    try:
        from packages.core.cache import cache

        saved = await cache.set(
            f"{RUNTIME_SANDBOX_CONTEXT_PREFIX}{conversation_id}",
            ctx,
            ttl=RUNTIME_SANDBOX_CONTEXT_TTL,
        )
        if not saved:
            logger.warning(
                "[runtime.sandbox] context save unavailable: conversation=%s",
                conversation_id,
            )
        return bool(saved)
    except Exception as exc:
        logger.warning("[runtime.sandbox] context save failed: %s", exc)
        return False


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
    *,
    skill_key: str | None = None,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    preserve_pptx_checkpoint: bool = True,
) -> dict[str, Any]:
    """Initialize or safely resume Runtime-owned sandbox context.

    Reinvoking the same skill is the normal cross-turn continuation path. It
    must not erase PPTX checkpoints, the immutable-snapshot flag, recorded
    project path, or one-read QA receipts. A different sandbox, skill, tenant,
    or user still gets a clean context.
    """

    entity_value = str(entity_id or "")
    user_value = str(user_id or "")
    agent_value = str(agent_id or "")
    skill_key_value = str(skill_key or "").strip()
    existing = await runtime_load_sandbox_context(conversation_id)
    same_owner_and_skill = (
        isinstance(existing, dict)
        and str(existing.get("skill_id") or "") == skill_id
        and str(existing.get("entity_id") or "") == entity_value
        and str(existing.get("user_id") or "") == user_value
        and str(existing.get("agent_id") or "") == agent_value
    )
    can_resume = same_owner_and_skill and str(existing.get("sandbox_id") or "") == sandbox_id
    can_restore_pptx = (
        preserve_pptx_checkpoint
        and same_owner_and_skill
        and skill_key_value in {"pptx", "slides"}
        and bool(str(existing.get("pptx_checkpoint_rel_path") or "").strip())
    )
    if can_resume:
        ctx = dict(existing)
        ctx.update(
            {
                "sandbox_id": sandbox_id,
                "skill_id": skill_id,
                "skill_key": skill_key_value,
                "entity_id": entity_value,
                "user_id": user_value,
                "agent_id": agent_value,
                "last_initialized_at": time.time(),
            }
        )
        ctx.setdefault("created_at", time.time())
        ctx.setdefault("exec_history", [])
    else:
        ctx = {
            "sandbox_id": sandbox_id,
            "skill_id": skill_id,
            "skill_key": skill_key_value,
            "entity_id": entity_value,
            "user_id": user_value,
            "agent_id": agent_value,
            "created_at": time.time(),
            "exec_history": [],
        }
        if can_restore_pptx:
            for key in _PERSISTED_PPTX_CONTEXT_KEYS:
                if key in existing:
                    ctx[key] = existing[key]
            ctx["pptx_checkpoint_restored_from_sandbox"] = str(
                existing.get("sandbox_id") or ""
            )
    if not await runtime_save_sandbox_context(conversation_id, ctx):
        raise RuntimeError("Sandbox owner context could not be persisted.")
    return ctx


def runtime_sandbox_context_owner_matches(
    ctx: dict[str, Any] | None,
    *,
    entity_id: str,
    user_id: str | None,
) -> bool:
    """Require an exact tenant/user match before reusing a conversation sandbox."""

    if not isinstance(ctx, dict):
        return False
    return (
        str(ctx.get("entity_id") or "") == str(entity_id or "")
        and str(ctx.get("user_id") or "") == str(user_id or "")
    )
