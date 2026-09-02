from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from packages.core.services import recruiting_ledger


@pytest.fixture
def recruiting_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = SimpleNamespace(storage_path="recruiting-ledger", display_path="recruiting-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id="recruiting-document-1")

    monkeypatch.setattr(recruiting_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(recruiting_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    return tmp_path


@pytest.mark.asyncio
async def test_recruiting_events_are_idempotent_and_project_current_stage(recruiting_runtime: Path) -> None:
    first = await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate:42:role:designer",
        display_name="Candidate 42",
        role_title="Product Designer",
        stage="screening",
        status="active",
        event="screen_completed",
        idempotency_key="candidate-42-screen",
        payload={"score": 4},
    )
    repeated = await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate:42:role:designer",
        display_name="Candidate 42",
        role_title="Product Designer",
        stage="screening",
        status="active",
        event="screen_completed",
        idempotency_key="candidate-42-screen",
        payload={"score": 4},
    )
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate:42:role:designer",
        display_name="Candidate 42",
        role_title="Product Designer",
        stage="offer",
        status="active",
        event="offer_sent",
        idempotency_key="candidate-42-offer",
        payload={"offer_approved": True},
    )

    assert first["document_id"] == "recruiting-document-1"
    assert repeated["idempotent"] is True
    assert repeated["event_id"] == first["event_id"]

    ledger = await recruiting_ledger.read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert ledger["entry_count"] == 2
    assert ledger["record_count"] == 1
    assert ledger["rows"][0]["stage"] == "offer"
    assert ledger["rows"][0]["payload"] == {"score": 4, "offer_approved": True}
    assert ledger["rows"][0]["recorded_at"].endswith("+00:00")
    json.dumps(ledger)


@pytest.mark.asyncio
async def test_recruiting_partial_event_preserves_current_subject_and_status(
    recruiting_runtime: Path,
) -> None:
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="employee-1",
        subject_type="employee",
        stage="active",
        status="hired",
        event="hired",
        idempotency_key="employee-1-hired",
    )
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="employee-1",
        role_title="Staff Engineer",
        event="role_changed",
        idempotency_key="employee-1-role-changed",
    )

    ledger = await recruiting_ledger.read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["rows"][0]["subject_type"] == "employee"
    assert ledger["rows"][0]["status"] == "hired"
    assert ledger["rows"][0]["role_title"] == "Staff Engineer"


@pytest.mark.asyncio
async def test_recruiting_clear_fields_append_a_projection_correction(
    recruiting_runtime: Path,
) -> None:
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="employee-1",
        subject_type="employee",
        role_title="Staff Engineer",
        department="Engineering",
        location="Remote",
        manager_ref="manager-1",
        stage="active",
        status="hired",
        event="hired",
        idempotency_key="employee-1-hired",
    )
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="employee-1",
        event="assignment_cleared",
        idempotency_key="employee-1-assignment-cleared",
        clear_fields=["department", "location", "manager_ref"],
    )

    ledger = await recruiting_ledger.read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    row = ledger["rows"][0]
    assert row["department"] is None
    assert row["location"] is None
    assert row["manager_ref"] is None
    assert row["role_title"] == "Staff Engineer"
    assert row["status"] == "hired"
    assert ledger["entries"][-1]["clear_fields"] == [
        "department",
        "location",
        "manager_ref",
    ]


@pytest.mark.asyncio
async def test_recruiting_clear_fields_validate_supported_and_conflicting_fields(
    recruiting_runtime: Path,
) -> None:
    with pytest.raises(recruiting_ledger.RecruitingLedgerError) as unsupported:
        await recruiting_ledger.record_recruiting_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            record_key="employee-1",
            event="invalid_clear",
            idempotency_key="employee-1-invalid-clear",
            clear_fields=["status"],
        )
    assert unsupported.value.code == "invalid_input"

    with pytest.raises(recruiting_ledger.RecruitingLedgerError) as conflict:
        await recruiting_ledger.record_recruiting_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            record_key="employee-1",
            location="New York",
            event="conflicting_clear",
            idempotency_key="employee-1-conflicting-clear",
            clear_fields=["location"],
        )
    assert conflict.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_recruiting_stage_is_validated(recruiting_runtime: Path) -> None:
    with pytest.raises(recruiting_ledger.RecruitingLedgerError, match="not a valid"):
        await recruiting_ledger.record_recruiting_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            record_key="candidate-1",
            stage="invented_stage",
            event="note",
            idempotency_key="note-1",
        )


@pytest.mark.asyncio
async def test_recruiting_idempotency_retry_reads_only_the_keyed_record(
    recruiting_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate-1",
        stage="screening",
        event="screen_completed",
        idempotency_key="candidate-1-screen",
    )

    async def scan_must_not_run(**_kwargs):
        raise AssertionError("idempotent retry must not scan the full ledger")

    monkeypatch.setattr(recruiting_ledger, "read_recruiting_ledger", scan_must_not_run)
    repeated = await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate-1",
        stage="screening",
        event="screen_completed",
        idempotency_key="candidate-1-screen",
    )

    assert repeated["idempotent"] is True


@pytest.mark.asyncio
async def test_recruiting_projection_is_cached_and_loaded_off_event_loop(
    recruiting_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await recruiting_ledger.record_recruiting_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        record_key="candidate-1",
        stage="screening",
        event="screen_completed",
        idempotency_key="candidate-1-screen",
    )
    recruiting_ledger._load_events_at_revision.cache_clear()
    original = recruiting_ledger._parse_event
    thread_ids: list[int] = []

    def tracked_parse(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(recruiting_ledger, "_parse_event", tracked_parse)
    main_thread_id = threading.get_ident()

    await recruiting_ledger.read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    await recruiting_ledger.read_recruiting_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert len(thread_ids) == 1
    assert thread_ids[0] != main_thread_id
