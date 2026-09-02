"""Tool boundary for the optional generic Finance Ledger contract."""

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
from packages.core.services.finance_ledger import (
    FINANCE_LEDGER_CONTRACT_ID,
    FinanceLedgerError,
    read_finance_ledger,
    record_finance_entry,
)


logger = logging.getLogger(__name__)


EVIDENCE_REFS_SCHEMA = {
    "type": "array",
    "items": {"type": "object"},
    "description": "Knowledge or external evidence references for this entry.",
}


READ_FINANCE_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_finance_ledger",
        "description": "Read validated append-only finance entries configured by the active Workspace Blueprint.",
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": [],
        },
    },
}


RECORD_FINANCE_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_finance_ledger",
        "description": "Append one validated finance entry; corrections use reverses_entry_id or supersedes_entry_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry_type": {
                    "type": "string",
                    "enum": [
                        "income", "expense", "invoice", "bill", "payment",
                        "refund", "transfer", "adjustment", "journal_entry",
                    ],
                },
                "status": {
                    "type": "string",
                    "enum": ["planned", "pending", "posted", "paid", "reconciled", "void", "reversed"],
                },
                "amount_minor": {"type": "integer", "minimum": 1},
                "currency": {"type": "string", "description": "ISO 4217 three-letter code, e.g. USD."},
                "direction": {"type": "string", "enum": ["inflow", "outflow"]},
                "account_ref": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "occurred_at": {"type": "string"},
                "counterparty_ref": {"type": "string"},
                "invoice_ref": {"type": "string"},
                "payment_ref": {"type": "string"},
                "source_system": {"type": "string"},
                "source_record_id": {"type": "string"},
                "reverses_entry_id": {"type": "string"},
                "supersedes_entry_id": {"type": "string"},
                "tax_minor": {"type": "integer", "minimum": 0},
                "fee_minor": {"type": "integer", "minimum": 0},
                "evidence_refs": EVIDENCE_REFS_SCHEMA,
                "journal_lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_ref": {"type": "string"},
                            "direction": {"type": "string", "enum": ["debit", "credit"]},
                            "amount_minor": {"type": "integer", "minimum": 1},
                            "memo": {"type": "string"},
                        },
                        "required": ["account_ref", "direction", "amount_minor"],
                    },
                },
                "memo": {"type": "string"},
                "payload": {"type": "object"},
                "directory": {"type": "string"},
            },
            "required": [
                "entry_type", "amount_minor", "currency", "direction",
                "account_ref", "idempotency_key",
            ],
        },
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error() -> str:
    return _json({"ok": False, "error": {"code": "missing_workspace_context", "message": "Entity and Workspace context are required"}})


def _service_error(exc: FinanceLedgerError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _integer_arg(kwargs: dict[str, Any], name: str, *, default: int = 0) -> int:
    raw = kwargs.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise FinanceLedgerError("invalid_input", f"{name} must be an integer") from exc


async def _read_finance_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=FINANCE_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
        )
        result = await read_finance_ledger(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            directory=str(config["directory"]),
        )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(FinanceLedgerError(exc.code, str(exc)))
    except FinanceLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read finance ledger")
        return _service_error(FinanceLedgerError("ledger_read_failed", f"Finance Ledger could not be read: {exc}"))
    return _json({"ok": True, **result})


async def _record_finance_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if not await runtime_workspace_ledger_write_allowed(context):
        return _json(workspace_ledger_write_forbidden_payload())
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=FINANCE_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
        )
        result = await record_finance_entry(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            entry_type=str(kwargs.get("entry_type") or ""),
            amount_minor=_integer_arg(kwargs, "amount_minor"),
            currency=str(kwargs.get("currency") or ""),
            direction=str(kwargs.get("direction") or ""),
            account_ref=str(kwargs.get("account_ref") or ""),
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
            occurred_at=kwargs.get("occurred_at"),
            status=str(kwargs.get("status") or "posted"),
            counterparty_ref=kwargs.get("counterparty_ref"),
            invoice_ref=kwargs.get("invoice_ref"),
            payment_ref=kwargs.get("payment_ref"),
            source_system=kwargs.get("source_system"),
            source_record_id=kwargs.get("source_record_id"),
            reverses_entry_id=kwargs.get("reverses_entry_id"),
            supersedes_entry_id=kwargs.get("supersedes_entry_id"),
            tax_minor=_integer_arg(kwargs, "tax_minor"),
            fee_minor=_integer_arg(kwargs, "fee_minor"),
            evidence_refs=kwargs.get("evidence_refs") if isinstance(kwargs.get("evidence_refs"), list) else None,
            journal_lines=kwargs.get("journal_lines") if isinstance(kwargs.get("journal_lines"), list) else None,
            memo=kwargs.get("memo"),
            payload=kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else None,
            directory=str(config["directory"]),
            agent_id=context.agent_id,
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            workflow_run_id=context.workflow_run_id,
        )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(FinanceLedgerError(exc.code, str(exc)))
    except FinanceLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record finance ledger")
        return _service_error(FinanceLedgerError("ledger_write_failed", f"Finance Ledger could not be recorded: {exc}"))
    return _json(result)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (READ_FINANCE_LEDGER_SCHEMA, _read_finance_ledger),
        (RECORD_FINANCE_LEDGER_SCHEMA, _record_finance_ledger),
    ]


__all__ = ["get_tools"]
