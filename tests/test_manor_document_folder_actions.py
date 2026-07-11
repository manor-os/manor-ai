from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_manor_can_create_folder_and_move_documents(client):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.services.document_service import create_document, get_document

    entity_id = "ent_folder_actions"

    async with db_module.async_session() as db:
        doc_a = await create_document(
            db,
            entity_id,
            name="video-plan.md",
            file_type="md",
            source="upload",
        )
        doc_b = await create_document(
            db,
            entity_id,
            name="thumbnail-brief.md",
            file_type="md",
            source="upload",
        )
        await db.commit()
        doc_a_id = doc_a.id
        doc_b_id = doc_b.id

    created = json.loads(
        await _dispatch_action(
            "create_document_folder",
            {"name": "YouTube Channel"},
            entity_id,
        )
    )
    assert created["created"] is True
    folder_id = created["folder"]["id"]
    assert created["folder"]["name"] == "YouTube Channel"

    moved = json.loads(
        await _dispatch_action(
            "move_documents_to_folder",
            {"document_ids": [doc_a_id, doc_b_id], "folder_id": folder_id},
            entity_id,
        )
    )
    assert moved["moved_count"] == 2
    assert moved["missing_ids"] == []

    listed = json.loads(
        await _dispatch_action(
            "list_document_folders",
            {},
            entity_id,
        )
    )
    assert listed["count"] == 1
    assert listed["folders"][0]["document_count"] == 2

    async with db_module.async_session() as db:
        assert (await get_document(db, doc_a_id, entity_id)).folder_id == folder_id
        assert (await get_document(db, doc_b_id, entity_id)).folder_id == folder_id


@pytest.mark.asyncio
async def test_manor_move_documents_to_folder_moves_filesystem_payload(client, tmp_path):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.config import get_settings
    from packages.core.services.document_service import create_document, get_document

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)

    try:
        entity_id = "ent_folder_fs_actions"
        entity_root = tmp_path / entity_id
        old_file = entity_root / "Old" / "brief.md"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("# Brief\n", encoding="utf-8")

        async with db_module.async_session() as db:
            doc = await create_document(
                db,
                entity_id,
                name="brief.md",
                fs_path="Old/brief.md",
                file_size=old_file.stat().st_size,
                file_type="md",
                mime_type="text/markdown",
                source="upload",
            )
            await db.commit()
            doc_id = doc.id

        created = json.loads(
            await _dispatch_action(
                "create_document_folder",
                {"name": "New"},
                entity_id,
            )
        )
        folder_id = created["folder"]["id"]

        moved = json.loads(
            await _dispatch_action(
                "move_documents_to_folder",
                {"document_ids": [doc_id], "folder_id": folder_id},
                entity_id,
            )
        )

        assert moved["moved_count"] == 1
        assert moved["filesystem_moved_count"] == 1
        assert moved["missing_file_ids"] == []
        assert not old_file.exists()
        assert (entity_root / "New" / "brief.md").is_file()
        assert moved["documents"][0]["fs_path"] == "New/brief.md"

        async with db_module.async_session() as db:
            stored = await get_document(db, doc_id, entity_id)
            assert stored.folder_id == folder_id
            assert stored.fs_path == "New/brief.md"
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


@pytest.mark.asyncio
async def test_manor_search_finds_knowledge_folder_actions():
    from packages.core.ai.tools.manor_tool import _manor_handler

    result = json.loads(
        await _manor_handler(
            entity_id="ent_folder_actions",
            action="search",
            query="create knowledge folder move documents",
        )
    )
    actions = {item["action"] for item in result["matches"]}

    assert "create_document_folder" in actions
    assert "move_documents_to_folder" in actions


@pytest.mark.asyncio
async def test_manor_create_document_folder_accepts_nested_path(client):
    from packages.core.ai.tools.manor_tool import _dispatch_action

    entity_id = "ent_nested_folder_actions"

    created = json.loads(
        await _dispatch_action(
            "create_document_folder",
            {"name": "Series/openings/op-01-Opening"},
            entity_id,
        )
    )

    assert created["created"] is True
    assert created["folder"]["name"] == "op-01-Opening"
    assert created["folder"]["path"] == "Series/openings/op-01-Opening"
    assert [folder["name"] for folder in created["created_folders"]] == [
        "Series",
        "openings",
        "op-01-Opening",
    ]

    repeated = json.loads(
        await _dispatch_action(
            "create_document_folder",
            {"name": "Series/openings/op-01-Opening"},
            entity_id,
        )
    )

    assert repeated["created"] is False
    assert repeated["existing"] is True
    assert repeated["folder"]["path"] == "Series/openings/op-01-Opening"


@pytest.mark.asyncio
async def test_manor_lists_workspace_artifacts_from_document_provenance(client):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import create_document

    entity_id = "ent_workspace_artifacts"
    workspace_id = "ws_artifacts"

    async with db_module.async_session() as db:
        artifact = await create_document(
            db,
            entity_id,
            name="generated-recap.md",
            fs_path="generated-recap.md",
            file_type="md",
            source="agent",
            metadata=merge_document_metadata(
                origin={
                    "workspace_id": workspace_id,
                    "task_id": "task_1",
                    "agent_id": "agent_1",
                    "conversation_id": "conv_1",
                    "tool_name": "write_file",
                },
                artifact={"role": "final"},
            ),
        )
        await create_document(
            db,
            entity_id,
            name="other-workspace.md",
            fs_path="other-workspace.md",
            file_type="md",
            source="agent",
            metadata=merge_document_metadata(
                origin={"workspace_id": "ws_other"},
                artifact={"role": "final"},
            ),
        )
        await db.commit()
        artifact_id = artifact.id

    listed = json.loads(
        await _dispatch_action(
            "list_workspace_artifacts",
            {"workspace_id": workspace_id, "limit": 10},
            entity_id,
        )
    )

    assert listed["workspace_id"] == workspace_id
    assert listed["count"] == 1
    assert listed["artifacts"][0]["id"] == artifact_id
    assert listed["artifacts"][0]["fs_path"] == "generated-recap.md"
    assert listed["artifacts"][0]["task_id"] == "task_1"
    assert listed["artifacts"][0]["agent_id"] == "agent_1"


@pytest.mark.asyncio
async def test_manor_infers_workspace_artifacts_from_task_id(client):
    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import create_document

    entity_id = "ent_ws_artifact_task"
    workspace_id = "ws_artifact_task"
    task_id = "task_artifact_task"

    async with db_module.async_session() as db:
        db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Artifact Workspace"))
        db.add(Task(id=task_id, entity_id=entity_id, workspace_id=workspace_id, title="Make artifact"))
        artifact = await create_document(
            db,
            entity_id,
            name="task-output.md",
            fs_path="task-output.md",
            file_type="md",
            source="agent",
            metadata=merge_document_metadata(
                origin={"workspace_id": workspace_id, "task_id": task_id},
                artifact={"role": "final"},
            ),
        )
        await db.commit()
        artifact_id = artifact.id

    listed = json.loads(
        await _dispatch_action(
            "list_workspace_artifacts",
            {"task_id": task_id, "limit": 10},
            entity_id,
        )
    )

    assert listed["workspace_id"] == workspace_id
    assert listed["count"] == 1
    assert listed["artifacts"][0]["id"] == artifact_id
