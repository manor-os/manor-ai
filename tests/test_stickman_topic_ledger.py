from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest


def _module():
    return importlib.import_module("packages.core.services.stickman_topic_ledger")


def _candidates() -> list[dict[str, str]]:
    return [
        {
            "topic": f"Fresh topic {index}",
            "hook": f"Hook {index}",
            "audience": "Solo operators",
            "single_takeaway": f"Takeaway {index}",
            "fit_reason": "Fits the Workspace",
        }
        for index in range(1, 6)
    ]


@pytest.mark.asyncio
async def test_topic_ledger_is_append_only_and_rejects_normalized_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    directory = SimpleNamespace(
        storage_path="Workspaces/_by_id/folder-1/topic-ledger",
        display_path="Workspaces/Demo/topic-ledger",
    )
    synced: list[str] = []

    async def ensure_directory(**kwargs):
        assert kwargs == {
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "directory_path": "topic-ledger",
        }
        return directory

    async def sync_to_knowledge(**kwargs):
        synced.append(kwargs["abs_path"])
        return SimpleNamespace(
            synced=True,
            document_id=f"document-{len(synced)}",
            reason=None,
        )

    monkeypatch.setattr(module, "ensure_workspace_artifact_directory", ensure_directory)
    monkeypatch.setattr(module, "resolve_workspace_artifact_directory", ensure_directory)
    monkeypatch.setattr(module, "runtime_entity_file_root", lambda entity_id: str(tmp_path))
    monkeypatch.setattr(module, "runtime_sync_entity_file_to_knowledge", sync_to_knowledge)

    reservation = await module.reserve_topic(
        entity_id="entity-1",
        workspace_id="workspace-1",
        run_key="01KZWLEDGERRUN000000000001",
        candidates=_candidates(),
        selected_topic="Fresh topic 1",
        youtube_title="A fresh title",
        youtube_description="A fresh description",
        selection_reason="Strongest fit",
        content_direction="productivity",
        agent_id="agent-1",
        task_id="task-1",
        conversation_id="conversation-1",
        user_id="user-1",
    )

    assert reservation["ok"] is True
    assert reservation["status"] == "reserved"
    assert reservation["document_id"] == "document-1"

    idempotent = await module.reserve_topic(
        entity_id="entity-1",
        workspace_id="workspace-1",
        run_key="01KZWLEDGERRUN000000000001",
        candidates=_candidates(),
        selected_topic="Fresh topic 1",
        youtube_title="A fresh title",
        youtube_description="A fresh description",
        selection_reason="Strongest fit",
    )
    assert idempotent["ok"] is True
    assert idempotent["idempotent"] is True
    assert idempotent["entry_id"] == reservation["entry_id"]

    duplicate_candidates = _candidates()
    duplicate_candidates[0]["topic"] = "  FRESH---TOPIC 1!!! "
    duplicate = await module.reserve_topic(
        entity_id="entity-1",
        workspace_id="workspace-1",
        run_key="01KZWLEDGERRUN000000000002",
        candidates=duplicate_candidates,
        selected_topic="FRESH---TOPIC 1!!!",
        youtube_title="Duplicate title",
        youtube_description="Duplicate description",
        selection_reason="Should be blocked",
    )
    assert duplicate == {
        "ok": False,
        "code": "topic_already_used",
        "message": "The selected Topic is already reserved or used in this Workspace",
        "selected_topic": "FRESH---TOPIC 1!!!",
        "existing_entry_id": reservation["entry_id"],
        "existing_recorded_at": duplicate["existing_recorded_at"],
    }

    video_event = await module.record_topic_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        event="video_ready",
        reservation_id=reservation["entry_id"],
        selected_topic="Fresh topic 1",
        details={"video_source": "final.mp4", "qa_status": "ready"},
    )
    youtube_event = await module.record_topic_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        event="youtube_saved",
        reservation_id=reservation["entry_id"],
        selected_topic="Fresh topic 1",
        details={
            "success": True,
            "video_id": "video-1",
            "watch_url": "https://www.youtube.com/watch?v=video-1",
            "visibility": "public",
        },
    )
    backfill = await module.backfill_used_topic(
        entity_id="entity-1",
        workspace_id="workspace-1",
        selected_topic="A verified historical Topic",
        evidence={
            "source_artifacts": ["publication-receipts/historical.md"],
            "reason": "Predates the Topic Ledger",
        },
    )
    history = await module.read_topic_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    publications = await module.list_saved_youtube_publications(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert video_event["status"] == "video_ready"
    assert youtube_event["status"] == "youtube_saved"
    assert backfill["status"] == "used_backfill"
    assert set(history["used_topics"]) == {
        "Fresh topic 1",
        "A verified historical Topic",
    }
    assert history["used_topic_count"] == 2
    assert history["entry_count"] == 4
    assert history["video_ready_count"] == 1
    assert history["youtube_saved_count"] == 1
    topic_rows = {row["selected_topic"]: row for row in history["topics"]}
    assert topic_rows["Fresh topic 1"]["status"] == "youtube_saved"
    assert topic_rows["Fresh topic 1"]["video_source"] == "final.mp4"
    assert topic_rows["Fresh topic 1"]["watch_url"] == (
        "https://www.youtube.com/watch?v=video-1"
    )
    assert topic_rows["A verified historical Topic"]["status"] == "used_backfill"
    assert len(publications) == 1
    assert publications[0]["video_id"] == "video-1"
    assert publications[0]["watch_url"] == (
        "https://www.youtube.com/watch?v=video-1"
    )
    assert publications[0]["recorded_at"]
    assert publications[0]["source_task_id"] is None
    assert {entry["status"] for entry in history["recent_entries"]} == {
        "reserved",
        "used_backfill",
        "video_ready",
        "youtube_saved",
    }
    assert len(synced) == 4

    reservation_file = tmp_path / reservation["path"]
    stored = json.loads(reservation_file.read_text())
    assert stored["candidates"] == _candidates()
    assert stored["selected_topic"] == "Fresh topic 1"

    markdown = await module.render_topic_ledger_markdown(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert "# Stickman Topic & Video Ledger" in markdown
    assert "| Topic | Video | Status | Created |" in markdown
    assert "Fresh topic 1" in markdown
    assert "YouTube saved · Public" in markdown
    assert "[Watch on YouTube](https://www.youtube.com/watch?v=video-1)" in markdown
    assert "Topics created or reserved: 2" in markdown


def test_live_ledger_document_requires_exact_blueprint_provenance() -> None:
    module = _module()
    matching = SimpleNamespace(metadata_={
        "blueprint_knowledge_pack_slug": "solo-stickman-studio-ops",
        "blueprint_starter_path": "topic-ledger/ledger.md",
        "origin": {"workspace_id": "workspace-1"},
    })
    wrong_document = SimpleNamespace(metadata_={
        **matching.metadata_,
        "blueprint_starter_path": "topic-ledger/README.md",
    })
    explicit_template = SimpleNamespace(metadata_={
        **matching.metadata_,
        "blueprint_template": {
            "id": "stickman-topic-video-ledger",
            "mode": "live_projection",
            "renderer": "stickman_topic_video_ledger",
            "version": 1,
        },
    })
    wrong_renderer = SimpleNamespace(metadata_={
        **explicit_template.metadata_,
        "blueprint_template": {
            **explicit_template.metadata_["blueprint_template"],
            "renderer": "another_renderer",
        },
    })

    assert module.topic_ledger_workspace_id_for_document(matching) == "workspace-1"
    assert module.topic_ledger_workspace_id_for_document(explicit_template) == "workspace-1"
    assert module.topic_ledger_workspace_id_for_document(wrong_document) is None
    assert module.topic_ledger_workspace_id_for_document(wrong_renderer) is None


@pytest.mark.asyncio
async def test_backfill_requires_durable_source_evidence() -> None:
    module = _module()

    with pytest.raises(module.StickmanTopicLedgerError, match="source_artifacts"):
        await module.backfill_used_topic(
            entity_id="entity-1",
            workspace_id="workspace-1",
            selected_topic="Unsupported historical claim",
            evidence={"reason": "No source"},
        )


def test_topic_normalization_handles_case_punctuation_and_unicode_width() -> None:
    module = _module()

    assert module.normalize_topic("  FRESH---Topic 1!!! ") == "fresh topic 1"
    assert module.normalize_topic("Ｆｒｅｓｈ　Ｔｏｐｉｃ") == "fresh topic"


def test_exclusive_record_is_complete_under_concurrent_writers(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "reservations" / "topic-same.json"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda index: module._exclusive_json_write(
                    str(target),
                    {"writer": index, "payload": "complete" * 100},
                ),
                range(32),
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == 31
    assert json.loads(target.read_text())["payload"] == "complete" * 100
    assert list(target.parent.glob(".*.tmp")) == []
