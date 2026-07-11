from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.config import get_settings
from packages.core.models.document import VectorStatus
from packages.core.services import document_file_repair


@pytest.fixture
def fs_settings(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    return settings


@pytest.mark.asyncio
async def test_repair_document_restores_missing_file_from_media_job(fs_settings, monkeypatch):
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="clips/out.mp4",
        file_url="",
        file_size=None,
        metadata_={},
        vector_status=VectorStatus.READY,
    )
    job = SimpleNamespace(id="job_1", source_url="https://provider.test/video.mp4", file_size=5)
    report = document_file_repair.DocumentFileRepairReport()

    async def fake_find(_db, _doc):
        return job

    async def fake_download(_url):
        return b"video"

    monkeypatch.setattr(document_file_repair, "_find_recoverable_media_job", fake_find)
    monkeypatch.setattr(document_file_repair, "_download_repair_source", fake_download)

    await document_file_repair._repair_document_if_missing(None, doc, report, dry_run=False)

    restored = fs_settings.MANOR_FS_ROOT + "/ent_1/clips/out.mp4"
    assert open(restored, "rb").read() == b"video"
    assert doc.file_size == 5
    assert doc.metadata_["file_integrity"]["status"] == "ok"
    assert doc.metadata_["file_integrity"]["restored_from_media_job_id"] == "job_1"
    assert report.missing == 1
    assert report.restored == 1


@pytest.mark.asyncio
async def test_repair_document_skips_missing_file_with_external_url(fs_settings):
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="clips/out.mp4",
        file_url="https://cdn.test/out.mp4",
        metadata_={},
        vector_status=VectorStatus.READY,
    )
    report = document_file_repair.DocumentFileRepairReport()

    await document_file_repair._repair_document_if_missing(None, doc, report, dry_run=False)

    assert report.skipped == 1
    assert report.missing == 0


@pytest.mark.asyncio
async def test_repair_document_records_missing_without_failing_by_default(fs_settings, monkeypatch):
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/missing.md",
        file_url="",
        metadata_={},
        vector_status=VectorStatus.READY,
    )
    report = document_file_repair.DocumentFileRepairReport()

    async def fake_find(_db, _doc):
        return None

    monkeypatch.setattr(document_file_repair, "_find_recoverable_media_job", fake_find)

    await document_file_repair._repair_document_if_missing(None, doc, report, dry_run=False)

    assert doc.vector_status == VectorStatus.READY
    assert doc.metadata_["file_integrity"]["status"] == "missing"
    assert report.missing == 1
    assert report.marked_missing == 1
    assert report.marked_failed == 0


@pytest.mark.asyncio
async def test_repair_document_requeues_skipped_doc_when_file_reappears(fs_settings):
    file_path = fs_settings.MANOR_FS_ROOT + "/ent_1/docs/restored.md"
    import os

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("restored")

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/restored.md",
        file_url="",
        metadata_={"file_integrity": {"status": "missing"}},
        vector_status=VectorStatus.SKIPPED,
    )
    report = document_file_repair.DocumentFileRepairReport()

    await document_file_repair._repair_document_if_missing(None, doc, report, dry_run=False)

    assert doc.vector_status == VectorStatus.PENDING
    assert doc.metadata_["file_integrity"]["status"] == "ok"
    assert report.requeued == 1


@pytest.mark.asyncio
async def test_repair_document_heal_existing_only_skips_missing_without_mutation(fs_settings, monkeypatch):
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/missing.md",
        file_url="",
        metadata_={"file_integrity": {"status": "missing", "recoverable": False}},
        vector_status=VectorStatus.FAILED,
    )
    report = document_file_repair.DocumentFileRepairReport()

    async def fail_if_called(_db, _doc):
        raise AssertionError("safe stale heal must not try provider repair for missing files")

    monkeypatch.setattr(document_file_repair, "_find_recoverable_media_job", fail_if_called)

    await document_file_repair._repair_document_if_missing(
        None,
        doc,
        report,
        dry_run=False,
        heal_existing_only=True,
    )

    assert doc.vector_status == VectorStatus.FAILED
    assert doc.metadata_["file_integrity"]["status"] == "missing"
    assert report.skipped == 1
    assert report.missing == 0
    assert report.marked_missing == 0
