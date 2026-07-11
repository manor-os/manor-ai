from __future__ import annotations

import copy
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Callable


logger = logging.getLogger(__name__)


async def runtime_start_workspace_draft_action(
    *,
    entity_id: str,
    user_id: str = "",
    initial_brief: str | None = None,
) -> str:
    """Start a workspace draft through the Runtime draft action boundary."""

    if not entity_id:
        return json.dumps({"error": "entity_id missing from tool context"})

    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_draft_service import start_draft

        async with async_session() as db:
            reply, draft = await start_draft(
                db,
                entity_id=entity_id,
                user_id=user_id or None,
                initial_brief=(initial_brief or "").strip() or None,
            )
            await db.commit()

        return json.dumps({
            "draft_id": draft.id,
            "status": draft.status,
            "ready": bool(draft.ready),
            "deep_link": f"/workspaces/new?draft={draft.id}",
            "assistant_reply": reply,
            "next_step": (
                "Tell the user the draft has started and link them to "
                f"/workspaces/new?draft={draft.id} to continue. They "
                "complete the creation in that dedicated chat."
            ),
        })
    except Exception as exc:
        logger.exception("start_workspace_draft failed")
        return json.dumps({"error": f"failed to start draft: {exc}"})


async def runtime_run_workspace_architect_turn(
    db: Any,
    *,
    draft_id: str,
    entity_id: str,
    user_id: str | None,
    user_message: str,
    history: Sequence[Mapping[str, Any]] | None = None,
    stream_handler: Any | None = None,
    on_tool_start: Any | None = None,
    on_tool_end: Any | None = None,
) -> str:
    """Run one workspace architect turn through the Runtime draft boundary."""

    from packages.core.services.workspace_architect import architect_run_turn

    return await architect_run_turn(
        db,
        draft_id=draft_id,
        entity_id=entity_id,
        user_id=user_id,
        user_message=user_message,
        history=list(history or []),
        stream_handler=stream_handler,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
    )


def runtime_workspace_architect_tool_schemas() -> list[dict[str, Any]]:
    """Return the Runtime-owned workspace architect typed-tool schemas."""

    from packages.core.ai.tools.workspace_arch_tools import ALL_TOOL_SCHEMAS

    return list(ALL_TOOL_SCHEMAS)


def runtime_workspace_architect_tool_executor(
    db: Any,
    *,
    draft_id: str,
    entity_id: str,
    user_id: str | None,
) -> Callable[[str, dict[str, Any]], Awaitable[str]]:
    """Build the Runtime-owned executor for workspace architect draft tools."""

    from packages.core.ai.tools.workspace_arch_tools import HANDLERS

    tool_lock = asyncio.Lock()

    async def executor(name: str, args: dict[str, Any]) -> str:
        handler = HANDLERS.get(name)
        if handler is None:
            return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
        next_args = dict(args or {})
        next_args.setdefault("draft_id", draft_id)
        async with tool_lock:
            try:
                return await handler(
                    db,
                    entity_id=entity_id,
                    user_id=user_id or "",
                    **next_args,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("architect tool %s crashed", name)
                return json.dumps({"ok": False, "error": f"tool crashed: {exc}"})

    return executor


def runtime_reconcile_workspace_draft_fields(
    fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply Runtime-owned workspace draft reconciliation rules."""

    from packages.core.ai.tools.workspace_arch_tools import (
        _reconcile_agent_design_flags,
        _reconcile_removed_channel_references,
    )

    next_fields = copy.deepcopy(dict(fields or {}))
    _reconcile_agent_design_flags(next_fields)
    _reconcile_removed_channel_references(next_fields)
    return next_fields


async def runtime_lint_workspace_draft(
    db: Any,
    *,
    entity_id: str,
    draft_id: str,
) -> dict[str, Any] | None:
    """Run the workspace architect draft lint through the Runtime boundary."""

    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    raw = await _lint_draft(db, entity_id=entity_id, draft_id=draft_id)
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
