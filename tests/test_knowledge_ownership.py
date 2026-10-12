from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.core.models.document import Document, DocumentFolder
from packages.core.services import knowledge_ownership


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows


class _FakeDb:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        return _ScalarResult(self._results.pop(0))


def test_personal_folder_uniqueness_includes_owner_namespace():
    index_names = {index.name for index in DocumentFolder.__table__.indexes}
    assert "uq_document_folders_entity_parent_owner_name" in index_names
    assert "uq_document_folders_entity_parent_name" not in index_names


@pytest.mark.asyncio
async def test_same_named_personal_folder_paths_keep_both_owner_candidates():
    from packages.core.services.document_access import _document_folders_by_paths
    from packages.core.services.filesystem_access import folder_projections_for_path
    from packages.core.services.knowledge_sync import _find_folder_path_in_session

    engine = create_engine("sqlite://")
    DocumentFolder.__table__.create(engine)
    folders = [
        DocumentFolder(id="root-a", entity_id="entity-1", name="Reports", owner_id="user-a"),
        DocumentFolder(id="root-b", entity_id="entity-1", name="Reports", owner_id="user-b"),
        DocumentFolder(id="child-a", entity_id="entity-1", name="Plans", parent_id="root-a", owner_id="user-a"),
        DocumentFolder(id="child-b", entity_id="entity-1", name="Plans", parent_id="root-b", owner_id="user-b"),
    ]

    class AsyncSessionAdapter:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement):
            return self.session.execute(statement)

        async def scalars(self, statement):
            return self.session.scalars(statement)

    try:
        with Session(engine) as session:
            session.add_all(folders)
            session.commit()
            db = AsyncSessionAdapter(session)

            path_folders = await _document_folders_by_paths(
                db,
                entity_id="entity-1",
                paths={"Reports", "Reports/Plans"},
            )
            assert {folder.id for folder in path_folders["Reports"]} == {"root-a", "root-b"}
            assert {folder.id for folder in path_folders["Reports/Plans"]} == {"child-a", "child-b"}
            projected = await folder_projections_for_path(
                db,
                entity_id="entity-1",
                rel_path="Reports/Plans",
            )
            assert {folder.id for folder in projected} == {"child-a", "child-b"}
            assert await _find_folder_path_in_session(
                db,
                entity_id="entity-1",
                rel_path="Reports/Plans",
            ) is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_departed_user_knowledge_transfer_changes_ownership_not_authorship(monkeypatch):
    document = Document(
        id="document-1",
        entity_id="entity-1",
        name="private.md",
        owner_id="departed-user",
        created_by="departed-user",
        visibility="private",
    )
    folder = DocumentFolder(
        id="folder-1",
        entity_id="entity-1",
        name="Private folder",
        owner_id="departed-user",
        visibility="private",
    )
    db = _FakeDb([[folder], [document]])
    audit_calls = []

    async def _capture_audit(*args, **kwargs):
        audit_calls.append((args, kwargs))
        return SimpleNamespace(id="audit-1")

    async def _skip_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge_ownership, "log_action", _capture_audit)
    monkeypatch.setattr(knowledge_ownership, "bump_tool_cache_version", _skip_cache)

    result = await knowledge_ownership.transfer_departed_user_knowledge_to_entity(
        db,
        entity_id="entity-1",
        departing_user_id="departed-user",
        actor_user_id="owner-user",
    )

    assert result.document_count == 1
    assert result.folder_count == 1
    assert document.owner_id is None
    assert document.visibility == "entity"
    assert document.created_by == "departed-user"
    assert folder.owner_id is None
    assert folder.visibility == "entity"
    assert audit_calls[0][1]["action"] == "knowledge.ownership.transfer_to_entity"
    assert audit_calls[0][1]["user_id"] == "owner-user"


@pytest.mark.asyncio
async def test_departed_user_transfer_detaches_private_root_and_avoids_entity_name_collision(
    monkeypatch,
):
    document = Document(
        id="document-private",
        entity_id="entity-1",
        name="orphaned.md",
        folder_id="other-users-private-folder",
        owner_id="departed-user",
        visibility="private",
    )
    folder = DocumentFolder(
        id="folder-private",
        entity_id="entity-1",
        name="Reports",
        parent_id="other-users-private-folder",
        owner_id="departed-user",
        visibility="private",
    )
    existing = DocumentFolder(
        id="folder-entity",
        entity_id="entity-1",
        name="Reports",
        owner_id=None,
        visibility="entity",
    )
    db = _FakeDb([[folder, existing], [document]])

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge_ownership, "log_action", _noop)
    monkeypatch.setattr(knowledge_ownership, "bump_tool_cache_version", _noop)

    await knowledge_ownership.transfer_departed_user_knowledge_to_entity(
        db,
        entity_id="entity-1",
        departing_user_id="departed-user",
        actor_user_id="owner-user",
    )

    assert folder.parent_id is None
    assert folder.name == "Reports · transferred ted-user"
    assert folder.owner_id is None
    assert document.folder_id is None
    assert document.owner_id is None
    assert document.visibility == "entity"


@pytest.mark.asyncio
async def test_generated_personal_artifact_folder_keeps_user_owner(monkeypatch):
    from packages.core.services import generated_artifact_service, knowledge_sync

    captured = {}

    async def _ensure_folder_path(entity_id, rel_dir, *, owner_id=None, db=None):
        captured.update({
            "entity_id": entity_id,
            "rel_dir": rel_dir,
            "owner_id": owner_id,
            "db": db,
        })
        return "folder-personal"

    monkeypatch.setattr(knowledge_sync, "ensure_folder_path", _ensure_folder_path)

    folder_id = await generated_artifact_service.generated_artifact_document_folder_id(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id=None,
        rel_path="reports/summary.docx",
    )

    assert folder_id == "folder-personal"
    assert captured == {
        "entity_id": "entity-1",
        "rel_dir": "reports",
        "owner_id": "user-1",
        "db": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [None, "workspace-1"])
async def test_media_folder_projection_uses_shared_service_with_runtime_context(monkeypatch, workspace_id):
    from packages.core.ai.tools import media_tools
    from packages.core.services import knowledge_sync, workspace_artifacts

    folder_path = AsyncMock(return_value="personal-folder")
    workspace_folder = AsyncMock(return_value="workspace-folder")
    monkeypatch.setattr(knowledge_sync, "ensure_folder_path", folder_path)
    monkeypatch.setattr(workspace_artifacts, "ensure_workspace_document_folder", workspace_folder)

    result = await media_tools._artifact_document_folder_id(
        entity_id="entity-1", user_id="user-1", workspace_id=workspace_id,
        rel_path="production/final.mp4", conversation_id="conversation-1", task_id="task-1",
    )

    if workspace_id:
        assert result == "workspace-folder"
        workspace_folder.assert_awaited_once_with(
            entity_id="entity-1", workspace_id=workspace_id, rel_path="production/final.mp4",
        )
        folder_path.assert_not_awaited()
    else:
        assert result == "personal-folder"
        folder_path.assert_awaited_once_with("entity-1", "production", owner_id="user-1")
        workspace_folder.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "workspace_id", "target_workspace_id", "expected_visibility"),
    [
        ("user-1", None, None, "private"),
        ("user-1", "workspace-1", None, "private"),
        ("", "workspace-1", "workspace-1", "workspace"),
        ("", None, None, "private"),
    ],
)
async def test_generated_artifact_visibility_matches_its_owner_and_workspace(
    monkeypatch, user_id, workspace_id, target_workspace_id, expected_visibility,
):
    from packages.core import database
    from packages.core.services import (
        document_service,
        generated_artifact_service,
        workspace_artifacts,
    )

    captured = {}
    document = SimpleNamespace(
        id="generated-document",
        source=None,
        created_by=None,
        metadata_=None,
    )

    class _GeneratedArtifactDb:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        async def commit(self):
            return None

    class _SessionContext:
        async def __aenter__(self):
            return _GeneratedArtifactDb()

        async def __aexit__(self, *_args):
            return None

    async def _folder_id(**_kwargs):
        return "folder-personal"

    async def _infer_workspace(**_kwargs):
        return target_workspace_id

    async def _capture_upsert(db, entity_id, **kwargs):
        captured.update({"db": db, "entity_id": entity_id, **kwargs})
        return document

    monkeypatch.setattr(database, "async_session", _SessionContext)
    monkeypatch.setattr(workspace_artifacts, "infer_workspace_id_from_storage_path", _infer_workspace)
    monkeypatch.setattr(
        generated_artifact_service,
        "generated_artifact_document_folder_id",
        _folder_id,
    )
    monkeypatch.setattr(
        document_service,
        "upsert_document_by_fs_path",
        _capture_upsert,
    )

    document_id = await generated_artifact_service.register_generated_file_artifact(
        entity_id="entity-1",
        user_id=user_id,
        filename="generated.docx",
        rel_path="reports/generated.docx",
        file_size=128,
        file_type="document",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        workspace_id=workspace_id,
        task_id=None,
        agent_id="agent-1",
        conversation_id="conversation-1",
        tool_name="generate_file",
        artifact_role="document",
        generation={"prompt": "Create a report"},
    )

    assert document_id == "generated-document"
    assert captured["entity_id"] == "entity-1"
    assert captured["owner_id"] == (user_id or None)
    assert captured["created_by"] == (user_id or None)
    assert captured["visibility"] == expected_visibility
    assert captured["source"] == "ai_generated"
    assert captured["folder_id"] == "folder-personal"
