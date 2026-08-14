"""Workspace-scoped tools for the Stickman Topic Ledger in Knowledge."""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.services.stickman_topic_ledger import (
    StickmanTopicLedgerError,
    backfill_used_topic,
    read_topic_ledger,
    record_topic_event,
    reserve_topic,
)


logger = logging.getLogger(__name__)


READ_STICKMAN_TOPIC_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_stickman_topic_ledger",
        "description": (
            "Read this Workspace's exact append-only Stickman Topic history from "
            "Knowledge before generating new Topic candidates."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


RECORD_STICKMAN_TOPIC_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_stickman_topic_ledger",
        "description": (
            "Atomically reserve a selected Stickman Topic in Workspace Knowledge "
            "before video generation, import an evidence-backed historical Topic, "
            "or append a video/upload result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "reserve",
                        "backfill_used",
                        "video_ready",
                        "youtube_saved",
                    ],
                },
                "run_key": {"type": "string"},
                "candidates": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "selected_topic": {"type": "string"},
                "youtube_title": {"type": "string"},
                "youtube_description": {"type": "string"},
                "selection_reason": {"type": "string"},
                "content_direction": {"type": "string"},
                "reservation_id": {"type": "string"},
                "details": {"type": "object"},
                "evidence": {"type": "object"},
            },
            "required": ["action", "selected_topic"],
        },
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error() -> str:
    return _json(
        {
            "ok": False,
            "error": {
                "code": "missing_workspace_context",
                "message": "Entity and Workspace context are required",
            },
        }
    )


def _service_error(exc: StickmanTopicLedgerError) -> str:
    return _json(
        {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
            },
        }
    )


async def _read_stickman_topic_ledger(
    entity_id: str = "",
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    try:
        result = await read_topic_ledger(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
        )
    except StickmanTopicLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read Stickman Topic Ledger")
        return _service_error(
            StickmanTopicLedgerError(
                "ledger_read_failed",
                f"Stickman Topic Ledger could not be read: {exc}",
            )
        )
    return _json({"ok": True, **result})


async def _record_stickman_topic_ledger(
    entity_id: str = "",
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    action = str(kwargs.get("action") or "").strip()
    try:
        if action == "reserve":
            result = await reserve_topic(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                run_key=str(kwargs.get("run_key") or ""),
                candidates=kwargs.get("candidates"),
                selected_topic=str(kwargs.get("selected_topic") or ""),
                youtube_title=str(kwargs.get("youtube_title") or ""),
                youtube_description=str(kwargs.get("youtube_description") or ""),
                selection_reason=str(kwargs.get("selection_reason") or ""),
                content_direction=str(kwargs.get("content_direction") or ""),
                agent_id=context.agent_id,
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
            )
        elif action == "backfill_used":
            result = await backfill_used_topic(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                selected_topic=str(kwargs.get("selected_topic") or ""),
                evidence=(dict(kwargs["evidence"]) if isinstance(kwargs.get("evidence"), dict) else {}),
                agent_id=context.agent_id,
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
            )
        elif action in {"video_ready", "youtube_saved"}:
            result = await record_topic_event(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                event=action,
                reservation_id=str(kwargs.get("reservation_id") or ""),
                selected_topic=str(kwargs.get("selected_topic") or ""),
                details=(dict(kwargs["details"]) if isinstance(kwargs.get("details"), dict) else {}),
                agent_id=context.agent_id,
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
            )
        else:
            raise StickmanTopicLedgerError(
                "invalid_action",
                "action must be reserve, backfill_used, video_ready, or youtube_saved",
            )
    except StickmanTopicLedgerError as exc:
        if exc.code == "topic_already_used":
            return _json({"ok": False, "code": exc.code, "message": str(exc)})
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record Stickman Topic Ledger")
        return _service_error(
            StickmanTopicLedgerError(
                "ledger_write_failed",
                f"Stickman Topic Ledger could not be recorded: {exc}",
            )
        )
    return _json(result)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (READ_STICKMAN_TOPIC_LEDGER_SCHEMA, _read_stickman_topic_ledger),
        (RECORD_STICKMAN_TOPIC_LEDGER_SCHEMA, _record_stickman_topic_ledger),
    ]
