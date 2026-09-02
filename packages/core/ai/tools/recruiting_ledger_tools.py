"""Tool boundary for the optional Recruiting/HR Ledger contract."""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_tool_call_context_is_external_customer,
)
from packages.core.ai.runtime.ledger_access import (
    WorkspaceLedgerConfigurationError,
    runtime_workspace_ledger_config,
    runtime_workspace_ledger_write_allowed,
    workspace_ledger_write_forbidden_payload,
)
from packages.core.services.recruiting_ledger import (
    RECRUITING_CLEARABLE_FIELDS,
    RECRUITING_LEDGER_CONTRACT_ID,
    RecruitingLedgerError,
    RecruitingStage,
    RecruitingStatus,
    RecruitingSubjectType,
    read_recruiting_ledger,
    record_recruiting_event,
)


logger = logging.getLogger(__name__)


READ_RECRUITING_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_recruiting_ledger",
        "description": "Read the validated current Recruiting/HR projection and recent lifecycle events.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "recent_limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    },
}


RECORD_RECRUITING_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_recruiting_ledger",
        "description": (
            "Append one immutable candidate, employee, or contractor lifecycle event. "
            "Use opaque stable record keys and keep sensitive source material in evidence references."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "record_key": {"type": "string", "description": "Stable Workspace-local candidate or employee record key."},
                "subject_type": {
                    "type": "string",
                    "enum": [item.value for item in RecruitingSubjectType],
                    "description": "Omit on later events to preserve the current subject type.",
                },
                "display_name": {"type": "string"},
                "role_title": {"type": "string"},
                "department": {"type": "string"},
                "location": {"type": "string"},
                "manager_ref": {"type": "string"},
                "stage": {"type": "string", "enum": [item.value for item in RecruitingStage]},
                "status": {
                    "type": "string",
                    "enum": [item.value for item in RecruitingStatus],
                    "description": "Omit on later events to preserve the current status.",
                },
                "clear_fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(RECRUITING_CLEARABLE_FIELDS),
                    },
                    "maxItems": len(RECRUITING_CLEARABLE_FIELDS),
                    "description": (
                        "Append an explicit correction that clears nullable current-projection fields. "
                        "Do not also set the same field in this event."
                    ),
                },
                "event": {"type": "string", "description": "Lifecycle event name, such as interview_completed or start_date_confirmed."},
                "occurred_at": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "payload": {"type": "object"},
                "evidence_refs": {"type": "array", "items": {"type": "object"}},
                "directory": {"type": "string"},
            },
            "required": ["record_key", "event", "idempotency_key"],
        },
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error() -> str:
    return _json({
        "ok": False,
        "error": {"code": "missing_workspace_context", "message": "Entity and Workspace context are required"},
    })


def _service_error(exc: RecruitingLedgerError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _external_surface_error() -> str:
    return _json({
        "ok": False,
        "error": {
            "code": "recruiting_ledger_not_available_on_external_surface",
            "message": "Recruiting/HR Ledger access is restricted to internal Workspace agents",
        },
    })


async def _configured_directory(
    *,
    entity_id: str,
    workspace_id: str,
    requested_directory: object,
) -> str:
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=workspace_id,
            contract_id=RECRUITING_LEDGER_CONTRACT_ID,
            requested_directory=requested_directory,
        )
    except WorkspaceLedgerConfigurationError as exc:
        raise RecruitingLedgerError(exc.code, str(exc)) from exc
    return str(config["directory"])


async def _read_recruiting_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if runtime_tool_call_context_is_external_customer(kwargs):
        return _external_surface_error()
    try:
        directory = await _configured_directory(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            requested_directory=kwargs.get("directory"),
        )
        result = await read_recruiting_ledger(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            directory=directory,
            recent_limit=kwargs.get("recent_limit", 100),
        )
    except RecruitingLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read Recruiting Ledger")
        return _service_error(RecruitingLedgerError("ledger_read_failed", f"Recruiting Ledger could not be read: {exc}"))
    return _json({"ok": True, **result})


async def _record_recruiting_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if runtime_tool_call_context_is_external_customer(kwargs):
        return _external_surface_error()
    if not await runtime_workspace_ledger_write_allowed(context):
        return _json(workspace_ledger_write_forbidden_payload())
    try:
        directory = await _configured_directory(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            requested_directory=kwargs.get("directory"),
        )
        result = await record_recruiting_event(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            record_key=str(kwargs.get("record_key") or ""),
            subject_type=kwargs.get("subject_type"),
            display_name=kwargs.get("display_name"),
            role_title=kwargs.get("role_title"),
            department=kwargs.get("department"),
            location=kwargs.get("location"),
            manager_ref=kwargs.get("manager_ref"),
            stage=kwargs.get("stage"),
            status=kwargs.get("status"),
            clear_fields=(
                kwargs.get("clear_fields")
                if isinstance(kwargs.get("clear_fields"), list)
                else None
            ),
            event=str(kwargs.get("event") or ""),
            occurred_at=kwargs.get("occurred_at"),
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
            payload=kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else None,
            evidence_refs=kwargs.get("evidence_refs") if isinstance(kwargs.get("evidence_refs"), list) else None,
            directory=directory,
            agent_id=context.agent_id,
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            workflow_run_id=context.workflow_run_id,
        )
    except RecruitingLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record Recruiting Ledger")
        return _service_error(RecruitingLedgerError("ledger_write_failed", f"Recruiting Ledger could not be recorded: {exc}"))
    return _json(result)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (READ_RECRUITING_LEDGER_SCHEMA, _read_recruiting_ledger),
        (RECORD_RECRUITING_LEDGER_SCHEMA, _record_recruiting_ledger),
    ]


__all__ = ["get_tools"]
