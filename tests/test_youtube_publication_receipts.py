from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.models.document import Document
from packages.core.services.youtube_public_video import YouTubePublicVideoMetrics


def _module():
    return importlib.import_module(
        "packages.core.services.youtube_publication_receipts"
    )


def _metrics() -> YouTubePublicVideoMetrics:
    return YouTubePublicVideoMetrics(
        video_id="tsJff6wc2XQ",
        public_url="https://www.youtube.com/watch?v=tsJff6wc2XQ",
        title="The Two-Minute Shutdown Ritual | Stickman Productivity",
        channel_id="UCbIyc_cOTGbOyC7CaIr_ZXg",
        channel_name="Dao Simon",
        subscribers=42,
        published_at="2026-08-10T05:01:29-07:00",
        views=0,
        likes=0,
        comments=None,
        unavailable_fields=("comments",),
        collected_at="2026-08-10T10:00:00+00:00",
    )


def _build_receipt(**overrides):
    values = {
        "metrics": _metrics(),
        "expected_title": _metrics().title,
        "expected_video_id": _metrics().video_id,
        "expected_channel_name": _metrics().channel_name,
        "source_kind": "verified_backfill",
        "source_task_id": None,
        "recorded_at": "2026-08-10T18:00:00+08:00",
    }
    values.update(overrides)
    return _module().build_publication_receipt(**values)


def test_workspace_publication_requires_source_execution_provenance() -> None:
    with pytest.raises(ValueError, match="source_task_id or source_workflow_run_id"):
        _build_receipt(
            source_kind="workspace_publication",
            source_task_id=None,
        )


def test_workspace_publication_accepts_a_source_workflow_run() -> None:
    receipt = _build_receipt(
        source_kind="workspace_publication",
        source_task_id=None,
        source_workflow_run_id="01KZXAKWTHA3TD4ARPGBYGGY5X",
    )

    assert receipt["source_task_id"] is None
    assert receipt["source_workflow_run_id"] == "01KZXAKWTHA3TD4ARPGBYGGY5X"


def test_verified_backfill_cannot_claim_a_source_task() -> None:
    with pytest.raises(ValueError, match="must not claim"):
        _build_receipt(
            source_kind="verified_backfill",
            source_task_id="01KZNS6WSKRTJZ8G99DNBDDMEB",
        )


def test_builds_the_versioned_verified_backfill_contract() -> None:
    receipt = _build_receipt()

    assert receipt == {
        "schema_version": 1,
        "video_id": "tsJff6wc2XQ",
        "public_url": "https://www.youtube.com/watch?v=tsJff6wc2XQ",
        "title": "The Two-Minute Shutdown Ritual | Stickman Productivity",
        "channel_id": "UCbIyc_cOTGbOyC7CaIr_ZXg",
        "channel_name": "Dao Simon",
        "published_at": "2026-08-10T05:01:29-07:00",
        "recorded_at": "2026-08-10T18:00:00+08:00",
        "source_kind": "verified_backfill",
        "source_task_id": None,
        "source_workflow_run_id": None,
    }


def test_uses_authoritative_public_channel_when_studio_channel_is_not_supplied() -> None:
    receipt = _build_receipt(expected_channel_name="")

    assert receipt["channel_id"] == _metrics().channel_id
    assert receipt["channel_name"] == _metrics().channel_name


def test_rejects_public_identity_mismatches() -> None:
    with pytest.raises(ValueError, match="video ID"):
        _build_receipt(expected_video_id="a-different-id")

    with pytest.raises(ValueError, match="title"):
        _build_receipt(expected_title="A different Studio title")

    with pytest.raises(ValueError, match="channel"):
        _build_receipt(expected_channel_name="A different channel")


def test_validates_that_receipt_url_and_video_id_match() -> None:
    receipt = _build_receipt()
    receipt["video_id"] = "abcdefghijk"

    with pytest.raises(ValueError, match="video ID"):
        _module().validate_publication_receipt(receipt)


@pytest.mark.asyncio
async def test_writes_and_reads_the_stable_workspace_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    directory = SimpleNamespace(
        storage_path="Workspaces/_by_id/folder-1/technical",
        display_path="Workspaces/Demo/technical",
    )
    synced: list[dict] = []

    async def ensure_directory(**kwargs):
        assert kwargs == {
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "directory_path": "technical",
        }
        return directory

    def write_atomic(
        entity_id: str,
        rel_path: str,
        data: bytes,
        **kwargs,
    ) -> str:
        assert entity_id == "entity-1"
        assert kwargs["expected_size"] == len(data)
        absolute = tmp_path / rel_path
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_bytes(data)
        return str(absolute)

    async def sync_to_knowledge(**kwargs):
        synced.append(kwargs)
        return SimpleNamespace(id="document-1")

    monkeypatch.setattr(module, "ensure_workspace_artifact_directory", ensure_directory)
    monkeypatch.setattr(module, "runtime_entity_file_root", lambda entity_id: str(tmp_path))
    monkeypatch.setattr(module, "runtime_write_entity_file_atomic", write_atomic)
    monkeypatch.setattr(module, "runtime_sync_entity_file_to_knowledge", sync_to_knowledge)

    receipt = _build_receipt()
    result = await module.write_publication_receipt(
        entity_id="entity-1",
        workspace_id="workspace-1",
        receipt=receipt,
        agent_id="agent-1",
        task_id=None,
        conversation_id="conversation-1",
        user_id="user-1",
    )
    loaded = await module.read_publication_receipt(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    expected_rel_path = (
        "Workspaces/_by_id/folder-1/technical/"
        "latest-youtube-publication.json"
    )
    assert result["path"] == expected_rel_path
    assert result["display_path"] == (
        "Workspaces/Demo/technical/latest-youtube-publication.json"
    )
    assert result["document_id"] == "document-1"
    assert loaded == receipt
    assert json.loads((tmp_path / expected_rel_path).read_text()) == receipt
    assert synced[0]["workspace_id"] == "workspace-1"
    assert synced[0]["task_id"] is None
    assert synced[0]["source"] == "youtube_publication"
    assert len(synced[0]["source"]) <= Document.__table__.c.source.type.length
