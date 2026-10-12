"""Regression: Workspace search follows explicit links and staff access.

A document placed in a Workspace Knowledge Net is explicitly shared with
Workspace staff. An artifact origin tag alone does not share a private file;
its name stays out of member search results.
"""

from __future__ import annotations

import pytest

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import (
    add_document_to_group,
    create_document,
    create_group,
)
from packages.core.workspace_chat.context import workspace_search


async def _make_user(entity_id: str, name: str, role: str) -> str:
    async with db_module.async_session() as db:
        if await db.get(Entity, entity_id) is None:
            db.add(Entity(id=entity_id, name=f"{name} test entity"))
            await db.flush()
        u = User(
            entity_id=entity_id,
            email=f"{name}-{entity_id.lower()}@test.com",
            display_name=name,
            password_hash=hash_password("pass123"), role=role, status="active",
        )
        db.add(u)
        await db.flush()
        db.add(
            UserMembership(
                user_id=u.id,
                entity_id=entity_id,
                role=role,
                status="active",
                is_primary=True,
            )
        )
        uid = u.id
        await db.commit()
    return uid


@pytest.mark.asyncio
async def test_workspace_knowledge_search_shares_group_docs_with_staff():
    entity_id = generate_ulid()
    owner_id = await _make_user(entity_id, "wsk_owner", "owner")
    member_id = await _make_user(entity_id, "wsk_member", "member")
    outsider_id = await _make_user(entity_id, "wsk_outsider", "member")

    async with db_module.async_session() as db:
        ws = Workspace(entity_id=entity_id, name="Ops")
        db.add(ws)
        await db.flush()
        ws_id = ws.id

        # The document owner and viewer can both read explicitly shared group
        # members. A same-entity user outside this Workspace cannot search it.
        from packages.core.models.workspace import WorkspaceStaff
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=owner_id,
            role="owner", status="active",
        ))
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=member_id,
            role="viewer", status="active",
        ))

        priv = await create_document(
            db, entity_id, name="board-comp.md", fs_path="board-comp.md",
            file_type="md", source="upload", visibility="private", owner_id=owner_id,
        )
        shared = await create_document(
            db, entity_id, name="handbook.md", fs_path="handbook.md",
            file_type="md", source="upload", visibility="entity", owner_id=owner_id,
        )
        net = await create_group(db, entity_id, name="Ops Net", workspace_id=ws_id)
        await add_document_to_group(db, priv.id, net.id, entity_id=entity_id)
        await add_document_to_group(db, shared.id, net.id, entity_id=entity_id)
        await db.commit()

    async with db_module.async_session() as db:
        out_member = await workspace_search(
            db, ws_id, entity_id, category="knowledge", user_id=member_id,
        )
        out_owner = await workspace_search(
            db, ws_id, entity_id, category="knowledge", user_id=owner_id,
        )
        out_outsider = await workspace_search(
            db, ws_id, entity_id, category="knowledge", user_id=outsider_id,
        )

    assert "board-comp.md" in out_member and "handbook.md" in out_member, out_member
    assert "Ops Net** (2 docs)" in out_member, out_member
    assert "board-comp.md" in out_owner and "handbook.md" in out_owner, out_owner
    assert out_outsider == "Workspace not found."


@pytest.mark.asyncio
async def test_workspace_artifact_search_resolves_document_id_within_workspace():
    entity_id = generate_ulid()
    owner_id = await _make_user(entity_id, "artifact_owner", "owner")

    async with db_module.async_session() as db:
        workspace = Workspace(entity_id=entity_id, name="Release A")
        other_workspace = Workspace(entity_id=entity_id, name="Release B")
        db.add_all([workspace, other_workspace])
        await db.flush()
        from packages.core.models.workspace import WorkspaceStaff
        db.add(WorkspaceStaff(
            workspace_id=workspace.id, user_id=owner_id,
            role="owner", status="active",
        ))
        current = await create_document(
            db, entity_id, name="current.md", fs_path="release-a/current.md",
            metadata={"origin": {"workspace_id": workspace.id}},
            visibility="entity", owner_id=owner_id,
        )
        foreign = await create_document(
            db, entity_id, name="foreign.md", fs_path="release-b/foreign.md",
            metadata={"origin": {"workspace_id": other_workspace.id}},
            visibility="entity", owner_id=owner_id,
        )
        workspace_id, current_id, foreign_id = workspace.id, current.id, foreign.id
        await db.commit()

    async with db_module.async_session() as db:
        found = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query=current_id, user_id=owner_id,
        )
        described = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query=f"Release State Document ID {current_id}", user_id=owner_id,
        )
        foreign_result = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query=f"Foreign Document ID {foreign_id}", user_id=owner_id,
        )

    assert f"document_id={current_id}" in found
    assert "fs_path=release-a/current.md" in found
    assert f"document_id={current_id}" in described
    assert "foreign.md" not in foreign_result


@pytest.mark.asyncio
async def test_workspace_artifact_search_hides_private_document_from_member():
    entity_id = generate_ulid()
    owner_id = await _make_user(entity_id, "artifact_private_owner", "owner")
    member_id = await _make_user(entity_id, "artifact_private_member", "member")

    async with db_module.async_session() as db:
        workspace = Workspace(entity_id=entity_id, name="Private Release")
        db.add(workspace)
        await db.flush()
        from packages.core.models.workspace import WorkspaceStaff
        db.add(WorkspaceStaff(
            workspace_id=workspace.id, user_id=member_id,
            role="viewer", status="active",
        ))
        private = await create_document(
            db, entity_id, name="secret-master.wav", fs_path="release/secret-master.wav",
            metadata={"origin": {"workspace_id": workspace.id}},
            visibility="private", owner_id=owner_id,
        )
        shared = await create_document(
            db, entity_id, name="shared-master.wav", fs_path="release/shared-master.wav",
            metadata={"origin": {"workspace_id": workspace.id}},
            visibility="entity", owner_id=owner_id,
        )
        workspace_id, private_id, shared_id = workspace.id, private.id, shared.id
        await db.commit()

    async with db_module.async_session() as db:
        by_name = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query="master.wav", user_id=member_id,
        )
        by_private_id = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query=f"Private Document ID {private_id}", user_id=member_id,
        )
        by_shared_id = await workspace_search(
            db, workspace_id, entity_id, category="artifacts",
            query=shared_id, user_id=member_id,
        )

    assert "secret-master.wav" not in by_name
    assert private_id not in by_private_id
    assert f"document_id={shared_id}" in by_shared_id
