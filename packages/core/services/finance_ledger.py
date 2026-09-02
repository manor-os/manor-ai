"""Workspace finance ledger contract.

This module is an application-level contract built on the generic Workspace
ledger storage primitives.  It deliberately contains no provider, accounting
integration, or Blueprint-specific policy.  A Blueprint may choose whether to
install the contract and which account vocabulary it wants to use.

Entries are append-only.  Corrections are represented by a new entry that
points at the entry it reverses or supersedes; the original record is never
edited or deleted.  Documents in Knowledge are referenced through
``evidence_refs`` rather than copied into the ledger.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
import os
from typing import Annotated, Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
)
from packages.core.models.base import generate_ulid
from packages.core.services.ledger_evidence import EvidenceReferenceError, LedgerEvidenceRef, verify_evidence_refs
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.workspace_artifacts import ensure_workspace_artifact_directory
from packages.core.services.workspace_artifacts import resolve_workspace_artifact_directory
from packages.core.services.workspace_ledger import (
    LedgerStorage,
    WorkspaceLedgerError,
    ensure_ledger_location,
    ledger_key_fingerprint,
    ledger_record_files,
    ledger_now_iso,
    write_immutable_json,
)


FINANCE_LEDGER_CONTRACT_ID = "manor.finance_ledger/v1"
DEFAULT_FINANCE_LEDGER_DIRECTORY = "finance-ledger"
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class FinanceLedgerError(ValueError):
    """Stable application error returned by the finance-ledger boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FinanceEntryType(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    INVOICE = "invoice"
    BILL = "bill"
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"
    JOURNAL_ENTRY = "journal_entry"


class FinanceDirection(StrEnum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"


class FinanceStatus(StrEnum):
    PLANNED = "planned"
    PENDING = "pending"
    POSTED = "posted"
    PAID = "paid"
    RECONCILED = "reconciled"
    VOID = "void"
    REVERSED = "reversed"


_REALIZED_FINANCE_STATUSES = frozenset({
    FinanceStatus.POSTED.value,
    FinanceStatus.PAID.value,
    FinanceStatus.RECONCILED.value,
})


class JournalLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_ref: NonEmptyText
    direction: Literal["debit", "credit"]
    amount_minor: int = Field(gt=0)
    memo: NonEmptyText | None = None


class FinanceLedgerEntry(BaseModel):
    """Versioned, immutable accounting event stored in a Workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_id: Literal[FINANCE_LEDGER_CONTRACT_ID] = FINANCE_LEDGER_CONTRACT_ID
    schema_version: Literal[1] = 1
    entry_id: NonEmptyText
    workspace_id: NonEmptyText
    entity_id: NonEmptyText
    entry_type: FinanceEntryType
    status: FinanceStatus = FinanceStatus.POSTED
    occurred_at: datetime
    recorded_at: datetime
    amount_minor: int = Field(gt=0)
    currency: CurrencyCode
    direction: FinanceDirection
    account_ref: NonEmptyText
    counterparty_ref: NonEmptyText | None = None
    invoice_ref: NonEmptyText | None = None
    payment_ref: NonEmptyText | None = None
    source_system: NonEmptyText | None = None
    source_record_id: NonEmptyText | None = None
    idempotency_key: NonEmptyText
    reverses_entry_id: NonEmptyText | None = None
    supersedes_entry_id: NonEmptyText | None = None
    tax_minor: int = Field(default=0, ge=0)
    fee_minor: int = Field(default=0, ge=0)
    evidence_refs: list[LedgerEvidenceRef] = Field(default_factory=list)
    journal_lines: list[JournalLine] = Field(default_factory=list)
    memo: NonEmptyText | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_entry(self) -> "FinanceLedgerEntry":
        if any(ref.workspace_id != self.workspace_id for ref in self.evidence_refs):
            raise ValueError("Evidence reference belongs to another Workspace")
        if self.entry_type == FinanceEntryType.JOURNAL_ENTRY:
            if not self.journal_lines:
                raise ValueError("journal_entry requires journal_lines")
            debits = sum(line.amount_minor for line in self.journal_lines if line.direction == "debit")
            credits = sum(line.amount_minor for line in self.journal_lines if line.direction == "credit")
            if debits != credits:
                raise ValueError("journal_entry debits and credits must balance")
            if debits != self.amount_minor:
                raise ValueError("journal_entry total must equal amount_minor")
        return self


def effective_finance_entries(
    entries: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return realized entries that remain effective in the current projection."""

    realized = [
        dict(entry)
        for entry in entries
        if str(entry.get("status") or "").strip().casefold()
        in _REALIZED_FINANCE_STATUSES
    ]
    superseded_ids = {
        str(entry.get("supersedes_entry_id") or "").strip()
        for entry in realized
        if str(entry.get("supersedes_entry_id") or "").strip()
    }
    return [
        entry
        for entry in realized
        if str(entry.get("entry_id") or "").strip() not in superseded_ids
    ]


def _safe_directory(value: str | None) -> str:
    raw = str(value or DEFAULT_FINANCE_LEDGER_DIRECTORY).strip().strip("/")
    if not raw or raw in {".", ".."} or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise FinanceLedgerError("invalid_ledger_path", "Ledger directory must be Workspace-relative")
    return raw


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise FinanceLedgerError("invalid_input", f"{field} is required")
    return text


def _idempotency_payload(entry: FinanceLedgerEntry, *, exclude_occurred_at: bool) -> dict[str, Any]:
    excluded = {"entry_id", "recorded_at"}
    if exclude_occurred_at:
        excluded.add("occurred_at")
    payload = entry.model_dump(mode="json", exclude=excluded)
    for ref in payload.get("evidence_refs") or []:
        ref.pop("verified_at", None)
    return payload


async def _ledger_location(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str,
    create: bool = True,
) -> tuple[Any, str, str]:
    try:
        return await ensure_ledger_location(
            entity_id=entity_id,
            workspace_id=workspace_id,
            storage=LedgerStorage(directory=_safe_directory(directory)),
            directory_resolver=(
                ensure_workspace_artifact_directory
                if create
                else resolve_workspace_artifact_directory
            ),
            entity_root_resolver=runtime_entity_file_root,
            create=create,
        )
    except WorkspaceLedgerError as exc:
        raise FinanceLedgerError(exc.code, str(exc)) from exc
    except FinanceLedgerError:
        raise
    except ValueError as exc:
        raise FinanceLedgerError("ledger_not_installed", str(exc)) from exc


async def _sync_ledger_projection(
    *,
    entity_id: str,
    workspace_id: str,
    abs_path: str,
    entity_root: str,
    agent_id: str | None,
    task_id: str | None,
    conversation_id: str | None,
    user_id: str | None,
) -> Any:
    await bump_tool_cache_version(entity_id, "ledgers")
    projection = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source="ai_generated",
        created_by=agent_id or "finance_ledger",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_finance_ledger",
    )
    if not bool(getattr(projection, "synced", False)):
        raise FinanceLedgerError("knowledge_sync_failed", "Finance ledger committed but Knowledge projection failed")
    return projection


def _parse_entry(path: str, *, workspace_id: str, entity_id: str) -> FinanceLedgerEntry:
    try:
        with open(path, encoding="utf-8") as source:
            raw = json.load(source)
        entry = FinanceLedgerEntry.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise FinanceLedgerError(
            "ledger_schema_invalid",
            f"Finance ledger record is invalid: {os.path.basename(path)}",
        ) from exc
    if entry.workspace_id != workspace_id or entry.entity_id != entity_id:
        raise FinanceLedgerError("ledger_scope_invalid", "Finance ledger record belongs to another Workspace")
    return entry


async def read_finance_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_FINANCE_LEDGER_DIRECTORY,
    recent_limit: int | None = 100,
    create_location: bool = True,
) -> dict[str, Any]:
    """Read validated finance entries for one Workspace entity."""

    _, ledger_root, _ = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        create=create_location,
    )
    entries = [
        _parse_entry(path, workspace_id=workspace_id, entity_id=entity_id)
        for path in ledger_record_files(ledger_root)
        if os.path.basename(os.path.dirname(path)) == "events"
    ]
    entries.sort(key=lambda item: item.recorded_at)
    if recent_limit is None or int(recent_limit) <= 0:
        visible_entries = entries
    else:
        limit = max(1, min(int(recent_limit), 500))
        visible_entries = entries[-limit:]
    return {
        "contract_id": FINANCE_LEDGER_CONTRACT_ID,
        "schema_version": 1,
        "entry_count": len(entries),
        "entries": [entry.model_dump(mode="json") for entry in visible_entries],
        "run_key": generate_ulid(),
    }


async def record_finance_entry(
    *,
    entity_id: str,
    workspace_id: str,
    entry_type: str,
    amount_minor: int,
    currency: str,
    direction: str,
    account_ref: str,
    idempotency_key: str,
    occurred_at: datetime | str | None = None,
    status: str = FinanceStatus.POSTED.value,
    counterparty_ref: str | None = None,
    invoice_ref: str | None = None,
    payment_ref: str | None = None,
    source_system: str | None = None,
    source_record_id: str | None = None,
    reverses_entry_id: str | None = None,
    supersedes_entry_id: str | None = None,
    tax_minor: int = 0,
    fee_minor: int = 0,
    evidence_refs: list[dict[str, Any]] | None = None,
    journal_lines: list[dict[str, Any]] | None = None,
    memo: str | None = None,
    payload: dict[str, Any] | None = None,
    directory: str = DEFAULT_FINANCE_LEDGER_DIRECTORY,
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    """Append one validated finance entry, idempotent by source key."""

    clean_workspace = _required_text(workspace_id, "workspace_id")
    clean_entity = _required_text(entity_id, "entity_id")
    clean_idempotency = _required_text(idempotency_key, "idempotency_key")
    occurred_was_supplied = occurred_at is not None
    occurred = occurred_at or datetime.fromisoformat(ledger_now_iso())
    if isinstance(occurred, str):
        try:
            occurred = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FinanceLedgerError("invalid_input", "occurred_at must be an ISO timestamp") from exc
    try:
        normalized_evidence_refs = await verify_evidence_refs(
            evidence_refs,
            entity_id=clean_entity,
            workspace_id=clean_workspace,
        )
        entry = FinanceLedgerEntry(
            entry_id=generate_ulid(),
            workspace_id=clean_workspace,
            entity_id=clean_entity,
            entry_type=entry_type,
            status=status,
            occurred_at=occurred,
            recorded_at=datetime.fromisoformat(ledger_now_iso()),
            amount_minor=amount_minor,
            currency=str(currency or "").strip().upper(),
            direction=direction,
            account_ref=account_ref,
            counterparty_ref=counterparty_ref,
            invoice_ref=invoice_ref,
            payment_ref=payment_ref,
            source_system=source_system,
            source_record_id=source_record_id,
            idempotency_key=clean_idempotency,
            reverses_entry_id=reverses_entry_id,
            supersedes_entry_id=supersedes_entry_id,
            tax_minor=tax_minor,
            fee_minor=fee_minor,
            evidence_refs=normalized_evidence_refs,
            journal_lines=journal_lines or [],
            memo=memo,
            payload=payload or {},
        )
    except EvidenceReferenceError as exc:
        raise FinanceLedgerError(exc.code, str(exc)) from exc
    except ValueError as exc:
        raise FinanceLedgerError("ledger_schema_invalid", str(exc)) from exc

    directory_record, ledger_root, entity_root = await _ledger_location(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        directory=directory,
    )
    filename = f"finance-{ledger_key_fingerprint(clean_idempotency)}.json"
    relative_name = f"events/{filename}"
    abs_path = os.path.join(ledger_root, relative_name)
    existing = await read_finance_ledger(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        directory=directory,
        recent_limit=500,
    )
    for raw in existing["entries"]:
        if raw.get("idempotency_key") != clean_idempotency:
            continue
        prior = FinanceLedgerEntry.model_validate(raw)
        if _idempotency_payload(prior, exclude_occurred_at=not occurred_was_supplied) == _idempotency_payload(
            entry,
            exclude_occurred_at=not occurred_was_supplied,
        ):
            projection = None
            if os.path.isfile(abs_path):
                projection = await _sync_ledger_projection(
                    entity_id=clean_entity,
                    workspace_id=clean_workspace,
                    abs_path=abs_path,
                    entity_root=entity_root,
                    agent_id=agent_id,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
            return {
                "ok": True,
                "idempotent": True,
                "entry_id": prior.entry_id,
                "status": prior.status,
                "document_id": getattr(projection, "document_id", None),
            }
        raise FinanceLedgerError(
            "idempotency_conflict",
            "The idempotency_key already exists with different finance data",
        )

    created = write_immutable_json(abs_path, entry.model_dump(mode="json"))
    if not created:
        existing_entry = _parse_entry(abs_path, workspace_id=clean_workspace, entity_id=clean_entity)
        if existing_entry.idempotency_key == clean_idempotency:
            if _idempotency_payload(
                existing_entry,
                exclude_occurred_at=not occurred_was_supplied,
            ) != _idempotency_payload(
                entry,
                exclude_occurred_at=not occurred_was_supplied,
            ):
                raise FinanceLedgerError(
                    "idempotency_conflict",
                    "The idempotency_key already exists with different finance data",
                )
            projection = await _sync_ledger_projection(
                entity_id=clean_entity,
                workspace_id=clean_workspace,
                abs_path=abs_path,
                entity_root=entity_root,
                agent_id=agent_id,
                task_id=task_id,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            return {
                "ok": True,
                "idempotent": True,
                "entry_id": existing_entry.entry_id,
                "status": existing_entry.status,
                "document_id": getattr(projection, "document_id", None),
            }
        raise FinanceLedgerError("idempotency_conflict", "The finance record path is already occupied")
    projection = await _sync_ledger_projection(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        abs_path=abs_path,
        entity_root=entity_root,
        agent_id=agent_id,
        task_id=task_id,
        conversation_id=conversation_id,
        user_id=user_id,
    )
    return {
        "ok": True,
        "idempotent": False,
        "entry_id": entry.entry_id,
        "status": entry.status,
        "path": f"{directory_record.storage_path}/{relative_name}",
        "display_path": f"{directory_record.display_path}/{relative_name}",
        "document_id": getattr(projection, "document_id", None),
    }


__all__ = [
    "DEFAULT_FINANCE_LEDGER_DIRECTORY",
    "FINANCE_LEDGER_CONTRACT_ID",
    "FinanceDirection",
    "FinanceEntryType",
    "FinanceLedgerEntry",
    "FinanceLedgerError",
    "FinanceStatus",
    "JournalLine",
    "effective_finance_entries",
    "read_finance_ledger",
    "record_finance_entry",
]
