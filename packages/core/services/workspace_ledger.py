"""Domain-neutral append-only ledger primitives for Workspace applications.

Application contracts own record schemas and lifecycle rules. This module only
owns safe Workspace storage, immutable writes, identity helpers, and structural
relationship validation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import unicodedata
from typing import Any

from packages.core.ai.runtime.file_actions import runtime_entity_file_root
from packages.core.models.base import generate_ulid
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
)


@dataclass(frozen=True)
class LedgerStorage:
    """Workspace storage layout selected by an application contract."""

    directory: str
    reservation_directory: str = "reservations"
    event_directory: str = "events"


class WorkspaceLedgerError(ValueError):
    """Stable storage/integrity error raised by the shared ledger layer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_ledger_key(value: str) -> str:
    """Return a deterministic, human-text key for exact deduplication."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    words = "".join(
        character if character.isalnum() else " "
        for character in normalized
    )
    return " ".join(words.split())


def ledger_key_fingerprint(value: str) -> str:
    """Hash a normalized application identity key."""

    normalized = normalize_ledger_key(value)
    if not normalized:
        raise WorkspaceLedgerError(
            "invalid_ledger_key",
            "The ledger identity key must contain text",
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ledger_file_content_hash(path: str) -> str:
    """Return the SHA-256 of a committed Ledger record's exact bytes."""

    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ledger_now_iso() -> str:
    """Return one UTC timestamp format for Workspace ledgers."""

    return datetime.now(timezone.utc).isoformat()


async def ensure_ledger_location(
    *,
    entity_id: str,
    workspace_id: str,
    storage: LedgerStorage,
    directory_resolver: Callable[..., Awaitable[Any]] | None = None,
    entity_root_resolver: Callable[[str], str | None] | None = None,
    create: bool = True,
) -> tuple[Any, str, str]:
    """Resolve and create a Workspace-scoped ledger directory safely."""

    if not entity_id or not workspace_id:
        raise WorkspaceLedgerError(
            "missing_workspace_context",
            "Entity and Workspace context are required",
        )
    resolve_directory = directory_resolver or ensure_workspace_artifact_directory
    directory = await resolve_directory(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory_path=storage.directory,
    )
    resolve_entity_root = entity_root_resolver or runtime_entity_file_root
    entity_root = resolve_entity_root(entity_id)
    if not entity_root:
        raise WorkspaceLedgerError(
            "filesystem_unavailable",
            "Workspace filesystem is not enabled",
        )
    ledger_root = os.path.realpath(os.path.join(entity_root, directory.storage_path))
    root = os.path.realpath(entity_root)
    if os.path.commonpath([root, ledger_root]) != root:
        raise WorkspaceLedgerError(
            "invalid_ledger_path",
            "Ledger path escaped the Workspace filesystem",
        )
    if create:
        os.makedirs(ledger_root, exist_ok=True)
    return directory, ledger_root, root


def ledger_record_files(
    ledger_root: str,
    *,
    reservation_directory: str = "reservations",
    event_directory: str = "events",
) -> list[str]:
    """List immutable JSON records in an application's lifecycle folders."""

    paths: list[str] = []
    for directory_name in (reservation_directory, event_directory):
        directory = os.path.join(ledger_root, directory_name)
        if not os.path.isdir(directory):
            continue
        paths.extend(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".json")
        )
    return sorted(paths)


def write_immutable_json(path: str, record: dict[str, Any]) -> bool:
    """Atomically publish one JSON record, returning false on an existing path."""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    temporary = os.path.join(
        os.path.dirname(path),
        f".{os.path.basename(path)}.{generate_ulid()}.tmp",
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def validate_ledger_relationships(
    reservations: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    reservation_id_field: str = "entry_id",
    event_reservation_id_field: str = "reservation_id",
    identity_field: str = "identity_key",
    event_status_field: str = "status",
) -> None:
    """Reject orphaned, mismatched, or duplicate lifecycle records."""

    reservations_by_id: dict[str, dict[str, Any]] = {}
    for reservation in reservations:
        reservation_id = str(reservation.get(reservation_id_field) or "")
        if reservation_id in reservations_by_id:
            raise WorkspaceLedgerError(
                "ledger_relationship_invalid",
                f"Ledger contains duplicate reservation id {reservation_id}",
            )
        reservations_by_id[reservation_id] = reservation

    seen_events: set[tuple[str, str]] = set()
    for record in records:
        if not record.get(event_reservation_id_field):
            continue
        reservation_id = str(record.get(event_reservation_id_field) or "")
        reservation = reservations_by_id.get(reservation_id)
        if reservation is None:
            raise WorkspaceLedgerError(
                "ledger_relationship_invalid",
                f"Ledger event references missing reservation {reservation_id}",
            )
        if record.get(identity_field) != reservation.get(identity_field):
            raise WorkspaceLedgerError(
                "ledger_relationship_invalid",
                f"Ledger event does not match reservation {reservation_id}",
            )
        event_key = (
            reservation_id,
            str(record.get(event_status_field) or ""),
        )
        if event_key in seen_events:
            raise WorkspaceLedgerError(
                "ledger_relationship_invalid",
                "Ledger contains duplicate lifecycle events for reservation "
                f"{reservation_id}",
            )
        seen_events.add(event_key)
