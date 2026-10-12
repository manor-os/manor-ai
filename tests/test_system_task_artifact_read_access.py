"""A user-less system task may read its own Workspace artifact store.

Strategist-proposal and scheduler-created Tasks execute with no User
(``resolve_task_execution_user_id`` returns ``None`` on trusted system
provenance). Until now ``unreadable_document_paths`` denied every path to
such an agent, while its writes went through — so a task could write
``recovered-prospect-list.csv`` and the next task could not read it back,
and ``read_file`` reported the denial as "File not found" (staging,
workspace 奶茶机营销, 2026-09-11: 9 of 12 tasks affected).

The rule added here is deliberately narrow, not a generic user-less bypass:
the trusted runtime Workspace's own artifact store, minus private,
restricted, quarantined and deleted-Workspace content. Everything else stays
denied exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.document_access import unreadable_document_paths
from tests.test_document_permissions import _auth


async def _me(client: AsyncClient, headers: dict) -> dict:
    return (await client.get("/api/v1/auth/me", headers=headers)).json()


def _folder(entity_id: str, name: str, **kwargs) -> DocumentFolder:
    return DocumentFolder(id=generate_ulid(), entity_id=entity_id, name=name, **kwargs)


def _doc(entity_id: str, name: str, fs_path: str, **kwargs) -> Document:
    return Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        file_type="csv",
        source="ai_generated",
        fs_path=fs_path,
        **kwargs,
    )


async def _blocked(db, *, entity_id: str, paths: list[str], workspace_id: str | None, dirs=None):
    return await unreadable_document_paths(
        db,
        entity_id=entity_id,
        rel_paths=paths,
        directory_paths=dirs,
        user_id=None,
        workspace_id=workspace_id,
        actor_type="agent",
    )


@pytest.mark.asyncio
async def test_user_less_agent_reads_only_its_own_workspace_artifact_store(client: AsyncClient):
    headers = await _auth(client, "systemtask_owner")
    me = await _me(client, headers)
    entity_id = me["entity_id"]
    owner_id = me["id"]

    async with db_module.async_session() as db:
        own_root = _folder(entity_id, "Own WS", visibility="entity")
        other_root = _folder(entity_id, "Other WS", visibility="entity")
        gone_root = _folder(entity_id, "Gone WS", visibility="entity")
        db.add_all([own_root, other_root, gone_root])
        await db.flush()
        own_ws = Workspace(entity_id=entity_id, name="Own WS", artifact_folder_id=own_root.id)
        other_ws = Workspace(entity_id=entity_id, name="Other WS", artifact_folder_id=other_root.id)
        gone_ws = Workspace(
            entity_id=entity_id, name="Gone WS", artifact_folder_id=gone_root.id,
            deleted_at=datetime.now(timezone.utc),
        )
        db.add_all([own_ws, other_ws, gone_ws])
        await db.flush()

        own_base = f"Workspaces/_by_id/{own_root.id}"
        entity_doc = f"{own_base}/documents/recovered-prospect-list.csv"
        unprojected = f"{own_base}/tasks/01TASK/documents/draft.md"
        own_dir = f"{own_base}/documents"
        private_doc = f"{own_base}/documents/private-notes.csv"
        restricted_doc = f"{own_base}/documents/restricted.csv"
        quarantined_doc = f"{own_base}/documents/quarantined.csv"
        other_doc = f"Workspaces/_by_id/{other_root.id}/documents/theirs.csv"
        gone_doc = f"Workspaces/_by_id/{gone_root.id}/documents/old.csv"
        outside = "docs/company-handbook.md"

        db.add_all([
            _doc(entity_id, "recovered-prospect-list.csv", entity_doc,
                 visibility="entity", owner_id=owner_id, folder_id=own_root.id),
            _doc(entity_id, "private-notes.csv", private_doc,
                 visibility="private", owner_id=owner_id, folder_id=own_root.id),
            _doc(entity_id, "restricted.csv", restricted_doc,
                 visibility="entity", classification="restricted", owner_id=owner_id,
                 folder_id=own_root.id),
            _doc(entity_id, "quarantined.csv", quarantined_doc,
                 visibility="entity", quarantine_status="quarantined", owner_id=owner_id,
                 folder_id=own_root.id),
            _doc(entity_id, "theirs.csv", other_doc,
                 visibility="entity", owner_id=owner_id, folder_id=other_root.id),
            _doc(entity_id, "old.csv", gone_doc,
                 visibility="entity", owner_id=owner_id, folder_id=gone_root.id),
        ])
        await db.commit()

        every_path = [
            entity_doc, unprojected, private_doc, restricted_doc,
            quarantined_doc, other_doc, gone_doc, outside,
        ]

        blocked = await _blocked(
            db, entity_id=entity_id, paths=every_path, dirs=[own_dir], workspace_id=own_ws.id,
        )
        assert entity_doc not in blocked
        assert unprojected not in blocked
        assert own_dir not in blocked
        assert {
            private_doc, restricted_doc, quarantined_doc, other_doc, gone_doc, outside,
        } <= blocked

        # Without a trusted Workspace scope nothing changes: still fail-closed.
        assert await _blocked(
            db, entity_id=entity_id, paths=every_path, dirs=[own_dir], workspace_id=None,
        ) == set(every_path) | {own_dir}

        # A Workspace from another entity, or a deleted one, grants nothing.
        gone_unprojected = f"Workspaces/_by_id/{gone_root.id}/tasks/01OLD/notes.md"
        assert await _blocked(
            db, entity_id=entity_id, paths=[gone_doc, gone_unprojected], workspace_id=gone_ws.id,
        ) == {gone_doc, gone_unprojected}
        assert await _blocked(
            db, entity_id=generate_ulid(), paths=[entity_doc], workspace_id=own_ws.id,
        ) == {entity_doc}
