from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest

from packages.core.services.generated_file_refs import ArtifactReferenceFactory


NAME = "AI_SDE_20小时课程_Agenda与课程内容.md"


@pytest.mark.parametrize("bad_id", [NAME, quote(NAME), quote(quote(NAME)), "../report.md", "/viewer/doc_1"])
def test_filename_is_not_a_document_identity(bad_id):
    factory = ArtifactReferenceFactory(entity_id="ent_1")
    ref = factory.create({
        "document_id": bad_id, "fs_path": f"Workspaces/Demo/{NAME}",
        "viewer_url": f"/viewer/{quote(bad_id)}", "name": NAME,
    })
    assert factory.inspect({"document_id": bad_id}).document_id == ""
    assert "document_id" not in ref
    assert "viewer_url" not in ref
    assert ref["open_url"] == f"/api/v1/fs/ent_1/Workspaces/Demo/{quote(NAME)}"


@pytest.mark.asyncio
async def test_bad_id_with_real_file_is_projected_to_real_document(tmp_path, monkeypatch):
    from packages.core.services import artifact_knowledge

    path = tmp_path / NAME
    path.write_text("# Interview curriculum", encoding="utf-8")
    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _: str(tmp_path))
    sync = AsyncMock(return_value=SimpleNamespace(synced=True, document_id="doc_real", reason=None))
    bind = AsyncMock()
    monkeypatch.setattr(artifact_knowledge, "sync_file_to_knowledge", sync)
    monkeypatch.setattr(artifact_knowledge, "bind_document_to_workspace", bind)
    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id="ent_1", workspace_id="ws_1", task_id="task_1",
        refs=[{"name": NAME, "document_id": quote(NAME), "fs_path": NAME}],
    )
    assert projection.failures == []
    assert projection.refs[0]["document_id"] == "doc_real"
    assert projection.refs[0]["open_url"] == "/viewer/doc_real"
    assert projection.knowledge_artifacts[0]["document_id"] == "doc_real"
    assert sync.call_args.kwargs["workspace_id"] == "ws_1"
    bind.assert_not_called()


@pytest.mark.asyncio
async def test_bad_id_without_materialized_artifact_is_not_a_knowledge_artifact(tmp_path, monkeypatch):
    from packages.core.services import artifact_knowledge

    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _: str(tmp_path))
    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id="ent_1", workspace_id="ws_1",
        refs=[{"name": NAME, "document_id": quote(NAME), "viewer_url": f"/viewer/{quote(quote(NAME))}"}],
    )
    assert projection.knowledge_artifacts == []
    # There is no storage path to project; do not invent a Document or file.
    assert projection.failures == []
    assert "document_id" not in projection.refs[0]
    assert "viewer_url" not in projection.refs[0]
    assert "open_url" not in projection.refs[0]


def test_completion_attachment_never_promotes_a_filename_to_document_id():
    from packages.core.workspace_chat.notifiers import extract_artifacts_for_chat, _artifacts_as_attachments

    artifacts = extract_artifacts_for_chat({
        "name": NAME, "document_id": quote(NAME), "fs_path": f"Workspaces/Demo/{NAME}",
    })
    attachments = _artifacts_as_attachments(artifacts, entity_id="ent_1")
    assert len(attachments) == 1
    assert "document_id" not in attachments[0]
    assert attachments[0]["previewUrl"].startswith("/api/v1/fs/ent_1/Workspaces/Demo/")


def test_existing_task_with_filename_id_still_requires_artifact_projection():
    from packages.core.services.task_execution_reconcile import _has_unprojected_local_artifacts

    assert _has_unprojected_local_artifacts({
        "files": [{"document_id": quote(NAME), "fs_path": f"Workspaces/Demo/{NAME}"}],
    })
    assert not _has_unprojected_local_artifacts({
        "files": [{"document_id": "doc_real", "fs_path": f"Workspaces/Demo/{NAME}"}],
    })
