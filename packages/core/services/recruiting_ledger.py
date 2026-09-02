"""Workspace Recruiting/HR Ledger contract.

The Ledger stores immutable lifecycle events for candidates, employees, and
contractors.  ``rows`` is a read-time current projection keyed by a stable,
Workspace-local ``record_key``; corrections are appended as later events.
Sensitive source material stays in Knowledge and is referenced through
verified ``evidence_refs`` instead of being copied into the Ledger payload.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
import json
import os
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
)
from packages.core.models.base import generate_ulid
from packages.core.services.ledger_evidence import (
    EvidenceReferenceError,
    LedgerEvidenceRef,
    verify_evidence_refs,
)
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
    resolve_workspace_artifact_directory,
)
from packages.core.services.workspace_ledger import (
    LedgerStorage,
    WorkspaceLedgerError,
    ensure_ledger_location,
    ledger_key_fingerprint,
    ledger_now_iso,
    ledger_record_files,
    write_immutable_json,
)


RECRUITING_LEDGER_CONTRACT_ID = "manor.recruiting_ledger/v1"
DEFAULT_RECRUITING_LEDGER_DIRECTORY = "recruiting-ledger"
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RecruitingClearField: TypeAlias = Literal[
    "display_name",
    "role_title",
    "department",
    "location",
    "manager_ref",
    "stage",
]
RECRUITING_CLEARABLE_FIELDS = (
    "display_name",
    "role_title",
    "department",
    "location",
    "manager_ref",
    "stage",
)


class RecruitingLedgerError(ValueError):
    """Stable application error returned by the Recruiting Ledger boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RecruitingSubjectType(StrEnum):
    CANDIDATE = "candidate"
    EMPLOYEE = "employee"
    CONTRACTOR = "contractor"


class RecruitingStage(StrEnum):
    SOURCED = "sourced"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    REFERENCE_CHECK = "reference_check"
    OFFER = "offer"
    HIRED = "hired"
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    LEAVE = "leave"
    OFFBOARDING = "offboarding"
    DEPARTED = "departed"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"


class RecruitingStatus(StrEnum):
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"


class RecruitingLedgerEvent(BaseModel):
    """Versioned immutable recruiting or people-lifecycle event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contract_id: Literal[RECRUITING_LEDGER_CONTRACT_ID] = RECRUITING_LEDGER_CONTRACT_ID
    schema_version: Literal[1] = 1
    event_id: NonEmptyText
    workspace_id: NonEmptyText
    entity_id: NonEmptyText
    entry_type: Literal["event"] = "event"
    record_key: NonEmptyText
    record_fingerprint: NonEmptyText
    subject_type: RecruitingSubjectType | None = None
    display_name: NonEmptyText | None = None
    role_title: NonEmptyText | None = None
    department: NonEmptyText | None = None
    location: NonEmptyText | None = None
    manager_ref: NonEmptyText | None = None
    stage: RecruitingStage | None = None
    status: RecruitingStatus | None = None
    event: NonEmptyText
    occurred_at: datetime | None = None
    recorded_at: datetime
    idempotency_key: NonEmptyText
    clear_fields: list[RecruitingClearField] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[LedgerEvidenceRef] = Field(default_factory=list)
    source_task_id: str | None = None
    source_workflow_run_id: str | None = None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RecruitingLedgerError("invalid_input", f"{field} is required")
    return text


def _safe_directory(value: str | None) -> str:
    raw = str(value or DEFAULT_RECRUITING_LEDGER_DIRECTORY).strip().strip("/")
    if not raw or raw in {".", ".."} or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise RecruitingLedgerError("invalid_ledger_path", "Ledger directory must be Workspace-relative")
    return raw


def _parse_timestamp(value: datetime | str | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecruitingLedgerError("invalid_input", f"{field} must be an ISO timestamp") from exc


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
        raise RecruitingLedgerError(exc.code, str(exc)) from exc
    except RecruitingLedgerError:
        raise
    except ValueError as exc:
        raise RecruitingLedgerError("ledger_not_installed", str(exc)) from exc


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
        created_by=agent_id or "recruiting_ledger",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_recruiting_ledger",
    )
    if not bool(getattr(projection, "synced", False)):
        raise RecruitingLedgerError(
            "knowledge_sync_failed",
            "Recruiting Ledger event committed but Knowledge projection failed",
        )
    return projection


def _parse_event(path: str, *, workspace_id: str, entity_id: str) -> RecruitingLedgerEvent:
    try:
        with open(path, encoding="utf-8") as source:
            event = RecruitingLedgerEvent.model_validate(json.load(source))
    except OSError as exc:
        raise RecruitingLedgerError(
            "filesystem_unavailable",
            f"Recruiting Ledger record could not be read: {os.path.basename(path)}",
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise RecruitingLedgerError(
            "ledger_schema_invalid",
            f"Recruiting Ledger record is invalid: {os.path.basename(path)}",
        ) from exc
    if event.workspace_id != workspace_id or event.entity_id != entity_id:
        raise RecruitingLedgerError(
            "ledger_scope_invalid",
            "Recruiting Ledger record belongs to another Workspace",
        )
    return event


def _events_revision(ledger_root: str) -> tuple[int, int, int]:
    events_dir = os.path.join(ledger_root, "events")
    try:
        stat = os.stat(events_dir)
    except FileNotFoundError:
        return (0, 0, 0)
    except OSError as exc:
        raise RecruitingLedgerError(
            "filesystem_unavailable",
            "Recruiting Ledger events could not be inspected",
        ) from exc
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


@lru_cache(maxsize=128)
def _load_events_at_revision(
    ledger_root: str,
    workspace_id: str,
    entity_id: str,
    _revision: tuple[int, int, int],
) -> tuple[RecruitingLedgerEvent, ...]:
    try:
        paths = ledger_record_files(ledger_root)
    except OSError as exc:
        raise RecruitingLedgerError(
            "filesystem_unavailable",
            "Recruiting Ledger events could not be listed",
        ) from exc
    events = [
        _parse_event(path, workspace_id=workspace_id, entity_id=entity_id)
        for path in paths
        if os.path.basename(os.path.dirname(path)) == "events"
    ]
    events.sort(key=lambda item: item.recorded_at)
    return tuple(events)


def _current_rows(events: list[RecruitingLedgerEvent]) -> list[dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda item: item.recorded_at):
        row = current.setdefault(
            event.record_fingerprint,
            {
                "record_key": event.record_key,
                "record_fingerprint": event.record_fingerprint,
                "workspace_id": event.workspace_id,
                "entity_id": event.entity_id,
                "subject_type": event.subject_type or RecruitingSubjectType.CANDIDATE,
                "display_name": event.display_name,
                "role_title": event.role_title,
                "department": event.department,
                "location": event.location,
                "manager_ref": event.manager_ref,
                "stage": event.stage,
                "status": event.status or RecruitingStatus.ACTIVE,
                "payload": {},
                "evidence_refs": [],
                "source_task_id": event.source_task_id,
                "source_workflow_run_id": event.source_workflow_run_id,
            },
        )
        for field in (
            "subject_type", "display_name", "role_title", "department", "location",
            "manager_ref", "stage", "status", "source_task_id", "source_workflow_run_id",
        ):
            value = getattr(event, field)
            if value is not None:
                row[field] = value
        for field in event.clear_fields:
            row[field] = None
        row["payload"].update(event.payload)
        if event.evidence_refs:
            row["evidence_refs"] = [ref.model_dump(mode="json") for ref in event.evidence_refs]
        row.update({
            "event_id": event.event_id,
            "event": event.event,
            "recorded_at": event.recorded_at.isoformat(),
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        })
    return sorted(current.values(), key=lambda item: str(item.get("recorded_at") or ""), reverse=True)


async def read_recruiting_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    directory: str = DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    recent_limit: int | None = 100,
    create_location: bool = True,
) -> dict[str, Any]:
    """Read the current people projection and immutable lifecycle events."""

    _, ledger_root, _ = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory=directory,
        create=create_location,
    )
    revision = await asyncio.to_thread(_events_revision, ledger_root)
    events = list(await asyncio.to_thread(
        _load_events_at_revision,
        ledger_root,
        workspace_id,
        entity_id,
        revision,
    ))
    visible = events if recent_limit is None or int(recent_limit) <= 0 else events[-max(1, min(int(recent_limit), 500)):]
    rows = _current_rows(events)
    return {
        "contract_id": RECRUITING_LEDGER_CONTRACT_ID,
        "schema_version": 1,
        "entry_count": len(events),
        "record_count": len(rows),
        "rows": rows,
        "entries": [event.model_dump(mode="json") for event in visible],
        "recent_entries": [event.model_dump(mode="json") for event in visible],
        "run_key": generate_ulid(),
    }


def _idempotency_payload(event: RecruitingLedgerEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json", exclude={"event_id", "recorded_at"})
    for ref in payload.get("evidence_refs") or []:
        ref.pop("verified_at", None)
    return payload


async def record_recruiting_event(
    *,
    entity_id: str,
    workspace_id: str,
    record_key: str,
    event: str,
    idempotency_key: str,
    subject_type: str | None = None,
    display_name: str | None = None,
    role_title: str | None = None,
    department: str | None = None,
    location: str | None = None,
    manager_ref: str | None = None,
    stage: str | None = None,
    status: str | None = None,
    clear_fields: list[str] | tuple[str, ...] | None = None,
    occurred_at: datetime | str | None = None,
    payload: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    directory: str = DEFAULT_RECRUITING_LEDGER_DIRECTORY,
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    """Append one Recruiting/HR event, idempotent by caller-provided key."""

    clean_entity = _required_text(entity_id, "entity_id")
    clean_workspace = _required_text(workspace_id, "workspace_id")
    clean_record = _required_text(record_key, "record_key")
    clean_event = _required_text(event, "event")
    clean_idempotency = _required_text(idempotency_key, "idempotency_key")
    clean_clear_fields = list(dict.fromkeys(
        str(field or "").strip()
        for field in (clear_fields or [])
        if str(field or "").strip()
    ))
    unsupported_clear_fields = sorted(
        set(clean_clear_fields) - set(RECRUITING_CLEARABLE_FIELDS)
    )
    if unsupported_clear_fields:
        raise RecruitingLedgerError(
            "invalid_input",
            f"clear_fields contains unsupported fields: {', '.join(unsupported_clear_fields)}",
        )
    supplied_values = {
        "display_name": display_name,
        "role_title": role_title,
        "department": department,
        "location": location,
        "manager_ref": manager_ref,
        "stage": stage,
    }
    conflicting_fields = sorted(
        field for field in clean_clear_fields if supplied_values[field] is not None
    )
    if conflicting_fields:
        raise RecruitingLedgerError(
            "invalid_input",
            f"Fields cannot be set and cleared in the same event: {', '.join(conflicting_fields)}",
        )
    try:
        normalized_refs = await verify_evidence_refs(
            evidence_refs,
            entity_id=clean_entity,
            workspace_id=clean_workspace,
        )
        entry = RecruitingLedgerEvent(
            event_id=generate_ulid(),
            workspace_id=clean_workspace,
            entity_id=clean_entity,
            record_key=clean_record,
            record_fingerprint=ledger_key_fingerprint(clean_record),
            subject_type=(
                RecruitingSubjectType(str(subject_type).strip().casefold())
                if subject_type
                else None
            ),
            display_name=display_name,
            role_title=role_title,
            department=department,
            location=location,
            manager_ref=manager_ref,
            stage=RecruitingStage(str(stage).strip().casefold()) if stage else None,
            status=(
                RecruitingStatus(str(status).strip().casefold())
                if status
                else None
            ),
            event=clean_event,
            occurred_at=_parse_timestamp(occurred_at, "occurred_at"),
            recorded_at=datetime.fromisoformat(ledger_now_iso()),
            idempotency_key=clean_idempotency,
            clear_fields=clean_clear_fields,
            payload=payload or {},
            evidence_refs=normalized_refs,
            source_task_id=task_id,
            source_workflow_run_id=workflow_run_id,
        )
    except EvidenceReferenceError as exc:
        raise RecruitingLedgerError(exc.code, str(exc)) from exc
    except WorkspaceLedgerError as exc:
        raise RecruitingLedgerError(exc.code, str(exc)) from exc
    except ValueError as exc:
        raise RecruitingLedgerError("ledger_schema_invalid", str(exc)) from exc

    directory_record, ledger_root, entity_root = await _ledger_location(
        entity_id=clean_entity,
        workspace_id=clean_workspace,
        directory=directory,
    )
    filename = f"recruiting-{ledger_key_fingerprint(clean_idempotency)}.json"
    relative_name = f"events/{filename}"
    abs_path = os.path.join(ledger_root, relative_name)
    # The idempotency key is part of the immutable filename.  Resolve that
    # exact record instead of scanning the entire event directory on every
    # retry; the atomic write below still closes the concurrent-writer race.
    if os.path.isfile(abs_path):
        prior = _parse_event(
            abs_path,
            workspace_id=clean_workspace,
            entity_id=clean_entity,
        )
        if prior.idempotency_key != clean_idempotency:
            raise RecruitingLedgerError(
                "idempotency_conflict",
                "The recruiting record path is already occupied",
            )
        if _idempotency_payload(prior) != _idempotency_payload(entry):
            raise RecruitingLedgerError(
                "idempotency_conflict",
                "The idempotency_key already exists with different recruiting data",
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
            "event_id": prior.event_id,
            "status": prior.status,
            "document_id": getattr(projection, "document_id", None),
        }

    created = write_immutable_json(abs_path, entry.model_dump(mode="json"))
    if not created:
        prior = _parse_event(abs_path, workspace_id=clean_workspace, entity_id=clean_entity)
        if (
            prior.idempotency_key != clean_idempotency
            or _idempotency_payload(prior) != _idempotency_payload(entry)
        ):
            raise RecruitingLedgerError("idempotency_conflict", "The recruiting record path is already occupied")
        entry = prior
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
        "idempotent": not created,
        "event_id": entry.event_id,
        "status": entry.status,
        "path": f"{directory_record.storage_path}/{relative_name}",
        "display_path": f"{directory_record.display_path}/{relative_name}",
        "document_id": getattr(projection, "document_id", None),
    }


__all__ = [
    "DEFAULT_RECRUITING_LEDGER_DIRECTORY",
    "RECRUITING_LEDGER_CONTRACT_ID",
    "RECRUITING_CLEARABLE_FIELDS",
    "RecruitingLedgerError",
    "RecruitingLedgerEvent",
    "RecruitingStage",
    "RecruitingStatus",
    "RecruitingSubjectType",
    "read_recruiting_ledger",
    "record_recruiting_event",
]
