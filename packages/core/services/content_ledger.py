"""Domain-neutral append-only content ledger service.

The ledger stores immutable Workspace-scoped reservations and lifecycle events.
The Blueprint chooses the content kind, storage directory, identity meaning,
and event names.  This module deliberately does not know about Stickman,
YouTube, music, sales, or any other application vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from typing import Any

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
)
from packages.core.models.base import generate_ulid
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
    resolve_workspace_artifact_directory,
)
from packages.core.services.ledger_evidence import (
    EvidenceReferenceError,
    normalize_evidence_refs,
    verify_evidence_refs,
)
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.workspace_ledger import (
    LedgerStorage,
    WorkspaceLedgerError,
    ensure_ledger_location,
    ledger_key_fingerprint,
    ledger_record_files,
    normalize_ledger_key,
    validate_ledger_relationships,
    write_immutable_json,
)


CONTENT_LEDGER_CONTRACT_ID = "manor.content_ledger/v1"


class ContentKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    ARTICLE = "article"
    DOCUMENT = "document"


class ContentLedgerError(ValueError):
    """Stable error returned by the generic content-ledger boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


DEFAULT_CONTENT_LEDGER_DIRECTORY = "content-ledger"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContentLedgerError("invalid_input", f"{field} is required")
    return text


def _safe_directory(value: str | None) -> str:
    raw = str(value or DEFAULT_CONTENT_LEDGER_DIRECTORY).strip().strip("/")
    if not raw or raw in {".", ".."} or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ContentLedgerError("invalid_ledger_path", "Ledger directory must be Workspace-relative")
    return raw


def _identity(value: str) -> tuple[str, str]:
    clean = _required_text(value, "identity_key")
    try:
        return clean, ledger_key_fingerprint(clean)
    except WorkspaceLedgerError as exc:
        raise ContentLedgerError(exc.code, str(exc)) from exc


def _evidence_refs(value: Any, *, workspace_id: str) -> list[dict[str, Any]]:
    """Normalize evidence while keeping the content-ledger error contract."""

    if value is None or value == []:
        return []
    try:
        return normalize_evidence_refs(value, workspace_id=workspace_id)
    except EvidenceReferenceError as exc:
        raise ContentLedgerError(exc.code, str(exc)) from exc


async def _verified_evidence_refs(
    value: Any,
    *,
    entity_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    try:
        return await verify_evidence_refs(
            value,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
    except EvidenceReferenceError as exc:
        raise ContentLedgerError(exc.code, str(exc)) from exc


def _normalize_record(
    value: dict[str, Any],
    *,
    legacy_identity_fields: tuple[str, ...] = (),
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Normalize current and legacy records without applying app semantics."""

    entry_type = str(value.get("entry_type") or "")
    # Accept the canonical shape plus explicitly migrated records whose legacy
    # entry names retain a suffix such as ``_reservation`` or ``_event``.
    is_reservation = entry_type == "reservation" or entry_type.endswith("_reservation")
    is_event = entry_type == "event" or entry_type.endswith(("_event", "_status"))
    if not is_reservation and not is_event:
        raise ContentLedgerError("ledger_schema_invalid", "Ledger record has an unsupported entry_type")
    identity_key = value.get("identity_key")
    if not identity_key:
        for field in legacy_identity_fields:
            candidate = value.get(field)
            if candidate:
                identity_key = candidate
                break
    identity_key = _required_text(identity_key, "identity_key")
    record_workspace_id = str(value.get("workspace_id") or workspace_id or "").strip()
    evidence_refs = _evidence_refs(
        value.get("evidence_refs"),
        workspace_id=record_workspace_id,
    )
    record: dict[str, Any] = {
        "contract_id": value.get("contract_id") or CONTENT_LEDGER_CONTRACT_ID,
        "content_kind": value.get("content_kind") or ContentKind.DOCUMENT.value,
        "file_type": value.get("file_type"),
        "schema_version": value.get("schema_version") or 1,
        "entity_id": value.get("entity_id"),
        "workspace_id": value.get("workspace_id") or workspace_id,
        "entry_type": "reservation" if is_reservation else "event",
        "recorded_at": value.get("recorded_at") or "",
        "identity_key": identity_key,
        "identity_fingerprint": value.get("identity_fingerprint"),
        "source_task_id": value.get("source_task_id"),
        "source_workflow_run_id": value.get("source_workflow_run_id"),
        "evidence_refs": evidence_refs,
    }
    if is_reservation:
        record.update(
            {
                "entry_id": value.get("entry_id"),
                "run_key": value.get("run_key"),
                "status": value.get("status") or "reserved",
                "payload": value.get("payload") if isinstance(value.get("payload"), dict) else dict(value),
            }
        )
    else:
        record.update(
            {
                "event_id": value.get("event_id"),
                "reservation_id": value.get("reservation_id"),
                "status": value.get("status") or "",
                "payload": value.get("payload") if isinstance(value.get("payload"), dict) else (
                    value.get("details") if isinstance(value.get("details"), dict) else dict(value)
                ),
            }
        )
    if not record["recorded_at"]:
        raise ContentLedgerError("ledger_schema_invalid", "Ledger record is missing recorded_at")
    return record


def _record_files_for_directory(ledger_root: str) -> list[str]:
    return ledger_record_files(ledger_root)


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
        raise ContentLedgerError(exc.code, str(exc)) from exc
    except ContentLedgerError:
        raise
    except ValueError as exc:
        raise ContentLedgerError("ledger_not_installed", str(exc)) from exc


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
        created_by=agent_id or "content_ledger",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_content_ledger",
    )
    if not bool(getattr(projection, "synced", False)):
        raise ContentLedgerError("knowledge_sync_failed", "Ledger record committed but Knowledge projection failed")
    return projection


def _read_records(
    ledger_root: str,
    *,
    legacy_identity_fields: tuple[str, ...] = (),
    workspace_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reservations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for path in _record_files_for_directory(ledger_root):
        try:
            with open(path, encoding="utf-8") as source:
                raw = json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContentLedgerError("ledger_unreadable", f"Ledger record is unreadable: {os.path.basename(path)}") from exc
        if not isinstance(raw, dict):
            raise ContentLedgerError("ledger_schema_invalid", f"Ledger record is not an object: {os.path.basename(path)}")
        record = _normalize_record(
            raw,
            legacy_identity_fields=legacy_identity_fields,
            workspace_id=workspace_id,
        )
        if record["entry_type"] == "reservation":
            reservations.append(record)
        else:
            events.append(record)
    reservations.sort(key=lambda item: str(item.get("recorded_at") or ""))
    events.sort(key=lambda item: str(item.get("recorded_at") or ""))
    try:
        validate_ledger_relationships(
            reservations,
            events,
            reservation_id_field="entry_id",
            event_reservation_id_field="reservation_id",
            identity_field="identity_key",
        )
    except WorkspaceLedgerError as exc:
        raise ContentLedgerError(exc.code, str(exc)) from exc
    return reservations, events


def _public_rows(reservations: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events_by_reservation: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        reservation_id = str(event.get("reservation_id") or "")
        if reservation_id:
            events_by_reservation.setdefault(reservation_id, []).append(event)
    rows: list[dict[str, Any]] = []
    for reservation in reservations:
        row = {
            "entry_id": reservation.get("entry_id"),
            "event_id": None,
            "entity_id": reservation.get("entity_id"),
            "workspace_id": reservation.get("workspace_id"),
            "content_kind": reservation.get("content_kind"),
            "file_type": reservation.get("file_type"),
            "identity_key": reservation.get("identity_key"),
            "identity_fingerprint": reservation.get("identity_fingerprint"),
            "status": reservation.get("status") or "reserved",
            "recorded_at": reservation.get("recorded_at"),
            "run_key": reservation.get("run_key"),
            "payload": dict(reservation.get("payload") or {}),
            "source_task_id": reservation.get("source_task_id"),
            "source_workflow_run_id": reservation.get("source_workflow_run_id"),
            "evidence_refs": list(reservation.get("evidence_refs") or []),
        }
        for event in events_by_reservation.get(str(row["entry_id"]), []):
            row["event_id"] = event.get("event_id")
            row["content_kind"] = event.get("content_kind") or row["content_kind"]
            row["file_type"] = event.get("file_type") or row["file_type"]
            if event.get("evidence_refs"):
                row["evidence_refs"] = list(event["evidence_refs"])
            row["status"] = event.get("status") or row["status"]
            row["recorded_at"] = event.get("recorded_at") or row["recorded_at"]
            row["payload"].update(dict(event.get("payload") or {}))
            row["source_task_id"] = event.get("source_task_id") or row.get("source_task_id")
            row["source_workflow_run_id"] = event.get("source_workflow_run_id") or row.get("source_workflow_run_id")
        rows.append(row)
    return sorted(rows, key=lambda item: str(item.get("recorded_at") or ""), reverse=True)


async def read_content_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_CONTENT_LEDGER_DIRECTORY,
    legacy_directories: tuple[str, ...] = (),
    legacy_identity_fields: tuple[str, ...] = (),
    recent_limit: int | None = 100,
    create_location: bool = True,
) -> dict[str, Any]:
    """Read current records plus legacy records without app-specific parsing."""

    directories = [_safe_directory(directory)] + [_safe_directory(item) for item in legacy_directories if item != directory]
    reservations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for current_directory in directories:
        try:
            _, ledger_root, _ = await _ledger_location(
                entity_id=entity_id,
                workspace_id=workspace_id,
                directory=current_directory,
                create=create_location,
            )
        except ContentLedgerError as exc:
            if exc.code == "filesystem_unavailable":
                raise
            continue
        for path in _record_files_for_directory(ledger_root):
            if path in seen_paths:
                continue
            seen_paths.add(path)
        current_reservations, current_events = _read_records(
            ledger_root,
            legacy_identity_fields=legacy_identity_fields,
            workspace_id=workspace_id,
        )
        for record in [*current_reservations, *current_events]:
            record_workspace_id = str(record.get("workspace_id") or "").strip()
            record_entity_id = str(record.get("entity_id") or "").strip()
            if record_workspace_id and record_workspace_id != workspace_id:
                raise ContentLedgerError("ledger_scope_invalid", "Ledger record belongs to another Workspace")
            if record_entity_id and record_entity_id != entity_id:
                raise ContentLedgerError("ledger_scope_invalid", "Ledger record belongs to another entity")
        reservations.extend(current_reservations)
        events.extend(current_events)
    try:
        validate_ledger_relationships(
            reservations,
            events,
            reservation_id_field="entry_id",
            event_reservation_id_field="reservation_id",
            identity_field="identity_key",
        )
    except WorkspaceLedgerError as exc:
        raise ContentLedgerError(exc.code, str(exc)) from exc
    rows = _public_rows(reservations, events)
    if recent_limit is None or int(recent_limit) <= 0:
        recent_entries = reservations + events
    else:
        limit = max(1, min(int(recent_limit), 500))
        recent_entries = (reservations + events)[-limit:]
    return {
        "contract_id": CONTENT_LEDGER_CONTRACT_ID,
        "schema_version": 1,
        "entry_count": len(reservations) + len(events),
        "reserved_count": len(reservations),
        "used_count": len(reservations),
        "used_keys": [str(item.get("identity_key") or "") for item in reservations],
        "rows": rows,
        "recent_entries": recent_entries,
        "run_key": generate_ulid(),
    }


async def reserve_content(
    *,
    entity_id: str,
    workspace_id: str,
    run_key: str,
    identity_key: str,
    payload: dict[str, Any] | None = None,
    content_kind: str = ContentKind.DOCUMENT.value,
    file_type: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    directory: str = DEFAULT_CONTENT_LEDGER_DIRECTORY,
    legacy_directories: tuple[str, ...] = (),
    legacy_identity_fields: tuple[str, ...] = (),
    filename_prefix: str = "content",
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    clean_identity, fingerprint = _identity(identity_key)
    clean_run_key = _required_text(run_key, "run_key")
    normalized_evidence_refs = await _verified_evidence_refs(
        evidence_refs,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    directory_record, ledger_root, entity_root = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
    )
    relative_name = f"reservations/{filename_prefix}-{fingerprint}.json"
    abs_path = os.path.join(ledger_root, relative_name)
    existing_ledger = await read_content_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        legacy_directories=legacy_directories,
        legacy_identity_fields=legacy_identity_fields,
    )
    for existing_row in existing_ledger["rows"]:
        if normalize_ledger_key(str(existing_row.get("identity_key") or "")) != normalize_ledger_key(clean_identity):
            continue
        if str(existing_row.get("run_key") or "") == clean_run_key:
            projection = None
            if os.path.isfile(abs_path):
                projection = await _sync_ledger_projection(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
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
                "entry_id": existing_row.get("entry_id"),
                "identity_key": existing_row.get("identity_key"),
                "status": existing_row.get("status"),
                "document_id": getattr(projection, "document_id", None),
            }
        return {
            "ok": False,
            "code": "content_already_reserved",
            "message": "The content identity is already reserved in this Workspace",
            "identity_key": clean_identity,
            "existing_entry_id": existing_row.get("entry_id"),
            "existing_recorded_at": existing_row.get("recorded_at"),
        }
    entry_id = generate_ulid()
    record = {
        "contract_id": CONTENT_LEDGER_CONTRACT_ID,
        "content_kind": str(content_kind or ContentKind.DOCUMENT.value),
        "file_type": file_type,
        "schema_version": 1,
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "entry_type": "reservation",
        "entry_id": entry_id,
        "run_key": clean_run_key,
        "recorded_at": _now_iso(),
        "status": "reserved",
        "identity_key": clean_identity,
        "identity_fingerprint": fingerprint,
        "payload": dict(payload or {}),
        "source_task_id": task_id,
        "source_workflow_run_id": workflow_run_id,
        "evidence_refs": normalized_evidence_refs,
    }
    created = write_immutable_json(abs_path, record)
    if not created:
        try:
            with open(abs_path, encoding="utf-8") as source:
                existing = _normalize_record(
                    json.load(source),
                    legacy_identity_fields=legacy_identity_fields,
                    workspace_id=workspace_id,
                )
        except (OSError, json.JSONDecodeError, ContentLedgerError) as exc:
            raise ContentLedgerError("ledger_unreadable", f"Ledger reservation is unreadable: {os.path.basename(abs_path)}") from exc
        if existing.get("run_key") == clean_run_key:
            projection = await _sync_ledger_projection(
                entity_id=entity_id,
                workspace_id=workspace_id,
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
                "entry_id": existing.get("entry_id"),
                "identity_key": existing.get("identity_key"),
                "status": existing.get("status"),
                "path": f"{directory_record.storage_path}/{relative_name}",
                "display_path": f"{directory_record.display_path}/{relative_name}",
                "document_id": getattr(projection, "document_id", None),
            }
        return {
            "ok": False,
            "code": "content_already_reserved",
            "message": "The content identity is already reserved in this Workspace",
            "identity_key": clean_identity,
            "existing_entry_id": existing.get("entry_id"),
            "existing_recorded_at": existing.get("recorded_at"),
        }
    projection = await _sync_ledger_projection(
        entity_id=entity_id,
        workspace_id=workspace_id,
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
        "entry_id": entry_id,
        "identity_key": clean_identity,
        "status": "reserved",
        "path": f"{directory_record.storage_path}/{relative_name}",
        "display_path": f"{directory_record.display_path}/{relative_name}",
        "document_id": getattr(projection, "document_id", None),
    }


async def record_content_event(
    *,
    entity_id: str,
    workspace_id: str,
    event: str,
    reservation_id: str,
    identity_key: str,
    payload: dict[str, Any] | None = None,
    content_kind: str = ContentKind.DOCUMENT.value,
    file_type: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    directory: str = DEFAULT_CONTENT_LEDGER_DIRECTORY,
    legacy_directories: tuple[str, ...] = (),
    legacy_identity_fields: tuple[str, ...] = (),
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    clean_identity, _ = _identity(identity_key)
    clean_event = _required_text(event, "event")
    clean_reservation = _required_text(reservation_id, "reservation_id")
    normalized_evidence_refs = await _verified_evidence_refs(
        evidence_refs,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    directory_record, ledger_root, entity_root = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
    )
    reservations, events = _read_records(
        ledger_root,
        legacy_identity_fields=legacy_identity_fields,
        workspace_id=workspace_id,
    )
    reservation = next((item for item in reservations if str(item.get("entry_id")) == clean_reservation), None)
    if reservation is None:
        # An upgraded Workspace may still have its reservation in an explicitly
        # configured legacy directory.
        legacy = await read_content_ledger(
            entity_id=entity_id,
            workspace_id=workspace_id,
            directory=directory,
            legacy_directories=legacy_directories,
            legacy_identity_fields=legacy_identity_fields,
        )
        reservation_row = next((item for item in legacy["rows"] if str(item.get("entry_id")) == clean_reservation), None)
        if reservation_row is None:
            raise ContentLedgerError("reservation_missing", "No content reservation exists for this identity")
        if normalize_ledger_key(str(reservation_row.get("identity_key") or "")) != normalize_ledger_key(clean_identity):
            raise ContentLedgerError("reservation_mismatch", "reservation_id does not match identity_key")
    elif normalize_ledger_key(str(reservation.get("identity_key") or "")) != normalize_ledger_key(clean_identity):
        raise ContentLedgerError("reservation_mismatch", "reservation_id does not match identity_key")
    relative_name = f"events/{clean_reservation}-{clean_event}.json"
    abs_path = os.path.join(ledger_root, relative_name)
    record = {
        "contract_id": CONTENT_LEDGER_CONTRACT_ID,
        "content_kind": str(content_kind or ContentKind.DOCUMENT.value),
        "file_type": file_type,
        "schema_version": 1,
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "entry_type": "event",
        "event_id": generate_ulid(),
        "reservation_id": clean_reservation,
        "recorded_at": _now_iso(),
        "status": clean_event,
        "identity_key": clean_identity,
        "payload": dict(payload or {}),
        "source_task_id": task_id,
        "source_workflow_run_id": workflow_run_id,
        "evidence_refs": normalized_evidence_refs,
    }
    created = write_immutable_json(abs_path, record)
    if not created:
        try:
            with open(abs_path, encoding="utf-8") as source:
                existing = _normalize_record(
                    json.load(source),
                    legacy_identity_fields=legacy_identity_fields,
                    workspace_id=workspace_id,
                )
        except (OSError, json.JSONDecodeError, ContentLedgerError) as exc:
            raise ContentLedgerError("ledger_unreadable", f"Ledger event is unreadable: {os.path.basename(abs_path)}") from exc
        projection = await _sync_ledger_projection(
            entity_id=entity_id,
            workspace_id=workspace_id,
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
            "event_id": existing.get("event_id"),
            "status": existing.get("status"),
            "path": f"{directory_record.storage_path}/{relative_name}",
            "display_path": f"{directory_record.display_path}/{relative_name}",
            "document_id": getattr(projection, "document_id", None),
        }
    projection = await _sync_ledger_projection(
        entity_id=entity_id,
        workspace_id=workspace_id,
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
        "event_id": record["event_id"],
        "status": clean_event,
        "path": f"{directory_record.storage_path}/{relative_name}",
        "display_path": f"{directory_record.display_path}/{relative_name}",
        "document_id": getattr(projection, "document_id", None),
    }


def content_ledger_workspace_id_for_document(document: Any) -> str | None:
    """Return the Workspace id for a generic live ledger projection."""

    metadata = getattr(document, "metadata_", None)
    if not isinstance(metadata, dict):
        return None
    template = metadata.get("blueprint_template")
    if not isinstance(template, dict) or template.get("mode") != "live_projection":
        return None
    starter_path = str(metadata.get("blueprint_starter_path") or "").strip().strip("/")
    if not starter_path or not starter_path.endswith("/ledger.md"):
        return None
    origin = metadata.get("origin")
    if not isinstance(origin, dict):
        return None
    workspace_id = str(origin.get("workspace_id") or "").strip()
    return workspace_id or None


async def render_content_ledger_markdown(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_CONTENT_LEDGER_DIRECTORY,
    legacy_directories: tuple[str, ...] = (),
    legacy_identity_fields: tuple[str, ...] = (),
) -> str:
    ledger = await read_content_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        legacy_directories=legacy_directories,
        legacy_identity_fields=legacy_identity_fields,
        recent_limit=500,
    )
    lines = [
        "# Content Ledger",
        "",
        "> Live view derived from immutable Workspace content records.",
        "",
        f"- Reserved content: {ledger['reserved_count']}",
        f"- Lifecycle events: {ledger['entry_count'] - ledger['reserved_count']}",
        "",
    ]
    if not ledger["rows"]:
        lines.extend(["No content has been reserved yet.", ""])
        return "\n".join(lines)
    lines.extend(["| Identity | Status | Recorded |", "| --- | --- | --- |"])
    for row in ledger["rows"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("identity_key") or "").replace("|", "\\|"),
                    str(row.get("status") or "reserved"),
                    str(row.get("recorded_at") or ""),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Source of truth", "", "Immutable JSON reservations and events in the configured Workspace ledger directory.", ""])
    return "\n".join(lines)
