from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services import content_ledger


@pytest.mark.asyncio
async def test_content_ledger_is_workspace_and_file_type_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = SimpleNamespace(storage_path="content-ledger", display_path="content-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id="document-1")

    monkeypatch.setattr(content_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(content_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)

    reservation = await content_ledger.reserve_content(
        entity_id="entity-1",
        workspace_id="workspace-1",
        run_key="run-1",
        identity_key="A generated video",
        content_kind="video",
        file_type="video/mp4",
        payload={"title": "Example"},
    )
    event = await content_ledger.record_content_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        event="video_ready",
        reservation_id=reservation["entry_id"],
        identity_key="A generated video",
        content_kind="video",
        file_type="video/mp4",
        payload={"qa_status": "passed"},
    )

    assert reservation["ok"] is True
    assert event["status"] == "video_ready"
    stored = await content_ledger.read_content_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        legacy_directories=(),
    )
    assert stored["rows"][0]["identity_key"] == "A generated video"
    assert stored["rows"][0]["status"] == "video_ready"


@pytest.mark.asyncio
async def test_legacy_record_shape_requires_blueprint_declared_identity_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = SimpleNamespace(storage_path="content-ledger", display_path="content-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    monkeypatch.setattr(content_ledger, "ensure_ledger_location", resolve_location)
    legacy_root = tmp_path / "legacy"
    (legacy_root / "reservations").mkdir(parents=True)
    (legacy_root / "reservations" / "legacy.json").write_text(
        json.dumps(
            {
                "entry_type": "legacy_reservation",
                "entry_id": "entry-1",
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "run_key": "run-1",
                "selected_topic": "A legacy identity",
                "entity_id": "entity-1",
                "workspace_id": "workspace-1",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(content_ledger.ContentLedgerError, match="identity_key"):
        await content_ledger.read_content_ledger(
            entity_id="entity-1",
            workspace_id="workspace-1",
            directory="content-ledger",
            legacy_directories=("legacy",),
        )

    migrated = await content_ledger.read_content_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
        directory="content-ledger",
        legacy_directories=("legacy",),
        legacy_identity_fields=("selected_topic",),
    )
    assert migrated["used_keys"] == ["A legacy identity"]
    assert "used_topics" not in migrated


@pytest.mark.asyncio
async def test_content_ledger_keeps_workspace_scoped_knowledge_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = SimpleNamespace(storage_path="content-ledger", display_path="content-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id="ledger-document-1")

    monkeypatch.setattr(content_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(content_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    evidence = {
        "workspace_id": "workspace-1",
        "role": "receipt",
        "document_id": "receipt-1",
        "version_number": 2,
        "sha256": "c" * 64,
    }
    reservation = await content_ledger.reserve_content(
        entity_id="entity-1",
        workspace_id="workspace-1",
        run_key="run-evidence",
        identity_key="receipt-backed expense",
        content_kind="document",
        evidence_refs=[evidence],
    )
    ledger = await content_ledger.read_content_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert reservation["ok"] is True
    assert ledger["rows"][0]["evidence_refs"][0]["version_number"] == 2

    with pytest.raises(content_ledger.ContentLedgerError, match="another Workspace"):
        await content_ledger.record_content_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            event="document_verified",
            reservation_id=reservation["entry_id"],
            identity_key="receipt-backed expense",
            evidence_refs=[{**evidence, "workspace_id": "workspace-2"}],
        )


@pytest.mark.asyncio
async def test_content_ledger_retry_reprojects_after_knowledge_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = SimpleNamespace(storage_path="content-ledger", display_path="content-ledger")
    calls = 0

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(synced=False)
        return SimpleNamespace(synced=True, document_id="content-document-retry")

    monkeypatch.setattr(content_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(content_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    kwargs = {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "run_key": "content-sync-retry-1",
        "identity_key": "content-sync-retry",
        "content_kind": "document",
    }
    with pytest.raises(content_ledger.ContentLedgerError, match="projection failed"):
        await content_ledger.reserve_content(**kwargs)

    retried = await content_ledger.reserve_content(**kwargs)
    assert retried["idempotent"] is True
    assert retried["document_id"] == "content-document-retry"
    assert calls == 2
