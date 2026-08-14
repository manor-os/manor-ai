from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.services import artifact_knowledge


@pytest.mark.asyncio
async def test_local_artifact_is_promoted_to_a_document(tmp_path, monkeypatch):
    entity_id = "ent_1"
    target = tmp_path / "Workspaces" / "Demo" / "final report.md"
    target.parent.mkdir(parents=True)
    target.write_text("done", encoding="utf-8")
    calls = []

    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _entity_id: str(tmp_path))

    async def fake_sync(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    monkeypatch.setattr(artifact_knowledge, "sync_file_to_knowledge", fake_sync)

    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id=entity_id,
        workspace_id="ws_1",
        task_id="task_1",
        refs=[{
            "type": "file",
            "url": f"/api/v1/fs/{entity_id}/Workspaces/Demo/final%20report.md",
        }],
    )

    assert projection.failures == []
    assert projection.refs[0]["fs_path"] == "Workspaces/Demo/final report.md"
    assert projection.refs[0]["document_id"] == "doc_1"
    assert projection.refs[0]["viewer_url"] == "/viewer/doc_1"
    assert projection.knowledge_artifacts == [{
        "type": "file",
        "name": "final report.md",
        "document_id": "doc_1",
        "viewer_url": "/viewer/doc_1",
        "markdown_link": "[final report.md](/viewer/doc_1)",
        "fs_path": "Workspaces/Demo/final report.md",
    }]
    assert calls[0]["force"] is True
    assert calls[0]["workspace_id"] == "ws_1"
    assert calls[0]["task_id"] == "task_1"


@pytest.mark.asyncio
async def test_claimed_local_artifact_without_a_file_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _entity_id: str(tmp_path))

    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id="ent_1",
        refs=[{"type": "file", "fs_path": "Workspaces/Demo/missing.pdf"}],
    )

    assert projection.knowledge_artifacts == []
    assert projection.failures == [{
        "fs_path": "Workspaces/Demo/missing.pdf",
        "reason": "not_file",
    }]


@pytest.mark.asyncio
async def test_external_url_is_not_misread_as_an_entity_file(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _entity_id: str(tmp_path))

    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id="ent_1",
        refs=[{"type": "url", "url": "https://example.test/report.pdf"}],
    )

    assert projection.failures == []
    assert projection.knowledge_artifacts == []
    assert projection.refs[0]["url"] == "https://example.test/report.pdf"


def test_projection_is_attached_as_the_canonical_result_handle():
    projection = artifact_knowledge.ArtifactKnowledgeProjection(
        refs=[],
        failures=[],
        knowledge_artifacts=[{
            "type": "file",
            "name": "report.md",
            "document_id": "doc_1",
            "viewer_url": "/viewer/doc_1",
            "markdown_link": "[report.md](/viewer/doc_1)",
            "fs_path": "Workspaces/Demo/report.md",
        }],
    )

    result = artifact_knowledge.attach_knowledge_artifacts(
        {"written": True, "fs_path": "Workspaces/Demo/report.md"},
        projection,
    )

    assert result["document_id"] == "doc_1"
    assert result["viewer_url"] == "/viewer/doc_1"
    assert result["markdown_link"] == "[report.md](/viewer/doc_1)"
    assert result["knowledge_artifacts"] == projection.knowledge_artifacts


def test_executor_prefers_canonical_knowledge_refs_for_multi_file_results():
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result({
        "created": True,
        "files": [
            {"name": "one.md", "fs_path": "Workspaces/W/one.md"},
            {"name": "two.md", "fs_path": "Workspaces/W/two.md"},
        ],
        "knowledge_artifacts": [
            {"type": "file", "name": "one.md", "fs_path": "Workspaces/W/one.md", "document_id": "doc_one"},
            {"type": "file", "name": "two.md", "fs_path": "Workspaces/W/two.md", "document_id": "doc_two"},
        ],
    })

    assert [(ref["fs_path"], ref["document_id"]) for ref in refs] == [
        ("Workspaces/W/one.md", "doc_one"),
        ("Workspaces/W/two.md", "doc_two"),
    ]


@pytest.mark.asyncio
async def test_user_facing_write_file_cannot_disable_knowledge_sync(tmp_path, monkeypatch):
    import json

    from packages.core.ai.tools import file_tools
    from packages.core.config import get_settings

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    sync_call = {}

    async def allow_write(**_kwargs):
        return None

    async def sync_file(**kwargs):
        sync_call.update(kwargs)
        return SimpleNamespace(synced=True, document_id="doc_write", reason=None)

    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_write)
    monkeypatch.setattr(file_tools, "runtime_sync_entity_file_to_knowledge", sync_file)
    try:
        (tmp_path / "ent_1").mkdir()
        result = json.loads(await file_tools._write_file(
            entity_id="ent_1",
            path="deliverable.md",
            content="# Done\n",
            save_to_knowledge=False,
        ))
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root

    assert sync_call["force"] is True
    assert result["document_id"] == "doc_write"
    assert result["viewer_url"] == "/viewer/doc_write"
    assert "save_to_knowledge" not in (
        file_tools.WRITE_FILE_SCHEMA["function"]["parameters"]["properties"]
    )


def test_completed_task_output_detects_legacy_files_without_document_ids():
    from packages.core.services.task_execution_reconcile import (
        _has_unprojected_local_artifacts,
    )

    assert _has_unprojected_local_artifacts({
        "files": [{"fs_path": "Workspaces/Demo/report.md"}],
    })
    assert _has_unprojected_local_artifacts({
        "files": [{"url": "/api/v1/fs/ent_1/Workspaces/Demo/report.md"}],
    })
    assert _has_unprojected_local_artifacts({
        "steps": [{"files": [{"fs_path": "Workspaces/Demo/legacy.md"}]}],
    })
    assert not _has_unprojected_local_artifacts({
        "files": [{
            "fs_path": "Workspaces/Demo/report.md",
            "document_id": "doc_1",
        }],
    })
