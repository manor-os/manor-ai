"""Merge uses real authorization, filesystem and PostgreSQL projection.

Media processing is not invoked: tests submit synthetic staged bytes to the
same commit boundary used after the renderer finishes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core import database
from packages.core.ai.runtime import file_actions
from packages.core.ai.runtime.media_outputs import runtime_generated_media_target
from packages.core.ai.tools import media_tools
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.knowledge_sync import KnowledgeSyncResult
from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
from tests.test_generated_media_authorization_contract import _approve, _authorized


async def _scope(db, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(file_actions, "runtime_trigger_document_embeddings", lambda _id: None)
    entity_id, user_id, workspace_id = (generate_ulid() for _ in range(3))
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Merge regression")
    db.add_all([
        Entity(id=entity_id, name="Merge regression"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.invalid", password_hash="unused", role="owner", status="active"),
        UserMembership(entity_id=entity_id, user_id=user_id, role="owner", status="active"),
        workspace,
    ])
    await db.flush()
    folder = await ensure_workspace_artifact_folder(db, workspace)
    await db.commit()
    root = tmp_path / entity_id
    root.mkdir(exist_ok=True)
    name = "Workspaces/Merge regression/Videos/final.mp4"
    request, receipt = await _approve(
        name, tool_name="merge_videos", task_id=None,
        entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
    )
    with _authorized(request, receipt):
        target = runtime_generated_media_target(
            kind="video", entity_id=entity_id,
            workspace_base_dir=f"Workspaces/_by_id/{folder.id}",
            ext=".mp4", prompt=name, output_name=name, entity_root=str(root),
        )
    staged = tmp_path / f"{entity_id}-staged.mp4"
    staged.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00synthetic regression bytes")
    kwargs = dict(
        entity_id=entity_id, user_id=user_id, entity_root=str(root), target=target,
        source_path=str(staged), workspace_id=workspace_id, task_id=None,
        agent_id=None, conversation_id=None,
        generation={"operation": "merge_videos", "inputs": [{"fs_path": "unchanged-input.mp4"}]},
    )
    return request, receipt, root, staged, kwargs


@pytest.mark.asyncio
async def test_merge_commit_returns_document_only_after_file_and_projection(db_session, tmp_path, monkeypatch):
    request, receipt, root, staged, kwargs = await _scope(db_session, tmp_path, monkeypatch)
    with _authorized(request, receipt):
        document_id = await media_tools._commit_merged_video_output(**kwargs)
    document = await db_session.get(Document, document_id)
    assert document is not None
    assert document.fs_path == kwargs["target"].rel_path
    assert (root / document.fs_path).read_bytes() == staged.read_bytes()
    assert document.mime_type == "video/mp4"
    assert document.metadata_["origin"]["workspace_id"] == kwargs["workspace_id"]
    assert document.metadata_["generation"] == kwargs["generation"]


@pytest.mark.asyncio
async def test_merge_projection_failure_rolls_back_new_output(db_session, tmp_path, monkeypatch):
    request, receipt, root, staged, kwargs = await _scope(db_session, tmp_path, monkeypatch)
    monkeypatch.setattr(file_actions, "runtime_sync_entity_file_to_knowledge", AsyncMock(
        return_value=KnowledgeSyncResult(False, reason="injected_projection_failure"),
    ))
    with _authorized(request, receipt):
        with pytest.raises(file_actions.RuntimeFileProjectionError, match="injected_projection_failure"):
            await media_tools._commit_merged_video_output(**kwargs)
    assert not (root / kwargs["target"].rel_path).exists()
    assert staged.read_bytes()
    documents = list((await db_session.scalars(select(Document).where(Document.entity_id == kwargs["entity_id"]))).all())
    assert documents == []


@pytest.mark.asyncio
async def test_merge_concurrent_create_never_overwrites_the_winner(db_session, tmp_path, monkeypatch):
    request, receipt, root, staged, kwargs = await _scope(db_session, tmp_path, monkeypatch)
    with _authorized(request, receipt):
        results = await asyncio.gather(
            media_tools._commit_merged_video_output(**kwargs),
            media_tools._commit_merged_video_output(**kwargs),
            return_exceptions=True,
        )
    assert sum(isinstance(result, str) for result in results) == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1
    assert "appeared" in str(failures[0])
    assert (root / kwargs["target"].rel_path).read_bytes() == staged.read_bytes()
    documents = list((await db_session.scalars(select(Document).where(Document.entity_id == kwargs["entity_id"]))).all())
    assert len(documents) == 1
