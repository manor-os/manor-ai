"""Generic Workspace content-ledger tools.

The installed Blueprint supplies the directory, content kind, identity field,
and event vocabulary through tool arguments.  No application name is encoded
in this runtime tool surface.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.ledger_access import (
    WorkspaceLedgerConfigurationError,
    runtime_workspace_ledger_config,
    runtime_workspace_ledger_write_allowed,
    workspace_ledger_write_forbidden_payload,
)
from packages.core.services.content_ledger import (
    CONTENT_LEDGER_CONTRACT_ID,
    ContentLedgerError,
    record_content_event,
    read_content_ledger,
    reserve_content,
)


logger = logging.getLogger(__name__)


READ_CONTENT_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_content_ledger",
        "description": (
            "Read the exact append-only content history configured by the "
            "active Workspace Blueprint."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "legacy_directories": {"type": "array", "items": {"type": "string"}},
                "legacy_identity_fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
        },
    },
}


RECORD_CONTENT_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_content_ledger",
        "description": (
            "Atomically reserve a content identity or append a lifecycle "
            "event in the content ledger configured by the active Blueprint."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["reserve", "event", "backfill_used"],
                },
                "event": {"type": "string"},
                "run_key": {"type": "string"},
                "identity_key": {"type": "string"},
                "reservation_id": {"type": "string"},
                "content_kind": {"type": "string"},
                "file_type": {"type": "string"},
                "directory": {"type": "string"},
                "legacy_directories": {"type": "array", "items": {"type": "string"}},
                "legacy_identity_fields": {"type": "array", "items": {"type": "string"}},
                "payload": {"type": "object"},
                "details": {"type": "object"},
                "candidates": {"type": "array", "items": {"type": "object"}},
                "evidence": {"type": "object"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Knowledge or external evidence references bound to this ledger record.",
                },
            },
            "required": ["action"],
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


def _service_error(exc: ContentLedgerError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    explicit = kwargs.get("payload")
    if isinstance(explicit, dict):
        return dict(explicit)
    details = kwargs.get("details")
    if isinstance(details, dict):
        return dict(details)
    # Compatibility with an installed Blueprint that still sends the fields
    # directly while it is being upgraded to the generic contract.
    ignored = {
        "action", "event", "run_key", "identity_key",
        "reservation_id", "content_kind", "file_type", "directory", "legacy_directories", "legacy_identity_fields", "payload", "details",
        "evidence_refs",
    }
    return {key: value for key, value in kwargs.items() if key not in ignored and not key.startswith("_")}


def _identity(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("identity_key") or "").strip()


def _run_key(raw_value: Any, workflow_run_id: str | None) -> str:
    requested = str(raw_value or "").strip()
    if workflow_run_id and (not requested or (requested.startswith("{{") and requested.endswith("}}"))):
        return workflow_run_id
    return requested


async def _read_content_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=CONTENT_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
            requested_legacy_directories=kwargs.get("legacy_directories"),
            requested_legacy_identity_fields=kwargs.get("legacy_identity_fields"),
        )
        result = await read_content_ledger(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            directory=str(config["directory"]),
            legacy_directories=tuple(config.get("legacy_directories") or ()),
            legacy_identity_fields=tuple(config.get("legacy_identity_fields") or ()),
        )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(ContentLedgerError(exc.code, str(exc)))
    except ContentLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read content ledger")
        return _service_error(ContentLedgerError("ledger_read_failed", f"Content Ledger could not be read: {exc}"))
    return _json({"ok": True, **result})


async def _record_content_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if not await runtime_workspace_ledger_write_allowed(context):
        return _json(workspace_ledger_write_forbidden_payload())
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=CONTENT_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
            requested_legacy_directories=kwargs.get("legacy_directories"),
            requested_legacy_identity_fields=kwargs.get("legacy_identity_fields"),
        )
        action = str(kwargs.get("action") or "").strip()
        directory = str(config["directory"])
        legacy_directories = tuple(config.get("legacy_directories") or ())
        legacy_identity_fields = tuple(config.get("legacy_identity_fields") or ())
        identity_key = _identity(kwargs)
        if action in {"reserve", "backfill_used"}:
            payload = _payload(kwargs)
            if action == "backfill_used":
                payload["evidence"] = dict(kwargs.get("evidence") or {})
            result = await reserve_content(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                run_key=_run_key(kwargs.get("run_key"), context.workflow_run_id),
                identity_key=identity_key,
                payload=payload,
                content_kind=str(kwargs.get("content_kind") or "document"),
                file_type=(str(kwargs["file_type"]) if kwargs.get("file_type") else None),
                evidence_refs=(kwargs.get("evidence_refs") if isinstance(kwargs.get("evidence_refs"), list) else None),
                directory=directory,
                legacy_directories=legacy_directories,
                legacy_identity_fields=legacy_identity_fields,
                agent_id=context.agent_id,
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
                workflow_run_id=context.workflow_run_id,
            )
        else:
            event = str(kwargs.get("event") or "").strip()
            result = await record_content_event(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                event=event,
                reservation_id=str(kwargs.get("reservation_id") or ""),
                identity_key=identity_key,
                payload=_payload(kwargs),
                content_kind=str(kwargs.get("content_kind") or "document"),
                file_type=(str(kwargs["file_type"]) if kwargs.get("file_type") else None),
                evidence_refs=(kwargs.get("evidence_refs") if isinstance(kwargs.get("evidence_refs"), list) else None),
                directory=directory,
                legacy_directories=legacy_directories,
                agent_id=context.agent_id,
                task_id=context.task_id,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
                workflow_run_id=context.workflow_run_id,
            )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(ContentLedgerError(exc.code, str(exc)))
    except ContentLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record content ledger")
        return _service_error(ContentLedgerError("ledger_write_failed", f"Content Ledger could not be recorded: {exc}"))
    return _json(result)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (READ_CONTENT_LEDGER_SCHEMA, _read_content_ledger),
        (RECORD_CONTENT_LEDGER_SCHEMA, _record_content_ledger),
    ]
