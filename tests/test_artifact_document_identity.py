from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest

from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    canonical_fs_path,
    dedupe_generated_file_refs,
)


NAME = "AI_SDE_20小时课程_Agenda与课程内容.md"


@pytest.mark.parametrize("name", ["report#1.md", "report?2.md", "report%23.md", "report%2520.md", "report%2F.md", "报告 1.md"])
def test_literal_file_paths_and_url_paths_decode_at_different_boundaries(name):
    path = f"Workspaces/W/{name}"
    address = f"/api/v1/fs/ent_1/{quote(path)}"
    factory = ArtifactReferenceFactory(entity_id="ent_1")
    ref = {"name": name, "fs_path": path}
    for _ in range(3):
        ref = factory.create(ref)
        assert ref["fs_path"] == path
        assert ref["open_url"] == address
    assert canonical_fs_path(address) == path
    assert canonical_fs_path(canonical_fs_path(address)) == path
    assert len(dedupe_generated_file_refs([ref, {"url": address}])) == 1


def test_path_normalization_does_not_merge_literal_url_characters():
    names = ["report#1.md", "report#2.md", "report%23.md", "report%2520.md", "report%20.md", "report .md"]
    refs = [{"fs_path": f"Workspaces/W/{name}"} for name in names]
    assert len(dedupe_generated_file_refs(refs, entity_id="ent_1")) == len(refs)
    for unsafe in [
        "../report.md",
        "/home/me/report.md",
        "/root/report.md",
        "/viewer/doc_1",
        "/api/v1/fs/ent_1/%2E%2E/report.md",
    ]:
        assert canonical_fs_path(unsafe) == ""


@pytest.mark.parametrize("preview", ["https://cdn.example/shared-cover.png", "data:image/png;base64,AAAA", "/api/v1/fs/ent_1/shared-cover.png"])
@pytest.mark.parametrize("field", ["previewUrl", "preview_url"])
@pytest.mark.parametrize("primary", ["document_id", "fs_path", "open_url"])
def test_preview_is_not_identity_when_a_primary_file_address_exists(preview, field, primary):
    values = {
        "document_id": ("doc_a", "doc_b"),
        "fs_path": ("Workspaces/W/a.pdf", "Workspaces/W/b.pdf"),
        "open_url": ("https://cdn.example/a.pdf", "https://cdn.example/b.pdf"),
    }[primary]
    refs = [{primary: value, field: preview} for value in values]
    result = dedupe_generated_file_refs(refs, entity_id="ent_1")
    assert len(result) == 2
    for ref in result:
        assert ref[field] == preview  # Display metadata survives.
        assert ref.get("fs_path") != "shared-cover.png"
        assert ref["open_url"] != preview


@pytest.mark.parametrize("field", ["open_url", "viewer_url", "url"])
@pytest.mark.parametrize("reverse", [False, True])
def test_viewer_address_matches_known_document_without_inventing_document_id(field, reverse):
    viewer = {field: "/viewer/doc_1?download=1#page-2", "name": "report.md"}
    assert "document_id" not in ArtifactReferenceFactory().create(viewer)
    refs = [{"document_id": "doc_1", "name": "report.md"}, viewer]
    result = dedupe_generated_file_refs(list(reversed(refs)) if reverse else refs)
    assert len(result) == 1
    assert result[0]["document_id"] == "doc_1"
    assert result[0]["open_url"] == "/viewer/doc_1"
    assert len(dedupe_generated_file_refs([
        {"document_id": "doc_1"}, {field: "https://other.example/viewer/doc_1"},
    ])) == 2


@pytest.mark.parametrize("field", ["previewUrl", "preview_url"])
def test_chat_preview_addresses_share_canonical_file_identity(field):
    factory = ArtifactReferenceFactory(entity_id="ent_1")
    address = f"/api/v1/fs/ent_1/Workspaces/Demo/{quote(NAME)}"
    ref = factory.create({"name": NAME, field: address})
    assert ref["fs_path"] == f"Workspaces/Demo/{NAME}"
    assert ref["open_url"] == address


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
