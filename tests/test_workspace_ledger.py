from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.content_ledger import (
    CONTENT_LEDGER_CONTRACT_ID,
    ContentKind,
)
from packages.core.services.workspace_ledger import (
    LedgerStorage,
    WorkspaceLedgerError,
    ledger_key_fingerprint,
    ledger_record_files,
    normalize_ledger_key,
    validate_ledger_relationships,
    write_immutable_json,
)


@pytest.mark.asyncio
async def test_read_only_ledger_location_does_not_create_workspace_directory(tmp_path: Path) -> None:
    from packages.core.services.workspace_ledger import ensure_ledger_location

    async def resolve_directory(**_kwargs):
        return SimpleNamespace(storage_path="application-ledger")

    directory, ledger_root, _root = await ensure_ledger_location(
        entity_id="entity-1",
        workspace_id="workspace-1",
        storage=LedgerStorage(directory="application-ledger"),
        directory_resolver=resolve_directory,
        entity_root_resolver=lambda _entity_id: str(tmp_path),
        create=False,
    )

    assert directory.storage_path == "application-ledger"
    assert ledger_root == str(tmp_path / "application-ledger")
    assert not (tmp_path / "application-ledger").exists()


def test_workspace_ledger_storage_and_key_are_domain_neutral() -> None:
    storage = LedgerStorage(directory="application-ledger")

    assert storage.reservation_directory == "reservations"
    assert storage.event_directory == "events"
    assert normalize_ledger_key("  Ｆresh---Record!!! ") == "fresh record"
    assert ledger_key_fingerprint("Fresh Record") == ledger_key_fingerprint(
        " fresh---record "
    )


def test_content_contract_uses_kind_instead_of_style_specific_profile() -> None:
    assert CONTENT_LEDGER_CONTRACT_ID == "manor.content_ledger/v1"
    assert ContentKind.VIDEO == "video"
    assert ContentKind.AUDIO == "audio"


def test_workspace_ledger_writes_immutable_records_and_lists_lifecycle_files(
    tmp_path: Path,
) -> None:
    reservation = tmp_path / "reservations" / "content-key.json"
    event = tmp_path / "events" / "reservation-1-artifact_ready.json"

    assert write_immutable_json(str(reservation), {"entry_id": "reservation-1"})
    assert not write_immutable_json(str(reservation), {"entry_id": "changed"})
    assert write_immutable_json(
        str(event),
        {
            "entry_type": "lifecycle_event",
            "reservation_id": "reservation-1",
            "status": "artifact_ready",
            "identity_key": "fresh article",
        },
    )

    assert ledger_record_files(str(tmp_path)) == [str(event), str(reservation)]
    assert json.loads(reservation.read_text()) == {"entry_id": "reservation-1"}


def test_workspace_ledger_relationship_validation_is_application_agnostic() -> None:
    reservations = [{"entry_id": "reservation-1", "identity_key": "fresh article"}]
    records = [{
        "entry_type": "lifecycle_event",
        "reservation_id": "reservation-1",
        "status": "artifact_ready",
        "identity_key": "fresh article",
    }]

    validate_ledger_relationships(reservations, records)

    with pytest.raises(WorkspaceLedgerError, match="does not match"):
        validate_ledger_relationships(
            reservations,
            [{**records[0], "identity_key": "different article"}],
        )
