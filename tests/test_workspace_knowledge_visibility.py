"""Regression: internal workspace knowledge search must honor Document.visibility.

`workspace_search(category="knowledge")` → `_search_knowledge` listed every
document in a workspace's Knowledge Nets (name + fs_path) with no per-user
filter, so a member could see another member's PRIVATE document placed in a
shared net. The internal path now filters by `user_can_read_document` when a
user_id is present.
"""

from __future__ import annotations

import pytest

import packages.core.database as db_module
from packages.core.models.user import User
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
        u = User(
            entity_id=entity_id, email=f"{name}@test.com", display_name=name,
            password_hash=hash_password("pass123"), role=role, status="active",
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    return uid


@pytest.mark.asyncio
async def test_workspace_knowledge_search_hides_private_docs_from_member():
    entity_id = "ent_ws_know_vis"
    owner_id = await _make_user(entity_id, "wsk_owner", "owner")
    member_id = await _make_user(entity_id, "wsk_member", "member")

    async with db_module.async_session() as db:
        ws = Workspace(entity_id=entity_id, name="Ops")
        db.add(ws)
        await db.flush()
        ws_id = ws.id

        # Both owner and member are workspace members: this test exercises the
        # per-document visibility filter, not the workspace-read gate (a
        # non-member is blocked earlier and is covered by
        # test_workspace_authz_holes).
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

    # As MEMBER: private doc name must not appear; shared doc does.
    assert "board-comp.md" not in out_member, f"private doc leaked to member:\n{out_member}"
    assert "handbook.md" in out_member, out_member
    # Owner sees both.
    assert "board-comp.md" in out_owner and "handbook.md" in out_owner, out_owner
