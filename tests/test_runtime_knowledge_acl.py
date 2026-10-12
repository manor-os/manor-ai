"""Fail-closed checks for the composite Knowledge tool boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_userless_runtime_knowledge_actions_fail_closed():
    from packages.core.ai.runtime.manor_actions import (
        runtime_manor_create_document_folder,
        runtime_manor_delete_document,
        runtime_manor_delete_document_folder,
        runtime_manor_list_document_folders,
        runtime_manor_list_document_groups,
        runtime_manor_move_documents_to_folder,
        runtime_manor_move_document_folder,
        runtime_manor_rename_document_folder,
    )

    db = object()
    for action in (
        runtime_manor_create_document_folder,
        runtime_manor_delete_document,
        runtime_manor_delete_document_folder,
        runtime_manor_list_document_folders,
        runtime_manor_list_document_groups,
        runtime_manor_move_documents_to_folder,
        runtime_manor_move_document_folder,
        runtime_manor_rename_document_folder,
    ):
        result = json.loads(await action(db, entity_id="entity-1"))
        assert "User context is required" in result["error"]


@pytest.mark.asyncio
async def test_folder_delete_rejects_unmanageable_child(monkeypatch):
    from packages.core.ai.runtime import manor_actions

    parent = SimpleNamespace(id="parent", parent_id=None)
    child = SimpleNamespace(id="child", parent_id="parent")
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [parent, child]))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))

    async def can_manage(_db, *, folder, **_kwargs):
        return folder.id == "parent"

    monkeypatch.setattr(manor_actions, "_runtime_user_can_manage_folder", can_manage)

    result = json.loads(await manor_actions.runtime_manor_delete_document_folder(
        db,
        entity_id="entity-1",
        user_id="member-1",
        params={"folder_id": "parent"},
    ))

    assert result["error"] == "A child folder is not deletable"
    assert db.execute.await_count == 1
